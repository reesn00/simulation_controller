"""Test ContextUnderstanding consumes light health scores."""
from __future__ import annotations

import pytest
from context_understanding import build_context_for_session
from routing.health import light_health_score_for_session


def test_cu_uses_light_health_for_window(cfg, session_with_failed_retry):
    session = session_with_failed_retry
    cfg.message_health_min_ratio = 0.5
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)
    # 轻量健康分 0.45 < 0.5，CU active window 应标记为不健康
    snapshots = cu._block_index["tc1"].active_window
    assert snapshots
    assert snapshots[0].is_healthy is False


def test_cu_render_archive_for_block_referenced(cfg, session_with_referenced_failed_block):
    session = session_with_referenced_failed_block
    # 把所有消息移出 active window，确保进入 archive
    cfg.context_active_window_size = 0
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)
    # 9.9 数字导致 thinking 与 toolresult 共指，失败 result 应被引用
    view = cu.get_view("tc1")
    assert view is not None
    assert view.referenced_by

    rendered = cu.render_archive_for_block("tc1", strategy="referenced")
    assert rendered
    # 摘要可能截断小数，但应包含 price 或 9 字样
    assert "price" in rendered or "9" in rendered
