"""Unit tests for the TOOL_REPETITIVE deterministic post-processor.

The validator lives at the boundary between the executor's tool-call
stream and the deterministic pipeline. It must:

* trigger only when consecutive same-name + similar-input toolcalls meet
  or exceed the configured threshold;
* keep unrelated PASS / FAIL verdicts intact when the detector does not
  fire;
* survive degenerate inputs (empty, non-list, malformed blocks) without
  raising so a misbehaving archiver cannot break the run.

The tests below exercise every legal exit of ``ToolRepetitiveValidator``
plus the failure modes. They intentionally do not touch the network, the
trajectory archiver, or any pydantic model — the unit under test is the
small pure-Python helper.
"""
from __future__ import annotations

import pytest

from simulate_serve.domain.provenance import SourceRef
from simulate_serve.domain.task import AcceptanceCriterion, RemediationSpec
from simulate_serve.domain.validation import Verdict
from simulate_serve.infrastructure.qwenpaw_client import (
    _iter_events,
    _read_toolcall_blocks,
)
from simulate_serve.validation.deterministic.tool_repetitive import (
    ToolRepetitiveValidator,
)


def _criterion() -> AcceptanceCriterion:
    return AcceptanceCriterion(
        criterion_id="media.playable",
        description="at least one playable link",
        source=SourceRef(source_type="scenario", source_id="media", path="media.playable"),
        remediation=RemediationSpec(owner="executor", guidance="recheck"),
    )


def _block(name: str, query: str) -> dict:
    return {"type": "tool_call", "name": name, "input": {"query": query}}


