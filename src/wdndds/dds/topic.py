from __future__ import annotations

import ctypes
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Callable

from . import _cshim

if TYPE_CHECKING:
    from typing import Self


class Topic:
    """High-level wrapper around a FastDDS Topic."""

    def __init__(self, raw: Any, *, owned: bool = False) -> None:
        self._raw = raw
        self._owned = owned

    @classmethod
    def _create(
        cls,
        participant: Any,
        name: str,
        type_name: str,
        qos: Any | None,
    ) -> Self:
        from ._fastdds import get

        fd = get()
        if qos is None:
            qos = fd.TopicQos()
        # Modern FastDDS Python bindings (≥2.10) changed the
        # ``create_topic`` signature: the listener + StatusMask overloads
        # require a ``ReturnCode_t &`` out-parameter that SWIG can't
        # match from Python. The 3-arg overload
        # ``create_topic(name, type_name, qos) -> Topic*`` is still
        # available and avoids the SWIG mismatch — the return code is
        # observable via ``topic.get_name()`` returning ``None`` on
        # failure (which we treat as an error below).
        raw = participant.create_topic(name, type_name, qos)
        if raw is None:
            raise RuntimeError(f"failed to create topic '{name}'")
        return cls(raw, owned=True)

    @property
    def raw(self) -> Any:
        return self._raw

    @property
    def name(self) -> str:
        return self._raw.get_name()

    @property
    def type_name(self) -> str:
        return self._raw.get_type_name()

    def close(self) -> None:
        if not self._owned:
            return
        self._owned = False
        with suppress(Exception):
            self._raw.get_participant().delete_topic(self._raw)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# Raw-bytes topic writer/reader — 走 cshim 原语，给 PCM 等不透明字节流用。
# 线缆内部：每片是 ComputeRequest 带一个 ComputeAttachment.bytes；
# caller 看不到 envelope。participant 必须是 wdndds.Hub
# (cshim-backed DomainParticipant)，legacy wdndds.dds.domain.Participant
# 不支持（cshim 原语吃 cshim handle）。

_TOPIC_QOS: dict[str, int] = {
    "best_effort": 0,
    "reliable": 1,
}


def _resolve_qos_kind(qos_kind: str | int) -> int:
    if isinstance(qos_kind, int):
        return 0 if qos_kind not in (0, 1) else qos_kind
    if qos_kind in _TOPIC_QOS:
        return _TOPIC_QOS[qos_kind]
    raise ValueError(
        f"qos_kind must be one of {sorted(_TOPIC_QOS)} or 0/1, got {qos_kind!r}")


def _participant_native_handle(participant: Any) -> int:
    """Return the cshim opaque handle for a Hub. Raises if the input
    doesn't expose :meth:`get_native` (the cshim-only API)."""
    if not hasattr(participant, "get_native"):
        raise TypeError(
            "create_topic_writer/reader requires a wdndds.Hub "
            "(cshim-backed DomainParticipant), got "
            f"{type(participant).__name__}")
    handle = participant.get_native()
    if not handle:
        raise RuntimeError("participant is not started (Hub.start() not called)")
    return int(handle)


