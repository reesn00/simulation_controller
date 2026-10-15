"""ls_bridge.scorecard: scorecard.v2 —— scout 四维评分卡。

重写自 ``archive/v1/label_studio/scorecard.py``（scorecard.v1, 六维 L0-L5
是 gdr 专有口径, 不迁移）。v2 的维度就是 scout 的四维:

    d2  观察-动作一致性          （code_scorer）
    d4  播放组件验证正确性      （d4_scorer）
    d5  记录/排除决策正确性      （d5_scorer, ★ 样本级一票否决）
    d6  记录字段完整性          （d6_scorer）

沿用 v1 的两条核心理念:

* **每维带 ``source``**（measured / missing）—— 评分卡不撒谎: 哪一维是
  真判出来的、哪一维没判出来, 标注员一眼可辨。v1 的
  ``partly_estimated`` / ``estimated`` 枚举保留兼容（新链路没有估算分量,
  目前只会出现前两个值）。
* **prediction 只落 risk_hints, 绝不落 overall_decision** —— 自动
  accept/reject 预判会把最该看的 hard 样本自动 reject 掉（v1 验证过的
  铁律）。
"""

from __future__ import annotations

from typing import Any

from trajectory_pipeline.evaluation.scout.types import ScoutRecord

#: 维度号 → 人读标签。与 ``evaluation/scorers/`` 各 docstring 的口径一致。
DIMENSION_LABELS: dict[str, str] = {
    "d2": "观察-动作一致性",
    "d4": "播放组件验证正确性",
    "d5": "记录/排除决策正确性",
    "d6": "记录字段完整性",
}

#: ``source`` 枚举。v2 只产 ``measured`` / ``missing``; 其余两个值是 v1
#: 评分卡的字段域, 回流侧若见到不应崩溃。
SOURCE_VALUES = ("measured", "partly_estimated", "estimated", "missing")

#: ``overall.confidence`` 枚举 → LS ``score`` 数值。LS 用它给预测排序, 不
#: 参与标注语义; 映射只为让"低置信度的建议"排在后面, 人先看高置信度的。
CONFIDENCE_TO_SCORE = {"high": 0.9, "medium": 0.6, "low": 0.3}

#: scout 负例分流的分数阈值。**必须**与
#: :data:`trajectory_pipeline.evaluation.scout.policy.NEGATIVE_SCORE_THRESHOLD`
#: 一致 —— 由 ``tests/contract/test_ls_bridge_contract.py`` 盯住。
NEGATIVE_SCORE_THRESHOLD = 2


def _dimension_source(score: int | None) -> str:
    """``score is None`` → missing（没判）; 有分 → measured（真判了）。"""
    return "missing" if score is None else "measured"


def derive_overall(record: ScoutRecord) -> dict[str, Any]:
    """从四维判定 ``overall``。

    判定顺序与 :func:`trajectory_pipeline.evaluation.scout.policy.classify`
    对齐（veto 先于 None 先于阈值）, 但产出的是**展示用建议**, 不是分流
    结论 —— 分流结论由 scout 落盘时的 bucket 决定, 两者互不覆盖。

    * 任一维 ``is_veto`` → ``kind=negative, confidence=high``
    * 任一维 ``score <= 2`` → ``kind=negative``
    * 任一维 ``score is None``（且无 veto）→ ``kind=undetermined, confidence=low``
    * 否则 → ``kind=positive``
    """
    scores = record["scores"]
    veto_dims = [s["dimension"] for s in scores if s["is_veto"]]
    failed_dims = [
        s["dimension"]
        for s in scores
        if s["score"] is not None and s["score"] <= NEGATIVE_SCORE_THRESHOLD
    ]
    missing_dims = [s["dimension"] for s in scores if s["score"] is None]

    if veto_dims:
        return {
            "kind": "negative",
            "confidence": "high",
            "reason": "veto: " + ",".join(veto_dims),
        }
    if failed_dims:
        return {
            "kind": "negative",
            "confidence": "high",
            "reason": "低分维度: " + ",".join(failed_dims),
        }
    if missing_dims:
        return {
            "kind": "undetermined",
            "confidence": "low",
            "reason": "缺判维度: " + ",".join(missing_dims),
        }
    return {"kind": "positive", "confidence": "high", "reason": "四维全部通过"}


