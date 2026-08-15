#!/usr/bin/env bash
# Build the wdndds 0.5 native stack from source:
#   - FastCDR 2.3.6
#   - foonathan_memory_vendor 1.4.1
#   - FastDDS 3.6.2
#   - FastDDS-Gen 4.3.0 (Java)
#   - wdndds IDL library (libwdndds_idl.so)
#   - wdndds cshim (libwdndds_cshim.so)
#
# Output: everything installs to wdndds/native/.
#          libfast{cdr,dds}.so.3.6.2.0, libwdndds_idl.so, libwdndds_cshim.so
#          bin/fastdds, bin/fastddsgen
#          share/fastddsgen/java/fastddsgen.jar
#          include/wdndds/idl/{compute,computePubSubTypes,...}.hpp
#
# Usage:
#   ./build_native.sh
#
# Requirements on the build host:
#   - apt: openjdk-17-jdk-headless cmake g++ patchelf
#   - 5-10 GB free disk
#   - 20-30 minutes wall time (FastDDS is big)
#
# The build artifacts go to:
#   WDNDDS_NATIVE (default wdndds/native) — install prefix
#   /userdata/src/deps + /userdata/src/Fast-DDS — sources (NOT /tmp!)

set -euo pipefail

WDNDDS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NATIVE_DIR="${WDNDDS_NATIVE:-${WDNDDS_DIR}/native}"
SRC_DIR="${WDNDDS_SRC:-/userdata/src}"
DEPS_DIR="${SRC_DIR}/deps"

log() { printf "\033[1;34m[build_native]\033[0m %s\n" "$*" >&2; }
die() { printf "\033[1;31m[build_native]\033[0m %s\n" "$*" >&2; exit 1; }

log "wdndds dir:    ${WDNDDS_DIR}"
log "install prefix: ${NATIVE_DIR}"
log "source dir:    ${SRC_DIR}"

# --------------------------------------------------------------------------- #
# Sanity: tools available
# --------------------------------------------------------------------------- #
for cmd in g++ cmake make git wget tar unzip java; do
    command -v "$cmd" >/dev/null || die "missing required command: $cmd"
done

# --------------------------------------------------------------------------- #
# Step 1: Download sources (skip if already present)
# --------------------------------------------------------------------------- #
mkdir -p "${SRC_DIR}" "${DEPS_DIR}" "${NATIVE_DIR}"

# FastDDS 3.6.2
if [ ! -d "${SRC_DIR}/Fast-DDS" ]; then
    log "downloading Fast-DDS 3.6.2 ..."
    wget -q --timeout=120 \
        "https://codeload.github.com/eProsima/Fast-DDS/tar.gz/refs/tags/v3.6.2" \
        -O "${SRC_DIR}/Fast-DDS.tar.gz"
    mkdir -p "${SRC_DIR}/Fast-DDS"
    tar -xzf "${SRC_DIR}/Fast-DDS.tar.gz" -C "${SRC_DIR}/Fast-DDS" \
        --strip-components=1
fi

# FastCDR 2.3.6
if [ ! -d "${DEPS_DIR}/fastcdr" ]; then
    log "downloading FastCDR 2.3.6 ..."
    wget -q --timeout=120 \
        "https://codeload.github.com/eProsima/Fast-CDR/tar.gz/refs/tags/v2.3.6" \
        -O "${DEPS_DIR}/fastcdr.tar.gz"
    mkdir -p "${DEPS_DIR}/fastcdr"
    tar -xzf "${DEPS_DIR}/fastcdr.tar.gz" -C "${DEPS_DIR}/fastcdr" \
        --strip-components=1
fi

# foonathan_memory_vendor 1.4.1
if [ ! -d "${DEPS_DIR}/foonathan_memory_vendor" ]; then
    log "downloading foonathan_memory_vendor 1.4.1 ..."
    wget -q --timeout=120 \
        "https://codeload.github.com/eProsima/foonathan_memory_vendor/tar.gz/refs/tags/v1.4.1" \
        -O "${DEPS_DIR}/foonathan_memory_vendor.tar.gz"
    mkdir -p "${DEPS_DIR}/foonathan_memory_vendor"
    tar -xzf "${DEPS_DIR}/foonathan_memory_vendor.tar.gz" \
        -C "${DEPS_DIR}/foonathan_memory_vendor" --strip-components=1
