"""P0-1.2: quality_scorer 单测。

覆盖:
  - 各分量默认值 (缺失 metadata 时给中性 0.5)
  - intent_fulfillment_score {0,1,2} 归一化
  - tier 分桶阈值
  - tier_distribution 批量统计
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.quality_scorer import compute_quality_score, compute_tier_distribution
from domain import Session, Message, ThinkingBlock, ToolcallBlock, ToolresultBlock, TextBlock


def _basic_session(msg_count: int = 3, toolcall_count: int = 2) -> Session:
    """构造一个最小 session: user + 若干 assistant, 含 toolcall/toolresult。"""
    msgs = [
        Message(role="user", id="u1", blocks=[TextBlock(type="text", id="utx", text="q")]),
    ]
    for i in range(msg_count):
        blocks = [
            ThinkingBlock(type="thinking", id=f"th{i}", thinking="plan"),
        ]
        for j in range(toolcall_count):
            blocks.append(ToolcallBlock(
                type="toolcall", id=f"tc{i}-{j}",
                name=f"tool_{j}", input="{}", state="finished",
            ))
            blocks.append(ToolresultBlock(
                type="toolresult", id=f"tc{i}-{j}",
                name=f"tool_{j}", output_text="ok", state="success",
            ))
        msgs.append(Message(role="assistant", id=f"a{i}", blocks=blocks))
    return Session(session_id="t", messages=msgs, metadata={})


def _cfg(**overrides) -> SimpleNamespace:
    base = dict(
        enable_quality_scorer=True,
        quality_scorer_weight_health=0.25,
        quality_scorer_weight_judge=0.25,
        quality_scorer_weight_intent=0.20,
        quality_scorer_weight_modified=0.10,
        quality_scorer_weight_diversity=0.10,
        quality_scorer_weight_noise=0.05,
        quality_scorer_weight_depth=0.05,
        quality_scorer_tier_easy_max=0.70,
        quality_scorer_tier_medium_max=0.40,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_basic_session_returns_mid_score_and_tier():
    s = _basic_session()
    out = compute_quality_score(s, _cfg())
    assert 0.0 <= out["training_value_score"] <= 1.0
    assert out["complexity_tier"] in {"easy", "medium", "hard"}
    assert s.metadata["training_value_score"] == out["training_value_score"]
    assert s.metadata["complexity_tier"] == out["complexity_tier"]
    assert "components" in out and len(out["components"]) == 7


def test_disabled_quality_scorer_returns_empty():
    cfg = _cfg(enable_quality_scorer=False)
    s = _basic_session()
    out = compute_quality_score(s, cfg)
    assert out == {}
    assert "training_value_score" not in (s.metadata or {})


def test_intent_fulfillment_normalization():
    """intent_fulfillment_score {0,1,2} → {0.0, 0.5, 1.0}。"""
    cases = [
        (0, 0.0),
        (1, 0.5),
        (2, 1.0),
        (None, 0.5),   # 缺失
        ("bad", 0.5),  # 非法
        (5, 0.5),      # 越界
    ]
    for raw, expected in cases:
        s = _basic_session()
        if raw is not None:
            s.metadata["user_intent_fulfillment_score"] = raw
        out = compute_quality_score(s, _cfg())
        assert out["components"]["intent"] == expected, f"intent={raw}"


def test_tier_bucketing_thresholds():
    """intent {0,1,2} → score {0.0,0.5,1.0} → tier {hard, medium, easy}。

    通过把所有权重打到 intent 上, 让 weighted score 完全等于 intent 分量,
    验证分桶阈值 (medium_max=0.40, easy_max=0.70) 的正确性.
    """
    cases = [
        (0, "hard"),    # score=0.0 < 0.40 → hard
        (1, "medium"),  # score=0.5 ∈ [0.40, 0.70) → medium
        (2, "easy"),    # score=1.0 ≥ 0.70 → easy
    ]
    for intent_score, expected_tier in cases:
        s = _basic_session()
        cfg = _cfg(
            quality_scorer_weight_health=0.0,
            quality_scorer_weight_judge=0.0,
            quality_scorer_weight_intent=1.0,
            quality_scorer_weight_modified=0.0,
            quality_scorer_weight_diversity=0.0,
            quality_scorer_weight_noise=0.0,
            quality_scorer_weight_depth=0.0,
        )
        s.metadata["user_intent_fulfillment_score"] = intent_score
        out = compute_quality_score(s, cfg)
        assert out["complexity_tier"] == expected_tier, (
            f"intent={intent_score} score={out['training_value_score']} "
            f"got tier={out['complexity_tier']} expected={expected_tier}"
        )


def test_tier_distribution_counts_correctly():
    sessions = []
    for tier in ("easy", "medium", "hard", "easy", "easy", "unknown"):
        s = _basic_session()
        s.metadata = {"complexity_tier": tier}
        sessions.append(s)
    sessions.append(None)  # None 安全跳过
    out = compute_tier_distribution(sessions)
    assert out == {"easy": 3, "medium": 1, "hard": 1}


def test_modified_ratio_low_score_high():
    """modified_blocks 接近 0 时 modified 分量 → 1.0 (原始好, 训练价值高)。"""
    s = _basic_session()
    s.metadata["validation_summary"] = {"total_blocks": 100, "modified_blocks": 5}
    out = compute_quality_score(s, _cfg(quality_scorer_weight_modified=1.0))
    assert out["components"]["modified"] == pytest.approx(0.95, abs=1e-4)


def test_modified_ratio_high_score_low():
    """modified_blocks 接近 total 时 modified 分量 → 0.0 (训练价值低)。"""
    s = _basic_session()
    s.metadata["validation_summary"] = {"total_blocks": 100, "modified_blocks": 95}
    out = compute_quality_score(s, _cfg(quality_scorer_weight_modified=1.0))
    assert out["components"]["modified"] == pytest.approx(0.05, abs=1e-4)


def test_depth_peaks_at_12_messages():
    """总消息数=12 (1 user + 11 assistant) 时 depth 分量 ≈ 1.0。

    算法 1 - abs(msg_count - 12) / 12, 在 msg_count=12 时取最大值 1.0.
    """
    s = _basic_session(msg_count=11)  # user + 11 assistant = 12 messages
    out = compute_quality_score(s, _cfg(quality_scorer_weight_depth=1.0))
    assert out["components"]["depth"] == pytest.approx(1.0, abs=1e-4)


def test_tool_diversity_low_when_same():
    """所有 toolcall 同名时 diversity = 1/n (n = toolcall 总数)。

    构造 1 个 assistant + 2 个同名 toolcall, diversity = 1/2 = 0.5.
    """
    s = _basic_session(msg_count=1, toolcall_count=2)
    for m in s.messages:
        if m.role != "assistant":
            continue
        for b in m.blocks:
            btype = b.get("type", "") if isinstance(b, dict) else getattr(b, "type", "")
            if btype in ("toolcall", "toolresult"):
                if isinstance(b, dict):
                    b["name"] = "same_tool"
                else:
                    setattr(b, "name", "same_tool")
    out = compute_quality_score(s, _cfg(quality_scorer_weight_diversity=1.0))
    assert out["components"]["diversity"] == pytest.approx(0.5, abs=1e-4)


def test_tool_diversity_high_when_unique():
    """所有 toolcall 异名时 diversity = 1.0。"""
    s = _basic_session(msg_count=1, toolcall_count=2)  # tool_0 + tool_1
    out = compute_quality_score(s, _cfg(quality_scorer_weight_diversity=1.0))
    assert out["components"]["diversity"] == pytest.approx(1.0, abs=1e-4)