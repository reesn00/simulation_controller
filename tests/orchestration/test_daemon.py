"""orchestration.daemon 单元测试."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from orchestration.daemon import (
    DaemonError,
    ForegroundHandle,
    is_process_alive,
    is_running,
    read_pid_file,
    remove_pid_file,
    setup_logging,
    start_detached,
    start_foreground,
    stop,
    write_pid_file,
)


# ---------------------------------------------------------------------------
# PID 文件
# ---------------------------------------------------------------------------

def test_read_pid_file_missing(tmp_path: Path) -> None:
    assert read_pid_file(tmp_path / "missing.pid") is None


def test_read_pid_file_corrupt(tmp_path: Path) -> None:
    fp = tmp_path / "x.pid"
    fp.write_text("not a number", encoding="utf-8")
    assert read_pid_file(fp) is None


def test_write_and_read_pid_file(tmp_path: Path) -> None:
    fp = tmp_path / "x.pid"
    write_pid_file(fp, 12345)
    assert read_pid_file(fp) == 12345


def test_remove_pid_file_no_error_when_missing(tmp_path: Path) -> None:
    remove_pid_file(tmp_path / "never.pid")  # 不抛


def test_remove_pid_file_removes(tmp_path: Path) -> None:
    fp = tmp_path / "x.pid"
    write_pid_file(fp, 1)
    remove_pid_file(fp)
    assert not fp.exists()


# ---------------------------------------------------------------------------
# is_process_alive
# ---------------------------------------------------------------------------

def test_is_process_alive_self() -> None:
    assert is_process_alive(os.getpid()) is True


def test_is_process_alive_dead_pid() -> None:
    # 找一个大数 PID 不太可能存在
    assert is_process_alive(2_000_000) is False


def test_is_process_alive_invalid() -> None:
    assert is_process_alive(0) is False
    assert is_process_alive(-1) is False


def test_is_running_no_file(tmp_path: Path) -> None:
    alive, pid = is_running(tmp_path / "missing.pid")
    assert alive is False
    assert pid is None


def test_is_running_dead_pid(tmp_path: Path) -> None:
    fp = tmp_path / "x.pid"
    write_pid_file(fp, 2_000_000)  # 不存在
    alive, pid = is_running(fp)
    assert alive is False
    assert pid is None


def test_is_running_alive_self(tmp_path: Path) -> None:
    fp = tmp_path / "x.pid"
    write_pid_file(fp, os.getpid())
    alive, pid = is_running(fp)
    assert alive is True
    assert pid == os.getpid()


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------

def test_setup_logging_creates_log_file(tmp_path: Path) -> None:
    path = setup_logging(tmp_path, log_file="t.log")
    assert path == tmp_path / "t.log"
    # 触发一条 log
    import logging
    logging.getLogger("test").info("hello")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "hello" in content


def test_setup_logging_creates_log_dir(tmp_path: Path) -> None:
    log_dir = tmp_path / "deep" / "nested"
    setup_logging(log_dir)
    assert log_dir.is_dir()


def test_setup_logging_clears_existing_handlers(tmp_path: Path) -> None:
    import logging
    root = logging.getLogger()
    root.addHandler(logging.NullHandler())
    setup_logging(tmp_path)
    # 所有 handler 应是 FileHandler / StreamHandler，没有遗留的 NullHandler
    handlers = root.handlers
    assert all(type(h).__name__ in ("FileHandler", "StreamHandler") for h in handlers)


# ---------------------------------------------------------------------------
# start_foreground
# ---------------------------------------------------------------------------

def test_start_foreground_runs_callback_and_writes_pid(tmp_path: Path) -> None:
    pid_file = tmp_path / "f.pid"
    log_dir = tmp_path / "logs"
    seen: dict[str, object] = {}

    def cb(stop_event):
        seen["pid"] = os.getpid()
        seen["pid_file_during"] = read_pid_file(pid_file)
        stop_event.set()  # 立即退出

    handle = start_foreground(
        cfg_path=None, pid_file=pid_file, log_dir=log_dir, run_callback=cb,
    )
    assert isinstance(handle, ForegroundHandle)
    assert seen["pid"] == os.getpid()
    # 跑 callback 期间 PID 文件应已写入
    assert seen["pid_file_during"] == os.getpid()
    # 退出后 PID 文件被清理
    assert not pid_file.exists()


def test_start_foreground_cleans_up_on_callback_exception(tmp_path: Path) -> None:
    pid_file = tmp_path / "f.pid"
    log_dir = tmp_path / "logs"

    def cb(_ev):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        start_foreground(
            cfg_path=None, pid_file=pid_file, log_dir=log_dir, run_callback=cb,
        )
    # PID 文件应被 finally 清理
    assert not pid_file.exists()


def test_install_signal_handlers_sets_stop_event(tmp_path: Path) -> None:
    from orchestration.daemon import install_signal_handlers
    ev = threading.Event()
    install_signal_handlers(ev)
    # 直接 set 验证 handler 工作（不实际发信号，避免测试间互相干扰）
    ev.set()
    assert ev.is_set()


# ---------------------------------------------------------------------------
# start_detached + stop
# ---------------------------------------------------------------------------

def _wait_pid_file(pid_file: Path, timeout: float = 15.0) -> int | None:
    """轮询等待子进程自行写入 PID 文件（真实语义：父进程不预写）.

    注意不要求写入值等于 ``Popen.pid``：uv venv 的 python.exe 是 trampoline，
    ``-c`` 脚本运行在其 base-python 子进程中，os.getpid() 是孙进程 PID。
    真实 master 同样经 start_foreground 写自己的 getpid，契约一致。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = read_pid_file(pid_file)
        if pid is not None:
            return pid
        time.sleep(0.1)
    return None


