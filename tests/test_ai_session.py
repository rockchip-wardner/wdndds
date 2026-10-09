"""Loopback tests for :mod:`wdndds.ai_session`.

These exercise the *real* DDS round trip — real cshim, real replier pump
thread, real ``/dev/shm`` side-files, real side-channel topics — against a
stub session, so they need no NPU and no model weights.

They pin the property the whole interface split rests on: the same call
surface and the same *result types* whether the model is in this process
or across a wire.

Run with::

    python -m pytest wdndds/tests/test_ai_session.py -v

or, without pytest installed::

    python -c "import tests.test_ai_session as t; t.test_*()"
"""
import asyncio
import glob
import os
from typing import Any, List, Optional, Tuple

import numpy as np
from pydantic import BaseModel

# NOTE: deliberately NO ``from __future__ import annotations`` in this
# module — ``@Generable`` reads ``__annotations__`` at class-creation time
# and needs real types, not the strings PEP 563 would substitute.
try:  # optional — the module also runs standalone via __main__
    import pytest  # noqa: F401
except ImportError:
    pytest = None

import wdnai
from wdnai import (
    Attachment, AudioAttachment, Callable, Field, ImageAttachment,
    TextAttachment,
)
from wdnai.model import ModelSpec

from wdndds.ai_session import (
    LLMRPCSession, decode_attachments, encode_attachments, serve_local_session,
)


def _spec(kind: str = "llm") -> ModelSpec:
    return ModelSpec(name=f"stub-{kind}", kind=kind, variant="qwen3")


# --------------------------------------------------------------------------- #
# Stub session — a real wdnai.LLMSession, but with a scripted backend
# --------------------------------------------------------------------------- #
class StubSession(wdnai.LLMSession):
    """Minimal in-process session: echoes a canned reply, streams deltas.

    Subclasses the real interface so the test covers the abstract seam
    rather than a parallel fake. ``_dispatch`` is the only thing a
    transport has to implement, so overriding exactly that is enough.
    """

    def __init__(self, reply: str = "hello world", *, kind: str = "llm"):
        super().__init__(_spec(kind))
        self.reply = reply
        self.prompts: List[str] = []
        self.attachment_counts: List[int] = []
        self.closed = False

    async def _dispatch(
        self,
        prompt: str,
        attachments: List[Attachment],
        *,
        on_delta: Optional[Any] = None,
        **overrides: Any,
    ) -> Tuple[str, List[Attachment]]:
        self.prompts.append(prompt)
        self.attachment_counts.append(len(attachments))
        if on_delta is not None:
            # Stream in 3-char chunks so the test sees more than one sample.
            for i in range(0, len(self.reply), 3):
                on_delta(self.reply[i:i + 3])
        return self.reply, []

    def reset(self) -> None:
        self.prompts.append("<reset>")

    def close(self) -> None:
        self.closed = True

    def embed_text(self, text: str):
        import numpy as np
        return np.ones(4, dtype=np.float32)

    def embed_video(self, frames, prompt: str = ""):
        import numpy as np
        return np.ones(4, dtype=np.float32)

    async def run_prompt(self, prompt, attachment=None, *, on_delta=None, **overrides):
        return await wdnai.LLMSession.run_prompt(
            self, prompt, attachment, on_delta=on_delta, **overrides)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
class _Harness:
    """One served stub session plus a matching LLMRPCSession."""

    def __init__(self, session: StubSession, service_name: str):
        self.session = session
        self.served = serve_local_session(session, service_name=service_name)
        # The replier needs a discovery grace period before the first call.
        self.client = LLMRPCSession(
            model=session.spec, service_name=service_name,
            timeout_ms=10_000, infer_timeout_ms=20_000,
        )
        self._warmup()

    def _warmup(self) -> None:
        for _ in range(40):
            try:
                reply = self.client._call_once(
                    {"action": "info", "op": "info"}, 5_000)
                if reply.get("ok"):
                    return
            except Exception:  # noqa: BLE001
                pass
            asyncio.run(asyncio.sleep(0.25))
        raise RuntimeError("replier never became reachable")

    def close(self) -> None:
        self.client.close()
        self.served.close()


def _harness(reply: str = "hello world", **kw) -> _Harness:
    name = f"wdndds/rpc/test-{os.getpid()}-{id(reply)}{abs(hash(reply)) % 9973}"
    return _Harness(StubSession(reply, **kw), name)