fi

# FastDDS-Gen 4.3.0 (Java)
if [ ! -d "${DEPS_DIR}/fastddsgen" ]; then
    log "downloading Fast-DDS-Gen 4.3.0 ..."
    wget -q --timeout=120 \
        "https://codeload.github.com/eProsima/Fast-DDS-Gen/tar.gz/refs/tags/v4.3.0" \
        -O "${DEPS_DIR}/fastddsgen.tar.gz"
    mkdir -p "${DEPS_DIR}/fastddsgen"
    tar -xzf "${DEPS_DIR}/fastddsgen.tar.gz" -C "${DEPS_DIR}/fastddsgen" \
        --strip-components=1
fi

# IDL parser submodule (for fastddsgen)
if [ ! -d "${DEPS_DIR}/fastddsgen/thirdparty/idl-parser/src" ]; then
    log "downloading IDL-Parser master ..."
    wget -q --timeout=120 \
        "https://codeload.github.com/eProsima/idl-parser/tar.gz/refs/heads/master" \
        -O "${DEPS_DIR}/idl-parser.tar.gz"
    mkdir -p "${DEPS_DIR}/fastddsgen/thirdparty/idl-parser"
    tar -xzf "${DEPS_DIR}/idl-parser.tar.gz" \
        -C "${DEPS_DIR}/fastddsgen/thirdparty/idl-parser" --strip-components=1
    # Patch build.gradle so submodule update step is a no-op (we already have
    # the source tree, no need to call git submodule).
    sed -i "s|commandLine = \['git', 'submodule', 'update', '--init'\]|commandLine = ['/bin/echo', 'skip']|" \
        "${DEPS_DIR}/fastddsgen/build.gradle"
fi

# Gradle 9.2.1
if [ ! -d "${SRC_DIR}/gradle-9.2.1" ]; then
    log "downloading Gradle 9.2.1 ..."
    wget -q --timeout=120 \
        "https://services.gradle.org/distributions/gradle-9.2.1-bin.zip" \
        -O "${SRC_DIR}/gradle-9.2.1-bin.zip"
    unzip -q "${SRC_DIR}/gradle-9.2.1-bin.zip" -d "${SRC_DIR}"
fi
export PATH="${SRC_DIR}/gradle-9.2.1/bin:${PATH}"

# --------------------------------------------------------------------------- #
# Step 2: Build FastCDR (small, 30s)
# --------------------------------------------------------------------------- #
log "building FastCDR ..."
mkdir -p "${DEPS_DIR}/fastcdr/build"
cd "${DEPS_DIR}/fastcdr/build"
cmake -DCMAKE_INSTALL_PREFIX="${NATIVE_DIR}" \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=ON \
      -DBUILD_TESTING=OFF \
      .. > /dev/null
make -j"$(nproc)" > /dev/null
make install > /dev/null

# --------------------------------------------------------------------------- #
# Step 3: Build foonathan_memory_vendor (header-only, 5s)
# --------------------------------------------------------------------------- #
log "building foonathan_memory_vendor ..."
mkdir -p "${DEPS_DIR}/foonathan_memory_vendor/build"
cd "${DEPS_DIR}/foonathan_memory_vendor/build"
cmake -DCMAKE_INSTALL_PREFIX="${NATIVE_DIR}" \
      -DCMAKE_BUILD_TYPE=Release \
      -DFOONATHAN_MEMORY_BUILD_EXAMPLES=OFF \
      -DFOONATHAN_MEMORY_BUILD_TESTS=OFF \
      -DBUILD_SHARED_LIBS=OFF \
      .. > /dev/null
make -j"$(nproc)" > /dev/null
make install > /dev/null

# --------------------------------------------------------------------------- #
# Step 4: Build FastDDS (~10 min)
# --------------------------------------------------------------------------- #
log "building FastDDS 3.6.2 (this takes ~10 min) ..."
mkdir -p "${SRC_DIR}/Fast-DDS/build"
cd "${SRC_DIR}/Fast-DDS/build"
cmake -DCMAKE_INSTALL_PREFIX="${NATIVE_DIR}" \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_PREFIX_PATH="${NATIVE_DIR}" \
      -DBUILD_SHARED_LIBS=ON \
      -DTHIRDPARTY=ON \
      -DBUILD_TESTING=OFF \
      .. > /dev/null
