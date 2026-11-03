"""Run a :class:`wdnai.LLMSession` in another process, over DDS.

This is the RPC half of wdnai's split session interface. ``wdnai`` owns
the interface (``LLMSession``) plus the in-process implementation
(``LLMLocalSession``); this module adds :class:`LLMRPCSession`, which
subclasses the *same* interface and forwards only the transport step.

The design property that makes this safe is that **prompt assembly and
output parsing stay in the caller's process**. ``wdnai.LLMSession.respond``
composes the prompt from ``generating=`` / ``tools=`` / ``history=`` and
parses the reply back into schema or ``@Callable`` instances. Neither end
of that needs to happen on the worker:

    caller (owns TargetDoc / TtsAlert classes)
      respond(generating=TargetDoc, tools=[...])
        _compose_prompt   <- schema hint + tool block baked into the string
        run_prompt ───────►  {prompt: "<finished>", attachments: [...], ...}
        try_parse_tool_call / _parse_output   <- both local, real instances
      ◄──────────────────  {status, text, attachments_out, ...}

So ``generating`` and ``tools`` never appear on the wire. That kills two
long-standing failure modes at once: a worker needing a hand-maintained
table of schema/tool class names, and schema-version drift between the
two processes.

Wire shape
----------
The wdndds cshim only has five typed request actions and smuggles the
whole client JSON through the IDL's ``prompt`` byte field, so the server
callback sees ``{"action", "prompt", "every_n", "request_id"}`` and must
re-parse ``payload["prompt"]`` to recover the original dict. The reply is
the handler's dict, embedded by cshim as ``reply["data"]``.

Streaming and cancellation deliberately do **not** travel over the RPC
replier. A replier handles one request at a time on its pump thread, and
that thread is inside the blocking ``session_run`` for the whole
inference — so anything sent through it would queue behind the very call
you are trying to observe or cancel. Both use side-channel DDS topics on
a **separate** ``Hub`` (its own ``DomainParticipant``; sharing one
participant deadlocks the publisher behind the replier's reply path).
"""
from __future__ import annotations

import asyncio
import atexit
import base64
import glob
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from wdnai.attachment import (
    Attachment, AudioAttachment, ImageAttachment, TextAttachment,
)
from wdnai.session import LLMSession

from .dds._rpc import Hub, Requester, Replier, Service
from .dds.topic import (
    TopicReader, TopicWriter, create_topic_reader, create_topic_writer,
)

_LOG = logging.getLogger("wdndds.ai_session")

#: Bumped whenever the request/reply shape changes incompatibly. Checked in
#: the ``info`` handshake so a stale detached worker fails loudly instead
#: of silently mis-parsing.
WIRE_VERSION = 2

# The cshim only recognises these action names and coerces anything else
# to "info", so the real op discriminator travels inside the body.
_ACTION_INFO = "info"
_ACTION_INFER = "infer"
_ACTION_RESET = "reset"

_SHM_DIR = "/dev/shm"
_FRAME_SHM_PREFIX = "wdndds_ai_frame_"
_AUDIO_SHM_PREFIX = "wdndds_ai_audio_"
_REPLY_SHM_PREFIX = "wdndds_ai_reply_"

# cshim enforces max_serialized_type_size = 1 MiB per message and the
# limit is not reachable from Python, so long replies travel via /dev/shm
# and only a short pointer crosses the wire.
_INLINE_TEXT_LIMIT = 512

_DEFAULT_TIMEOUT_MS = 5_000
_INFER_TIMEOUT_MS = 15_000

# A brand-new DataWriter isn't matched with the worker's reader yet, so a
# single write can be dropped; re-publish over ~1s to make stop reliable.
_STOP_PUBLISH_RETRIES = 5
_STOP_PUBLISH_INTERVAL_S = 0.25

_SOFT_RETRIES = 2      # plain retries, no channel churn
_HARD_RETRIES = 1      # destroy + recreate the requester, then retry

# Bound on how long we wait for the worker's end-of-stream marker before
# returning. The marker is published before the handler returns, so this
# normally resolves instantly; the cap just stops a lost sample from
# hanging the caller.
_STREAM_END_TIMEOUT_S = 2.0

_SHM_PREFIXES = (_FRAME_SHM_PREFIX, _AUDIO_SHM_PREFIX, _REPLY_SHM_PREFIX)


# --------------------------------------------------------------------------- #
# /dev/shm side-files
# --------------------------------------------------------------------------- #
def sweep_stale_shm(max_age_seconds: float = 3600.0) -> int:
    """Delete leftover side-files older than ``max_age_seconds``.

    A crash between "write the side-file" and "read + unlink it" leaks one
    file per frame. Boot sweeps with a 1 h floor so that two processes
    booting at the same moment cannot delete each other's in-flight files;
    the atexit sweep uses ``max_age=0`` to clear everything.
    """
    n = 0
    now = time.time()
    for prefix in _SHM_PREFIXES:
        for path in glob.glob(os.path.join(_SHM_DIR, f"{prefix}*.bin")):
            try:
                if now - os.stat(path).st_mtime < max_age_seconds:
                    continue
                os.unlink(path)
                n += 1
            except OSError:
                pass
    if n:
        _LOG.info("swept %d stale /dev/shm side-files", n)
    return n


def _write_shm(payload: bytes, prefix: str) -> str:
    os.makedirs(_SHM_DIR, exist_ok=True)
    path = os.path.join(_SHM_DIR, f"{prefix}{uuid.uuid4().hex}.bin")
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


def _bgr_to_path(arr: np.ndarray) -> Dict[str, Any]:
    """Raw BGR ``uint8`` HxWx3 -> ``/dev/shm`` descriptor. Byte-exact.

    JPEG would be cheaper on the wire but lossy, and VLM output quality is
    measurably sensitive to it — so the frame never gets re-encoded.
    """
    if (not isinstance(arr, np.ndarray) or arr.dtype != np.uint8
            or arr.ndim != 3 or arr.shape[2] != 3):
        # Anything else (float frames, grayscale, foreign ndarray types)
        # takes the lossy path rather than silently mis-reshaping.
        return {"data_b64": _bgr_to_b64(arr)}
    path = _write_shm(arr.tobytes(), _FRAME_SHM_PREFIX)
    h, w, _ = arr.shape
    return {"data_path": path, "shape": [int(h), int(w), 3], "dtype": "uint8"}


