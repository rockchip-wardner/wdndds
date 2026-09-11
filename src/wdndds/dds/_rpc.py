"""wdndds.dds._rpc — Python wrappers around libwdndds_cshim.

v0.5: uses FastDDS 3.6's rrpc Service / Replier / Requester API
(transparently wire-encoded via the IDL-generated ComputeRequest /
ComputeReply types).

Public API:
  Hub(participant_name)         — DomainParticipant (1 per process)
  Service(hub, service_name)    — RPC service endpoint
  Replier(service, callback)    — server side, pumps one std::thread
  Requester(service)            — client side, .call(payload) blocks
"""
import ctypes
import json
import sys
import logging
import threading
from typing import Any, Callable, Optional

from . import _cshim


_LOG = logging.getLogger("wdndds.rpc")


# Match C signatures (in sync with wdndds_cshim.h).
_request_cb = ctypes.CFUNCTYPE(
    ctypes.c_int32,
    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32,
    ctypes.c_int64, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_uint32),
)
_peer_cb = ctypes.CFUNCTYPE(
    None, ctypes.c_char_p, ctypes.c_int32, ctypes.c_void_p,
)
_peer_event_cb = ctypes.CFUNCTYPE(
    None, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_void_p,
)


# Hold references to all CFUNCTYPE wrappers so ctypes GC doesn't kill them.
_cb_refs: dict[int, Any] = {}
_cb_refs_lock = threading.Lock()


def _keepalive(cb):
    with _cb_refs_lock:
        cid = id(cb)
        _cb_refs[cid] = cb
        return cid


def _release_keepalive(cid: int) -> bool:
    """Release a keepalive handle previously obtained from :func:`_keepalive`.

    Returns ``True`` if the entry was found and removed, ``False``
    otherwise (already released or never registered). Pair every
    ``_keepalive(cb)`` call with a matching ``_release_keepalive(cid)``
    once the underlying cshim entity is destroyed — otherwise the
    CFUNCTYPE stays pinned in :data:`_cb_refs` forever, leaking the
    closure that captures the parent ``Requester`` / ``Replier`` and
    transitively their ``Service`` + ``Hub``.
    """
    with _cb_refs_lock:
        return _cb_refs.pop(cid, None) is not None