class TestToolRepetitiveValidator:
    def test_returns_none_below_threshold(self) -> None:
        blocks = [_block("web_search", f"query {i}") for i in range(4)]
        validator = ToolRepetitiveValidator(blocks, threshold=5)
        assert validator.validate(_criterion(), "any text") is None

    def test_returns_none_for_empty_toolcalls(self) -> None:
        validator = ToolRepetitiveValidator((), threshold=5)
        assert validator.validate(_criterion(), "any text") is None

    def test_fires_when_run_meets_threshold(self) -> None:
        blocks = [_block("web_search", "same query") for _ in range(5)]
        validator = ToolRepetitiveValidator(blocks, threshold=5)
        result = validator.validate(_criterion(), "any text")
        assert result is not None
        assert result.verdict is Verdict.FAIL
        assert result.reason_code == "TOOL_REPETITIVE"
        assert result.criterion_id == "media.playable"
        assert result.retryable is True
        assert "5" in result.message

    def test_fires_above_threshold(self) -> None:
        blocks = [_block("web_search", "same query") for _ in range(7)]
        validator = ToolRepetitiveValidator(blocks, threshold=5)
        result = validator.validate(_criterion(), "any text")
        assert result is not None
        assert result.verdict is Verdict.FAIL
        assert "7" in result.message

    def test_different_tool_name_breaks_run(self) -> None:
        # web_search x3, then browser.snapshot x3, then web_search x3 — no run
        # of 5 same-name consecutive calls.
        blocks = (
            [_block("web_search", "q") for _ in range(3)]
            + [_block("browser.snapshot", "url") for _ in range(3)]
            + [_block("web_search", "q") for _ in range(3)]
        )
        validator = ToolRepetitiveValidator(blocks, threshold=5)
        assert validator.validate(_criterion(), "any text") is None

    def test_genuinely_different_inputs_break_run(self) -> None:
        # Unrelated topics: very little character overlap, so the
        # SequenceMatcher ratio falls well below the 0.9 cutoff.
        blocks = [
            _block("web_search", "Python type hints tutorial"),
            _block("web_search", "renewable energy statistics"),
            _block("web_search", "Italian pasta recipes"),
            _block("web_search", "quantum mechanics primer"),
            _block("web_search", "stock market open hours"),
        ]
        validator = ToolRepetitiveValidator(blocks, threshold=5)
        assert validator.validate(_criterion(), "any text") is None

    def test_similar_inputs_treated_as_same(self) -> None:
        # Whitespace / case / trailing-period drift is collapsed by the
        # normaliser — the 0.9 cutoff must still trip.
        blocks = [
            _block("web_search", "open source movie streaming"),
            _block("web_search", "Open  Source  Movie streaming"),
            _block("web_search", "open source movie  streaming"),
            _block("web_search", "open source movie streaming"),
            _block("web_search", "open source movie streaming."),
        ]
        validator = ToolRepetitiveValidator(blocks, threshold=5)
        result = validator.validate(_criterion(), "any text")
        assert result is not None
        assert result.reason_code == "TOOL_REPETITIVE"

    def test_threshold_clamps_below_2(self) -> None:
        blocks = [_block("web_search", "same") for _ in range(2)]
        validator = ToolRepetitiveValidator(blocks, threshold=0)
        result = validator.validate(_criterion(), "any text")
        assert result is not None
        assert result.reason_code == "TOOL_REPETITIVE"

    def test_threshold_clamps_above_20(self) -> None:
        # Threshold of 99 is clamped to 20, so 21 calls still trips.
        blocks = [_block("web_search", "same") for _ in range(21)]
        validator = ToolRepetitiveValidator(blocks, threshold=99)
        result = validator.validate(_criterion(), "any text")
        assert result is not None
        assert result.reason_code == "TOOL_REPETITIVE"

    def test_non_toolcall_blocks_are_ignored(self) -> None:
        # Mixed payload: thinking, text, tool_call. Only the tool_call blocks
        # count.
        blocks: list[dict] = [
            {"type": "thinking", "text": "hmm"},
            {"type": "text", "text": "let me try"},
            _block("web_search", "q"),
            {"type": "tool_result", "output": "result"},
            _block("web_search", "q"),
            _block("web_search", "q"),
        ]
        validator = ToolRepetitiveValidator(blocks, threshold=3)
        result = validator.validate(_criterion(), "any text")
        assert result is not None
        assert result.reason_code == "TOOL_REPETITIVE"
        assert "3" in result.message

    def test_blocks_without_name_are_skipped(self) -> None:
        # A nameless toolcall block is filtered out before the run detector
        # sees it, so the 2 remaining same-name blocks stay below the
        # threshold of 3.
        blocks: list[dict] = [
            {"type": "tool_call", "input": {"q": "v"}},  # no name -> skipped
            _block("web_search", "q"),
            _block("web_search", "q"),
        ]
        validator = ToolRepetitiveValidator(blocks, threshold=3)
        assert validator.validate(_criterion(), "any text") is None

    def test_toolcall_aliases_are_accepted(self) -> None:
        # toolcall / tool_use must be treated as tool_call, not silently
        # dropped. Schema drift across trajectory versions must not silently
        # disable the detector.
        blocks = [
            {"type": "toolcall", "name": "web_search", "input": {"q": "x"}},
            {"type": "tool_use", "name": "web_search", "input": {"q": "x"}},
            {"type": "tool_call", "name": "web_search", "input": {"q": "x"}},
            {"type": "tool_call", "name": "web_search", "input": {"q": "x"}},
            {"type": "tool_call", "name": "web_search", "input": {"q": "x"}},
        ]
        validator = ToolRepetitiveValidator(blocks, threshold=5)
        result = validator.validate(_criterion(), "any text")
        assert result is not None
        assert result.reason_code == "TOOL_REPETITIVE"

    def test_longest_run_wins_over_partial_run(self) -> None:
        # Three at the start, then five in the middle, then two at the end.
        # The detector must report 5, not 3.
        blocks = (
            [_block("web_search", "a") for _ in range(3)]
            + [_block("web_search", "b") for _ in range(5)]
            + [_block("web_search", "c") for _ in range(2)]
        )
        validator = ToolRepetitiveValidator(blocks, threshold=5)
        result = validator.validate(_criterion(), "any text")
        assert result is not None
        assert "5" in result.message

    def test_input_aliases_input_arguments_args(self) -> None:
        # Different payload keys ("input" / "arguments" / "args") must
        # normalize to the same signature so a serializer change cannot break
        # the detector.
        blocks = [
            {"type": "tool_call", "name": "web_search", "input": {"q": "x"}},
            {"type": "tool_call", "name": "web_search", "arguments": {"q": "x"}},
            {"type": "tool_call", "name": "web_search", "args": {"q": "x"}},
            {"type": "tool_call", "name": "web_search", "input": {"q": "x"}},
            {"type": "tool_call", "name": "web_search", "input": {"q": "x"}},
        ]
        validator = ToolRepetitiveValidator(blocks, threshold=5)
        result = validator.validate(_criterion(), "any text")
        assert result is not None
        assert result.reason_code == "TOOL_REPETITIVE"


