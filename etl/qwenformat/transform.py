"""Transform: 把 OpenAI function-calling SFT 数据适配为 Qwen3 官方 chat template 可用的格式。

相对原始 sft_openai.json 的修复点（对齐 Qwen3/2.5 官方 Jinja 模板）:
    1. tool_calls.function.arguments 由 JSON 字符串反序列化为 dict
       （模板用 arguments|items 遍历参数，字符串会直接渲染失败）
    2. content 为 null 的消息转为 ""（部分实现不接受 None）
    3. 渲染时把 tools 传入模板，自动生成带 <tools> 的 system 段
    4. reasoning_content 保持独立字段，由模板在最后一轮 user 查询之后
       的 assistant 消息上自动包裹 <think>...</think>

注意：与官方模板一致，位于最后一次 user 查询之前的 assistant 中间轮
（tool-call 轮）不会渲染 <think>，其 reasoning_content 只保留在结构化
messages 里，不进入 text。

输出每条样本:
    {
      "id": "...",
      "source_file": "...",
      "messages": [ Message, ... ],   # 清洗后的结构化消息
      "tools": [ ToolDef, ... ],
      "text": "<|im_start|>system..."  # 用官方模板渲染好的训练文本
    }
"""

from __future__ import annotations

import json
from typing import Any, Optional

from jinja2.sandbox import ImmutableSandboxedEnvironment


def _raise_exception(message: str) -> None:
    raise Exception(message)


