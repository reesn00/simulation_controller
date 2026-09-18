"""etl.qwenformat.load: 把 agent trajectory JSONL 重放为 Session dict.

输入 (``<run_id>__<session_id>.json``) 是 QwenPaw 的 JSONL 事件流, 每个事件
是独立的 JSON 对象, 但 ``tool_execution.payload.output`` 可能包含原始
换行符和引号 —— 不能按行切分后 ``json.loads``, 需要大括号深度计数.

完整格式定义见 ``docs/agent-trajectory-format.md``。事件信封统一为
``{trace_id, span_id, parent_span_id, event_type, timestamp, session_id,
agent_id, user_id, channel, provider_id, model_name, payload, metadata}``。

事件类型与重放规则 (每类事件只有一个职责, 互不重复):

    turn_start         -> payload: {input_text, ...}
                          行为: flush assistant buffer; 追加 user message
    model_request      -> payload: {messages, tools}
                          行为: 首个事件提取 system prompt (summary);
                          最后一个非空 payload.tools 胜出 (工具定义)
    model_response     -> payload: {content: [blocks], usage, finished_reason}
                          行为: content 块累积到 assistant buffer:
                          type=thinking -> ThinkingBlock
                          type=tool_call -> ToolCallBlock (state 归一为 finished)
                          type=text     -> TextBlock
    tool_execution     -> payload: {tool_call_id, tool_name, input, output}
                          行为: 累积 ToolResultBlock (state 取 metadata.end_state)
    final_reply        -> payload: {content: [Message 快照]}
                          行为: flush assistant buffer (本轮完整 assistant
                          message); usage 取 metadata.usage。payload.content
                          是冗余快照, 不参与重放
    error / cancel     -> payload: {...}
                          行为: flush assistant buffer

冗余事件 (重放时跳过, 详见格式文档):
    tool_call_request  -> 与 model_response.content 的 tool_call 块重复
                          (OpenAI function 形态)
    model_request.payload.messages 的对话快照 -> 与事件流重放结果重复

多轮会话: 每轮一对 turn_start / final_reply; 每轮产出一个 user message 和
一个 assistant message (含该轮全部 thinking / tool_call / tool_result /
最终 thinking / 最终 text 块)。

输出 Session dict (与 ``trajectory_to_session_with_openai_metadata`` 对齐):
    {
      "session_id": str,
      "run_id": str,
      "summary": str,            # system prompt
      "messages": [Message],
      "source_file": str,
      "tools": [ToolDef, ...],
    }

旧格式 (CAMEL 单对象 / 无 model_response.content 的早期事件流 / inline
 md 内嵌 thinking 的 AI SDK 形态) 不再支持, 解析不出消息时抛 ``ValueError``,
由调用方 (qf_worker) 决定如何处置 (本项目选择 dead archive + 不丢数据).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional


# ---------------------------------------------------------------------------
# 中间模型 (与 GDR Session/Message/Block 的形态对齐, 但保持 dataclass 以
# 避免在 etl 引入 pydantic 依赖循环)
# ---------------------------------------------------------------------------


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
    created_at: Optional[str] = None


@dataclass
class ToolResultBlock:
    id: str             # 与对应 tool_call.id 相同
    name: str
    output_text: str
    state: str = "success"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None


@dataclass
class Message:
    role: str                                   # "system" | "user" | "assistant"
    name: str
    id: str
    blocks: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    finished_at: Optional[str] = None
    usage: Optional[dict[str, Any]] = None
    error: Optional[Any] = None


@dataclass
class SessionRecord:
    session_id: str
    run_id: str
    summary: str
    messages: list[Message]
    source_file: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    raw_state: dict[str, Any] = field(default_factory=dict)

    def to_session_dict(self) -> dict[str, Any]:
        """序列化为 qf transform 期望的 Session dict 形态.

        形态:
            {"session_id", "summary", "messages": [
                {"role", "name", "id", "blocks": [block_dict, ...], "metadata", ...}
            ], "source_file", "run_id", "tools": [ToolDef, ...]}

        ``tools`` 字段透传 SessionRecord.tools (model_request.payload.tools).
        qf transform 优先用 trajectory["tools"]; 没有时再由 toolcall 推导.
        """
        out_messages: list[dict[str, Any]] = []
        for m in self.messages:
            blocks: list[dict[str, Any]] = []
            for b in m.blocks:
                if isinstance(b, TextBlock):
                    blocks.append({"type": "text", "id": b.id or "", "text": b.text})
                elif isinstance(b, ThinkingBlock):
                    blocks.append({"type": "thinking", "id": b.id or "", "thinking": b.thinking})
                elif isinstance(b, ToolCallBlock):
                    blocks.append({
                        "type": "toolcall",
                        "id": b.id,
                        "name": b.name,
                        "input": b.input,
                        "state": b.state,
                    })
                elif isinstance(b, ToolResultBlock):
                    blocks.append({
                        "type": "toolresult",
                        "id": b.id,
                        "name": b.name,
                        "output_text": b.output_text,
                        "state": b.state,
                        # metadata 可能含 raw_output (tool_output_summarizer 保留的原始返回)
                        "metadata": b.metadata,
                    })
                else:
                    blocks.append(b)
            msg_dict: dict[str, Any] = {
                "role": m.role,
                "name": m.name,
                "id": m.id,
                "blocks": blocks,
                "metadata": m.metadata,
            }
            if m.usage is not None:
                msg_dict["usage"] = m.usage
            if m.error is not None:
                msg_dict["error"] = m.error
            if m.created_at:
                msg_dict["created_at"] = m.created_at
            if m.finished_at:
                msg_dict["finished_at"] = m.finished_at
            out_messages.append(msg_dict)
        return {
            "session_id": self.session_id,
            "summary": self.summary,
            "messages": out_messages,
            "source_file": self.source_file,
            "run_id": self.run_id,
            "tools": list(self.tools),
        }


# ---------------------------------------------------------------------------
# 解析 helpers
# ---------------------------------------------------------------------------


_DOUBLE_UNDERSCORE = "__"


def parse_filename(stem: str) -> tuple[str, str]:
    """解析 ``<run_id>__<session_id>`` 文件名 stem.

    Returns: ``(run_id, session_id)``。当 stem 不含 ``__`` 时, 退化为
    ``(stem, "")`` 以保证下游仍能从事件流中读到 session_id。
    """
    if _DOUBLE_UNDERSCORE in stem:
        run_id, session_id = stem.split(_DOUBLE_UNDERSCORE, 1)
        return run_id, session_id or ""
    return stem, ""


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


def _json_str(value: Any, default: str = "") -> str:
    """非字符串值序列化为 JSON 字符串; 失败时退化为 str()."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# 大括号深度计数: 处理含内嵌换行符 / 引号的 JSON 对象
