from __future__ import annotations

from typing import Any

_fastdds: Any | None = None


def get() -> Any:
    global _fastdds
    if _fastdds is not None:
        return _fastdds
    try:
        import fastdds as _mod
    except ImportError as exc:
        raise ImportError(
            "pyiot.dds requires the eProsima Fast DDS Python bindings "
            "(module name 'fastdds'). Install via your system package manager "
            "(e.g. apt install libfastdds-dev fastdds-python) or build from "
            "source: https://fast-dds.docs.eprosima.com/en/latest/"
        ) from exc
    _fastdds = _mod
    return _mod