def _bgr_to_b64(arr: Any, quality: int = 85) -> str:
    """JPEG + base64. Lossy — only for LAN peers that cannot read our shm."""
    import cv2  # lazy: only the fallback path needs it

    ok, buf = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _audio_to_path(data: bytes, *, mime_type: str = "audio/wav") -> Dict[str, Any]:
    return {
        "data_path": _write_shm(bytes(data), _AUDIO_SHM_PREFIX),
        "kind": "audio",
        "mime_type": mime_type,
    }


def _read_shm_text(path: str) -> Optional[str]:
    """Read a shm side-file and unlink it. Missing file -> ``None``.

    A missing file means the peer died between write and read; that is a
    degraded result, not a crash, so callers get ``None`` and log.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        _LOG.warning("shm side-file vanished before read: %s", path)
        return None
    except OSError as exc:
        _LOG.warning("shm side-file unreadable (%s): %s", exc, path)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _read_shm_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        _LOG.warning("shm side-file vanished before read: %s", path)
        return None
    except OSError as exc:
        _LOG.warning("shm side-file unreadable (%s): %s", exc, path)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Attachment codec
# --------------------------------------------------------------------------- #
def encode_attachments(
    attachment: Any, *, is_remote: bool = False,
) -> List[Any]:
    """Serialise :class:`wdnai` attachments for the wire.

    Each entry is one of: a ``str`` (text prefix), a ``{"data_path":…}``
    frame/audio descriptor, a bare base64 ``str`` (image, LAN peer only),
    or an ``{"data_b64": …, "kind": …}`` blob.

    ``is_remote`` selects JPEG+base64 because ``/dev/shm`` is per-host and
    a LAN peer's worker cannot open our files. Negotiated in the ``info``
    handshake rather than guessed.
    """
    if attachment is None:
        return []
    if not isinstance(attachment, (list, tuple)):
        attachment = [attachment]

    out: List[Any] = []
    for item in attachment:
        if isinstance(item, ImageAttachment):
            for img in item.images:
                out.append(_bgr_to_b64(img) if is_remote else _bgr_to_path(img))
        elif isinstance(item, TextAttachment):
            out.append(item.text or "")
        elif isinstance(item, AudioAttachment):
            mime = getattr(item, "mime_type", "audio/wav") or "audio/wav"
            if is_remote:
                out.append({"data_b64": base64.b64encode(bytes(item.data)).decode("ascii"),
                            "kind": "audio", "mime_type": mime})
            else:
                out.append(_audio_to_path(item.data, mime_type=mime))
        elif isinstance(item, Attachment):
            # Generic/duck-typed subclass — keep the historical probing so
            # custom attachments keep working.
            if getattr(item, "images", None):
                for img in item.images:
                    out.append(_bgr_to_b64(img) if is_remote else _bgr_to_path(img))
            elif getattr(item, "text", None):
                out.append(item.text)
            elif getattr(item, "data", None):
                mime = (getattr(item, "mime_type", "audio/wav") or "audio/wav")
                if is_remote:
                    out.append({"data_b64": base64.b64encode(bytes(item.data)).decode("ascii"),
                                "kind": "audio", "mime_type": mime})
                else:
                    out.append(_audio_to_path(item.data, mime_type=mime))
        elif isinstance(item, np.ndarray):
            out.append(_bgr_to_b64(item) if is_remote else _bgr_to_path(item))
        elif isinstance(item, (bytes, bytearray)):
            mime = getattr(item, "mime_type", "audio/wav") or "audio/wav"
            if is_remote:
                out.append({"data_b64": base64.b64encode(bytes(item)).decode("ascii"),
                            "kind": "audio", "mime_type": mime})
            else:
                out.append(_audio_to_path(item, mime_type=mime))
        elif isinstance(item, str):
            out.append(item)
        else:
            out.append(str(item))
    return out


def decode_attachments(entries: Any) -> List[Attachment]:
    """Inverse of :func:`encode_attachments`, worker side.

    Reconstructs real ``wdnai`` attachment objects and consumes (unlinks)
    every side-file it reads, so a healthy round trip leaves nothing
    behind in ``/dev/shm``.
    """
    if not entries:
        return []
    out: List[Attachment] = []
    for entry in entries if isinstance(entries, list) else [entries]:
        if isinstance(entry, str):
            # Ambiguous by design (legacy): a bare base64 blob is an image,
            # anything else is a text prefix.
            try:
                raw = base64.b64decode(entry, validate=True)
                import cv2  # lazy

                arr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
                if arr is not None:
                    out.append(ImageAttachment(arr))
                    continue
            except Exception:  # noqa: BLE001 — not base64 → treat as text
                pass
            out.append(TextAttachment(entry))
        elif isinstance(entry, dict):
            if entry.get("data_path"):
                path = entry["data_path"]
                shape = entry.get("shape")
                if shape:  # image frame
                    raw = _read_shm_bytes(path)
                    if raw is None:
                        continue
                    arr = np.frombuffer(raw, np.uint8).reshape(
                        int(shape[0]), int(shape[1]), int(shape[2]))
                    out.append(ImageAttachment(arr))
                else:  # audio bytes
                    raw = _read_shm_bytes(path)
                    if raw is None:
                        continue
                    out.append(AudioAttachment(
                        raw, mime_type=entry.get("mime_type", "audio/wav")))
            elif entry.get("data_b64"):
                raw = base64.b64decode(entry["data_b64"])
                if entry.get("kind") == "audio":
                    out.append(AudioAttachment(
                        raw, mime_type=entry.get("mime_type", "audio/wav")))
                else:
                    import cv2  # lazy

                    arr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
                    if arr is not None:
                        out.append(ImageAttachment(arr))
    return out


def encode_attachments_out(att_out: Any) -> List[Dict[str, Any]]:
    """Encode ``attachments_out`` (TTS audio, generated images …) for the reply.

    Uses ``data_path`` unconditionally: the writer is the worker, the
    reader is the caller, and ``/dev/shm`` is shared on the local host. The
    LAN-peer reply path is out of scope for now — a remote reply that
    carries audio will come back as a descriptor with an unreadable path
    and degrade to ``None`` rather than raising.

    **图像**（文生图 worker 的产物）走同一个 ``/dev/shm`` 通道：
    ``ImageAttachment.images`` 是一组 BGR ``uint8`` ndarray，用
    :func:`_bgr_to_path` 原样落盘（**不重新编码** ⇒ 逐字节一致），
    回程按 ``shape`` 还原成 ndarray。

    ⚠️ 这里曾经只认 ``item.data``（音频专有属性），于是文生图 worker
    生成的图片**被静默丢掉**：reply 里 ``attachments_out`` 是空的，
    调用方只拿到一句 prompt 回显，看不出"图丢了"。任何"产物型"
    attachment 都必须在这里显式列出，否则就是静默降级。
    """
    if not att_out:
        return []
    out: List[Dict[str, Any]] = []
    for item in att_out:
        if isinstance(item, ImageAttachment):
            for img in item.images:
                out.append(_bgr_to_path(img))
            continue
        data = getattr(item, "data", None)
        if data is None:
            continue
        out.append(_audio_to_path(
            data, mime_type=getattr(item, "mime_type", "audio/wav") or "audio/wav"))
    return out


def decode_attachments_out(entries: Any) -> List[Attachment]:
    """Inverse of :func:`encode_attachments_out`, caller side."""
    if not entries:
        return []
    out: List[Attachment] = []
    for entry in entries if isinstance(entries, list) else [entries]:
        if not isinstance(entry, dict):
            continue
        if entry.get("data_path"):
            raw = _read_shm_bytes(entry["data_path"])
            if raw is None:
                continue
            shape = entry.get("shape")
            if shape:
                # 图像：按 shape 还原成 BGR uint8 ndarray
                try:
                    dims = tuple(int(x) for x in shape)
                    arr = np.frombuffer(raw, dtype=np.uint8).reshape(dims)
                except (TypeError, ValueError) as exc:
                    _LOG.warning("bad image shape %r: %s", shape, exc)
                    continue
                out.append(ImageAttachment(arr))
                continue
            out.append(AudioAttachment(
                raw, mime_type=entry.get("mime_type", "audio/wav") or "audio/wav"))
            continue
        if entry.get("data_b64"):
            # JPEG + base64 —— 非 uint8 HxWx3 帧 / LAN peer 的降级路径
            try:
                blob = base64.b64decode(entry["data_b64"])
                import cv2  # lazy: 只有降级路径需要
                arr = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8),
                                   cv2.IMREAD_COLOR)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("decode image b64 failed: %s", exc)
                continue
            if arr is None:
                continue
            out.append(ImageAttachment(arr))
    return out


# --------------------------------------------------------------------------- #
# Client side
# --------------------------------------------------------------------------- #
class _StreamBridge:
    """Local end of one streaming ``respond`` call.

    The caller's ``on_delta`` callable never leaves this process: it lives
    here and is invoked from the cshim reader thread when a delta sample
    lands on the topic. What crosses the wire is only the *intent* to
    stream (``stream_id``), plus each delta as a separate sample.

    ``end_of_stream`` is published by the worker before it returns its
    reply. Waiting on it is what gives ``respond`` its "all deltas
    delivered before I return" guarantee, without sleeping.
    """

    def __init__(self, stream_id: str, on_delta: Callable[[str], None]):
        self.stream_id = stream_id
        self.on_delta = on_delta
        self.done = threading.Event()

    def feed(self, raw: bytes) -> None:
        # Framing: "<stream_id>\n<delta>"; the terminal marker carries no
        # body. A stream_id mismatch means a sample from a previous call.
        head, sep, body = raw.partition(b"\n")
        if head.decode("utf-8", "replace") != self.stream_id:
            return
        if not sep:
            self.done.set()          # terminal marker
            return
        try:
            self.on_delta(body.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 — a bad callback must not kill the pump
            _LOG.debug("on_delta raised; dropped sample", exc_info=True)


class LLMRPCSession(LLMSession):
    """A :class:`wdnai.LLMSession` whose model lives in another process.

    Same call surface as :class:`wdnai.LLMLocalSession` — ``respond``,
    ``reset``, ``close``, ``stop``, ``embed_text``, ``embed_video`` — so
    application code does not branch on transport::

        sess = LLMRPCSession(model=spec, service_name="wdndds/rpc/qwen3-vl-4b")
        out = await sess.respond(prompt=..., generating=TargetDoc)

    Four behaviours genuinely differ from the local implementation, and
    are inherent to the boundary rather than incidental:

    1. **Frames cross as a path, not a value.** Images and audio go
       through ``/dev/shm`` (1 MiB/message ceiling). Against a LAN peer
       that cannot read our ``/dev/shm``, they fall back to JPEG+base64,
       which is **lossy**.
    2. **``on_delta`` crosses a topic.** The callback still runs locally,
       but on a cshim daemon thread and with RELIABLE QoS — deltas can
       still be coalesced, so only "concatenated == final text" is safe
       to assert.
    3. **Exceptions arrive as ``RuntimeError``.** The worker's original
       exception type cannot survive the process boundary.
    4. **Calls time out.** ``infer_timeout_ms`` (15 s by default) against
       a local call that simply blocks.

    ``stop()`` does not interrupt the in-flight ``session_run`` — rknn3
    exposes no cooperative cancellation. It publishes a cancellation to
    the worker out-of-band; the worker notices once inference returns and
    reports ``stopped``, so the abort takes effect at the next safe point
    rather than waiting on any RPC reply.

    **Running several sessions in one process?** Pass the *same*
    ``side_hub=`` to all of them. Each session creates a DomainParticipant
    for its delta/stop topics, and participant names must be unique within
    a domain — two identically-named participants break discovery, and the
    second worker's RPCs then silently stop routing while the process still
    looks alive. Sharing one participant is also cheaper. The topics
    themselves cannot collide: they are ``<service_name>/delta`` and
    ``<service_name>/stop``.
    """

    def __init__(
        self,
        model: Any,
        *,
        service_name: str,
        hub: Optional[Hub] = None,
        requester: Optional[Requester] = None,
        side_hub: Optional[Hub] = None,
        is_remote: bool = False,
        infer_timeout_ms: int = _INFER_TIMEOUT_MS,
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
        on_client_recreated: Optional[Callable[[Requester], None]] = None,
        _svc: Optional[Service] = None,
        _owns_hub: bool = False,
        **options: Any,
    ) -> None:
        super().__init__(model, npu=None, device_id=None, **options)
        self.service_name = service_name
        self._infer_timeout_ms = int(infer_timeout_ms)
        self._timeout_ms = int(timeout_ms)
        self._on_client_recreated = on_client_recreated
        self._is_remote = bool(is_remote)
        self._closed = False
        self._client_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"wdnai-rpc-{service_name.rsplit('/', 1)[-1]}",
        )

        if hub is None and requester is None:
            hub = Hub(participant_name=f"wdnai-rpc-{os.getpid()}")
            hub.start()
            self._owns_hub = True
        self._hub = hub
        self._svc = _svc
        if requester is None:
            if _svc is None:
                if hub is None:
                    raise ValueError("need hub= or requester= to build LLMRPCSession")
                _svc = hub.create_service(service_name)
                self._svc = _svc
            requester = _svc.create_requester(timeout_ms=self._timeout_ms)
        self._client = requester

        # Side-channel topics live on their own participant so a delta or
        # cancel publish is never queued behind the replier's reply path.
        #
        # ``participant_name`` must be unique per participant in a domain:
        # two participants sharing a name break discovery, and the symptom
        # is silent — the second worker's RPCs stop being routed while the
        # process still looks healthy. That is why the default carries a
        # slice of the service name, and why a caller running several
        # sessions in one process should pass a single shared ``side_hub``.
        self._owns_side_hub = side_hub is None
        if self._owns_side_hub:
            tail = service_name.rsplit("/", 1)[-1][:40]
            self._side_hub = Hub(
                participant_name=f"wdnai-rpc-side-{tail}-{os.getpid()}")
            self._side_hub.start()
        else:
            self._side_hub = side_hub
        self._delta_topic = f"{service_name}/delta"
        self._stop_topic = f"{service_name}/stop"
        self._delta_reader: Optional[TopicReader] = None
        self._active: Optional[_StreamBridge] = None

    # ------------------------------------------------------------------ #
    # Transport seam
    # ------------------------------------------------------------------ #
    async def _dispatch(
        self,
        prompt: str,
        attachments: List[Attachment],
        *,
        on_delta: Optional[Callable[[str], None]] = None,
        **overrides: Any,
    ) -> Tuple[str, List[Attachment]]:
        if self._closed or self._client is None:
            raise RuntimeError("session is closed")

        bridge: Optional[_StreamBridge] = None
        stream_id = ""
        if on_delta is not None:
            stream_id = uuid.uuid4().hex[:16]
            bridge = _StreamBridge(stream_id, on_delta)
            self._active = bridge
            self._ensure_delta_reader(bridge)

        payload = {
            "action": _ACTION_INFER,
            "op": "respond",
            "v": WIRE_VERSION,
            "prompt": prompt,
            "attachments": encode_attachments(
                attachments, is_remote=self._is_remote),
            "options": dict(overrides),
            "stream_id": stream_id or None,
        }
        try:
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(
                self._executor,
                lambda: self._call_resilient(payload, self._infer_timeout_ms),
            )
            text, atts_out, stopped = self._decode_reply(reply)
            if stopped:
                raise RuntimeError(
                    f"LLMRPCSession[{self.service_name}]: generation stopped")
            return text, atts_out
        finally:
            if bridge is not None:
                # Ordering guarantee: every delta the worker published is
                # delivered before respond() returns.
                bridge.done.wait(_STREAM_END_TIMEOUT_S)
                self._active = None

    def _on_delta_sample(self, raw: bytes) -> None:
        """Topic callback (cshim daemon thread) → whichever call is active.

        One reader is created per session and never re-bound: rebinding it
        per call would drop samples that arrive between calls. The
        ``stream_id`` prefix in every sample is what keeps a late sample
        from a previous call from being mistaken for the current one.
        """
        bridge = self._active
        if bridge is not None:
            bridge.feed(raw)

    def _ensure_delta_reader(self, bridge: _StreamBridge) -> None:
        if self._delta_reader is not None:
            return
        # RELIABLE, not best_effort: FastDDS silently drops best-effort
        # samples and never surfaces the drop, which would render a
        # silently hole-punched stream to the user.
        self._delta_reader = create_topic_reader(
            self._side_hub, self._delta_topic, self._on_delta_sample,
            qos_kind="reliable",
        )

    def _decode_reply(self, reply: Dict[str, Any]) -> Tuple[str, List[Attachment], bool]:
        if not reply.get("ok"):
            raise RuntimeError(
                f"LLMRPCSession[{self.service_name}] transport error: "
                f"{reply.get('error', 'unknown')}")
        data = reply.get("data") or {}
        if not isinstance(data, dict):
            raise RuntimeError(
                f"LLMRPCSession[{self.service_name}]: malformed reply {data!r:.120}")
        status = data.get("status")
        if status == "error":
            raise RuntimeError(
                f"LLMRPCSession[{self.service_name}] worker error: "
                f"{data.get('error', 'unknown')}")

        text = data.get("text")
        if text is None and data.get("text_path"):
            text = _read_shm_text(data["text_path"])
        if text is None:
            text = ""
        return text, decode_attachments_out(data.get("attachments_out")), bool(
            data.get("stopped"))

    # ------------------------------------------------------------------ #
    # Failure / recovery
    # ------------------------------------------------------------------ #
    def _recreate_client(self) -> None:
        """Destroy a wedged requester and spin up a fresh one.

        Two orderings here are load-bearing, both learned the hard way:

        - ``destroy()`` runs inside the old requester's own lock. A
          destroy concurrent with an in-flight ``call`` is a C-level
          use-after-free and has been observed to corrupt replies or
          SIGSEGV outright.
        - ``Service.remove_requester(old)`` runs **before**
          ``create_requester()``. ``Service`` tracks requesters for
          lifecycle cleanup; skipping the removal makes that list grow
          without bound, leaking one Python object plus one dead C handle
          per recreate.
        """
        if self._svc is None:
            _LOG.warning("rpc[%s]: no Service ref, cannot recreate requester",
                         self.service_name)
            return
        with self._client_lock:
            old = self._client
            try:
                with old._lock:
                    old.destroy()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("rpc[%s]: destroy old requester raised %s",
                           self.service_name, exc)
            try:
                self._svc.remove_requester(old)
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("rpc[%s]: remove_requester(old) raised %s",
                           self.service_name, exc)
            try:
                self._client = self._svc.create_requester(
                    timeout_ms=self._timeout_ms)
            except Exception as exc:  # noqa: BLE001
                _LOG.error("rpc[%s]: recreate failed: %s", self.service_name, exc)
                return
            _LOG.info("rpc[%s]: requester recreated (was wedged)", self.service_name)
        if self._on_client_recreated is not None:
            try:
                self._on_client_recreated(self._client)
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("rpc[%s]: on_client_recreated raised %s",
                           self.service_name, exc)

    def _call_once(self, payload: Dict[str, Any], timeout_ms: int) -> Dict[str, Any]:
        """One attempt, recreating only on a *structural* wedge.

        A plain timeout must NOT trigger a recreate. While the worker is
        inside ``session_run`` its replier pump cannot answer anything —
        that deaf window is "the worker is busy", not "the channel is
        broken". Destroying the requester there would throw away the
        reply we are about to get and race destroy against call.
        """
        reply = self._client.call(payload, timeout_ms=timeout_ms)
        if reply.get("ok") or not reply.get("error"):
            return reply
        err = str(reply.get("error", "")).lower()
        wedged = ("send failed" in err or "no data" in err
                  or "write rc" in err or "discovery_pending" in err)
        if not wedged:
            return reply
        _LOG.warning("rpc[%s]: channel wedged (%s); recreating requester",
                     self.service_name, err)
        self._recreate_client()
        try:
            # Warm the discovery path so the retry doesn't re-hit the same
            # race that wedged us.
            self._client.call({"action": _ACTION_INFO}, timeout_ms=self._timeout_ms)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("rpc[%s]: post-recreate warmup raised %s",
                       self.service_name, exc)
        return self._client.call(payload, timeout_ms=timeout_ms)

    def _call_resilient(self, payload: Dict[str, Any], timeout_ms: int) -> Dict[str, Any]:
        """Retry ladder: plain retries, then one recreate, then give up.

        Non-transient failures (a worker-side business error, a malformed
        reply) are returned untouched — retrying them just burns time.
        """
        last: Dict[str, Any] = {}
        for _ in range(_SOFT_RETRIES + 1):
            last = self._call_once(payload, timeout_ms)
            if last.get("ok"):
                return last
            if not _is_transient(last.get("error")):
                return last
            _LOG.warning("rpc[%s]: transient failure (%s); retrying",
                         self.service_name, last.get("error"))
            time.sleep(0.1)

        for _ in range(_HARD_RETRIES):
            self._recreate_client()
            last = self._call_once(payload, timeout_ms)
            if last.get("ok"):
                return last
            if not _is_transient(last.get("error")):
                return last
        return last

    # ------------------------------------------------------------------ #
    # Interface
    # ------------------------------------------------------------------ #
    def call_op(
        self,
        op: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Invoke a worker-side ``extra_ops`` handler and return its reply.

        The escape hatch for application lifecycle verbs (``load`` /
        ``unload`` / ``shutdown``) that have no counterpart in the
        :class:`wdnai.LLMSession` interface but that the embedding worker
        still needs. Goes through the same retry ladder as everything
        else. The action field is always ``"info"`` — the cshim coerces
        unknown actions anyway, and the real discriminator travels in the
        body.
        """
        body: Dict[str, Any] = {
            "action": _ACTION_INFO, "op": op, "v": WIRE_VERSION,
        }
        body.update(payload or {})
        reply = self._call_resilient(
            body, timeout_ms if timeout_ms is not None else self._timeout_ms)
        if not reply.get("ok"):
            raise RuntimeError(
                f"LLMRPCSession[{self.service_name}] {op} transport error: "
                f"{reply.get('error', 'unknown')}")
        data = reply.get("data") or {}
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(
                f"LLMRPCSession[{self.service_name}] {op} worker error: "
                f"{data.get('error', 'unknown')}")
        return data

    def stop(self) -> None:
        """Ask the worker to abandon the current generation.

        Published to a side topic, never through the replier — which is busy
        running the inference we are trying to stop. Returns as soon as the
        topic has been written a few times (see ``_STOP_PUBLISH_RETRIES``);
        the worker aborts mid-run and reports ``stopped``.

        The payload carries the active ``stream_id`` because those retries
        outlive their turn: a stop naming a different turn is dropped
        instead of aborting whatever is running by then (which surfaced as
        "generation stopped" on the *next* question).
        """
        target = self._active.stream_id if self._active is not None else ""
        payload = f"stop:{target}".encode("utf-8")
        try:
            writer = create_topic_writer(
                self._side_hub, self._stop_topic, qos_kind="reliable")
            try:
                for _ in range(_STOP_PUBLISH_RETRIES):
                    writer.write(payload)
                    time.sleep(_STOP_PUBLISH_INTERVAL_S)
            finally:
                writer.close()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("rpc[%s]: stop publish failed: %s",
                         self.service_name, exc)

    def reset(self) -> None:
        self._call_resilient(
            {"action": _ACTION_RESET, "op": "reset", "v": WIRE_VERSION},
            self._timeout_ms,
        )

    def embed_text(self, text: str) -> np.ndarray:
        reply = self._call_resilient(
            {"action": _ACTION_INFO, "op": "embed_text",
             "v": WIRE_VERSION, "text": text},
            self._timeout_ms,
        )
        data = _require_data(reply, self.service_name, "embed_text")
        return np.asarray(data.get("vec") or [], dtype=np.float32).reshape(-1)

    def embed_video(self, frames: List[np.ndarray], prompt: str = "") -> np.ndarray:
        # Frames go over as the same /dev/shm descriptors ``respond`` uses
        # for its image attachments: raw BGR on the local path, JPEG+base64
        # against a LAN peer that can't read our tmpfs.
        #
        # (An earlier version wrote bare ``arr.tobytes()`` and read it back
        # with ``cv2.imdecode``, which expects an *encoded* blob — so every
        # frame came back as None and the whole call failed. Reusing the
        # attachment descriptor keeps the two paths from drifting again.)
        written: List[str] = []
        if frames and isinstance(frames[0], str):
            entries: List[Any] = list(frames)
        else:
            entries = []
            for f in frames:
                entry = _bgr_to_path(np.asarray(f))
                if entry.get("data_path"):
                    written.append(entry["data_path"])
                entries.append(entry)
        try:
            reply = self._call_resilient(
                {"action": _ACTION_INFO, "op": "embed_video",
                 "v": WIRE_VERSION, "frames": entries, "prompt": prompt or ""},
                self._timeout_ms,
            )
        finally:
            for p in written:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        data = _require_data(reply, self.service_name, "embed_video")
        return np.asarray(data.get("vec") or [], dtype=np.float32).reshape(-1)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._delta_reader is not None:
            self._delta_reader.close()
            self._delta_reader = None
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None
        if self._client is not None:
            try:
                with self._client._lock:
                    self._client.destroy()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("rpc[%s]: requester destroy raised %s",
                           self.service_name, exc)
            self._client = None
        if self._side_hub is not None:
            if self._owns_side_hub:
                self._side_hub.close()
            # A shared side hub outlives any one session; the caller that
            # created it owns the teardown.
            self._side_hub = None
        if self._owns_hub and self._hub is not None:
            self._hub.close()
            self._hub = None