# ---------------------------------------------------------------------------


def _iter_json_objects(raw: str) -> Iterator[dict[str, Any]]:
    """按大括号深度切分 raw 文本中的 JSON 对象 (支持字符串边界 / 转义).

    trajectory JSONL 中每个事件一行, 但 ``tool_execution.payload.output``
    字段经常含原始换行符, 不能直接 ``splitlines()`` + ``json.loads``.
    """
    depth = 0
    in_string = False
    escape = False
    obj_start = -1
    for i, ch in enumerate(raw):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
            if depth == 0 and obj_start >= 0:
                obj_text = raw[obj_start:i + 1]
                obj_start = -1
                try:
                    obj = json.loads(obj_text)
                except json.JSONDecodeError:
                    # 跳过无法解析的对象 (例如尾部截断的事件)
                    continue
                if isinstance(obj, dict):
                    yield obj


# ---------------------------------------------------------------------------
# 重放器
# ---------------------------------------------------------------------------


def parse_trajectory(events: list[dict[str, Any]], source_file: str) -> SessionRecord:
    """重放 trajectory 事件流, 重建为 SessionRecord.

    事件按 timestamp 顺序到达 (调用方应保证有序)。重放规则见模块 docstring;
    每类事件只有一个职责:

        turn_start      -> user message
        model_request   -> system prompt (首个) + tools (最后非空)
        model_response   -> thinking / tool_call / text 块
        tool_execution   -> tool_result 块
        final_reply     -> flush 本轮 assistant message (+ usage)
        error / cancel  -> flush

    每个用户轮产出一个 assistant message, 含该轮全部块 (多轮工具循环的
    thinking / tool_call / tool_result 与最终 thinking / text 按发生顺序排列)。
    """
    if not events:
        return SessionRecord(
            session_id="", run_id="", summary="", messages=[],
            source_file=source_file,
        )

    session_id = ""
    system_prompt = ""
    tools: list[dict[str, Any]] = []
    trace_ids: set[str] = set()
    event_types: dict[str, int] = {}
    model_name = provider_id = agent_id = user_id = channel = ""

    messages: list[Message] = []
    asst_buf: Optional[Message] = None

    def flush_assistant(usage: Optional[dict[str, Any]] = None) -> None:
        nonlocal asst_buf
        if asst_buf is not None and asst_buf.blocks:
            if usage is not None:
                asst_buf.usage = usage
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
        event_types[et] = event_types.get(et, 0) + 1

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
                tools = ev_tools  # 后到的覆盖先到的, 最后一次 model_request 的 tools 胜出
        elif et == "model_response":
            content = payload.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "thinking":
                    thinking = b.get("thinking", "") or ""
                    if thinking:
                        buf = ensure_assistant()
                        buf.blocks.append(ThinkingBlock(
                            thinking=thinking,
                            id=b.get("id"),
                            created_at=b.get("created_at") or ev_ts,
                        ))
                elif btype == "tool_call":
                    buf = ensure_assistant()
                    # model_response 中 state=pending (尚未执行); 重放视角下
                    # 调用已发生, 归一为 finished (GDR schema 仅接受 finished)
                    buf.blocks.append(ToolCallBlock(
                        id=b.get("id", "") or "",
                        name=b.get("name", "") or "",
                        input=_json_str(b.get("input", "{}")),
                        state="finished",
                        created_at=b.get("created_at") or ev_ts,
                    ))
                elif btype == "text":
                    text = b.get("text", "") or ""
                    if text:
                        buf = ensure_assistant()
                        buf.blocks.append(TextBlock(
                            text=text,
                            id=b.get("id"),
                            created_at=b.get("created_at") or ev_ts,
                        ))
                # 未知块类型: 跳过 (image / audio 等多模态块暂不消费)
        elif et == "tool_execution":
            buf = ensure_assistant()
            tc_id = payload.get("tool_call_id", "") or ""
            tc_name = payload.get("tool_name", "") or ""
            end_state = (ev.get("metadata") or {}).get("end_state", "success")
            buf.blocks.append(ToolResultBlock(
                id=tc_id, name=tc_name,
                output_text=payload.get("output", "") or "",
                state=end_state if isinstance(end_state, str) else "success",
                metadata=ev.get("metadata") or {},
                created_at=ev_ts,
            ))
        elif et == "final_reply":
            usage = (ev.get("metadata") or {}).get("usage")
            flush_assistant(usage=usage)
        elif et in ("error", "cancel"):
            flush_assistant()
        # tool_call_request: 与 model_response.content 的 tool_call 块重复, 跳过

    flush_assistant()

    # system message (进 messages 列表供 qf transform / qf_worker 看到)
    if system_prompt:
        messages.insert(0, Message(
            role="system", name="system", id="",
            blocks=[TextBlock(text=system_prompt)],
        ))

    raw_state = {
        "trace_ids": sorted(trace_ids),
        "event_count": sum(event_types.values()),
        "event_types": event_types,
        "model_name": model_name,
        "provider_id": provider_id,
        "agent_id": agent_id,
        "user_id": user_id,
        "channel": channel,
    }

    file_run_id, file_session_id = parse_filename(Path(source_file).stem)
    final_session_id = file_session_id or session_id

    return SessionRecord(
        session_id=final_session_id,
        run_id=file_run_id,
        summary=system_prompt,
        messages=messages,
        source_file=source_file,
        tools=tools,
        raw_state=raw_state,
    )


