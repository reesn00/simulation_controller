"""P0-1.1: user_intent 抽取层单测。

覆盖:
  - heuristic_user_intent  零 LLM 截断行为 + 边界条件
  - extract_user_intent_llm  LLM 失败 fallback 到 heuristic
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from core.user_intent import heuristic_user_intent, extract_user_intent_llm
from domain import Session, Message, TextBlock


def _session_with_user(user_text: str | None) -> Session:
    if user_text is None:
        return Session(session_id="t", messages=[
            Message(role="assistant", id="a1", blocks=[
                TextBlock(type="text", id="tx1", text="hi"),
            ]),
        ])
    return Session(session_id="t", messages=[
        Message(role="user", id="u1", blocks=[
            TextBlock(type="text", id="utx", text=user_text),
        ]),
        Message(role="assistant", id="a1", blocks=[
            TextBlock(type="text", id="atx", text="ok"),
        ]),
    ])


def test_heuristic_returns_full_text_when_within_limit():
    s = _session_with_user("查询北京天气")
    out = heuristic_user_intent(s, max_chars=100, min_chars=5)
    assert out == "查询北京天气"


def test_heuristic_truncates_to_max_chars():
    long = "x" * 5000
    s = _session_with_user(long)
    out = heuristic_user_intent(s, max_chars=500, min_chars=5)
    assert out is not None
    assert len(out) <= 500


def test_heuristic_returns_none_when_too_short():
    s = _session_with_user("hi")
    out = heuristic_user_intent(s, max_chars=500, min_chars=20)
    assert out is None


def test_heuristic_returns_none_when_no_user_message():
    s = _session_with_user(None)
    out = heuristic_user_intent(s, max_chars=500, min_chars=5)
    assert out is None


def test_extract_llm_falls_back_to_heuristic_on_failure():
    """LLM 抛异常时降级到 heuristic_user_intent 返回原文。"""
    s = _session_with_user("查询天气并给出建议" * 5)
    cfg = SimpleNamespace(
        enable_user_intent_extraction=True,
        user_intent_max_chars=1500,
        user_intent_min_chars_for_extract=20,
        user_intent_model=None,
        user_intent_max_tokens=1024,
        llm_timeout_s=120,
        main_model="m",
        llm_base_url="http://x", llm_api_key="x",
    )
    # 让 infrastructure.LlamaCppClient.get 抛异常, 触发 fallback
    fake_module = mock.MagicMock()
    fake_module.LlamaCppClient.get.side_effect = RuntimeError("boom")
    with mock.patch.dict("sys.modules", {"infrastructure": fake_module}):
        out = extract_user_intent_llm(s, cfg)
    assert isinstance(out, str)
    assert "查询天气" in out


def test_extract_llm_skips_when_disabled():
    """enable_user_intent_extraction=False 时直接走 heuristic (零 LLM)."""
    s = _session_with_user("查询天气" * 5)
    cfg = SimpleNamespace(
        enable_user_intent_extraction=False,
        user_intent_max_chars=1500,
        user_intent_min_chars_for_extract=20,
    )
    out = extract_user_intent_llm(s, cfg)
    assert out is not None
    assert "查询天气" in out


def test_extract_llm_returns_empty_when_no_user():
    s = _session_with_user(None)
    cfg = SimpleNamespace(
        enable_user_intent_extraction=True,
        user_intent_max_chars=1500,
        user_intent_min_chars_for_extract=20,
        user_intent_model=None,
        user_intent_max_tokens=1024,
        llm_timeout_s=120,
        main_model="m",
    )
    out = extract_user_intent_llm(s, cfg)
    assert out == ""