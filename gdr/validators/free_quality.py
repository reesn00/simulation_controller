"""独立式绝对质量分 (方案 trajectory-scoring-two-layer.md §2.2 维度2).

整合红线合规 + 1-5 绝对质量分 + 子分门槛, 作为训练集最终准入门控.
零 LLM (组合函数); 红线 LLM 复核由 redline.check 内部按 cfg 决定.
"""
from __future__ import annotations

import logging
from typing import Any

from domain import Session, TrajectoryFreeResult, RedlineResult, AbsoluteQuality

log = logging.getLogger(__name__)


def evaluate(session: Session, cfg: Any) -> TrajectoryFreeResult:
    """独立式评分主入口.

    Returns:
        TrajectoryFreeResult: decision ∈ {accept, reject, resample}.
            - 红线违规 → reject (一票否决)
            - 总分 < min_score → reject
            - 子分门槛未达 → reject
            - 否则 → accept
    """
    traj_id = getattr(session, "session_id", "?")

    redline = _check_redline(session, cfg)
    quality = _compute_absolute_quality(session, cfg)

    decision = _decide(redline, quality, cfg)

    result = TrajectoryFreeResult(
        traj_id=traj_id,
        redline=redline,
        absolute_quality=quality,
        decision=decision,
    )
    log.debug(
        "free_quality: traj=%s redline=%s score=%d subscores=%s decision=%s",
        traj_id, redline.violation, quality.score, quality.subscores, decision,
    )
    return result


def _check_redline(session: Session, cfg: Any) -> RedlineResult:
    if not getattr(cfg, "enable_redline", True):
        return RedlineResult(violation=False, labels=[])
    from validators.redline import check
    return check(session, cfg)


def _compute_absolute_quality(session: Session, cfg: Any) -> AbsoluteQuality:
    from core.quality_scorer import (
        compute_quality_score,
        map_to_1_5,
        compute_subscores,
    )
    qs = compute_quality_score(session, cfg)
    score_01 = float(qs.get("training_value_score", 0.5))
    tier = qs.get("complexity_tier", "medium")
    overall_score = map_to_1_5(score_01, tier)
    subscores = compute_subscores(session, cfg)

    fail_reasons: list[str] = []
    min_score = int(getattr(cfg, "absolute_quality_min_score", 4))
    min_exec = int(getattr(cfg, "absolute_quality_min_subscore_executability", 4))
    min_action_obs = int(getattr(cfg, "absolute_quality_min_subscore_action_obs", 4))

    if overall_score < min_score:
        fail_reasons.append(f"总分 {overall_score} < {min_score}")
    if subscores.get("executability", 0) < min_exec:
        fail_reasons.append(f"executability {subscores['executability']} < {min_exec}")
    if subscores.get("action_obs_alignment", 0) < min_action_obs:
        fail_reasons.append(f"action_obs_alignment {subscores['action_obs_alignment']} < {min_action_obs}")

    return AbsoluteQuality(
        score=overall_score,
        subscores=subscores,
        fail_reasons=fail_reasons,
    )


def _decide(
    redline: RedlineResult,
    quality: AbsoluteQuality,
    cfg: Any,
) -> str:
    if redline.violation:
        return "reject"
    if quality.fail_reasons:
        if quality.score <= 2:
            return "resample"
        return "reject"
    return "accept"
