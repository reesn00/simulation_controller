"""Load: 写出 SFT 产物。

产物布局:
    <output_dir>/
        sft_openai.jsonl          # OpenAI messages 格式，每行一个样本
        sft_openai.json           # 数组形式（便于人工查看）
        audit/<session_id>.json   # 每会话审计：原始 state 摘要 + 中间消息 + stats
        stats.json                # 全量统计汇总
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from extract import SessionRecord
from transform import SFTSample, to_jsonl_dict


_ABNORMAL_TERMINATORS = (0x2028, 0x2029, 0x0085)


def _sanitize(text: str) -> str:
    if not text:
        return text
    sanitized = text
    for code in _ABNORMAL_TERMINATORS:
        sanitized = sanitized.replace(chr(code), " ")
    return sanitized


@dataclass
class LoadResult:
    output_dir: Path
    jsonl_path: Path
    json_path: Path
    stats_path: Path
    audit_dir: Path
    sample_count: int


def _audit_dict(record: SessionRecord, sample: SFTSample) -> dict[str, Any]:
    return {
        "session_id": record.session_id,
        "source_file": record.source_file,
        "summary": record.summary,
        "raw_state_keys": list(record.raw_state.keys()),
        "reply_context": record.raw_state.get("reply_context"),
        "permission_context": record.raw_state.get("permission_context"),
        "tool_context_activated_groups": (
            record.raw_state.get("tool_context", {}).get("activated_groups")
        ),
        "messages": [
            {
                "role": m.role,
                "name": m.name,
                "id": m.id,
                "blocks": [
                    {
                        "type": b.__class__.__name__.replace("Block", "").lower(),
                        **_block_payload(b),
                    }
                    for b in m.blocks
                ],
                "metadata": m.metadata,
                "usage": m.usage,
                "error": m.error,
                "created_at": m.created_at,
                "finished_at": m.finished_at,
            }
            for m in record.messages
        ],
        "sft_stats": sample.stats,
    }


def _block_payload(b: Any) -> dict[str, Any]:
    from extract import TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock
    if isinstance(b, TextBlock):
        return {"text": b.text, "id": b.id}
    if isinstance(b, ThinkingBlock):
        return {"thinking": b.thinking, "id": b.id}
    if isinstance(b, ToolCallBlock):
        return {
            "id": b.id,
            "name": b.name,
            "input": b.input,
            "state": b.state,
        }
    if isinstance(b, ToolResultBlock):
        return {
            "id": b.id,
            "name": b.name,
            "output_text": b.output_text,
            "state": b.state,
        }
    return {}


def load(
    pairs: list[tuple[SessionRecord, SFTSample]],
    output_dir: Path,
) -> LoadResult:
    output_dir = Path(output_dir)
    audit_dir = output_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = output_dir / "sft_openai.jsonl"
    json_path = output_dir / "sft_openai.json"
    stats_path = output_dir / "stats.json"

    samples_json: list[dict[str, Any]] = []
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for record, sample in pairs:
            obj = to_jsonl_dict(sample)
            samples_json.append(obj)
            f.write(_sanitize(json.dumps(obj, ensure_ascii=False)))
            f.write("\n")

    with open(json_path, "w", encoding="utf-8") as f:
        f.write(_sanitize(json.dumps(samples_json, ensure_ascii=False, indent=2)))

    for record, sample in pairs:
        apath = audit_dir / f"{record.session_id or Path(record.source_file).stem}.json"
        with open(apath, "w", encoding="utf-8") as f:
            f.write(_sanitize(json.dumps(_audit_dict(record, sample), ensure_ascii=False, indent=2)))

    total_stats = {
        "sample_count": len(pairs),
        "totals": {
            "user_turns": sum(s.stats["user_turns"] for _, s in pairs),
            "assistant_turns": sum(s.stats["assistant_turns"] for _, s in pairs),
            "tool_calls": sum(s.stats["tool_calls"] for _, s in pairs),
            "tool_results": sum(s.stats["tool_results"] for _, s in pairs),
            "output_messages": sum(s.stats["output_messages"] for _, s in pairs),
        },
        "sessions": [s.stats for _, s in pairs],
    }
    with open(stats_path, "w", encoding="utf-8") as f:
        f.write(_sanitize(json.dumps(total_stats, ensure_ascii=False, indent=2)))

    return LoadResult(
        output_dir=output_dir,
        jsonl_path=jsonl_path,
        json_path=json_path,
        stats_path=stats_path,
        audit_dir=audit_dir,
        sample_count=len(pairs),
    )
