#!/usr/bin/env bash
# Build the bundled FastDDS Python bindings + WdnddsStringTopicDataType helper.
#
# This produces the wheel-payload files under wdndds/dds/_native/:
#
#   fastdds/                       # SWIG-generated fastdds Python module
#   lib/libfastdds.so.3.6 + libfastcdr.so.2
#   _wdndds_string.so              # our custom C++ TopicDataType
#   wdndds_string.py               # its Python wrapper
#
# Usage:
#   ./build_native.sh [PLATFORM_TAG]
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

dpkg -l libasio-dev libtinyxml2-dev libfastrtps-dev >/dev/null 2>&1 \
    || die "missing apt deps; run: sudo apt install libasio-dev libtinyxml2-dev libfastrtps-dev"

# --------------------------------------------------------------------------- #
# Step 1: clone Fast-DDS-Python repos via vcs
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

# --------------------------------------------------------------------------- #
# Step 2: build the fastdds Python module via colcon
# --------------------------------------------------------------------------- #
log "running colcon build --packages-up-to fastdds_python (this takes ~6 minutes)..."
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

[ -f "${WS_DIR}/install/lib/python3.11/site-packages/fastdds/_fastdds_python.so" ] \
    || die "colcon build did not produce ${WS_DIR}/install/lib/python3.11/site-packages/fastdds/_fastdds_python.so"

log "fastdds python bindings OK"

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
mkdir -p "${NATIVE_DIR}/fastdds" "${NATIVE_DIR}/lib"

# Reset _native/fastdds with the colcon build's fastdds package.
rm -rf "${NATIVE_DIR}/fastdds"
cp -r "${WS_DIR}/install/lib/python3.11/site-packages/fastdds" "${NATIVE_DIR}/fastdds"

# Reset _native/lib with the colcon build's shared libs (preserve symlinks).
rm -rf "${NATIVE_DIR}/lib"
mkdir -p "${NATIVE_DIR}/lib"
cp -P "${WS_DIR}/install/lib/libfastdds.so"* "${NATIVE_DIR}/lib/"
cp -P "${WS_DIR}/install/lib/libfastcdr.so"* "${NATIVE_DIR}/lib/"

# Copy the WdnddsStringTopicDataType helper.
cp "${WDNDDS_CXX_DIR}/generated/_wdndds_string.so" "${NATIVE_DIR}/"
cp "${WDNDDS_CXX_DIR}/generated/wdndds_string.py" "${NATIVE_DIR}/"

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