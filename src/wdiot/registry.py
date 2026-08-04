"""High-level capability registry built on top of :mod:`wdiot.dds`.

A :class:`Hub` owns one ``DomainParticipant`` and brokers:

* registry broadcasts on ``wdiot/registry`` (RELIABLE + TRANSIENT_LOCAL)
* per-capability-type request topics ``wdiot/capability/<type>`` (RELIABLE)
* per-call reply topics ``wdiot/reply/<uuid>`` (RELIABLE)

Instance names (which may be non-ASCII) only ever appear inside JSON
payloads — DDS topic names are ASCII-safe.

Network operations return ``concurrent.futures.Future``. Local setup
(:func:`connect`, :func:`register`, :func:`start_scan`, :func:`stop_scan`)
is synchronous.
"""

from __future__ import annotations

import json
import threading
import uuid
import warnings
from concurrent.futures import Future
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Callable

from . import dds
from .event import Event

if TYPE_CHECKING:
    from .capabilities import Capability


_REGISTRY_TOPIC = "wdiot/registry"
_CAPABILITY_TOPIC_PREFIX = "wdiot/capability/"
_REPLY_TOPIC_PREFIX = "wdiot/reply/"

ScanCallback = Callable[["Device", Event], None]


class Hub:
    """Singleton coordinator for one wdiot process. Built by :func:`connect`."""

    _PUMP_INTERVAL = 0.05

    def __init__(self, domain_id: int = 0) -> None:
        self._domain_id = domain_id
        self._self_name: str | None = None
        self._stopped = threading.Event()

        self._participant = dds.Participant(domain_id=domain_id)
        self._publisher = self._participant.create_publisher()
        self._subscriber = self._participant.create_subscriber()

        self._t_registry = self._participant.register_string_type("registry")
        self._t_cap = self._participant.register_string_type("cap")
        self._t_reply = self._participant.register_string_type("reply")

        self._registry_topic = self._participant.create_topic(
            _REGISTRY_TOPIC,
            self._t_registry,
            dds.transient_local_reader_qos(),
        )
        self._registry_writer = self._publisher.create_writer(
            self._registry_topic,
            dds.transient_local_writer_qos(),
        )
        self._registry_reader = self._subscriber.create_reader(
            self._registry_topic,
            dds.transient_local_reader_qos(),
        )

        self._pump_threads: list[threading.Thread] = []
        self._start_pump(self._registry_reader, self._dispatch_registry)

        self._cap_topics: dict[str, Any] = {}
        self._cap_writers: dict[str, Any] = {}
        self._cap_readers: dict[str, Any] = {}

        self._reply_writers: dict[str, Any] = {}
        self._reply_writers_lock = threading.Lock()

        self._local_caps: dict[type, dict[str, Capability]] = {}
        self._local_device: Device | None = None

        self._remote_devices: dict[str, Device] = {}
        self._seen_registry: dict[str, tuple] = {}
        self._remote_lock = threading.Lock()

        self._scan_callbacks: list[ScanCallback] = []
        self._scan_lock = threading.Lock()

        self._pending: dict[str, Future] = {}
        self._pending_lock = threading.Lock()

        self._attach_participant_listener()

    # ------------------------------------------------------------------ pumps

    def _start_pump(self, reader: Any, dispatch: Callable[[Any], None]) -> None:
        t = threading.Thread(
            target=self._pump_loop,
            args=(reader, dispatch),
            daemon=True,
            name="wdiot-pump",
        )
        t.start()
        self._pump_threads.append(t)

    def _pump_loop(self, reader: Any, dispatch: Callable[[Any], None]) -> None:
        while not self._stopped.is_set():
            try:
                samples = reader.take(max_samples=50)
            except Exception:
                samples = []
            for sample in samples:
                try:
                    dispatch(sample)
                except Exception as exc:
                    warnings.warn(f"wdiot pump dispatch error: {exc!r}", stacklevel=2)
            self._stopped.wait(timeout=self._PUMP_INTERVAL)

    # ------------------------------------------------------------------ public

    def register(self, name: str, capabilities: list[Capability]) -> None:
        if self._local_device is not None:
            raise RuntimeError("device already registered in this process")
        device = Device(name=name, hub=self)
        self._local_device = device
        self._self_name = name

        for cap in capabilities:
            if cap._target is not None:
                raise ValueError(f"capability {cap!r} looks like a remote proxy")
            t = cap.capability_type
            if not t:
                raise ValueError(f"{type(cap).__name__}.capability_type is empty")
            bucket = self._local_caps.setdefault(type(cap), {})
            if cap.name in bucket:
                raise ValueError(f"duplicate capability {cap.name!r} for {t}")
            bucket[cap.name] = cap
            self._ensure_capability_endpoint(t)

        self._publish_registry()

    def start_scan(self, callback: ScanCallback) -> None:
        with self._scan_lock:
            if callback not in self._scan_callbacks:
                self._scan_callbacks.append(callback)
        with self._remote_lock:
            snapshot = list(self._remote_devices.values())
        for dev in snapshot:
            try:
                callback(dev, Event(kind="added", capability=None))
            except Exception as exc:
                warnings.warn(f"wdiot scan replay error: {exc!r}", stacklevel=2)

    def stop_scan(self, callback: ScanCallback) -> None:
        with self._scan_lock, suppress(ValueError):
            self._scan_callbacks.remove(callback)

    def invoke(
        self,
        *,
        target_device: str,
        cap_type: str,
        instance_name: str,
        payload: Any,
        timeout: float | None = None,
    ) -> Future:
        request_id = uuid.uuid4().hex
        reply_topic = f"{_REPLY_TOPIC_PREFIX}{request_id}"
        fut: Future = Future()
        self._install_reply_reader(reply_topic, request_id, fut)

        writer = self._cap_writers.get(cap_type)
        if writer is None:
            fut.set_exception(RuntimeError(f"unknown capability type {cap_type!r}"))
            return fut

        msg = {
            "from": self._self_name or "",
            "to": target_device,
            "type": cap_type,
            "instance": instance_name,
            "id": request_id,
            "payload": payload,
            "reply_topic": reply_topic,
        }
        try:
            writer.write(json.dumps(msg, ensure_ascii=False))
        except Exception as exc:
            fut.set_exception(exc)
        return fut

    def close(self) -> None:
        self._stopped.set()
        for t in self._pump_threads:
            t.join(timeout=0.5)
        with suppress(Exception):
            self._participant.close()

    def __enter__(self) -> Hub:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ internal

    def _ensure_capability_endpoint(self, cap_type: str) -> None:
        if cap_type in self._cap_topics:
            return
        topic_name = f"{_CAPABILITY_TOPIC_PREFIX}{cap_type}"
        topic = self._participant.create_topic(topic_name, self._t_cap, dds.topic_qos())
        writer = self._publisher.create_writer(topic, dds.reliable_writer_qos())
        reader = self._subscriber.create_reader(topic, dds.reliable_reader_qos())
        self._cap_topics[cap_type] = topic
        self._cap_writers[cap_type] = writer
        self._cap_readers[cap_type] = reader
        self._start_pump(reader, lambda s: self._dispatch_capability(cap_type, s))

    def _publish_registry(self) -> None:
        instances: list[dict[str, str]] = []
        for bucket in self._local_caps.values():
            for cap in bucket.values():
                instances.append({"type": cap.capability_type, "name": cap.name})
        msg = {"name": self._self_name, "instances": instances}
        self._registry_writer.write(json.dumps(msg, ensure_ascii=False))

    def _dispatch_registry(self, sample: Any) -> None:
        data = sample[0] if isinstance(sample, tuple) else sample
        try:
            msg = json.loads(data) if isinstance(data, (str, bytes)) else data
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        name = msg.get("name")
        instances = msg.get("instances", [])
        if not name or not isinstance(instances, list):
            return
        if name == self._self_name:
            return
        sig = tuple(sorted(
            (i.get("type", ""), i.get("name", "")) for i in instances
        ))
        with self._remote_lock:
            if self._seen_registry.get(name) == sig:
                return
            self._seen_registry[name] = sig
            existing = self._remote_devices.get(name)
            if existing is None:
                existing = Device(name=name, hub=self)
                self._remote_devices[name] = existing
            existing._update_caps(instances)
        self._fire_scan(existing, Event(kind="added", capability=None))

    def _dispatch_capability(self, cap_type: str, sample: Any) -> None:
        data = sample[0] if isinstance(sample, tuple) else sample
        try:
            msg = json.loads(data) if isinstance(data, (str, bytes)) else data
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        if msg.get("to") != self._self_name:
            return
        if msg.get("type") != cap_type:
            return
        instance = msg.get("instance")
        bucket: dict[str, Capability] | None = None
        for t, td in self._local_caps.items():
            if t.capability_type == cap_type:
                bucket = td
                break
        if bucket is None or instance not in bucket:
            return
        cap = bucket[instance]
        request_id = msg.get("id")
        reply_topic = msg.get("reply_topic")
        payload = msg.get("payload")

        def reply(value: Any) -> None:
            if not request_id or not reply_topic:
                return
            self._send_reply(reply_topic, {"id": request_id, "ok": True, "result": value})

        try:
            cap._handle(payload, reply)
        except Exception as exc:
            if request_id and reply_topic:
                self._send_reply(reply_topic, {
                    "id": request_id, "ok": False, "error": str(exc),
                })

    def _send_reply(self, reply_topic: str, payload: dict[str, Any]) -> None:
        try:
            with self._reply_writers_lock:
                if reply_topic not in self._reply_writers:
                    topic = self._participant.create_topic(
                        reply_topic, self._t_reply, dds.topic_qos(),
                    )
                    self._reply_writers[reply_topic] = self._publisher.create_writer(
                        topic, dds.reliable_writer_qos(),
                    )
            self._reply_writers[reply_topic].write(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            warnings.warn(f"wdiot: failed to send reply on {reply_topic}: {exc!r}", stacklevel=2)

    def _install_reply_reader(
        self, reply_topic: str, request_id: str, fut: Future
    ) -> None:
        with self._pending_lock:
            self._pending[request_id] = fut

        def _cleanup(_f: Future) -> None:
            with self._pending_lock:
                self._pending.pop(request_id, None)

        fut.add_done_callback(_cleanup)

        try:
            topic = self._participant.create_topic(
                reply_topic, self._t_reply, dds.topic_qos(),
            )
            reader = self._subscriber.create_reader(
                topic, dds.reliable_reader_qos(),
            )
            self._start_pump(reader, lambda s: self._dispatch_reply(request_id, s))
        except Exception as exc:
            fut.set_exception(exc)

    def _dispatch_reply(self, request_id: str, sample: Any) -> None:
        data = sample[0] if isinstance(sample, tuple) else sample
        try:
            msg = json.loads(data) if isinstance(data, (str, bytes)) else data
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        if msg.get("id") != request_id:
            return
        with self._pending_lock:
            fut = self._pending.pop(request_id, None)
        if fut is None or fut.done():
            return
        if msg.get("ok"):
            fut.set_result(msg.get("result"))
        else:
            fut.set_exception(RuntimeError(str(msg.get("error", "remote error"))))

    def _fire_scan(self, device: Device, event: Event) -> None:
        with self._scan_lock:
            cbs = list(self._scan_callbacks)
        for cb in cbs:
            try:
                cb(device, event)
            except Exception as exc:
                warnings.warn(f"wdiot scan callback error: {exc!r}", stacklevel=2)

    def _fire_removed_for(self, name: str) -> None:
        with self._remote_lock:
            dev = self._remote_devices.pop(name, None)
            self._seen_registry.pop(name, None)
        if dev is not None:
            self._fire_scan(dev, Event(kind="removed", capability=None))

    def _attach_participant_listener(self) -> None:
        """Best-effort wire-up of FastDDS built-in liveliness for removed events.

        On failure (unknown listener API, wrong constant name, etc.) we silently
        skip — discovery will still work for ``added`` events.
        """
        try:
            fd = dds._fastdds.get()
            listener_cls = getattr(fd, "DomainParticipantListener", None)
            if listener_cls is None:
                return
            hub_ref = self

            class _L(listener_cls):  # type: ignore[misc, valid-type]
                def on_participant_lost(self, *args: Any, **kwargs: Any) -> None:
                    with hub_ref._remote_lock:
                        names = list(hub_ref._remote_devices.keys())
                    for n in names:
                        hub_ref._fire_removed_for(n)

                def on_liveliness_lost(self, *args: Any, **kwargs: Any) -> None:
                    pass

            mask = getattr(fd, "PARTICIPANT_LIVELINESS_CHANGED_STATUS", 0)
            self._participant.raw.set_listener(_L(), mask)
        except Exception:
            pass


# ---------------------------------------------------------------- namespace


class _CapabilityNamespace:
    def __init__(self, cls: type, device: Device) -> None:
        self.cls = cls
        self.device = device

    def __getitem__(self, instance_name: str) -> Capability:
        return self.device._get_or_create_proxy(self.cls, instance_name)

    def __iter__(self):
        return iter(self.device.caps.get(self.cls, {}).values())

    def __repr__(self) -> str:
        return f"<CapabilityNamespace {self.cls.__name__} on {self.device.name!r}>"


_CAPABILITY_CLASSES_BY_NAME: dict[str, type] = {}
_TYPE_TO_CLS: dict[str, type] = {}


def _register_capability_class(cls: type) -> None:
    _CAPABILITY_CLASSES_BY_NAME[cls.__name__] = cls
    if cls.capability_type:
        _TYPE_TO_CLS[cls.capability_type] = cls


def _class_by_capability_type(t: str) -> type | None:
    return _TYPE_TO_CLS.get(t)


def _class_by_name(name: str) -> type | None:
    return _CAPABILITY_CLASSES_BY_NAME.get(name)


# ---------------------------------------------------------------- Device


class Device:
    """Proxy for a discovered wdiot device.

    Capabilities are exposed via attribute-style namespaces::

        gate1 = my_devices["Gate1Office"]
        cam = gate1.HWVideoInput["大门正门"]
        fut = cam({"q": "..."})

    Attribute access (``gate1.HWVideoInput``) and subscript access
    (``gate1[HWVideoInput]``) are equivalent.
    """

    def __init__(self, *, name: str, hub: Hub) -> None:
        self.name = name
        self._hub = hub
        self.caps: dict[type, dict[str, Capability]] = {}

    def _update_caps(self, instances: list[dict[str, str]]) -> None:
        self.caps.clear()
        for inst in instances:
            tname = inst.get("type", "")
            iname = inst.get("name", "")
            cls = _class_by_capability_type(tname)
            if cls is None:
                continue
            self.caps.setdefault(cls, {})[iname] = cls(iname, target=self)

    def _get_or_create_proxy(self, cls: type, instance_name: str) -> Capability:
        bucket = self.caps.setdefault(cls, {})
        if instance_name not in bucket:
            bucket[instance_name] = cls(instance_name, target=self)
        return bucket[instance_name]

    def __getattr__(self, name: str):
        cls = _class_by_name(name)
        if cls is not None:
            return _CapabilityNamespace(cls, self)
        raise AttributeError(name)

    def __getitem__(self, cls: type):
        if not isinstance(cls, type):
            raise TypeError(
                "Device[...] expects a Capability subclass, got "
                f"{type(cls).__name__}"
            )
        return _CapabilityNamespace(cls, self)

    @property
    def capabilities(self) -> set[type]:
        return set(self.caps.keys())

    def __repr__(self) -> str:
        return f"<Device {self.name!r} caps={[c.__name__ for c in self.caps]}>"


def _init_class_tables() -> None:
    from .capabilities import (
        HWAudioInput,
        HWAudioOutput,
        HWHDMIDisplay,
        HWVideoInput,
        SWAudioAnalyse,
        SWAudioQuery,
        SWAudioTTS,
        SWTextChat,
        SWVideoAnalyse,
        SWVideoDescribe,
        SWVideoQuery,
    )
    for cls in (
        HWVideoInput,
        SWVideoQuery,
        SWVideoDescribe,
        SWVideoAnalyse,
        HWAudioInput,
        HWAudioOutput,
        SWAudioQuery,
        SWAudioAnalyse,
        SWAudioTTS,
        SWTextChat,
        HWHDMIDisplay,
    ):
        _register_capability_class(cls)


_init_class_tables()


# ---------------------------------------------------------------- module API


_hub: Hub | None = None
_hub_lock = threading.Lock()


def connect(domain_id: int = 0) -> Hub:
    """Open a wdiot session. Use as ``with wdiot.connect():``."""
    global _hub
    with _hub_lock:
        if _hub is not None:
            raise RuntimeError("wdiot.connect() called twice")
        _hub = Hub(domain_id=domain_id)
    return _hub


def _get_hub() -> Hub:
    if _hub is None:
        raise RuntimeError("wdiot not connected; call wdiot.connect() first")
    return _hub


def register(name: str, capabilities: list[Capability]) -> None:
    """Publish this process's capabilities under ``name``.

    Each ``Capability`` is a class instance (e.g. ``HWVideoInput("大门正门")``)
    with an optional ``callback``.
    """
    _get_hub().register(name, capabilities)


def start_scan(callback: ScanCallback) -> None:
    """Subscribe to discovery events. ``callback(device, event)`` fires for each
    ``Event(kind="added"|"removed")``.

    The callback runs on a wdiot background thread — keep it short or hand off
    to your own queue/worker.
    """
    _get_hub().start_scan(callback)


def stop_scan(callback: ScanCallback) -> None:
    """Unsubscribe a previously registered scan callback."""
    _get_hub().stop_scan(callback)