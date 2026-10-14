"""scout:在 P2 与黄金集之间加一道 rubric 评分门禁。

设计来源:``docs/设计方案/09-rubric-gate改造方案v04-实施细节.md``
"""

from trajectory_pipeline.evaluation.scout.types import (
    DimensionScore,
    ScoutRecord,
    ScoutStatus,
    ScoutTag,
)
from trajectory_pipeline.evaluation.scout.policy import classify, tags_for

__all__ = [
    "ScoutStatus",
    "ScoutTag",
    "DimensionScore",
    "ScoutRecord",
    "classify", "tags_for",
]
