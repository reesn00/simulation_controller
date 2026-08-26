"""Test Router.tag can inject ContextUnderstanding archive into LLM prompt."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from context_understanding import build_context_for_session
from domain import Session, Message
from routing import Router
from routing import router as router_module
from routing.health import light_health_score_for_session


def _session(blocks_by_msg: list[list[dict]]) -> Session:
    messages = []
    for i, blocks in enumerate(blocks_by_msg):
        role = "assistant" if i % 2 == 0 else "user"
        messages.append(Message(role=role, id=f"msg-{i}", blocks=blocks))
    return Session(session_id="router-test", messages=messages)


def test_router_tag_uses_cu_context(cfg):
    # 构造一条消息，使其健康但又命中规则层缺陷，从而进入 LLM 投票
    cfg = cfg.model_copy(update={"enable_llm_layer": True})
    session = _session([
        [
            {"type": "thinking", "id": "th1", "thinking": "a" * 30},
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"q":"x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"},
            {"type": "toolresult", "id": "tr2", "name": "browser", "output_text": "DEBUG info", "state": "success"},
        ],
    ])
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    router = Router()
    original_build = router._build_review_context
    calls = []

    def class_spy(self, session, msg_idx, block_idx, block_id, strategy, context_understanding=None, cfg=None):
        calls.append(context_understanding)
        # original_build 是 bound method, self 已绑定
        return original_build(session, msg_idx, block_idx, block_id, strategy, context_understanding, cfg)

    router_module.Router._build_review_context = class_spy
    try:
        with patch("infrastructure.LlamaCppClient") as mock_llm:
            mock_llm.get.return_value.generate.return_value = ('{"has_defect": false}', None)
            router.tag(session, ["browser"], set(), cfg, context_understanding=cu)
    finally:
        router_module.Router._build_review_context = original_build

    # 当 candidate_blocks 为空时 _build_review_context 不会被调用；
    # 此处 tr2 命中 OBS_DEBUG_LEAK，应进入 LLM 投票并调用上下文构建。
    assert calls
    assert cu in calls


def test_build_review_context_prefers_cu_when_enabled(cfg):
    session = _session([
        [
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": "{}", "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"},
        ],
    ])
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    router = Router()
    ctx = router._build_review_context(
        session, 0, 0, "tc1", "±1",
        context_understanding=cu, cfg=cfg,
    )
    assert ctx
    assert "T0" in ctx or "browser" in ctx or "tc1" in ctx


def test_build_review_context_falls_back_when_disabled(cfg):
    session = _session([
        [
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": "{}", "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"},
        ],
    ])
    cfg.llm_vote_use_cu = False
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    router = Router()
    ctx = router._build_review_context(
        session, 0, 1, "tc1-result", "±1",
        context_understanding=cu, cfg=cfg,
    )
    # 旧 surrounding context 路径会包含前后 block 标签
    assert "前" in ctx or "后" in ctx