# --------------------------------------------------------------------------- #
# 1. Signature / interface parity
# --------------------------------------------------------------------------- #
def test_both_impls_share_one_signature():
    """The interface split is only worth anything if callers can't tell."""
    import inspect

    a = inspect.signature(wdnai.LLMLocalSession.respond)
    b = inspect.signature(LLMRPCSession.respond)
    assert str(a) == str(b)
    for name in ("reset", "stop", "close", "embed_text", "embed_video"):
        assert hasattr(LLMRPCSession, name), name
        assert hasattr(wdnai.LLMLocalSession, name), name


# --------------------------------------------------------------------------- #
# 2. Result parity: local == RPC
# --------------------------------------------------------------------------- #
def test_rpc_matches_local_result():
    text = "the quick brown fox"
    local = StubSession(text)
    h = _harness(text)
    try:
        local_out = asyncio.run(local.respond(prompt="hi"))
        rpc_out = asyncio.run(h.client.respond(prompt="hi"))
        assert local_out == rpc_out == text
        # Same prompt reaches the worker, byte for byte.
        assert h.session.prompts[0] == "hi"
    finally:
        h.close()


def test_respond_composes_prompt_exactly_once():
    """The worker must see the caller's composed prompt, unaltered.

    ``respond()`` normalises the prompt through ``_compose_prompt``
    (which rstrips it) — that has always been true and is true for the
    local session too. The property that matters is that the worker does
    **not** compose a second time: whatever ``respond`` produced is what
    the model gets.
    """
    h = _harness()
    try:
        prompt = "  describe  "
        asyncio.run(h.client.respond(prompt=prompt))
        expected = wdnai.LLMSession._compose_prompt(prompt, None, [])
        assert expected == "  describe"      # rstrip only, not strip
        assert h.session.prompts[0] == expected
    finally:
        h.close()


def test_run_prompt_forwards_trailing_newline():
    """``run_prompt`` must not re-compose — not even an rstrip.

    This is why the serve side calls ``run_prompt`` instead of
    ``respond``: ``respond``'s ``rstrip`` would eat the trailing newline
    of a ChatML transcript and change what the model sees.
    """
    h = _harness()
    try:
        prompt = "system\nYou are a helpful assistant.\nuser\nA\nassistant\n"
        asyncio.run(h.client.run_prompt(prompt))
        assert h.session.prompts[0] == prompt
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# 3. Schema instances survive the wire
# --------------------------------------------------------------------------- #
@wdnai.Generable
class _TargetDoc(BaseModel):
    """A schema the transport has never heard of."""

    label: str = Field(description="label")
    score: int = Field(default=0, description="score")


def test_structured_output_is_a_real_instance():
    """Any schema works — no per-schema table on either side."""
    payload = '{"label": "car", "score": 7}'
    h = _harness(payload)
    try:
        out = asyncio.run(h.client.respond(prompt="describe", generating=_TargetDoc))
        assert isinstance(out, _TargetDoc), type(out)
        assert out.label == "car" and out.score == 7
    finally:
        h.close()


def test_tool_call_is_a_real_instance():
    from wdnai._callable import get_tool_specs

    @Callable
    class Echo:
        text: str = Field(description="text to echo")

        def __call__(self):
            return self.text

    reply = 'ok <tool_call>{"name": "Echo", "arguments": {"text": "pong"}}</tool_call>'
    h = _harness(reply)
    try:
        out = asyncio.run(h.client.respond(prompt="echo", tools=[Echo]))
        assert isinstance(out, Echo), type(out)
        assert out() == "pong"
        # And the tool definitions really were inlined into the prompt.
        assert "Echo" in h.session.prompts[0]
        assert get_tool_specs([Echo])
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# 4. Nothing schema-shaped leaks to the worker
# --------------------------------------------------------------------------- #
def test_wire_carries_no_generating_or_tools():
    """AC: schema/tool identity never crosses the boundary.

    This is what lets the worker stay a thin RPC server — it has no way to
    resolve a class it was never told about.
    """
    seen: List[dict] = []
    h = _harness('{"label": "x", "score": 1}')
    try:
        original = h.client._call_resilient

        def spy(payload, timeout_ms):
            seen.append(payload)
            return original(payload, timeout_ms)

        h.client._call_resilient = spy
        out = asyncio.run(h.client.respond(prompt="p", generating=_TargetDoc))

        assert isinstance(out, _TargetDoc)
        assert seen, "client never issued a request"
        for payload in seen:
            assert "generating" not in payload, payload.keys()
            assert "tools" not in payload, payload.keys()
            # Prompt carries the schema hint inline instead.
            assert "_TargetDoc" in payload["prompt"] or "label" in payload["prompt"]
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# 5. Streaming
# --------------------------------------------------------------------------- #
def test_on_delta_concatenates_to_final_text():
    text = "streaming deltas must reassemble exactly"
    h = _harness(text)
    try:
        got: List[str] = []
        out = asyncio.run(h.client.respond(prompt="p", on_delta=got.append))
        assert got, "no deltas crossed the topic"
        assert "".join(got) == out == text
    finally:
        h.close()


