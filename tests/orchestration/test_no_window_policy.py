"""orchestration._windows 单元测试.

验证 ``install_no_window_policy()`` 在 Windows 上 monkey-patch
``_winapi.CreateProcess``、强制 multiprocessing spawn worker 使用
``CREATE_NO_WINDOW`` (0x08000000);非 Windows 平台是 no-op;多次调用幂等。
"""

from __future__ import annotations

import multiprocessing
import subprocess
import sys
from pathlib import Path

import _winapi  # type: ignore[import-not-found]
import pytest

from orchestration._windows import (
    CREATE_NO_WINDOW,
    install_no_window_policy,
    is_no_window_policy_installed,
)

import orchestration._windows as _w_pkg


def _pool_task_return_42() -> int:
    """Pool worker 任务:模块级函数,可 pickle."""
    return 42


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 _INSTALLED 重置为 False,让后续 install 真的触发 patch.

    同时保存当前 wrap,test 末恢复(单进程测试隔离)。

    注意:本测试假设 wrap 函数通过 closure 持有 ``original = _winapi.CreateProcess``
    的引用;重置 _INSTALLED 后再 install 会用当前的 ``_winapi.CreateProcess``
    作为新 original (这是 spy 替换的基础)。
    """
    monkeypatch.setattr(_w_pkg, "_INSTALLED", False)


# ---------------------------------------------------------------------------
# 幂等性 + 平台行为
# ---------------------------------------------------------------------------


def test_install_is_idempotent(reset_policy: None) -> None:
    """多次调 install → 后续是 no-op,is_installed 始终 True."""
    install_no_window_policy()  # 首次
    assert is_no_window_policy_installed() is True

    for _ in range(3):
        assert install_no_window_policy() is False  # 后续 no-op
    assert is_no_window_policy_installed() is True


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only: patch _winapi.CreateProcess",
)
def test_createprocess_gets_wrapped_marker(reset_policy: None) -> None:
    """在 Windows 上,``_winapi.CreateProcess`` 应被打上 ``_no_window_wrapped`` 标记。"""
    install_no_window_policy()
    assert getattr(_winapi.CreateProcess, "_no_window_wrapped", False) is True


# ---------------------------------------------------------------------------
# wrap 行为(直接调 wrap 验证 dwCreationFlags 注入/不注入)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only",
)
def test_wrap_with_mp_fork_injects_create_no_window(
    reset_policy: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """含 ``--multiprocessing-fork`` 指纹的 cmd 行,wrap 应把
    dwCreationFlags 从 0 补成 ``CREATE_NO_WINDOW``。"""
    captured: dict[str, int] = {}

    def _fake_real(app, cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if len(args) >= 6:
            captured["flags"] = int(args[3])
        else:
            captured["flags"] = int(kwargs.get("dwCreationFlags", 0))
        # 模拟失败;不要真起进程
        raise OSError("test sentinel")

    # 用 _fake_real 当底层;再 install 让 wrap 把它当 original
    monkeypatch.setattr(_winapi, "CreateProcess", _fake_real)
    install_no_window_policy()

    with pytest.raises(OSError, match="test sentinel"):
        _winapi.CreateProcess(
            None,
            f"<dummy> python -c pass {_w_pkg._MP_FORK_FLAG}",
            None, None, False, 0, None, None, None,
        )
    assert captured["flags"] == CREATE_NO_WINDOW


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only",
)
def test_wrap_with_explicit_nonzero_flags_respects_caller(
    reset_policy: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dwCreationFlags 显式传非零值时,wrap 不覆盖 (尊重调用方)。"""
    captured: dict[str, int] = {}

    def _fake_real(app, cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if len(args) >= 6:
            captured["flags"] = int(args[3])
        else:
            captured["flags"] = int(kwargs.get("dwCreationFlags", 0))
        raise OSError("test sentinel")

    monkeypatch.setattr(_winapi, "CreateProcess", _fake_real)
    install_no_window_policy()

    with pytest.raises(OSError):
        _winapi.CreateProcess(
            None,
            f"<dummy> python -c pass {_w_pkg._MP_FORK_FLAG}",
            None, None, False, 0x00000008, None, None, None,  # DETACHED_PROCESS
        )
    # 调用方已传 0x00000008 → wrap 不补,仍 0x00000008
    assert captured["flags"] == 0x00000008


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only",
)
def test_wrap_without_mp_fork_does_not_inject(
    reset_policy: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cmd 不含 ``--multiprocessing-fork`` 指纹时,wrap 不改 dwCreationFlags
    (避免影响其他 _winapi.CreateProcess 用途)。"""
    captured: dict[str, int] = {}

    def _fake_real(app, cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if len(args) >= 6:
            captured["flags"] = int(args[3])
        else:
            captured["flags"] = int(kwargs.get("dwCreationFlags", 0))
        raise OSError("test sentinel")

    monkeypatch.setattr(_winapi, "CreateProcess", _fake_real)
    install_no_window_policy()

    with pytest.raises(OSError):
        _winapi.CreateProcess(
            None,
            "<dummy> python -c pass",  # 无 mp fork 指纹
            None, None, False, 0, None, None, None,
        )
    assert captured["flags"] == 0  # wrap 没动


# ---------------------------------------------------------------------------
# multiprocessing.Pool 集成烟雾测试
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only",
)
def test_mp_pool_worker_creationflags_have_no_window(
    reset_policy: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 ``multiprocessing.Pool(1)``:worker 子进程的 _winapi.CreateProcess
    调用 dwCreationFlags 包含 ``CREATE_NO_WINDOW``。"""
    flags_seen: list[int] = []

    if sys.platform == "win32":
        real_createprocess = _winapi.CreateProcess.__wrapped__  # type: ignore[attr-defined]
    else:  # pragma: no cover - skipif 已守门
        real_createprocess = _winapi.CreateProcess

    def _spy(app, cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if len(args) >= 6:
            flags_seen.append(int(args[3]))
        else:
            flags_seen.append(int(kwargs.get("dwCreationFlags", 0)))
        return real_createprocess(app, cmd, *args, **kwargs)

    # spy 替换;再 install,wrap 把 spy 当 original (spy 同时记录 flags)
    monkeypatch.setattr(_winapi, "CreateProcess", _spy)
    install_no_window_policy()

    with multiprocessing.Pool(processes=1) as pool:
        result = pool.apply_async(_pool_task_return_42).get(timeout=10)
    assert result == 42

    # Pool 至少起 1 个 worker,标志应包含 CREATE_NO_WINDOW
    assert any(
        (f & CREATE_NO_WINDOW) for f in flags_seen
    ), f"worker subprocess creation did not get CREATE_NO_WINDOW: {flags_seen}"


# ---------------------------------------------------------------------------
# daemon.start_detached 创建子进程的 creationflags 验证
# ---------------------------------------------------------------------------


def test_daemon_start_detached_creationflags_have_no_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``daemon.start_detached`` 在 Windows 上传的 creationflags 应包含
    ``CREATE_NO_WINDOW`` (除原有 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)。"""
    from orchestration.daemon import start_detached

    captured: dict[str, object] = {}
    real_popen = subprocess.Popen

    class _RecordingPopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            self._real = real_popen(argv, **kwargs)

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(self._real, name)

    monkeypatch.setattr(
        "orchestration.daemon.subprocess.Popen", _RecordingPopen,
    )

    pid_file = tmp_path / "d.pid"
    log_dir = tmp_path / "logs"
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]

    handle = start_detached(
        argv=cmd, pid_file=pid_file, log_dir=log_dir,
        child_mode_arg="--ignored",
    )
    try:
        if sys.platform == "win32":
            flags = int(captured.get("creationflags", 0))  # type: ignore[arg-type]
            assert (flags & CREATE_NO_WINDOW) == CREATE_NO_WINDOW
            assert (flags & 0x00000008) == 0x00000008  # DETACHED_PROCESS
            assert (flags & 0x00000200) == 0x00000200  # CREATE_NEW_PROCESS_GROUP
    finally:
        if handle.is_alive():
            handle.proc.kill()
            handle.proc.wait(timeout=5)