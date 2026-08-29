"""Extract: 读取 QwenPaw session JSON，解析为强类型中间模型。

源文件结构（节选）:
    {
      "agent": {
        "state": {
          "session_id": "...",
          "summary": "...",          # 会话总结（可能为空）
          "context": [ Message ],     # 多轮消息列表
          ...
        },
        "scroll": {...},
        "mode_state": {...}
      }
    }

Message:
    name: "user" | "Default"
    role: "user" | "assistant"
    content: [ ContentBlock ]
    metadata: {...}                  # 含 qwenpaw_tag / qwenpaw_turn_usage

ContentBlock.type:
    text      -> {"text": "..."}
    thinking  -> {"thinking": "..."}
    tool_call -> {"id","name","input"(JSON str),"state","suggested_rules"}
    tool_result -> {"id","name","output":[{"type":"text","text":"..."}],"state","metadata"}
"""

from __future__ import annotations

import json
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
    raw_state: dict[str, Any] = field(default_factory=dict)   # 审计用原始 state（去掉 context 巨量字段后）

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


def _parse_block(raw: dict[str, Any]) -> Optional[Any]:
    # toolcall/toolresult 为预处理格式（无下划线）的类型名，等价于 tool_call/tool_result
    t = raw.get("type")
    if t == "text":
        return TextBlock(
            text=raw.get("text", "") or "",
            id=raw.get("id"),
            created_at=raw.get("created_at"),
        )
    if t == "thinking":
        return ThinkingBlock(
            thinking=raw.get("thinking", "") or "",
            id=raw.get("id"),
            created_at=raw.get("created_at"),
        )
    if t in ("tool_call", "toolcall"):
        return ToolCallBlock(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            input=raw.get("input", "") or "",
            state=raw.get("state", "finished"),
            suggested_rules=raw.get("suggested_rules", []) or [],
            created_at=raw.get("created_at"),
        )
    if t in ("tool_result", "toolresult"):
        if isinstance(raw.get("output_text"), str):
            # 预处理格式：output_text 直接是拼接好的字符串
            output_text = raw["output_text"]
        else:
            out = raw.get("output") or []
            text_parts: list[str] = []
            for item in out:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(item.get("text", "") or "")
                elif isinstance(item, dict) and "text" in item:
                    text_parts.append(item.get("text", "") or "")
            output_text = "\n".join(text_parts)
        return ToolResultBlock(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            output_text=output_text,
            state=raw.get("state", "success"),
            metadata=raw.get("metadata", {}) or {},
            created_at=raw.get("created_at"),
        )
    return None


def _parse_message(raw: dict[str, Any]) -> Message:
    blocks: list[Any] = []
    # 预处理格式内容在 blocks，原始 dump 在 content
    for b in raw.get("blocks") or raw.get("content", []) or []:
        parsed = _parse_block(b)
        if parsed is not None:
            blocks.append(parsed)
    return Message(
        role=raw.get("role", "user"),
        name=raw.get("name", ""),
        id=raw.get("id", ""),
        blocks=blocks,
        metadata=raw.get("metadata", {}) or {},
        created_at=raw.get("created_at"),
        finished_at=raw.get("finished_at"),
        finished_reason=raw.get("finished_reason"),
        usage=raw.get("usage"),
        error=raw.get("error"),
    )


def parse_session(raw: dict[str, Any], source_file: str) -> SessionRecord:
    if isinstance(raw.get("messages"), list):
        # 预处理格式：session_id / summary / messages 位于顶层
        return SessionRecord(
            session_id=raw.get("session_id", "") or "",
            summary=raw.get("summary", "") or "",
            messages=[_parse_message(m) for m in raw["messages"]],
            source_file=source_file,
            raw_state={
                k: v for k, v in raw.items() if k not in {"messages"}
            },
        )
    # 原始 dump 格式：agent.state.context
    agent = raw.get("agent") or {}
    state = agent.get("state") or {}
    session_id = state.get("session_id", "")
    summary = state.get("summary", "") or ""
    messages = [_parse_message(m) for m in (state.get("context") or [])]
    audit_state = {
        k: v
        for k, v in state.items()
        if k not in {"context"}
    }
    return SessionRecord(
        session_id=session_id,
        summary=summary,
        messages=messages,
        source_file=source_file,
        raw_state=audit_state,
    )


def iter_session_files(root: Path) -> Iterator[Path]:
    yield from sorted((root).rglob("*.json"))


def load_session(path: Path) -> SessionRecord:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return parse_session(raw, str(path))
