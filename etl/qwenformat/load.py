"""etl.qwenformat.load: 把新格式 agent trajectory JSONL 重放为 Session dict.

输入 (``<run_id>__<session_id>.json``) 是 QwenPaw 的 JSONL 事件流, 每个事件
是独立的 JSON 对象, 但 ``tool_execution.payload.output`` 可能包含原始
换行符和引号 —— 不能按行切分后 ``json.loads``, 需要大括号深度计数.

事件类型 (event_type):
    turn_start         -> payload: {input_text, ...}
                          行为: 追加 user message (TextBlock)
    model_request      -> payload: {messages, tools, ...}
                          行为: 首次提取 system prompt 作为 summary; 记录 tools
    thinking           -> payload: {thinking: str}
                          行为: 累积 ThinkingBlock 到 assistant buffer
    tool_call_request  -> payload: {tool_calls: [{id, type, function: {name, arguments}}]}
                          行为: 累积 ToolCallBlock 到 assistant buffer
    tool_execution     -> payload: {tool_call_id, tool_name, input, output}
                          行为: 累积 ToolResultBlock 到 assistant buffer
    final_reply        -> payload: {content: [Message]}, Message.type ∈ {reasoning, message, ...}
                          行为: flush assistant buffer; 追加最终 assistant message
    error / cancel     -> payload: {...}
                          行为: flush assistant buffer

输出 Session dict (与 ``trajectory_to_session_with_openai_metadata`` 对齐):
    {
      "session_id": str,
      "run_id": str,
      "summary": str,            # system prompt
      "messages": [Message],
      "source_file": str,
    }

不再支持旧 CAMEL 单对象 / 旧 .jsonl 命名; 旧格式直接抛 ``ValueError``,
由调用方 (qf_worker) 决定如何处置 (本项目选择 dead archive + 不丢数据).
"""

from __future__ import annotations

import json
import re
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
    role: str                                   # "user" | "assistant"
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


_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def _split_thinking(text: str) -> tuple[str, str]:
    """把 text 拆为 (thinking, plain_text).

    AI SDK 把 reasoning_content 内联到 text 块里 (`<think>...</think>`).
    多个 <think> 段会被串成单个 thinking (用空行分隔);
    段外的可见文本被保留作为 plain_text.
    """
    if not isinstance(text, str) or "<think>" not in text:
        return "", text or ""
    thinking_parts = _THINK_RE.findall(text)
    plain_text = _THINK_RE.sub("", text)
    return "\n\n".join(p.strip("\n") for p in thinking_parts), plain_text


def _build_assistant_from_aisdk_message(
    msg: dict[str, Any],
) -> Optional[Message]:
    """把 AI SDK 形态的 assistant message (model_request.payload.messages[i].content)
    转换为我们的 Message 形态.

    AI SDK content 块类型:
        type=text           -> 文本（含 inline <think>...</think>）→ 拆 thinking/text
        type=tool_call      -> ToolCallBlock (id, name, input[JSON str])
        type=tool_result    -> ToolResultBlock (id, name, output)

    不识别的类型被静默跳过; 若全无可识别块, 返回 None.
    """
    if not isinstance(msg, dict):
        return None
    content = msg.get("content") or []
    blocks: list[Any] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            text = b.get("text", "") or ""
            thinking, plain = _split_thinking(text)
            if thinking:
                blocks.append(ThinkingBlock(thinking=thinking))
            if plain:
                blocks.append(TextBlock(text=plain))
        elif btype == "tool_call":
            input_val = b.get("input", "{}")
            if not isinstance(input_val, str):
                try:
                    input_val = json.dumps(input_val, ensure_ascii=False)
                except (TypeError, ValueError):
                    input_val = str(input_val)
            blocks.append(ToolCallBlock(
                id=b.get("id", "") or "",
                name=b.get("name", "") or "",
                input=input_val,
                state=b.get("state", "finished"),
            ))
        elif btype == "tool_result":
            output = b.get("output", "")
            if not isinstance(output, str):
                try:
                    output = json.dumps(output, ensure_ascii=False)
                except (TypeError, ValueError):
                    output = str(output)
            blocks.append(ToolResultBlock(
                id=b.get("id", "") or "",
                name=b.get("name", "") or "",
                output_text=output,
                state=b.get("state", "success"),
            ))
        # 未知类型: 跳过 (AI SDK 未来可能加新类型)
    if not blocks:
        return None
    return Message(role="assistant", name="assistant", id="", blocks=blocks)