class Hub:
    """One DomainParticipant per process.

    A Hub doesn't tie to a single service; multiple Services can be
    registered on the same Hub (and same participant). One Replier
    per service for server-side, multiple Requesters for client-side.

    `discovery_servers` is a list of "host:port" strings. When set,
    the participant is configured with FastDDS SERVER discovery
    protocol + avoid_builtin_multicast=true. Every host on the LAN
    should have exactly one daemon listening on the configured port;
    all participants point to it. This replaces the unreliable
    SIMPLE discovery (multicast + initialPeers) on embedded boards.

    Usage::

        # Server
        with wdndds.Hub("worker-0") as hub:
            svc = hub.create_service("wdndds/rpc/qwen3-vl-4b")
            replier = svc.create_replier(callback=lambda svc, req: {...})
            hub.wait_forever()

        # Client (same LAN, daemon at 192.168.1.10:7411)
        with wdndds.Hub("client-0",
                        discovery_servers=["192.168.1.10:7411"]) as hub:
            svc = hub.create_service("wdndds/rpc/qwen3-vl-4b")
            requester = svc.create_requester(timeout_ms=10000)
            reply = requester.call({"action": "info"})
    """

    def __init__(self,
                 participant_name: str = "wdnwebui",
                 domain_id: int = 0,
                 *,
                 services: Optional[list[str]] = None,
                 callback: Optional[Callable[[str, dict], dict]] = None,
                 discovery_servers: Optional[list[str]] = None,
                 is_discovery_server: bool = False):
        """Create a Hub (one DomainParticipant per process).

        services: optional list of service names to pre-register
            (e.g. ["wdndds/rpc/qwen3-vl-4b"]). Server-side (worker)
            processes pass the list of services they will host.
            Client-side processes can pass an empty list and use
            hub.create_service() as needed.

        callback: legacy param for server-side — when set, the Hub
            auto-creates a Replier per service that dispatches to
            this single router function. Equivalent to calling
            hub.create_service(name).create_replier(callback) for
            each service in the services list.

        discovery_servers: list of "host:port" of FastDDS discovery
            servers (one daemon per LAN). When set, the participant
            uses SERVER (daemon) or CLIENT (everyone else) protocol
            and avoid_builtin_multicast=true.

        is_discovery_server: True only for the daemon process itself.
        """
        self._lib = _cshim.get()
        self._domain_id = domain_id
        self._participant_name = participant_name
        self._services_to_create = list(services or [])
        self._global_callback = callback
        self._discovery_servers = list(discovery_servers or [])
        self._is_discovery_server = is_discovery_server
        self._participant = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._services: dict[str, "Service"] = {}

    def start(self) -> None:
        with self._lock:
            if self._participant is not None:
                return
            csv = "\n".join(self._discovery_servers).encode("utf-8")
            self._participant = self._lib.wdndds_participant_create(
                self._domain_id,
                self._participant_name.encode(),
                csv,
                1 if self._is_discovery_server else 0)
            if not self._participant:
                raise RuntimeError(
                    f"wdndds: failed to create DomainParticipant "
                    f"(domain_id={self._domain_id}, "
                    f"discovery_servers={self._discovery_servers})")
            # Pre-create services if specified. Server processes pass
            # the list of services they host + a global callback;
            # each service gets its own Replier.
            for svc_name in self._services_to_create:
                svc = self.create_service(svc_name)
                if self._global_callback is not None:
                    svc.create_replier(self._global_callback)

    def create_service(self, service_name: str) -> "Service":
        if self._participant is None:
            raise RuntimeError("Hub is not started")
        if service_name in self._services:
            return self._services[service_name]
        svc = Service(self, service_name)
        svc.create()
        self._services[service_name] = svc
        return svc

    def destroy_service(self, service_name: str) -> bool:
        """Tear down + drop a single service from this Hub.

        Calls :meth:`Service.destroy` (which deletes the cshim
        ``wdndds_service`` handle and clears all tracked
        Repliers/Requesters) and removes the ``Service`` object from
        ``_services``. Without this call, repeatedly creating services
        with unique names (e.g. ``wdndds/rpc/<model>/worker-<pid>``
        where ``pid`` changes each restart cycle) would accumulate
        one ``Service`` per cycle — each holding its pool of
        cshim-side DataReader/DataWriter payload pools, which are
        tens of MB each.

        Returns ``True`` if a service was found and removed,
        ``False`` otherwise. Safe to call repeatedly.
        """
        with self._lock:
            svc = self._services.pop(service_name, None)
        if svc is None:
            return False
        try:
            svc.destroy()
        except Exception:
            _LOG.warning("Hub.destroy_service: svc.destroy raised for %s",
                         service_name)
        return True

    def get_native(self) -> int:
        return int(self._participant or 0)

    def guid(self) -> str:
        """Return this participant's GUID as 47-char hex string.

        Empty string if the cshim call fails. The daemon uses this
        to identify itself in the discovery-server's participant list.
        """
        if self._participant is None:
            return ""
        buf = ctypes.create_string_buffer(64)
        rc = self._lib.wdndds_participant_guid(
            self._participant, buf, len(buf))
        if rc != 0:
            return ""
        return buf.value.decode("utf-8", errors="replace")

    def set_peer_listener(self,
                          on_event: Optional[Callable[[str, str, str, bool], None]]):
        """Install a peer-discovery callback.

        on_event(guid, name, ip, discovered=True/False) is invoked from
        FastDDS' DomainParticipantListener thread on every peer
        add/remove. Pass None to remove the listener.
        """
        if self._participant is None:
            return -1
        if on_event is None:
            self._lib.wdndds_participant_set_listener(
                self._participant, _peer_event_cb(None), None)
            return 0
        # Wrap the user's Python callback in a CFUNCTYPE so ctypes can call it.
        def _cb(guid_ptr, name_ptr, ip_ptr, discovered, user):
            guid = guid_ptr.decode("utf-8", errors="replace") if guid_ptr else ""
            name = name_ptr.decode("utf-8", errors="replace") if name_ptr else ""
            ip = ip_ptr.decode("utf-8", errors="replace") if ip_ptr else ""
            try:
                on_event(guid, name, ip, bool(discovered))
            except Exception as e:  # noqa: BLE001
                _LOG.warning("peer_listener raised: %s", e)
        c_cb = _peer_event_cb(_cb)
        _keepalive(c_cb)
        rc = self._lib.wdndds_participant_set_listener(
            self._participant, c_cb, None)
        return rc

    def list_peers(self) -> list[str]:
        """Return the discovered peer list as raw lines.

        Currently returns [] — FastDDS 3.6's
        get_discovered_participants returns RETCODE_UNSUPPORTED. The
        daemon process exists for the sole purpose of brokering
        discovery; counting participants is a debug-only nicety
        deferred to a follow-up.
        """
        if self._participant is None:
            return []
        out = ctypes.c_char_p()
        out_len = ctypes.c_uint32()
        rc = self._lib.wdndds_participant_list_peers(
            self._participant,
            ctypes.byref(out), ctypes.byref(out_len))
        ptr = ctypes.cast(out, ctypes.c_void_p).value
        try:
            if rc != 0 or not ptr:
                return []
            raw = bytes(ctypes.string_at(ptr, out_len.value)).decode(
                "utf-8", errors="replace")
            return [line for line in raw.split("\n") if line]
        finally:
            if ptr:
                try:
                    self._lib.wdndds_free(ctypes.c_char_p(ptr))
                except Exception as e:
                    _LOG.warning("wdndds_free failed (peer buffer leaked): %s", e)

    def wait_forever(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=1.0)
        except KeyboardInterrupt:
            pass

    def stop(self) -> None:
        self._stop_event.set()

    def close(self) -> None:
        with self._lock:
            for svc in self._services.values():
                try:
                    svc.destroy()
                except Exception:
                    pass
            self._services.clear()
            if self._participant is not None:
                self._lib.wdndds_participant_destroy(self._participant)
                self._participant = None

    def __enter__(self) -> "Hub":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Service:
    """One RPC service endpoint. Holds a Service* on the participant."""

    def __init__(self, hub: Hub, service_name: str):
        self._hub = hub
        self._service_name = service_name
        self._service = None
        self._repliers: list[Replier] = []
        self._requesters: list[Requester] = []

    def create(self) -> None:
        if self._service is not None:
            return
        self._service = self._hub._lib.wdndds_service_create(
            self._hub._participant,
            self._service_name.encode())
        if not self._service:
            raise RuntimeError(
                f"wdndds: failed to create service {self._service_name!r}")

    def create_replier(self, callback: Callable[[str, dict], dict]) -> "Replier":
        replier = Replier(self, callback)
        replier.create()
        self._repliers.append(replier)
        return replier

    def create_requester(self, timeout_ms: int = 30000) -> "Requester":
        requester = Requester(self, timeout_ms=timeout_ms)
        requester.create()
        self._requesters.append(requester)
        return requester

    def remove_requester(self, requester: "Requester") -> bool:
        """Drop ``requester`` from this Service's tracking list.

        Call this *after* ``requester.destroy()`` has already freed the
        underlying cshim handle. We do NOT call ``destroy()`` here —
        callers may want to remove a requester without destroying it
        (uncommon; the normal pattern is destroy then remove, or pass
        through :meth:`destroy` which clears both ``_repliers`` and
        ``_requesters`` atomically).

        Returns ``True`` if the requester was found and removed,
        ``False`` otherwise. Removing twice is safe.
        """
        try:
            self._requesters.remove(requester)
            return True
        except ValueError:
            return False

    def destroy(self) -> None:
        for r in self._repliers:
            try:
                r.destroy()
            except Exception:
                pass
        self._repliers.clear()
        for q in self._requesters:
            try:
                q.destroy()
            except Exception:
                pass
        self._requesters.clear()
        if self._service is not None:
            self._hub._lib.wdndds_service_destroy(self._service)
            self._service = None

    @property
    def name(self) -> str:
        return self._service_name


