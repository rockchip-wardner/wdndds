#!/usr/bin/env bash
# Build the bundled FastDDS Python bindings + WdnddsStringTopicDataType helper.
#
# This produces the wheel-payload files under wdndds/dds/_native/:
#
#   _fastdds_python.so + fastdds.py   # SWIG-generated, 1.x flat layout
#       (or fastdds/__init__.py + fastdds/_fastdds_python.so  # 2.x+ package)
#   lib/libfast{rtps,dds}.so* + libfastcdr.so*
#   _wdndds_string.so                 # our custom C++ TopicDataType
#   wdndds_string.py                  # its Python wrapper
#
# Usage:
#   ./build_native.sh [PLATFORM_TAG]
#   FASTDDDS_BRANCH=origin/2.14.x ./build_native.sh   # build a specific tag
#
# PLATFORM_TAG defaults to ``aarch64-bookworm-py3.11``. The current
# shipped artifacts match this tag; rebuilding for a different
# platform (e.g. ``x86_64-jammy-py3.10``) requires apt-installing the
# matching ``libfastrtps-dev`` and running the colcon build under
# that OS.
#
# Requirements on the build host:
#   - apt: libasio-dev libtinyxml2-dev libfastrtps-dev swig cmake g++ python3-dev
#   - pip: colcon-common-extensions vcstool
#   - git (for the Fast-DDS-Python repos)
#   - 5-10 GB free disk (colcon build artifacts)
#   - 20-30 minutes wall time

set -euo pipefail

PLATFORM_TAG="${1:-aarch64-bookworm-py3.11}"
WDNDDS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_DIR="${FASTDDDS_WS:-/tmp/fastdds_ws}"
WDNDDS_CXX_DIR="${WDNDDS_DIR}/../wdndds_cxx"
NATIVE_DIR="${WDNDDS_DIR}/src/wdndds/dds/_native"

log() { printf "\033[1;34m[build_native]\033[0m %s\n" "$*" >&2; }
die() { printf "\033[1;31m[build_native]\033[0m %s\n" "$*" >&2; exit 1; }

log "platform tag: ${PLATFORM_TAG}"
log "build workspace: ${WS_DIR}"
log "wdndds_cxx dir: ${WDNDDS_CXX_DIR}"
log "wdndds native dir: ${NATIVE_DIR}"

# --------------------------------------------------------------------------- #
# Sanity: tools available
# --------------------------------------------------------------------------- #
for cmd in g++ cmake swig git python3; do
    command -v "$cmd" >/dev/null || die "missing required command: $cmd"
done
python3 -c "import colcon_core" 2>/dev/null \
    || die "missing python module: colcon-core (pip install colcon-common-extensions)"
python3 -c "import vcstool" 2>/dev/null \
    || die "missing python module: vcstool (pip install vcstool)"

dpkg -l libasio-dev libtinyxml2-dev libfastrtps-dev patchelf >/dev/null 2>&1 \
    || die "missing apt deps; run: sudo apt install libasio-dev libtinyxml2-dev libfastrtps-dev patchelf"

# --------------------------------------------------------------------------- #
# Step 1: clone Fast-DDS-Python repos via vcs (or use the FASTDDDS_WS that
# the user pre-populated with a specific branch).
# --------------------------------------------------------------------------- #
if [ ! -d "${WS_DIR}/src/fastdds_python" ]; then
    log "cloning Fast-DDS-Python repos into ${WS_DIR} ..."
    mkdir -p "${WS_DIR}/src"
    wget -q -O "${WS_DIR}/fastdds_python.repos" \
        https://raw.githubusercontent.com/eProsima/Fast-DDS-Python/main/fastdds_python.repos
    ( cd "${WS_DIR}" && python3 -m vcstool import --recursive src < fastdds_python.repos )
else
    log "fastdds_python repos already present at ${WS_DIR}/src"
fi

