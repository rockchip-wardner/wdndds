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
#   (or 1.x flat layout: _native/fastdds.py + _native/_fastdds_python.so +
#    _native/lib/libfastrtps.so.2.6 + libfastcdr.so.1)
#
# 用户 ``pip install wdndds`` 一次就把所有原生依赖带进来；不需要 apt 装
# libfastdds-dev / fastdds-python3 / 从源码 colcon 构建 —— 那条路径根本
# 不存在（apt 上没包，PyPI 上也没包）。
#
# 加载顺序 (在 import _fastdds 阶段就同步跑完,后面 ``import fastdds`` /
# ``import wdndds_string`` 直接受益,不会被它们的 dlopen 抢跑):
#
#   1. ctypes.CDLL(RTLD_GLOBAL) 显式预加载 bundled libfastcdr +
#      libfastdds/libfastrtps + _wdndds_string.so,把 SONAME 注册到
#      进程全局已加载库表,让 _fastdds_python.so 里的 dlopen(SONAME) 找得到
#   2. 把 bundled ``_native/`` 目录 prepend 到 sys.path, 让 ``import fastdds``
#      命中 bundled,而不是系统可能存在的同名包
#   3. 同步设 ``LD_LIBRARY_PATH`` —— 当前进程来不及生效(动态链接器已
#      初始化),但后续 subprocess 继承,可兜底外部工具链
#   4. 每条 SONAME 的 present / loaded / error 记到 _PRELOAD_RESULTS,
#      ``import fastdds`` 失败时把真实根因(arch / glibc / 损坏)wrap 进
#      ImportError,而不是把"cannot open shared object file"这层表象抛给用户
# --------------------------------------------------------------------------- #
_BUNDLED_NATIVE_DIR = os.path.join(os.path.dirname(__file__), "_native")
_BUNDLED_LIB_DIR = os.path.join(_BUNDLED_NATIVE_DIR, "lib")

# 每条 SONAME 的预加载结果: {soname, path, present, loaded, error}
# 供 get() 的 import 失败分支 + check_runtime() 读取。
_PRELOAD_RESULTS: list[dict[str, Any]] = []


def _record_preload(soname: str, path: str) -> dict[str, Any]:
    """Try to preload a single .so with ctypes+RTLD_GLOBAL and record the outcome.

    Returns the record dict (also appended to _PRELOAD_RESULTS). Error
    is captured verbatim — the original ``import fastdds`` failure
    ("cannot open shared object file") is a red herring; the real
    cause is whatever OSError ctypes raises here (wrong arch, GLIBC
    version missing, file truncated, etc.).
    """
    import ctypes as _ct

    rec: dict[str, Any] = {
        "soname": soname,
        "path": path,
        "present": False,
        "loaded": False,
        "error": None,
    }
    if not os.path.isfile(path):
        _PRELOAD_RESULTS.append(rec)
        return rec
    rec["present"] = True
    try:
        _ct.CDLL(path, mode=_ct.RTLD_GLOBAL)
        rec["loaded"] = True
    except OSError as e:
        rec["error"] = f"{type(e).__name__}: {e}"
    _PRELOAD_RESULTS.append(rec)
    return rec


def _bootstrap_bundled_at_import() -> None:
    """在 import _fastdds 阶段同步预加载,覆盖 1.x flat / 2.x package 两种 layout。

    早期版本对 1.x flat layout 是 no-op,preload 推到 ``_get()`` 懒路径——
    缺点是任何在 ``get()`` 之前发生的 ``import fastdds`` 都会因 SONAME
    找不到而失败。现在统一在 import 阶段同步做。
    """
    native_dir = _BUNDLED_NATIVE_DIR
    lib_dir = _BUNDLED_LIB_DIR

    # 0. LD_LIBRARY_PATH 子进程继承兜底 (对当前进程动态链接器不生效,
    #    留作 spawn 出去的 rkv encoder / external DDS 工具用)
    if os.path.isdir(lib_dir):
        current = os.environ.get("LD_LIBRARY_PATH", "")
        parts = [p for p in current.split(":") if p]
        if lib_dir not in parts:
            parts.insert(0, lib_dir)
            os.environ["LD_LIBRARY_PATH"] = ":".join(parts)

    # 1. libfastcdr 必须先于 libfastdds/libfastrtps (后者对它有 DT_NEEDED)
    for soname in (
        # 3.x (master)
        "libfastcdr.so.2",
        "libfastdds.so.3.6",
        # 2.x (legacy FastRTPS naming, 当前 bundled 即此分支)
        "libfastcdr.so.1",
        "libfastrtps.so.2.6",
    ):
        _record_preload(soname, os.path.join(lib_dir, soname))

    # 2. bundled C++ helper (WdnddsStringTopicDataType SWIG) 同样 RTLD_GLOBAL
    _wds_so = os.path.join(native_dir, "_wdndds_string.so")
    if os.path.isfile(_wds_so):
        _record_preload("_wdndds_string.so", _wds_so)

    # 3. sys.path prepend,让后续 ``import fastdds`` / ``import wdndds_string``
    #    命中 bundled,而不是系统 site-packages 里碰巧同名的包
    if os.path.isdir(native_dir) and native_dir not in sys.path:
        sys.path.insert(0, native_dir)


