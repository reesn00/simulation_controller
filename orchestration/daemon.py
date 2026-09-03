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
import time
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
STOP_SENTINEL_FILE = "STOP"


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
# STOP 哨兵文件（Windows 优雅停止主通道）
# ---------------------------------------------------------------------------


def stop_sentinel_path(pid_file: Path) -> Path:
    """STOP 哨兵文件路径：固定放 pid 文件同目录（即 data/）."""
    return Path(pid_file).parent / STOP_SENTINEL_FILE


def write_stop_sentinel(pid_file: Path) -> Path:
    """写 STOP 哨兵文件；master 轮询到即走优雅 shutdown.

    detach 子进程没有控制台（DETACHED_PROCESS），CTRL_BREAK_EVENT
    根本送达不了，taskkill /F 又是硬杀（master.shutdown 不会执行，
    SQLite 任务卡 running）。哨兵文件是跨进程模型都可靠的通道。
    """
    path = stop_sentinel_path(pid_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"stop requested at {time.strftime('%Y-%m-%dT%H:%M:%S')}\n",
        encoding="utf-8",
    )
    return path


def clear_stop_sentinel(pid_file: Path) -> None:
    """清除 STOP 哨兵文件（master 启动时做，避免上次残留立刻自杀）."""
    try:
        stop_sentinel_path(pid_file).unlink()
    except FileNotFoundError:
        pass


def is_stop_sentinel_present(pid_file: Path) -> bool:
    return stop_sentinel_path(pid_file).exists()


def _poll_stop_sentinel(pid_file: Path, stop_event: threading.Event) -> None:
    """后台线程：哨兵文件出现即 set stop_event（master 各阻塞点都会响应）."""
    while not stop_event.is_set():
        try:
            if is_stop_sentinel_present(pid_file):
                _log.info("daemon: STOP sentinel detected, setting stop_event")
                stop_event.set()
                return
        except OSError as exc:
            _log.warning("daemon: stop sentinel check failed: %s", exc)
        stop_event.wait(1.0)


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
    信号之外还监听 STOP 哨兵文件（pid 同目录 /STOP），覆盖 detach 子进程
    无控制台、CTRL_BREAK_EVENT 送达不了的 Windows 场景。
    """
    setup_logging(log_dir)
    # 上次 stop 的哨兵残留会让刚启动的 master 立刻自杀，先清掉
    clear_stop_sentinel(pid_file)
    write_pid_file(pid_file, os.getpid())
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    sentinel_thread = threading.Thread(
        target=_poll_stop_sentinel, args=(pid_file, stop_event),
        name="stop-sentinel", daemon=True,
    )
    sentinel_thread.start()
    _log.info("daemon: foreground start pid=%s", os.getpid())

    def cleanup(*_a):
        remove_pid_file(pid_file)
        clear_stop_sentinel(pid_file)

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
    """优雅停止 PID 文件里的 master；超时未退出就强杀.

    顺序（Windows / POSIX 一致）：
        1. 写 STOP 哨兵文件 → master 哨兵线程 set stop_event →
           各阻塞点中断 → ``master.shutdown`` 优雅收尾（最多 timeout 秒）
        2. 超时兜底：Windows ``taskkill /F /T`` 强杀整棵树；
           POSIX ``SIGTERM``（进程若已收到信号再补发无害，等价强杀）
        3. 进程退出后清哨兵 + PID 文件

    Returns: True 表示进程已退出；False 表示超时或 PID 文件不存在.
    """
    pid = read_pid_file(pid_file)
    if pid is None:
        return False
    if not is_process_alive(pid):
        remove_pid_file(pid_file)
        clear_stop_sentinel(pid_file)
        return True

    # 1. 优雅通道：哨兵文件（master 侧 1s 轮询到即开始 shutdown）
    write_stop_sentinel(pid_file)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            remove_pid_file(pid_file)
            clear_stop_sentinel(pid_file)
            return True
        time.sleep(0.2)

    # 2. 超时强杀兜底（master 卡在不可中断调用时的最后手段）
    _log.warning("daemon: graceful stop timed out for pid=%s, force killing", pid)
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, timeout=timeout,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log.warning("daemon: stop force kill failed for pid=%s: %s", pid, exc)

    # 3. 强杀后确认退出（不给满 timeout，3s 足够 taskkill 生效）
    deadline = time.monotonic() + min(timeout, 3.0)
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            break
        time.sleep(0.1)
    exited = not is_process_alive(pid)
    remove_pid_file(pid_file)
    clear_stop_sentinel(pid_file)
    return exited
