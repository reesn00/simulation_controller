"""scout 三件套 → per-Run 聚合(``trajectory_candidates.jsonl``)。

设计基线: ``docs/设计方案/14-完整任务视图与LS上传.md`` §4.2-§4.3。
聚合策略: **min**——四维各自取**最差 unit**的 score,任一 unit 站错
整条轨迹站错;任一维 ``score=None`` 短路到 ``undetermined``;任一
``is_veto=True`` 短路到任务级 ``is_veto=True``。

输入
----
- 三件套(per-unit): ``scout_dir/{positive,negative,quarantine}_candidates.jsonl``
  (``evaluation/scout/types.py::ScoutRecord``)
- 期望 unit 数(可选): ``search + N sites``——少于此数记 ``missing_unit`` 留痕

输出
----
- ``trajectory_candidates.jsonl``(per-Run): 每条 ``TrajectoryRecord``,
  主键 ``trajectory_id = <task_id>__<run_id>``
- ``trajectory_stats.json``:聚合后三态计数(positive / negative /
  quarantine / skip / missing_source),给报告侧读

纪律
----
- **不 import executor / assembler**——本模块只动 scout 三件套的内存
  dict,落盘路径走标准 ``Path.write_text`` 而非依赖 storage 模块(避免
  跨包耦合)。
- **fail-closed**:能力不可用(读不出 / 坏 JSON)返回 ``None``/ 进 skipped,
  **不猜**;且 ``None`` 不中断整批。
- **默认不覆盖**已存在的 ``trajectory_candidates.jsonl``(与
  ``cli build``/``cli task-view`` 同款纪律)——重建整批显式传
  ``overwrite=True``。
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from trajectory_pipeline.evaluation.ls_bridge.exporter import (
    BUCKET_FILES,
    read_scout_records,
)
from trajectory_pipeline.evaluation.scout.types import (
    AggregationLogEntry,
    DimensionScore,
    ScoutRecord,
    ScoutStatus,
    TrajectoryRecord,
)

log = logging.getLogger(__name__)

#: 任务级聚合产物文件名(D14.5)。ls_bridge.exporter 读它做 LS 上传主路径。
TRAJECTORY_BUCKET_FILE: str = "trajectory_candidates.jsonl"

#: 聚合后三态计数(报告侧读)。
TRAJECTORY_STATS_FILE: str = "trajectory_stats.json"

#: 四维固定顺序(min 策略按这个顺序产出聚合 scores 数组)。
DIMENSION_ORDER: tuple[str, ...] = ("d2", "d4", "d5", "d6")


# ── 纯函数:聚合 ────────────────────────────────────────────────


def _min_score(
    units: list[ScoutRecord],
    dimension: str,
    log_entries: list[AggregationLogEntry],
) -> tuple[int | None, bool, list[str], str | None]:
    """取 ``dimension`` 的最差 score(min 策略)。

    Args:
        units: 同一 (task_id, run_id) 的所有 unit 列表。
        dimension: ``d2``/``d4``/``d5``/``d6`` 之一。
        log_entries: 聚合期决策留痕(本函数追加 min_score 行)。

    Returns:
        ``(score, is_veto, reasons, picked_unit)``:
        - ``score``:任务级 score;任一 unit 该维 ``score=None`` 时短路
          返回 ``None``(任务级 ``undetermined``);
        - ``is_veto``:任一 unit 该维 ``is_veto=True`` 时为 ``True``(短路);
        - ``reasons``:挑中的最差 unit 的 reasons 列表(只挑一条,
          不合并——与 rubric 「任一 unit 站错整条轨迹站错」一致);
        - ``picked_unit``:被挑中的最差 unit sample_id;score=None 短路时为
          ``None``(全 None 没法挑)。
    """
    per_dim: list[DimensionScore] = []
    for u in units:
        for d in u.get("scores") or ():
            if d.get("dimension") == dimension:
                per_dim.append(d)
                break

    if not per_dim:
        # 全部 unit 都没这一维(理论不可能:scout 固定 4 维全到齐,但
        # 坏档可能少——记留痕,降级为 None)
        log_entries.append({
            "unit": "-",
            "action": "min_score",
            "reason": f"所有 unit 缺 {dimension} 维, 任务级 score=None",
        })
        return None, False, [], None

    # 短路 1:任一 unit 该维 score=None → 任务级 score=None
    nones = [d for d in per_dim if d.get("score") is None]
    if nones:
        bad_unit = units[per_dim.index(nones[0])]["sample_id"]
        log_entries.append({
            "unit": bad_unit,
            "action": "quarantine_short_circuit",
            "reason": (f"{dimension}: 任一 unit score=None, "
                       f"任务级 score=None (undetermined)"),
        })
        return None, False, [], bad_unit

    # 短路 2:任一 unit 该维 is_veto=True → 任务级 is_veto=True
    vetoes = [d for d in per_dim if d.get("is_veto")]
    if vetoes:
        bad_unit = units[per_dim.index(vetoes[0])]["sample_id"]
        log_entries.append({
            "unit": bad_unit,
            "action": "veto",
            "reason": f"{dimension}: 任一 unit is_veto=True, 任务级 is_veto=True",
        })
        # veto 时 score 也取最差(选 veto 那条);理由透传
        bad = vetoes[0]
        return int(bad["score"]), True, list(bad.get("reasons") or ()), bad_unit

    # 正常:min
    worst = min(per_dim, key=lambda d: int(d["score"]))
    worst_idx = per_dim.index(worst)
    bad_unit = units[worst_idx]["sample_id"]
    log_entries.append({
        "unit": bad_unit,
        "action": "min_score",
        "reason": (f"{dimension}: 任务级 score={worst['score']} 取自 "
                   f"最差 unit (min 策略)"),
    })
    return (
        int(worst["score"]),
        False,
        list(worst.get("reasons") or ()),
        bad_unit,
    )


def _aggregate_status(
    task_scores: list[DimensionScore],
    units: list[ScoutRecord],
) -> ScoutStatus:
    """由聚合后的 scores + unit status 推导任务级 status。

    规则(见方案 14 §4.3):
    - 全部维 ``score`` 都到齐(非 None)→ ``scored``
    - 任一维 ``score=None`` → ``undetermined``
    - 全部 unit ``status=not_run`` → ``not_run``
    - ``is_veto`` **不**影响 status——只在该维打 ``is_veto=True`` 标记,
      ``status`` 仍按 score 是否到齐判断(veto 的 score 仍是 int 不是 None)。
    """
    to_quarantine = any(d.get("score") is None for d in task_scores)
    to_skip = all(
        u.get("status") == "not_run" for u in units
    ) and len(units) > 0

    if to_quarantine:
        return "undetermined"
    if to_skip:
        return "not_run"
    # 兜底:四维 score 全 None(理论不可能——to_quarantine 已接住)
    if all(d.get("score") is None for d in task_scores):
        return "undetermined"
    return "scored"


def aggregate_to_trajectory(
    units: list[ScoutRecord],
    *,
    expected_unit_count: int | None = None,
) -> TrajectoryRecord:
    """把同 (task_id, run_id) 的 N 个 unit 聚合成一条 ``TrajectoryRecord``。

    纯函数——不读盘、不落盘,只接受内存列表(``tests/unit/test_scout_aggregate``
    单测盯分支)。

    Args:
        units: 同一 (task_id, run_id) 的所有 unit 列表;**至少 1 条**(空
            列表是调用方 bug,本函数直接 ``ValueError``——别静默返回「
            not_run」假象,那会让上游以为真有 unit)。
        expected_unit_count: 期望 unit 数(可选);少于此数记 ``missing_unit``
            留痕。**不补 unit**——per-run 聚合只能如实聚合已有数据,
            缺 unit 是上游 scout 的事。

    Returns:
        :class:`TrajectoryRecord`——聚合后的 per-Run 记录。

    Raises:
        ValueError: ``units`` 为空。
        KeyError: 任一 unit 缺 ``task_id``/``run_id``(应有但意外缺失)。
    """
    if not units:
        raise ValueError("aggregate_to_trajectory: units 不能为空")

    task_id = str(units[0]["task_id"])
    run_id = str(units[0]["run_id"])
    # 一致性检查:同 task_id / run_id 才聚合
    mismatched = [
        u["sample_id"] for u in units
        if str(u.get("task_id", "")) != task_id
        or str(u.get("run_id", "")) != run_id
    ]
    if mismatched:
        raise ValueError(
            f"aggregate_to_trajectory: 跨 (task_id, run_id) 混聚: {mismatched}"
        )

    log_entries: list[AggregationLogEntry] = []

    # ── 1. 聚合 scores(四维 min) ──
    task_scores: list[DimensionScore] = []
    for dim in DIMENSION_ORDER:
        score, is_veto, reasons, _picked = _min_score(units, dim, log_entries)
        task_scores.append({
            "dimension": dim,  # type: ignore[typeddict-item]
            "score": score,
            "is_veto": is_veto,
            "reasons": reasons,
        })

    # ── 2. tags 并集(去重,保序) ──
    seen_tags: set[str] = set()
    merged_tags: list[str] = []
    for u in units:
        for t in (u.get("tags") or ()):
            if t not in seen_tags:
                seen_tags.add(t)
                merged_tags.append(t)
                log_entries.append({
                    "unit": u["sample_id"],
                    "action": "tag_merged",
                    "reason": f"tag={t} 并入任务级 tags",
                })

    # ── 3. unit_outcomes(各 unit 的 sample_id) ──
    unit_outcomes = [u["sample_id"] for u in units]

    # ── 4. evidence_supplement 合并(键冲突取第一个非空) ──
    merged_ev: dict[str, Any] = {}
    for u in units:
        sup = u.get("evidence_supplement") or {}
        if not isinstance(sup, dict):
            continue
        for k, v in sup.items():
            if k in merged_ev:
                if merged_ev[k] not in (None, "", [], {}):
                    # 已有非空值,跳过——决策留痕
                    log_entries.append({
                        "unit": u["sample_id"],
                        "action": "evidence_kept_first",
                        "reason": (f"key={k!r} 已被 {unit_outcomes[0]!r} 占,"
                                   f" 跳过该 unit 的值"),
                    })
                    continue
                log_entries.append({
                    "unit": u["sample_id"],
                    "action": "evidence_merged",
                    "reason": f"key={k!r} 首次取到非空值",
                })
                merged_ev[k] = v
            else:
                merged_ev[k] = v
                log_entries.append({
                    "unit": u["sample_id"],
                    "action": "evidence_merged",
                    "reason": f"key={k!r} 首次取到非空值",
                })

    # ── 5. 缺 unit 留痕 ──
    if expected_unit_count is not None and len(units) < expected_unit_count:
        log_entries.append({
            "unit": "-",
            "action": "missing_unit",
            "reason": (f"期望 {expected_unit_count} 个 unit, 实际 {len(units)} 个"
                       f"——不补, 上游 scout 漏抓"),
        })

    # ── 6. status ──
    status = _aggregate_status(task_scores, units)

    return TrajectoryRecord(
        trajectory_id=f"{task_id}__{run_id}",
        task_id=task_id,
        run_id=run_id,
        status=status,
        scores=task_scores,
        tags=merged_tags,  # type: ignore[typeddict-item]
        unit_outcomes=unit_outcomes,
        evidence_supplement=merged_ev,
        aggregation_log=log_entries,
    )


# ── 纯函数:分组 ────────────────────────────────────────────────


def group_units_by_trajectory(
    records: Iterable[ScoutRecord],
) -> dict[str, list[ScoutRecord]]:
    """按 ``(task_id, run_id)`` 把 per-unit 记录分组。

    Args:
        records: 全部 unit(可能跨多个 trajectory)。

    Returns:
        ``{trajectory_id: [unit1, unit2, ...]}``。**保序**(按 units 出现顺序,
        同 trajectory 内也按出现顺序)——保证聚合结果可重现。
    """
    out: dict[str, list[ScoutRecord]] = {}
    for r in records:
        tid = f"{r['task_id']}__{r['run_id']}"
        out.setdefault(tid, []).append(r)
    return out


# ── I/O 门面:从 scout 三件套 → trajectory_candidates.jsonl ─────────


def aggregate_from_buckets(
    scout_dir: str | Path,
    output_path: str | Path | None = None,
    *,
    buckets: tuple[str, ...] = ("positive", "negative", "quarantine"),
    expected_unit_count: int | None = None,
    overwrite: bool = False,
) -> tuple[list[TrajectoryRecord], Counter]:
    """从 scout 三件套聚合到 ``trajectory_candidates.jsonl``。

    Args:
        scout_dir: 三件套所在目录。
        output_path: 聚合产物落盘路径;``None`` 时落
            ``<scout_dir>/trajectory_candidates.jsonl``。
        buckets: 要参与聚合的 bucket;默认全三态。
        expected_unit_count: 期望 unit 数(透传给 :func:`aggregate_to_trajectory`)。
        overwrite: 目标文件已存在时是否覆盖。**默认 False**(与 cli build /
            cli task-view 同款纪律)——重建整批显式传 True。

    Returns:
        ``(trajectory_records, stats)``——聚合后的轨迹列表 + 三态计数。
        **出错不抛出**——scout 产物坏行进 skipped,落空 jsonl 写不进也
        走 skipped 返回,不阻塞整批。
    """
    scout_dir = Path(scout_dir)
    if output_path is None:
        output_path = scout_dir / TRAJECTORY_BUCKET_FILE
    output_path = Path(output_path)

    # 1) 读三件套(ls_bridge.exporter.read_scout_records 已把坏行容错)
    bucket_records: list[tuple[str, ScoutRecord]] = read_scout_records(
        scout_dir, buckets=buckets,
    )
    # 不再区分 bucket——per-unit 都进同一组聚合(不分正负桶,聚合后再判
    # trajectory 是 positive / negative / undetermined / not_run)
    all_units = [r for _b, r in bucket_records]

    # 2) 分组 + 聚合
    grouped = group_units_by_trajectory(all_units)
    trajectories: list[TrajectoryRecord] = []
    for _tid, units in grouped.items():
        try:
            rec = aggregate_to_trajectory(
                units, expected_unit_count=expected_unit_count,
            )
        except (ValueError, KeyError) as exc:
            log.warning(
                "aggregate_from_buckets: %s 聚合失败 (%s): %s",
                units[0]["sample_id"] if units else "?", type(exc).__name__, exc,
            )
            continue
        trajectories.append(rec)

    # 3) 三态计数(任务级 status)
    stats: Counter = Counter()
    for tr in trajectories:
        stats[tr["status"]] += 1
    # 补 0 项(report 读起来一致)
    for k in ("scored", "undetermined", "not_run"):
        stats.setdefault(k, 0)

    # 4) 落盘(原子写,.tmp + os.replace)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and output_path.exists():
        log.warning(
            "aggregate_from_buckets: %s 已存在, 跳过写入(要重建显式 overwrite=True)",
            output_path,
        )
        return trajectories, stats

    import os
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for tr in trajectories:
                f.write(json.dumps(tr, ensure_ascii=False) + "\n")
        os.replace(tmp, output_path)
    except OSError as exc:
        log.warning(
            "aggregate_from_buckets: 写 %s 失败: %s", output_path, exc,
        )
        return trajectories, stats

    # 5) stats 落盘(同目录)
    stats_path = output_path.parent / TRAJECTORY_STATS_FILE
    try:
        stats_path.write_text(
            json.dumps(dict(stats), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning(
            "aggregate_from_buckets: 写 %s 失败: %s", stats_path, exc,
        )

    return trajectories, stats


__all__ = [
    "TRAJECTORY_BUCKET_FILE",
    "TRAJECTORY_STATS_FILE",
    "DIMENSION_ORDER",
    "aggregate_to_trajectory",
    "group_units_by_trajectory",
    "aggregate_from_buckets",
]