# Optional: switch the source trees to a specific FastDDS / Fast-DDS-Python
# branch. The wdndds SDK bootstraps both 1.x (flat fastdds.py) and 2.x+
# (package fastdds/__init__.py) layouts from this build directory, so the
# pinned branch decides which API the bundled .so + .py target.
if [ -n "${FASTDDDS_BRANCH:-}" ]; then
    log "switching fastdds source to ${FASTDDDS_BRANCH}"
    ( cd "${WS_DIR}/src/fastdds" && git fetch --all >/dev/null 2>&1 || true )
    ( cd "${WS_DIR}/src/fastdds" && git checkout "${FASTDDDS_BRANCH}" )
    ( cd "${WS_DIR}/src/fastdds" && git submodule update --init --recursive )
fi

# --------------------------------------------------------------------------- #
# Step 2: build the fastdds Python module via colcon
# --------------------------------------------------------------------------- #
log "running colcon build --packages-up-to fastdds_python (this takes ~6 minutes)..."
# Always start from a clean build dir if FASTDDDS_BRANCH was set — the
# CMake cache from a previous branch pollutes include paths and breaks the
# 3.x → 2.x ABI transition.
if [ -n "${FASTDDDS_BRANCH:-}" ]; then
    rm -rf "${WS_DIR}/build" "${WS_DIR}/install" "${WS_DIR}/log"
fi
if [ ! -d "${WS_DIR}/build" ] || [ ! -f "${WS_DIR}/install/setup.bash" ]; then
    ( cd "${WS_DIR}" && colcon build --merge-install --symlink-install \
        --packages-up-to fastdds_python \
        --cmake-args -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release )
else
    log "colcon install/ already present; rebuilding fastdds_python only"
    ( cd "${WS_DIR}" && colcon build --merge-install --symlink-install \
        --packages-select fastdds_python \
        --cmake-args -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release )
fi

# Detect the built layout. Fast-DDS-Python 1.x ships flat
# ``fastdds.py`` + ``_fastdds_python.so`` in site-packages; 2.x+ ships a
# ``fastdds/`` package directory with __init__.py and the .so inside.
PY_SITE="${WS_DIR}/install/lib/python3.11/site-packages"
if [ -f "${PY_SITE}/fastdds.py" ]; then
    LAYOUT="1.x-flat"
elif [ -f "${PY_SITE}/fastdds/__init__.py" ]; then
    LAYOUT="2.x+package"
else
    die "colcon build produced neither ${PY_SITE}/fastdds.py nor ${PY_SITE}/fastdds/__init__.py"
fi
log "detected bindings layout: ${LAYOUT}"

# --------------------------------------------------------------------------- #
# Step 3: build the bundled C++ helper (WdnddsStringTopicDataType + SWIG)
# --------------------------------------------------------------------------- #
if [ ! -d "${WDNDDS_CXX_DIR}" ]; then
    die "wdndds_cxx dir not found: ${WDNDDS_CXX_DIR}"
fi
log "building WdnddsStringTopicDataType SWIG helper..."
rm -rf "${WDNDDS_CXX_DIR}/build/generated" "${WDNDDS_CXX_DIR}/generated"
mkdir -p "${WDNDDS_CXX_DIR}/build"
( cd "${WDNDDS_CXX_DIR}/build" && cmake .. )
( cd "${WDNDDS_CXX_DIR}/build" && cmake --build . -j"$(nproc)" )

[ -f "${WDNDDS_CXX_DIR}/generated/_wdndds_string.so" ] \
    || die "wdndds_cxx build did not produce _wdndds_string.so"
[ -f "${WDNDDS_CXX_DIR}/generated/wdndds_string.py" ] \
    || die "wdndds_cxx build did not produce wdndds_string.py"

log "WdnddsStringTopicDataType OK"

# --------------------------------------------------------------------------- #
# Step 4: drop everything into wdndds/dds/_native/
# --------------------------------------------------------------------------- #
log "populating ${NATIVE_DIR} ..."
mkdir -p "${NATIVE_DIR}/lib"

# Reset the previous fastdds payload (either flat fastdds.py or
# fastdds/ package dir).
rm -rf "${NATIVE_DIR}/fastdds.py" "${NATIVE_DIR}/fastdds"
case "${LAYOUT}" in
    1.x-flat)
        cp "${PY_SITE}/fastdds.py" "${NATIVE_DIR}/fastdds.py"
        cp "${PY_SITE}/_fastdds_python.so" "${NATIVE_DIR}/_fastdds_python.so"
        ;;
    2.x+package)
        cp -r "${PY_SITE}/fastdds" "${NATIVE_DIR}/fastdds"
        cp "${PY_SITE}/fastdds/_fastdds_python.so" "${NATIVE_DIR}/fastdds/_fastdds_python.so"
        ;;
