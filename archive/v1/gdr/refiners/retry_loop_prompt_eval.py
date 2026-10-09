"""gdr/refiners/retry_loop_prompt_eval: 重试循环 LLM 判定 prompt 调优评估.

提供:
    - ``EvalResult`` — 评估结果数据类 (准确率, 分类别统计, 错判列表)
    - ``evaluate_prompt(samples, llm_client)`` — 对 ground truth 样本逐个跑 LLM, 算指标
    - ``format_report(result)`` — 生成 Markdown 报告

调优工作流:
    1. 改 retry_loop_clip.py::_CLIP_PROMPT
    2. 跑 evaluate_prompt(GROUND_TRUTH_SAMPLES, real_llm_client)
    3. format_report(result) → 看错判, 调 prompt

判定规则:
    - ``expected_is_retry_loop=True, actual_is_retry_loop=False`` → 漏判 (FN)
    - ``expected_is_retry_loop=False, actual_is_retry_loop=True`` → 误判 (FP)
    - ``is_retry_loop=True`` 但 keep_indices 与期望不匹配 → 部分正确 (is_retry_loop 命中, keep 错)
    - LLM 调用异常 / JSON 解析失败 → 视为漏判 (保守)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from gdr.refiners.retry_loop_ground_truth import (
    GROUND_TRUTH_SAMPLES,
    GroundTruthSample,
)
from gdr.refiners.retry_loop_clip import (
    _summarize_call,
    _CLIP_PROMPT,
)

log = logging.getLogger(__name__)


@dataclass
class EvalResult:
    total: int
    correct: int
    accuracy: float
    by_category: dict[str, dict[str, int]]
    misclassified: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


def _keep_indices_match(expected: list[int] | None, actual: list[int] | None) -> bool:
    """keep_indices 是否视为"匹配":
    - 任一为 None → 不匹配
    - 集合相等 (顺序不敏感) → 匹配
    """
    if expected is None or actual is None:
        return False
    return set(expected) == set(actual)


def _evaluate_one(
    sample: GroundTruthSample,
    llm_client: Any,
) -> dict[str, Any]:
    """对单个样本跑 LLM, 返回判定结果 dict."""
    calls_payload = json.dumps(
        [_summarize_call(call, call) for call in sample.calls],
        ensure_ascii=False, indent=2,
    )
    prompt = _CLIP_PROMPT.format(
        count=len(sample.calls), max_keep=3, calls=calls_payload,
    )

    try:
        raw, _meta = llm_client.generate(prompt, max_tokens=400)
    except Exception as e:
        return {
            "ok": False,
            "is_retry_loop": False,
            "keep_indices": None,
            "error": f"llm_call_error: {e}",
        }
    try:
        parsed = json.loads(raw)
    except Exception as e:
        return {
            "ok": False,
            "is_retry_loop": False,
            "keep_indices": None,
            "error": f"json_parse_error: {e}",
        }

    if not isinstance(parsed, dict):
        return {
            "ok": False,
            "is_retry_loop": False,
            "keep_indices": None,
            "error": "non_dict_response",
        }

    return {
        "ok": True,
        "is_retry_loop": bool(parsed.get("is_retry_loop", False)),
        "keep_indices": parsed.get("keep_indices"),
        "reason": parsed.get("reason", ""),
    }


def evaluate_prompt(
    samples: list[GroundTruthSample],
    llm_client: Any,
) -> EvalResult:
    """对每个样本跑 LLM, 统计准确率 + 错判列表."""
    total = len(samples)
    correct = 0
    by_category: dict[str, dict[str, int]] = {}
    misclassified: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for sample in samples:
        by_category.setdefault(sample.category, {"total": 0, "correct": 0})
        by_category[sample.category]["total"] += 1

        result = _evaluate_one(sample, llm_client)

        if not result["ok"]:
            errors.append({
                "sample_id": sample.id,
                "category": sample.category,
                "error": result.get("error", ""),
            })
            # 视为漏判 (保守, 计入 misclassified)
            misclassified.append({
                "sample_id": sample.id,
                "category": sample.category,
                "expected": {
                    "is_retry_loop": sample.expected_is_retry_loop,
                    "keep_indices": sample.expected_keep_indices,
                },
                "actual": {
                    "is_retry_loop": False,
                    "keep_indices": None,
                },
                "reason": result.get("error", "LLM 调用失败"),
            })
            continue

        actual_is_retry = result["is_retry_loop"]
        actual_keep = result["keep_indices"]
        is_retry_loop_hit = (actual_is_retry == sample.expected_is_retry_loop)
        # keep_indices 匹配: 仅当 expected_is_retry_loop=True 时才有意义
        # (expected=False 时 expected_keep_indices=None, 不应比较 keep_match)
        if sample.expected_is_retry_loop:
            keep_match = _keep_indices_match(sample.expected_keep_indices, actual_keep)
        else:
            keep_match = True  # 不参与判定

        if is_retry_loop_hit and keep_match:
            correct += 1
            by_category[sample.category]["correct"] += 1
        else:
            misclassified.append({
                "sample_id": sample.id,
                "category": sample.category,
                "expected": {
                    "is_retry_loop": sample.expected_is_retry_loop,
                    "keep_indices": sample.expected_keep_indices,
                },
                "actual": {
                    "is_retry_loop": actual_is_retry,
                    "keep_indices": actual_keep,
                },
                "is_retry_loop_hit": is_retry_loop_hit,
                "keep_match": keep_match,
                "reason": result.get("reason", ""),
                "llm_keep_indices_raw": actual_keep,
            })

    accuracy = correct / total if total > 0 else 0.0
    return EvalResult(
        total=total,
        correct=correct,
        accuracy=accuracy,
        by_category=by_category,
        misclassified=misclassified,
        errors=errors,
    )


def format_report(result: EvalResult) -> str:
    """生成 Markdown 格式报告."""
    lines: list[str] = []
    lines.append("# Retry Loop Clip Prompt 评估报告")
    lines.append("")
    lines.append(f"**总样本**: {result.total}")
    lines.append(f"**判对**: {result.correct}")
    lines.append(f"**判错**: {len(result.misclassified)}")
    lines.append(f"**LLM 错误**: {len(result.errors)}")
    lines.append(f"**准确率**: {result.accuracy:.1%} ({result.correct}/{result.total})")
    lines.append("")

    lines.append("## 按类别统计")
    lines.append("")
    lines.append("| 类别 | 总数 | 判对 | 准确率 |")
    lines.append("|---|---|---|---|")
    for cat in sorted(result.by_category):
        stats = result.by_category[cat]
        cat_total = stats["total"]
        cat_correct = stats["correct"]
        cat_acc = cat_correct / cat_total if cat_total > 0 else 0.0
        lines.append(f"| {cat} | {cat_total} | {cat_correct} | {cat_acc:.1%} |")
    lines.append("")

    if result.errors:
        lines.append("## LLM 调用错误")
        lines.append("")
        for err in result.errors:
            lines.append(f"- `{err['sample_id']}` ({err['category']}): {err['error']}")
        lines.append("")

    if result.misclassified:
        lines.append("## 错判样本")
        lines.append("")
        for m in result.misclassified:
            lines.append(f"### {m['sample_id']} ({m['category']})")
            lines.append("")
            lines.append(f"- 期望: `is_retry_loop={m['expected']['is_retry_loop']}`, "
                         f"`keep_indices={m['expected']['keep_indices']}`")
            lines.append(f"- 实际: `is_retry_loop={m['actual']['is_retry_loop']}`, "
                         f"`keep_indices={m['actual']['keep_indices']}`")
            if m.get("is_retry_loop_hit") is not None:
                lines.append(f"- is_retry_loop 命中: {m['is_retry_loop_hit']}")
                lines.append(f"- keep_indices 匹配: {m['keep_match']}")
            if m.get("reason"):
                lines.append(f"- LLM reason: {m['reason']}")
            lines.append("")

    return "\n".join(lines)
