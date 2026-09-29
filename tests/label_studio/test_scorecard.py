"""label_studio.scorecard 单元测试 (P1, 2026-09-28).

重点验证三件事:
1. L0–L5 六个维度各自从正确的数据源取值
2. ``source`` 必填且语义正确 —— 尤其 L4 的 ``partly_estimated`` (health 是粗估)
3. 数据缺失时给 ``missing`` 而不是 0
"""

from __future__ import annotations

import copy

import pytest

from label_studio.scorecard import (
    SCHEMA_VERSION,
    build_risk_hints,
    build_scorecard,
    derive_overall,
)
from label_studio.settings import ScorecardSettings
from c3_fixtures import RICH_META


def _by_id(scorecard: dict) -> dict:
    return {d["id"]: d for d in scorecard["dimensions"]}


# ---------------------------------------------------------------------------
# 顶层结构
# ---------------------------------------------------------------------------


def test_top_level_shape():
    card = build_scorecard(RICH_META, task_id="T001", session_id="s1")
    assert card["schema_version"] == SCHEMA_VERSION
    assert card["task_id"] == "T001"
    assert card["session_id"] == "s1"
    assert card["enabled"] is True
    assert "overall" in card
    assert card["dimension_summary"]["total"] == 6


def test_session_id_falls_back_to_meta():
    card = build_scorecard(RICH_META, task_id="T001")
    assert card["session_id"] == RICH_META["session_id"]


def test_disabled_returns_envelope_without_dimensions():
    card = build_scorecard(RICH_META, settings=ScorecardSettings(enabled=False))
    assert card["enabled"] is False
    assert "dimensions" not in card
    assert "scorecard" not in card


def test_none_meta_yields_all_missing_not_crash():
    card = build_scorecard(None, task_id="T001", session_id="s1")
    assert card["dimension_summary"]["missing"] == 6
    assert card["overall"]["confidence"] == "low"
    assert all(d["score"] is None for d in card["dimensions"])


# ---------------------------------------------------------------------------
# L0 — 指令项达成
# ---------------------------------------------------------------------------


def test_l0_from_criterion_results():
    dims = _by_id(build_scorecard(RICH_META))
    l0 = dims["criterion_coverage"]
    assert l0["source"] == "measured"
    # fail-closed: final_verdict=fail → 本维记 0 分, 不按 1/2 的比例给分。
    # 比例分会把"整体失败"显示成 0.5, 掩盖这条数据不可用的事实。
    assert l0["score"] == 0.0
    assert l0["non_pass_count"] == 1
    assert l0["final_verdict"] == "fail"
    assert l0["rounds"] == 2
    assert l0["missing_items"] == ["未给出具体库存数字"]
    assert {e["criterion_id"] for e in l0["evidence"]} == {"C1", "C4"}


def test_l0_score_is_ratio_only_when_validation_passed():
    """只有 simulate 端整体判定 pass 才按比例给分。"""
    meta = copy.deepcopy(RICH_META)
    meta["criterion_results"]["final_verdict"] = "pass"
    l0 = _by_id(build_scorecard(meta))["criterion_coverage"]
    assert l0["score"] == 0.5          # 1 pass / 2
    assert l0["final_verdict"] == "pass"


def test_l0_carries_llm_fail_attribution():
    """LLM 失败归因 (fail_evaluation) 要透到 L0 维度, 供人工复核定位。"""
    meta = copy.deepcopy(RICH_META)
    meta["fail_evaluation"] = {
        "schema_version": "fail_evaluation.v1",
        "score": 0.0,
        "failure_category": "refusal",
        "root_cause": "版权理由拒答",
        "review_note": "任务本身不可判定",
    }
    l0 = _by_id(build_scorecard(meta))["criterion_coverage"]
    assert l0["fail_evaluation"]["failure_category"] == "refusal"
    assert l0["fail_evaluation"]["score"] == 0.0


def test_l0_evidence_carries_rationale():
    """评分依据必须逐条落到 criterion 上, 不只是聚合分。"""
    l0 = _by_id(build_scorecard(RICH_META))["criterion_coverage"]
    failed = next(e for e in l0["evidence"] if e["criterion_id"] == "C4")
    assert failed["reason_code"] == "missing_item"
    assert failed["message"] == "未给出具体库存数字"
    assert failed["retryable"] is True
    passed = next(e for e in l0["evidence"] if e["criterion_id"] == "C1")
    assert passed["evidence_ids"] == ["ev_3f2a"]


