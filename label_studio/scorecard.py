"""label_studio.scorecard: C3 meta.json → 评分卡 ``scorecard.v1``.

这是本项目给 Label Studio 的**核心交付物**（方案 §4.2 / §4.3）：不推单一
标量，推**分层的指令评分 + 每层依据 + 来源可信度**。

为什么强调"来源可信度"
--------------------
``quality_scorer`` 的 ``health`` 分量（权重 0.25，四分之一）是**粗估** ——
``gdr/core/quality_scorer.py::_avg_health_score`` 的 docstring 自承："health
来自 router.health_scores，runner 没把它落盘，这里按 message 数和总 toolcall
数做粗估"。不标出来，标注员会拿 0.83 这样的数字做 accept/reject 判定，
建立在假精度上。所以每个维度强制带 ``source``：

===============  ==========================================
``source``       含义
===============  ==========================================
``measured``     有真实落盘数据支撑
``partly_estimated``  部分分量为粗估（见 ``estimated_components``）
``estimated``    整体为推断
``missing``      数据缺失，**不可评分**（UI 须显示"不可用"而非 0）
===============  ==========================================

分层（L0–L5）
------------
=========  ==========================  ================================
层         id                          数据源
=========  ==========================  ================================
L0         ``criterion_coverage``     ``meta["criterion_results"]``（F2 注入）
L1         ``instruction_adherence``  ``trajectory_compare``
L2         ``redline``                ``trajectory_free`` / redline 结果
L3         ``block_validation``       ``validation_summary``
L4         ``training_value``         ``training_value_score`` + components
L5         ``edit_status``            ``edit_status_summary``
=========  ==========================  ================================
"""

from __future__ import annotations

import logging
from typing import Any