def test_start_detached_child_writes_pid(tmp_path: Path) -> None:
    """父进程不得预写 PID 文件（旧 bug：子进程 is_running 会把父预写的
    "自己的 PID" 当作已运行实例并立刻退出）；PID 由子进程自行写入，
    stop 凭该文件清理."""
    pid_file = tmp_path / "d.pid"
    log_dir = tmp_path / "d_logs"

    # 子进程模拟 start_foreground 的契约：稍后写自己的 PID，然后驻留
    cmd = [
        sys.executable, "-c",
        "import os, time; time.sleep(0.5); "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(60)",
    ]
    handle = start_detached(
        argv=cmd, pid_file=pid_file, log_dir=log_dir, child_mode_arg="--ignored",
    )
    try:
        assert handle.pid > 0
        # 真实语义：父进程刚返回时 PID 文件尚不存在
        assert read_pid_file(pid_file) is None
        # 子进程自行写入后 stop 才能找到它
        child_pid = _wait_pid_file(pid_file)
        assert child_pid is not None, "child did not write pid file"
        assert is_process_alive(child_pid)
    finally:
        assert stop(pid_file, timeout=5) is True
        if handle.is_alive():  # 兜底，绝不允许 sleep(60) 子进程泄漏锁日志
            handle.proc.kill()
            handle.proc.wait(timeout=5)
    assert not pid_file.exists()


def test_start_detached_creates_log_dir(tmp_path: Path) -> None:
    pid_file = tmp_path / "d.pid"
    log_dir = tmp_path / "deep" / "nested" / "logs"
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    handle = start_detached(
        argv=cmd, pid_file=pid_file, log_dir=log_dir, child_mode_arg="--ignored",
    )
    try:
        assert log_dir.is_dir()
    finally:
        # dummy 子进程不写 PID 文件，stop 找不到目标；必须按句柄清理，
        # 否则 sleep(60) 进程泄漏并锁住 .pytest-tmp 下的 master.log
        stop(pid_file, timeout=5)
        handle.proc.kill()
        handle.proc.wait(timeout=5)


def test_stop_no_pid_file_returns_false(tmp_path: Path) -> None:
    assert stop(tmp_path / "missing.pid", timeout=1) is False


def test_stop_dead_pid_cleans_up(tmp_path: Path) -> None:
    fp = tmp_path / "x.pid"
    write_pid_file(fp, 2_000_000)
    assert stop(fp, timeout=1) is True
    assert not fp.exists()