make -j"$(nproc)" > /dev/null
make install > /dev/null

# --------------------------------------------------------------------------- #
# Step 5: Build FastDDS-Gen (Java, 30s)
# --------------------------------------------------------------------------- #
log "building FastDDS-Gen 4.3.0 ..."
cd "${DEPS_DIR}/fastddsgen"
# First build the idl-parser submodule jar
log "  building idl-parser submodule ..."
cd "${DEPS_DIR}/fastddsgen/thirdparty/idl-parser"
gradle clean jar -x test > /dev/null
# Copy to expected location
cp build/libs/idlparser-*.jar build/libs/idlparser-4.3.0.jar 2>/dev/null || true

cd "${DEPS_DIR}/fastddsgen"
# Make sure buildIDLParser doesn't clobber the jar
sed -i 's|tasks = \['"'"'clean'"'"', '"'"'build'"'"'\]|tasks = ["jar"]|' build.gradle
gradle install --install_path "${NATIVE_DIR}" -x test > /dev/null

# --------------------------------------------------------------------------- #
# Step 6: Build wdndds IDL library
# --------------------------------------------------------------------------- #
log "building wdndds_idl ..."
NATIVE_BIN="${NATIVE_DIR}/bin/fastddsgen"
mkdir -p "${WDNDDS_DIR}/idl/build"
cd "${WDNDDS_DIR}/idl/build"
cmake -DCMAKE_PREFIX_PATH="${NATIVE_DIR}" \
      -DCMAKE_BUILD_TYPE=Release \
      -DCOMPUTE_SOURCE=ON \
      .. > /dev/null
make -j"$(nproc)" > /dev/null
# Copy artifacts to native (no install target in our CMakeLists)
mkdir -p "${NATIVE_DIR}/include/wdndds/idl"
cp "${WDNDDS_DIR}/idl"/compute*.hpp "${WDNDDS_DIR}/idl"/compute*.ipp \
    "${WDNDDS_DIR}/idl"/compute*.cxx "${WDNDDS_DIR}/idl/build"/libwdndds_idl.so \
    "${NATIVE_DIR}/include/wdndds/idl/"
cp "${WDNDDS_DIR}/idl/build/libwdndds_idl.so" "${NATIVE_DIR}/lib/"

# CMake config for finding wdndds_idl
mkdir -p "${NATIVE_DIR}/share/wdndds/cmake"
cat > "${NATIVE_DIR}/share/wdndds/cmake/wdndds_idl-config.cmake" <<'CMK'
include(CMakeFindDependencyMacro)
find_dependency(fastdds REQUIRED)
add_library(wdndds_idl SHARED IMPORTED)
set_target_properties(wdndds_idl PROPERTIES
    IMPORTED_LOCATION "${CMAKE_CURRENT_LIST_DIR}/../../lib/libwdndds_idl.so"
    INTERFACE_INCLUDE_DIRECTORIES "${CMAKE_CURRENT_LIST_DIR}/../../include/wdndds/idl"
)
CMK

# --------------------------------------------------------------------------- #
# Step 7: Build wdndds cshim
# --------------------------------------------------------------------------- #
log "building wdndds_cshim ..."
mkdir -p "${WDNDDS_DIR}/cshim_src/build"
cd "${WDNDDS_DIR}/cshim_src/build"
cmake -DCMAKE_PREFIX_PATH="${NATIVE_DIR}" \
      -DCMAKE_INSTALL_PREFIX="${NATIVE_DIR}" \
      -DCMAKE_BUILD_TYPE=Release \
      .. > /dev/null
make -j"$(nproc)" > /dev/null
cp libwdndds_cshim.so "${NATIVE_DIR}/lib/"

# --------------------------------------------------------------------------- #
# Done.
# --------------------------------------------------------------------------- #
log "all native libraries installed to: ${NATIVE_DIR}"
log ""
log "lib/ contains:"
ls -la "${NATIVE_DIR}/lib" | sed 's/^/  /' >&2
log ""
log "bin/ contains:"
ls -la "${NATIVE_DIR}/bin" | sed 's/^/  /' >&2
log ""
log "To use from wdnwebui:"
log "  export LD_LIBRARY_PATH=${NATIVE_DIR}/lib"
log "  python -c 'from wdndds.dds._cshim import check_runtime; print(check_runtime())'"