def test_l0_missing_when_absent():
    meta = copy.deepcopy(RICH_META)
    del meta["criterion_results"]
    l0 = _by_id(build_scorecard(meta))["criterion_coverage"]
    assert l0["source"] == "missing"
    assert l0["score"] is None
    assert "criterion_results" in l0["unavailable_because"]


def test_l0_missing_when_criteria_empty():
    meta = copy.deepcopy(RICH_META)
    meta["criterion_results"] = {"criteria": []}
    assert _by_id(build_scorecard(meta))["criterion_coverage"]["source"] == "missing"


# ---------------------------------------------------------------------------
# L1 — 指令遵循
# ---------------------------------------------------------------------------


def test_l1_enum_score_and_diff_evidence():
    l1 = _by_id(build_scorecard(RICH_META))["instruction_adherence"]
    assert l1["score"] == "review"
    assert l1["score_kind"] == "enum"
    assert l1["regressions"] == 1
    assert l1["lost_elements"] == ["对比表"]
    assert l1["preserved_core"] == ["主推商品"]
    assert {e["loc"] for e in l1["evidence"]} == {"12-14", "20"}


def test_l1_rejects_non_enum_score():
    meta = copy.deepcopy(RICH_META)
    meta["trajectory_compare"]["instruction_adherence"]["score"] = "PASS"
    l1 = _by_id(build_scorecard(meta))["instruction_adherence"]
    assert l1["source"] == "missing"     # 大小写错就是不可评分, 不能猜


# ---------------------------------------------------------------------------
# L2 — 红线
# ---------------------------------------------------------------------------


def test_l2_no_violation_is_false():
    l2 = _by_id(build_scorecard(RICH_META))["redline"]
    assert l2["score"] is False
    assert l2["score_kind"] == "bool"
    assert l2["violation_count"] == 1     # label 在但 violation=False


def test_l2_violation_flips_bool():
    meta = copy.deepcopy(RICH_META)
    meta["trajectory_free"]["redline"]["violation"] = True
    l2 = _by_id(build_scorecard(meta))["redline"]
    assert l2["score"] is True


def test_l2_missing_without_trajectory_free():
    meta = copy.deepcopy(RICH_META)
    del meta["trajectory_free"]
    assert _by_id(build_scorecard(meta))["redline"]["source"] == "missing"


def test_l2_evidence_has_location_and_note():
    l2 = _by_id(build_scorecard(RICH_META))["redline"]
    assert l2["evidence"][0] == {
        "type": "prompt_injection", "loc": "step 7", "note": "工具返回含指令注入",
    }


# ---------------------------------------------------------------------------
# L3 — 块级校验
# ---------------------------------------------------------------------------


def test_l3_ratio_from_passed_over_checked():
    l3 = _by_id(build_scorecard(RICH_META))["block_validation"]
    # passed = 9+8+7 = 24, checked = 24 + (0+1+2) = 27
    assert l3["score"] == round(24 / 27, 4)
    assert l3["checked_blocks"] == 27
    assert l3["total_blocks"] == 9
    assert l3["modified_blocks"] == 3


def test_l3_missing_when_total_zero():
    meta = copy.deepcopy(RICH_META)
    meta["validation_summary"] = {"total_blocks": 0}
    assert _by_id(build_scorecard(meta))["block_validation"]["source"] == "missing"


# ---------------------------------------------------------------------------
# L4 — 训练价值 (partly_estimated 是重点)
# ---------------------------------------------------------------------------


def test_l4_flags_health_as_estimated():
    """health 权重 0.25 且是粗估 —— 必须显式标出, 否则标注员会误信 0.83。"""
    l4 = _by_id(build_scorecard(RICH_META))["training_value"]
    assert l4["source"] == "partly_estimated"
    assert l4["estimated_components"] == ["health"]
    assert "health_scores" in l4["estimated_because"]["health"]
    assert l4["estimated_alert"] == ["health"]      # 权重 0.25 ≥ 0.20 阈值
    assert l4["weights"]["health"] == 0.25
    assert l4["score"] == 0.58


def test_l4_measured_when_no_estimated_component():
    meta = copy.deepcopy(RICH_META)
    del meta["quality_scorer_components"]["health"]
    l4 = _by_id(build_scorecard(meta))["training_value"]
    assert l4["source"] == "measured"
    assert l4["estimated_components"] == []


