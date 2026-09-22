"""orchestration.queue: SQLite 任务队列.

封装 ``tasks`` 表的状态机、原子抢占、计数、重试与死信。
设计见 ``docs/orchestration-design.md`` §5（schema）和 §6.1-§6.3（算法）。

新架构 ``simulation server → gdr → etl`` 下:
    pending → gdr_processing → pending_etl → etl_processing → done
qf 阶段已删除；旧 qf_* 常量不再导出。
"""

from __future__ import annotations

from orchestration.queue.sqlite_queue import (
    STAGE_ETL,
    STAGE_GDR,
    STATE_DEAD,
    STATE_DONE,
    STATE_ETL_PROCESSING,
    STATE_GDR_PROCESSING,
    STATE_PENDING,
    STATE_PENDING_ETL,
    SQLiteQueue,
    Task,
)

__all__ = [
    "SQLiteQueue",
    "Task",
    "STAGE_GDR",
    "STAGE_ETL",
    "STATE_PENDING",
    "STATE_GDR_PROCESSING",
    "STATE_PENDING_ETL",
    "STATE_ETL_PROCESSING",
    "STATE_DONE",
    "STATE_DEAD",
]