def _is_transient(error: Any) -> bool:
    if not error:
        return False
    err = str(error).lower()
    return any(tok in err for tok in (
        "timeout", "send failed", "no data", "write rc", "discovery_pending"))


def _require_data(reply: Dict[str, Any], service: str, op: str) -> Dict[str, Any]:
    if not reply.get("ok"):
        raise RuntimeError(
            f"LLMRPCSession[{service}] {op} transport error: "
            f"{reply.get('error', 'unknown')}")
    data = reply.get("data") or {}
    if data.get("status") == "error":
        # Surface worker-side failures loudly: a silent empty embedding
        # would only show up much later as "0/N segments matched".
        raise RuntimeError(
            f"LLMRPCSession[{service}] {op} worker error: "
            f"{data.get('error', 'unknown')}")
    return data


# --------------------------------------------------------------------------- #
# Server side
# --------------------------------------------------------------------------- #
class ServedSession:
    """Handle returned by :func:`serve_local_session`.

    Owns the replier plus the side-channel topics, and the stop flag that
    the delta/stop topics feed.
    """

    def __init__(
        self,
        *,
        session: LLMSession,
        hub: Hub,
        side_hub: Hub,
        service: Service,
        replier: Replier,
        delta_writer: Optional[TopicWriter],
        owns_hub: bool,
        owns_side_hub: bool,
        infer_timeout_s: Optional[float] = None,
    ) -> None:
        self.session = session
        self.service_name = service.name
        self._hub = hub
        self._side_hub = side_hub
        self._service = service
        self._replier = replier
        self._delta_writer = delta_writer
        self._owns_hub = owns_hub
        self._owns_side_hub = owns_side_hub
        self._infer_timeout_s = infer_timeout_s
        self._stop_requested = False
        self._stop_lock = threading.Lock()
        self._stop_reader: Optional[TopicReader] = None
        # stream_id of the turn currently holding the session ("" = none).
        self._current_stream_id: Optional[str] = None
        self._extra_ops: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}

    # -- cancellation ---------------------------------------------------- #
    def request_stop(self, raw: bytes = b"") -> None:
        """Record the request **and** push it into the running inference.

        Forwarding to the session's ``stop()`` is what makes the abort take
        effect mid-generation; the flag alone is only read by
        :meth:`consume_stop`, after the turn already finished.

        ``raw`` is the wire payload ``b"stop:<stream_id>"``. A stop naming a
        different turn than the one running is **dropped** — the publisher
        retries for ~1s, so samples can outlive their turn, and honouring a
        stale one aborts the *next* question.

        Best-effort: a session without ``stop()``, or one that raises, still
        gets the flag and keeps the old end-of-run semantics.
        """
        target = self._decode_stop_target(raw)
        current = self._current_stream_id
        if target and current and target != current:
            _LOG.debug("serve[%s]: ignoring stale stop for %s (running %s)",
                       self.service_name, target, current)
            return
        with self._stop_lock:
            self._stop_requested = True
        session = getattr(self, "session", None)
        stop = getattr(session, "stop", None)
        if stop is not None:
            try:
                stop()
            except Exception as exc:  # noqa: BLE001 — soft stop is best-effort
                _LOG.warning("serve[%s]: session.stop() failed: %s",
                             self.service_name, exc)

    def consume_stop(self) -> bool:
        """Read-and-clear the stop flag."""
        with self._stop_lock:
            was = self._stop_requested
            self._stop_requested = False
            return was

    def session_was_stopped(self) -> bool:
        """True when the session itself reports the run was cut short."""
        session = getattr(self, "session", None)
        return bool(getattr(session, "was_stopped", lambda: False)())

    @staticmethod
    def _decode_stop_target(raw: bytes) -> str:
        """``b"stop:<stream_id>"`` → ``<stream_id>``; anything else → ``""``.

        Empty means "unscoped" — honoured as before, so a caller publishing a
        bare ``b"stop"`` keeps working.
        """
        text = raw.decode("utf-8", errors="replace")
        return text.split(":", 1)[1].strip() if ":" in text else ""

    # -- lifecycle ------------------------------------------------------- #
    def close(self) -> None:
        try:
            self._replier.destroy()
        except Exception:  # noqa: BLE001
            pass
        if self._stop_reader is not None:
            self._stop_reader.close()
            self._stop_reader = None
        if self._delta_writer is not None:
            self._delta_writer.close()
        if self._owns_side_hub:
            self._side_hub.close()
        if self._owns_hub:
            self._hub.close()


