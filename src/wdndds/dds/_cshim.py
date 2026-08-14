"""wdndds.dds._cshim — ctypes loader for libwdndds_cshim.

Bridges the wdndds C ABI to Python via ctypes. The C ABI uses FastDDS
3.6's rrpc Service / Replier / Requester API.

v0.5 ABI surface (renamed from v0.4):
  wdndds_participant_create / destroy
  wdndds_service_create / destroy        (replaces Hub + services list)
  wdndds_replier_create / destroy        (replaces RpcServer)
  wdndds_requester_create / destroy      (replaces RpcClient)
  wdndds_requester_call                  (replaces rpc_client_call)
"""
import ctypes
import os
import logging
import sys
from pathlib import Path
from typing import Optional


_LOG = logging.getLogger("wdndds.cshim")

# The native libraries shipped inside the wdndds wheel (or, in a dev
# tree where the wheel hasn't been built, the dev build output).
#
# Resolution order (first hit wins):
#   1. ``<wdndds>/dds/_native/libwdndds_cshim.so`` — the canonical
#      wheel-install location. ``_build.py`` populates this on
#      ``pip install wdndds`` by source-compiling
#      ``cshim_src/wdndds_cshim.cpp`` → ``libwdndds_cshim.so``.
#   2. ``<wdndds>/../../../native/lib/libwdndds_cshim.so`` — dev tree
#      where ``build_native.sh`` has produced native/. Useful for
#      running ``wdndds.dds`` from a fresh git checkout without
#      running pip install (the wheel's _build.py hook has to
#      succeed before import works in the venv).
_HERE = Path(__file__).resolve()
_NATIVE_WHEEL = _HERE.parent / "_native" / "libwdndds_cshim.so"
_NATIVE_DEV   = _HERE.parent.parent.parent.parent / "native" / "lib" / "libwdndds_cshim.so"

_LIB_PATH = _NATIVE_WHEEL if _NATIVE_WHEEL.is_file() else _NATIVE_DEV


def _preload_dependencies():
    """RTLD_GLOBAL load the FastDDS libs + our own idl/cshim so that
    libwdndds_cshim.so can resolve its symbols at ctypes.CDLL() time.

    The order matters — libwdndds_cshim.so depends on libwdndds_idl.so
    which depends on libfastdds.so which depends on libfastcdr.so +
    libfoonathan_memory.

    Libs come from two places:
      * FastDDS 3.6 official (libfastdds / libfastcdr / foonathan) —
        vendored at ``native/lib/`` (gitignored source build output);
        in installed wheels the wheel installer puts them at
        ``$ORIGIN/lib/`` next to libwdndds_cshim.so via
        RUNPATH=$ORIGIN/lib.
      * Our own wdndds .so (libwdndds_idl.so) — also in the same
        wheel-install directory as the cshim.

    We preload by absolute path (not SONAME) because the wheel's
    lib dir is not on the system loader's search path; relying on
    SONAME lookup would fail for installed wheels.
    """
    # 1. FastDDS 3.6 — system loader picks up libfoonathan /
    #    libssl / libcrypto automatically. libfastdds / libfastcdr are
    #    either on the system path (apt install of the 3.x package,
    #    rare) or vendored under native/lib/ (our default).
    for soname in ("libfoonathan_memory.so.0.7.2",
                   "libfastcdr.so.2",
                   "libfastdds.so.3.6"):
        try:
            ctypes.CDLL(soname, mode=ctypes.RTLD_GLOBAL)
        except OSError:
            pass

    # 2. libwdndds_idl.so — absolute path from the wheel-install dir.
    idl_so = _HERE.parent / "_native" / "libwdndds_idl.so"
    if idl_so.is_file():
        try:
            ctypes.CDLL(str(idl_so), mode=ctypes.RTLD_GLOBAL)
        except OSError as e:
            _LOG.warning("preload libwdndds_idl.so failed: %s", e)


def _bootstrap_at_import():
    """Preload dependencies on module import so subsequent .so loads work."""
    _preload_dependencies()


_bootstrap_at_import()


_lib: Optional[ctypes.CDLL] = None
_LIB_LOAD_ERROR: Optional[Exception] = None


def get() -> ctypes.CDLL:
    """Return the loaded libwdndds_cshim.so (lazy)."""
    global _lib, _LIB_LOAD_ERROR
    if _lib is not None:
        return _lib
    if _LIB_LOAD_ERROR is not None:
        raise _LIB_LOAD_ERROR
    if not _LIB_PATH.exists():
        _LIB_LOAD_ERROR = FileNotFoundError(
            f"wdndds: {str(_LIB_PATH)} not found. The wdndds native "
            f"libraries must be installed under {_LIB_PATH.parent}.")
        raise _LIB_LOAD_ERROR
    try:
        _lib = ctypes.CDLL(str(_LIB_PATH))
    except OSError as e:
        _LIB_LOAD_ERROR = e
        raise
    _bind(_lib)
    return _lib


