from __future__ import annotations

import json
from pathlib import Path

from simulate_serve.domain.run import ConversationTurn, TaskRun
from simulate_serve.domain.state_machine import RunState, RunStateMachine
from simulate_serve.domain.validation import CriterionResult, ValidationReport, Verdict
from simulate_serve.infrastructure.json_run_repository import JsonRunRepository
from simulate_serve.infrastructure.json_run_repository import RepositoryError
import pytest


def run_record(run_id: str, state: RunState, content: str = "answer", *, validated: bool = True) -> TaskRun:
    run = TaskRun(
        run_id=run_id,
        task_id="T1",
        task_type="x",
        state=state,
        conversation=[ConversationTurn(role="user", content="question"), ConversationTurn(role="assistant", content=content)],
    )
    if state is RunState.SUCCESS and validated:
        run.validation_rounds.append(
            ValidationReport(
                verdict=Verdict.PASS,
                criteria=(
                    CriterionResult(
                        criterion_id="required",
                        verdict=Verdict.PASS,
                        reason_code="PASSED",
                        message="done",
                    ),
                ),
            )
        )
    return run


def test_repository_exports_all_runs_but_distills_clean_success_only(tmp_path: Path) -> None:
    repository = JsonRunRepository(tmp_path)
    repository.save_run(run_record("success", RunState.SUCCESS))
    repository.save_run(run_record("failed", RunState.GUIDE_EXHAUSTED))
    repository.save_run(run_record("thought", RunState.SUCCESS, "<think>hidden</think>answer"))
    stats = repository.export(output_format="both")
    all_runs = (tmp_path / "datasets" / "all_runs.v2.jsonl").read_text(encoding="utf-8").splitlines()
    distill = (tmp_path / "datasets" / "distill_dataset.v2.jsonl").read_text(encoding="utf-8").splitlines()
    legacy = (tmp_path / "legacy" / "distill_dataset.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(all_runs) == 3
    assert len(distill) == 1
    assert len(legacy) == 1
    assert json.loads(distill[0])["run_id"] == "success"
    assert json.loads(distill[0])["task"]["task_id"] == "T1"
    assert all(json.loads(line)["internal_thoughts"] == [] for line in legacy)
    assert stats["states"]["success"] == 2
    assert stats["stats_schema_version"] == "2"
    assert stats["by_task_type"]["x"]["success"] == 2


def test_repository_marks_non_terminal_runs_interrupted(tmp_path: Path) -> None:
    repository = JsonRunRepository(tmp_path)
    repository.save_run(run_record("active", RunState.WAITING_EXECUTOR))
    interrupted = repository.mark_interrupted()
    assert len(interrupted) == 1
    assert repository.load_runs()[0].state is RunState.INTERRUPTED
    events = (tmp_path / "runs" / "active" / "events.jsonl").read_text(encoding="utf-8")
    assert "RUN_INTERRUPTED" in events


def test_success_without_validation_is_not_distilled_or_reported_as_pass(tmp_path: Path) -> None:
    repository = JsonRunRepository(tmp_path)
    repository.save_run(run_record("unvalidated", RunState.SUCCESS, validated=False))

    repository.export(output_format="v2")

    assert not (tmp_path / "datasets" / "distill_dataset.v2.jsonl").read_text(encoding="utf-8").strip()
    loaded = repository.load_runs()[0]
    assert repository._validation_summary(loaded)["verdict"] == Verdict.INCONCLUSIVE.value


def test_untagged_internal_reasoning_is_not_distilled(tmp_path: Path) -> None:
    repository = JsonRunRepository(tmp_path)
    repository.save_run(
        run_record(
            "untagged-reasoning",
            RunState.SUCCESS,
            "用户希望我先分析任务。让我先搜索资料。\n\n这是给用户的最终答案。",
        )
    )

    repository.export(output_format="v2")

    assert not (tmp_path / "datasets" / "distill_dataset.v2.jsonl").read_text(encoding="utf-8").strip()


def test_recovery_reconciles_event_appended_before_checkpoint(tmp_path: Path) -> None:
    repository = JsonRunRepository(tmp_path)
    run = TaskRun(run_id="crashed", task_id="T1", task_type="x")
    RunStateMachine.transition(run, RunState.PREPARING, "RUN_PREPARING")
    repository.save_run(run)
    RunStateMachine.transition(run, RunState.GENERATING_OPENING, "OPENING_REQUESTED")
    repository.append_event(run.run_id, run.state_events[-1])

    recovered = JsonRunRepository(tmp_path).mark_interrupted()[0]

    event_types = [
        json.loads(line)["event_type"]
        for line in (tmp_path / "runs" / "crashed" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert event_types == ["RUN_PREPARING", "OPENING_REQUESTED", "RUN_INTERRUPTED"]
    assert [item.event_type for item in recovered.state_events] == event_types
    assert recovered.state is RunState.INTERRUPTED


def test_artifacts_are_hash_deduplicated(tmp_path: Path) -> None:
    repository = JsonRunRepository(tmp_path)
    first = repository.save_artifact(b"same", ".txt")
    second = repository.save_artifact(b"same", ".txt")
    assert first == second
    assert len(list((tmp_path / "artifacts").iterdir())) == 1


def test_artifact_size_limits_fail_explicitly(tmp_path: Path) -> None:
    repository = JsonRunRepository(tmp_path, max_artifact_bytes=3, max_total_artifact_bytes=4)
    with pytest.raises(RepositoryError):
        repository.save_artifact(b"four")
    repository.save_artifact(b"abc")
    with pytest.raises(RepositoryError):
        repository.save_artifact(b"de")
