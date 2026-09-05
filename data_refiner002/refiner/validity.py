"""无效文件判定 (R3)。

文件无效当且仅当：
- 仅有 user 发言（没有任何 assistant 消息）；或
- 仅一次 assistant 回复，且该回复只含 thinking 与 tool 调用、没有任何 tool 调用成功。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("data_refiner.validity")


def is_tool_result_error(block: dict) -> bool:
    """失败判定 (仅 state 字段, 不做内容级模式匹配)。"""
    return block.get("type") == "toolresult" and block.get("state") == "error"


def check_validity(data: dict) -> tuple[bool, str]:
    """返回 (是否有效, 无效原因)。有效文件原因为空字符串。"""
    messages = data.get("messages") or []
    assistant_messages = [m for m in messages if m.get("role") == "assistant"]

    if not assistant_messages:
        reason = "文件无效: 仅有 user 发言, 无 assistant 回复"
        logger.warning(reason)
        return False, reason

    if len(assistant_messages) == 1:
        blocks = assistant_messages[0].get("blocks") or []
        has_text = any(b.get("type") == "text" for b in blocks)
        tool_results = [b for b in blocks if b.get("type") == "toolresult"]
        any_success = any(b.get("state") == "success" for b in tool_results)
        if not has_text and not any_success:
            reason = ("文件无效: 仅一次 assistant 回复, 且只含 thinking 与失败的 tool 调用, "
                      "无正文无成功调用")
            logger.warning(reason)
            return False, reason

    return True, ""
