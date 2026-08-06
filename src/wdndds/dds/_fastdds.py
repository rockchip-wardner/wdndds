from __future__ import annotations

import os
import sys
from typing import Any

_fastdds: Any | None = None


# --------------------------------------------------------------------------- #
# bundled fastdds Python bindings
#
# wdndds 把 eProsima FastDDS Python bindings 编译后直接打进 wheel:
#
#   src/wdndds/dds/_native/fastdds/__init__.py + _fastdds_python.so
#   src/wdndds/dds/_native/lib/libfastdds.so.3.6[.2.0] + libfastcdr.so.2[.3.6]
#
# 用户 ``pip install wdndds`` 一次就把所有原生依赖带进来；不需要 apt 装
# libfastdds-dev / fastdds-python3 / 从源码 colcon 构建 —— 那条路径根本
# 不存在（apt 上没包，PyPI 上也没包）。
#
# 加载顺序:
#   1. ctypes.CDLL(RTLD_GLOBAL) 显式预加载 bundled libfastcdr + libfastdds,
#      让 _fastdds_python.so 里的 dlopen(libfastdds.so.3.6) 能找到 SONAME
#   2. 把 bundled ``_native/`` 目录 prepend 到 sys.path, 让 ``import fastdds``
#      命中 ``_native/fastdds/__init__.py``,而不是系统可能存在的同名包
#   3. 系统 fastdds (apt 装的是 libfastcdr1,SONAME .so.1,跟我们要的 .so.2
#      不兼容) 不可能满足, 所以不需要"先试系统再试内置"的分流
# --------------------------------------------------------------------------- #
_BUNDLED_NATIVE_DIR = os.path.join(os.path.dirname(__file__), "_native")
_BUNDLED_LIB_DIR = os.path.join(_BUNDLED_NATIVE_DIR, "lib")


# At import time, add the bundled fastdds directory to sys.path so any
# subsequent ``import fastdds`` (by us, by callers, by transitive
# dependencies) hits the bundled module. We also preload the shared
# libs that ``_fastdds_python.so`` transitively needs (libfastcdr +
# libfastdds) BEFORE any fastdds import — otherwise Python's own
# dlopen on the .so fails with "libfastdds.so.3.6: cannot open shared
# object file".
def _bootstrap_bundled_at_import() -> None:
    native_dir = _BUNDLED_NATIVE_DIR
    if not os.path.isdir(os.path.join(native_dir, "fastdds")):
        return

    # 1. Preload shared libs (CTYPES_CDLL with RTLD_GLOBAL).
    import ctypes as _ct
    for _soname in ("libfastcdr.so.2", "libfastdds.so.3.6"):
        _p = os.path.join(_BUNDLED_LIB_DIR, _soname)
        if os.path.isfile(_p):
            try:
                _ct.CDLL(_p, mode=_ct.RTLD_GLOBAL)
            except OSError:
                pass

    # 2. Preload bundled C++ helper (WdnddsStringTopicDataType SWIG).
    _wds_so = os.path.join(native_dir, "_wdndds_string.so")
    if os.path.isfile(_wds_so):
        try:
            _ct.CDLL(_wds_so, mode=_ct.RTLD_GLOBAL)
        except OSError:
            pass

    # 3. Prepend to sys.path so ``import fastdds`` / ``import wdndds_string``
    #    hit the bundled modules before any same-named site-packages entry.
    if native_dir not in sys.path:
        sys.path.insert(0, native_dir)


_bootstrap_bundled_at_import()


def _preload_bundled_libs() -> None:
    """ctypes 预加载 bundled shared libs (RTLD_GLOBAL)。

    _fastdds_python.so 启动时会 dlopen libfastdds.so.3.6, 而后者又依赖
    libfastcdr.so.2 —— SONAME 必须满足。系统 apt 上的 libfastcdr 是
    .so.1.x, SONAME 不匹配;不显式预加载 bundled 的版本 import 直接
    失败 (libfastdds.so.3.6 => not found).
    """
    import ctypes

    # libfastcdr 必须先于 libfastdds,后者对它有依赖
    for soname in ("libfastcdr.so.2", "libfastdds.so.3.6"):
        path = os.path.join(_BUNDLED_LIB_DIR, soname)
        if not os.path.isfile(path):
            continue
        try:
            ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
        except OSError:
            # 已经加载过同名 SONAME 没关系,跳过
            pass


def _ensure_bundled_on_path() -> None:
    """Prepend bundled ``_native/`` 目录到 sys.path。

    这样后面的 ``import fastdds`` 命中 ``_native/fastdds/__init__.py``,
    不会去碰系统 site-packages 里碰巧同名的第三方包 (理论上不该有,
    但显式 prepend 防一手)。
    """
    if _BUNDLED_NATIVE_DIR in sys.path:
        return
    sys.path.insert(0, _BUNDLED_NATIVE_DIR)