class Replier:
    """Server side: a request callback that runs on the cshim's pump thread."""

    def __init__(self, service: Service, callback: Callable[[str, dict], dict]):
        self._service = service
        self._callback = callback
        self._native = None

    def create(self) -> None:
        if self._native is not None:
            return
        self._c_cb = _request_cb(self._on_request_c)
        self._c_keepalive_id = _keepalive(self._c_cb)
        self._native = self._service._hub._lib.wdndds_replier_create(
            self._service._hub._participant,
            self._service._service,
            self._c_cb, None, None)
        if not self._native:
            # Failed creation — release the keepalive slot we just took,
            # otherwise the CFUNCTYPE wrapper stays pinned in _cb_refs
            # even though the Replier never came up.
            _release_keepalive(self._c_keepalive_id)
            self._c_cb = None
            self._c_keepalive_id = 0
            raise RuntimeError("wdndds: failed to create Replier")

    def _on_request_c(self, service_name, payload, payload_len,
                      correlation_id, user,
                      out_reply, out_reply_len) -> int:
        try:
            body = bytes(payload[:payload_len]).decode("utf-8", errors="replace")
            try:
                payload_obj = json.loads(body)
            except Exception:
                payload_obj = body
            reply_obj = self._callback(
                service_name.decode("utf-8") if service_name else "",
                payload_obj)
            reply_bytes = json.dumps(reply_obj, ensure_ascii=False).encode("utf-8")
            out_reply[0] = reply_bytes
            out_reply_len[0] = len(reply_bytes)
            return 0
        except Exception as e:
            try:
                err = json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}
                                 ).encode("utf-8")
                out_reply[0] = err
                out_reply_len[0] = len(err)
            except Exception:
                pass
            return -1

    def destroy(self) -> None:
        if self._native is not None:
            self._service._hub._lib.wdndds_replier_destroy(self._native)
            self._native = None
        if getattr(self, "_c_keepalive_id", 0):
            _release_keepalive(self._c_keepalive_id)
            self._c_keepalive_id = 0
            self._c_cb = None


