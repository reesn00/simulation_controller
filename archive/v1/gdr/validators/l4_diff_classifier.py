"""轨迹级 diff 分类器 (方案 trajectory-scoring-two-layer.md §2.1 维度2).

对每个 BlockRefineRecord 判断差异类型:
  - required_change: 指令要求的改动 (edit_status=EDITED 且校验通过)
  - incidental_change: 无指令依据的改动 (edit_status=NEEDS_REVIEW)
  - regression: 把原来对的地方改坏了 (edit_status=ROLLBACK 或校验失败)

rule_first 模式零 LLM; hybrid 模式对模糊项 LLM 兜底 (TODO).
"""
from __future__ import annotations

import logging
from typing import Any

from domain import BlockRefineRecord, DiffItem, StepEditStatus

log = logging.getLogger(__name__)


def _step_range(record: BlockRefineRecord) -> str:
    """把 BlockIndex 转为 step_range 字符串."""
    bi = record.block_index
    return f"{bi.msg_idx}-{bi.block_idx}"


def _has_validation_failure(record: BlockRefineRecord) -> bool:
    """检查 validation_results 中是否有 L1/L2/L3 失败."""
    for vr in record.validation_results:
        if not vr.passed:
            return True
    return False


def classify_one(record: BlockRefineRecord, cfg: Any) -> DiffItem | None:
    """对单条 BlockRefineRecord 分类, 返回 DiffItem 或 None (无差异)."""
    status = record.edit_status

    if status == StepEditStatus.UNTOUCHED:
        return None
    if status == StepEditStatus.PRESERVED:
        return None

    note = f"module={record.module}, status={status.value}"
    step_rng = _step_range(record)

    if status == StepEditStatus.ROLLBACK:
        return DiffItem(
            step_range=step_rng,
            type="regression",
            note=f"回滚: {note}",
        )

    if status == StepEditStatus.NEEDS_REVIEW:
        return DiffItem(
            step_range=step_rng,
            type="incidental_change",
            note=f"需人工审核: {note}",
        )

    if status == StepEditStatus.EDITED:
        if _has_validation_failure(record):
            return DiffItem(
                step_range=step_rng,
                type="regression",
                note=f"编辑后校验失败: {note}",
            )
        return DiffItem(
            step_range=step_rng,
            type="required_change",
            note=f"按缺陷修复: {note}",
        )

    return None


def classify_batch(
    records: list[BlockRefineRecord],
    cfg: Any,
) -> list[DiffItem]:
    """对一批 BlockRefineRecord 分类, 返回 diff_summary."""
    mode = getattr(cfg, "compare_diff_classifier", "rule_first")
    items: list[DiffItem] = []
    for rec in records:
        item = classify_one(rec, cfg)
        if item is not None:
            items.append(item)

    if mode == "llm_only":
        log.warning(
            "compare_diff_classifier=llm_only not yet implemented; "
            "falling back to rule_first results",
        )
    elif mode == "hybrid":
        log.warning(
            "compare_diff_classifier=hybrid LLM fallback not yet implemented; "
            "using rule_first results",
        )
    return items


def has_regression(items: list[DiffItem]) -> bool:
    """diff_summary 中是否存在 regression 类型."""
    return any(it.type == "regression" for it in items)


def adherence_score(items: list[DiffItem]) -> str:
    """根据 diff_summary 判定 instruction_adherence.score."""
    if not items:
        return "pass"
    if has_regression(items):
        return "fail"
    incidental_count = sum(1 for it in items if it.type == "incidental_change")
    if incidental_count == 0:
        return "pass"
    if incidental_count <= 2:
        return "review"
    return "fail"