from label_studio.settings import (
    COMPONENT_WEIGHTS,
    ESTIMATED_WEIGHT_ALERT_THRESHOLD,
    KNOWN_ESTIMATED_COMPONENTS,
    ScorecardSettings,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = "scorecard.v1"

#: 判定枚举。
_DECISIONS = ("accept", "revise", "reject")

#: ``health`` 为粗估的出处 —— 直接进产物, 标注员能看到为什么不可全信。
_HEALTH_ESTIMATION_REASON = (
    "quality_scorer._avg_health_score: router.health_scores 未落盘, "
    "由 toolcall 成功率粗估"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float) -> float:
    return round(value, 4)


def _missing(dimension_id: str, label: str, reason: str) -> dict[str, Any]:
    return {
        "id": dimension_id,
        "label": label,
        "score": None,
        "score_kind": None,
        "source": "missing",
        "unavailable_because": reason,
        "evidence": [],
    }


# ---------------------------------------------------------------------------
# L0 — 指令项达成（评分卡主载体）
# ---------------------------------------------------------------------------


def _dimension_criterion_coverage(meta: dict[str, Any]) -> dict[str, Any]:
    """L0: 每条 criterion 的 PASS/FAIL/INCONCLUSIVE/ERROR + 依据。

    数据源是 F2 从 ``output/runs/<run_id>/`` 注入的 ``criterion_results``。
    criterion 就是用户指令的可验证条目，所以这一维是"明确的指令评分"。
    """
    evaluation = meta.get("criterion_results")
    if not isinstance(evaluation, dict):
        return _missing("criterion_coverage", "指令项达成", "C3 未携带 criterion_results（simulate 端未产出或 F2 未注入）")

    criteria = evaluation.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        return _missing("criterion_coverage", "指令项达成", "criterion_results.criteria 为空")

    pass_count = sum(1 for c in criteria if c.get("verdict") == "pass")
    evidence = [
        {
            "criterion_id": c.get("criterion_id"),
            "verdict": c.get("verdict"),
            "reason_code": c.get("reason_code", ""),
            "message": c.get("message", ""),
            "evidence_ids": c.get("evidence_ids") or [],
            "retryable": bool(c.get("retryable", False)),
        }
        for c in criteria
        if isinstance(c, dict)
    ]
    non_pass = [e for e in evidence if e["verdict"] != "pass"]
    final_verdict = evaluation.get("final_verdict")
    # fail-closed: simulate 端整体判定没过 → 本维直接 0 分, 不按比例给分。
    # 比例分会把「3/5 条通过但任务整体失败」显示成 0.6, 掩盖"这条数据不可用"
    # 的事实 (CLAUDE.md 数据保留原则: 分数表达质量, 不表达"还在讨论中")。
    validated = final_verdict == "pass"
    return {
        "id": "criterion_coverage",
        "label": "指令项达成",
        "score": _round(pass_count / len(criteria)) if validated else 0.0,
        "score_kind": "ratio",
        "source": "measured",
        "final_verdict": final_verdict,
        "rounds": evaluation.get("rounds"),
        "missing_items": evaluation.get("missing_items") or [],
        "non_pass_count": len(non_pass),
        # LLM 失败归因 (orchestration.fail_evaluator); 未评价时为 None
        "fail_evaluation": meta.get("fail_evaluation"),
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# L1 — 指令遵循（对比式评分）
# ---------------------------------------------------------------------------


def _dimension_instruction_adherence(meta: dict[str, Any]) -> dict[str, Any]:
    """L1: ``TrajectoryCompareResult.instruction_adherence``。

    依据是结构化的 ``diff_summary[{step_range, type, note}]`` —— 直接指出
    哪几步、什么性质、改了什么。
    """
    compare = meta.get("trajectory_compare")
    if not isinstance(compare, dict):
        return _missing("instruction_adherence", "指令遵循", "未跑对比式评分（trajectory_compare 缺失）")

    adherence = compare.get("instruction_adherence")
    if not isinstance(adherence, dict):
        return _missing("instruction_adherence", "指令遵循", "trajectory_compare.instruction_adherence 缺失")

    score = adherence.get("score")
    if score not in ("pass", "review", "fail"):
        return _missing("instruction_adherence", "指令遵循", f"instruction_adherence.score 非枚举: {score!r}")

    diff_summary = adherence.get("diff_summary")
    evidence = [
        {
            "loc": item.get("step_range", ""),
            "type": item.get("type", ""),
            "note": item.get("note", ""),
        }
        for item in (diff_summary or [])
        if isinstance(item, dict)
    ]
    fidelity = compare.get("fidelity") if isinstance(compare.get("fidelity"), dict) else {}
    return {
        "id": "instruction_adherence",
        "label": "指令遵循",
        "score": score,
        "score_kind": "enum",
        "source": "measured",
        "trajectory_overall": compare.get("overall"),
        "lost_elements": fidelity.get("lost_elements") or [],
        "preserved_core": fidelity.get("preserved_core") or [],
        "regressions": sum(1 for e in evidence if e["type"] == "regression"),
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# L2 — 红线合规
# ---------------------------------------------------------------------------


def _dimension_redline(meta: dict[str, Any]) -> dict[str, Any]:
    """L2: 红线违规与否 + 违规证据。零违规才放行。"""
    free = meta.get("trajectory_free")
    candidates: list[dict[str, Any]] = []
    for container in (meta.get("redline"), free):
        if isinstance(container, dict):
            for key in ("redline", "redline_result"):
                nested = container.get(key)
                if isinstance(nested, dict):
                    candidates.append(nested)
            if "violation" in container:
                candidates.append(container)
    if not candidates:
        return _missing("redline", "红线合规", "未跑红线检查（trajectory_free 缺失）")

    labels: list[dict[str, Any]] = []
    violated = False
    seen: set[int] = set()
    for result in candidates:
        if id(result) in seen:
            continue
        seen.add(id(result))
        if result.get("violation"):
            violated = True
        for label in result.get("labels") or []:
            if isinstance(label, dict):
                labels.append(
                    {
                        "type": label.get("type", ""),
                        "loc": f"step {label.get('step_location')}",
                        "note": label.get("evidence", ""),
                    }
                )
    return {
        "id": "redline",
        "label": "红线合规",
        "score": violated,
        "score_kind": "bool",
        "source": "measured",
        "violation_count": len(labels),
        "evidence": labels,
    }


# ---------------------------------------------------------------------------
# L3 — 块级校验
# ---------------------------------------------------------------------------


def _dimension_block_validation(meta: dict[str, Any]) -> dict[str, Any]:
    """L3: L1/L2/L3 三层的块级通过计数。"""
    summary = meta.get("validation_summary")
    if not isinstance(summary, dict):
        return _missing("block_validation", "块级校验", "validation_summary 缺失")

    total = _num(summary.get("total_blocks"))
    if not total:
        return _missing("block_validation", "块级校验", "validation_summary.total_blocks 为 0 或缺失")

    passed = {lv: int(_num(summary.get(f"passed_{lv}")) or 0) for lv in ("L1", "L2", "L3")}
    failed = {lv: int(_num(summary.get(f"failed_{lv}")) or 0) for lv in ("L1", "L2", "L3")}
    checked = sum(passed.values()) + sum(failed.values())
    evidence = [
        {"loc": level, "note": f"passed={passed[level]} failed={failed[level]}"}
        for level in ("L1", "L2", "L3")
    ]
    return {
        "id": "block_validation",
        "label": "块级校验",
        "score": _round(sum(passed.values()) / checked) if checked else None,
        "score_kind": "ratio",
        "source": "measured",
        "total_blocks": int(total),
        "checked_blocks": checked,
        "modified_blocks": int(_num(summary.get("modified_blocks")) or 0),
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# L4 — 轨迹训练价值（含分量可信度）
# ---------------------------------------------------------------------------


def _dimension_training_value(meta: dict[str, Any]) -> dict[str, Any]:
    """L4: 训练价值分 + 七维分量 + **哪些分量是估算的**。

    这是整个评分卡里最容易误导人的一维：总分看起来精确到小数点后四位，
    但其中权重最大的 ``health`` 分量是粗估。所以本维必须把
    ``estimated_components`` 显式摊开。
    """
    score = _num(meta.get("training_value_score"))
    if score is None:
        return _missing("training_value", "轨迹训练价值", "training_value_score 缺失（quality_scorer 未跑）")

    components = meta.get("quality_scorer_components")
    if not isinstance(components, dict) or not components:
        return {
            "id": "training_value",
            "label": "轨迹训练价值",
            "score": _round(score),
            "score_kind": "ratio",
            "source": "measured",
            "complexity_tier": meta.get("complexity_tier"),
            "components": {},
            "weights": dict(COMPONENT_WEIGHTS),
            "estimated_components": [],
            "estimated_because": {},
            "evidence": [],
            "note": "分量缺失, 总分无法追溯构成",
        }

    known: list[str] = []
    reasons: dict[str, str] = {}
    for name in sorted(components):
        if name in KNOWN_ESTIMATED_COMPONENTS:
            known.append(name)
            reasons[name] = _HEALTH_ESTIMATION_REASON
    for name in components:
        if name not in COMPONENT_WEIGHTS:
            reasons.setdefault(
                name, "权重未知的新增分量, 未经审计, 按估算处理"
            )
            if name not in known:
                known.append(name)

    heavy = [
        name for name in known
        if COMPONENT_WEIGHTS.get(name, 0.0) >= ESTIMATED_WEIGHT_ALERT_THRESHOLD
    ]
    source = "partly_estimated" if known else "measured"
    return {
        "id": "training_value",
        "label": "轨迹训练价值",
        "score": _round(score),
        "score_kind": "ratio",
        "source": source,
        "complexity_tier": meta.get("complexity_tier"),
        "components": {k: _round(float(v)) for k, v in components.items() if _num(v) is not None},
        "weights": dict(COMPONENT_WEIGHTS),
        "estimated_components": known,
        "estimated_because": reasons,
        "estimated_alert": heavy,
        "evidence": [
            {"loc": name, "note": f"score={components[name]} weight={COMPONENT_WEIGHTS.get(name)}"}
            for name in sorted(components)
        ],
    }


# ---------------------------------------------------------------------------
# L5 — 编辑状态
# ---------------------------------------------------------------------------


def _dimension_edit_status(meta: dict[str, Any]) -> dict[str, Any]:
    """L5: 精修阶段各编辑状态的块数分布。"""
    summary = meta.get("edit_status_summary")
    if not isinstance(summary, dict) or not summary:
        return _missing("edit_status", "编辑状态", "edit_status_summary 缺失")

    total = sum(int(_num(v) or 0) for v in summary.values())
    return {
        "id": "edit_status",
        "label": "编辑状态",
        "score": _round(int(_num(summary.get("EDIT")) or 0) / total) if total else None,
        "score_kind": "ratio",
        "score_note": "EDIT 占比（改动越小, 原始轨迹越接近已通过）",
        "source": "measured",
        "total": total,
        "evidence": [{"loc": k, "note": f"count={v}"} for k, v in sorted(summary.items())],
    }


#: L0–L5 构建器, 顺序即展示顺序。
_DIMENSION_BUILDERS = (
    _dimension_criterion_coverage,
    _dimension_instruction_adherence,
    _dimension_redline,
    _dimension_block_validation,
    _dimension_training_value,
    _dimension_edit_status,
)


# ---------------------------------------------------------------------------
# 总体判定
# ---------------------------------------------------------------------------


def derive_overall(dimensions: list[dict[str, Any]]) -> dict[str, Any]:
    """从各维推出**建议**判定 + 置信度 + 人可读的推导链。

    刻意**不预判 overall_decision** —— 人工判定是终点的核心动作（方案 §5.2）。
    这里给的是"建议 + 依据"，标注员可以任意推翻。

    建议规则（保守）：任一红线违规 → reject；有未达成 criterion → revise；
    否则 accept。

    **可测维度少于一半时不给建议**（``suggested_decision=None``）：那种情况下
    "未见异常"只意味着"没看到要看的地方"，把它写成 accept 就是在卖弄确定性 ——
    与本模块标 ``estimated`` 的动机是同一件事, 只是发生在总体层。
    """
    by_id = {d.get("id"): d for d in dimensions if d.get("id")}

    scored = [d for d in dimensions if d.get("source") != "missing"]
    estimated = [d for d in scored if d.get("source") in ("estimated", "partly_estimated")]
    basis = [d["id"] for d in scored if d.get("id")]

    # 置信度：可评维度越少、可信度越低
    if len(scored) <= 2 or len(estimated) >= 3:
        confidence = "low"
    elif estimated:
        confidence = "medium"
    else:
        confidence = "high"

    if len(dimensions) and len(scored) * 2 < len(dimensions):
        return {
            "suggested_decision": None,
            "confidence": "low",
            "basis_dimensions": basis,
            "derivation": (
                f"数据不足: {len(dimensions)} 维中仅 {len(scored)} 维可评, "
                f"不给自动建议 (可评: {', '.join(basis) or '无'})"
            ),
            "note": "本判定为自动建议, LS 侧 overall_decision 必须由人工选择",
        }

    redline = by_id.get("redline")
    coverage = by_id.get("criterion_coverage")
    value = by_id.get("training_value")

    reasons: list[str] = []
    decision = "accept"

    if redline and redline.get("source") != "missing" and redline.get("score"):
        decision = "reject"
        reasons.append(f"红线违规 {redline.get('violation_count')} 项")

    if coverage and coverage.get("source") != "missing":
        non_pass = int(coverage.get("non_pass_count") or 0)
        if non_pass and decision != "reject":
            decision = "revise"
            reasons.append(f"指令项 {non_pass}/{len(coverage.get('evidence') or [])} 未通过")

    if value and value.get("source") != "missing":
        if value.get("score") is not None and value["score"] < 0.4 and decision == "accept":
            reasons.append(f"训练价值分偏低 {value['score']}（不改变建议, 仅供参考）")

    if not reasons:
        reasons.append("自动检查未见显著异常")

    derivation = (
        "；".join(reasons) if decision == "reject"
        else f"建议 {decision}：" + "；".join(reasons)
    )
    return {
        "suggested_decision": decision if decision in _DECISIONS else "revise",
        "confidence": confidence,
        "basis_dimensions": basis,
        "derivation": derivation,
        "note": "本判定为自动建议, LS 侧 overall_decision 必须由人工选择",
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


#: ``meta["audit_reason"]`` → 人话。gdr 是判分方, 标注员不认识这些内部状态名。
_AUDIT_REASON_LABELS = {
    "judge_discard": "精修质量评分未达标, gdr 已拒收",
    "scoring_reject": "评分拒收, gdr 已拒收",
}

_AUDIT_NOTE = (
    "本样本结构合格、但被 gdr 以质量评分拒收, 按项目「数据保留原则」仍推上来"
    "供人工复核 —— 结构合格的低分轨迹是有用素材, 不是废数据。请重点复核"
    "「精修质量」维度, 判断拒收是否恰当; 其余维度的低分不代表样本本身不可用。"
)


def build_audit_marker(meta: dict[str, Any]) -> dict[str, Any] | None:
    """C3 meta 的 ``audit_reason`` → 低分标记。**没有就返回 None**。

    与 :func:`orchestration.criterion_source.inject_audit_reason` 配套: 那边
    ``None`` 时不写键, 这边就靠「键存在」判断要不要打标。空串 / 未知值都
    当没有 —— 宁可漏标也不能把正常样本误标成拒收样本。
    """
    reason = meta.get("audit_reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    reason = reason.strip()
    return {
        "audited": True,
        "reason": reason,
        "label": _AUDIT_REASON_LABELS.get(reason, f"gdr 拒收 ({reason})"),
        "note": _AUDIT_NOTE,
    }


def build_scorecard(
    meta: dict[str, Any] | None,
    *,
    task_id: str = "",
    session_id: str = "",
    settings: ScorecardSettings | None = None,
) -> dict[str, Any]:
    """C3 ``meta.json`` → 评分卡。

    Args:
        meta: ``*.meta.json`` 的内容（``save_session_v2`` 写的平铺结构）。
        task_id: 从文件名解析, 供反向追溯。
        session_id: 一般取 ``meta["session_id"]``。
        settings: 评分卡开关；``None`` 用默认。

    Returns:
        ``scorecard.v1`` dict。``settings.enabled=False`` 时返回
        ``{"schema_version", "task_id", "session_id", "enabled": False}`` ——
        轨迹仍会推送，只是不带评分卡。
    """
    meta = meta if isinstance(meta, dict) else {}
    settings = settings or ScorecardSettings()

    if not settings.enabled:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": task_id,
            "session_id": session_id or str(meta.get("session_id") or ""),
            "enabled": False,
            "note": "scorecard.enabled=false: 仅推送轨迹, 不带评分卡",
        }

    dimensions: list[dict[str, Any]] = []
    for builder in _DIMENSION_BUILDERS:
        dimension = builder(meta)
        if dimension.get("source") == "missing" and settings.drop_missing_dimensions:
            # 保留但降级: 仍写进 dimensions, 让 UI 显式显示"不可用"而不是
            # 让标注员误以为"没有这一项"。只有在明确要求时才彻底丢弃。
            pass
        dimensions.append(dimension)

    if settings.require_estimated_flag:
        # 保证每个非 missing 维度都带 source —— 缺了就标 measured 之外的
        # 保守值, 宁可多提示不可少提示。
        for dimension in dimensions:
            if dimension.get("source") not in (
                "measured", "partly_estimated", "estimated", "missing",
            ):
                dimension["source"] = "estimated"
                dimension.setdefault(
                    "estimated_because", {"_": "source 字段异常, 按估算处理"}
                )

    scorecard: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "session_id": session_id or str(meta.get("session_id") or ""),
        "enabled": True,
        "overall": derive_overall(dimensions),
        "dimensions": dimensions,
        "dimension_summary": {
            "total": len(dimensions),
            "measured": sum(1 for d in dimensions if d.get("source") == "measured"),
            "estimated": sum(
                1 for d in dimensions
                if d.get("source") in ("estimated", "partly_estimated")
            ),
            "missing": sum(1 for d in dimensions if d.get("source") == "missing"),
        },
    }
    # 低分标记。**没有就不写这个键** —— 靠键存在与否判断, 不塞 audited=false,
    # 免得下游"这个字段恒在"就当成每条都拒收。
    audit = build_audit_marker(meta)
    if audit is not None:
        scorecard["audit"] = audit
    return scorecard


def build_risk_hints(scorecard: dict[str, Any]) -> list[str]:
    """生成 ML 预标注的风险提示（方案 §5.2）—— **不是** accept/reject 预判。

    刻意不产出 ``overall_decision``：hard 样本恰恰最需要人工看，自动 reject
    等于把最该看的样本排除（且与方案 §3「优先标注 hard 样本」自相矛盾）。
    """
    if not scorecard.get("enabled", True):
        return []
    hints: list[str] = []
    by_id = {d.get("id"): d for d in scorecard.get("dimensions") or [] if d.get("id")}

    # 低分样本必须排第一: 它解释了后面所有低分维度的成因, 藏在末尾等于没说。
    audit = scorecard.get("audit")
    if isinstance(audit, dict) and audit.get("audited"):
        hints.append(
            f"低分样本（{audit.get('label')}）｜{audit.get('note')}"
        )

    coverage = by_id.get("criterion_coverage")
    if coverage and coverage.get("source") != "missing":
        failed = [
            e.get("criterion_id")
            for e in coverage.get("evidence") or []
            if e.get("verdict") == "fail"
        ]
        # inconclusive 也要提示: 它同样是"没过", 只筛 fail 会让语义待定项
        # 在人工复核界面上完全隐身 (scorecard 唯一提示入口就是这里)。
        inconclusive = [
            e.get("criterion_id")
            for e in coverage.get("evidence") or []
            if e.get("verdict") in ("inconclusive", "error")
        ]
        if failed:
            hints.append(
                "指令未完全达成，请核对: " + ", ".join(str(c) for c in failed)
            )
        if inconclusive:
            hints.append(
                "指令判定待定，请核对: " + ", ".join(str(c) for c in inconclusive)
            )
        # 验证未通过 → 本维已按 fail-closed 记 0 分, 附上 LLM 归因方便定位
        if coverage.get("final_verdict") not in (None, "pass"):
            attribution = coverage.get("fail_evaluation")
            if isinstance(attribution, dict):
                hints.append(
                    f"该样本验证未通过（已记 0 分）｜归因 "
                    f"{attribution.get('failure_category')}："
                    f"{attribution.get('root_cause') or attribution.get('review_note')}"
                )
            else:
                hints.append("该样本验证未通过（已记 0 分）")

    redline = by_id.get("redline")
    if redline and redline.get("source") != "missing" and redline.get("score"):
        hints.append(f"红线违规 {redline.get('violation_count')} 项，必须复核")

    value = by_id.get("training_value")
    if value:
        heavy = value.get("estimated_alert") or []
        if heavy:
            hints.append(
                "该分量为估算值，非实测: " + ", ".join(heavy)
                + "（合计权重 "
                + str(sum(COMPONENT_WEIGHTS.get(n, 0.0) for n in heavy))
                + "）"
            )

    if not hints:
        hints.append("自动检查未见异常，仍需人工确认")
    return hints


__all__ = [
    "SCHEMA_VERSION",
    "build_scorecard",
    "build_risk_hints",
    "derive_overall",
]