def test_no_deltas_when_callback_absent():
    h = _harness("quiet")
    try:
        assert asyncio.run(h.client.respond(prompt="p")) == "quiet"
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# 6. Attachments
# --------------------------------------------------------------------------- #
def _shm_snapshot() -> set:
    return set(glob.glob("/dev/shm/wdndds_ai_*"))


def test_attachment_round_trip_is_byte_exact():
    img = np.random.randint(0, 255, (12, 16, 3), dtype=np.uint8)
    before = _shm_snapshot()
    encoded = encode_attachments(ImageAttachment(img))
    decoded = decode_attachments(encoded)
    assert len(decoded) == 1 and isinstance(decoded[0], ImageAttachment)
    assert np.array_equal(decoded[0].images[0], img)
    # Nothing this test wrote is left behind.
    assert _shm_snapshot() == before


def test_text_and_audio_attachments_round_trip():
    audio = b"\x00\x01\x02" * 100
    before = _shm_snapshot()
    encoded = encode_attachments([
        TextAttachment("prefix"),
        AudioAttachment(audio, mime_type="audio/wav"),
    ])
    decoded = decode_attachments(encoded)
    kinds = [type(d).__name__ for d in decoded]
    assert kinds == ["TextAttachment", "AudioAttachment"], kinds
    assert decoded[0].text == "prefix"
    assert decoded[1].data == audio
    assert _shm_snapshot() == before


def test_missing_side_file_degrades_to_none():
    """A worker death mid-flight must not raise — it degrades."""
    from wdndds.ai_session import _read_shm_text

    assert _read_shm_text("/dev/shm/wdndds_ai_reply_does_not_exist.bin") is None


# --------------------------------------------------------------------------- #
# 7. Long replies travel out-of-band (1 MiB ceiling is not reachable in Python)
# --------------------------------------------------------------------------- #
def test_long_reply_uses_shm_pointer():
    long_text = "x" * 4000
    before = _shm_snapshot()
    h = _harness(long_text)
    try:
        assert asyncio.run(h.client.respond(prompt="p")) == long_text
        assert _shm_snapshot() == before
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# 8. Embedding passthroughs
# --------------------------------------------------------------------------- #
def test_embed_text_over_rpc():
    h = _harness()
    try:
        import numpy as np
        vec = h.client.embed_text("query")
        assert vec.shape == (4,) and vec.dtype == np.float32
    finally:
        h.close()


def test_reset_over_rpc():
    h = _harness()
    try:
        h.client.reset()
        assert "<reset>" in h.session.prompts
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# 9. Cancellation is out-of-band
# --------------------------------------------------------------------------- #
def test_stop_is_published_and_reported():
    h = _harness("some long generation")
    try:
        from wdndds.dds.topic import create_topic_writer

        writer = create_topic_writer(
            h.client._side_hub, f"{h.client.service_name}/stop",
            qos_kind="reliable")
        try:
            writer.write(b"stop")
        finally:
            writer.close()
        # Give the stop reader a moment to consume the sample.
        asyncio.run(asyncio.sleep(0.5))
        assert h.served.consume_stop() is True
    finally:
        h.close()


# --------------------------------------------------------------------------- #
# 10. extra_ops escape hatch (what wdnwebui's worker needs for lifecycle verbs)
# --------------------------------------------------------------------------- #
def test_extra_ops_round_trip():
    seen = {}

    name = f"wdndds/rpc/extra-{os.getpid()}"
    session = StubSession("served")
    served = serve_local_session(
        session, service_name=name,
        extra_ops={"load": lambda p: {"status": "ok", "npu": p.get("npu_index"),
                                      "saw": "load"}},
    )
    client = LLMRPCSession(model=session.spec, service_name=name,
                           timeout_ms=10_000, infer_timeout_ms=20_000)
    try:
        for _ in range(40):
            try:
                if client._call_once({"action": "info", "op": "info"}, 5_000).get("ok"):
                    break
            except Exception:  # noqa: BLE001
                pass
            asyncio.run(asyncio.sleep(0.25))
        out = client.call_op("load", {"npu_index": 1})
        assert out["npu"] == 1 and out["saw"] == "load"
        seen.update(out)
    finally:
        client.close()
        served.close()
    assert seen, "extra op never returned"


