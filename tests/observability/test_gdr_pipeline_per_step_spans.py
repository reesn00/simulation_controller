"""gdr 流水线 21 步骤 span 接入 contract tests (PR 3 / Commits 3a-3e).

mock 策略: patch ``gdr.observability.runner_helpers.step_span`` 直接验证
helper 传给工厂的 kwargs (name / metadata / session_id / as_type).
这是最稳的 contract test 策略 — 避免 mock 整个 Langfuse SDK 链路.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest import mock

import pytest


# 21 步骤 span 名白名单 (与 docs/langfuse-gdr.md §2.2 一致)
WHITELIST_21_STEPS = {
    "gdr.hard_filter",
    "gdr.light_health",
    "gdr.context_understanding.build",
    "gdr.fold.failed_toolresults",
    "gdr.fold.repeated_thinking",
    "gdr.retry_loop_clip",
    "gdr.cu.retrack_state",
    "gdr.user_intent.heuristic",
    "gdr.router.tag",
    "gdr.policy.decide",
    "gdr.refine.run_repairs",
    "gdr.early_exit",
    "gdr.reassemble",
    "gdr.timeout_fallback",
    "gdr.unhandled_error",
    "gdr.audit.routing_abstain",
    "gdr.incomplete_check",
    "gdr.save_refined_session",
    "gdr.audit.deferred",
    "gdr.audit.judge_low",
}

# reassembler 内部嵌的 3 个 generation 子 span
REASSEMBLE_GENERATION_SUBSPANS = {
    "gdr.reassemble.user_intent_llm",
    "gdr.reassemble.consistency_check",
    "gdr.reassemble.l3_judge",
}


# ---------------------------------------------------------------------------
# 静态分析测试 (无 SDK mock 依赖)
# ---------------------------------------------------------------------------


def test_span_naming_convention_regex() -> None:
    """21 步骤 + outer + 子 span name 命中正则 ``^gdr(?:\\:[A-Za-z0-9_]+)?(?:\\.[a-z0-9_]+)+$``。"""
    import re

    pattern = re.compile(r"^gdr(?:\:[A-Za-z0-9_]+)?(?:\.[a-z0-9_]+)+$")
    all_names = WHITELIST_21_STEPS | REASSEMBLE_GENERATION_SUBSPANS | {
        "gdr.process_one",  # _process_one_file outer
        "gdr.retry_loop_clip.judge",  # retry_loop_clip 子 generation
    }
    for name in sorted(all_names):
        assert pattern.match(name), f"{name!r} fails convention"


def test_all_21_steps_present_in_runner_source() -> None:
    """grep ``gdr/pipeline/runner.py`` 验证 21 个 span name 都被引用过。"""
    import pathlib
    src = pathlib.Path("gdr/pipeline/runner.py").read_text(encoding="utf-8")
    missing = [name for name in WHITELIST_21_STEPS if name not in src]
    assert not missing, f"Missing 21-step span names in runner.py: {missing}"


def test_reassemble_subspans_present_in_reassembler_source() -> None:
    """grep ``gdr/reassembly/reassembler.py`` 验证 3 个 generation 子 span 都被引用。"""
    import pathlib
    src = pathlib.Path("gdr/reassembly/reassembler.py").read_text(encoding="utf-8")
    missing = [
        name for name in REASSEMBLE_GENERATION_SUBSPANS if name not in src
    ]
    assert not missing, (
        f"Missing reassemble sub-span names: {missing}"
    )


def test_retry_loop_clip_judge_subspan_in_refiner_source() -> None:
    """``gdr.retry_loop_clip.judge`` 子 generation span 在 retry_loop_clip.py。"""
    import pathlib
    src = pathlib.Path("gdr/refiners/retry_loop_clip.py").read_text(encoding="utf-8")
    assert "gdr.retry_loop_clip.judge" in src, (
        "Missing retry_loop_clip.judge sub-span name in retry_loop_clip.py"
    )


# ---------------------------------------------------------------------------
# helper contract 测试 (mock step_span 直接验证 kwargs)
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_spans(monkeypatch):
    """替换 ``gdr.observability.runner_helpers.step_span`` 为 capture function.

    返回一个 ``captured`` list: 每条记录是调用 ``step_span(...)`` 时传入的 kwargs.
    """
    from gdr.observability import runner_helpers

    captured: list[dict[str, Any]] = []

    @dataclass
    class _CM:
        """假 cm: __enter__ 返回 span mock, __exit__ 透传."""

        def __enter__(self):
            return mock.MagicMock(name="SpanMock")

        def __exit__(self, *exc):
            return False

    def _capture(*args, **kwargs):
        captured.append(kwargs)
        return _CM()

    monkeypatch.setattr(runner_helpers, "step_span", _capture)
    monkeypatch.setattr(runner_helpers, "get_client", lambda cfg: object())  # 假 client
    return captured


def _enabled_cfg():
    """最小 enable cfg."""

    @dataclass
    class _Cfg:
        langfuse_enabled: bool = True
        langfuse_gdr_per_step_span: bool = True
        langfuse_upload_payload: str = "full"
        langfuse_max_block_payload_bytes: int = 0

    return _Cfg()


def test_step_span_ctx_calls_step_span_with_correct_kwargs(captured_spans) -> None:
    """``_gdr_step_span_ctx("gdr.router.tag", session, metadata=...)`` 把 kwargs
    完整透传到 ``step_span(client, name=..., metadata=..., session_id=..., ...)``。"""
    from gdr.observability import _gdr_step_span_ctx

    class _Session:
        _gdr_cfg = _enabled_cfg()
        session_id = "useramulation-real-abc"

    with _gdr_step_span_ctx(
        "gdr.router.tag", _Session(),
        metadata={
            "candidate_blocks": 12,
            "vote_concurrency": 4,
        },
    ):
        pass

    assert len(captured_spans) == 1
    call = captured_spans[0]
    assert call["name"] == "gdr.router.tag"
    assert call["metadata"] == {
        "candidate_blocks": 12,
        "vote_concurrency": 4,
    }
    assert call["session_id"] == "useramulation-real-abc"
    assert call["payload_mode"] == "full"
    assert call["max_payload_bytes"] == 0
    assert call["input_data"] is not None  # snapshot(session)
    # output_capture 是 callable (闭包)
    assert callable(call["output_capture"])


def test_step_span_ctx_as_type_generation_for_l3_judge(captured_spans) -> None:
    """``gdr.reassemble.l3_judge`` 通过 ``step_span(as_type="generation")`` 起。

    helper 本身不强制设 as_type; reassembler 内部 ``_lf_gen_span`` 直接调
    ``step_span(as_type="generation")``. 本测试验证 helper 透传 span 名与
    metadata 时不掺杂 as_type, 业务方需自己设。
    """
    from gdr.observability import _gdr_step_span_ctx

    class _Session:
        _gdr_cfg = _enabled_cfg()
        session_id = "s1"

    with _gdr_step_span_ctx(
        "gdr.reassemble.l3_judge", _Session(),
        metadata={"tool": "l3_judge", "judge_score": 7},
    ):
        pass

    call = captured_spans[-1]
    assert call["name"] == "gdr.reassemble.l3_judge"
    assert call["metadata"]["tool"] == "l3_judge"
    # helper 不强制设 as_type, 业务层 (reassembler._lf_gen_span) 显式传
    assert "as_type" not in call  # helper 没设


def test_step_span_ctx_session_id_from_session(captured_spans) -> None:
    """session_id 从 session.session_id 透传 (非空字符串)."""
    from gdr.observability import _gdr_step_span_ctx

    class _Session:
        _gdr_cfg = _enabled_cfg()
        session_id = "useramulation-real-abc"

    with _gdr_step_span_ctx("gdr.hard_filter", _Session()):
        pass

    assert captured_spans[0]["session_id"] == "useramulation-real-abc"


def test_step_span_ctx_yields_none_when_disabled(captured_spans) -> None:
    """``langfuse_enabled=False`` 时 helper yield None, 不调 step_span."""

    @dataclass
    class _Cfg:
        langfuse_enabled: bool = False
        langfuse_gdr_per_step_span: bool = True

    class _Session:
        _gdr_cfg = _Cfg()
        session_id = "s1"

    from gdr.observability import _gdr_step_span_ctx

    with _gdr_step_span_ctx("gdr.hard_filter", _Session()) as span:
        assert span is None
    assert len(captured_spans) == 0


def test_step_span_ctx_yields_none_when_per_step_disabled(captured_spans) -> None:
    """``langfuse_gdr_per_step_span=False`` 时 helper yield None."""

    @dataclass
    class _Cfg:
        langfuse_enabled: bool = True
        langfuse_gdr_per_step_span: bool = False

    class _Session:
        _gdr_cfg = _Cfg()
        session_id = "s1"

    from gdr.observability import _gdr_step_span_ctx

    with _gdr_step_span_ctx("gdr.hard_filter", _Session()) as span:
        assert span is None
    assert len(captured_spans) == 0


def test_step_span_ctx_yields_none_when_no_cfg(captured_spans) -> None:
    """``session._gdr_cfg`` 缺失时 yield None (兜底)."""

    class _Session:
        # 注意: 没有 _gdr_cfg
        session_id = "s1"

    from gdr.observability import _gdr_step_span_ctx

    with _gdr_step_span_ctx("gdr.hard_filter", _Session()) as span:
        assert span is None
    assert len(captured_spans) == 0


def test_step_span_ctx_input_snapshot_is_deepcopy(captured_spans) -> None:
    """``input_data`` 是 session 的 deep copy; 步骤内修改不影响 input.

    用 class-based session (helper 通过 ``getattr(session, "_gdr_cfg", None)``
    读 cfg). snapshot 优先 deepcopy, 失败回退 _to_jsonable; 这里验证:
      1. input 不是同一对象 (deep copy 隔离)
      2. input 含原始 messages 列表
    """
    from gdr.observability import _gdr_step_span_ctx

    @dataclass
    class _Cfg:
        langfuse_enabled: bool = True
        langfuse_gdr_per_step_span: bool = True
        langfuse_upload_payload: str = "full"
        langfuse_max_block_payload_bytes: int = 0

    class _Session:
        def __init__(self):
            self._gdr_cfg = _Cfg()
            self.session_id = "s1"
            self.data = {"messages": ["hello"]}

    s = _Session()
    with _gdr_step_span_ctx("gdr.fold.failed_toolresults", s):
        s.data["messages"].append("modified-inside-step")

    captured_input = captured_spans[0]["input_data"]
    captured_output_callable = captured_spans[0]["output_capture"]

    # s.data 已修改
    assert s.data["messages"] == ["hello", "modified-inside-step"]
    # input 不是同一对象 (deep copy 隔离生效)
    assert captured_input is not s
    # output 是 callable, 真正调用时拿到当前 session
    captured_output = captured_output_callable()
    assert captured_output is s
    # 通过修改 input 验证 input 与 s 是独立对象
    captured_input.data["messages"].append("should-not-leak")
    assert "should-not-leak" not in s.data["messages"], (
        "snapshot must be a deep copy, not a reference"
    )


def test_step_span_ctx_metadata_kwargs_not_unpacked() -> None:
    """helper 把 metadata 作为整体 dict 传给 step_span (避免字段名冲突).

    签名必须是 ``def _gdr_step_span_ctx(name, session, metadata=None, **_ignored)``,
    显式 ``metadata`` 参数 (而非 ``**metadata``); 调用 ``step_span(..., metadata=...)``
    必须用 ``metadata=...`` 形式, 不可 ``step_span(..., **metadata)`` 解包.
    """
    import inspect
    from gdr.observability.runner_helpers import _gdr_step_span_ctx

    src = inspect.getsource(_gdr_step_span_ctx)
    # 必须有 metadata=metadata 或 metadata=dict(metadata) 这种"整体 dict 传入"
    assert ("metadata=metadata" in src or "metadata=dict(metadata" in src), (
        "helper must pass metadata via metadata= kwarg, not **metadata unpack"
    )
    # 不许出现 ``step_span(..., **metadata)`` 这种解包到 step_span 入参
    # 的反模式.
    idx = src.find("step_span(")
    if idx >= 0:
        snippet = src[idx:idx + 800]
        assert "**metadata" not in snippet, (
            f"helper unpacked metadata to step_span: {snippet}"
        )
    # 函数签名必须有显式 metadata= 参数 (而非 **metadata)
    sig = inspect.signature(_gdr_step_span_ctx)
    assert "metadata" in sig.parameters, (
        "helper signature must have explicit metadata= parameter"
    )
    assert "**metadata" not in str(sig), (
        "helper signature must NOT use **metadata (causes double-wrap)"
    )