_bootstrap_bundled_at_import()


def _preload_diagnostics() -> list[dict[str, Any]]:
    return list(_PRELOAD_RESULTS)


def _preload_bundled_libs() -> None:
    """Backward-compat no-op。

    早期版本懒路径的入口。preload 已经在 :func:`_bootstrap_bundled_at_import`
    阶段同步跑完,这里只需返回,避免对同一 SONAME 二次 ctypes.CDLL。
    """
    return


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
    """内置 fastdds 是否齐。

    支持两种 layout:

    * 2.x/3.x 包形式: ``_native/fastdds/__init__.py`` + ``_fastdds_python.so``
    * 1.x flat 形式:    ``_native/fastdds.py``             + ``_fastdds_python.so``

    Fast-DDS-Python 1.x (FastDDS 2.6.x 配套版本) 输出 flat ``fastdds.py``;
    2.x 之后切到 ``fastdds/__init__.py`` 包形式。
    """
    pkg_dir = os.path.join(_BUNDLED_NATIVE_DIR, "fastdds")
    if os.path.isdir(pkg_dir):
        return (
            os.path.isfile(os.path.join(pkg_dir, "__init__.py"))
            and os.path.isfile(os.path.join(pkg_dir, "_fastdds_python.so"))
        )
    return (
        os.path.isfile(os.path.join(_BUNDLED_NATIVE_DIR, "fastdds.py"))
        and os.path.isfile(os.path.join(_BUNDLED_NATIVE_DIR, "_fastdds_python.so"))
    )


def _preload_bundled_wdndds_string() -> None:
    """Backward-compat no-op。

    preload 已在 :func:`_bootstrap_bundled_at_import` 同步跑完,
    这里只是占位以防外部代码继续调用。
    """
    return


def _format_import_error(exc: BaseException) -> str:
    """Build a diagnostic ImportError message from preload + import results.

    Original ``import fastdds`` failure is typically a red herring
    ("libfastrtps.so.2.6: cannot open shared object file") — the
    real cause is whatever OSError ctypes raised during preload
    (wrong arch, GLIBC version missing, file truncated, RTLD_GLOBAL
    not propagated). Re-surface the preload error verbatim so the
    user can act on it.
    """
    diag = _preload_diagnostics()
    failed = [r for r in diag if r.get("present") and not r.get("loaded")]
    missing = [r for r in diag if not r.get("present")]

    parts: list[str] = ["wdndds.dds: bundled fastdds bindings failed to import."]
    if failed:
        lines = "\n".join(f"  - {r['soname']}: {r['error']}" for r in failed)
        parts.append(
            "Preload failures (this is the actual root cause, NOT the "
            "import error below — that one is just the symptom):\n" + lines
        )
    elif missing:
        lines = "\n".join(
            f"  - {r['soname']}: not present at {r['path']}" for r in missing
        )
        parts.append(
            "Bundled .so files missing from _native/lib/ (the wheel was "
            "built without them — reinstall a wheel that matches your "
            "platform: aarch64 Debian 12 / glibc 2.36 / Python 3.11):\n" + lines
        )
    parts.append(f"Original import error: {type(exc).__name__}: {exc}")
    return "\n".join(parts)


def get() -> Any:
    global _fastdds
    if _fastdds is not None:
        return _fastdds

    # 先尝试内置 fastdds (正常路径)
    if _bundled_available():
        # preload 已在 import _fastdds 阶段同步跑完 (覆盖 1.x flat +
        # 2.x package 两种 layout)。这里只需确保 _native/ 在 sys.path
        # 上,以及退化情况下还能再尝试一次 preload (例如 _get() 被多次
        # 调用且之间 sys.path 被改过)。
        _preload_bundled_libs()
        _preload_bundled_wdndds_string()
        _ensure_bundled_on_path()
        try:
            import fastdds as _mod
        except Exception as exc:
            # 内置版本导入失败 —— 真实原因九成九在 _PRELOAD_RESULTS 里
            # (arch / glibc / 二进制损坏),而不是"wheel 不对"。把 preload
            # 失败贴出来,避免客户在错的修复路径上瞎撞。
            raise ImportError(_format_import_error(exc)) from exc
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

    # Fast-DDS-Python 1.x 把 ``RETCODE_OK`` 放在 ``fastdds.ReturnCode_t``
    # 类属性里(没在模块级暴露);2.x 之后切到模块级 ``fastdds.RETCODE_OK``。
    # wdndds SDK 内部 ``check(rc, ..., get().RETCODE_OK)`` 当模块属性读,
    # 1.x 下会 AttributeError —— 这里做一次兜底,值仍是 0。
    if not hasattr(_mod, "RETCODE_OK"):
        try:
            _mod.RETCODE_OK = int(_mod.ReturnCode_t.RETCODE_OK)
        except AttributeError:
            pass

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
    info["preload"] = _preload_diagnostics()
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