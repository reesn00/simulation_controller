"""全量逐行预测快照（方案 16 §9.2 C-15）。

为什么需要它（这句话决定了本模块的形态）
----------------------------------------
分歧卡只记「**这一轮**不一致」。而回归判定问的是「**上一轮对、这一轮错**」——
那一行在上一轮的卡里**根本不存在**（上一轮它是对的啊）。所以：

    只有分歧卡 ⇒ 回归判不出来。必须先有全量快照，卡是快照的视图。

快照按 ``(v{N}, rubric_version, evaluator_version)`` 落盘。三样都记，是因为
三者任一变了，「同一行判了什么」就不再可比（方案 16 §5.5：改一条款就作废该
条款上旧金标的可比性）。

判定口径（五态封闭集）
----------------------
每条 ``(golden_id, dimension)`` 一条 entry，:data:`VERDICTS` 五选一：

=============== ==========================================================
``agree``       两侧都有值且一致（硬分类按口径 C 的语义等价类）
``detail_mismatch`` 口径 C 一致、但细分标签不同——**D-10 不算错**，
                记下来是因为细分支对账还需要它（§3.2）
``disagree``    口径 C 不一致（``eq_class_mismatch``）——这才是 D-10 分子
``tier_gap``    软维度档差超过 tolerance
``ungraded``    任一侧缺失 / 越界 / 维度未登记——**不计分母**（``None ≠ 0``）
=============== ==========================================================

``label_mismatch`` / ``eq_class_mismatch`` / ``tier_gap`` 是三态 ``delta_kind``
（§3.2），只有后三态进分歧卡；``agree`` 与 ``ungraded`` 不进。

与 ``builder.cmd_agreement`` 的同源要求
---------------------------------------
:func:`iter_site_rows` 的过滤判据**必须**与 ``cmd_agreement`` 里算 ``site``
那两行一致（非 ``p2_sample`` 且 ``golden`` 非空）。两处会漂，由
``tests/unit/test_calibration_snapshot.py::test_快照与agreement同源`` 钉住：
快照在**头条档**上的 ``n`` / ``agreed`` 必须与命令行打印的逐字相等。

分档（方案 16 C-04 / C-17）
--------------------------
每条的 ``split`` 原样带出行上的 ``__split__``（``calibration`` / ``regression``
/ ``holdout``）。快照**三档全收**：回归集要能判"原本判对的现在判错"，
光靠调参池判不出来；但 :func:`summarize_snapshot` 的 ``d10`` 只算头条档，
并把分档读数拆在 ``d10_by_split`` 里——两个数都在，不靠"读的人记得是哪个"。

fail-closed
-----------
- 未登记标签 → :func:`~...alignment.equiv_class` 抛 ``KeyError``，本模块**不捕获**
  （D-10 当场炸，而不是悄悄算错）；
- 维度不在 :data:`SOFT_DIM_NAMES` 白名单 → 记 ``ungraded`` + ``note``，
  **不猜它的条款号**（``rubric_ref`` 留空）。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Iterable, Iterator, Mapping, Sequence

from trajectory_pipeline.evaluation.golden.alignment import (
    SOFT_DIM_NAMES,
    dim_agreement,
    is_equivalent,
)
from trajectory_pipeline.evaluation.golden.splits import HEADLINE_SPLIT
from trajectory_pipeline.evaluation.scorers.rubric_version import clause_of

#: 快照文件 schema 版本。字段增删必须升它（回归判定吃的是这份文件）。
SNAPSHOT_SCHEMA_VERSION = "1"

VERDICT_AGREE = "agree"
VERDICT_DETAIL_MISMATCH = "detail_mismatch"
VERDICT_DISAGREE = "disagree"
VERDICT_TIER_GAP = "tier_gap"
VERDICT_UNGRADED = "ungraded"

#: 五态封闭集（顺序即报表顺序）。新增状态先问：它属于哪一档？
VERDICTS: tuple[str, ...] = (
    VERDICT_AGREE,
    VERDICT_DETAIL_MISMATCH,
    VERDICT_DISAGREE,
    VERDICT_TIER_GAP,
    VERDICT_UNGRADED,
)

#: 进分歧卡的三态（有差异、且差异有责任方）。``agree`` / ``ungraded`` 不进。
DIFF_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_DETAIL_MISMATCH, VERDICT_DISAGREE, VERDICT_TIER_GAP}
)

#: ``delta_kind`` 三态（方案 16 §3.2）。
DELTA_LABEL_MISMATCH = "label_mismatch"
DELTA_EQ_CLASS_MISMATCH = "eq_class_mismatch"
DELTA_TIER_GAP = "tier_gap"

#: 硬分类维度名（D-10 口径 C 的那一列）。
HARD_DIM = "d10"

#: 预测侧出处标签。**D-10 的 pred 不是当前评分器重算的**，是冻结行里采集时
#: 写下的 ``code.branch``——把它当成本次代码的产物是这类报表最常见的误读。
PROVIDER_FROZEN_BRANCH = "code:frozen_row"
PROVIDER_ROW_DIMS = "code:row.dimension_scores"


def iter_site_rows(
    rows: Iterable[Mapping[str, Any]],
) -> Iterator[Mapping[str, Any]]:
    """站点级已标注行——**与 ``cmd_agreement`` 的 ``site`` 同一判据**。

    轨迹级样本（``source_kind == "p2_sample"``）没有可比的人工标签，
    未标注的行（``golden`` 为空）无从判对错。两处过滤必须一致，
    否则快照的分母和头条的分母是两个数（见模块 docstring 的同源要求）。
    """
    for row in rows:
        if row.get("source_kind") == "p2_sample":
            continue
        if not row.get("golden"):
            continue
        yield row


def _dim_scores(side: Mapping[str, Any] | None) -> Mapping[str, Any]:
    scores = side.get("dimension_scores") if isinstance(side, Mapping) else None
    return scores if isinstance(scores, Mapping) else {}


def _pred_reasons(row: Mapping[str, Any], dimension: str) -> list[str]:
    """该维度的判分理由原文（``scores_detail`` 里按 dimension 取）。"""
    detail = row.get("scores_detail")
    if not isinstance(detail, Sequence):
        return []
    for item in detail:
        if isinstance(item, Mapping) and item.get("dimension") == dimension:
            reasons = item.get("reasons")
            if isinstance(reasons, Sequence) and not isinstance(reasons, str):
                return [str(r) for r in reasons]
            return []
    return []


def _clause_or_empty(dimension: str, tier: Any) -> tuple[str, str]:
    """``(rubric_ref, note)``；维度未登记条款号时留空并说明原因。"""
    try:
        tier_int = int(tier) if isinstance(tier, (int, float)) else None
    except (TypeError, ValueError):
        tier_int = None
    try:
        return clause_of(dimension, tier_int), ""
    except (KeyError, ValueError) as exc:
        return "", f"条款号不可用：{exc}"


def _entry(
    *,
    golden_id: str,
    split: str,
    dimension: str,
    rubric_ref: str,
    gold_value: Any,
    pred_value: Any,
    pred_provider: str,
    verdict: str,
    delta_kind: str | None,
    tier_gap: int | None,
    gold_note: str,
    pred_reasons: Sequence[str],
    pred_evidence: str | None,
    note: str = "",
) -> dict[str, Any]:
    if verdict not in VERDICTS:  # pragma: no cover - 由构造点保证
        raise ValueError(f"未登记 verdict {verdict!r}；先扩 VERDICTS")
    return {
        "golden_id": golden_id,
        "split": split,
        "dimension": dimension,
        "rubric_ref": rubric_ref,
        "gold_value": gold_value,
        "pred_value": pred_value,
        "pred_provider": pred_provider,
        "verdict": verdict,
        "delta_kind": delta_kind,
        "tier_gap": tier_gap,
        "gold_note": gold_note,
        "pred_reasons": list(pred_reasons),
        "pred_evidence": pred_evidence,
        "note": note,
    }


def _hard_entry(
    row: Mapping[str, Any], *, split: str, success_label: str,
) -> dict[str, Any]:
    """D-10 硬分类条目：金标标签 ↔ 冻结行的 ``code.branch``。"""
    golden = row.get("golden") or {}
    code = row.get("code") or {}
    gold_value = golden.get("label") or success_label
    pred_value = code.get("branch") or success_label
    rubric_ref, note = _clause_or_empty(HARD_DIM, None)

    if is_equivalent(gold_value, pred_value):
        if gold_value == pred_value:
            verdict, delta = VERDICT_AGREE, None
        else:
            # 等价类相同、字面不同：D-10 不算错，但细分对账要它。
            verdict, delta = VERDICT_DETAIL_MISMATCH, DELTA_LABEL_MISMATCH
    else:
        verdict, delta = VERDICT_DISAGREE, DELTA_EQ_CLASS_MISMATCH

    return _entry(
        golden_id=str(row.get("golden_id") or ""),
        split=split,
        dimension=HARD_DIM,
        rubric_ref=rubric_ref,
        gold_value=gold_value,
        pred_value=pred_value,
        pred_provider=PROVIDER_FROZEN_BRANCH,
        verdict=verdict,
        delta_kind=delta,
        tier_gap=None,
        gold_note=str(golden.get("note") or ""),
        pred_reasons=(),
        pred_evidence=code.get("evidence"),
        note=note,
    )


def _soft_entry(
    row: Mapping[str, Any],
    *,
    split: str,
    dimension: str,
    tolerance: int,
) -> dict[str, Any]:
    """软维度条目：人工 1–5 档 ↔ 冻结行的 ``dimension_scores``。"""
    golden = row.get("golden") or {}
    gold_dims = _dim_scores(golden)
    pred_dims = _dim_scores(row)
    gold_value = gold_dims.get(dimension)
    pred_value = pred_dims.get(dimension)
    rubric_ref, note = _clause_or_empty(dimension, gold_value)

    if dimension not in SOFT_DIM_NAMES:
        note = note or f"维度 {dimension!r} 不在软维度白名单"
        verdict, delta, gap = VERDICT_UNGRADED, None, None
    else:
        gap = None
        if gold_value is not None and pred_value is not None:
            try:
                gap = abs(int(gold_value) - int(pred_value))
            except (TypeError, ValueError):
                gap = None
        ok = dim_agreement(gold_value, pred_value, tolerance)
        if ok is None:
            verdict, delta = VERDICT_UNGRADED, None
        elif ok:
            verdict, delta = VERDICT_AGREE, None
        else:
            verdict, delta = VERDICT_TIER_GAP, DELTA_TIER_GAP

    return _entry(
        golden_id=str(row.get("golden_id") or ""),
        split=split,
        dimension=dimension,
        rubric_ref=rubric_ref,
        gold_value=gold_value,
        pred_value=pred_value,
        pred_provider=PROVIDER_ROW_DIMS,
        verdict=verdict,
        delta_kind=delta,
        tier_gap=gap,
        gold_note=str(golden.get("note") or ""),
        pred_reasons=_pred_reasons(row, dimension),
        pred_evidence=None,
        note=note,
    )


def build_snapshot(
    rows: Sequence[Mapping[str, Any]],
    *,
    golden_version: int,
    success_label: str,
    rubric_version: str = "",
    evaluator_version: str = "",
    tolerance: int = 1,
) -> dict[str, Any]:
    """全量逐行快照。

    ``rows`` 是冻结档读出的行（可含 ``__split__`` 标注，与 ``cmd_agreement``
    同一份）。D-10 每条站点行一条 entry；软维度按「金标侧 ∪ 预测侧」出现的
    dim 逐条出（缺一侧即 ``ungraded``——缺失本身是要记的事实，不是要抹掉的噪声）。

    ``rubric_version`` / ``evaluator_version`` 由调用方从 manifest 与
    ``rubric_version.evaluator_version()`` 取。老冻结档（v6 及更早）没有
    manifest 字段时传空串——**不拿当前值顶替**（方案 16 §9.2 C-01 口径 1）。
    """
    entries: list[dict[str, Any]] = []
    for row in iter_site_rows(rows):
        split = str(row.get("__split__") or "")
        entries.append(_hard_entry(row, split=split, success_label=success_label))
        gold_dims = _dim_scores(row.get("golden") or {})
        pred_dims = _dim_scores(row)
        for dimension in sorted(set(gold_dims) | set(pred_dims)):
            entries.append(
                _soft_entry(
                    row,
                    split=split,
                    dimension=dimension,
                    tolerance=tolerance,
                )
            )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "meta": {
            "golden_version": int(golden_version),
            "rubric_version": rubric_version,
            "evaluator_version": evaluator_version,
            "tolerance": int(tolerance),
            "success_label": success_label,
            "hard_dim": HARD_DIM,
            "merged_rows": len(rows),
            "entries": len(entries),
        },
        "entries": entries,
    }


def summarize_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """快照的计数视图（报表 / 断言用；不重算判定）。"""
    by_verdict = {v: 0 for v in VERDICTS}
    by_split: dict[str, int] = {}
    by_dimension: dict[str, dict[str, int]] = {}
    for entry in snapshot.get("entries", []):
        verdict = entry.get("verdict")
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        split = str(entry.get("split") or "")
        by_split[split] = by_split.get(split, 0) + 1
        dim = str(entry.get("dimension") or "")
        bucket = by_dimension.setdefault(dim, {v: 0 for v in VERDICTS})
        bucket[verdict] = bucket.get(verdict, 0) + 1

    # D-10 计数：分档各算一份，``d10`` 取头条档（方案 16 C-04）。
    # ``ungraded`` 不进分母（None ≠ 0）——两侧都没判的条目记不进对错。
    d10_by_split: dict[str, dict[str, int]] = {}
    for entry in snapshot.get("entries", []):
        if entry.get("dimension") != HARD_DIM:
            continue
        split = str(entry.get("split") or "")
        bucket = d10_by_split.setdefault(split, {"n": 0, "agreed": 0})
        verdict = entry.get("verdict")
        if verdict in (VERDICT_AGREE, VERDICT_DETAIL_MISMATCH):
            bucket["n"] += 1
            bucket["agreed"] += 1
        elif verdict == VERDICT_DISAGREE:
            bucket["n"] += 1
    # 老冻结档没有 ``__split__``（空串）→ 全部算头条，与换分母前一致。
    spills = [s for s in d10_by_split if s]
    d10 = (
        d10_by_split.get(HEADLINE_SPLIT, {"n": 0, "agreed": 0})
        if spills
        else d10_by_split.get("", {"n": 0, "agreed": 0})
    )
    return {
        "entries": sum(by_verdict.values()),
        "by_verdict": by_verdict,
        "by_split": dict(sorted(by_split.items())),
        "by_dimension": {k: by_dimension[k] for k in sorted(by_dimension)},
        # ``d10`` = 头条档口径（与 ``cmd_agreement`` 打印等价）；分档在
        # ``d10_by_split``，旧口径（三档合计）在 ``d10_all_splits``。
        "d10": d10,
        "d10_by_split": {k: d10_by_split[k] for k in sorted(d10_by_split)},
        "d10_all_splits": {
            "n": sum(b["n"] for b in d10_by_split.values()),
            "agreed": sum(b["agreed"] for b in d10_by_split.values()),
        },
    }


def verdicts_of(snapshot: Mapping[str, Any], dimension: str) -> list[str]:
    """某维度上逐条的 verdict（测试与下钻用）。"""
    return [
        str(e.get("verdict"))
        for e in snapshot.get("entries", [])
        if e.get("dimension") == dimension
    ]


def write_snapshot(path: pathlib.Path | str, snapshot: Mapping[str, Any]) -> pathlib.Path:
    """落 JSON（确定性：条目顺序 = 行序，不写时间戳，便于跨版本 diff）。"""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return p


def load_snapshot(path: pathlib.Path | str) -> dict[str, Any]:
    """读回快照；结构不对直接抛（回归判定不许吃半份文件）。"""
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "entries" not in data or "meta" not in data:
        raise ValueError(f"{path} 不是快照文件（缺 entries / meta）")
    return data


__all__ = [
    "DELTA_EQ_CLASS_MISMATCH",
    "DELTA_LABEL_MISMATCH",
    "DELTA_TIER_GAP",
    "DIFF_VERDICTS",
    "HARD_DIM",
    "PROVIDER_FROZEN_BRANCH",
    "PROVIDER_ROW_DIMS",
    "SNAPSHOT_SCHEMA_VERSION",
    "VERDICTS",
    "VERDICT_AGREE",
    "VERDICT_DETAIL_MISMATCH",
    "VERDICT_DISAGREE",
    "VERDICT_TIER_GAP",
    "VERDICT_UNGRADED",
    "build_snapshot",
    "iter_site_rows",
    "load_snapshot",
    "summarize_snapshot",
    "verdicts_of",
    "write_snapshot",
]