"""轻量级消息健康评分。

拆分到独立模块，避免 ContextUnderstanding 与 Router 之间循环引用。
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Optional


def _get_attr(block, attr: str, default=""):
    if isinstance(block, dict):
        return block.get(attr, default)
    return getattr(block, attr, default)


def _input_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _toolcall_state(block) -> str:
    if _get_attr(block, "type", "") == "toolresult":
        return _get_attr(block, "state", "")
    return ""


def light_health_score(blocks: list, cfg) -> dict:
    """只返回最轻量的健康字段，供 ContextUnderstanding 初始化使用。

    返回: {
        "total_toolcalls": int,
        "success_toolcalls": int,
        "failed_toolcalls": int,
        "failures_before_first_success": int,
        "has_repetitive_loop": bool,
        "has_context_switch_loop": bool,
        "health_score": float,
        "is_healthy": bool,
    }
    """
    result = {
        "total_toolcalls": 0,
        "success_toolcalls": 0,
        "failed_toolcalls": 0,
        "failures_before_first_success": 0,
        "has_repetitive_loop": False,
        "has_context_switch_loop": False,
        "health_score": 1.0,
        "is_healthy": True,
    }

    toolcall_blocks = []
    toolresult_blocks = []
    for b in blocks:
        t = _get_attr(b, "type", "")
        if t == "toolcall":
            toolcall_blocks.append(b)
        elif t == "toolresult":
            toolresult_blocks.append(b)

    result["total_toolcalls"] = len(toolcall_blocks)
    if result["total_toolcalls"] == 0:
        return result

    first_success_idx = -1
    for i, tr in enumerate(toolresult_blocks):
        if _toolcall_state(tr) == "success":
            result["success_toolcalls"] += 1
            if first_success_idx == -1:
                first_success_idx = i
        else:
            result["failed_toolcalls"] += 1

    result["failures_before_first_success"] = (
        first_success_idx if first_success_idx >= 0 else len(toolresult_blocks)
    )

    # 检测 REPETITIVE_CALL
    for i in range(len(toolcall_blocks) - 2):
        b1, b2, b3 = toolcall_blocks[i], toolcall_blocks[i + 1], toolcall_blocks[i + 2]
        n1 = _get_attr(b1, "name", "")
        n2 = _get_attr(b2, "name", "")
        n3 = _get_attr(b3, "name", "")
        if n1 == n2 == n3:
            i1 = _get_attr(b1, "input", "")
            i2 = _get_attr(b2, "input", "")
            i3 = _get_attr(b3, "input", "")
            if _input_similarity(i1, i2) > 0.9 and _input_similarity(i2, i3) > 0.9:
                result["has_repetitive_loop"] = True
                break

    # 检测 CONTEXT_SWITCH_LOOP
    tool_names_ordered = [_get_attr(b, "name", "") for b in toolcall_blocks]
    switch_count = 0
    for j in range(1, len(tool_names_ordered)):
        prev, curr = tool_names_ordered[j - 1], tool_names_ordered[j]
        if (prev, curr) in [("browser", "execute_shell_command"), ("execute_shell_command", "browser")]:
            switch_count += 1
    if switch_count >= cfg.context_switch_threshold:
        result["has_context_switch_loop"] = True

    success_ratio = result["success_toolcalls"] / result["total_toolcalls"]
    failure_penalty = min(result["failures_before_first_success"] / cfg.max_failures_before_success, 1.0) * 0.4
    loop_penalty = 0.3 if result["has_repetitive_loop"] else 0.0
    switch_penalty = 0.3 if result["has_context_switch_loop"] else 0.0

    result["health_score"] = max(
        0.0,
        success_ratio - failure_penalty - loop_penalty - switch_penalty,
    )
    result["is_healthy"] = (
        result["health_score"] >= cfg.message_health_min_ratio
        and result["failures_before_first_success"] <= cfg.max_failures_before_success
    )
    return result


def light_health_score_for_session(session, cfg) -> dict[int, float]:
    """返回 msg_idx -> health_score 的映射，只包含 assistant 消息。

    用于 ContextUnderstanding 初始化 `MessageSnapshot.is_healthy`，避免零 LLM
    的上下文理解阶段反向依赖 Router 的完整健康对象。
    """
    scores: dict[int, float] = {}
    for msg_idx, msg in enumerate(session.messages):
        if msg.role != "assistant":
            continue
        scores[msg_idx] = light_health_score(msg.blocks, cfg)["health_score"]
    return scores