def _bind(lib: ctypes.CDLL) -> None:
    """Bind the wdndds_* C ABI signatures to ctypes."""

    lib.wdndds_check_runtime.argtypes = [
        ctypes.POINTER(ctypes.c_char_p),
    ]
    lib.wdndds_check_runtime.restype = ctypes.c_int32

    lib.wdndds_free.argtypes = [ctypes.c_char_p]
    lib.wdndds_free.restype = None

    lib.wdndds_participant_create.argtypes = [
        ctypes.c_int32, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int32,
    ]
    lib.wdndds_participant_create.restype = ctypes.c_void_p

    lib.wdndds_participant_destroy.argtypes = [ctypes.c_void_p]
    lib.wdndds_participant_destroy.restype = None

    lib.wdndds_participant_guid.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
    ]
    lib.wdndds_participant_guid.restype = ctypes.c_int32

    lib.wdndds_participant_list_peers.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_uint32),
    ]
    lib.wdndds_participant_list_peers.restype = ctypes.c_int32

    lib.wdndds_participant_set_listener.argtypes = [
        ctypes.c_void_p, _peer_event_cb, ctypes.c_void_p,
    ]
    lib.wdndds_participant_set_listener.restype = ctypes.c_int32

    lib.wdndds_service_create.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.wdndds_service_create.restype = ctypes.c_void_p

    lib.wdndds_service_destroy.argtypes = [ctypes.c_void_p]
    lib.wdndds_service_destroy.restype = None

    lib.wdndds_replier_create.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,            # wdndds_request_cb_t
        ctypes.c_void_p,            # wdndds_peer_change_cb_t
        ctypes.c_void_p,
    ]
    lib.wdndds_replier_create.restype = ctypes.c_void_p

    lib.wdndds_replier_destroy.argtypes = [ctypes.c_void_p]
    lib.wdndds_replier_destroy.restype = None

    lib.wdndds_requester_create.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
    ]
    lib.wdndds_requester_create.restype = ctypes.c_void_p

    lib.wdndds_requester_destroy.argtypes = [ctypes.c_void_p]
    lib.wdndds_requester_destroy.restype = None

    lib.wdndds_requester_call.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p, ctypes.c_uint32,
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_uint32),
    ]
    lib.wdndds_requester_call.restype = ctypes.c_int32

    lib.wdndds_requester_matched_count.argtypes = [ctypes.c_void_p]
    lib.wdndds_requester_matched_count.restype = ctypes.c_int32


def check_runtime() -> tuple[bool, Optional[str]]:
    """Lightweight runtime check.

    Returns (available, error_message). The error_message is None on
    success and a str explaining the failure otherwise. The caller
    must NOT keep a reference to the returned str; it's a copy of
    the C buffer (the cshim frees its own buffer on return).
    """
    try:
        lib = get()
    except FileNotFoundError as e:
        return False, str(e)
    except OSError as e:
        return False, f"wdndds: failed to load cshim: {e}"

    out = ctypes.c_char_p()
    rc = lib.wdndds_check_runtime(ctypes.byref(out))
    if rc == 0:
        return True, None
    if out.value:
        msg = out.value.decode("utf-8", errors="replace")
        lib.wdndds_free(out.value)
        return False, msg
    return False, f"check_runtime rc={rc}"


# Auto-install the FastDDS profile for cross-process discovery on
# the local host (127.0.0.1:7410 unicast loopback, no multicast).
def _install_default_dds_profile():
    """Drop a default FastDDS profile into $XDG_RUNTIME_DIR (or /tmp)
    so processes that join the same domain can find each other via
    UDPv4 loopback without depending on the host's multicast routing.

    Idempotent. Skipped if FASTRTPS_DEFAULT_PROFILES_FILE is already set.
    """
    if os.environ.get("FASTRTPS_DEFAULT_PROFILES_FILE"):
        return

    candidates = [
        os.environ.get("XDG_RUNTIME_DIR"),
        os.environ.get("TMPDIR"),
        os.environ.get("TMP"),
        "/tmp",
        "/var/tmp",
    ]
    target_dir = next((d for d in candidates if d and d != ""), "/tmp")
    if not target_dir.endswith("/"):
        target_dir += "/"
    path = f"{target_dir}wdndds_default_profile.xml"

    try:
        with open(path, "w") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8" ?>\n'
                '<profiles xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles">\n'
                '    <participant profile_name="wdndds_default_profile" '
                'is_default_profile="true">\n'
                '        <rtps>\n'
                '            <builtin>\n'
                '                <initialPeersList>\n'
                '                    <locator>\n'
                '                        <udpv4>\n'
                '                            <address>127.0.0.1</address>\n'
                '                            <port>7410</port>\n'
                '                        </udpv4>\n'
                '                    </locator>\n'
                '                </initialPeersList>\n'
                '            </builtin>\n'
                '        </rtps>\n'
                '    </participant>\n'
                '</profiles>\n'
            )
        os.environ["FASTRTPS_DEFAULT_PROFILES_FILE"] = path
    except OSError as e:
        _LOG.debug("wdndds: failed to install default DDS profile at %s: %s",
                   path, e)


_install_default_dds_profile()

# (append at end - none for now, integration is via _rpc.py Hub.set_participant_listener)


# Peer event listener binding (added later — appended to existing types)

_peer_event_cb = ctypes.CFUNCTYPE(
    None, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_void_p,
)

# Note: bind is done in _bind() inside _cshim.py after _lib is loaded.
# The actual binding is added at the bottom of the existing _bind() function.
