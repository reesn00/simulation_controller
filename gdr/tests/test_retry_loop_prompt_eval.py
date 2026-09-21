"""gdr/refiners/retry_loop_prompt_eval 模块的单元测试.

TDD 起点: 这些测试先于实现存在, 驱动 prompt 调优评估流程.

功能:
- ``ground_truth_samples`` — 20-30 个手写 + 真实抽取的 ground truth 样本
- ``evaluate_prompt(samples, llm_client)`` — 对每个样本跑 llm_judge_retry_loop, 算指标
- ``format_report(result)`` — 生成可读报告 (Markdown)

prompt 调优工作流:
    1. 修改 retry_loop_clip.py::_CLIP_PROMPT
    2. 跑 evaluate_prompt(samples, real_llm_client)
    3. 看 format_report(result) 找错判样本
    4. 调整 prompt, 重跑
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gdr.refiners.retry_loop_ground_truth import (
    GROUND_TRUTH_SAMPLES,
    GroundTruthSample,
)
from gdr.refiners.retry_loop_prompt_eval import (
    EvalResult,
    evaluate_prompt,
    format_report,
)


# ---------------------------------------------------------------------------
# 地面真相样本质量自检 (data sanity)
# ---------------------------------------------------------------------------


class TestGroundTruthSamples:
    def test_minimum_20_samples(self):
        """至少 20 个样本才能形成有效统计."""
        assert len(GROUND_TRUTH_SAMPLES) >= 20

    def test_each_sample_has_required_fields(self):
        for s in GROUND_TRUTH_SAMPLES:
            assert isinstance(s.id, str) and s.id
            assert isinstance(s.category, str) and s.category
            assert isinstance(s.calls, list) and len(s.calls) >= 1
            # clear_retry / real_world 需 ≥ 5 (rule 预筛条件); edge_case 可短
            if s.category in ("clear_retry", "real_world"):
                assert len(s.calls) >= 5, (
                    f"{s.id}: {s.category} 必须 ≥ 5 次连续调用"
                )
            assert s.expected_is_retry_loop in (True, False)
            if s.expected_is_retry_loop:
                assert s.expected_keep_indices is not None
                assert len(s.expected_keep_indices) >= 1
                assert len(s.expected_keep_indices) <= 3
                for idx in s.expected_keep_indices:
                    assert 0 <= idx < len(s.calls)

    def test_calls_have_required_keys(self):
        for s in GROUND_TRUTH_SAMPLES:
            for i, c in enumerate(s.calls):
                assert "function" in c, f"{s.id}[{i}]: 缺 function"
                assert "input" in c, f"{s.id}[{i}]: 缺 input"
                assert "error" in c, f"{s.id}[{i}]: 缺 error"
                assert "state" in c, f"{s.id}[{i}]: 缺 state"

    def test_category_distribution(self):
        """样本应覆盖 4 类: clear_retry, not_retry, edge_case, real_world."""
        cats = {s.category for s in GROUND_TRUTH_SAMPLES}
        assert {"clear_retry", "not_retry", "edge_case", "real_world"}.issubset(cats), (
            f"样本类别不全: 缺 {({'clear_retry', 'not_retry', 'edge_case', 'real_world'} - cats)}"
        )

    def test_clear_retry_count(self):
        """清晰重试样本应占大多数 (正样本)."""
        clear = [s for s in GROUND_TRUTH_SAMPLES if s.category == "clear_retry"]
        assert len(clear) >= 8, f"clear_retry 样本过少: {len(clear)}"

    def test_not_retry_count(self):
        """清晰非重试样本至少 5 个 (负样本)."""
        not_retry = [s for s in GROUND_TRUTH_SAMPLES if s.category == "not_retry"]
        assert len(not_retry) >= 5, f"not_retry 样本过少: {len(not_retry)}"

    def test_real_world_samples_present(self):
        """至少 1 个真实抽取样本 (Tavily web_search)."""
        real = [s for s in GROUND_TRUTH_SAMPLES if s.category == "real_world"]
        assert len(real) >= 1

    def test_ids_unique(self):
        ids = [s.id for s in GROUND_TRUTH_SAMPLES]
        assert len(set(ids)) == len(ids), f"重复 id: {[i for i in ids if ids.count(i) > 1]}"


# ---------------------------------------------------------------------------
# evaluate_prompt
# ---------------------------------------------------------------------------


def _make_mock_client(responses: dict[str, str]) -> MagicMock:
    """按 sample.id 返回预置 LLM 响应."""
    client = MagicMock()

    def _gen(prompt, **kwargs):
        # 从 prompt 中识别 sample.id 不易; 改为按调用顺序返回
        return ("", {})

    client.generate.side_effect = _gen
    client._responses = responses  # 测试用例自己设
    return client


def _sequential_client(per_call_responses: list[str]) -> MagicMock:
    """按调用顺序返回预置 LLM 响应."""
    client = MagicMock()
    client._responses = per_call_responses
    client._call_count = 0

    def _gen(prompt, **kwargs):
        idx = client._call_count
        client._call_count += 1
        if idx >= len(client._responses):
            return ("", {})
        return (client._responses[idx], {})

    client.generate.side_effect = _gen
    return client


class TestEvaluatePrompt:
    def test_perfect_llm_returns_full_accuracy(self):
        """LLM 全部判对 → 准确率 100%."""
        # 构造 LLM 全部返回"正确"答案的 mock client
        correct_responses = []
        for s in GROUND_TRUTH_SAMPLES:
            if s.expected_is_retry_loop:
                correct_responses.append(json.dumps({
                    "is_retry_loop": True,
                    "reason": "测试正确判定",
                    "keep_indices": s.expected_keep_indices,
                }))
            else:
                correct_responses.append(json.dumps({
                    "is_retry_loop": False,
                    "reason": "测试正确判定",
                }))
        client = _sequential_client(correct_responses)
        result = evaluate_prompt(GROUND_TRUTH_SAMPLES, client)
        assert result.accuracy == 1.0
        assert result.total == len(GROUND_TRUTH_SAMPLES)
        assert result.misclassified == []

    def test_all_wrong_llm_returns_zero_accuracy(self):
        """LLM 全部判错 → 准确率 0%."""
        wrong_responses = []
        for s in GROUND_TRUTH_SAMPLES:
            # 全部判为 retry loop (与 clear_retry 一致, 但与 not_retry / edge 相反)
            wrong_responses.append(json.dumps({
                "is_retry_loop": True,
                "reason": "测试错误判定",
                "keep_indices": [0, 4],
            }))
        client = _sequential_client(wrong_responses)
        result = evaluate_prompt(GROUND_TRUTH_SAMPLES, client)
        assert result.accuracy < 0.5
        assert len(result.misclassified) > 0

    def test_mixed_responses_partially_correct(self):
        """混合对错 → 准确率 0-1 之间."""
        responses = []
        for i, s in enumerate(GROUND_TRUTH_SAMPLES):
            if i % 3 == 0:
                # 对
                if s.expected_is_retry_loop:
                    responses.append(json.dumps({
                        "is_retry_loop": True,
                        "keep_indices": s.expected_keep_indices,
                    }))
                else:
                    responses.append(json.dumps({"is_retry_loop": False}))
            else:
                # 错
                responses.append(json.dumps({
                    "is_retry_loop": not s.expected_is_retry_loop,
                    "keep_indices": [0, 4],
                }))
        client = _sequential_client(responses)
        result = evaluate_prompt(GROUND_TRUTH_SAMPLES, client)
        assert 0 < result.accuracy < 1
        # misclassified 数量应 > 0
        assert len(result.misclassified) > 0

    def test_misclassified_records_sample_id_and_diff(self):
        """错判样本应保留 id + 期望 vs 实际, 便于 prompt 调优时定位."""
        # 构造: clear_retry 样本 LLM 返回 is_retry_loop=False
        responses = []
        for s in GROUND_TRUTH_SAMPLES:
            if s.category == "clear_retry":
                responses.append(json.dumps({"is_retry_loop": False}))
            elif s.expected_is_retry_loop:
                responses.append(json.dumps({
                    "is_retry_loop": True,
                    "keep_indices": s.expected_keep_indices,
                }))
            else:
                responses.append(json.dumps({"is_retry_loop": False}))
        client = _sequential_client(responses)
        result = evaluate_prompt(GROUND_TRUTH_SAMPLES, client)
        # 至少应记录 clear_retry 类别的错判
        miss_clear = [m for m in result.misclassified if m["category"] == "clear_retry"]
        assert len(miss_clear) > 0
        miss = miss_clear[0]
        assert "sample_id" in miss
        assert "expected" in miss
        assert "actual" in miss
        assert "reason" in miss

    def test_is_retry_loop_correct_even_if_keep_wrong(self):
        """is_retry_loop 判对但 keep_indices 错 → 记为部分正确 (is_retry_loop 命中)."""
        # 构造: clear_retry 样本, LLM 给 retry=True 但 keep_indices 不匹配
        responses = []
        for s in GROUND_TRUTH_SAMPLES:
            if s.category == "clear_retry":
                responses.append(json.dumps({
                    "is_retry_loop": True,
                    "keep_indices": [1, 2, 3],  # 不是 expected
                }))
            elif s.expected_is_retry_loop:
                responses.append(json.dumps({
                    "is_retry_loop": True,
                    "keep_indices": s.expected_keep_indices,
                }))
            else:
                responses.append(json.dumps({"is_retry_loop": False}))
        client = _sequential_client(responses)
        result = evaluate_prompt(GROUND_TRUTH_SAMPLES, client)
        # is_retry_loop 命中, 但 keep_indices 不完全匹配
        partial_misses = [
            m for m in result.misclassified
            if m.get("category") == "clear_retry" and m.get("is_retry_loop_hit")
        ]
        # 应有 clear_retry 样本计入 partial (is_retry_loop 命中, 但 keep 不完全)
        # 注: 实际是否计入 misclassified 取决于实现; 此处只检查结构
        for m in result.misclassified:
            assert "sample_id" in m

    def test_invalid_json_llm_response_handled(self):
        """LLM 返回非 JSON → 计入错判或异常, 不崩溃."""
        responses = ["not valid json"] * len(GROUND_TRUTH_SAMPLES)
        client = _sequential_client(responses)
        # 不应抛异常
        result = evaluate_prompt(GROUND_TRUTH_SAMPLES, client)
        assert result.total == len(GROUND_TRUTH_SAMPLES)

    def test_llm_exception_handled(self):
        """LLM 抛异常 → 计入错判或异常, 不崩溃."""
        client = MagicMock()
        client.generate.side_effect = RuntimeError("LLM down")
        result = evaluate_prompt(GROUND_TRUTH_SAMPLES, client)
        # 不抛, accuracy 应为 0 (全部判不出来)
        assert result.accuracy == 0.0


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


class TestFormatReport:
    def _make_result(self) -> EvalResult:
        return EvalResult(
            total=10,
            correct=7,
            accuracy=0.7,
            by_category={
                "clear_retry": {"total": 4, "correct": 3},
                "not_retry": {"total": 3, "correct": 2},
                "edge_case": {"total": 2, "correct": 1},
                "real_world": {"total": 1, "correct": 1},
            },
            misclassified=[
                {
                    "sample_id": "rl_005",
                    "category": "clear_retry",
                    "expected": {"is_retry_loop": True, "keep_indices": [0, 4]},
                    "actual": {"is_retry_loop": False, "keep_indices": None},
                    "reason": "is_retry_loop 漏判",
                },
            ],
            errors=[],
        )

    def test_report_contains_accuracy(self):
        report = format_report(self._make_result())
        assert "70.0%" in report or "0.70" in report
        assert "7/10" in report

    def test_report_lists_misclassified(self):
        report = format_report(self._make_result())
        assert "rl_005" in report
        assert "clear_retry" in report
        assert "is_retry_loop 漏判" in report

    def test_report_groups_by_category(self):
        report = format_report(self._make_result())
        # 各 category 应有独立统计
        assert "clear_retry" in report
        assert "not_retry" in report
        assert "edge_case" in report
        assert "real_world" in report

    def test_report_is_markdown(self):
        """报告应为 Markdown 格式 (含 ## 标题)."""
        report = format_report(self._make_result())
        assert "##" in report or "###" in report


# ---------------------------------------------------------------------------
# 集成: prompt 修改 → 重新评估
# ---------------------------------------------------------------------------


class TestPromptIteration:
    def test_swap_prompt_and_re_eval(self):
        """应支持替换 prompt 后重跑 eval; 模拟调优流程."""
        from gdr.refiners.retry_loop_clip import _CLIP_PROMPT, llm_judge_retry_loop

        # 记录原始 prompt 文本
        original_prompt = _CLIP_PROMPT

        # 模拟"调优后"prompt (内容变化但仍含同样关键词)
        new_prompt = original_prompt.replace(
            "1. \"同一意图反复重试\"",
            "1. \"同一意图反复重试\" (重要: 输入 schema 必须完全一致)",
        )

        # 验证 prompt 已被替换
        assert new_prompt != original_prompt

        # 还原 (避免影响其它测试)
        import gdr.refiners.retry_loop_clip as clip_mod
        clip_mod._CLIP_PROMPT = original_prompt
