"""Shared pytest fixtures."""
from __future__ import annotations

import pytest
from config import Settings
from domain import Session, Message, ThinkingBlock, ToolcallBlock, ToolresultBlock


@pytest.fixture
def cfg() -> Settings:
    return Settings(
        enable_llm_layer=False,
        enable_context_understanding=True,
        llm_vote_use_cu=True,
        cu_prompt_archive_strategy="referenced",
        fold_use_cu=True,
        context_active_window_size=2,
        context_max_archive_chars=4000,
    )


def _make_session(blocks_by_msg: list[list[dict]]) -> Session:
    messages = []
    for i, blocks in enumerate(blocks_by_msg):
        role = "assistant" if i % 2 == 0 else "user"
        messages.append(Message(role=role, id=f"msg-{i}", blocks=blocks))
    return Session(session_id="test-session", messages=messages)


@pytest.fixture
def session_with_failed_retry(cfg) -> Session:
    return _make_session([
        [
            {"type": "thinking", "id": "t1", "thinking": "plan to search"},
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": "{}", "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "error", "state": "error"},
            {"type": "toolcall", "id": "tc2", "name": "browser", "input": "{}", "state": "finished"},
            {"type": "toolresult", "id": "tc2", "name": "browser", "output_text": "ok", "state": "success"},
        ],
    ])


@pytest.fixture
def session_with_repeated_thinking(cfg) -> Session:
    return _make_session([
        [
            {"type": "thinking", "id": "th1", "thinking": "first thought"},
            {"type": "thinking", "id": "th2", "thinking": "second thought"},
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": "{}", "state": "finished"},
        ],
    ])


@pytest.fixture
def session_with_referenced_failed_block(cfg) -> Session:
    """失败 toolresult 被后续 text block 引用, 不应被 fold 删除。"""
    return _make_session([
        [
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"url": "http://x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "price is 9.9元", "state": "error"},
            {"type": "thinking", "id": "th1", "thinking": "mentioned 9.9"},
            {"type": "toolcall", "id": "tc2", "name": "browser", "input": '{"url": "http://x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc2", "name": "browser", "output_text": "price is 9.9元", "state": "success"},
        ],
    ])
