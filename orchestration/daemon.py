"""orchestration.daemon: PID file + signal + 日志重定向.

设计见 ``docs/orchestration-design.md`` §8、§11。

职责：
    * ``start_foreground(...)``: 当前进程跑 master（用于 dev / 调试）
    * ``start_detached(...)``: 子进程化（Windows: ``CREATE_NEW_PROCESS_GROUP`` + 父进程退出）
    * ``stop(pid_file)``: 发 ``CTRL_BREAK_EVENT`` 给子进程组（Windows） / ``SIGTERM``（Unix）
    * ``is_running(pid_file)``: 读 PID + 检查进程是否存活
    * ``setup_logging(log_dir)``: 把 root logger 重定向到 ``log_dir/master.log``

边界：
    * 不解释 config；只做进程生命周期 + logging
    * 不触碰 SQLite / tasks
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_PID_FILE = "orchestration.pid"
DEFAULT_LOG_DIR = "orchestration/logs"
DEFAULT_LOG_FILE = "master.log"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class DaemonError(RuntimeError):
    """daemon 启动 / 停止 / 状态读取错误."""


# ---------------------------------------------------------------------------
# PID 文件 + 进程检查
# ---------------------------------------------------------------------------


def read_pid_file(pid_file: Path) -> int | None:
    """读 PID 文件，返回 PID 或 None（不存在 / 损坏）."""
    pid_file = Path(pid_file)
    if not pid_file.exists():
        return None
    try:
        text = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    return int(text)


def write_pid_file(pid_file: Path, pid: int) -> None:
    """写 PID 文件；父目录自动创建."""
    pid_file = Path(pid_file)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(int(pid)), encoding="utf-8")


def remove_pid_file(pid_file: Path) -> None:
    pid_file = Path(pid_file)
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass


def is_process_alive(pid: int) -> bool:
    """检查进程是否存在（跨平台）."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Windows：用 tasklist 查 PID；失败 = 进程不存在
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return str(pid) in out.stdout
    # POSIX
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但无权
    return True


def is_running(pid_file: Path) -> tuple[bool, int | None]:
    """读 PID 文件 + 检查进程存活；返回 (alive, pid)."""
    pid = read_pid_file(pid_file)
    if pid is None:
        return False, None
    alive = is_process_alive(pid)
    return alive, pid if alive else None


# ---------------------------------------------------------------------------
# signal 绑定（在前台进程内）
# ---------------------------------------------------------------------------


def install_signal_handlers(stop_event: threading.Event) -> None:
    """绑定 SIGINT / SIGTERM（Unix）/ SIGBREAK（Windows） → stop_event.set()."""
    def handler(signum, frame):  # noqa: ARG001
        _log.info("daemon: received signal %s, setting stop_event", signum)
        stop_event.set()

    if sys.platform == "win32":
        # Windows：SIGBREAK 是 CTRL_BREAK_EVENT（发给子进程组）;
        # SIGINT 是 Ctrl+C（控制台前台）
        signal.signal(signal.SIGBREAK, handler)  # type: ignore[attr-defined]
        signal.signal(signal.SIGINT, handler)
    else:
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)


# ---------------------------------------------------------------------------
# 日志重定向
# ---------------------------------------------------------------------------


