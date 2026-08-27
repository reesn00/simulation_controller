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
        context_state_tracker_enabled=False,  # 单测纯本地, 不触发状态追踪 LLM 调用
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
    """失败 toolresult 被后续 message 的 text/thinking 引用, 不应被 fold 删除。

    拆成两条 assistant message 才能验证"跨 message 的 active-text 引用"算保护;
    同 message 内的 thinking 只是决策上下文, 不构成对失败调用的实质依赖。

    注意: output_text 使用纯 ASCII 避免 "9.9元" 这种 CJK 字符干扰
    Python re 的 \\b 边界 (CJK 字符在 \\w 中), 进而让 entity 共指
    失效导致引用关系丢失。
    """
    return _make_session([
        # msg[0] assistant: 失败 toolcall
        [
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"url": "http://x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "price 9.9 dollars", "state": "error"},
        ],
        # msg[1] user: 桥接消息, 让 th1 处于不同 message
        [],
        # msg[2] assistant: thinking 引用 tc1 的实体, 然后成功 toolcall
        [
            {"type": "thinking", "id": "th1", "thinking": "noted price 9.9 dollars"},
            {"type": "toolcall", "id": "tc2", "name": "browser", "input": '{"url": "http://x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc2", "name": "browser", "output_text": "price 9.9 dollars", "state": "success"},
        ],
    ])


@pytest.fixture
def session_with_multiple_successes(cfg) -> Session:
    """连续多次同名成功 + 一个错误, 应只保留最后一次成功。"""
    return _make_session([
        [
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"url": "a"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok-a", "state": "success"},
            {"type": "toolcall", "id": "tc2", "name": "browser", "input": '{"url": "b"}', "state": "finished"},
            {"type": "toolresult", "id": "tc2", "name": "browser", "output_text": "ok-b", "state": "success"},
            {"type": "toolcall", "id": "tc3", "name": "browser", "input": '{"url": "c"}', "state": "finished"},
            {"type": "toolresult", "id": "tc3", "name": "browser", "output_text": "err-c", "state": "error"},
            {"type": "toolcall", "id": "tc4", "name": "browser", "input": '{"url": "d"}', "state": "finished"},
            {"type": "toolresult", "id": "tc4", "name": "browser", "output_text": "ok-d", "state": "success"},
        ],
    ])