def _bundled_available() -> bool:
    """内置 fastdds 包是否齐:目录 + _fastdds_python.so + __init__.py。"""
    pkg = os.path.join(_BUNDLED_NATIVE_DIR, "fastdds")
    return (
        os.path.isdir(pkg)
        and os.path.isfile(os.path.join(pkg, "__init__.py"))
        and os.path.isfile(
            os.path.join(pkg, "_fastdds_python.so")
        )
    )


def _preload_bundled_wdndds_string() -> None:
    """Preload the bundled C++ ``WdnddsStringTopicDataType`` SWIG extension.

    The wdndds wheel ships a tiny C++ helper that defines a working
    string-typed TopicDataType (the upstream ``eprosima::fastdds::dds::
    StringTopicDataType`` is C++-only, never SWIG-wrapped). The SWIG
    glue and impl are linked into ``_wdndds_string.so`` which we
    preload with ``RTLD_GLOBAL`` so its symbols (which reference the
    bundled ``libfastdds.so``) are available before any
    ``fastdds.DomainParticipant.register_type`` call.
    """
    import ctypes

    so_path = os.path.join(_BUNDLED_NATIVE_DIR, "_wdndds_string.so")
    if not os.path.isfile(so_path):
        return
    try:
        ctypes.CDLL(so_path, mode=ctypes.RTLD_GLOBAL)
    except OSError:
        pass


def get() -> Any:
    global _fastdds
    if _fastdds is not None:
        return _fastdds

    # 先尝试内置 fastdds (正常路径)
    if _bundled_available():
        _preload_bundled_libs()
        _preload_bundled_wdndds_string()
        _ensure_bundled_on_path()
        try:
            import fastdds as _mod
        except Exception as exc:
            # 内置版本导入失败 —— 继续 fallback 到系统 fastdds,给用户
            # 一个自救路径 (例如他们在另一个 wheel 里塞了同样 bundled,
            # 但二进制不兼容;此时报个清晰错误比较好)
            raise ImportError(
                "wdndds.dds: bundled fastdds bindings failed to import. "
                "Try ``pip install --force-reinstall --no-deps wdndds`` "
                "to overwrite the wheel. Original error: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    else:
        # 没内置 (用户自己 wheel 出来的旧版本?) —— 退回系统 fastdds,行为
        # 跟之前一样: 找不到就 raise 一个有用的指引。注意此时不需要再
        # preload (系统的 .so 是标准 lib 路径里能直接找到的)。
        try:
            import fastdds as _mod
        except ImportError as exc:
            raise ImportError(
                "wdndds.dds requires the eProsima Fast DDS Python bindings "
                "(importable as 'fastdds' / 'fastdds_python').\n"
                "This wdndds wheel bundles them under wdndds/dds/_native/, "
                "but they appear to be missing — your wheel may have been "
                "built without the bundled native binaries (reinstall a "
                "wheel that matches your platform: "
                "aarch64 Debian 12 / glibc 2.36 / Python 3.11).\n"
                "If you really want to fall back to a system install, see "
                "https://fast-dds.docs.eprosima.com/en/latest/"
                "fastdds/python_bindings/python_bindings.html (build from "
                "source)."
            ) from exc

    # SWIG 不会把 fastdds::dds::StatusMask::none() 暴露成模块级属性;
    # wdndds.dds 内部多处 ``fd.STATUS_MASK_NONE`` 当 ``StatusMask`` 实例
    # 传给 ``create_participant(topic, qos, listener, mask)`` 这类重载,
    # SWIG 类型检查严格 —— 整数 0 不会被自动转成 StatusMask。这里统一
    # 注入 ``StatusMask.none()`` 的真实例,值与 C++ 端 ``StatusMask::none()``
    # (= 0) 一致。
    if not hasattr(_mod, "STATUS_MASK_NONE"):
        _mod.STATUS_MASK_NONE = _mod.StatusMask.none()

    # Bring the bundled wdndds_string C++ helper module into
    # ``sys.modules`` so ``wdndds.dds.types`` can ``import wdndds_string``
    # without a second ``sys.path.insert`` dance. ``wdndds_string`` is
    # the SWIG-generated wrapper for ``_wdndds_string.so``, shipped as
    # ``wdndds.dds._native.wdndds_string`` (we point ``sys.modules`` at
    # ``wdndds_string`` for backwards compat with the Python API).
    if "wdndds_string" not in sys.modules and _bundled_available():
        try:
            import importlib
            importlib.import_module("wdndds.dds._native.wdndds_string")
            sys.modules["wdndds_string"] = sys.modules[
                "wdndds.dds._native.wdndds_string"
            ]
        except Exception:
            # Missing or incompatible custom type — the SDK will still
            # work for everything except the string-typed DDS topics,
            # which is what ``register_string_type`` catches.
            pass

    _fastdds = _mod
    return _mod


def check_runtime() -> dict[str, Any]:
    info: dict[str, Any] = {"available": False}
    try:
        mod = get()
    except ImportError as exc:
        info["error"] = str(exc)
        info["bundled"] = _bundled_available()
        return info
    info["available"] = True
    info["module"] = mod.__name__
    info["path"] = getattr(mod, "__file__", None)
    info["version"] = getattr(mod, "__version__", "unknown")
    info["bundled"] = _bundled_available()
    return info