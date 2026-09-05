"""Extract: 读取 QwenPaw agent trajectory JSONL，重放事件流为强类型中间模型。

源文件格式（每行一个 TrajectoryEvent，JSONL）:
    {"trace_id", "span_id", "parent_span_id", "event_type", "timestamp",
     "session_id", "agent_id", "user_id", "channel", "provider_id", "model_name",
     "payload", "metadata"}

事件类型（event_type）:
    turn_start         -> payload: {input_text, request_agent_id, agent_backend}
    model_request      -> payload: {messages, tools, tool_choice?, ...}
    model_response     -> payload: {usage, ...}
    tool_call_request  -> payload: {tool_calls: [{id, type, function: {name, arguments}}]}
    tool_execution     -> payload: {tool_call_id, tool_name, input, output}
    thinking           -> payload: {thinking: str}
    error              -> payload: {...}
    cancel             -> payload: {...}
    final_reply        -> payload: {content: [Message]}, Message.type ∈ {reasoning, message, ...}

重放规则:
    turn_start        -> user message (input_text)
    model_request     -> 提取 system prompt (首次) + tools 定义
    thinking          -> 累积 ThinkingBlock 到当前 assistant buffer
    tool_call_request -> 累积 ToolCallBlock 到当前 assistant buffer
    tool_execution    -> 累积 ToolResultBlock 到当前 assistant buffer
    final_reply       -> flush assistant buffer; 追加最终 assistant message (reasoning + text)
    error/cancel      -> flush assistant buffer
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional


@dataclass
class TextBlock:
    text: str
    id: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class ThinkingBlock:
    thinking: str
    id: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class ToolCallBlock:
    id: str
    name: str
    input: str          # 原始 JSON 字符串参数
    state: str = "finished"
    suggested_rules: list[Any] = field(default_factory=list)
    created_at: Optional[str] = None


@dataclass
class ToolResultBlock:
    id: str             # 与对应 tool_call.id 相同
    name: str
    output_text: str    # 拼接后的输出文本
    state: str = "success"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None


@dataclass
class Message:
    role: str                                   # "user" | "assistant"
    name: str                                   # "user" | "Default" | ...
    id: str
    blocks: list[Any] = field(default_factory=list)   # Text/Thinking/ToolCall/ToolResult
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
    finished_reason: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    error: Optional[Any] = None


@dataclass
class SessionRecord:
    session_id: str
    summary: str
    messages: list[Message]
    source_file: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    raw_state: dict[str, Any] = field(default_factory=dict)

    @property
    def user_turns(self) -> int:
        return sum(1 for m in self.messages if m.role == "user")

    @property
    def assistant_turns(self) -> int:
        return sum(1 for m in self.messages if m.role == "assistant")

    @property
    def tool_call_count(self) -> int:
        return sum(
            1 for m in self.messages for b in m.blocks if isinstance(b, ToolCallBlock)
        )

    @property
    def tool_result_count(self) -> int:
        return sum(
            1 for m in self.messages for b in m.blocks if isinstance(b, ToolResultBlock)
        )


def _content_blocks_text(content: Any) -> str:
    """从 message.content（ContentBlock 列表）拼接所有 text 块。"""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", "") or "")
    return "".join(parts)


def _extract_system_prompt(messages: Any) -> str:
    """从 model_request.payload.messages 提取首个 system message 文本。"""
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or msg.get("name")
        if role == "system":
            return _content_blocks_text(msg.get("content"))
    return ""


def _parse_tool_call(tc: Any) -> Optional[ToolCallBlock]:
    """把 tool_call_request.payload.tool_calls 的一项转为 ToolCallBlock。"""
    if not isinstance(tc, dict):
        return None
    tc_id = tc.get("id", "") or ""
    func = tc.get("function")
    if isinstance(func, dict):
        tc_name = func.get("name", "") or ""
        args = func.get("arguments", "")
    else:
        tc_name = tc.get("name", "") or ""
        args = tc.get("arguments", "") or tc.get("input", "")
    if not isinstance(args, str):
        try:
            args = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            args = str(args)
    return ToolCallBlock(id=tc_id, name=tc_name, input=args)


def _parse_final_reply(content: Any) -> tuple[str, str]:
    """从 final_reply.payload.content 提取 (reasoning, text)。

    content 是 Message 列表，每个 Message.type ∈ {reasoning, message, ...}。
    只取 reasoning 和 message 的文本，跳过 function_call 等工具相关类型
    （工具调用已由独立的 tool_call_request/tool_execution 事件记录）。
    """
    if not isinstance(content, list):
        return "", ""
    reasoning_parts: list[str] = []
    text_parts: list[str] = []
    for msg in content:
        if not isinstance(msg, dict):
            continue
        msg_type = msg.get("type")
        msg_text = _content_blocks_text(msg.get("content"))
        if msg_type == "reasoning" and msg_text:
            reasoning_parts.append(msg_text)
        elif msg_type == "message" and msg_text:
            text_parts.append(msg_text)
    return "".join(reasoning_parts), "".join(text_parts)


def parse_trajectory(events: list[dict[str, Any]], source_file: str) -> SessionRecord:
    """重放 trajectory 事件流，重建为 SessionRecord。

    事件按 timestamp 顺序到达（调用方应保证有序）。重放算法：
        turn_start    -> 追加 user message
        model_request -> 首次提取 system prompt 作为 summary；记录 tools
        thinking      -> 累积到 assistant buffer
        tool_call_request -> 累积 ToolCallBlock 到 assistant buffer
        tool_execution    -> 累积 ToolResultBlock 到 assistant buffer
        final_reply  -> flush buffer；追加最终 assistant message
        error/cancel -> flush buffer
    """
    if not events:
        return SessionRecord(
            session_id="", summary="", messages=[], source_file=source_file,
        )

    session_id = ""
    system_prompt = ""
    tools: list[dict[str, Any]] = []
    messages: list[Message] = []
    trace_ids: set[str] = set()
    event_counter: Counter[str] = Counter()
    model_name = ""
    provider_id = ""
    agent_id = ""
    user_id = ""
    channel = ""

    asst_buf: Optional[Message] = None

    def flush_assistant() -> None:
        nonlocal asst_buf
        if asst_buf is not None and asst_buf.blocks:
            messages.append(asst_buf)
        asst_buf = None

    def ensure_assistant() -> Message:
        nonlocal asst_buf
        if asst_buf is None:
            asst_buf = Message(role="assistant", name="assistant", id="", blocks=[])
        return asst_buf

    for ev in events:
        if not isinstance(ev, dict):
            continue
        et = ev.get("event_type", "")
        payload = ev.get("payload") or {}
        ev_ts = ev.get("timestamp")

        if not session_id:
            session_id = ev.get("session_id", "") or ""
        if not agent_id:
            agent_id = ev.get("agent_id", "") or ""
        if not user_id:
            user_id = ev.get("user_id", "") or ""
        if not channel:
            channel = ev.get("channel", "") or ""
        if not model_name:
            model_name = ev.get("model_name", "") or ""
        if not provider_id:
            provider_id = ev.get("provider_id", "") or ""
        trace_id = ev.get("trace_id", "") or ""
        if trace_id:
            trace_ids.add(trace_id)
        event_counter[et] += 1

        if et == "turn_start":
            flush_assistant()
            input_text = payload.get("input_text", "") or ""
            if input_text:
                messages.append(Message(
                    role="user", name="user", id="",
                    blocks=[TextBlock(text=input_text, created_at=ev_ts)],
                    created_at=ev_ts,
                ))
        elif et == "model_request":
            if not system_prompt:
                system_prompt = _extract_system_prompt(payload.get("messages"))
            ev_tools = payload.get("tools")
            if isinstance(ev_tools, list) and ev_tools:
                tools = ev_tools
        elif et == "thinking":
            thinking_text = payload.get("thinking", "") or ""
            if thinking_text:
                buf = ensure_assistant()
                buf.blocks.append(ThinkingBlock(thinking=thinking_text, created_at=ev_ts))
        elif et == "tool_call_request":
            buf = ensure_assistant()
            for tc in payload.get("tool_calls", []) or []:
                parsed = _parse_tool_call(tc)
                if parsed is not None:
                    parsed.created_at = ev_ts
                    buf.blocks.append(parsed)
        elif et == "tool_execution":
            buf = ensure_assistant()
            tc_id = payload.get("tool_call_id", "") or ""
            tc_name = payload.get("tool_name", "") or ""
            output = payload.get("output", "")
            if not isinstance(output, str):
                try:
                    output = json.dumps(output, ensure_ascii=False)
                except (TypeError, ValueError):
                    output = str(output)
            end_state = (ev.get("metadata") or {}).get("end_state", "success")
            buf.blocks.append(ToolResultBlock(
                id=tc_id, name=tc_name, output_text=output,
                state=end_state if isinstance(end_state, str) else "success",
                metadata=ev.get("metadata") or {},
                created_at=ev_ts,
            ))
        elif et == "final_reply":
            flush_assistant()
            reasoning, text = _parse_final_reply(payload.get("content"))
            blocks: list[Any] = []
            if reasoning:
                blocks.append(ThinkingBlock(thinking=reasoning, created_at=ev_ts))
            if text:
                blocks.append(TextBlock(text=text, created_at=ev_ts))
            if blocks:
                messages.append(Message(
                    role="assistant", name="assistant", id="",
                    blocks=blocks, created_at=ev_ts,
                    usage=(ev.get("metadata") or {}).get("usage"),
                ))
        elif et in ("error", "cancel"):
            flush_assistant()

    flush_assistant()

    raw_state = {
        "trace_ids": sorted(trace_ids),
        "event_count": sum(event_counter.values()),
        "event_types": dict(event_counter),
        "model_name": model_name,
        "provider_id": provider_id,
        "agent_id": agent_id,
        "user_id": user_id,
        "channel": channel,
    }

    return SessionRecord(
        session_id=session_id,
        summary=system_prompt,
        messages=messages,
        source_file=source_file,
        tools=tools,
        raw_state=raw_state,
    )


def iter_session_files(root: Path) -> Iterator[Path]:
    yield from sorted(Path(root).rglob("*.jsonl"))


def load_session(path: Path) -> SessionRecord:
    events: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return parse_trajectory(events, str(path))
