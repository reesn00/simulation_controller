"""Test fold functions respect ContextUnderstanding references."""
from __future__ import annotations

import pytest
from context_understanding import build_context_for_session
from reassembly.reassembler import fold_failed_toolresults, fold_repeated_thinking
from routing.health import light_health_score_for_session


def test_fold_failed_keeps_referenced_block(cfg, session_with_referenced_failed_block):
    session = session_with_referenced_failed_block
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    ids_before = {b.id for msg in session.messages for b in msg.blocks}
    removed = fold_failed_toolresults(session, cfg, cu=cu)
    ids_after = {b.id for msg in session.messages for b in msg.blocks}

    # 失败组中 tc1/tc1-result 被后续 thinking 引用，应保留
    assert "tc1" in ids_after
    # tc2 成功，必然保留
    assert "tc2" in ids_after
    # 至少没有误删成功对
    assert removed == 0


def test_fold_failed_deletes_unreferenced_error(cfg, session_with_failed_retry):
    session = session_with_failed_retry
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    removed = fold_failed_toolresults(session, cfg, cu=cu)
    ids_after = {b.id for msg in session.messages for b in msg.blocks}

    # tc1 失败且无引用，tc1 的 toolcall 应被删除（toolresult 无独立 id 或同 id 已删）
    assert "tc1" not in ids_after
    assert "tc2" in ids_after
    assert removed >= 1


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