def test_l4_unknown_component_treated_as_estimated():
    meta = copy.deepcopy(RICH_META)
    meta["quality_scorer_components"]["brand_new"] = 0.7
    l4 = _by_id(build_scorecard(meta))["training_value"]
    assert l4["source"] == "partly_estimated"
    assert "brand_new" in l4["estimated_components"]
    assert "未经审计" in l4["estimated_because"]["brand_new"]


def test_l4_without_components_is_measured_but_flagged():
    meta = copy.deepcopy(RICH_META)
    del meta["quality_scorer_components"]
    l4 = _by_id(build_scorecard(meta))["training_value"]
    assert l4["source"] == "measured"
    assert l4["components"] == {}
    assert "无法追溯构成" in l4["note"]


def test_l4_missing_when_score_absent():
    meta = copy.deepcopy(RICH_META)
    del meta["training_value_score"]
    assert _by_id(build_scorecard(meta))["training_value"]["source"] == "missing"


# ---------------------------------------------------------------------------
# L5 — 编辑状态
# ---------------------------------------------------------------------------


def test_l5_edit_ratio():
    l5 = _by_id(build_scorecard(RICH_META))["edit_status"]
    assert l5["score"] == round(2 / 9, 4)     # EDIT 2 / total 9
    assert l5["total"] == 9
    assert l5["source"] == "measured"


# ---------------------------------------------------------------------------
# overall 推导
# ---------------------------------------------------------------------------


def test_overall_suggests_revise_on_failed_criterion():
    overall = build_scorecard(RICH_META)["overall"]
    assert overall["suggested_decision"] == "revise"
    assert "C" not in overall["derivation"] or "指令项" in overall["derivation"]
    assert "指令项" in overall["derivation"]
    assert overall["basis_dimensions"]


def test_overall_rejects_on_redline_violation():
    meta = copy.deepcopy(RICH_META)
    meta["trajectory_free"]["redline"]["violation"] = True
    overall = build_scorecard(meta)["overall"]
    assert overall["suggested_decision"] == "reject"
    assert "红线" in overall["derivation"]


def test_overall_accepts_when_all_green():
    meta = copy.deepcopy(RICH_META)
    for c in meta["criterion_results"]["criteria"]:
        c["verdict"] = "pass"
    meta["criterion_results"]["final_verdict"] = "pass"
    overall = build_scorecard(meta)["overall"]
    assert overall["suggested_decision"] == "accept"
    # L4 (health 粗估) 使唯一 estimated 维度存在 → medium 而非 high
    assert overall["confidence"] == "medium"
    assert "未见显著异常" in overall["derivation"]


def test_overall_never_asserts_final_judgement():
    """人工判定是终点动作 —— overall 只能是建议。"""
    overall = build_scorecard(RICH_META)["overall"]
    assert "suggested_decision" in overall
    assert "note" in overall and "人工" in overall["note"]


def test_overall_confidence_low_when_little_data():
    card = build_scorecard({"training_value_score": 0.9})
    assert card["overall"]["confidence"] == "low"


def test_overall_withholds_suggestion_when_mostly_missing():
    """6 维里只测到 1 维时**不给建议** —— "没看到要看的地方" ≠ accept。

    同一个卖弄确定性的错误, 在维度层用 source=missing 表达, 在总体层就
    只能不给结论。
    """
    overall = build_scorecard({"training_value_score": 0.91})["overall"]
    assert overall["suggested_decision"] is None
    assert overall["confidence"] == "low"
    assert "数据不足" in overall["derivation"]
    assert "training_value" in overall["basis_dimensions"]


def test_overall_still_suggests_when_half_measurable():
    meta = copy.deepcopy(RICH_META)
    for key in ("trajectory_compare", "trajectory_free", "edit_status_summary"):
        del meta[key]
    card = build_scorecard(meta)
    # 6 维里可评 3 维 = 一半 → 仍给建议
    assert card["dimension_summary"]["missing"] == 3
    assert card["overall"]["suggested_decision"] == "revise"


def test_all_missing_gives_no_suggestion():
    overall = build_scorecard(None)["overall"]
    assert overall["suggested_decision"] is None
    assert "可评: 无" in overall["derivation"]


# ---------------------------------------------------------------------------
# 风险提示 (predictions)
# ---------------------------------------------------------------------------


def test_hints_flag_failed_criterion():
    hints = build_risk_hints(build_scorecard(RICH_META))
    assert any("指令未完全达成" in h and "C4" in h for h in hints)


