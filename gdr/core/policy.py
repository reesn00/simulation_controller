"""Policy 决策层 —— 基于 defect + context view 选择三级响应策略。

设计文档: docs/gdr-context-understanding-and-policy.md §3

策略枚举:
  - REPAIR_IN_PLACE    调整后保留
  - PRUNE_BLOCK        删除单个 block (保留所在消息)
  - PRUNE_WITH_PAIR    删除 block + 上一轮 user turn (保守, 默认关闭)
  - PRUNE_MESSAGE      整条 assistant 消息删除
  - DEFER_TO_HUMAN     标记待人工复核 (重要但不可自动修)

P0 范围:
  - 决策表 (rule-based, 覆盖设计文档 §3.2 全部 case)
  - 与 ContextUnderstanding 完全解耦 (decide_policy 是纯函数)
  - 不调用任何 LLM
"""
from __future__ import annotations

import logging
from enum import StrEnum
from typing import Iterable

from core.context_understanding import BlockContextView
from domain import DefectTag

log = logging.getLogger(__name__)


class RefinementPolicy(StrEnum):
    REPAIR_IN_PLACE = "repair_in_place"
    PRUNE_BLOCK = "prune_block"
    PRUNE_WITH_PAIR = "prune_with_pair"
    PRUNE_MESSAGE = "prune_message"
    DEFER_TO_HUMAN = "defer_to_human"


class FailureHandlingMode(StrEnum):
    """失败调用处理模式（方案 §5.3）。"""
    CLEAN = "clean"      # 只保留成功路径
    ROBUST = "robust"    # 保留 1 次典型错误 + 恢复, 触发 thought_refactor
    DROP = "drop"        # 连续失败过多 → PRUNE_MESSAGE


# === 决策表 ===

_REPETITIVE_PRUNE = (DefectTag.REPETITIVE_CALL,)
_CONTEXT_SWITCH = (DefectTag.CONTEXT_SWITCH_LOOP,)
_DEBUG_LEAK = (DefectTag.OBS_DEBUG_LEAK,)
_OBS_NOISE = (DefectTag.OBS_NOISE,)
_HALLUCINATED = (DefectTag.TOOL_HALLUCINATED,)
_JSON_INVALID = (DefectTag.TOOL_JSON_INVALID,)
_API_HALLU = (DefectTag.API_HALLUCINATION,)
_WRONG_TOOL = (DefectTag.TOOL_WRONG_SELECTION,)
_TOO_SHORT = (DefectTag.THOUGHT_TOO_SHORT,)
_TOO_LONG = (DefectTag.THOUGHT_TOO_LONG,)
_BROKEN_LOGIC = (DefectTag.THOUGHT_BROKEN_LOGIC,)
_TEXT_FACT = (DefectTag.TEXT_FACT_HALLUCINATION,)


def _has_any(defects: Iterable[DefectTag], targets: tuple) -> bool:
    return any(d in targets for d in defects)


