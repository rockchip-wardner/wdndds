"""Regression test for the replier reply-buffer lifetime bug.

``Replier._on_request_c`` hands the serialized reply to the cshim through
a ctypes out-param, and the cshim copies that pointer **after** the Python
callback returns. The out-param was the only reference to the ``bytes``
object, so it was dropped the moment the callback frame unwound — and any
allocation the handler did in between (an asyncio loop, a model inference)
recycled the buffer before C could copy it. The caller then got a reply
whose leading bytes were garbage while the tail survived.

The bug was invisible to a handler that returned immediately, which is why
it survived in the wild: a trivial echo round-trips clean, a real one does
not.

Fixed by pinning the buffer on the Replier until the next request.
"""
from __future__ import annotations

import asyncio
import os
import sys

from wdndds.dds._rpc import Hub

PAYLOAD = {"status": "ok", "text": "the quick brown fox"}
CALLS = 8


def _run(churn: bool) -> tuple[int, int]:
    """Return (failures, corruptions) over CALLS round trips."""
    name = f"wdndds/rpc/replybuf-{os.getpid()}-{int(churn)}"

    server = Hub(participant_name=f"srv-{os.getpid()}-{int(churn)}")
    server.start()

    def handler(_name, _payload):
        reply = dict(PAYLOAD)
        if churn:
            # Stand in for whatever a real handler does: allocate, block on
            # an event loop, free. This is what used to eat the buffer.
            for _ in range(20):
                asyncio.run(asyncio.sleep(0))
                junk = [bytearray(4096) for _ in range(64)]
                del junk
        return reply

    server.create_service(name).create_replier(callback=handler)

    client = Hub(participant_name=f"cli-{os.getpid()}-{int(churn)}")
    client.start()
    req = client.create_service(name).create_requester(timeout_ms=10_000)

    failures = corruptions = 0
    for _ in range(CALLS):
        rep = req.call({"action": "infer", "op": "x", "prompt": "hi"},
                       timeout_ms=10_000)
        if rep.get("ok") and rep.get("data") == PAYLOAD:
            continue
        failures += 1
        # A timeout is the first-call discovery race, not corruption.
        if "timeout" not in str(rep.get("error", "")):
            corruptions += 1

    client.close()
    server.close()
    return failures, corruptions


def test_trivial_handler_round_trips():
    _, corrupt = _run(churn=False)
    assert corrupt == 0


def test_handler_that_allocates_round_trips():
    """The regression: this returned garbage on every call before the fix."""
    failures, corrupt = _run(churn=True)
    assert corrupt == 0, f"{corrupt}/{CALLS} replies came back corrupted"


if __name__ == "__main__":  # pragma: no cover
    failures = 0
    for _name, fn in sorted(globals().items()):
        if not _name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {_name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {_name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
