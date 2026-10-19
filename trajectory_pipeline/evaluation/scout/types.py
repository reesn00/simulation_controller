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


#: 聚合期决策留痕。一行 = 一个决策(打 tag / 选 score / 合并 evidence /
#: 缺 unit 标记)。给人工复查与回归。
#: ``unit`` 是做出该决策的 unit sample_id(``-`` 表示任务级、非某 unit)；
#: ``action`` 是机器可读动作(``"min_score"`` / ``"merge_evidence"`` /
#: ``"missing_unit"`` / ``"veto"`` 等)；``reason`` 是人类可读解释。
class AggregationLogEntry(TypedDict, total=False):
    unit: str
    action: str
    reason: str


class TrajectoryRecord(TypedDict):
    """per-Run 聚合后的 scout 判定结果——给 LS 上传用(per-Run 完整任务)。

    与 :class:`ScoutRecord` 的差异:``ScoutRecord`` 描述**单 unit**(P2 样本);
    ``TrajectoryRecord`` 描述**整条任务**(同 ``(task_id, run_id)`` 的 N 个
    unit 聚合)。LS 上传走 ``TrajectoryRecord``(D14.5)。

    设计基线:见 ``docs/设计方案/14-完整任务视图与LS上传.md`` §4.2-§4.3。
    聚合策略:**min**——四维各自取最差 unit 的 score,任一 unit 站错整条
    轨迹站错。任一维 ``score=None`` 短路到 ``undetermined``。
    """

    trajectory_id: str  # ``<task_id>__<run_id>``
    task_id: str
    run_id: str
    status: ScoutStatus
    scores: list[DimensionScore]
    #: 任一 unit 的 tag 并集(``set`` 语义,顺序不保证)。
    tags: list[ScoutTag]
    #: 各 unit 的 sample_id(per-unit P3 反查入口)。
    unit_outcomes: list[str]
    #: 合并后的 evidence_supplement。键冲突时取第一个非空,决策记
    #: ``aggregation_log``。
    evidence_supplement: dict[str, Any]
    #: 聚合期所有决策的留痕(打 tag / 选 score / 合并 evidence / 缺 unit)。
    aggregation_log: list[AggregationLogEntry]


__all__ = [
    "ScoutStatus",
    "ScoutTag",
    "DimensionScore",
    "ScoutRecord",
    "AggregationLogEntry",
    "TrajectoryRecord",
]