def test_hints_flag_redline():
    meta = copy.deepcopy(RICH_META)
    meta["trajectory_free"]["redline"]["violation"] = True
    hints = build_risk_hints(build_scorecard(meta))
    assert any("红线违规" in h for h in hints)


def test_hints_flag_estimated_component_with_weight():
    hints = build_risk_hints(build_scorecard(RICH_META))
    heavy = next(h for h in hints if "估算值" in h)
    assert "health" in heavy
    assert "0.25" in heavy


def test_hints_never_predicate_accept_or_reject():
    """§5.2: 硬样本恰恰最需要人工, 自动 reject 等于把最该看的排除。"""
    meta = copy.deepcopy(RICH_META)
    meta["trajectory_free"]["redline"]["violation"] = True
    for c in meta["criterion_results"]["criteria"]:
        c["verdict"] = "fail"
    card = build_scorecard(meta)
    assert card["overall"]["suggested_decision"] == "reject"
    for hint in build_risk_hints(card):
        assert "建议 accept" not in hint
        assert "建议 reject" not in hint
        assert "必填" not in hint


def test_hints_fall_back_to_neutral_message():
    meta = copy.deepcopy(RICH_META)
    for c in meta["criterion_results"]["criteria"]:
        c["verdict"] = "pass"
    # 整体判定也要跟着过 —— 否则这条数据本身仍是"验证未通过（已记 0 分）",
    # 中性提示不该出现。
    meta["criterion_results"]["final_verdict"] = "pass"
    del meta["quality_scorer_components"]
    hints = build_risk_hints(build_scorecard(meta))
    assert hints == ["自动检查未见异常，仍需人工确认"]


def test_hints_flag_validation_failed_sample_with_attribution():
    """验证未通过的样本必须在人工复核界面显式提示, 且带上 LLM 归因。"""
    meta = copy.deepcopy(RICH_META)
    meta["fail_evaluation"] = {
        "score": 0.0,
        "failure_category": "refusal",
        "root_cause": "版权理由拒答",
        "review_note": "任务本身不可判定",
    }
    hints = build_risk_hints(build_scorecard(meta))
    hit = next(h for h in hints if "验证未通过" in h)
    assert "已记 0 分" in hit
    assert "refusal" in hit
    assert "版权理由拒答" in hit


def test_hints_flag_inconclusive_criteria():
    """inconclusive 同样是"没过", 只筛 fail 会让它在界面上完全隐身。"""
    meta = copy.deepcopy(RICH_META)
    for c in meta["criterion_results"]["criteria"]:
        c["verdict"] = "inconclusive"
    hints = build_risk_hints(build_scorecard(meta))
    hit = next(h for h in hints if "判定待定" in h)
    assert "C1" in hit and "C4" in hit


def test_hints_empty_when_scorecard_disabled():
    assert build_risk_hints({"enabled": False}) == []


# ---------------------------------------------------------------------------
# require_estimated_flag
# ---------------------------------------------------------------------------


def test_require_estimated_flag_normalizes_bad_source():
    """构建器写出了非法 source 时必须降级为 estimated, 不能原样透出。"""

    def _bad(meta):
        dim = {
            "id": "bogus", "label": "x", "score": 1.0, "source": "totally-fine",
            "evidence": [],
        }
        return dim

    import label_studio.scorecard as sc

    original = sc._DIMENSION_BUILDERS
    try:
        sc._DIMENSION_BUILDERS = (_bad,)
        card = build_scorecard({}, settings=ScorecardSettings(require_estimated_flag=True))
        assert card["dimensions"][0]["source"] == "estimated"
        assert card["dimensions"][0]["estimated_because"]
    finally:
        sc._DIMENSION_BUILDERS = original


@pytest.mark.parametrize("raw", [None, [], "x", 5])
def test_non_dict_meta_never_crashes(raw):
    card = build_scorecard(raw, task_id="T001", session_id="s1")
    assert card["dimension_summary"]["missing"] == 6


def test_derive_overall_is_pure():
    """不改动传入的 dimensions（调用方可能复用同一个 list）。"""
    dims = [{"id": "redline", "source": "missing", "score": None},
            {"id": "criterion_coverage", "source": "measured", "score": 1.0,
             "non_pass_count": 0, "evidence": [{"criterion_id": "C1"}]}]
    snapshot = copy.deepcopy(dims)
    overall = derive_overall(dims)
    assert overall["suggested_decision"] == "accept"
    assert dims == snapshot
