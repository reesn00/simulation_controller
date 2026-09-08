#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将 qf_out 中的 agent 轨迹 JSON 转换为 Qwen3.5 SFT 训练数据。

输入格式（qf_out 轨迹文件）:
    {
      "session_id": ...,
      "summary": ...,          # 系统提示（与 messages[0] 的 system blocks 相同）
      "messages": [
        {
          "role": "system|user|assistant",
          "blocks": [
            {"type": "text", "text": "..."},
            {"type": "toolcall", "id": ..., "name": ..., "input": "<json str 或 dict>"},
            {"type": "toolresult", "id": ..., "name": ..., "output_text": "..."}
          ]
        }, ...
      ],
      "metadata": {"tools": [ {type: "function", function: {...}}, ... ], ...}
    }

输出（JSONL）:
    --mode full     每个会话一条:  {"session_id", "tools", "messages", "text"}
    --mode per-turn 每个助手回复一条:
                    {"session_id", "turn_index", "tools", "prompt", "completion", "messages"}
                    prompt/completion 按 etl/qwenformat/chat_template.jinja（Qwen3.5 模板）渲染。

用法:
    python scripts/export_qf_sft/export_qf_sft.py \
        --input output/qf_out \
        --out-dir scripts/export_qf_sft/output \
        --mode per-turn
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import jinja2
from jinja2.sandbox import SandboxedEnvironment

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "output/qf_out"
DEFAULT_TEMPLATE = ROOT / "etl/qwenformat/chat_template.jinja"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "output"

EMPTY_THINK = "<think>\n\n</think>\n\n"


# ---------------------------------------------------------------------------
# 轨迹 blocks -> OpenAI 风格 messages
# ---------------------------------------------------------------------------

def _parse_arguments(raw: Any) -> dict | list:
    """toolcall.input 可能是 JSON 字符串，统一反序列化。"""
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {"_raw": str(raw)}


def blocks_to_messages(role: str, blocks: list[dict]) -> list[dict]:
    """把单条轨迹消息的 blocks 展开成 chat messages。

    约定: 连续 toolcall 聚合为一条带 tool_calls 的 assistant 消息；
    toolresult 转成 role=tool 消息；thinking/reasoning block 转成 reasoning_content。
    """
    messages: list[dict] = []
    pending_calls: list[dict] = []
    pending_reasoning: list[str] = []

    def flush_calls() -> None:
        if not pending_calls:
            return
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "type": "function",
                    "id": c["id"],
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                }
                for c in pending_calls
            ],
        }
        if pending_reasoning:
            msg["reasoning_content"] = "\n".join(pending_reasoning)
        messages.append(msg)
        pending_calls.clear()
        pending_reasoning.clear()

    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text") or ""
            if not text:
                continue
            flush_calls()
            if role == "assistant":
                msg = {"role": "assistant", "content": text}
                if pending_reasoning:
                    msg["reasoning_content"] = "\n".join(pending_reasoning)
                    pending_reasoning.clear()
                messages.append(msg)
            else:
                messages.append({"role": role, "content": text})
        elif btype == "toolcall":
            pending_calls.append(
                {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": _parse_arguments(block.get("input")),
                }
            )
        elif btype == "toolresult":
            flush_calls()
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "content": block.get("output_text", ""),
                }
            )
        elif btype in ("thinking", "reasoning"):
            text = block.get("text") or block.get("thinking") or ""
            if text:
                pending_reasoning.append(text)
        else:
            print(f"[warn] 未知的 block 类型: {btype}，已跳过", file=sys.stderr)
    flush_calls()
    return messages


def trajectory_to_messages(traj: dict) -> list[dict]:
    messages: list[dict] = []
    for msg in traj.get("messages", []):
        messages.extend(blocks_to_messages(msg.get("role", ""), msg.get("blocks", [])))
    return messages


# ---------------------------------------------------------------------------
# Qwen3.5 chat template 渲染
# ---------------------------------------------------------------------------

def make_jinja_env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(trim_blocks=False, lstrip_blocks=False)

    def raise_exception(message: str) -> None:
        raise jinja2.TemplateError(message)

    env.globals["raise_exception"] = raise_exception
    # transformers 官方模板依赖的自定义 filter
    env.filters["items"] = lambda x: x.items() if isinstance(x, dict) else x
    return env


