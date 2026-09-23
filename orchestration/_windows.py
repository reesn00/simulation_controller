"""orchestration._windows: Windows 控制台窗口抑制工具.

背景
----

Pytest / IDE 测试运行器在 Windows 上跑 ``multiprocessing.Pool`` worker 或
``daemon.start_detached`` 子进程时,默认会弹一个 cmd 终端窗口,挤占桌面。

根因:

1. CPython 3.12 ``Lib/multiprocessing/popen_spawn_win32.py:77`` 调
   ``_winapi.CreateProcess(python_exe, cmd, ..., 0, ...)`` (creationflags=0)
   创建 worker 子进程。``python.exe`` 是 console application,creationflags=0
   即"按默认行为" → **会** 创建新控制台窗口。
2. ``daemon.start_detached`` 调 ``subprocess.Popen(argv, creationflags=
   DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP)``。``DETACHED_PROCESS`` 仅让
   子进程脱离父控制台,但 ``python.exe`` 仍会创建**新**控制台窗口。

修复
----

提供幂等的 ``install_no_window_policy()``:

- **multiprocessing.Pool worker**:monkey-patch ``_winapi.CreateProcess``,
  凡是 cmd 行包含 ``--multiprocessing-fork`` 指纹的调用 (CPython 3.12
  ``spawn.get_command_line`` 生成的 worker 启动命令行唯一标记),自动把
  dwCreationFlags 从 0 补成 ``CREATE_NO_WINDOW``(0x08000000)。其他
  _winapi.CreateProcess 用途 (其他库 / 其他子进程) 不受影响。
- 非 Windows 平台:no-op。
- 多次调用:幂等,后续是 no-op。

副作用
----

- 多进程 worker 子进程的 stdin/stdout/stderr 行为不变 (multiprocessing
  通过 pipe 通信,CREATE_NO_WINDOW 不影响 pipe handle 继承)。
- ``subprocess.run`` / ``subprocess.Popen`` 默认行为不变;需要走
  ``daemon.start_detached`` 的子进程,在其 creationflags 中显式 OR 上
  ``CREATE_NO_WINDOW``(本模块导出 ``CREATE_NO_WINDOW`` 常量供使用)。
"""

from __future__ import annotations

import functools
import sys

__all__ = [
    "CREATE_NO_WINDOW",
    "install_no_window_policy",
    "is_no_window_policy_installed",
]


# CREATE_NO_WINDOW (Windows):禁止子进程创建新控制台窗口。
# 常量供 daemon.start_detached 等显式 Popen 使用。
CREATE_NO_WINDOW: int = 0x08000000

# multiprocessing.spawn 启动 worker 的命令行指纹 (CPython 3.12
# spawn.get_command_line):形如 ``<python> -c
# "from multiprocessing.spawn import spawn_main; ..."`` 加
# ``--multiprocessing-fork``。这是 worker 启动的唯一可靠标记。
_MP_FORK_FLAG: str = "--multiprocessing-fork"

# 哨兵:幂等标记。模块级 state 即可,进程内单例语义。
_INSTALLED: bool = False


def is_no_window_policy_installed() -> bool:
    """返回是否已在本进程内安装 no-window 策略 (幂等检查)."""
    return _INSTALLED


def install_no_window_policy() -> bool:
    """在本进程内安装 no-window 策略.

    幂等:首次调用真正 patch,后续调用 no-op。

    Returns:
        True 表示本次**新安装**(Windows 平台);False 表示已安装或非
        Windows 平台 (no-op)。

    在 Windows 上,会 monkey-patch ``_winapi.CreateProcess``,对包含
    ``--multiprocessing-fork`` 指纹的 cmd 行,强制把 dwCreationFlags
    补上 ``CREATE_NO_WINDOW``。其他 _winapi.CreateProcess 用途不受影响。
    """
    global _INSTALLED

    if _INSTALLED:
        return False

    if sys.platform != "win32":
        # 非 Windows:概念不存在,标记为"已处理"避免重复判断
        _INSTALLED = True
        return False

    _patch_winapi_createprocess()
    _INSTALLED = True
    return True


# ---------------------------------------------------------------------------
# 实现:拦截 _winapi.CreateProcess
# ---------------------------------------------------------------------------


def _patch_winapi_createprocess() -> None:
    """拦截 ``_winapi.CreateProcess`` 调用,给 multiprocessing spawn worker
    强制 CREATE_NO_WINDOW.

    CPython 3.12 ``popen_spawn_win32.Popen.__init__`` 走
    ``_winapi.CreateProcess(python_exe, cmd, None, None, False,
    dwCreationFlags, env, None, None)``。在 _winapi 模块层 wrap
    CreateProcess,对包含 ``--multiprocessing-fork`` 指纹的 cmd 自动补
    CREATE_NO_WINDOW。已有非零 creationflags 的调用方不受影响 (尊重意图)。
    """
    import _winapi as _w

    if getattr(_w.CreateProcess, "_no_window_wrapped", False):
        return

    _original_createprocess = _w.CreateProcess

    @functools.wraps(_original_createprocess)
    def _wrapped_createprocess(
        application_name, command_line, *args, **kwargs
    ):  # type: ignore[no-untyped-def]
        # multiprocessing.spawn 启动 worker 的 cmd 行总含 ``--multiprocessing-fork``
        # (CPython 3.12 spawn.get_command_line 注入),这是 worker 启动唯一可靠指纹。
        if isinstance(command_line, str) and _MP_FORK_FLAG in command_line:
            if len(args) >= 6:
                # 位置参数形式:
                #   proc_attrs, thread_attrs, bInheritHandles,
                #   dwCreationFlags, lpEnvironment, ...
                args = list(args)
                if args[3] == 0:
                    args[3] = CREATE_NO_WINDOW
                args = tuple(args)
            else:
                # 关键字参数形式;未传则补,已传非零值尊重
                if kwargs.get("dwCreationFlags", 0) == 0:
                    kwargs["dwCreationFlags"] = CREATE_NO_WINDOW
        return _original_createprocess(
            application_name, command_line, *args, **kwargs
        )

    _wrapped_createprocess._no_window_wrapped = True  # type: ignore[attr-defined]
    _w.CreateProcess = _wrapped_createprocess  # type: ignore[attr-assign]