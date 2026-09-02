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

from simulate_serve.infrastructure.trajectory_archiver import (
    sanitize_filename_part,
)

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
        #: 上一轮 run_once 拉到的 task 数（含最终失败的）；退避判活用。
        self._last_pulled = 0

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
    # 产物命名 (#9: 可追溯性)
    # ------------------------------------------------------------------

    def _output_name(self, task: Task, session_id: str, *, suffix: str) -> str:
        """``{task_id}__{session_id}{suffix}.json``；映射缺失时回退旧命名.

        task_id 从 run_tasks 表按 ``task.run_id``（watcher 从 trajectory 文件
        名解析的 sanitize 形态）反查。查不到（旧库、手工放入的 replay
        trajectory、producer 未跑完的残留）时保持 ``{session_id}{suffix}.json``
        旧文件名，行为向后兼容。
        """
        task_id: str | None = None
        try:
            task_id = self._queue.lookup_task_id(task.run_id)
        except Exception:
            _log.warning(
                "[%s worker %s] lookup_task_id failed for run %s; fallback naming",
                self.stage, self._worker_id, task.run_id,
            )
        safe_task = sanitize_filename_part(task_id) if task_id else ""
        prefix = f"{safe_task}__" if safe_task else ""
        return f"{prefix}{session_id}{suffix}.json"

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run_once(self) -> int:
        """跑一轮：pull → process → mark；返回处理成功的 task 数.

        副作用：把本轮拉到的 task 数（含后续失败的）记入 ``_last_pulled``，
        供 ``run_forever`` 区分"真空闲"与"有活但失败"。
        """
        tasks = self.pull()
        self._last_pulled = len(tasks)
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

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        max_poll_seconds: float = 0.0,
    ) -> None:
        """阻塞主循环；``stop_event`` 设置后退出（最多延迟当前等待间隔）.

        空闲退避 (#7): ``max_poll_seconds > 0`` 时，连续**真正空闲**（pull
        不到任何 task）的轮次让等待从 ``poll_seconds`` 指数翻倍、封顶
        ``max_poll_seconds``；拉到任务（哪怕处理失败）立即复位。默认 0 =
        关闭退避，保持恒定 ``poll_seconds``（与旧行为一致，短超时的测试/
        低延迟场景不受影响；master 生产路径经配置显式开启）。
        """
        idle_rounds = 0
        while not stop_event.is_set():
            self._last_pulled = 0
            processed = 0
            try:
                processed = self.run_once()
            except Exception:
                _log.exception("[%s worker %s] run_once failed", self.stage, self._worker_id)
            if processed or self._last_pulled:
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
            stop_event.wait(wait)