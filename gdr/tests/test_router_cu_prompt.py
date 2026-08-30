"""Test Router.tag can inject ContextUnderstanding archive into LLM prompt."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from core.context_understanding import build_context_for_session
from domain import Session, Message, DefectTag
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
    # 构造一条消息，使其健康但 thinking 命中 THOUGHT_TOO_LONG 规则缺陷:
    # TOO_LONG 是唯一"追加语义标签会改变决策"的规则缺陷 (BROKEN_LOGIC 可把
    # PRUNE 翻成 REPAIR/DEFER), 因此进入级联投票并调用上下文构建。
    cfg = cfg.model_copy(update={"enable_llm_layer": True})
    session = _session([
        [
            {"type": "thinking", "id": "th1", "thinking": "a" * 600},
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

    def class_spy(*args, **kwargs):
        # _build_review_context(self, session, msg_idx, block_idx, block_id, strategy, ...)
        # 通过类赋值绑定了 self, 转发前剥离, 保证真实方法被执行
        calls.append(kwargs.get("context_understanding"))
        return original_build(*args[1:], **kwargs)

    router_module.Router._build_review_context = class_spy
    try:
        with patch("infrastructure.LlamaCppClient") as mock_llm:
            mock_llm.get.return_value.chat.return_value = ('{"has_defect": false}', None)
            router.tag(session, ["browser"], set(), cfg, context_understanding=cu)
    finally:
        router_module.Router._build_review_context = original_build

    # tr2 只含 OBS_DEBUG_LEAK (确定性标签) 应被跳过; th1 (TOO_LONG) 应进入投票
    assert calls
    assert cu in calls
    assert all(c is cu for c in calls)


def test_vote_skips_rule_decidable_blocks(cfg):
    """只含确定性标签的块不应进入 LLM 投票 (llm_vote_skip_rule_decidable=True)。"""
    cfg = cfg.model_copy(update={"enable_llm_layer": True})
    session = _session([
        [
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": "not-json", "state": "finished"},
            {"type": "toolresult", "id": "tr2", "name": "browser", "output_text": "DEBUG info", "state": "success"},
        ],
    ])
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    router = Router()
    calls = []
    original_build = router._build_review_context

    def spy(*args, **kwargs):
        calls.append(args)
        return original_build(*args[1:], **kwargs)

    router_module.Router._build_review_context = spy
    try:
        with patch("infrastructure.LlamaCppClient") as mock_llm:
            router.tag(session, ["browser"], set(), cfg, context_understanding=cu)
    finally:
        router_module.Router._build_review_context = original_build

    # tc1 (TOOL_JSON_INVALID) 与 tr2 (OBS_DEBUG_LEAK) 都不应触发投票
    assert not calls
    mock_llm.get.assert_not_called()


def test_vote_cascades_confirm_only_on_yes(cfg):
    """级联投票: 首票 false 不补票; 首票 true 才用 surrounding 上下文补确认票。"""
    cfg = cfg.model_copy(update={"enable_llm_layer": True})
    session = _session([
        [
            {"type": "thinking", "id": "th1", "thinking": "a" * 600},
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"q":"x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"},
        ],
    ])
    light = light_health_score_for_session(session, cfg)
    cu = build_context_for_session(session, cfg, light_health=light)

    router = Router()
    contexts = []
    original_build = router._build_review_context

    def spy(*args, **kwargs):
        # 剥离类赋值自动绑定的 self 后转发, 让真实方法执行
        ctx = original_build(*args[1:], **kwargs)
        contexts.append((kwargs.get("force_surrounding", False), ctx))
        return ctx

    router_module.Router._build_review_context = spy
    try:
        with patch("infrastructure.LlamaCppClient") as mock_llm:
            client = mock_llm.get.return_value
            # 首票 false → 不应有第二次调用
            client.chat.return_value = ('{"has_defect": false}', None)
            defects, _ = router.tag(session, ["browser"], set(), cfg, context_understanding=cu)
            assert len(contexts) == 1
            assert contexts[0][0] is False
            assert DefectTag.THOUGHT_BROKEN_LOGIC not in defects.get("th1", [])

            # 首票 true + 确认票 true → 标记; 确认票强制 surrounding
            contexts.clear()
            client.chat.side_effect = [
                ('{"has_defect": true}', None),
                ('{"has_defect": true}', None),
            ]
            defects, _ = router.tag(session, ["browser"], set(), cfg, context_understanding=cu)
            assert len(contexts) == 2
            assert contexts[0][0] is False and contexts[1][0] is True
            assert DefectTag.THOUGHT_BROKEN_LOGIC in defects.get("th1", [])

            # 首票 true + 确认票 false → 保守不标记
            contexts.clear()
            client.chat.side_effect = [
                ('{"has_defect": true}', None),
                ('{"has_defect": false}', None),
            ]
            defects, _ = router.tag(session, ["browser"], set(), cfg, context_understanding=cu)
            assert len(contexts) == 2
            assert DefectTag.THOUGHT_BROKEN_LOGIC not in defects.get("th1", [])
    finally:
        router_module.Router._build_review_context = original_build


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
