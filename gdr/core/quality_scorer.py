"""P0-1.2: 训练价值评分器 (quality_scorer).

组合已有信号 (health_score / judge_score / intent_fulfillment / 修改率 /
tool_diversity / noise_level / depth) 输出 training_value_score ∈ [0,1]
和 complexity_tier ∈ {easy, medium, hard}.

设计要点:
  - 零 LLM, 纯组合函数; 任意输入缺失都用默认值兜底 (中性 0.5),
    保证新旧 metadata 字段都能算.
  - 七个组件分别归一化到 [0,1], 加权求和; 子权重从 cfg 读, 不写死,
    便于按数据特性调.
  - tier 分桶: score >= tier_easy_max → easy; >= tier_medium_max → medium;
    否则 hard. 默认阈值按高斯分布大致 33/33/33.
  - 写到 metadata.training_value_score + metadata.complexity_tier,
    供后续 batch 报告分桶 / 训练抽样使用.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from domain import Session

log = logging.getLogger(__name__)


def _safe_meta_get(meta: dict, key: str, default: Any = None) -> Any:
    if not isinstance(meta, dict):
        return default
    val = meta.get(key, default)
    return val if val is not None else default


def _avg_health_score(session: Session, meta: dict) -> float:
    """平均 health_score, 无 health 数据时返回中性 0.5."""
    # metadata.validation_summary 不含 health; health 来自 router.health_scores,
    # runner 没把它落盘. 这里按 message 数和总 toolcall 数做粗估.
    total = sum(len(m.blocks) for m in session.messages)
    if total == 0:
        return 0.5
    msg_count = max(1, sum(1 for m in session.messages if m.role == "assistant"))
    toolcall_count = 0
    failed_count = 0
    for m in session.messages:
        if m.role != "assistant":
            continue
        for b in m.blocks:
            btype = b.get("type", "") if isinstance(b, dict) else getattr(b, "type", "")
            if btype == "toolcall":
                toolcall_count += 1
            elif btype == "toolresult":
                state = b.get("state", "") if isinstance(b, dict) else getattr(b, "state", "")
                if state != "success":
                    failed_count += 1
    if toolcall_count == 0:
        return 0.7  # 无工具调用, 默认视为健康
    success_ratio = max(0.0, 1.0 - failed_count / max(1, toolcall_count + failed_count))
    return min(1.0, max(0.0, success_ratio))


def _judge_score_component(meta: dict) -> float:
    """judge_score 归一化: 0-10 → 0-1; judge 未跑 (无字段) → 0.5 中性."""
    # judge_low 的 session 把 score 落 metadata.judge_discard.score;
    # 通过的 session 不一定落分. 我们用 metadata.judge_relaxed.score
    # (有 relaxed 才写) 或 judge_unavailable_at (不可用, 中性 0.5).
    if isinstance(meta.get("judge_unavailable_at"), dict):
        return 0.5
    if isinstance(meta.get("judge_relaxed"), dict):
        s = meta["judge_relaxed"].get("score")
        if s is not None:
            return min(1.0, max(0.0, float(s) / 10.0))
    if isinstance(meta.get("judge_discard"), dict):
        s = meta["judge_discard"].get("score")
        if s is not None:
            return min(1.0, max(0.0, float(s) / 10.0))
    return 0.5


def _intent_fulfillment_component(meta: dict) -> float:
    """intent_fulfillment_score 归一化: {0,1,2} → {0.0, 0.5, 1.0}; 未跑 → 0.5."""
    s = _safe_meta_get(meta, "user_intent_fulfillment_score")
    if s is None:
        return 0.5
    try:
        n = int(s)
    except (TypeError, ValueError):
        return 0.5
    return {0: 0.0, 1: 0.5, 2: 1.0}.get(n, 0.5)


def _modified_ratio_component(meta: dict) -> float:
    """修改率: modified_blocks / total_blocks, 归一化到 [0,1].

    低修改率 = 原始轨迹已接近通过, 训练价值高 (1.0);
    高修改率 = 原始轨迹问题多, 训练价值低 (接近 0).
    用 1 - ratio 作为分量, 再 clamp.
    """
    validation = _safe_meta_get(meta, "validation_summary") or {}
    total = int(validation.get("total_blocks") or 0)
    modified = int(validation.get("modified_blocks") or 0)
    if total <= 0:
        return 0.5
    ratio = modified / total
    return max(0.0, min(1.0, 1.0 - ratio))


def _tool_diversity_component(session: Session) -> float:
    """tool_diversity: 去重 tool name 数 / 全部 toolcall 数, [0,1].

    高 = 工具多样化 (训练时学不同调用模式);
    低 = 单一工具反复用 (训练价值低, 可考虑折叠).
    """
    names: list[str] = []
    for m in session.messages:
        if m.role != "assistant":
            continue
        for b in m.blocks:
            btype = b.get("type", "") if isinstance(b, dict) else getattr(b, "type", "")
            if btype == "toolcall":
                name = b.get("name", "") if isinstance(b, dict) else getattr(b, "name", "")
                if name:
                    names.append(name)
    if not names:
        return 0.5  # 无工具调用, 中性
    unique = len(set(names))
    return min(1.0, unique / len(names))


def _noise_level_component(meta: dict) -> float:
    """noise_level: 1 - obs 噪声密度, [0,1].

    噪声多 = 训练价值低. metadata.validation_summary.failed_L1 近似噪声计数;
    fallback: 缺失即中性.
    """
    validation = _safe_meta_get(meta, "validation_summary") or {}
    failed_l1 = int(validation.get("failed_L1") or 0)
    failed_l3 = int(validation.get("failed_L3") or 0)
    total = int(validation.get("total_blocks") or 0)
    if total <= 0:
        return 0.5
    noise = (failed_l1 + failed_l3) / total
    return max(0.0, min(1.0, 1.0 - noise))


def _depth_component(session: Session) -> float:
    """depth: 对话轮数归一化, [0,1]; 太浅 < 2 轮 / 太深 > 30 轮都视为低价值.

    SFT 数据理想深度 4~20 轮 (medium 偏多). 用三角函数把 4~20 映射到 ~1.0,
    < 2 / > 30 衰减.
    """
    msg_count = len(session.messages)
    if msg_count <= 0:
        return 0.0
    # 三角峰在 12, 半宽 8
    score = max(0.0, 1.0 - abs(msg_count - 12) / 12.0)
    return max(0.0, min(1.0, score))


def compute_quality_score(session: Session, cfg: Any) -> dict:
    """计算 training_value_score + complexity_tier, 写 metadata 并返回 dict.

    字段:
      training_value_score    ∈ [0,1], 加权求和, 越高越值得进训练集
      complexity_tier         ∈ {"easy", "medium", "hard"}
      components              七维分量 (调试 / 抽样解释用)
    """
    if not getattr(cfg, "enable_quality_scorer", True):
        return {}
    meta = session.metadata or {}

    components = {
        "health": _avg_health_score(session, meta),
        "judge": _judge_score_component(meta),
        "intent": _intent_fulfillment_component(meta),
        "modified": _modified_ratio_component(meta),
        "diversity": _tool_diversity_component(session),
        "noise": _noise_level_component(meta),
        "depth": _depth_component(session),
    }

    w = {
        "health": float(getattr(cfg, "quality_scorer_weight_health", 0.25)),
        "judge": float(getattr(cfg, "quality_scorer_weight_judge", 0.25)),
        "intent": float(getattr(cfg, "quality_scorer_weight_intent", 0.20)),
        "modified": float(getattr(cfg, "quality_scorer_weight_modified", 0.10)),
        "diversity": float(getattr(cfg, "quality_scorer_weight_diversity", 0.10)),
        "noise": float(getattr(cfg, "quality_scorer_weight_noise", 0.05)),
        "depth": float(getattr(cfg, "quality_scorer_weight_depth", 0.05)),
    }
    # 权重归一化 (用户可调成任意正数; 总和不强制 1)
    w_sum = sum(w.values())
    if w_sum <= 0:
        return {
            "training_value_score": 0.5,
            "complexity_tier": "medium",
            "components": components,
        }
    score = sum(components[k] * w[k] for k in components) / w_sum
    score = max(0.0, min(1.0, score))

    easy_max = float(getattr(cfg, "quality_scorer_tier_easy_max", 0.70))
    medium_max = float(getattr(cfg, "quality_scorer_tier_medium_max", 0.40))
    if score >= easy_max:
        tier = "easy"
    elif score >= medium_max:
        tier = "medium"
    else:
        tier = "hard"

    out = {
        "training_value_score": round(score, 4),
        "complexity_tier": tier,
        "components": {k: round(v, 4) for k, v in components.items()},
    }

    session.metadata = meta
    meta["training_value_score"] = out["training_value_score"]
    meta["complexity_tier"] = tier
    meta["quality_scorer_components"] = out["components"]
    log.debug(
        "quality_scorer: session=%s score=%.3f tier=%s components=%s",
        getattr(session, "session_id", "?"),
        out["training_value_score"], tier, out["components"],
    )
    return out


def compute_tier_distribution(sessions: list[Session]) -> dict[str, int]:
    """汇总一批 session 的 tier 分布 (供 batch report)."""
    out = {"easy": 0, "medium": 0, "hard": 0}
    for s in sessions:
        if s is None:
            continue
        tier = (s.metadata or {}).get("complexity_tier")
        if tier in out:
            out[tier] += 1
    return out


# ============================================================
# 两层评分系统: [0,1]→1-5 映射 + 子分拆分
# (方案 trajectory-scoring-two-layer.md §2.2 维度2)
# ============================================================

def map_to_1_5(score: float, tier: str = "medium") -> int:
    """[0,1] 连续分 → 1-5 整数分 (linear_tier 策略).

    基础线性映射 round(1 + score*4), 再按 tier 校准:
      easy   → max(base, 4)  (高质量不低于 4)
      hard   → min(base, 3)  (低质量不高于 3)
      medium → base
    """
    base = round(1.0 + max(0.0, min(1.0, float(score))) * 4.0)
    base = max(1, min(5, base))
    if tier == "easy":
        return max(base, 4)
    if tier == "hard":
        return min(base, 3)
    return base


def _executability_subscore(session: Session) -> int:
    """可执行性子分: 有配对 toolresult 且 state=success 的 toolcall 比例 → 1-5."""
    toolcall_count = 0
    success_count = 0
    all_blocks: list[tuple[str, str, str]] = []
    for msg in session.messages:
        if msg.role != "assistant":
            continue
        for blk in msg.blocks:
            btype = blk.get("type", "") if isinstance(blk, dict) else getattr(blk, "type", "")
            bid = blk.get("id", "") if isinstance(blk, dict) else getattr(blk, "id", "")
            bstate = blk.get("state", "") if isinstance(blk, dict) else getattr(blk, "state", "")
            all_blocks.append((btype, bid, bstate))
    for i, (bt, bid, _bs) in enumerate(all_blocks):
        if bt != "toolcall":
            continue
        toolcall_count += 1
        for j in range(i + 1, len(all_blocks)):
            nbt, nbid, nbs = all_blocks[j]
            if nbt not in ("toolcall", "toolresult"):
                break
            if nbt == "toolresult" and nbid == bid:
                if nbs == "success":
                    success_count += 1
                break
    if toolcall_count == 0:
        return 4
    ratio = success_count / toolcall_count
    return max(1, min(5, round(1 + ratio * 4)))


def _action_obs_alignment_subscore(session: Session) -> int:
    """action-obs 对齐子分: 5 - min(断裂点数, 4)."""
    from validators.l4_trajectory_compare import check_alignment
    breakpoints = check_alignment(session)
    return max(1, 5 - min(len(breakpoints), 4))


def _result_quality_subscore(session: Session, meta: dict) -> int:
    """结果质量子分: intent_fulfillment {0,1,2}→{3,4,5} + claims 命中."""
    fulfillment = _safe_meta_get(meta, "user_intent_fulfillment_score")
    base = {0: 3, 1: 4, 2: 5}.get(int(fulfillment) if fulfillment is not None else None, 3)
    validation = _safe_meta_get(meta, "validation_summary") or {}
    total = int(validation.get("total_blocks") or 0)
    failed_l1 = int(validation.get("failed_L1") or 0)
    if total > 0 and failed_l1 == 0:
        base = max(base, 4)
    return max(1, min(5, base))


def _language_subscore(session: Session, meta: dict) -> int:
    """语言/格式子分: L1/L3 通过率 + meta_tag 无污染 → 1-5."""
    validation = _safe_meta_get(meta, "validation_summary") or {}
    total = int(validation.get("total_blocks") or 0)
    failed_l1 = int(validation.get("failed_L1") or 0)
    failed_l3 = int(validation.get("failed_L3") or 0)
    if total <= 0:
        return 4
    pass_ratio = 1.0 - (failed_l1 + failed_l3) / (2 * total)
    pass_ratio = max(0.0, min(1.0, pass_ratio))
    score = round(1 + pass_ratio * 4)
    contamination = _safe_meta_get(meta, "meta_tag_contamination") or {}
    if isinstance(contamination, dict) and contamination.get("has_meta_tag"):
        score = max(1, score - 1)
    return max(1, min(5, score))


def compute_subscores(session: Session, cfg: Any) -> dict[str, int]:
    """计算四维子分 (1-5 制), 供独立式绝对质量分使用."""
    meta = session.metadata or {}
    return {
        "executability": _executability_subscore(session),
        "action_obs_alignment": _action_obs_alignment_subscore(session),
        "result_quality": _result_quality_subscore(session, meta),
        "language": _language_subscore(session, meta),
    }