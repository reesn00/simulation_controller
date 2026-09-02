"""orchestration.watcher: 轮询 trajectory 目录, 把新文件登记到 SQLite 队列.

设计见 ``docs/orchestration-design.md`` §6.5。

trajectory 文件名由 ``simulate_serve/infrastructure/trajectory_archiver.py:30-34``
控制：``"<safe_run>__<safe_session>.json"``，双下划线分隔 run 和 session。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from orchestration.queue import SQLiteQueue


_DOUBLE_UNDERSCORE = "__"


def parse_trajectory_filename(fp: Path) -> tuple[str, Optional[str]]:
    """解析 ``<safe_run>__<safe_session>.json``.

    Returns: ``(run_id, session_id or None)``.

    若文件名不含 ``__``，整 stem 作为 run_id，session_id 为 None。
    """
    stem = fp.stem
    if _DOUBLE_UNDERSCORE in stem:
        run_id, session_id = stem.split(_DOUBLE_UNDERSCORE, 1)
        return run_id, session_id or None
    return stem, None


class TrajectoryWatcher:
    """轮询 trajectory_dir → SQLiteQueue.insert.

    进程模型：单进程；可由 master 用 multiprocessing.spawn 起多个，
    每个绑定不同 batch_id。SQLite 写锁自动串行化多个 watcher 实例。
    """

    def __init__(
        self,
        *,
        trajectory_dir: Path,
        queue: SQLiteQueue,
        batch_id: int,
        poll_seconds: float = 2.0,
        dead_log_path: Optional[Path] = None,
    ) -> None:
        self._trajectory_dir = Path(trajectory_dir)
        self._queue = queue
        self._batch_id = int(batch_id)
        self._poll_seconds = float(poll_seconds)
        self._dead_log_path = Path(dead_log_path) if dead_log_path else None

    # ------------------------------------------------------------------
    # 单次扫描
    # ------------------------------------------------------------------

    def scan_once(self) -> dict[str, int]:
        """扫描 trajectory_dir 一次.

        Returns: ``{"registered", "skipped", "dead"}``
            - registered: 新登记数
            - skipped: 已存在跳过数（幂等）
            - dead: 登记失败的 dead 数（记到 dead_log）
        """
        result = {"registered": 0, "skipped": 0, "dead": 0}
        if not self._trajectory_dir.is_dir():
            return result

        for fp in sorted(self._trajectory_dir.glob("*.json")):
            run_id, session_id = parse_trajectory_filename(fp)
            try:
                _, inserted = self._queue.insert(
                    src_path=fp,
                    run_id=run_id,
                    session_id=session_id,
                    batch_id=self._batch_id,
                )
                if inserted:
                    result["registered"] += 1
                else:
                    result["skipped"] += 1
            except Exception as exc:
                self._log_dead(fp, exc)
                result["dead"] += 1
        return result

    def _log_dead(self, fp: Path, exc: Exception) -> None:
        msg = f"[watcher] dead: {fp} -> {type(exc).__name__}: {exc}\n"
        if self._dead_log_path is None:
            return
        self._dead_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._dead_log_path.open("a", encoding="utf-8") as f:
            f.write(msg)

    # ------------------------------------------------------------------
    # 阻塞主循环
    # ------------------------------------------------------------------

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        max_poll_seconds: float = 0.0,
    ) -> None:
        """阻塞轮询，每轮扫描一次；``stop_event`` 设置后退出.

        空闲退避 (#7): ``max_poll_seconds > 0`` 时，连续无新登记的轮次让等待
        从 ``poll_seconds`` 指数翻倍、封顶 ``max_poll_seconds``；一旦有登记
        立即复位。默认 0 = 关闭退避（与旧行为一致）。退避只延迟"新文件
        出现→登记"的最坏路径，master 本来就要等批终态，可接受。
        """
        idle_rounds = 0
        while not stop_event.is_set():
            registered = 0
            try:
                registered = self.scan_once().get("registered", 0)
            except Exception as exc:
                # 扫描层不应该整体崩溃；记 dead 后继续
                self._log_dead(Path("(scan_once)"), exc)
            if registered:
                idle_rounds = 0
            else:
                idle_rounds += 1
            if max_poll_seconds > 0 and idle_rounds:
                wait = min(
                    self._poll_seconds * (2 ** (idle_rounds - 1)),
                    max_poll_seconds,
                )
            else:
                wait = self._poll_seconds
            # wait 返回 True 表示 set 被调用，False 表示 timeout
            stop_event.wait(wait)