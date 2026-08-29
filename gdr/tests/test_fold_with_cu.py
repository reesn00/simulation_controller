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


def test_fold_failed_keeps_all_successes(cfg, session_with_multiple_successes):
    """连续工具段: 保留所有成功, 只删除失败尝试。"""
    session = session_with_multiple_successes
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    removed = fold_failed_toolresults(session, cfg, cu=cu)
    ids_after = {b.id for msg in session.messages for b in msg.blocks}

    # tc1, tc2 是较早的成功, 现在必须保留
    assert "tc1" in ids_after, f"early success tc1 should be kept; got {ids_after}"
    assert "tc2" in ids_after, f"early success tc2 should be kept; got {ids_after}"
    # tc3 是失败, 且其后有成功收尾, 应被删除
    assert "tc3" not in ids_after
    # tc4 是最后一次成功, 必须保留
    assert "tc4" in ids_after
    assert removed >= 1


def test_fold_failed_keeps_last_attempt_of_trailing_failures(cfg):
    """段尾无成功收尾的失败串: 只保留最后一次尝试。"""
    from tests.conftest import _make_session

    def pair(bid: str, name: str, state: str) -> list[dict]:
        return [
            {"type": "toolcall", "id": bid, "name": name, "input": "{}", "state": "finished"},
            {"type": "toolresult", "id": bid, "name": name, "output_text": "o", "state": state},
        ]

    blocks: list[dict] = []
    # F F S  -> 保留 S
    blocks += pair("a1", "bash", "error")
    blocks += pair("a2", "bash", "error")
    blocks += pair("a3", "bash", "success")
    # F F F (段尾, 无 success) -> 只保留最后一个
    blocks += pair("b1", "browser", "error")
    blocks += pair("b2", "browser", "error")
    blocks += pair("b3", "browser", "error")

    session = _make_session([blocks])
    fold_failed_toolresults(session, cfg)
    ids_after = {b.id for msg in session.messages for b in msg.blocks}

    assert ids_after == {"a3", "b3"}, ids_after


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
