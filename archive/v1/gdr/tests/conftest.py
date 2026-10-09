"""Shared pytest fixtures."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from config import Settings
from domain import Session, Message, ThinkingBlock, ToolcallBlock, ToolresultBlock

# Windows: 抑制子进程弹控制台窗口 (与 ``tests/conftest.py`` 同一套策略)。
# gdr/tests 独立于 tests/ 收集时不会加载那边的 conftest, 漏装会让
# multiprocessing.Pool worker 每跑一次弹一个黑窗。
#
# 刻意**不**往 sys.path 里插仓库根: gdr 的 ``from config import Settings``
# 要求 ``gdr/`` 优先于仓库根, 而仓库根也有一个 ``config/`` 目录(根配置)。
# 插进去会让 gdr/config 被遮蔽, Settings 加载变成 load_error。
# 正常入口是仓库根跑 ``uv run python -m pytest``, cwd 即根, 天然可导入;
# 真跑不到 (如 cd gdr 单独跑) 就静默跳过 —— 弹窗难看, 但绝不该让测试失败。
try:
    from orchestration._windows import (
        install_no_window_policy,
        install_subprocess_no_window_policy,
    )

    install_no_window_policy()
    install_subprocess_no_window_policy()
except Exception:  # pragma: no cover - 兜底,绝不阻塞测试启动
    pass


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
        # 阶梯阈值: 既有测试断言基于单层 (relaxed_min=3 for modified<=5),
        # 关闭新增 passthrough / low_edit 两档以保持既有测试通过; 阶梯行为
        # 由 tests/test_judge_relaxation.py 单独覆盖.
        judge_min_modified_passthrough=0,
        judge_min_modified_low_edit=0,
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
def session_with_parallel_calls(cfg) -> Session:
    """并行工具调用: 新格式 trajectory 中 result 按完成序返回,
    与 call 顺序不一致 (call, call, result, result)。"""
    return _make_session([
        [
            {"type": "thinking", "id": "th1", "thinking": "search two sources"},
            {"type": "toolcall", "id": "tc1", "name": "web_search", "input": '{"q": "a"}', "state": "finished"},
            {"type": "toolcall", "id": "tc2", "name": "web_search", "input": '{"q": "b"}', "state": "finished"},
            {"type": "toolresult", "id": "tc2", "name": "web_search", "output_text": "ok-b", "state": "success"},
            {"type": "toolresult", "id": "tc1", "name": "web_search", "output_text": "ok-a", "state": "success"},
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


# ---------------------------------------------------------------------------
# C1 trajectory 事件流 fixture
# ---------------------------------------------------------------------------
#
# 2026-09-18 起 C1 是**单路径事件流**（JSONL，一行一个事件对象），
# `gdr.parsers.from_trajectory` 重放 `turn_start` / `model_request` /
# `model_response` / `tool_execution` / `final_reply` 重建 Session。
#
# 在此之前 gdr 曾只消费 etl 导出的「嵌套 Session JSON」(qf_out 形态)，
# 测试 fixture 也照着写；格式演进后那些 fixture 全部命中
# ``trajectory events yielded no messages`` —— 因为嵌套格式里没有
# 任何 ``event_type``。跑 gdr 单测时才发现。

_TS = "2026-09-30T00:00:00.000000+00:00"


def _event(event_type: str, payload: dict, session_id: str, **over) -> dict:
    """构造一个 C1 事件信封（契约 §3 的必填字段齐全）。"""
    ev = {
        "trace_id": f"trace-{session_id}",
        "span_id": f"span-{session_id}-{event_type}",
        "parent_span_id": None,
        "event_type": event_type,
        "timestamp": _TS,
        "session_id": session_id,
        "agent_id": "default",
        "user_id": "tester",
        "channel": "web",
        "provider_id": "dashscope",
        "model_name": "qwen3-max",
        "payload": payload,
        "metadata": {},
    }
    ev.update(over)
    return ev


def write_c1_trajectory(
    path: Path,
    *,
    session_id: str = "s1",
    system_prompt: str = "",
    user_text: str = "hi",
    thinking_text: str = "",
    assistant_text: str = "你好",
    tools: list | None = None,
    tool_calls: list[tuple[str, str, str, str]] | None = None,
    with_terminal: bool = True,
) -> Path:
    """写一份**可被 ``from_trajectory`` 重放**的最小 C1 事件流, 返回路径。

    事件序列: ``turn_start`` → ``model_request`` → ``model_response``
    → （每个 tool_call 一个 ``tool_execution``）→ ``final_reply``。

    Args:
        path: 输出文件（``.json``，内容是 JSONL 事件流）。
        session_id: 事件信封里的 session_id。
        system_prompt: 非空时经 ``model_request.payload.messages`` 传入 ——
            重放会把它提为 messages[0] 的 system message。
        thinking_text / assistant_text: assistant 轮的 thinking / text 块。
        tools: ``model_request.payload.tools``；**最后一次非空**的胜出。
        tool_calls: ``[(tool_call_id, name, input_json, output)]``；每项产出一对
            ``model_response.content`` 的 tool_call 块 + 一个 ``tool_execution``。
            usage_prune 靠它判断「哪些工具真被调用过」。
        with_terminal: False 时**不写** ``final_reply``，用于构造未收尾的
            残缺轨迹。
    """
    tool_calls = tool_calls or []
    blocks: list[dict] = []
    if thinking_text:
        blocks.append({"type": "thinking", "id": "th1", "thinking": thinking_text})
    for call_id, name, call_input, _out in tool_calls:
        blocks.append({
            "type": "tool_call", "id": call_id, "name": name,
            "input": call_input, "state": "pending",
        })
    if assistant_text:
        blocks.append({"type": "text", "id": "tx1", "text": assistant_text})

    events = [
        _event("turn_start", {"input_text": user_text}, session_id),
        _event(
            "model_request",
            {
                # content 必须是 **ContentBlock 列表**而非裸字符串:
                # load._content_blocks_text 见到非 list 直接返回 "", system prompt
                # 会静默变空 (踩过: 写字符串时 usage_prune 报 system_chars_before=0)
                "messages": [
                    *(
                        [{"role": "system", "content": [{"type": "text", "text": system_prompt}]}]
                        if system_prompt
                        else []
                    ),
                    {"role": "user", "content": [{"type": "text", "text": user_text}]},
                ],
                "tools": tools or [],
            },
            session_id,
        ),
        _event(
            "model_response",
            {"content": blocks, "usage": {"total_tokens": 32}, "finished_reason": "stop"},
            session_id,
        ),
    ]
    for call_id, name, call_input, out in tool_calls:
        events.append(_event(
            "tool_execution",
            {"tool_call_id": call_id, "tool_name": name, "input": call_input, "output": out},
            session_id,
            metadata={"end_state": "success"},
        ))
    if with_terminal:
        events.append(_event(
            "final_reply",
            {"content": [{"type": "text", "id": "tx1", "text": assistant_text}]},
            session_id,
            metadata={"usage": {"total_tokens": 32}},
        ))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 大括号深度切分允许内嵌换行, 但按行写更贴近真实产物
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def c1_trajectory():
    """``write_c1_trajectory`` 的 fixture 形态。

    刻意**不**让测试 ``from conftest import write_c1_trajectory``: 仓库里有
    ``tests/conftest.py`` 和 ``gdr/tests/conftest.py`` 两个同名模块, pytest
    不保证任一能以顶层名 ``conftest`` 导入(实测 ModuleNotFoundError)。
    """
    return write_c1_trajectory