def serve_local_session(
    session: LLMSession,
    *,
    service_name: str,
    hub: Optional[Hub] = None,
    side_hub: Optional[Hub] = None,
    delta_writer: Optional[TopicWriter] = None,
    participant_name: Optional[str] = None,
    extra_ops: Optional[Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = None,
    infer_timeout_s: Optional[float] = None,
) -> ServedSession:
    """Expose a local :class:`wdnai.LLMSession` over DDS.

    The built-in ops are ``respond`` (carried by the ``infer`` action),
    ``info``, ``reset``, ``embed_text`` and ``embed_video`` — everything
    :class:`LLMRPCSession` can ask for.

    ``extra_ops`` maps an op name to ``handler(payload) -> reply_dict``,
    which is how an embedding application carries its own lifecycle verbs
    (``load`` / ``unload`` / ``shutdown``) without forking the transport,
    and how it replaces a built-in op that does not fit — an ``info``
    reporting the worker's own load state, or ``embed_text`` moved off
    the pump thread behind a timeout. A name in ``extra_ops`` takes
    precedence over the built-in of the same name; ``respond`` is the one
    op you cannot override.
    Two caveats:

    - The op name travels in the body, and the cshim only routes five
      *actions* (anything else is coerced to ``"info"``). That is fine —
      the whole client JSON is re-parsed out of the prompt field before
      dispatch, so ``op`` arrives intact either way.
    - A handler runs on the replier pump thread, so it **blocks every
      other request** for its duration and **cannot be cancelled**, same
      as ``respond``. Do not put anything long-running here.

    The replier handler is deliberately thin: it decodes attachments,
    calls :meth:`wdnai.LLMSession.run_prompt` (a finished prompt, no
    re-composition), and returns the raw text. It never sees ``generating``
    or ``tools``, because those never left the caller — schema parsing and
    tool dispatch happen over there, where those classes live.

    The handler runs on the cshim pump thread, which is single-threaded
    per service, so ``LLMLocalSession``'s own serialisation is not in
    contention with anything here.

    ``infer_timeout_s`` bounds how long ``respond`` may hold that pump.
    Set it whenever a backend can hang: a wedged NPU driver would
    otherwise pin the serial pump forever and take the whole worker deaf,
    not merely slow. The inference thread is a daemon and is abandoned
    on timeout, so the caller gets a clean error reply and the worker
    keeps serving — but the NPU slot stays occupied until the driver
    returns on its own. The default ``None`` (unbounded) is only right
    for a backend you trust to always return.
    """
    owns_hub = False
    if hub is None:
        hub = Hub(participant_name=participant_name or f"wdnai-serve-{os.getpid()}")
        hub.start()
        owns_hub = True
    service = hub.create_service(service_name)

    owns_side_hub = False
    if side_hub is None:
        side_hub = Hub(
            participant_name=participant_name or f"wdnai-serve-side-{os.getpid()}")
        side_hub.start()
        owns_side_hub = True

    # RELIABLE, not best_effort, for the same reason as the delta reader:
    # FastDDS drops best-effort samples without telling anyone.
    if delta_writer is None:
        delta_writer = create_topic_writer(
            side_hub, f"{service_name}/delta", qos_kind="reliable")

    served = ServedSession(
        session=session, hub=hub, side_hub=side_hub, service=service,
        replier=None, delta_writer=delta_writer,   # type: ignore[arg-type]
        owns_hub=owns_hub, owns_side_hub=owns_side_hub,
        infer_timeout_s=infer_timeout_s,
    )
    served._extra_ops = dict(extra_ops or {})

    # Cancellation rides its own topic, read on the side Hub: the replier's
    # pump thread is blocked inside session_run and could not consume it.
    served._stop_reader = create_topic_reader(
        side_hub, f"{service_name}/stop",
        lambda raw: served.request_stop(raw),
        qos_kind="reliable",
    )

    replier = service.create_replier(
        callback=lambda _svc, payload: _handle_request(served, payload))
    served._replier = replier
    return served


def _unwrap_payload(payload: Any) -> Dict[str, Any]:
    """Recover the client's JSON dict from what the replier hands us.

    The cshim carries the whole client JSON through the IDL's ``prompt``
    byte field and rebuilds ``{"action", "prompt", "every_n",
    "request_id"}``, where ``prompt`` is that JSON as an escaped *string*.
    """
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("prompt")
    if isinstance(inner, str) and inner.lstrip().startswith("{"):
        try:
            decoded = json.loads(inner)
            if isinstance(decoded, dict):
                return decoded
        except (ValueError, TypeError):
            pass
    return payload


def _handle_request(served: ServedSession, raw_payload: Any) -> Dict[str, Any]:
    payload = _unwrap_payload(raw_payload)
    op = payload.get("op") or "respond"

    # ``extra_ops`` wins over the built-in dispatch, for every op name —
    # not just unknown ones. An application that already owns an op
    # (``info`` reporting its own lifecycle state, ``embed_text`` running
    # off the pump thread with a timeout) has to be able to replace the
    # generic implementation rather than be stuck behind it.
    #
    # ``respond`` excepted: it is the transport, not a verb. The delta
    # bridge, the stop-flag consume and the shm text-path all hang off
    # it, so swapping it out would break streaming in ways that surface
    # much later as missing deltas.
    extra = served._extra_ops.get(op) if op != "respond" else None
    if extra is not None:
        try:
            reply = extra(payload)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("serve[%s]: extra op %r raised %s: %s",
                         served.service_name, op, type(exc).__name__, exc)
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        return reply if isinstance(reply, dict) else {"status": "ok", "result": reply}

    if op == "reset":
        served.session.reset()
        served.consume_stop()
        return {"status": "ok"}

    if op == "embed_text":
        vec = served.session.embed_text(payload.get("text") or "")
        return {"status": "ok", "vec": np.asarray(vec).reshape(-1).tolist()}

    if op == "embed_video":
        # Same descriptor shape as ``respond``'s image attachments, so a
        # frame's bytes and its shape stay coupled. Decoding through
        # ``decode_attachments`` keeps one implementation of "raw BGR vs
        # JPEG base64" instead of two that can disagree.
        frames = [att.images[0] for att in decode_attachments(
            payload.get("frames")) if isinstance(att, ImageAttachment)]
        vec = served.session.embed_video(frames, payload.get("prompt") or "")
        return {"status": "ok", "vec": np.asarray(vec).reshape(-1).tolist()}

    if op == "info":
        return {
            "status": "ok",
            "wire_version": WIRE_VERSION,
            "kind": getattr(getattr(served.session, "spec", None), "kind", None),
        }

    if op != "respond":
        return {"status": "error", "error": f"unknown op {op!r}"}

    # --- respond ---------------------------------------------------------
    stream_id = payload.get("stream_id") or ""
    attachments = decode_attachments(payload.get("attachments"))
    options = payload.get("options") or {}

    # This turn owns the session: publish its id so a stop sample can tell
    # "meant for this turn" from "left over from the previous one", and drop
    # any flag that landed before this turn started.
    served._current_stream_id = stream_id
    served.consume_stop()

    bridge: Optional[Callable[[str], None]] = None
    if stream_id and served._delta_writer is not None:
        writer = served._delta_writer

        def _publish(delta: str, _sid: str = stream_id, _w: TopicWriter = writer) -> None:
            _w.write(f"{_sid}\n{delta}".encode("utf-8"))

        bridge = _publish

    try:
        text, att_out = _run_coroutine(
            served.session.run_prompt(
                payload.get("prompt") or "",
                attachments or None,
                on_delta=bridge,
                **options,
            ),
            timeout_s=served._infer_timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("serve[%s]: respond raised %s: %s",
                     served.service_name, type(exc).__name__, exc)
        if stream_id:
            _publish_stream_end(served, stream_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    # Publish the terminal marker *before* returning, so the caller has a
    # definite "no more deltas" signal instead of a timing guess.
    if stream_id:
        _publish_stream_end(served, stream_id)

    stopped = served.consume_stop() or served.session_was_stopped()
    reply: Dict[str, Any] = {"status": "ok", "text": text}
    att_out_enc = encode_attachments_out(att_out)
    if att_out_enc:
        reply["attachments_out"] = att_out_enc
    if stopped:
        reply["stopped"] = True
    # Keep the reply small; the 1 MiB ceiling is not reachable from Python
    # so a long completion has to travel as a pointer.
    if len(text) > _INLINE_TEXT_LIMIT:
        reply.pop("text")
        reply["text_path"] = _write_shm(
            text.encode("utf-8"), _REPLY_SHM_PREFIX)
    return reply


def _publish_stream_end(served: ServedSession, stream_id: str) -> None:
    writer = served._delta_writer
    if writer is None:
        return
    try:
        writer.write(stream_id.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("serve[%s]: stream-end publish failed: %s",
                   served.service_name, exc)


def _run_coroutine(coro: Any, timeout_s: Optional[float] = None) -> Any:
    """Drive a coroutine from the replier's synchronous pump thread.

    A fresh loop per request is deliberate: the pump thread serves one
    request at all and the work is dominated by a multi-second C call, so
    loop setup cost is noise. ``LLMLocalSession`` does its actual work on
    its own executor, so this loop does nothing but wait on it.

    ``timeout_s`` exists because the pump thread is a scarce, serial
    resource: a backend that hangs inside its C call — a wedged NPU
    driver, a ``clear_kvcache`` that blocks for 30 s — would otherwise pin
    it forever and turn one bad request into a permanently deaf worker
    (every later ``info`` / ``reset`` / infer queues behind it, and the
    client just sees timeouts). With a bound, the pump is released after
    ``timeout_s`` and the caller gets a clean error reply instead.

    The abandoned thread is a daemon and is deliberately **not** joined;
    there is no safe way to kill a thread stuck in C. The NPU slot stays
    busy until the driver returns on its own, so callers should treat a
    timeout as "this worker needs restarting", not as a retryable error.
    """
    if timeout_s is None:
        return asyncio.run(coro)

    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    th = threading.Thread(target=_target, daemon=True,
                          name=f"ai-serve-{threading.current_thread().name}")
    th.start()
    th.join(timeout_s)
    if "error" in box:
        raise box["error"]
    if "value" in box:
        return box["value"]
    raise TimeoutError(f"inference exceeded {timeout_s}s")


def _install_boot_sweeps() -> None:
    """Sweep leftover side-files at import and at interpreter exit.

    The boot sweep keeps a 1 h floor so that two processes starting at
    once cannot delete each other's in-flight files; the exit sweep
    clears everything.
    """
    try:
        sweep_stale_shm(3600.0)
        atexit.register(sweep_stale_shm, 0.0)
    except Exception:  # noqa: BLE001 — never block import on cleanup
        pass


_install_boot_sweeps()


__all__ = [
    "LLMRPCSession",
    "ServedSession",
    "serve_local_session",
    "encode_attachments",
    "decode_attachments",
    "encode_attachments_out",
    "decode_attachments_out",
    "sweep_stale_shm",
    "WIRE_VERSION",
]