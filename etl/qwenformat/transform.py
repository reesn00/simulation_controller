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
