"""fail-closed 白名单私有镜像。

``scout/fail_closed.py`` 用到的「采集/仲裁层主动 fail-closed 口径」真源
在 :mod:`trajectory_pipeline.executor.arbitration.L5_FAIL_CLOSED_NOTES` 与
:mod:`trajectory_pipeline.executor.review_queue.AUTO_REVIEW_NOTES`。

设计动机:
    scout 子包刻意不 import executor 顶层(契约 ``test_scout_contract`` 盯着),
    所以白名单镜像必须落在 evaluation 包内、由 scout 引用、且**不**写字面量。
    本模块就是那个镜像器 —— 唯二 ``from trajectory_pipeline.executor.*``
    的入口,契约 ``test_shared_literal_sync`` 锁源唯一性。
"""

from __future__ import annotations

from trajectory_pipeline.executor.arbitration import L5_FAIL_CLOSED_NOTES
from trajectory_pipeline.executor.review_queue import AUTO_REVIEW_NOTES


def is_fail_closed_note(note: str) -> bool:
    """note 是不是采集/仲裁层主动 fail-closed 的口径。

    返回 ``True`` 的样本在 scout 阶段标 ``not_run`` —— 不走四维评分,
    也**不**进 quarantine(quarantine 是缺件,not_run 是主动放弃)。
    """
    return note in L5_FAIL_CLOSED_NOTES or note in AUTO_REVIEW_NOTES


__all__ = [
    "is_fail_closed_note",
    "L5_FAIL_CLOSED_NOTES",
    "AUTO_REVIEW_NOTES",
]
