from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, ClassVar

if TYPE_CHECKING:
    from .registry import Device


Callback = Callable[[Any, Callable[[Any], None]], None]


class Capability:
    """Base class for every capability a wdiot device can advertise.

    Subclasses must set :attr:`capability_type` (the string used on the DDS
    wire). They may override :meth:`_default_handle` to provide a sensible
    response when the user registers the capability without a callback.

    Construction
    ------------
    Use ``name`` for the per-instance label (e.g. ``"大门正门"``) and
    ``callback`` for the request handler:

        cam = CameraInput("大门正门", callback=on_frame)

    The callback receives ``(payload, reply)``. It must call ``reply(value)``
    exactly once — that's how the caller's ``Future`` completes.

    When the same class is used to address a remote instance
    (``CameraInput("大门正门", target=remote_device)``) ``__call__`` becomes
    a remote invocation that returns a ``concurrent.futures.Future``.
    """

    capability_type: ClassVar[str] = ""

    def __init__(
        self,
        name: str,
        callback: Callback | None = None,
        *,
        target: Device | None = None,
    ) -> None:
        self.name = name
        self._user_callback = callback
        self._target = target

    def __call__(self, payload: Any, *, timeout: float | None = None):
        if self._target is not None:
            return self._target._hub.invoke(
                target_device=self._target.name,
                cap_type=self.capability_type,
                instance_name=self.name,
                payload=payload,
                timeout=timeout,
            )
        result: dict[str, Any] = {}

        def reply(value: Any) -> None:
            result["v"] = value

        self._handle(payload, reply)
        return result.get("v")

    def _handle(self, payload: Any, reply: Callable[[Any], None]) -> None:
        if self._user_callback is not None:
            self._user_callback(payload, reply)
        else:
            self._default_handle(payload, reply)

    def _default_handle(self, payload: Any, reply: Callable[[Any], None]) -> None:
        reply({"ok": False, "error": f"no handler for {self.capability_type}/{self.name}"})

    def __repr__(self) -> str:
        side = "remote" if self._target is not None else "local"
        return f"<{type(self).__name__} {self.name!r} ({side})>"


class CameraInput(Capability):
    """A camera producing a video stream (frames pushed by callers)."""

    capability_type = "video_input"

    def _default_handle(self, payload, reply):
        reply({"ok": False, "reason": "no camera handler", "dropped": True})


class CameraQuery(Capability):
    """Query a camera for live information (e.g. "what do you see?")."""

    capability_type = "video_query"

    def _default_handle(self, payload, reply):
        reply({"answer": "unconfigured"})


class CameraDescribe(Capability):
    """Ask a camera to describe its current scene."""

    capability_type = "video_describe"

    def _default_handle(self, payload, reply):
        reply({"description": "unconfigured"})


class MicInput(Capability):
    """A microphone producing an audio stream."""

    capability_type = "audio_input"

    def _default_handle(self, payload, reply):
        reply({"ok": False, "reason": "no mic handler", "dropped": True})


class SpeakerOutput(Capability):
    """A speaker that plays audio."""

    capability_type = "audio_output"

    def _default_handle(self, payload, reply):
        reply({"played": False, "reason": "no speaker handler"})


class AudioQuery(Capability):
    """Query an audio source for information."""

    capability_type = "audio_query"

    def _default_handle(self, payload, reply):
        reply({"answer": "unconfigured"})


class AudioAnalyse(Capability):
    """Run audio analysis on a stream (e.g. level, VAD, classification)."""

    capability_type = "audio_analyse"

    def _default_handle(self, payload, reply):
        reply({"result": None})


class HDMIDisplay(Capability):
    """An HDMI display sink."""

    capability_type = "hdmi_display"

    def _default_handle(self, payload, reply):
        reply({"displayed": False, "reason": "no display handler"})