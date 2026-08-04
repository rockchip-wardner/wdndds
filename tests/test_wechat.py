"""Tests for the synchronous ``wdiot.Wechat`` facade.

The real ``wechatbot.WeChatBot`` would make a network round-trip the moment
it's constructed (it sets up an ``aiohttp.ClientSession``). We monkey-patch
it out with a fake that records calls and lets us drive the state machine
deterministically.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from unittest.mock import patch

import pytest

import wdiot
from wdiot import Wechat
from wdiot.wechat import DEFAULT_CRED_PATH


# --------------------------------------------------------------------- fakes

class _FakeBot:
    """In-memory replacement for ``wechatbot.WeChatBot``.

    Captures the callbacks so tests can drive them, and records the
    coroutines passed to ``start`` / ``login`` / ``send`` so we can assert
    what the wrapper is scheduling.
    """

    last_instance: "_FakeBot | None" = None

    def __init__(
        self,
        *,
        base_url=None,
        cred_path=None,
        on_qr_url=None,
        on_scanned=None,
        on_expired=None,
        on_error=None,
        on_verify_code=None,
        bot_agent=None,
    ) -> None:
        self.kw = {
            "cred_path": cred_path,
            "bot_agent": bot_agent,
        }
        self._on_qr_url = on_qr_url
        self._on_scanned = on_scanned
        self._on_expired = on_expired
        self._on_error = on_error
        self._message_handlers: list[Callable] = []
        self._stopped = False
        self._started = False
        self._context_tokens: dict[str, str] = {}
        type(self).last_instance = self

    def on_message(self, handler: Callable) -> Callable:
        self._message_handlers.append(handler)
        return handler

    async def login(self, *, force: bool = False):
        # Drive the QR flow when no creds on disk; otherwise short-circuit
        # (mirrors the real SDK).
        if not force and self.kw["cred_path"] and self.kw["cred_path"].endswith("has-creds.json"):
            return _FakeCredentials()
        self._on_qr_url("weixin://fake/qr")
        # ``await asyncio.sleep(0)`` yields once so wrappers that have
        # scheduled sibling tasks (long-poll) can settle.
        await _sleep(0)
        self._on_scanned()
        await _sleep(0)
        creds = _FakeCredentials()
        self._context_tokens["wx_user"] = "ctx-token"
        return creds

    async def start(self):
        self._started = True
        try:
            while not self._stopped:
                await _sleep(0.05)
        finally:
            self._started = False

    def stop(self) -> None:
        self._stopped = True

    async def send(self, user_id: str, text: str):
        if user_id not in self._context_tokens:
            # Re-raise the SDK's own exception so the wrapper propagates it.
            from wechatbot.errors import NoContextError

            raise NoContextError(user_id)
        return {"ok": True}

    async def send_media(self, user_id: str, content: dict):
        if user_id not in self._context_tokens:
            from wechatbot.errors import NoContextError

            raise NoContextError(user_id)
        return {"ok": True}

    # Test helpers ---------------------------------------------------
    def emit_message(self, msg):
        for h in self._message_handlers:
            h(msg)


class _FakeCredentials:
    def __init__(self) -> None:
        self.token = "tok"
        self.base_url = "https://ilinkai.weixin.qq.com"
        self.account_id = "acct"
        self.user_id = "wx_user"
        self.saved_at = "2026-01-01T00:00:00Z"


async def _sleep(secs: float) -> None:
    import asyncio

    await asyncio.sleep(secs)


# --------------------------------------------------------------------- tests

@pytest.fixture(autouse=True)
def _patch_wechatbot(monkeypatch):
    """Replace ``wechatbot.WeChatBot`` with the in-memory fake for every test."""
    monkeypatch.setattr("wdiot.wechat.WeChatBot", _FakeBot)
    _FakeBot.last_instance = None
    yield


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll ``predicate()`` at 10 ms until it returns True or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_class_exports():
    assert wdiot.Wechat is Wechat
    assert "Wechat" in wdiot.__all__
    # Error aliases for nested-attribute style:
    assert hasattr(Wechat, "NoContextError")
    assert hasattr(Wechat, "AuthError")
    assert hasattr(Wechat, "MediaError")


def test_constructor_does_not_block_on_network():
    """``__init__`` only spins up the loop + bot; it doesn't network."""
    t0 = time.monotonic()
    wc = Wechat(cred_path="/tmp/wdiot-test-creds.json")
    elapsed = time.monotonic() - t0
    wc.close()
    # Should be quick (sub-second); proves we don't call ``bot.login()`` here.
    assert elapsed < 2.0, f"constructor blocked for {elapsed:.2f}s"


def test_default_cred_path():
    assert DEFAULT_CRED_PATH.name == "credentials.json"
    assert str(DEFAULT_CRED_PATH).startswith(str(__import__("pathlib").Path.home().as_posix()))


def test_env_override(monkeypatch, tmp_path):
    custom = tmp_path / "wc.json"
    monkeypatch.setenv("WDIOT_WECHAT_CRED_PATH", str(custom))
    wc = Wechat()
    try:
        assert wc.cred_path == str(custom)
    finally:
        wc.close()


def test_start_login_then_confirmed_runs_long_poll():
    wc = Wechat(cred_path="/tmp/wdiot-test-creds.json")
    try:
        wc.start_login(force=True)
        # The fake bot completes login instantly, so the state races
        # through ``awaiting_scan`` → ``scanned`` → ``confirmed``. The real
        # SDK has a 2 s poll interval that makes the intermediate states
        # observable; for the wrapper test we just wait for the terminal
        # state + long-poll startup.
        assert _wait_until(
            lambda: wc.started and wc.login_status()["state"] == "confirmed",
            timeout=3.0,
        )
        assert wc.user_id == "wx_user"
        assert wc.login_status()["error"] is None
    finally:
        wc.close()