def decide_policy(
    block,
    defects: list[DefectTag],
    context_view: BlockContextView | None,
    retry_exhausted: bool,
    cfg,
) -> RefinementPolicy:
    """基于 defect + context_view 决策。

    输入:
      block          Pydantic 模型或 dict
      defects        此 block 上的 DefectTag 列表
      context_view   ContextUnderstanding.get_view(block_id), 若 None 则退化为 REPAIR_IN_PLACE
      retry_exhausted refiner 重试是否已耗尽 (影响降级策略)
      cfg            Settings

    返回: RefinementPolicy
    """
    # 开关: 全局禁用 policy 层 → 全部走 REPAIR
    if not getattr(cfg, "enable_policy_layer", True):
        return RefinementPolicy.REPAIR_IN_PLACE

    block_type = context_view.block_type if context_view else _block_type(block)
    defer_on_exhaust = getattr(cfg, "policy_defer_on_exhausted", True)

    # === 无 context view 时退化为最小决策 ===
    if context_view is None:
        if retry_exhausted and defer_on_exhaust:
            return RefinementPolicy.DEFER_TO_HUMAN
        return RefinementPolicy.REPAIR_IN_PLACE

    # === REPETITIVE_CALL ===
    if _has_any(defects, _REPETITIVE_PRUNE):
        mode = getattr(cfg, "failure_handling_mode", "clean")
        try:
            fmode = FailureHandlingMode(mode)
        except ValueError:
            fmode = FailureHandlingMode.CLEAN
        if context_view.is_redundant_in_window:
            # CLEAN: 直接删; ROBUST: 保留典型错误; DROP: 整条消息删
            if fmode == FailureHandlingMode.DROP:
                return RefinementPolicy.PRUNE_MESSAGE
            if fmode == FailureHandlingMode.ROBUST:
                return RefinementPolicy.REPAIR_IN_PLACE
            return RefinementPolicy.PRUNE_BLOCK
        if context_view.referenced_by:
            return RefinementPolicy.REPAIR_IN_PLACE
        return RefinementPolicy.PRUNE_BLOCK if not context_view.is_referenced_in_active \
            else RefinementPolicy.REPAIR_IN_PLACE

    # === CONTEXT_SWITCH_LOOP ===
    if _has_any(defects, _CONTEXT_SWITCH):
        # 已在 archive 中见过相同模式的切换 → 重复无效 → 删
        if context_view.referenced_by:
            return RefinementPolicy.REPAIR_IN_PLACE
        if retry_exhausted and defer_on_exhaust:
            return RefinementPolicy.DEFER_TO_HUMAN
        return RefinementPolicy.PRUNE_BLOCK

    # === OBS_DEBUG_LEAK / OBS_NOISE ===
    if _has_any(defects, _DEBUG_LEAK + _OBS_NOISE):
        if context_view.referenced_by:
            return RefinementPolicy.REPAIR_IN_PLACE
        if retry_exhausted and defer_on_exhaust:
            return RefinementPolicy.DEFER_TO_HUMAN
        return RefinementPolicy.PRUNE_BLOCK

    # === TOOL_HALLUCINATED / TOOL_JSON_INVALID / API_HALLUCINATION / TOOL_WRONG_SELECTION ===
    if _has_any(defects, _HALLUCINATED + _JSON_INVALID + _API_HALLU + _WRONG_TOOL):
        if retry_exhausted and defer_on_exhaust:
            return RefinementPolicy.DEFER_TO_HUMAN
        return RefinementPolicy.REPAIR_IN_PLACE

    # === THOUGHT_TOO_SHORT ===
    if _has_any(defects, _TOO_SHORT):
        if context_view.is_transition_point and defer_on_exhaust:
            return RefinementPolicy.DEFER_TO_HUMAN
        return RefinementPolicy.REPAIR_IN_PLACE

    # === THOUGHT_TOO_LONG ===
    if _has_any(defects, _TOO_LONG):
        if context_view.key_decisions and len(context_view.key_decisions) > 0:
            # 信息密度高: 含决策句 → 修复压缩
            return RefinementPolicy.REPAIR_IN_PLACE
        # 信息密度低: 纯冗余填充 → 删
        return RefinementPolicy.PRUNE_BLOCK

    # === THOUGHT_BROKEN_LOGIC ===
    if _has_any(defects, _BROKEN_LOGIC):
        if context_view.is_transition_point:
            return RefinementPolicy.DEFER_TO_HUMAN
        return RefinementPolicy.REPAIR_IN_PLACE

    # === TEXT_FACT_HALLUCINATION ===
    if _has_any(defects, _TEXT_FACT):
        # 数值/平台名在 archive 出现过 → 可修复
        if context_view.referenced_by or context_view.entities_mentioned:
            return RefinementPolicy.REPAIR_IN_PLACE
        # 无来源
        return RefinementPolicy.DEFER_TO_HUMAN

    # === MESSAGE_UNHEALTHY (由上层 message 健康分处理, 这里仅兜底) ===
    if DefectTag.MESSAGE_UNHEALTHY in defects:
        if retry_exhausted and defer_on_exhaust:
            return RefinementPolicy.DEFER_TO_HUMAN
        return RefinementPolicy.PRUNE_MESSAGE

    # === 默认 ===
    if retry_exhausted and defer_on_exhaust:
        return RefinementPolicy.DEFER_TO_HUMAN
    return RefinementPolicy.REPAIR_IN_PLACE


def _block_type(block) -> str:
    if isinstance(block, dict):
        return block.get("type", "")
    return getattr(block, "type", "")


def decide_batch(
    blocks_with_defects: list[tuple],
    context_understanding,
    cfg,
) -> list[RefinementPolicy]:
    """批量决策: 输入 [(block, defects, retry_exhausted, block_id), ...] → [policy, ...]"""
    decisions: list[RefinementPolicy] = []
    for block, defects, retry_exhausted, block_id in blocks_with_defects:
        view = context_understanding.get_view(block_id) if context_understanding else None
        decisions.append(decide_policy(block, defects, view, retry_exhausted, cfg))
    return decisions


# === 决策理由生成 (供 metadata 落盘) ===

def policy_reason(policy: RefinementPolicy, defects: list[DefectTag], context_view: BlockContextView | None) -> str:
    """生成人类可读的决策理由, 用于 metadata.policy_decisions[].reason。"""
    defect_str = ", ".join(d.value for d in defects) if defects else "(no defect)"
    if context_view is None:
        return f"{defect_str} → {policy.value} (no context)"
    if policy == RefinementPolicy.REPAIR_IN_PLACE:
        if context_view.referenced_by:
            return f"{defect_str} → repair (referenced by {len(context_view.referenced_by)} blocks)"
        return f"{defect_str} → repair (no redundancy detected)"
    if policy == RefinementPolicy.PRUNE_BLOCK:
        return f"{defect_str} → prune_block (redundant_in_window={context_view.is_redundant_in_window})"
    if policy == RefinementPolicy.PRUNE_WITH_PAIR:
        return f"{defect_str} → prune_with_pair"
    if policy == RefinementPolicy.PRUNE_MESSAGE:
        return f"{defect_str} → prune_message (message unhealthy)"
    if policy == RefinementPolicy.DEFER_TO_HUMAN:
        return f"{defect_str} → defer (transition_point={context_view.is_transition_point})"
    return f"{defect_str} → {policy.value}"