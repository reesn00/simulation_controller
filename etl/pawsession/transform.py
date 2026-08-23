"""Transform: 把 SessionRecord 转为 OpenAI function-calling SFT 消息序列。

输出每条样本:
    {
      "id": "<session_id>",
      "source_file": "...",
      "messages": [ Message, ... ],
      "tools": [ ToolDef, ... ]     # 从 tool_call 推导出的最小工具定义
    }

Message 形态:
    {"role": "system", "content": "..."}                       # 仅当 summary 非空
    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "..."|null,
     "reasoning_content": "..."|省略,                          # thinking，可选
     "tool_calls": [{"id":"call_...","type":"function",
                     "function":{"name":"...","arguments":"..."}}]}
    {"role": "tool", "tool_call_id": "call_...", "name": "...", "content": "..."}

切分规则（一个 assistant turn 内可能有多轮 tool 调用）:
    thinking / text  -> 累积到当前 assistant buffer
    tool_call        -> 累积到当前 assistant buffer 的 tool_calls
    tool_result      -> 先 flush 当前 assistant buffer，再追加 tool 消息
    末尾残留         -> flush 为最终 assistant 回复
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from extract import (
    Message,
    SessionRecord,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)


CALL_PREFIX = "call_"


@dataclass
class TransformOptions:
    include_thinking: bool = True           # 保留 thinking 为 reasoning_content
    include_summary_as_system: bool = True  # summary 非空时作为 system 消息
    drop_empty_assistant: bool = False      # 丢弃空 content 且无 tool_calls 的 assistant
    keep_tool_result_state: bool = True     # 在 tool content 前加 [state] 标记（error 时）


@dataclass
class SFTSample:
    id: str
    source_file: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    stats: dict[str, Any]


def _user_content(msg: Message) -> str:
    parts: list[str] = []
    for b in msg.blocks:
        if isinstance(b, TextBlock):
            parts.append(b.text)
    return "\n".join(p for p in parts if p)


def _new_assistant_buffer() -> dict[str, Any]:
    return {"role": "assistant", "_content_parts": [], "_reasoning_parts": [], "_tool_calls": []}


def _flush_assistant(buf: dict[str, Any], opts: TransformOptions) -> Optional[dict[str, Any]]:
    content = "".join(buf["_content_parts"]).strip()
    reasoning = "".join(buf["_reasoning_parts"]).strip()
    tool_calls = buf["_tool_calls"]
    has_content = bool(content)
    has_reasoning = bool(reasoning)
    has_tools = bool(tool_calls)
    if not has_content and not has_tools and not has_reasoning:
        return None
    if not has_content and not has_tools and opts.drop_empty_assistant:
        return None
    out: dict[str, Any] = {"role": "assistant"}
    out["content"] = content if has_content else None
    if has_reasoning and opts.include_thinking:
        out["reasoning_content"] = reasoning
    if has_tools:
        out["tool_calls"] = tool_calls
    return out


def _transform_assistant(msg: Message, opts: TransformOptions) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    buf = _new_assistant_buffer()
    for b in msg.blocks:
        if isinstance(b, ThinkingBlock):
            if b.thinking:
                buf["_reasoning_parts"].append(b.thinking)
        elif isinstance(b, TextBlock):
            if b.text:
                buf["_content_parts"].append(b.text)
        elif isinstance(b, ToolCallBlock):
            buf["_tool_calls"].append({
                "id": CALL_PREFIX + b.id,
                "type": "function",
                "function": {
                    "name": b.name,
                    "arguments": b.input,
                },
            })
        elif isinstance(b, ToolResultBlock):
            flushed = _flush_assistant(buf, opts)
            if flushed is not None:
                out.append(flushed)
            buf = _new_assistant_buffer()
            content = b.output_text
            if opts.keep_tool_result_state and b.state and b.state != "success":
                content = f"[{b.state}] {content}"
            out.append({
                "role": "tool",
                "tool_call_id": CALL_PREFIX + b.id,
                "name": b.name,
                "content": content,
            })
    flushed = _flush_assistant(buf, opts)
    if flushed is not None:
        out.append(flushed)
    return out


def _collect_tools(record: SessionRecord) -> list[dict[str, Any]]:
    seen: dict[str, set[str]] = {}
    for m in record.messages:
        for b in m.blocks:
            if isinstance(b, ToolCallBlock):
                seen.setdefault(b.name, set())
    tools: list[dict[str, Any]] = []
    for name in sorted(seen):
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": "",
                "parameters": {"type": "object", "properties": {}},
            },
        })
    return tools


def transform_session(record: SessionRecord, opts: Optional[TransformOptions] = None) -> SFTSample:
    opts = opts or TransformOptions()
    messages: list[dict[str, Any]] = []

    if opts.include_summary_as_system and record.summary.strip():
        messages.append({"role": "system", "content": record.summary.strip()})

    for m in record.messages:
        if m.role == "user":
            content = _user_content(m)
            if not content:
                continue
            messages.append({"role": "user", "content": content})
        elif m.role == "assistant":
            messages.extend(_transform_assistant(m, opts))
        else:
            messages.append({"role": m.role, "content": _user_content(m)})

    tools = _collect_tools(record)
    stats = {
        "session_id": record.session_id,
        "user_turns": record.user_turns,
        "assistant_turns": record.assistant_turns,
        "tool_calls": record.tool_call_count,
        "tool_results": record.tool_result_count,
        "output_messages": len(messages),
        "has_summary": bool(record.summary.strip()),
        "tools": [t["function"]["name"] for t in tools],
    }
    return SFTSample(
        id=record.session_id or record.source_file,
        source_file=record.source_file,
        messages=messages,
        tools=tools,
        stats=stats,
    )


def to_jsonl_dict(sample: SFTSample) -> dict[str, Any]:
    return {
        "id": sample.id,
        "source_file": sample.source_file,
        "messages": sample.messages,
        "tools": sample.tools,
    }
