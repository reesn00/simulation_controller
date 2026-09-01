"""orchestration.queue: SQLite 任务队列.

封装 ``tasks`` 表的状态机、原子抢占、计数、重试与死信。
设计见 ``docs/orchestration-design.md`` §5（schema）和 §6.1-§6.3（算法）。
"""

from __future__ import annotations

from orchestration.queue.sqlite_queue import (
    STAGE_GDR,
    STAGE_QF,
    STATE_DEAD,
    STATE_DONE,
    STATE_GDR_PROCESSING,
    STATE_PENDING,
    STATE_PENDING_GDR,
    STATE_QF_PROCESSING,
    SQLiteQueue,
    Task,
)

__all__ = [
    "SQLiteQueue",
    "Task",
    "STAGE_QF",
    "STAGE_GDR",
    "STATE_PENDING",
    "STATE_QF_PROCESSING",
    "STATE_PENDING_GDR",
    "STATE_GDR_PROCESSING",
    "STATE_DONE",
    "STATE_DEAD",
]