class TestTrajectoryScanner:
    """``_read_toolcall_blocks`` + ``_iter_events`` are private but easy to
    break with a naive line-split. These tests pin their behavior so a
    refactor cannot regress the brace-balanced scanner.
    """

    def test_iter_events_yields_balanced_objects(self) -> None:
        text = (
            '{"event_type": "turn_start", "seq": 1}'
            '\n{"event_type": "model_response", "payload": {"content": []}}'
            '\n{"event_type": "final_reply", "payload": {"content": []}}'
        )
        events = list(_iter_events(text))
        assert [event["event_type"] for event in events] == [
            "turn_start",
            "model_response",
            "final_reply",
        ]

    def test_iter_events_handles_embedded_newlines(self) -> None:
        # Trajectory tool_result payloads can contain newlines inside JSON
        # strings. The scanner must respect string boundaries and not slice
        # mid-object.
        text = (
            '{"event_type": "tool_execution", "output": "line1\\nline2"}'
            '\n{"event_type": "model_response", "payload": {"content": ['
            '{"type": "text", "text": "hi"}'
            ']}}'
        )
        events = list(_iter_events(text))
        assert len(events) == 2
        assert events[0]["event_type"] == "tool_execution"
        assert events[0]["output"] == "line1\nline2"
        assert events[1]["event_type"] == "model_response"

    def test_read_toolcall_blocks_extracts_only_tool_call_blocks(self, tmp_path) -> None:
        trajectory = tmp_path / "session.jsonl"
        trajectory.write_text(
            '{"event_type": "model_response", "payload": {"content": ['
            '{"type": "thinking", "text": "hmm"},'
            '{"type": "tool_call", "name": "web_search", "input": {"q": "a"}},'
            '{"type": "text", "text": "answer"}'
            ']}}\n',
            encoding="utf-8",
        )
        blocks = _read_toolcall_blocks(trajectory)
        assert len(blocks) == 1
        assert blocks[0]["name"] == "web_search"
        assert blocks[0]["input"] == {"q": "a"}

    def test_read_toolcall_blocks_returns_empty_for_missing_file(self, tmp_path) -> None:
        assert _read_toolcall_blocks(tmp_path / "absent.jsonl") == ()

    def test_read_toolcall_blocks_returns_empty_for_empty_file(self, tmp_path) -> None:
        trajectory = tmp_path / "empty.jsonl"
        trajectory.write_text("", encoding="utf-8")
        assert _read_toolcall_blocks(trajectory) == ()

    def test_read_toolcall_blocks_returns_empty_for_invalid_json(self, tmp_path) -> None:
        trajectory = tmp_path / "bad.jsonl"
        trajectory.write_text("{not really json", encoding="utf-8")
        assert _read_toolcall_blocks(trajectory) == ()


class TestPipelineIntegration:
    """End-to-end: the validator must run when ``ValidationPipeline.validate``
    is called with ``toolcall_blocks`` and the scenario's threshold is met.
    """

    @pytest.mark.asyncio
    async def test_pipeline_overrides_with_tool_repetitive(self) -> None:
        from simulate_serve.domain.run import TaskRun
        from simulate_serve.domain.task import CompiledTask
        from simulate_serve.validation.pipeline import ValidationPipeline

        # Build a minimal CompiledTask-shaped object via duck-typing. The
        # pipeline only reads ``criteria`` and ``interaction_policy``.
        criterion = _criterion()
        task = CompiledTask.__new__(CompiledTask)
        object.__setattr__(task, "criteria", [criterion])
        object.__setattr__(
            task,
            "interaction_policy",
            type("IP", (), {"tool_repetitive_threshold": 5})(),
        )
        run = TaskRun.__new__(TaskRun)
        pipeline = ValidationPipeline()
        blocks = tuple(_block("web_search", "same") for _ in range(5))
        report = await pipeline.validate(
            task, run, "any visible text", toolcall_blocks=blocks
        )
        tool_rep_results = [
            item for item in report.criteria if item.reason_code == "TOOL_REPETITIVE"
        ]
        assert tool_rep_results, "TOOL_REPETITIVE not surfaced to the report"
        assert tool_rep_results[0].verdict is Verdict.FAIL

    @pytest.mark.asyncio
    async def test_pipeline_no_op_when_toolcall_blocks_empty(self) -> None:
        from simulate_serve.domain.run import TaskRun
        from simulate_serve.domain.task import CompiledTask
        from simulate_serve.validation.pipeline import ValidationPipeline

        criterion = _criterion()
        task = CompiledTask.__new__(CompiledTask)
        object.__setattr__(task, "criteria", [criterion])
        object.__setattr__(
            task,
            "interaction_policy",
            type("IP", (), {"tool_repetitive_threshold": 5})(),
        )
        run = TaskRun.__new__(TaskRun)
        pipeline = ValidationPipeline()
        report = await pipeline.validate(
            task, run, "any visible text", toolcall_blocks=()
        )
        assert all(item.reason_code != "TOOL_REPETITIVE" for item in report.criteria)