class Requester:
    """Client side: synchronous .call(payload)."""

    def __init__(self, service: Service, timeout_ms: int = 30000):
        self._service = service
        self._timeout_ms = timeout_ms
        self._native = None
        self._lock = threading.RLock()

    def create(self) -> None:
        if self._native is not None:
            return
        self._native = self._service._hub._lib.wdndds_requester_create(
            self._service._hub._participant,
            self._service._service,
            int(self._timeout_ms))
        if not self._native:
            raise RuntimeError("wdndds: failed to create Requester")

    def call(self, payload: Any, *, timeout_ms: Optional[int] = None) -> dict:
        if timeout_ms is None:
            timeout_ms = self._timeout_ms
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        out_reply = ctypes.c_char_p()
        out_len = ctypes.c_uint32()

        with self._lock:
            rc = self._service._hub._lib.wdndds_requester_call(
                self._native,
                body, len(body),
                int(timeout_ms),
                ctypes.byref(out_reply),
                ctypes.byref(out_len))

        ptr = ctypes.cast(out_reply, ctypes.c_void_p).value
        try:
            if rc == 0 and ptr:
                raw_bytes = bytes(ctypes.string_at(ptr, out_len.value))
                raw = raw_bytes.decode("utf-8", errors="replace")
                try:
                    return json.loads(raw)
                except Exception as _exc:
                    _LOG.error(
                        "[WIRE-DEBUG-PROXY] rc=%d PARSE_FAIL out_len=%d "
                        "raw_chars=%d raw_utf8_bytes=%d err=%s "
                        "FULL_RAW=%r",
                        rc, out_len.value, len(raw), len(raw_bytes),
                        _exc, raw)
                    return {"ok": False,
                            "error": f"malformed reply: {raw[:200]!r}"}
            elif rc == 1:
                return {"ok": False, "error": "timeout"}
            elif rc == -1:
                if ptr:
                    raw = bytes(ctypes.string_at(ptr, out_len.value)).decode(
                        "utf-8", errors="replace")
                    try:
                        return json.loads(raw)
                    except Exception:
                        return {"ok": False, "error": raw}
                return {"ok": False, "error": "send failed"}
            else:
                return {"ok": False, "error": f"unknown rc={rc}"}
        finally:
            if ptr:
                try:
                    self._service._hub._lib.wdndds_free(ctypes.c_char_p(ptr))
                except Exception as e:
                    _LOG.warning("wdndds_free failed (reply buffer leaked): %s", e)

    def destroy(self) -> None:
        if self._native is not None:
            self._service._hub._lib.wdndds_requester_destroy(self._native)
            self._native = None
