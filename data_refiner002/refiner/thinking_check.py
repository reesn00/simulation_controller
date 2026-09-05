"""thinking 长度检查 (R2)。只标注不删除。"""

from __future__ import annotations

import logging

logger = logging.getLogger("data_refiner.thinking_check")


def flag_thinking(
    data: dict,
    min_chars: int = 20,
    max_chars: int = 4000,
) -> list[dict]:
    """返回 thinking 过长/过短的标注事件列表。"""
    events: list[dict] = []
    for mi, msg in enumerate(data.get("messages") or []):
        if msg.get("role") != "assistant":
            continue
        for bi, block in enumerate(msg.get("blocks") or []):
            if block.get("type") != "thinking":
                continue
            text = block.get("thinking") or ""
            length = len(text)
            if length <= min_chars:
                action = "FLAGGED_THINKING_TOO_SHORT"
                reason = f"thinking 过短: {length} 字符 (阈值 <= {min_chars})"
            elif length >= max_chars:
                action = "FLAGGED_THINKING_TOO_LONG"
                reason = f"thinking 过长: {length} 字符 (阈值 >= {max_chars})"
            else:
                continue
            logger.info("msg %d block %d: %s", mi, bi, reason)
            events.append({
                "message_index": mi,
                "block_index": bi,
                "block_type": "thinking",
                "tool": None,
                "action": action,
                "segment_id": None,
                "segment_size": None,
                "reason": reason,
                "original_block": block,
            })
    return events
