"""orchestration.workers.base_worker: 通用 pull-process-mark 循环 + 重试/dead 逻辑.

设计见 ``docs/orchestration-design.md`` §6（worker 通用行为）。

子类只需实现：
    * ``stage``: STAGE_QF 或 STAGE_GDR
    * ``pull()``: 从队列拉任务
    * ``process(task) -> Path``: 处理单个任务，返回输出路径
    * ``mark_done(task, output)``: 标记完成

主循环 ``run_forever(stop_event)``：每轮 pull → process → mark；处理
异常走 ``queue.mark_failed(stage=...)``，attempts 超 max 时入 dead。
"""

from __future__ import annotations

import logging
import threading
import traceback
from abc import ABC, abstractmethod
from pathlib import Path

from orchestration.queue import (
    STATE_DEAD,
    STAGE_GDR,
    STAGE_QF,
    SQLiteQueue,
    Task,
)


_log = logging.getLogger(__name__)


class BaseWorker(ABC):
    """qf / gdr worker 的通用基类."""

    @property
    @abstractmethod
    def stage(self) -> str:
        """返回 ``STAGE_QF`` 或 ``STAGE_GDR``."""

    @abstractmethod
    def pull(self) -> list[Task]:
        """从 SQLite 队列拉任务；返回 ``[]`` 表示当前无活可干."""

    @abstractmethod
    def process(self, task: Task) -> Path:
        """处理单个 task，返回主产物路径."""

    @abstractmethod
    def mark_done(self, task: Task, output: Path) -> None:
        """标记 task 完成（state 转下一阶段 + 记录 output 路径）."""

    def __init__(
        self,
        *,
        queue: SQLiteQueue,
        worker_id: str,
        n: int = 1,
        poll_seconds: float = 2.0,
    ) -> None:
        if self.stage not in (STAGE_QF, STAGE_GDR):
            raise ValueError(f"invalid stage: {self.stage!r}")
        self._queue = queue
        self._worker_id = worker_id
        self._n = max(1, int(n))
        self._poll_seconds = float(poll_seconds)

    # ------------------------------------------------------------------
    # 失败处理
    # ------------------------------------------------------------------

    def _handle_failure(self, task: Task, exc: BaseException) -> None:
        msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}"
        new_state = self._queue.mark_failed(
            task.id, stage=self.stage, error_msg=msg,
        )
        if new_state == STATE_DEAD:
            _log.error(
                "[%s worker %s] task %d dead after %d retries: %s",
                self.stage, self._worker_id, task.id,
                self._queue._max_retry_qf if self.stage == STAGE_QF  # noqa: SLF001
                else self._queue._max_retry_gdr,                         # noqa: SLF001
                exc,
            )

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run_once(self) -> int:
        """跑一轮：pull → process → mark；返回处理成功的 task 数."""
        tasks = self.pull()
        if not tasks:
            return 0
        success = 0
        for task in tasks:
            try:
                output = self.process(task)
            except Exception as exc:
                self._handle_failure(task, exc)
                continue
            try:
                self.mark_done(task, output)
            except Exception as exc:
                # mark_done 自身失败（极少见，例如 SQLite 不可用）→ 记失败
                self._handle_failure(task, exc)
                continue
            success += 1
        return success

    def run_forever(self, stop_event: threading.Event) -> None:
        """阻塞主循环；``stop_event`` 设置后退出（最多延迟 ``poll_seconds``）."""
        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                _log.exception("[%s worker %s] run_once failed", self.stage, self._worker_id)
            stop_event.wait(self._poll_seconds)