def test_login_qr_callback_can_be_observed_with_slow_fake(monkeypatch):
    """A slower fake that inserts a yield between QR and confirm lets us
    observe the ``awaiting_scan`` state from the outside. This is closer
    to real-SDK timing and proves the callback is wired through the loop.
    """
    import asyncio as _asyncio

    class SlowFake(_FakeBot):
        async def login(self, *, force: bool = False):
            self._on_qr_url("weixin://slow/qr")
            await _asyncio.sleep(0.2)
            self._on_scanned()
            await _asyncio.sleep(0.05)
            return _FakeCredentials()

    monkeypatch.setattr("wdiot.wechat.WeChatBot", SlowFake)
    wc = Wechat(cred_path="/tmp/wdiot-test-creds.json")
    try:
        wc.start_login(force=True)
        # Within the 200 ms QR window we should see ``awaiting_scan``.
        time.sleep(0.05)
        snap = wc.login_status()
        assert snap["state"] == Wechat.STATE_AWAITING_SCAN, snap
        assert snap["qr_url"] == "weixin://slow/qr"
        # Then transitions to ``scanned``.
        assert _wait_until(
            lambda: wc.login_status()["state"] == Wechat.STATE_SCANNED,
            timeout=1.0,
        )
        # Finally ``confirmed`` + long-poll.
        assert _wait_until(
            lambda: wc.started and wc.login_status()["state"] == "confirmed",
            timeout=3.0,
        )
    finally:
        wc.close()


def test_send_text_records_outbound():
    wc = Wechat(cred_path="/tmp/wdiot-test-creds.json")
    try:
        wc.start_login(force=True)
        assert _wait_until(lambda: wc.started, timeout=3.0)

        wc.send_text("wx_user", "hello")
        msgs = wc.recent_messages(limit=10)
        assert any(m["direction"] == "out" and m["text"] == "hello" for m in msgs)
    finally:
        wc.close()


def test_send_without_context_raises():
    wc = Wechat(cred_path="/tmp/wdiot-test-creds.json")
    try:
        wc.start_login(force=True)
        assert _wait_until(lambda: wc.started, timeout=3.0)

        with pytest.raises(Wechat.NoContextError):
            wc.send_text("unknown_user", "hi")
    finally:
        wc.close()


def test_inbound_message_appears_in_recent():
    wc = Wechat(cred_path="/tmp/wdiot-test-creds.json")
    try:
        wc.start_login(force=True)
        assert _wait_until(lambda: wc.started, timeout=3.0)

        from wechatbot import IncomingMessage

        fake_msg = IncomingMessage(
            user_id="wx_user",
            text="hello bot",
            type="text",
            timestamp=datetime.now(timezone.utc),
            images=[], voices=[], files=[], videos=[],
            quoted_message=None, raw={},
            _context_token="ctx-token",
        )
        bot = _FakeBot.last_instance
        bot.emit_message(fake_msg)
        assert _wait_until(
            lambda: any(
                m["direction"] == "in" and m["text"] == "hello bot"
                for m in wc.recent_messages(limit=10)
            ),
            timeout=2.0,
        )
        assert "wx_user" in wc.known_users()
    finally:
        wc.close()


def test_handler_invoked_on_inbound():
    seen: list[Any] = []

    wc = Wechat(cred_path="/tmp/wdiot-test-creds.json")
    try:
        wc.register_message_handler(lambda msg: seen.append(msg.user_id))
        wc.start_login(force=True)
        assert _wait_until(lambda: wc.started, timeout=3.0)

        from wechatbot import IncomingMessage

        bot = _FakeBot.last_instance
        bot.emit_message(IncomingMessage(
            user_id="wx_user",
            text="hi",
            type="text",
            timestamp=datetime.now(timezone.utc),
            images=[], voices=[], files=[], videos=[],
            quoted_message=None, raw={}, _context_token="ctx",
        ))
        assert _wait_until(lambda: "wx_user" in seen, timeout=2.0)
    finally:
        wc.close()


def test_cancel_login_idempotent():
    wc = Wechat(cred_path="/tmp/wdiot-test-creds.json")
    try:
        wc.start_login(force=True)
        snap = wc.cancel_login()
        assert snap["state"] == Wechat.STATE_IDLE
        # cancel_login is safe to call again.
        snap2 = wc.cancel_login()
        assert snap2["state"] == Wechat.STATE_IDLE
    finally:
        wc.close()


def test_stop_then_close_is_idempotent():
    wc = Wechat(cred_path="/tmp/wdiot-test-creds.json")
    wc.start_login(force=True)
    assert _wait_until(lambda: wc.started, timeout=3.0)
    wc.stop()
    assert _wait_until(lambda: not wc.started, timeout=2.0)
    wc.close()
    # Second close must not raise.
    wc.close()


def test_logout_clears_credentials():
    wc = Wechat(cred_path="/tmp/wdiot-test-creds.json")
    try:
        wc.start_login(force=True)
        assert _wait_until(lambda: wc.started and wc.user_id is not None, timeout=3.0)
        wc.logout()
        assert wc.user_id is None
        assert wc.login_status()["state"] == Wechat.STATE_IDLE
    finally:
        wc.close()


def test_check_runtime_does_not_raise():
    info = Wechat.check_runtime()
    assert info["available"] is True
    assert info["module"] == "wechatbot"