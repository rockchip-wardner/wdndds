"""setuptools build_py hook — source-compile wdndds native artifacts.

Pip-installs wdndds from source: ``libwdndds_idl.so`` + ``libwdndds_cshim.so``
are NOT committed to git. They get built fresh on every install from:

* ``idl/compute.idl`` (and its CMakeLists.txt) → ``libwdndds_idl.so``
* ``cshim_src/wdndds_cshim.cpp`` (and its CMakeLists.txt) →
  ``libwdndds_cshim.so``, linking against ``libwdndds_idl`` + FastDDS 3.6

Both ``.so`` files land in ``src/wdndds/dds/_native/`` where the
``package_data`` rule picks them up for the wheel.

Build-time requirement: FastDDS 3.6 dev install (headers +
``libfastdds.so.3.6`` + libfastcdr + fastddsgen binary).
Resolution order:
  1. ``$WDNDDS_FASTDDS_PREFIX`` env var, if set and contains
     ``include/fastdds/`` (cross-build / out-of-tree install)
  2. ``<project_root>/native/`` (dev workflow — produced by
     ``build_native.sh``; gitignored)

If neither is available the build aborts with a clear message
telling the user to run ``bash build_native.sh`` first. We don't
fall back to apt's ``libfastrtps-dev``: that's FastDDS 2.x and
incompatible with the v0.5 cshim ABI.

Why not setuptools ``Extension`` / ``build_ext``?
-------------------------------------------------
``Extension`` ties the output path to ``build_lib``/``build_platlib``
under ``build/`` and needs an extra ``build_clib`` dance to copy
back. We shell out to ``cmake`` + ``make`` directly so the path
is trivially auditable and we don't have to fight
``build_py``/``build_ext`` ordering.

Why is the hook a top-level module, not nested under ``wdndds/``?
-----------------------------------------------------------------
setuptools' ``cmdclass`` discovery looks up the class by import path
*before* ``wdndds`` is installed. Putting the hook under
``wdndds._build`` would chicken-and-egg. A top-level module next to
``pyproject.toml`` is reachable as soon as setuptools adds the
project root to ``sys.path``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from setuptools.command.build_py import build_py as _build_py


# ---------------------------------------------------------------------------
# Layout — all paths resolved relative to this file so the hook is cwd-
# independent (works whether invoked from ``pip install .`` or
# ``setuptools``'s interactive setup.py).
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent.parent          # .venv/src/wdndds
_IDL_DIR       = _PROJECT_ROOT / "idl"
_CSHIM_SRC_DIR = _PROJECT_ROOT / "cshim_src"
_INCLUDE_DIR   = _PROJECT_ROOT / "include"
_NATIVE_DIR    = _THIS_DIR / "dds" / "_native"  # wheel-install location
_BUILD_TMP     = _NATIVE_DIR / "_build_tmp"       # scratch dir for cmake

_OUTPUT_IDL_SO  = _NATIVE_DIR / "libwdndds_idl.so"
_OUTPUT_CSHIM_SO = _NATIVE_DIR / "libwdndds_cshim.so"


def _resolve_fastdds_prefix() -> Path:
    """Find a usable FastDDS 3.6 dev install.

    Looks at (in priority order):
      1. ``$WDNDDS_FASTDDS_PREFIX`` (must contain ``include/fastdds/``)
      2. ``<project_root>/native/`` (build_native.sh output)

    Returns the prefix Path; raises RuntimeError with actionable
    hint if neither works.
    """
    env = os.environ.get("WDNDDS_FASTDDS_PREFIX")
    if env:
        p = Path(env).resolve()
        if (p / "include" / "fastdds").is_dir():
            return p
        raise RuntimeError(
            f"WDNDDS_FASTDDS_PREFIX={env} does not look like a FastDDS "
            "install: missing include/fastdds/. Either unset the env var "
            "(default <project_root>/native/ will be tried) or point it "
            "at a FastDDS 3.6 install prefix."
        )
    dev = _PROJECT_ROOT / "native"
    if (dev / "include" / "fastdds").is_dir():
        return dev
    raise RuntimeError(
        "wdndds: FastDDS 3.6 dev install not found.\n"
        "Either:\n"
        "  - cd .venv/src/wdndds && bash build_native.sh\n"
        "    (builds FastDDS 3.6 from source into native/; ~10 min)\n"
        "  - WDNDDS_FASTDDS_PREFIX=/path/to/fastdds/install pip install .\n"
        "    (cross-build / out-of-tree install)\n"
        "\nWe do NOT fall back to apt's libfastrtps-dev: that's FastDDS "
        "2.x and incompatible with the v0.5 cshim ABI."
    )


def _fastddsgen_bin(prefix: Path) -> Path:
    """Locate the ``fastddsgen`` binary."""
    candidates = [
        prefix / "bin" / "fastddsgen",
        Path("/usr/local/bin/fastddsgen"),
        Path("/usr/bin/fastddsgen"),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    raise RuntimeError(
        f"wdndds: fastddsgen binary not found under {prefix}/bin/ or in PATH."
    )


def _run(cmd: list[str], cwd: Path, timeout: int = 600) -> None:
    """subprocess.run wrapper with clear error surface."""
    print(f"[wdndds] {' '.join(cmd)}  (cwd={cwd})", file=sys.stderr)
    proc = subprocess.run(cmd, cwd=str(cwd), check=False,
                         capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"wdndds: command failed (rc={proc.returncode}):\n"
            f"  cmd: {' '.join(cmd)}\n"
            f"  cwd: {cwd}\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )


def _build_idl_and_cshim(prefix: Path) -> tuple[Path, Path]:
    """Source-compile libwdndds_idl + libwdndds_cshim into ``_NATIVE_DIR``.

    Single cmake invocation: a temporary top-level project that uses
    ``add_subdirectory(idl)`` + ``add_subdirectory(cshim_src)`` so the
    cshim target can link against the idl target directly (no manual
    copy of ``libwdndds_idl.so`` between two separate cmake projects).

    The idl/ and cshim_src/ CMakeLists.txt we ship don't declare
    install(EXPORT) targets, so after ``cmake --build`` we copy the
    produced ``.so`` files into ``_NATIVE_DIR/`` (the wheel-install
    location ``_cshim.py`` reads at import time).
    """
    _NATIVE_DIR.mkdir(parents=True, exist_ok=True)
    _BUILD_TMP.mkdir(parents=True, exist_ok=True)

    # 1. fastddsgen — generates the .hpp / .cxx the idl CMakeLists.txt
    #    expects. Output lands in _BUILD_TMP/, which we add to
    #    CMAKE_PREFIX_PATH so the idl CMakeLists can find it.
    gen = _fastddsgen_bin(prefix)
    _run([str(gen), "-replace", "-d", str(_BUILD_TMP),
           str(_IDL_DIR / "compute.idl")], cwd=_BUILD_TMP)

    # 2. Write a tiny top-level CMakeLists.txt that glues both
    #    subprojects together. add_subdirectory uses the existing
    #    CMakeLists.txt in each dir, so we re-use the canonical
    #    build recipes.
    glue = _BUILD_TMP / "CMakeLists.txt"
    glue.write_text(
        "cmake_minimum_required(VERSION 3.22)\n"
        "project(wdndds_build LANGUAGES C CXX)\n"
        f"add_subdirectory({_IDL_DIR} idl)\n"
        f"add_subdirectory({_CSHIM_SRC_DIR} cshim)\n"
    )

    # 3. cmake configure + build both targets.
    cmake_build_dir = _BUILD_TMP / "build"
    cmake_build_dir.mkdir(exist_ok=True)
    _run(["cmake", "-S", str(_BUILD_TMP), "-B", str(cmake_build_dir),
           "-DCMAKE_PREFIX_PATH=" + str(prefix) + ";" + str(_BUILD_TMP),
           "-DCMAKE_BUILD_TYPE=Release"],
          cwd=cmake_build_dir)
    _run(["cmake", "--build", str(cmake_build_dir),
           "--target", "wdndds_idl", "wdndds_cshim",
           "-j", str(os.cpu_count() or 1)], cwd=cmake_build_dir)

    # 4. Locate the built .so files (cmake build dir layout varies:
    #    CMakeFiles/<target>.dir/ or src/cpp/).
    def _find_so(target: str) -> Path:
        cands = list(cmake_build_dir.rglob(f"lib{target}.so"))
        if not cands:
            raise RuntimeError(
                f"wdndds: cmake build did not produce lib{target}.so"
            )
        return cands[0]

    built_idl = _find_so("wdndds_idl")
    built_cshim = _find_so("wdndds_cshim")
    shutil.copy2(str(built_idl), str(_OUTPUT_IDL_SO))
    shutil.copy2(str(built_cshim), str(_OUTPUT_CSHIM_SO))

    # 5. Stamp RUNPATH on cshim so it finds libfastdds / libfastcdr /
    #    libwdndds_idl at load time. Prefer $ORIGIN/lib (the wheel
    #    installs the official FastDDS libs alongside); fall back to
    #    the prefix lib dir for in-place runs.
    bundled_lib = _NATIVE_DIR / "lib"
    if bundled_lib.is_dir():
        runpath = "$ORIGIN/lib"
    else:
        runpath = str(prefix / "lib")
    patchelf = shutil.which("patchelf")
    if patchelf:
        try:
            subprocess.run([patchelf, "--set-rpath", runpath,
                            str(_OUTPUT_CSHIM_SO)],
                           check=True, capture_output=True, text=True)
            print(f"[wdndds] patched RUNPATH={runpath} on "
                  f"{_OUTPUT_CSHIM_SO.name}", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"[wdndds] WARN: patchelf failed: {e.stderr}",
                  file=sys.stderr)
    else:
        print(f"[wdndds] WARN: patchelf not found; RUNPATH not set on "
              f"{_OUTPUT_CSHIM_SO.name} (cshim will rely on "
              "LD_LIBRARY_PATH at runtime)", file=sys.stderr)

    print(f"[wdndds] idl built:   {_OUTPUT_IDL_SO} "
          f"({_OUTPUT_IDL_SO.stat().st_size} bytes)", file=sys.stderr)
    print(f"[wdndds] cshim built: {_OUTPUT_CSHIM_SO} "
          f"({_OUTPUT_CSHIM_SO.stat().st_size} bytes)", file=sys.stderr)
    return _OUTPUT_IDL_SO, _OUTPUT_CSHIM_SO


def _cleanup_scratch() -> None:
    """Drop the cmake scratch dirs after a successful build."""
    if _BUILD_TMP.is_dir():
        shutil.rmtree(_BUILD_TMP, ignore_errors=True)


class build_py(_build_py):
    """Build wdndds native artifacts before the wheel is packaged."""

    def run(self) -> None:  # type: ignore[override]
        try:
            prefix = _resolve_fastdds_prefix()
            _build_idl_and_cshim(prefix)
        finally:
            _cleanup_scratch()
        super().run()