def _final_reply_last_text(content: Any) -> str:
    """从 final_reply.payload.content 取**最后**一个 message / reasoning 块的文本.

    AI SDK 形态下, final_reply 包含整段对话快照:
        [message(prior turn), plugin_call, plugin_call_output, message(final turn)]
    我们只关心**最后**一个 message (AI 的最终回复); 中间的 message / plugin_call /
    plugin_call_output 都被 model_request.messages 覆盖, 跳过.

    Returns the raw text of the last message/reasoning block (含 inline <think>),
    or "" if none.
    """
    if not isinstance(content, list):
        return ""
    last_text = ""
    for msg in content:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") in ("message", "reasoning"):
            last_text = _content_blocks_text(msg.get("content"))
    return last_text


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
    """从 final_reply.payload.content 提取 (reasoning, text).

    content 是 Message 列表, 每个 Message.type ∈ {reasoning, message, ...}.
    只取 reasoning 和 message 的文本, 跳过 function_call 等工具相关类型
    (工具调用已由独立的 tool_call_request/tool_execution 事件记录).
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

    事件按 timestamp 顺序到达 (调用方应保证有序).

    实际事件流有两种形态:
      1. **AI SDK 形态** (QwenPaw 当前默认): ``model_request.payload.messages``
         内嵌完整对话快照 — system / user / assistant(含 tool_call + tool_result)。
         工具调用不再以独立事件出现, 而是嵌在 assistant.content 中;
         工具定义在 ``model_request.payload.tools``。
      2. **早期事件流形态**: ``thinking`` / ``tool_call_request`` / ``tool_execution``
         作为独立事件, ``model_request`` 只有 system + user。

    检测: 若 LAST model_request 的 messages 中含 assistant 角色 → 走形态 1;
    否则走形态 2 保留向后兼容。

    重放规则 (形态 1):
        turn_start                 → 追加 user message
        model_request[last].messages[0]      → system message + summary
        model_request[last].messages[2..]    → assistant (含 thinking+tool_call+tool_result)
        model_request[*].payload.tools      → SessionRecord.tools
        final_reply.payload.content (last message/reasoning) → 最终 assistant message

    重放规则 (形态 2):
        turn_start        → 追加 user message
        model_request     → 首次提取 system prompt 作为 summary
        thinking          → 累积 ThinkingBlock 到 asst_buf
        tool_call_request → 累积 ToolCallBlock 到 asst_buf
        tool_execution    → 累积 ToolResultBlock 到 asst_buf
        final_reply       → flush + 追加最终 assistant message
    """
    if not events:
        return SessionRecord(
            session_id="", run_id="", summary="", messages=[],
            source_file=source_file,
        )

    # ------------------------------------------------------------------
    # Pass 1: 收集元数据 + 关键事件
    # ------------------------------------------------------------------
    session_id = ""
    system_prompt = ""
    tools: list[dict[str, Any]] = []
    trace_ids: set[str] = set()
    event_types: dict[str, int] = {}
    model_name = provider_id = agent_id = user_id = channel = ""

    turn_start_event: Optional[dict[str, Any]] = None
    model_request_events: list[dict[str, Any]] = []
    final_reply_event: Optional[dict[str, Any]] = None
    # 形态 2 累计缓冲
    legacy_thinking: list[dict[str, Any]] = []
    legacy_tool_call_requests: list[dict[str, Any]] = []
    legacy_tool_executions: list[dict[str, Any]] = []

    for ev in events:
        if not isinstance(ev, dict):
            continue
        et = ev.get("event_type", "")
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
            turn_start_event = ev
        elif et == "model_request":
            model_request_events.append(ev)
        elif et == "thinking":
            legacy_thinking.append(ev)
        elif et == "tool_call_request":
            legacy_tool_call_requests.append(ev)
        elif et == "tool_execution":
            legacy_tool_executions.append(ev)
        elif et == "final_reply":
            final_reply_event = ev

    # ------------------------------------------------------------------
    # 提取 system_prompt (从首个 model_request) + tools (从任一 model_request)
    # ------------------------------------------------------------------
    first_mr = model_request_events[0] if model_request_events else None
    if first_mr:
        first_msgs = (first_mr.get("payload") or {}).get("messages", []) or []
        system_prompt = _extract_system_prompt(first_msgs)
    for ev in model_request_events:
        ev_tools = (ev.get("payload") or {}).get("tools")
        if isinstance(ev_tools, list) and ev_tools:
            tools = ev_tools  # 后到的覆盖先到的, 最后一次 model_request 的 tools 胜出

    # ------------------------------------------------------------------
    # 检测形态: LAST model_request 是否含 assistant message
    # ------------------------------------------------------------------
    last_mr = model_request_events[-1] if model_request_events else None
    last_msgs = (last_mr.get("payload") or {}).get("messages", []) if last_mr else []
    has_ai_sdk_assistant = any(
        (m.get("role") or m.get("name")) == "assistant" for m in last_msgs
    )

    messages: list[Message] = []

    if has_ai_sdk_assistant:
        # --------------------------------------------------------------
        # 形态 1: AI SDK 形态 — 从 LAST model_request.messages 抽取
        # --------------------------------------------------------------
        # 1. system message (进 messages 列表供 qf transform 看到)
        for msg in last_msgs:
            if (msg.get("role") or msg.get("name")) == "system":
                sys_text = _content_blocks_text(msg.get("content"))
                if sys_text:
                    messages.append(Message(
                        role="system", name="system", id="",
                        blocks=[TextBlock(text=sys_text)],
                    ))
                break
        # 2. user message (来自 turn_start, 保留时间戳)
        if turn_start_event:
            ts = turn_start_event.get("timestamp")
            payload = turn_start_event.get("payload") or {}
            input_text = payload.get("input_text", "") or ""
            if input_text:
                messages.append(Message(
                    role="user", name="user", id="",
                    blocks=[TextBlock(text=input_text, created_at=ts)],
                    created_at=ts,
                ))
        # 3. assistant message (含 thinking + tool_call + tool_result)
        for msg in last_msgs:
            if (msg.get("role") or msg.get("name")) == "assistant":
                asst_msg = _build_assistant_from_aisdk_message(msg)
                if asst_msg is not None and asst_msg.blocks:
                    messages.append(asst_msg)
                break  # 只取第一个 assistant message (含 tool calls 的那一轮)
        # 4. 最终 assistant message (来自 final_reply 的最后一个 message/reasoning 块)
        if final_reply_event:
            payload = final_reply_event.get("payload") or {}
            raw_text = _final_reply_last_text(payload.get("content"))
            thinking, plain = _split_thinking(raw_text)
            blocks: list[Any] = []
            if thinking:
                blocks.append(ThinkingBlock(thinking=thinking))
            if plain:
                blocks.append(TextBlock(text=plain))
            if blocks:
                messages.append(Message(
                    role="assistant", name="assistant", id="",
                    blocks=blocks,
                    created_at=final_reply_event.get("timestamp"),
                    usage=(final_reply_event.get("metadata") or {}).get("usage"),
                ))
    else:
        # --------------------------------------------------------------
        # 形态 2: 早期事件流 — 沿用 thinking/tool_call_request/tool_execution 累计
        # --------------------------------------------------------------
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

            if et == "turn_start":
                flush_assistant()
                input_text = payload.get("input_text", "") or ""
                if input_text:
                    messages.append(Message(
                        role="user", name="user", id="",
                        blocks=[TextBlock(text=input_text, created_at=ev_ts)],
                        created_at=ev_ts,
                    ))
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
                blocks2: list[Any] = []
                if reasoning:
                    blocks2.append(ThinkingBlock(thinking=reasoning, created_at=ev_ts))
                if text:
                    blocks2.append(TextBlock(text=text, created_at=ev_ts))
                if blocks2:
                    messages.append(Message(
                        role="assistant", name="assistant", id="",
                        blocks=blocks2, created_at=ev_ts,
                        usage=(ev.get("metadata") or {}).get("usage"),
                    ))
            elif et in ("error", "cancel"):
                flush_assistant()

        flush_assistant()

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
      事件而没有 ``turn_start`` / ``final_reply``) → ``ValueError("yielded no messages")``
    """
    raw = path.read_text(encoding="utf-8")
    events = list(_iter_json_objects(raw))
    if not events:
        raise ValueError(f"trajectory contains no parseable events: {path}")
    record = parse_trajectory(events, str(path))
    if not record.messages:
        raise ValueError(
            f"trajectory events yielded no messages (missing turn_start / "
            f"final_reply?): {path}"
        )
    return record
