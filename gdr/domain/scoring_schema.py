"""两层评分系统结果 schema (方案 trajectory-scoring-two-layer.md).

第一层对比式 (Reference-based): TrajectoryCompareResult
第二层独立式 (Reference-free): TrajectoryFreeResult

两层均零隐式 LLM 依赖; LLM 调用由各自 validator 内部按 cfg 开关决定.
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field, ConfigDict


# ============================================================
# 第一层: 对比式评分 (Reference-based)
# ============================================================

class FidelityVerdict(BaseModel):
    """任务意图保真度."""
    model_config = ConfigDict(extra="allow")
    verdict: Literal["faithful", "degraded"]
    lost_elements: list[str] = Field(default_factory=list)
    preserved_core: list[str] = Field(default_factory=list)


class DiffItem(BaseModel):
    """结构化 diff 单项."""
    model_config = ConfigDict(extra="allow")
    step_range: str
    type: Literal["required_change", "incidental_change", "regression"]
    note: str


class InstructionAdherence(BaseModel):
    """指令遵循度."""
    model_config = ConfigDict(extra="allow")
    diff_summary: list[DiffItem] = Field(default_factory=list)
    score: Literal["pass", "review", "fail"]


class Breakpoint(BaseModel):
    """action-observation 对齐断裂点."""
    model_config = ConfigDict(extra="allow")
    step: int
    issue: str


class TrajectoryCompareResult(BaseModel):
    """轨迹级对比式评分结果 (原始轨迹 vs 修改后轨迹)."""
    model_config = ConfigDict(extra="allow")
    pair_id: str
    fidelity: FidelityVerdict
    instruction_adherence: InstructionAdherence
    coherence_delta: Literal["improved", "unchanged", "degraded"]
    breakpoints: list[Breakpoint] = Field(default_factory=list)
    overall: Literal["pass", "fail"]


# ============================================================
# 第二层: 独立式评分 (Reference-free)
# ============================================================

RedlineType = Literal[
    "piracy", "privacy", "prompt_injection", "tos_violation", "sensitive_content",
]


class RedlineViolation(BaseModel):
    """单条红线违规."""
    model_config = ConfigDict(extra="allow")
    type: RedlineType
    step_location: int
    evidence: str


class RedlineResult(BaseModel):
    """红线合规结果 (零违规才放行)."""
    model_config = ConfigDict(extra="allow")
    violation: bool
    labels: list[RedlineViolation] = Field(default_factory=list)


QualitySubscoreKey = Literal[
    "executability", "action_obs_alignment", "result_quality", "language",
]


class AbsoluteQuality(BaseModel):
    """绝对质量分 (1-5 分制)."""
    model_config = ConfigDict(extra="allow")
    score: int  # 1-5
    subscores: dict[QualitySubscoreKey, int] = Field(default_factory=dict)
    fail_reasons: list[str] = Field(default_factory=list)


class TrajectoryFreeResult(BaseModel):
    """轨迹级独立式评分结果 (只看修改后轨迹)."""
    model_config = ConfigDict(extra="allow")
    traj_id: str
    redline: RedlineResult
    absolute_quality: AbsoluteQuality
    decision: Literal["accept", "reject", "resample"]


# ============================================================
# 漂移监控
# ============================================================

class AnchorScoreRecord(BaseModel):
    """单条锚点轨迹的评分记录 (漂移监控用)."""
    model_config = ConfigDict(extra="allow")
    anchor_id: str
    batch_id: str
    score: int  # 1-5
    delta_from_baseline: float = 0.0
    timestamp: str = ""


class DriftReport(BaseModel):
    """漂移监控报告."""
    model_config = ConfigDict(extra="allow")
    batch_id: str
    anchor_count: int
    max_drift: float
    mean_drift: float
    threshold: float
    triggered: bool
    records: list[AnchorScoreRecord] = Field(default_factory=list)