def build_scorecard(
    record: ScoutRecord, *, classify_kind: str | None = None
) -> dict[str, Any]:
    """一条 :class:`ScoutRecord` → scorecard.v2 dict。

    Args:
        classify_kind: scout 落盘时的分流结论（positive / negative /
            quarantine）。带进评分卡是为了让标注员看到"机器当时把它分到
            了哪个桶" —— 但它只是信息, 不是决策（决策由 ``overall`` 单独
            派生, 两者不一致时以人工判断为准）。
    """
    dimensions = [
        {
            "id": s["dimension"],
            "label": DIMENSION_LABELS.get(s["dimension"], s["dimension"]),
            "score": s["score"],
            "is_veto": s["is_veto"],
            "source": _dimension_source(s["score"]),
            "reasons": list(s["reasons"]),
        }
        for s in record["scores"]
    ]
    measured = sum(1 for d in dimensions if d["source"] == "measured")
    return {
        "schema_version": "scorecard.v2",
        "sample_id": record["sample_id"],
        "task_id": record["task_id"],
        "run_id": record["run_id"],
        "scout_status": record["status"],
        "scout_bucket": classify_kind,
        "enabled": True,
        "overall": derive_overall(record),
        "dimensions": dimensions,
        "dimension_summary": {
            "total": len(dimensions),
            "measured": measured,
            "missing": len(dimensions) - measured,
        },
        "tags": list(record["tags"]),
    }


def render_scorecard_text(scorecard: dict[str, Any]) -> str:
    """评分卡 → 标注页 ``scorecard_view`` 的多行文本。"""
    overall = scorecard.get("overall") or {}
    lines = [
        f"schema: {scorecard.get('schema_version', 'scorecard.v2')}",
        f"sample: {scorecard.get('sample_id', '')}  task: {scorecard.get('task_id', '')}",
        f"scout 分流: {scorecard.get('scout_bucket') or '(未分流)'}"
        f"  status: {scorecard.get('scout_status', '')}",
        f"机器建议: {overall.get('kind', '?')} (confidence={overall.get('confidence', '?')}"
        f"  {overall.get('reason', '')})",
        "── 维度 ──  score=1..5, ≤2 为低分; source=missing 表示该维没判出来, 不可全信",
    ]
    for dim in scorecard.get("dimensions") or []:
        veto_mark = " [VETO]" if dim.get("is_veto") else ""
        lines.append(
            f"  {dim['id']} {dim['label']}: score={dim['score']}{veto_mark}"
            f"  source={dim['source']}"
        )
        for reason in dim.get("reasons") or []:
            lines.append(f"      - {reason}")
    tags = scorecard.get("tags") or []
    lines.append(f"tags: {', '.join(tags) if tags else '(无)'}")
    return "\n".join(lines)


def build_risk_hints(scorecard: dict[str, Any]) -> list[str]:
    """风险提示行。**只提示, 不做 accept/reject 预判**。

    规则（照搬 v1 理念, 换 v2 字段）:
    * veto 维度 → 必提示（标注页第一优先级）
    * 低分维度 → 提示
    * missing 维度 → 提示"该维没判出来, 需人工核"
    * 无任何风险信号 → 单行 "机器未发现风险信号", 而不是空列表 ——
      空 TextArea 和 "没填" 在标注页上无法区分。
    """
    hints: list[str] = []
    for dim in scorecard.get("dimensions") or []:
        if dim.get("is_veto"):
            hints.append(f"⚠ {dim['id']} {dim['label']}: 一票否决 ({dim.get('reasons') or ['无理由记录']})")
        elif dim.get("score") is not None and dim["score"] <= NEGATIVE_SCORE_THRESHOLD:
            hints.append(f"⚠ {dim['id']} {dim['label']}: 低分 {dim['score']}/5 ({dim.get('reasons') or []})")
        elif dim.get("source") == "missing":
            hints.append(f"? {dim['id']} {dim['label']}: 该维未判出, 请人工复核")
    if not hints:
        hints.append("机器未发现风险信号; 仍需人工确认。")
    return hints


def render_risk_hints(scorecard: dict[str, Any]) -> str:
    """风险提示 → ``risk_hints`` TextArea 的文本（每 hint 一行）。"""
    return "\n".join(build_risk_hints(scorecard))


__all__ = [
    "DIMENSION_LABELS",
    "SOURCE_VALUES",
    "CONFIDENCE_TO_SCORE",
    "NEGATIVE_SCORE_THRESHOLD",
    "derive_overall",
    "build_scorecard",
    "render_scorecard_text",
    "build_risk_hints",
    "render_risk_hints",
]