def setup_logging(
    log_dir: Path,
    *,
    log_file: str = DEFAULT_LOG_FILE,
    level: int = logging.INFO,
    also_stderr: bool = True,
) -> Path:
    """把 root logger 输出到 ``log_dir/<log_file>``.

    Returns: 实际写入的日志文件路径.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_file

    root = logging.getLogger()
    root.setLevel(level)
    # 清掉现有 handler（避免重复）
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if also_stderr:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    # 降噪: simulate_serve executor 轮询 QwenPaw 时 httpx 每个 GET 打一条
    # INFO（master.log 里 ~3 秒一条的心跳噪声即来源于此，而非 watcher 本身）。
    # 请求失败时 httpx 仍会在 WARNING+ 浮出，丢失的只是成功请求的行级记录。
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, level))

    return log_path


# ---------------------------------------------------------------------------
# 前台启动
# ---------------------------------------------------------------------------


@dataclass
class ForegroundHandle:
    """``start_foreground`` 返回的句柄：stop() 用于优雅停止."""

    stop_event: threading.Event
    pid_file: Path

    def stop(self, *, timeout: float = 10.0) -> None:
        self.stop_event.set()


def start_foreground(
    *,
    cfg_path: Path | None,
    pid_file: Path,
    log_dir: Path,
    run_callback: Callable[[threading.Event], None],
) -> ForegroundHandle:
    """在当前进程跑 ``run_callback(stop_event)``；绑定信号 + 写 PID + 日志.

    ``run_callback`` 收到 stop_event 后应优雅退出（典型为 ``Master.shutdown``）。
    """
    setup_logging(log_dir)
    write_pid_file(pid_file, os.getpid())
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    _log.info("daemon: foreground start pid=%s", os.getpid())

    def cleanup(*_a):
        remove_pid_file(pid_file)

    try:
        run_callback(stop_event)
    finally:
        cleanup()
        _log.info("daemon: foreground exit pid=%s", os.getpid())

    return ForegroundHandle(stop_event=stop_event, pid_file=pid_file)


# ---------------------------------------------------------------------------
# 后台（子进程化）启动
# ---------------------------------------------------------------------------


@dataclass
class DetachedHandle:
    """``start_detached`` 返回的句柄：含 PID + 子进程对象."""
    pid: int
    pid_file: Path
    proc: subprocess.Popen

    def is_alive(self) -> bool:
        return self.proc.poll() is None


def start_detached(
    *,
    argv: list[str] | None = None,
    cwd: Path | None = None,
    pid_file: Path,
    log_dir: Path,
    child_mode_arg: str = "--child",
) -> DetachedHandle:
    """把 ``argv`` 作为子进程启动；父进程立即返回.

    Windows：用 ``CREATE_NEW_PROCESS_GROUP`` 让子进程独立（后续 ``CTRL_BREAK_EVENT`` 可达）;
    POSIX：用 ``start_new_session=True``（setsid）.
    """
    argv = list(argv or sys.argv)
    if child_mode_arg not in argv:
        argv.append(child_mode_arg)

    pid_file = Path(pid_file)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / DEFAULT_LOG_FILE
    log_fh = log_path.open("a", encoding="utf-8")

    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fh,
        "stderr": subprocess.STDOUT,
        "cwd": str(cwd) if cwd else None,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP = 0x00000200
        # DETACHED_PROCESS = 0x00000008
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # type: ignore[assignment]
    else:
        kwargs["start_new_session"] = True  # type: ignore[assignment]

    proc = subprocess.Popen(argv, **kwargs)
    log_fh.close()
    # 注意：不要在这里写 PID 文件——子进程会经过 ``_cmd_start`` 的
    # ``is_running`` 检查，若父进程提前写入，子进程会把"自己的 PID"
    # 当成"已运行实例"并立刻退出。PID 文件由子进程在
    # ``start_foreground`` 中写入。
    _log.info("daemon: detached child pid=%s argv=%s", proc.pid, argv)
    return DetachedHandle(pid=proc.pid, pid_file=pid_file, proc=proc)


# ---------------------------------------------------------------------------
# 停止
# ---------------------------------------------------------------------------


def stop(pid_file: Path, *, timeout: float = 10.0) -> bool:
    """发信号给 PID 文件里的进程；超时未退出就 kill.

    Returns: True 表示进程已退出；False 表示超时或 PID 文件不存在.
    """
    pid = read_pid_file(pid_file)
    if pid is None:
        return False
    if not is_process_alive(pid):
        remove_pid_file(pid_file)
        return True

    try:
        if sys.platform == "win32":
            # Windows：向进程树发 CTRL_BREAK_EVENT；进程组内有效
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, timeout=timeout,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("daemon: stop signal failed for pid=%s: %s", pid, exc)

    # 等退出
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            remove_pid_file(pid_file)
            return True
        time.sleep(0.1)
    return False
