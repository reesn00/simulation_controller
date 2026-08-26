"""Test fold functions respect ContextUnderstanding references."""
from __future__ import annotations

import pytest
from core.context_understanding import build_context_for_session
from reassembly.reassembler import fold_failed_toolresults, fold_repeated_thinking
from routing.health import light_health_score_for_session


def test_fold_failed_keeps_referenced_block(cfg, session_with_referenced_failed_block):
    session = session_with_referenced_failed_block
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    removed = fold_failed_toolresults(session, cfg, cu=cu)
    ids_after = {b.id for msg in session.messages for b in msg.blocks}

    # th1 在 active window 且是 thinking 类型, 通过 entity 共指引用了 tc1 的 toolresult
    # (含 "9.9 dollars"), 所以 tc1 受 referenced_by_active_text 保护
    assert "tc1" in ids_after, f"tc1 should be protected by active text ref; got {ids_after}"
    # tc2 是最后一次成功, 必须保留
    assert "tc2" in ids_after
    # 至少没有误删被引用的失败对
    assert removed == 0


def test_fold_failed_deletes_unreferenced_error(cfg, session_with_failed_retry):
    session = session_with_failed_retry
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    removed = fold_failed_toolresults(session, cfg, cu=cu)
    ids_after = {b.id for msg in session.messages for b in msg.blocks}

    # tc1 失败且无 active-text 引用, 应被删除
    assert "tc1" not in ids_after
    # tc2 是唯一成功, 必须保留
    assert "tc2" in ids_after
    assert removed >= 1


def test_fold_failed_keeps_only_last_success(cfg, session_with_multiple_successes):
    """同组多次成功 + 中间错误, 仅保留最后一次成功。"""
    session = session_with_multiple_successes
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    removed = fold_failed_toolresults(session, cfg, cu=cu)
    ids_after = {b.id for msg in session.messages for b in msg.blocks}

    # tc1, tc2 是中间的成功, 应被删除
    assert "tc1" not in ids_after, f"early success tc1 should be deleted; got {ids_after}"
    assert "tc2" not in ids_after, f"early success tc2 should be deleted; got {ids_after}"
    # tc3 是错误, 应被删除
    assert "tc3" not in ids_after
    # tc4 是最后一次成功, 必须保留
    assert "tc4" in ids_after
    assert removed >= 3


def test_fold_repeated_thinking_keeps_referenced(cfg, session_with_repeated_thinking):
    session = session_with_repeated_thinking
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    # 人为把 th1 标记为被引用
    cu._block_index["th1"].referenced_by = ["tc1"]

    removed = fold_repeated_thinking(session, cfg, cu=cu)
    ids_after = {b.id for msg in session.messages for b in msg.blocks}

    # th1 被引用，th2 是连续组最后一条，都应保留
    assert "th1" in ids_after
    assert "th2" in ids_after
    assert removed == 0