class TemplateRenderer:
    def __init__(self, template_path: Path, drop_empty_think: bool = False):
        env = make_jinja_env()
        with open(template_path, encoding="utf-8") as f:
            self.tmpl = env.from_string(f.read())
        self.drop_empty_think = drop_empty_think

    def render(self, messages: list[dict], tools: list | None = None,
               add_generation_prompt: bool = False,
               enable_thinking: bool | None = None) -> str:
        kwargs: dict[str, Any] = {"messages": messages, "add_generation_prompt": add_generation_prompt}
        if tools:
            kwargs["tools"] = tools
        if enable_thinking is not None:
            kwargs["enable_thinking"] = enable_thinking
        return self._clean(self.tmpl.render(**kwargs))

    def _clean(self, text: str) -> str:
        if self.drop_empty_think:
            text = text.replace("<|im_start|>assistant\n" + EMPTY_THINK, "<|im_start|>assistant\n")
        return text

    @staticmethod
    def _has_real_query(prefix: list[dict]) -> bool:
        """prefix 中是否存在真实的用户 query（模板会把纯 tool_response 的 user
        消息排除在 last_query_index 之外）。"""
        for m in reversed(prefix):
            if m.get("role") != "user":
                continue
            content = m.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            content = content.strip()
            if not (content.startswith("<tool_response>") and content.endswith("</tool_response>")):
                return True
        return False

    def prompt_completion(self, prefix: list[dict], assistant_msg: dict,
                          tools: list | None) -> tuple[str, str]:
        """渲染单轮 (prompt, completion)，与模板逐字节前缀一致。

        模板规则: 仅当助手消息位于「最后一条用户 query」之后才插入 <think> 块；
        多轮会话早期轮次的助手回复不带 think。因此不能依赖 add_generation_prompt
        （它会无条件加 think 头），改为按同一规则手动拼接 assistant 头部。
        """
        base = self.render(prefix, tools=tools)
        head = "<|im_start|>assistant\n"
        if self._has_real_query(prefix):
            head += "<think>\n"
        prompt = self._clean(base + head)
        full = self.render(prefix + [assistant_msg], tools=tools)
        if not full.startswith(prompt):
            tail = full[len(base):][:120]
            raise RuntimeError(
                f"模板渲染结果与 prompt 前缀不一致: assistant 头部后实际内容为 {tail!r}"
            )
        return prompt, full[len(prompt):]


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def export_trajectory(traj: dict, renderer: TemplateRenderer, mode: str,
                      include_tools: bool) -> list[dict]:
    session_id = traj.get("session_id", "")
    messages = trajectory_to_messages(traj)
    tools = traj.get("metadata", {}).get("tools") or [] if include_tools else []

    if mode == "full":
        text = renderer.render(messages, tools=tools)
        return [{"session_id": session_id, "tools": tools, "messages": messages, "text": text}]

    # per-turn / unsloth: 每个助手回复切成一条样本
    samples = []
    prefix: list[dict] = []
    turn_index = 0
    for msg in messages:
        if msg["role"] != "assistant":
            prefix.append(msg)
            continue
        # 助手消息必须跟在 user/tool 之后；跳过开头异常情况
        prompt, completion = renderer.prompt_completion(prefix, msg, tools)
        sample = {
            "session_id": session_id,
            "turn_index": turn_index,
            "tools": tools,
            "prompt": prompt,
            "completion": completion,
            "messages": prefix + [msg],
        }
        if mode == "unsloth":
            # Unsloth SFTTrainer 用单列 text; train_on_responses_only 会在
            # response_part="<|im_start|>assistant\n" 处开始计算损失
            sample = {"session_id": session_id, "turn_index": turn_index, "text": prompt + completion}
        samples.append(sample)
        turn_index += 1
        prefix.append(msg)
    return samples


def load_trajectories(input_path: Path) -> list[Path]:
    if input_path.is_dir():
        files = sorted(input_path.glob("*.json"))
    elif input_path.is_file():
        files = [input_path]
    else:
        raise FileNotFoundError(f"输入不存在: {input_path}")
    if not files:
        raise FileNotFoundError(f"未找到轨迹 JSON: {input_path}")
    return files


def write_samples(samples: list[dict], out_file: Path, fmt: str) -> None:
    if fmt == "jsonl":
        with open(out_file, "w", encoding="utf-8") as f:
            for sample in samples:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
        return
    if fmt == "parquet":
        # 优先用 datasets（训练侧装 unsloth 自带），回退 pandas
        try:
            from datasets import Dataset
            Dataset.from_list(samples).to_parquet(str(out_file))
        except ImportError:
            try:
                import pandas as pd
            except ImportError as exc:
                raise SystemExit(
                    "parquet 输出需要 datasets 或 pandas+pyarrow: uv add datasets"
                ) from exc
            pd.DataFrame(samples).to_parquet(out_file, index=False)
        return
    raise ValueError(f"未知格式: {fmt}")


def main() -> None:
    parser = argparse.ArgumentParser(description="qf_out agent 轨迹 -> Qwen3.5 SFT 数据")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                        help="轨迹 JSON 文件或目录（默认 output/qf_out）")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="输出目录")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE,
                        help="Qwen3.5 chat template 路径")
    parser.add_argument("--mode", choices=["full", "per-turn", "unsloth"], default="per-turn",
                        help="full=整个会话一条; per-turn=每个助手回复一条(prompt/completion); "
                             "unsloth=每个助手回复一条(text 单列, 适配 Unsloth SFTTrainer)")
    parser.add_argument("--format", choices=["jsonl", "parquet"], default="jsonl",
                        help="输出文件格式（parquet 需要 pandas+pyarrow）")
    parser.add_argument("--no-tools", action="store_true", help="不在样本中携带 tools 定义")
    parser.add_argument("--drop-empty-think", action="store_true",
                        help="移除空的 <think></think> 块（非思考型 SFT）")
    args = parser.parse_args()

    renderer = TemplateRenderer(args.template, drop_empty_think=args.drop_empty_think)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for path in load_trajectories(args.input):
        with open(path, encoding="utf-8") as f:
            traj = json.load(f)
        samples = export_trajectory(traj, renderer, args.mode, not args.no_tools)
        out_file = (args.out_dir /
                    f"{path.stem}.sft_{args.mode.replace('-', '_')}.{args.format}")
        write_samples(samples, out_file, args.format)
        total += len(samples)
        print(f"{path.name}: {len(samples)} 条样本 -> {out_file}")
    print(f"共导出 {total} 条样本")


if __name__ == "__main__":
    main()
