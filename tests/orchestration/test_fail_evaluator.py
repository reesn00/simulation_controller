"""orchestration.fail_evaluator 测试.

重点覆盖两条:
1. **raw CoT 红线** —— 发给 LLM 的 prompt 里绝不能出现 ``<think>`` 原始
   推理链 (CLAUDE.md 隐私红线)。
2. **分数恒为 0 + fail-soft** —— LLM 只做定性归因, 不参与打分; LLM 挂掉
   不能阻断 etl。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestration.fail_evaluator import (
    FAILURE_CATEGORIES,
    SCHEMA_VERSION,
    _format_failures,
    evaluate_failed_run,
    extract_final_reply,
    inject_fail_evaluation,
    strip_raw_cot,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _write_trajectory(path: Path, texts: list[str]) -> Path:
    """写一个最小 C1 trajectory JSONL (含 raw CoT 的 text block)."""
    lines = []
    for text in texts:
        lines.append(json.dumps({
            "event_type": "model_response",
            "payload": {"content": [{"type": "text", "text": text}]},
        }, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _FakeGdrSettings:
    """够用的假 Settings —— 被测逻辑只看 main_model / llm_timeout_s."""

    main_model = "fake-model"
    llm_timeout_s = 30


# ---------------------------------------------------------------------------
# raw CoT 红线
# ---------------------------------------------------------------------------


def test_strip_raw_cot_removes_closed_think_block() -> None:
    raw = "<think>内部推理: 用户要盗版链接\n我应该拒绝</think>\n这是给用户的正文。"
    assert strip_raw_cot(raw) == "这是给用户的正文。"


def test_strip_raw_cot_drops_unclosed_think_tail() -> None:
    """截断的 trajectory: 未闭合 think 之后的内容也是推理链, 一并丢弃."""
    assert strip_raw_cot("正文开头<think>推理开始了但没闭合\n后面全是推理") == "正文开头"


def test_strip_raw_cot_handles_multiple_and_empty() -> None:
    raw = "<think>a</think>正文<think>b</think>尾巴"
    assert strip_raw_cot(raw) == "正文尾巴"
    assert strip_raw_cot("") == ""
    assert strip_raw_cot("没有think的纯正文") == "没有think的纯正文"


def test_extract_final_reply_strips_cot_and_takes_last(tmp_path: Path) -> None:
    """取最后一条 model_response 的 text, 且 CoT 已剥离."""
    traj = _write_trajectory(tmp_path / "t.json", [
        "<think>第一轮推理</think>第一轮正文",
        "<think>第二轮推理:拒绝理由</think>第二轮正文, 给你正版平台",
    ])
    result = extract_final_reply(traj)
    assert "第二轮正文" in result
    assert "第一轮正文" not in result
    assert "<think>" not in result
    assert "第二轮推理" not in result


def test_extract_final_reply_missing_file_returns_empty(tmp_path: Path) -> None:
    assert extract_final_reply(tmp_path / "nope.json") == ""


def test_evaluate_prompt_never_contains_raw_cot(tmp_path: Path, monkeypatch) -> None:
    """端到端红线断言: 真正构造出的 LLM messages 里不得出现 raw CoT."""
    traj = _write_trajectory(tmp_path / "t.json", [
        "<think>绝密推理:版权规避方案</think>我拒绝提供盗版链接。",
    ])

    captured: dict[str, object] = {}

    class _FakeClient:
        @classmethod
        def get(cls, *a, **kw):
            return cls()

        def chat(self, messages, **kw):
            captured["messages"] = messages
            return (json.dumps({
                "failure_category": "refusal",
                "root_cause": "版权理由拒答",
                "agent_intent": "推荐正版平台",
                "should_revise_task": True,
                "review_priority": "high",
                "review_note": "任务本身不可判定",
            }), {})

    import gdr.infrastructure.llm_client as llm_mod

    monkeypatch.setattr(llm_mod, "LlamaCppClient", _FakeClient)

    evaluation = evaluate_failed_run(
        run_id="run-x",
        trajectory_path=traj,
        criterion_evaluation={
            "final_verdict": "fail",
            "rounds": 1,
            "criteria": [{"criterion_id": "c1", "verdict": "fail",
                          "reason_code": "URL_MISSING", "message": "URL 不足 0/1"}],
        },
        gdr_settings=_FakeGdrSettings(),
    )

    assert evaluation is not None
    blob = json.dumps(captured["messages"], ensure_ascii=False)
    assert "<think>" not in blob
    assert "绝密推理" not in blob
    assert "版权规避方案" not in blob
    # 但正文确实送进去了 — 剥离不能把内容也吃掉
    assert "我拒绝提供盗版链接" in blob


# ---------------------------------------------------------------------------
# 分数恒为 0 + 归因结构
# ---------------------------------------------------------------------------


def test_evaluate_scores_zero_and_records_attribution(tmp_path: Path, monkeypatch) -> None:
    class _FakeClient:
        @classmethod
        def get(cls, *a, **kw):
            return cls()

        def chat(self, messages, **kw):
            return (json.dumps({
                "failure_category": "refusal",
                "root_cause": "版权理由拒答",
                "agent_intent": "推荐正版平台",
                "should_revise_task": True,
                "review_priority": "high",
                "review_note": "任务本身不可判定",
            }), {})

    import gdr.infrastructure.llm_client as llm_mod

    monkeypatch.setattr(llm_mod, "LlamaCppClient", _FakeClient)

    evaluation = evaluate_failed_run(
        run_id="run-1",
        trajectory_path=_write_trajectory(tmp_path / "t.json", ["正文"]),
        criterion_evaluation={
            "final_verdict": "fail", "rounds": 1,
            "criteria": [{"criterion_id": "c1", "verdict": "fail",
                          "reason_code": "URL_MISSING", "message": "URL 不足"}],
        },
        gdr_settings=_FakeGdrSettings(),
    )

    assert evaluation is not None
    # 分层: 分数恒 0, 来自 simulate 端确定性校验, 不是 LLM 裁量
    assert evaluation["score"] == 0.0
    assert evaluation["score_source"] == "simulate_validation"
    assert evaluation["schema_version"] == SCHEMA_VERSION
    assert evaluation["run_id"] == "run-1"
    assert evaluation["final_verdict"] == "fail"
    assert evaluation["failure_category"] == "refusal"
    assert evaluation["review_priority"] == "high"
    assert evaluation["should_revise_task"] is True


def test_evaluate_normalizes_out_of_range_llm_fields(tmp_path: Path, monkeypatch) -> None:
    """LLM 返回越界的分类/优先级时收敛到合法值, 不让脏数据进 C3."""

    class _FakeClient:
        @classmethod
        def get(cls, *a, **kw):
            return cls()

        def chat(self, messages, **kw):
            return (json.dumps({
                "failure_category": "胡说的分类",
                "review_priority": "urgent",
            }), {})

    import gdr.infrastructure.llm_client as llm_mod

    monkeypatch.setattr(llm_mod, "LlamaCppClient", _FakeClient)

    evaluation = evaluate_failed_run(
        run_id="run-2",
        trajectory_path=None,
        criterion_evaluation={"final_verdict": "fail", "rounds": 1, "criteria": []},
        gdr_settings=_FakeGdrSettings(),
    )

    assert evaluation is not None
    assert evaluation["failure_category"] == "unknown"
    assert evaluation["failure_category"] in FAILURE_CATEGORIES
    assert evaluation["review_priority"] == "medium"


def test_evaluate_tolerates_field_name_drift(tmp_path: Path, monkeypatch) -> None:
    """后端不支持 json_schema 时模型会改写键名 (实测 root_cause → root__agent).

    归因不能因此整条丢失 —— 字段名漂移要能收敛回 schema。
    """

    class _DriftingClient:
        @classmethod
        def get(cls, *a, **kw):
            return cls()

        def chat(self, messages, **kw):
            # 第一轮漂字段名 (缺 required 字段 → 触发重试); 第二轮正常
            if len(messages) <= 2:
                return (json.dumps({
                    "category": "refusal",
                    "root__agent": "版权理由拒答",
                    "note": "任务不可判定",
                }), {})
            return (json.dumps({
                "failure_category": "refusal",
                "root_cause": "版权理由拒答",
                "review_note": "任务不可判定",
            }), {})

    import gdr.infrastructure.llm_client as llm_mod

    monkeypatch.setattr(llm_mod, "LlamaCppClient", _DriftingClient)

    evaluation = evaluate_failed_run(
        run_id="run-drift",
        trajectory_path=None,
        criterion_evaluation={"final_verdict": "fail", "rounds": 1, "criteria": []},
        gdr_settings=_FakeGdrSettings(),
    )

    assert evaluation is not None
    assert evaluation["failure_category"] == "refusal"
    assert evaluation["root_cause"] == "版权理由拒答"
    assert evaluation["review_note"] == "任务不可判定"


def test_evaluate_retries_on_incomplete_output(tmp_path: Path, monkeypatch) -> None:
    """首轮没按 schema 输出 → 带字段名提示重试一次, 而不是直接放弃."""
    calls: list[int] = []

    class _SloppyClient:
        @classmethod
        def get(cls, *a, **kw):
            return cls()

        def chat(self, messages, **kw):
            calls.append(len(messages))
            if len(calls) == 1:
                return ("这不是 JSON, 只是模型的闲聊。", {})
            return (json.dumps({
                "failure_category": "capability_gap",
                "root_cause": "推理错误",
                "review_note": "需复核",
            }), {})

    import gdr.infrastructure.llm_client as llm_mod

    monkeypatch.setattr(llm_mod, "LlamaCppClient", _SloppyClient)

    evaluation = evaluate_failed_run(
        run_id="run-retry",
        trajectory_path=None,
        criterion_evaluation={"final_verdict": "fail", "rounds": 1, "criteria": []},
        gdr_settings=_FakeGdrSettings(),
    )

    assert len(calls) == 2          # 重试过一次
    assert evaluation is not None
    assert evaluation["failure_category"] == "capability_gap"


def test_evaluate_still_degrades_when_never_parseable(tmp_path: Path, monkeypatch) -> None:
    """两次都解析不出来 → 仍返回可用结果 (字段降级), 不抛异常."""

    class _GarbageClient:
        @classmethod
        def get(cls, *a, **kw):
            return cls()

        def chat(self, messages, **kw):
            return ("完全不是 JSON", {})

    import gdr.infrastructure.llm_client as llm_mod

    monkeypatch.setattr(llm_mod, "LlamaCppClient", _GarbageClient)

    evaluation = evaluate_failed_run(
        run_id="run-garbage",
        trajectory_path=None,
        criterion_evaluation={"final_verdict": "fail", "rounds": 1, "criteria": []},
        gdr_settings=_FakeGdrSettings(),
    )

    # 归因缺失但样本仍带 0 分标记 —— 分数信息比归因更重要, 不能一起丢
    assert evaluation is not None
    assert evaluation["score"] == 0.0
    assert evaluation["final_verdict"] == "fail"
    assert evaluation["failure_category"] == "unknown"


def test_evaluate_skipped_when_verdict_pass(tmp_path: Path, monkeypatch) -> None:
    """验证通过的 run 不该带失败归因 —— 也不该白花一次 LLM 调用."""

    def _boom(*a, **kw):
        raise AssertionError("验证通过时不应调 LLM")

    import gdr.infrastructure.llm_client as llm_mod

    monkeypatch.setattr(llm_mod, "LlamaCppClient", _boom)

    assert evaluate_failed_run(
        run_id="run-3",
        trajectory_path=None,
        criterion_evaluation={"final_verdict": "pass", "rounds": 1, "criteria": []},
        gdr_settings=_FakeGdrSettings(),
    ) is None


def test_evaluate_skipped_without_criterion_evaluation(tmp_path: Path) -> None:
    assert evaluate_failed_run(
        run_id="run-4",
        trajectory_path=None,
        criterion_evaluation=None,
        gdr_settings=_FakeGdrSettings(),
    ) is None


def test_evaluate_is_fail_soft_on_llm_error(tmp_path: Path, monkeypatch) -> None:
    """LLM 挂掉 → 返回 None, 由调用方跳过注入; 绝不能抛给 etl."""

    class _BoomClient:
        @classmethod
        def get(cls, *a, **kw):
            return cls()

        def chat(self, messages, **kw):
            raise RuntimeError("LLM 500")

    import gdr.infrastructure.llm_client as llm_mod

    monkeypatch.setattr(llm_mod, "LlamaCppClient", _BoomClient)

    assert evaluate_failed_run(
        run_id="run-5",
        trajectory_path=None,
        criterion_evaluation={"final_verdict": "fail", "rounds": 1, "criteria": []},
        gdr_settings=_FakeGdrSettings(),
    ) is None


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.metadata: dict = {}
        self.session_id = "s1"


def test_inject_writes_metadata() -> None:
    session = _FakeSession()
    assert inject_fail_evaluation(session, {"score": 0.0}) is True
    assert session.metadata["fail_evaluation"]["score"] == 0.0


def test_inject_none_is_noop_and_does_not_wipe() -> None:
    session = _FakeSession()
    session.metadata = {"fail_evaluation": {"score": 0.0}}
    assert inject_fail_evaluation(session, None) is False
    assert session.metadata["fail_evaluation"] == {"score": 0.0}


# ---------------------------------------------------------------------------
# 失败原因渲染
# ---------------------------------------------------------------------------


def test_format_failures_lists_non_pass_items() -> None:
    rendered = _format_failures({
        "final_verdict": "fail",
        "rounds": 2,
        "criteria": [
            {"criterion_id": "ok", "verdict": "pass", "reason_code": "PASSED", "message": ""},
            {"criterion_id": "bad", "verdict": "fail",
             "reason_code": "URL_MISSING", "message": "URL 不足 0/1"},
        ],
        "missing_items": ["缺链接"],
    })
    assert "最终判定: fail" in rendered
    assert "未通过项: 1/2" in rendered
    assert "URL_MISSING" in rendered
    assert "缺链接" in rendered
    assert "[ok]" not in rendered  # 通过项不进清单


def test_format_failures_handles_none() -> None:
    assert "未取到验证报告" in _format_failures(None)
