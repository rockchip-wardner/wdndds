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
            "(importable as 'fastdds' / 'fastdds_python').\n"
            "It is NOT on PyPI; install via one of:\n"
            "  - apt (Debian/Ubuntu): apt install libfastdds-dev fastdds-python3\n"
            "  - source build: https://fast-dds.docs.eprosima.com/en/latest/"
            "fastdds/python_bindings/python_bindings.html\n"
            "After installation, verify with: python -c 'import fastdds; "
            "print(fastdds.__file__)'"
        ) from exc
    _fastdds = _mod
    return _mod


def check_runtime() -> dict[str, Any]:
    info: dict[str, Any] = {"available": False}
    try:
        mod = get()
    except ImportError as exc:
        info["error"] = str(exc)
        return info
    info["available"] = True
    info["module"] = mod.__name__
    info["path"] = getattr(mod, "__file__", None)
    info["version"] = getattr(mod, "__version__", "unknown")
    return info