def test_extra_ops_error_is_reported_not_swallowed():
    name = f"wdndds/rpc/extraerr-{os.getpid()}"
    session = StubSession("served")

    def boom(_payload):
        raise ValueError("nope")

    served = serve_local_session(session, service_name=name,
                                 extra_ops={"unload": boom})
    client = LLMRPCSession(model=session.spec, service_name=name,
                           timeout_ms=10_000, infer_timeout_ms=20_000)
    try:
        for _ in range(40):
            try:
                if client._call_once({"action": "info", "op": "info"}, 5_000).get("ok"):
                    break
            except Exception:  # noqa: BLE001
                pass
            asyncio.run(asyncio.sleep(0.25))
        try:
            client.call_op("unload")
            raise AssertionError("extra op error was swallowed")
        except RuntimeError as exc:
            assert "ValueError" in str(exc), exc
    finally:
        client.close()
        served.close()


def test_unknown_op_errors():
    name = f"wdndds/rpc/unknown-{os.getpid()}"
    session = StubSession("served")
    served = serve_local_session(session, service_name=name)
    client = LLMRPCSession(model=session.spec, service_name=name,
                           timeout_ms=10_000, infer_timeout_ms=20_000)
    try:
        for _ in range(40):
            try:
                if client._call_once({"action": "info", "op": "info"}, 5_000).get("ok"):
                    break
            except Exception:  # noqa: BLE001
                pass
            asyncio.run(asyncio.sleep(0.25))
        try:
            client.call_op("no_such_op")
            raise AssertionError("unknown op did not error")
        except RuntimeError as exc:
            assert "unknown op" in str(exc), exc
    finally:
        client.close()
        served.close()


def test_extra_ops_override_a_builtin_op():
    """An ``extra_ops`` name must win over the built-in of the same name.

    This is what lets an application report its own lifecycle through
    ``info`` instead of the generic ``{status, wire_version, kind}``, and
    move ``embed_text`` off the replier pump thread. ``respond`` is the
    one op that stays untouchable.
    """
    name = f"wdndds/rpc/override-{os.getpid()}"
    session = StubSession("served")
    served = serve_local_session(
        session, service_name=name,
        extra_ops={
            "info": lambda p: {"status": "running", "npu": p.get("npu_index")},
            "embed_text": lambda p: {"status": "ok", "vec": [7.0, 8.0],
                                     "via": "extra"},
        },
    )
    client = LLMRPCSession(model=session.spec, service_name=name,
                           timeout_ms=10_000, infer_timeout_ms=20_000)
    try:
        for _ in range(40):
            try:
                if client._call_once({"action": "info", "op": "info"}, 5_000).get("ok"):
                    break
            except Exception:  # noqa: BLE001
                pass
            asyncio.run(asyncio.sleep(0.25))
        out = client.call_op("info", {"npu_index": 2})
        assert out["status"] == "running" and out["npu"] == 2, out
        assert "wire_version" not in out, f"builtin info leaked through: {out}"
        vec = client.embed_text("hello")
        assert list(vec) == [7.0, 8.0], vec
    finally:
        client.close()
        served.close()


def test_extra_ops_cannot_override_respond():
    """``respond`` is the transport itself — no handler may replace it."""
    name = f"wdndds/rpc/respondguard-{os.getpid()}"
    session = StubSession("served")
    served = serve_local_session(
        session, service_name=name,
        extra_ops={"respond": lambda p: {"status": "ok", "text": "HIJACKED"}},
    )
    client = LLMRPCSession(model=session.spec, service_name=name,
                           timeout_ms=10_000, infer_timeout_ms=20_000)
    try:
        for _ in range(40):
            try:
                if client._call_once({"action": "info", "op": "info"}, 5_000).get("ok"):
                    break
            except Exception:  # noqa: BLE001
                pass
            asyncio.run(asyncio.sleep(0.25))
        out = asyncio.run(client.respond(prompt="say hi"))
        assert out == session.reply, out
        assert "HIJACKED" not in str(out), out
    finally:
        client.close()
        served.close()


if __name__ == "__main__":  # pragma: no cover
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    raise SystemExit(1 if failures else 0)