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
from .wechat import Wechat

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
    "Wechat",
    "__version__",
    "connect",
    "register",
    "start_scan",
    "stop_scan",
]