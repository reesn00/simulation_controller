from __future__ import annotations

import json
import uuid
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


def test_repository_no_longer_writes_datasets_or_reports(tmp_path: Path) -> None:
    """新架构下 JsonRunRepository 不再产 datasets/reports; SFT 数据由 gdr/etl 写 (C3 契约)."""
    repository = JsonRunRepository(tmp_path)
    repository.save_run(run_record("success", RunState.SUCCESS))
    repository.save_run(run_record("failed", RunState.GUIDE_EXHAUSTED))
    # 旧位置 (datasets/, reports/) 不再被创建 / 写入
    assert not (tmp_path / "datasets").exists()
    assert not (tmp_path / "reports").exists()
    # runs 仍在
    assert (tmp_path / "runs" / "success" / "run.json").exists()


def test_repository_marks_non_terminal_runs_interrupted(tmp_path: Path) -> None:
    repository = JsonRunRepository(tmp_path)
    repository.save_run(run_record("active", RunState.WAITING_EXECUTOR))
    interrupted = repository.mark_interrupted()
    assert len(interrupted) == 1
    assert repository.load_runs()[0].state is RunState.INTERRUPTED
    events = (tmp_path / "runs" / "active" / "events.jsonl").read_text(encoding="utf-8")
    assert "RUN_INTERRUPTED" in events


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


# ----------------------------------------------------------------------
# 重投清盘 + 坏 run 容错 (2026-10-03)
# 起因: T004 重投后 events.jsonl 累积成两段断链, 该 run 之后 load 必抛
# "broken event transition chain"; 而 bootstrap 每次启动都 mark_interrupted
# → load_runs 遍历全盘, 于是 98 个任务里 94 个死在 bootstrap 阶段。
# ----------------------------------------------------------------------


def _write_two_segment_chain(run_dir: Path) -> None:
    """手工造「重投未清盘」留下的两段断链 events.jsonl (复现 T004 现场)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    events = [
        ("RUN_PREPARING", RunState.PENDING, RunState.PREPARING),
        ("OPENING_REQUESTED", RunState.PREPARING, RunState.GENERATING_OPENING),
        ("OPENING_CREATED", RunState.GENERATING_OPENING, RunState.WAITING_EXECUTOR),
        ("EXECUTOR_FAILED", RunState.WAITING_EXECUTOR, RunState.EXECUTOR_ERROR),
        # ↓ 第二 attempt: 内存 state_events 被清空后从头开始, 与上一段接不上
        ("RUN_PREPARING", RunState.PENDING, RunState.PREPARING),
        ("OPENING_REQUESTED", RunState.PREPARING, RunState.GENERATING_OPENING),
    ]
    with (run_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event_type, from_state, to_state in events:
            handle.write(
                json.dumps(
                    {
                        "event_id": f"event_{uuid.uuid4().hex}",
                        "event_type": event_type,
                        "from_state": from_state.value,
                        "to_state": to_state.value,
                        "created_at": "2026-10-03T00:00:00Z",
                        "detail": {},
                    }
                )
                + "\n"
            )


def test_retry_reset_clears_append_only_records(tmp_path: Path) -> None:
    """reset_run_records 应清掉 events/validations, 保留 run.json 与血缘."""
    repository = JsonRunRepository(tmp_path)
    run = run_record("retry_me", RunState.EXECUTOR_ERROR)
    repository.save_run(run)
    _write_two_segment_chain(tmp_path / "runs" / "retry_me")
    (tmp_path / "runs" / "retry_me" / "evidence.jsonl").write_text("{}\n", encoding="utf-8")

    repository.reset_run_records("retry_me")

    run_dir = tmp_path / "runs" / "retry_me"
    assert not (run_dir / "events.jsonl").exists()
    assert not (run_dir / "validations.jsonl").exists()
    # 非链条文件与 checkpoint 本身不受影响
    assert (run_dir / "evidence.jsonl").exists()
    assert (run_dir / "run.json").exists()


def test_retry_without_reset_would_break_chain(tmp_path: Path) -> None:
    """回归: 不清盘时断链确实会让该 run 不可读 (即旧行为)."""
    repository = JsonRunRepository(tmp_path)
    repository.save_run(run_record("broken", RunState.INCONCLUSIVE, validated=False))
    _write_two_segment_chain(tmp_path / "runs" / "broken")

    with pytest.raises(RepositoryError, match="broken event transition chain"):
        repository.load_runs()


def test_load_runs_skip_broken_keeps_strict_mode_raising(tmp_path: Path) -> None:
    """skip_broken=True 跳过坏 run; 默认严格模式仍抛 (不静默改语义)."""
    repository = JsonRunRepository(tmp_path)
    repository.save_run(run_record("good", RunState.INCONCLUSIVE, validated=False))
    repository.save_run(run_record("broken", RunState.INCONCLUSIVE, validated=False))
    _write_two_segment_chain(tmp_path / "runs" / "broken")

    recovered = JsonRunRepository(tmp_path).load_runs(skip_broken=True)
    assert [item.run_id for item in recovered] == ["good"]

    with pytest.raises(RepositoryError, match="broken event transition chain"):
        repository.load_runs()


def test_mark_interrupted_survives_broken_neighbour(tmp_path: Path) -> None:
    """恢复扫描: 一个坏 run 不应让其余 run 标记不了 (bootstrap 会全盘失败)."""
    repository = JsonRunRepository(tmp_path)
    repository.save_run(run_record("good", RunState.WAITING_EXECUTOR))
    repository.save_run(run_record("broken", RunState.INCONCLUSIVE, validated=False))
    _write_two_segment_chain(tmp_path / "runs" / "broken")

    interrupted = JsonRunRepository(tmp_path).mark_interrupted()

    assert [item.run_id for item in interrupted] == ["good"]
    assert JsonRunRepository(tmp_path).load_runs(skip_broken=True)[0].state is RunState.INTERRUPTED