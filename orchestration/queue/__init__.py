"""orchestration.queue: SQLite 任务队列 (2026-09-22 ST-2 重构).

新架构 ``simulation server → gdr → etl`` 下:
    pending → simulate → gdr → etl → done
(失败终态走 dead; 评分低走 audited, 不进 dead)

封装 ``tasks`` 表的状态机推进与产物路径追踪; 公开 API 严格按
``docs/设计方案/pipeline-contracts.md`` §2.
"""

from __future__ import annotations

from orchestration.queue.sqlite_queue import (
    PHASE_AUDITED,
    PHASE_DEAD,
    PHASE_DONE,
    PHASE_ETL,
    PHASE_GDR,
    PHASE_PENDING,
    PHASE_SIMULATE,
    SQLiteQueue,
    STAGE_ETL,
    STAGE_GDR,
    STAGE_SIMULATE,
    Task,
    TaskAlreadyTerminal,
)


PHASE_PENDING = "pending"
PHASE_SIMULATE = "simulate"
PHASE_GDR = "gdr"
PHASE_ETL = "etl"
PHASE_DONE = "done"
PHASE_DEAD = "dead"
PHASE_AUDITED = "audited"

ALL_PHASES = frozenset({
    PHASE_PENDING, PHASE_SIMULATE, PHASE_GDR,
    PHASE_ETL, PHASE_DONE, PHASE_DEAD, PHASE_AUDITED,
})
TERMINAL_PHASES = frozenset({PHASE_DONE, PHASE_DEAD, PHASE_AUDITED})


__all__ = [
    "SQLiteQueue",
    "Task",
    "TaskAlreadyTerminal",
    "PHASE_PENDING",
    "PHASE_SIMULATE",
    "PHASE_GDR",
    "PHASE_ETL",
    "PHASE_DONE",
    "PHASE_DEAD",
    "PHASE_AUDITED",
    "ALL_PHASES",
    "TERMINAL_PHASES",
    "STAGE_SIMULATE",
    "STAGE_GDR",
    "STAGE_ETL",
]
