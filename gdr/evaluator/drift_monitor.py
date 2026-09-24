"""漂移监控 (方案 trajectory-scoring-two-layer.md §6 第三步).

对金标集中固定锚点轨迹随每批评审, 监控锚点分稳定性.
漂移 > threshold (默认 0.3, 比文本场景收紧) 即触发告警.
"""
from __future__ import annotations

import logging
from typing import Any

from domain import AnchorScoreRecord, DriftReport

log = logging.getLogger(__name__)


def monitor(
    anchor_scores: list[AnchorScoreRecord],
    baseline: dict[str, int],
    threshold: float = 0.3,
    batch_id: str = "",
) -> DriftReport:
    """对比当前批次锚点分与基线分, 计算漂移.

    Args:
        anchor_scores: 当前批次各锚点的评分记录.
        baseline: anchor_id → 基线分 (1-5).
        threshold: 漂移告警阈值 (默认 0.3).
        batch_id: 当前批次标识.

    Returns:
        DriftReport: max_drift / mean_drift / triggered.
    """
    records_with_delta: list[AnchorScoreRecord] = []
    deltas: list[float] = []
    for rec in anchor_scores:
        base_score = baseline.get(rec.anchor_id)
        if base_score is None:
            delta = 0.0
        else:
            delta = abs(float(rec.score) - float(base_score))
        deltas.append(delta)
        records_with_delta.append(AnchorScoreRecord(
            anchor_id=rec.anchor_id,
            batch_id=batch_id,
            score=rec.score,
            delta_from_baseline=round(delta, 4),
            timestamp=rec.timestamp,
        ))

    max_drift = max(deltas) if deltas else 0.0
    mean_drift = sum(deltas) / len(deltas) if deltas else 0.0
    triggered = max_drift > threshold

    report = DriftReport(
        batch_id=batch_id,
        anchor_count=len(anchor_scores),
        max_drift=round(max_drift, 4),
        mean_drift=round(mean_drift, 4),
        threshold=threshold,
        triggered=triggered,
        records=records_with_delta,
    )
    if triggered:
        log.warning(
            "drift monitor: batch=%s max_drift=%.3f > threshold=%.3f; action recommended",
            batch_id, max_drift, threshold,
        )
    else:
        log.info(
            "drift monitor: batch=%s max_drift=%.3f <= threshold=%.3f; stable",
            batch_id, max_drift, threshold,
        )
    return report


def run_drift_check(
    cfg: Any,
    current_scores: list[AnchorScoreRecord],
    batch_id: str = "",
) -> DriftReport | None:
    """便捷入口: 从 cfg 读配置, 加载基线, 执行漂移监控."""
    if not getattr(cfg, "golden_set_enabled", False):
        return None
    from evaluator.golden_set import load_baseline
    baseline_path = Path(str(getattr(cfg, "golden_set_path", ""))) / "baseline.json"
    baseline = load_baseline(baseline_path)
    threshold = float(getattr(cfg, "golden_set_drift_threshold", 0.3))
    return monitor(current_scores, baseline, threshold=threshold, batch_id=batch_id)


from pathlib import Path  # noqa: E402