def iter_session_files(root: Path) -> Iterator[Path]:
    """扫描 ``<run_id>__<session_id>.json`` trajectory 文件.

    只识别新格式命名; 旧 .jsonl 与裸 JSON 都不再处理.
    """
    yield from sorted(p for p in Path(root).glob("*__*.json") if p.is_file())


def load_trajectory(path: Path) -> SessionRecord:
    """读取新格式 trajectory 文件并解析为 SessionRecord.

    文件必须是 JSONL 事件流 (每行一个 TrajectoryEvent, 大括号深度计数
    兼容内嵌换行), 文件名匹配 ``run_<run_id>__<session_id>.json``.

    解析失败抛 ``ValueError``/``json.JSONDecodeError``, 由调用方处理:

    - 文件为空 / 全无可解析的 JSON 对象 → ``ValueError("no parseable events")``
    - 事件流没有产生任何 message (例如只有 ``model_request`` 等元数据
      事件而没有 ``turn_start`` / ``model_response``) → ``ValueError("yielded no messages")``
    """
    raw = path.read_text(encoding="utf-8")
    events = list(_iter_json_objects(raw))
    if not events:
        raise ValueError(f"trajectory contains no parseable events: {path}")
    record = parse_trajectory(events, str(path))
    if not record.messages:
        raise ValueError(
            f"trajectory events yielded no messages (missing turn_start / "
            f"model_response?): {path}"
        )
    return record
