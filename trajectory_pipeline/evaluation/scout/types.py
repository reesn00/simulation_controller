"""scout 的纯类型声明。

不依赖任何运行时模块,只有 ``TypedDict`` 与 ``Literal``。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

#: 三态 —— 与 :data:`trajectory_pipeline.rationale.gates.GateStatus` 同款口径。
#: ``scored`` = 四维都有 ``score: int``;``undetermined`` = 任何一维
#: ``score is None``(采集/门禁问题,不是内容差);``not_run`` = fail-closed
#: 触发,样本不参与评分。
ScoutStatus = Literal["scored", "undetermined", "not_run"]

#: scout 阶段的负向标签。``negative_candidates.jsonl`` 每条至少带一个。
ScoutTag = Literal["d2_failed", "d4_failed", "d5_failed", "d6_failed", "veto"]


class DimensionScore(TypedDict):
    """四维里的一维。"""

    dimension: Literal["d2", "d4", "d5", "d6"]
    #: ``None`` 表示该维未判出来 —— 走 quarantine 而不是负例。
    score: int | None
    is_veto: bool
    reasons: list[str]


class ScoutRecord(TypedDict):
    """scout 对一条 P2 样本的判定结果。

    ⚠️ ``scores`` 始终是 4 条(``d2`` / ``d4`` / ``d5`` / ``d6`` 全到齐);
    缺一维的状态通过 ``score is None`` 表达,而不是少一条。
    """

    sample_id: str
    task_id: str
    run_id: str
    status: ScoutStatus
    scores: list[DimensionScore]
    #: 负向 tag 集合。空 = 无负向信号(候选可能为 positive)。
    tags: list[ScoutTag]
    #: 可选 L5 证据补充(L5 = ``BrowserProbeResult.l5_outcome``)。
    #: 缺件留空 dict —— **不算缺件**(rubric 不变量:缺件 ≠ 扣分)。
    evidence_supplement: dict[str, Any]


__all__ = [
    "ScoutStatus",
    "ScoutTag",
    "DimensionScore",
    "ScoutRecord",
]
