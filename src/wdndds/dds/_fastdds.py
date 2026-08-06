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
            "wdndds.dds requires the eProsima Fast DDS Python bindings "
            "(importable as 'fastdds' / 'fastdds_python').\n"
            "It is NOT on PyPI and NOT shipped in Debian/Ubuntu apt; the "
            "only supported install path is to build from source.\n"
            "Step 1 — apt build deps (Debian 12 / Ubuntu 22.04+):\n"
            "  sudo apt install -y libasio-dev libtinyxml2-dev libfastrtps-dev\n"
            "  # NOTE: there is NO 'libfastdds-dev' / 'fastdds-python3' "
            "apt package — the C++ dev package is libfastrtps-dev, and the\n"
            "  # Python bindings must always be built from source.\n"
            "Step 2 — colcon build of Fast-DDS-Python:\n"
            "  pip install -U colcon-common-extensions vcstool\n"
            "  mkdir -p ~/fastdds_ws/src && cd ~/fastdds_ws\n"
            "  wget https://raw.githubusercontent.com/eProsima/Fast-DDS-Python/main/fastdds_python.repos\n"
            "  vcs import src < fastdds_python.repos\n"
            "  colcon build\n"
            "Step 3 — make the bindings importable:\n"
            "  source ~/fastdds_ws/install/setup.bash\n"
            "  # (or extend PYTHONPATH to include\n"
            "  #  ~/fastdds_ws/install/fastdds_python/lib/python3.X/site-packages)\n"
            "Verify with:\n"
            "  python -c 'import fastdds; print(fastdds.__file__)'\n"
            "Full guide: https://fast-dds.docs.eprosima.com/en/latest/"
            "fastdds/python_bindings/python_bindings.html"
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
