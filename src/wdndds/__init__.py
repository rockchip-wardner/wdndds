from __future__ import annotations

from ._version import __version__
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
from .event import Event
from .registry import Device, connect, register, start_scan, stop_scan

__all__ = [
    "Device",
    "Event",
    "HWAudioInput",
    "HWAudioOutput",
    "HWHDMIDisplay",
    "HWVideoInput",
    "SWAudioAnalyse",
    "SWAudioQuery",
    "SWAudioTTS",
    "SWTextChat",
    "SWVideoAnalyse",
    "SWVideoDescribe",
    "SWVideoQuery",
    "__version__",
    "connect",
    "register",
    "start_scan",
    "stop_scan",
]

# ``ai_session`` needs wdnai; the rest of wdndds does not. Resolved lazily
# so ``import wdndds`` stays usable on hosts that only do pub/sub, while
# ``from wdndds import LLMRPCSession`` still works wherever the AI stack is
# installed.
_LAZY = {"LLMRPCSession", "serve_local_session", "ServedSession"}


def __getattr__(name: str):
    if name in _LAZY:
        from . import ai_session
        return getattr(ai_session, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(__all__) | _LAZY)