def build_chat_env() -> ImmutableSandboxedEnvironment:
    """复刻 transformers.apply_chat_template 的 Jinja 环境。

    trim_blocks/lstrip_blocks 必须与 transformers 一致，否则模板里
    换行/缩进的处理会和官方 tokenizer 对不上。
    """
    env = ImmutableSandboxedEnvironment(
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["raise_exception"] = _raise_exception
    return env


def load_chat_template(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def sanitize_agent_sample(sample: dict[str, Any], stats: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """清洗单条 Agent SFT 数据以适配 Qwen3 官方 Jinja Template。

    stats 用于统计修复次数（可为 None）。
    """

    def bump(key: str) -> None:
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    messages = sample.get("messages", [])
    for msg in messages:
        # 1. content 为 None 时转为空字符串
        if msg.get("content") is None:
            msg["content"] = ""
            bump("content_none_filled")

        # 2. tool_calls.function.arguments JSON 字符串 -> dict
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function")
            if not func:
                continue
            args = func.get("arguments")
            if isinstance(args, str):
                try:
                    func["arguments"] = json.loads(args)
                    bump("arguments_deserialized")
                except json.JSONDecodeError:
                    # 容错：解析失败时包装为 raw，避免模板崩溃
                    func["arguments"] = {"raw": args}
                    bump("arguments_parse_failed")

    return sample


def render_sample_text(
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]],
    template_str: str,
    env: ImmutableSandboxedEnvironment,
) -> str:
    """用官方模板渲染为训练文本（等价 tokenizer.apply_chat_template(tokenize=False)）。"""
    tmpl = env.from_string(template_str)
    return tmpl.render(
        messages=messages,
        tools=tools,
        add_generation_prompt=False,
    )


def transform_sample(
    sample: dict[str, Any],
    template_str: str,
    env: ImmutableSandboxedEnvironment,
    stats: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    sanitize_agent_sample(sample, stats)
    text = render_sample_text(sample["messages"], sample.get("tools"), template_str, env)
    return {
        "id": sample.get("id"),
        "source_file": sample.get("source_file"),
        "messages": sample["messages"],
        "tools": sample.get("tools"),
        "text": text,
    }


# ---------------------------------------------------------------------------
# trajectory → Session JSON（含 OpenAI metadata）
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    """UTC ISO8601 字符串（微秒精度，Z 结尾）。"""
    from datetime import datetime, timezone  # 局部导入避免与已有依赖耦合

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _make_tool_call_id(raw_id: str) -> str:
    """OpenAI tool_calls.id 必须以 'call_' 开头；trajectory 里通常是 'tc_xxx'."""
    if not raw_id:
        return "call_unknown"
    return raw_id if raw_id.startswith("call_") else f"call_{raw_id}"


def camel_agent_state_to_session(trajectory: dict[str, Any]) -> dict[str, Any]:
    """把 QwenPaw 拷贝出来的 CAMEL agent trajectory 转为 GDR Session 形态.

    输入（CAMEL agent 原始 session JSON）：
        ``{"agent": {"state": {"session_id", "summary", "context": [
            {"role", "name", "id", "content": [block, ...], "metadata", ...}]}}}``

    输出（GDR ``domain.schema.Session`` 期待的 morph，block type 对齐 GDR 命名）：
        ``{"session_id", "summary", "messages": [{"role", "name", "id",
          "blocks": [block, ...], "metadata", ...}]}``

    变换点：
        1. 顶层 ``agent.state`` 拍平为 ``session_id`` / ``summary``
        2. 消息列表从 ``state.context`` 取出，``content`` 字段重命名为 ``blocks``
        3. block type 对齐 GDR schema 命名：
           - ``tool_call``  → ``toolcall``（保留 id/name/input/state）
           - ``tool_result`` → ``toolresult``（``output`` list → ``output_text`` str）
           - ``hint`` 等 GDR 未定义的类型 → 丢弃
    """
    agent = trajectory.get("agent") or {}
    state = agent.get("state") or {}

    messages: list[dict[str, Any]] = []
    for msg in state.get("context") or []:
        blocks: list[dict[str, Any]] = []
        for raw_block in msg.get("content") or []:
            btype = raw_block.get("type")
            block = dict(raw_block)
            if btype == "tool_call":
                block["type"] = "toolcall"
            elif btype == "tool_result":
                block["type"] = "toolresult"
                parts = [
                    item.get("text", "")
                    for item in (block.get("output") or [])
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                block["output_text"] = "".join(parts)
                block.pop("output", None)
            elif btype not in ("thinking", "text"):
                # GDR schema 未定义的类型（如 hint）丢弃
                continue
            blocks.append(block)
        messages.append({
            "role": msg.get("role"),
            "name": msg.get("name"),
            "id": msg.get("id"),
            "blocks": blocks,
            "metadata": msg.get("metadata") or {},
        })

    return {
        "session_id": state.get("session_id"),
        "summary": state.get("summary", ""),
        "messages": messages,
    }


def parse_qwenpaw_jsonl(raw_text: str) -> dict[str, Any]:
    """把 QwenPaw JSONL trajectory 解析为 Session dict.

    QwenPaw 的 trajectory 文件是 JSONL 格式（每行一个独立 JSON event），
    不是单个 JSON 对象。本函数逐行解析，从 ``turn_start`` 和
    ``final_reply.content`` 重组为 ``trajectory_to_session_with_openai_metadata``
    期望的 Session 形态。

    Event types:
        - ``turn_start``: payload.input_text → user message
        - ``final_reply``: payload.content 是完整对话历史消息列表
        - ``model_request`` / ``model_response`` / ``tool_execution``: 中间
          状态，不直接用于 Session 重组（final_reply 已包含完整历史）

    final_reply.content 中每条消息的 type 映射：
        - ``reasoning`` → assistant + thinking block
        - ``plugin_call`` → assistant + toolcall block
        - ``plugin_call_output`` → tool message + toolresult block
        - ``message`` → assistant + text block
    """
    events: list[dict[str, Any]] = []
    for line in raw_text.strip().splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))

    session_id = ""
    messages: list[dict[str, Any]] = []

    for event in events:
        et = event.get("event_type")
        payload = event.get("payload") or {}

        if et == "turn_start":
            session_id = event.get("session_id", "")
            messages.append({
                "role": "user",
                "blocks": [{"type": "text", "text": payload.get("input_text", "")}],
            })

        elif et == "final_reply":
            for msg in payload.get("content") or []:
                mtype = msg.get("type")
                blocks: list[dict[str, Any]] = []

                if mtype == "reasoning":
                    for c in msg.get("content") or []:
                        if c.get("type") == "text":
                            blocks.append({"type": "thinking", "thinking": c.get("text", "")})
                    if blocks:
                        messages.append({"role": "assistant", "blocks": blocks})

                elif mtype == "plugin_call":
                    for c in msg.get("content") or []:
                        if c.get("type") == "data":
                            d = c.get("data") or {}
                            blocks.append({
                                "type": "toolcall",
                                "id": d.get("call_id", ""),
                                "name": d.get("name", ""),
                                "input": json.dumps(d.get("arguments", {}), ensure_ascii=False),
                            })
                    if blocks:
                        messages.append({"role": "assistant", "blocks": blocks})

                elif mtype == "plugin_call_output":
                    for c in msg.get("content") or []:
                        if c.get("type") == "data":
                            d = c.get("data") or {}
                            messages.append({
                                "role": "tool",
                                "tool_call_id": d.get("call_id", ""),
                                "name": d.get("name", ""),
                                "blocks": [{"type": "toolresult", "output_text": str(d.get("output", ""))}],
                            })

                elif mtype == "message":
                    for c in msg.get("content") or []:
                        if c.get("type") == "text":
                            blocks.append({"type": "text", "text": c.get("text", "")})
                    if blocks:
                        messages.append({"role": "assistant", "blocks": blocks})

    return {"session_id": session_id, "summary": "", "messages": messages}


def trajectory_to_session_with_openai_metadata(
    trajectory: dict[str, Any],
    template_str: str,
    env: ImmutableSandboxedEnvironment,
    *,
    stats: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    """把 simulate_serve 的 trajectory 转为 Session JSON（含 OpenAI metadata）.

    详见 ``docs/orchestration-design.md`` §6.4。

    输入：trajectory dict（gdr 期待的 Session 形态）：
        ``{"session_id": ..., "summary": ..., "messages": [{"role", "name", "id", "blocks", ...}, ...]}``

    输出：保持 ``session_id`` / ``summary`` / ``messages``（blocks 不动）；在
    ``metadata`` 里追加：
        - ``openai_messages``: 从 blocks 拆出的 OpenAI function-calling messages
        - ``tools``: 从所有 tool_call.name 推导（去重、保序）
        - ``qf_text``: 用 Qwen3 chat_template 渲染的训练文本
        - ``qf_rendered_at`` / ``qf_stats``

    blocks 类型识别（GDR session schema）：
        - ``text``: 普通文本，拼到 user/assistant 的 content
        - ``thinking``: 思维链，拼到 assistant 的 reasoning_content
        - ``toolcall``: 一条 tool call，input 是 JSON 字符串
        - ``toolresult``: 生成独立 role=tool 消息（字段 ``output_text``）

    Args:
        trajectory: 原始 trajectory dict
        template_str: 渲染好的 chat_template 文本
        env: 由 ``build_chat_env()`` 构造的 Jinja 环境
        stats: 可选统计 dict，会就地累加以下键：
            ``openai_messages_emitted``, ``tool_calls_emitted``,
            ``tool_results_emitted``, ``tools_unique``,
            ``content_none_filled``, ``arguments_deserialized``,
            ``arguments_parse_failed``（后三个来自 ``sanitize_agent_sample``）
    """
    if stats is None:
        local_stats: dict[str, int] = {}
    else:
        local_stats = stats

    def bump(key: str) -> None:
        local_stats[key] = local_stats.get(key, 0) + 1

    session_id = trajectory.get("session_id")
    summary = trajectory.get("summary", "")
    original_messages = trajectory.get("messages", []) or []

    openai_messages: list[dict[str, Any]] = []
    tool_names_in_order: list[str] = []
    seen_tool_names: set[str] = set()

    def _flush_assistant(
        content_parts: list[str],
        reasoning_parts: list[str],
        tool_calls: list[dict[str, Any]],
    ) -> None:
        """若累积非空，则追加一条 assistant 消息并清空累积。"""
        if not (content_parts or reasoning_parts or tool_calls):
            return
        msg: dict[str, Any] = {"role": "assistant"}
        msg["content"] = "".join(content_parts) if content_parts else None
        if reasoning_parts:
            msg["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            msg["tool_calls"] = tool_calls
        openai_messages.append(msg)

    for msg in original_messages:
        role = msg.get("role")
        blocks = msg.get("blocks") or []

        if role == "user":
            content_parts = [
                b.get("text", "") for b in blocks if b.get("type") == "text"
            ]
            openai_messages.append({"role": "user", "content": "".join(content_parts)})
            bump("openai_messages_emitted")

        elif role == "assistant":
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []

            for block in blocks:
                btype = block.get("type")
                if btype == "text":
                    content_parts.append(block.get("text", ""))
                elif btype == "thinking":
                    reasoning_parts.append(block.get("thinking", ""))
                elif btype == "toolcall":
                    tc_id = block.get("id", "")
                    tc_name = block.get("name", "")
                    tc_input = block.get("input", "{}")
                    tool_calls.append({
                        "id": _make_tool_call_id(tc_id),
                        "type": "function",
                        "function": {"name": tc_name, "arguments": tc_input},
                    })
                    bump("tool_calls_emitted")
                    if tc_name and tc_name not in seen_tool_names:
                        tool_names_in_order.append(tc_name)
                        seen_tool_names.add(tc_name)
                elif btype == "toolresult":
                    _flush_assistant(content_parts, reasoning_parts, tool_calls)
                    content_parts = []
                    reasoning_parts = []
                    tool_calls = []
                    # toolresult → 独立 role=tool message
                    raw_tc_id = block.get("tool_call_id") or block.get("id") or ""
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": _make_tool_call_id(raw_tc_id),
                        "name": block.get("name", ""),
                        "content": block.get("output_text", "") or "",
                    })
                    bump("tool_results_emitted")

            _flush_assistant(content_parts, reasoning_parts, tool_calls)

        elif role == "system":
            content_parts = [
                b.get("text", "") for b in blocks if b.get("type") == "text"
            ]
            openai_messages.append({"role": "system", "content": "".join(content_parts)})
            bump("openai_messages_emitted")

        elif role == "tool":
            content_parts = [
                b.get("text", "") for b in blocks if b.get("type") == "text"
            ]
            openai_messages.append({
                "role": "tool",
                "tool_call_id": _make_tool_call_id(msg.get("tool_call_id", "")),
                "name": msg.get("name", ""),
                "content": "".join(content_parts),
            })
            bump("openai_messages_emitted")
            bump("tool_results_emitted")

        else:
            # 未知 role：跳过
            continue

    # tools 列表（去重、保序；按 OpenAI function schema 默认空 description+parameters）
    tools: list[dict[str, Any]] = []
    for name in tool_names_in_order:
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": "",
                "parameters": {"type": "object"},
            },
        })
    local_stats["tools_unique"] = len(tools)

    # 包装成 OpenAI sample，过一遍 sanitize，再渲染
    sample = {
        "id": session_id,
        "source_file": str(trajectory.get("source_file", "")),
        "messages": openai_messages,
        "tools": tools,
    }
    sanitize_agent_sample(sample, local_stats)
    qf_text = render_sample_text(
        sample["messages"], sample.get("tools"), template_str, env
    )

    return {
        "session_id": session_id,
        "summary": summary,
        "messages": original_messages,
        "metadata": {
            "openai_messages": sample["messages"],
            "tools": tools,
            "qf_text": qf_text,
            "qf_rendered_at": _utc_now_iso(),
            "qf_stats": dict(local_stats),
        },
    }