class TopicWriter:
    """Best-effort raw-bytes writer on a single DDS topic."""

    def __init__(self, raw: int, topic_name: str, qos_kind: int) -> None:
        self._raw = int(raw)
        self._topic_name = topic_name
        self._qos_kind = qos_kind
        self._closed = False

    @property
    def topic_name(self) -> str:
        return self._topic_name

    @property
    def qos_kind(self) -> str:
        return "reliable" if self._qos_kind == 1 else "best_effort"

    def write(self, payload: bytes | bytearray | memoryview) -> int:
        """Publish raw bytes. Returns 0 on RETCODE_OK, non-zero on error.

        Best-effort drops are NOT surfaced as errors — the cshim returns
        the FastDDS return code, and FastDDS silently drops samples for
        unmatched writers (BEST_EFFORT) instead of erroring. So a
        non-zero return here is a hard failure (disabled writer, type
        mismatch, etc.).
        """
        if self._closed:
            return -1
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError(
                f"payload must be bytes-like, got {type(payload).__name__}")
        buf = bytes(payload)
        if not buf:
            return 0
        lib = _cshim.get()
        return lib.wdndds_topic_writer_write(
            self._raw, buf, len(buf))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            _cshim.get().wdndds_topic_writer_destroy(self._raw)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class TopicReader:
    """Best-effort raw-bytes reader; calls ``callback(bytes)`` per chunk.

    The callback runs on a cshim daemon thread (one per reader); keep
    it short — heavy work should hand the bytes off to your own queue.
    """

    def __init__(
        self,
        raw: int,
        topic_name: str,
        callback: Callable[[bytes], None],
    ) -> None:
        self._raw = int(raw)
        self._topic_name = topic_name
        self._callback = callback
        self._closed = False

    @property
    def topic_name(self) -> str:
        return self._topic_name

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):
            _cshim.get().wdndds_topic_reader_destroy(self._raw)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _make_chunk_callback(
    user_callback: Callable[[bytes], None],
    keepalive: list,
) -> Any:
    """Wrap a Python bytes-callback in a CFUNCTYPE for the cshim.

    ``keepalive`` is a list we stash the CFUNCTYPE in so ctypes' GC
    doesn't collect the trampoline out from under the cshim pump.
    Errors raised inside the user callback are logged and swallowed —
    one bad chunk must not poison the reader's pump thread.
    """
    import logging
    _log = logging.getLogger("wdndds.topic.chunk_cb")

    def _trampoline(payload_ptr, payload_len, _user):
        if not payload_ptr or not payload_len:
            return
        try:
            user_callback(
                bytes(ctypes.string_at(payload_ptr, payload_len)))
        except Exception as e:  # noqa: BLE001
            _log.warning("topic reader callback raised: %s", e)

    cback = _cshim._chunk_cb(_trampoline)
    keepalive.append(cback)
    return cback


def create_topic_writer(
    participant: Any,
    topic_name: str,
    *,
    qos_kind: str | int = "best_effort",
) -> TopicWriter:
    """Create a best-effort raw-bytes writer on ``topic_name``.

    ``topic_name`` is used verbatim as the DDS topic name — caller is
    responsible for namespacing (``"wdndds/audio/tts_chunk/<model>"``).
    Returns a :class:`TopicWriter` whose :meth:`TopicWriter.write` takes
    raw ``bytes`` and silently drops on transport failure.

    Raises :class:`RuntimeError` if the cshim fails to create the
    underlying DataWriter (no DomainParticipant, already destroyed, etc.).
    """
    qos_int = _resolve_qos_kind(qos_kind)
    handle = _participant_native_handle(participant)
    lib = _cshim.get()
    raw = lib.wdndds_topic_writer_create(
        handle, topic_name.encode("utf-8"), qos_int)
    if not raw:
        raise RuntimeError(
            f"wdndds_topic_writer_create failed for topic {topic_name!r}")
    return TopicWriter(raw, topic_name, qos_int)


def create_topic_reader(
    participant: Any,
    topic_name: str,
    callback: Callable[[bytes], None],
    *,
    qos_kind: str | int = "best_effort",
) -> TopicReader:
    """Create a best-effort raw-bytes reader on ``topic_name``.

    ``callback(bytes)`` fires once per received chunk on a cshim
    daemon thread. Return value is a :class:`TopicReader` whose
    :meth:`TopicReader.close` joins the pump thread and destroys the
    underlying DataReader.
    """
    qos_int = _resolve_qos_kind(qos_kind)
    handle = _participant_native_handle(participant)
    if not callable(callback):
        raise TypeError(
            f"callback must be callable[[bytes], None], got {type(callback).__name__}")
    lib = _cshim.get()
    # Stash the CFUNCTYPE on a keepalive list to prevent ctypes GC.
    keepalive: list = []
    c_back = _make_chunk_callback(callback, keepalive)
    raw = lib.wdndds_topic_reader_create(
        handle, topic_name.encode("utf-8"), qos_int, c_back, None)
    if not raw:
        raise RuntimeError(
            f"wdndds_topic_reader_create failed for topic {topic_name!r}")
    reader = TopicReader(raw, topic_name, callback)
    reader._keepalive = keepalive
    return reader