esac

# Reset _native/lib with the colcon build's shared libs (preserve symlinks).
rm -rf "${NATIVE_DIR}/lib"
mkdir -p "${NATIVE_DIR}/lib"
cp -P "${WS_DIR}/install/lib/libfastdds.so"* "${NATIVE_DIR}/lib/" 2>/dev/null || true
cp -P "${WS_DIR}/install/lib/libfastrtps.so"* "${NATIVE_DIR}/lib/" 2>/dev/null || true
cp -P "${WS_DIR}/install/lib/libfastcdr.so"* "${NATIVE_DIR}/lib/" 2>/dev/null || true

# Copy the WdnddsStringTopicDataType helper.
cp "${WDNDDS_CXX_DIR}/generated/_wdndds_string.so" "${NATIVE_DIR}/"
cp "${WDNDDS_CXX_DIR}/generated/wdndds_string.py" "${NATIVE_DIR}/"

# --------------------------------------------------------------------------- #
# Stamp bundled .so RPATHs so the dynamic linker can find libfastcdr/libfastrtps
# WITHOUT relying on ctypes.CDLL(RTLD_GLOBAL) preload.
#
# Why: _fastdds_python.so has DT_NEEDED libfastrtps.so.2.6 — the loader needs
# to resolve it at dlopen() time. RTLD_GLOBAL preload from Python *should*
# make it visible, but doesn't always propagate on every libc version /
# dlopen call order — customers then hit "libfastrtps.so.2.6: cannot open
# shared object file" with no useful diagnostic.
#
# The fix: embed a DT_RUNPATH of ``$ORIGIN/lib`` (or ``$ORIGIN/../lib`` for
# the 2.x package layout where the .so lives at _native/fastdds/) so the
# loader searches the bundled lib dir FIRST, before any system path. This
# makes preload redundant and survives across libc versions.
#
# Requires the ``patchelf`` apt package (added to the dep check at the top
# of the script).
# --------------------------------------------------------------------------- #
if command -v patchelf >/dev/null 2>&1; then
    case "${LAYOUT}" in
        1.x-flat)
            # _fastdds_python.so and _wdndds_string.so live at _native/
            # libfast*.so live at _native/lib/ → $ORIGIN/lib
            patchelf --set-rpath '$ORIGIN/lib' "${NATIVE_DIR}/_fastdds_python.so"
            patchelf --set-rpath '$ORIGIN/lib' "${NATIVE_DIR}/_wdndds_string.so"
            log "stamped RUNPATH=\$ORIGIN/lib on _fastdds_python.so + _wdndds_string.so"
            ;;
        2.x+package)
            # .so live at _native/fastdds/, lib at _native/lib/ → $ORIGIN/../lib
            patchelf --set-rpath '$ORIGIN/../lib' "${NATIVE_DIR}/fastdds/_fastdds_python.so"
            log "stamped RUNPATH=\$ORIGIN/../lib on fastdds/_fastdds_python.so"
            ;;
    esac
else
    log "WARNING: patchelf not installed — bundled .so will rely on ctypes"
    log "         RTLD_GLOBAL preload only. Install with: sudo apt install patchelf"
fi

# Strip __pycache__ if it slipped in.
find "${NATIVE_DIR}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# --------------------------------------------------------------------------- #
# Step 5: print summary + size
# --------------------------------------------------------------------------- #
log "build done. _native contents:"
find "${NATIVE_DIR}" -maxdepth 2 -type f -o -type l | sort | while read -r f; do
    sz=$(stat -c '%s' "$f")
    printf "  %10d  %s\n" "$sz" "${f#${NATIVE_DIR}/}"
done
printf "  total: "
du -sh "${NATIVE_DIR}" | cut -f1
log "now: pip install -e ${WDNDDS_DIR}"
