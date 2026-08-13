"""setuptools build hook — compile libwdndds_cshim.so before wheel packaging.

The package ships C++ source files (``src/wdndds/cshim_src/*.cpp``) that
wrap eProsima FastDDS via a thin opinionated C ABI (``wdndds_cshim.h``).
The shim has to be compiled for the wheel's target architecture before
setuptools can package it, so this module provides a custom
:class:`build_py` subclass that:

  1. Locates the FastDDS public headers (default ``/usr/include`` for
     system Debian package ``libfastrtps-dev``; override with
     ``WDNDDS_FASTDDS_INC`` env var);
  2. Locates the matching libfastrtps / libfastcdr (.so);
  3. Invokes ``g++ -shared -fPIC -std=c++17`` on the shim sources with
     the FastDDS include path, linking the output into
     ``src/wdndds/dds/_native/libwdndds_cshim.so``;
  4. Drops the resulting ``.so`` into the source tree where
     ``package_data`` knows to look;
  5. Stamps ``DT_RUNPATH=$ORIGIN/lib`` so the cshim can find the
     bundled libfastrtps / libfastcdr at runtime (falling back to the
     system loader if the bundled ones aren't there);
  6. Defers to the standard ``build_py`` so Python source packaging
     proceeds unchanged.

Compile failures are surfaced as a clear :class:`RuntimeError` listing
the underlying g++ error — silent failures here would mean a wheel that
imports fine but fails the first time anyone tries to use DDS.

Why not setuptools ``Extension`` / ``build_ext``?
-------------------------------------------------
``Extension`` would work for the compile step itself, but it ties the
output path to the ``build_lib``/``build_platlib`` tree under ``build/``
and requires an extra ``build_clib`` / ``build_ext`` orchestration to
copy the result back into the source tree's
``src/wdndds/dds/_native/`` where ``package_data`` knows to look. A
direct ``subprocess.run`` keeps the build path trivially auditable and
side-steps the ``build_py``/``build_ext`` ordering issue (``_cshim.py``
reads from ``src/wdndds/dds/_native/libwdndds_cshim.so`` at import
time, so the .so MUST exist in the source tree before ``build_py``
copies the package out).

Why is the hook a top-level module, not nested under ``wdndds/``?
-----------------------------------------------------------------
setuptools' ``cmdclass`` discovery looks up the class by import path
at the start of the build — *before* ``wdndds`` has been installed.
Putting the hook under ``wdndds._build`` would force a chicken-and-egg
import. A top-level module next to ``pyproject.toml`` is reachable as
soon as setuptools adds the project root to ``sys.path``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from setuptools.command.build_py import build_py as _build_py


# ---------------------------------------------------------------------------
# Compile configuration
# ---------------------------------------------------------------------------

# Canonical location of the FastDDS public headers on Debian. The package
# ``libfastrtps-dev`` installs them under /usr/include/fastdds + /usr/include.
# Override at build time via WDNDDS_FASTDDS_INC=/path/to/inc for cross-builds.
_DEFAULT_FASTDDS_INC = "/usr/include"

# Canonical location of libfastcdr / libfastrtps shared libs on Debian
# multiarch (aarch64). Override via WDNDDS_FASTDDS_LIB.
_DEFAULT_FASTDDS_LIB = "/usr/lib/aarch64-linux-gnu"

# C++ sources / output layout — relative to the project root. ``build_py``
# runs from the project root, but we resolve everything through
# ``os.path.dirname(__file__)`` so the hook still works when invoked
# from a different working directory (e.g. ``pip install .``).
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent.parent  # .venv/src/wdndds
_CSHIM_SRC_DIR = _PROJECT_ROOT / "cshim_src"
_INCLUDE_DIR   = _PROJECT_ROOT / "include"
_NATIVE_DIR    = _THIS_DIR / "dds" / "_native"
_OUTPUT_SO     = _NATIVE_DIR / "libwdndds_cshim.so"

_CPP_SOURCES = [
    "wdndds_cshim.cpp",
    "wdndds_participant.cpp",
    "wdndds_hub.cpp",
    "wdndds_string_type.cpp",
]


def _resolve_fastdds_inc() -> str:
    inc = os.environ.get("WDNDDS_FASTDDS_INC", _DEFAULT_FASTDDS_INC)
    return inc


def _resolve_fastdds_lib() -> str:
    lib = os.environ.get("WDNDDS_FASTDDS_LIB", _DEFAULT_FASTDDS_LIB)
    return lib


def _verify_fastdds_available() -> tuple[bool, str]:
    """Confirm libfastrtps headers + library are reachable.

    Returns ``(ok, message)``. ``message`` explains what was missing on
    failure so the surfaced :class:`RuntimeError` can point the user at
    the right apt command.
    """
    inc = _resolve_fastdds_inc()
    has_header = Path(inc, "fastdds", "dds", "domain", "DomainParticipant.hpp").is_file()
    has_header_rtps = Path(inc, "fastrtps", "attributes", "LibrarySettingsAttributes.h").is_file()
    lib = _resolve_fastdds_lib()
    has_lib = any(
        Path(lib, name).is_file()
        for name in ("libfastrtps.so", "libfastrtps.so.2.9", "libfastcdr.so", "libfastcdr.so.1")
    )

    if has_header and has_lib:
        return True, ""
    parts: List[str] = []
    if not has_header or not has_header_rtps:
        parts.append(f"  - FastDDS C++ headers not found under {inc} (need fastdds/ + fastrtps/ subdirs)")
    if not has_lib:
        parts.append(f"  - libfastrtps / libfastcdr not found under {lib}")
    return False, "\n".join(parts)


def _compile_cshim() -> Path:
    """Run g++ on the shim sources. Returns the produced .so path."""
    ok, msg = _verify_fastdds_available()
    if not ok:
        raise RuntimeError(
            "wdndds: FastDDS dev libs not available — cannot compile libwdndds_cshim.so.\n"
            + msg
            + "\n\nFix one of:\n"
            "  - apt install libfastrtps-dev libfastcdr-dev   (Debian / Ubuntu)\n"
            "  - WDNDDS_FASTDDS_INC=/path WDNDDS_FASTDDS_LIB=/path env (cross-build)"
        )

    inc = _resolve_fastdds_inc()
    lib = _resolve_fastdds_lib()

    src_paths = [str(_CSHIM_SRC_DIR / s) for s in _CPP_SOURCES]
    for p in src_paths:
        if not Path(p).is_file():
            raise RuntimeError(f"wdndds: missing cshim source: {p}")

    _NATIVE_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        "g++",
        "-shared",
        "-fPIC",
        "-O2",
        "-std=c++17",
        "-Wall",
        "-Wno-unused-parameter",
        "-Wno-deprecated-declarations",
        f"-I{inc}",
        f"-I{_CSHIM_SRC_DIR}",   # for include "wdndds_xxx.hpp"
        f"-I{_INCLUDE_DIR}",     # for include "wdndds_cshim.h" (public C header)
        "-o", str(_OUTPUT_SO),
        *src_paths,
        f"-L{lib}",
        "-lfastrtps",
        "-lfastcdr",
    ]

    print(f"[wdndds] compiling cshim: {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"wdndds: cshim compile timed out after {e.timeout}s\n"
            f"command: {' '.join(cmd)}"
        ) from e

    if proc.returncode != 0:
        raise RuntimeError(
            f"wdndds: cshim compile failed (rc={proc.returncode}).\n"
            f"command: {' '.join(cmd)}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )

    if not _OUTPUT_SO.is_file():
        raise RuntimeError(
            f"wdndds: cshim compile reported success but {_OUTPUT_SO} not found"
        )

    # Stamp RUNPATH so the .so can find libfastrtps / libfastcdr at
    # load time. Prefer the bundled _native/lib dir (the wheel ships
    # the .so files for runtime); fall back to the system multiarch
    # path so a dev box running the wheel directly from the source tree
    # also works.
    bundled_lib_dir = _NATIVE_DIR / "lib"
    if bundled_lib_dir.is_dir():
        runpath = "$ORIGIN/lib"
    else:
        runpath = lib
    try:
        patchelf = shutil.which("patchelf")
        if patchelf:
            subprocess.run(
                [patchelf, "--set-rpath", runpath, str(_OUTPUT_SO)],
                check=True,
                capture_output=True,
                text=True,
            )
            print(f"[wdndds] patched RUNPATH={runpath} on {_OUTPUT_SO.name}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        # patchelf missing or rpath edit failed — not fatal. ctypes
        # loading via RTLD_GLOBAL preload in _cshim.py will fall back
        # to whatever libfastrtps / libfastcdr the system loader finds.
        print(f"[wdndds] WARN: patchelf RUNPATH edit failed: {e}", file=sys.stderr)

    print(f"[wdndds] cshim built: {_OUTPUT_SO} ({_OUTPUT_SO.stat().st_size} bytes)", file=sys.stderr)
    return _OUTPUT_SO


# ---------------------------------------------------------------------------
# build_py subclass
# ---------------------------------------------------------------------------
class build_py(_build_py):
    """Compile the cshim before Python packaging copies the package out."""

    def run(self) -> None:  # type: ignore[override]
        _compile_cshim()
        super().run()