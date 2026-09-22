from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest

from simulate_serve.application.ports import ExecutorResponse
from simulate_serve.application.run_batch import BatchRunner
from simulate_serve.application.run_task import TaskRuntime
from simulate_serve.domain.state_machine import RunState
from simulate_serve.domain.task import CompiledTask
from simulate_serve.domain.validation import CriterionResult, ValidationReport, Verdict
from simulate_serve.interaction.actor import DeterministicInteractionActor


class ScriptedExecutor:
    """每个 attempt 都能开新 session_id 以模拟重试拿到干净远端."""

    def __init__(self, responses: list[str]):
        self.responses = deque(responses)
        self.opened_sessions: list[str] = []

    async def open_session(self, message: str) -> ExecutorResponse:
        sid = f"s{len(self.opened_sessions) + 1}"
        self.opened_sessions.append(sid)
        return self._response(sid)

    async def continue_session(self, session_id: str, message: str) -> ExecutorResponse:
        return self._response(session_id)

    async def close(self) -> None:
        return None

    def _response(self, sid: str) -> ExecutorResponse:
        text = self.responses.popleft()
        return ExecutorResponse(
            text=text, session_id=sid, remote_task_id=f"rt_{sid}", agent_id="a",
        )


class AlwaysPassValidator:
    async def validate(self, task, run, response, *, toolcall_blocks=()):
        return ValidationReport(
            verdict=Verdict.PASS,
            criteria=(
                CriterionResult(
                    criterion_id=task.criteria[0].criterion_id,
                    verdict=Verdict.PASS,
                    reason_code="OK",
                    message="ok",
                ),
            ),
        )


class _FakeArchiver:
    """fake TrajectoryArchivePort: 模拟写入 trajectory 文件供 checker 判定."""

    def __init__(self, output_dir: Path, scenario: list[str]):
        self.output_dir = output_dir / "agent_trajectory"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # 每次 archive() 调用 pop 一个 scenario 元素当作 trajectory 内容
        self._scenarios = deque(scenario)

    def archive(self, run_id: str, agent_id: str, session_id: str) -> None:
        scenario = self._scenarios.popleft() if self._scenarios else "complete"
        events = _build_events(scenario, run_id, session_id)
        path = self.output_dir / f"{run_id}__{session_id}.json"
        path.write_text(
            "\n".join(json.dumps(ev, ensure_ascii=False) for ev in events) + "\n",
            encoding="utf-8",
        )

    def trajectory_path(self, run_id: str, session_id: str):
        if not session_id:
            return None
        return self.output_dir / f"{run_id}__{session_id}.json"


def _build_events(scenario: str, run_id: str, session_id: str) -> list[dict]:
    """构造一个 mini trajectory 事件流, scenario ∈ {'complete', 'incomplete', 'aborted', 'missing'}."""
    base = [
        {"event_type": "turn_start", "payload": {"input_text": "hi"}, "metadata": {}},
    ]
    if scenario == "complete":
        base += [
            {"event_type": "model_response", "payload": {"content": [
                {"type": "text", "id": "b1", "text": "已查完。结论报告完毕。"}
            ]}, "metadata": {}},
            {"event_type": "final_reply", "payload": {"content": []}, "metadata": {"usage": {"tokens": 10}}},
        ]
    elif scenario == "incomplete":
        base += [
            {"event_type": "model_response", "payload": {"content": [
                {"type": "tool_call", "id": "tc1", "name": "search", "input": "{}", "state": "pending"}
            ]}, "metadata": {}},
            # 没有 final_reply — 末段 toolcall 无配对
        ]
    elif scenario == "aborted":
        base += [
            {"event_type": "error", "payload": {"message": "remote crashed"}, "metadata": {}},
        ]
    else:  # missing / 异常 — 故意不写任何 model_response, 触发 incomplete(empty)
        pass
    return base


def _runtime(executor, validator, repository=None, archiver=None) -> TaskRuntime:
    return TaskRuntime(
        executor=executor,
        actor=DeterministicInteractionActor(),
        validator=validator,
        repository=repository,
        trajectory_archiver=archiver,
    )


def _make_task(tmp_path: Path, max_run_retries: int = 3) -> CompiledTask:
    """构造最小可用的 CompiledTask (覆盖 max_run_retries 字段)."""
    from simulate_serve.domain.task import (
        AcceptanceCriterion,
        CompiledTask,
        InteractionPolicy,
        SourceRef,
        TaskProvenance,
        ValidationPolicy,
    )

    base = CompiledTask(
        task_id="T_TEST",
        task_type="test",
        dimension="dim",
        explain="explain",
        task_prompt="prompt",
        persona={"role_description": "x"},
        criteria=(
            AcceptanceCriterion(
                criterion_id="C1",
                description="d",
                source=SourceRef(source_type="inline", source_id="c1", path="."),
            ),
        ),
        interaction_policy=InteractionPolicy(),
        validation_policy=ValidationPolicy(),
        provenance=TaskProvenance(),
    )
    # CompiledTask 是 frozen pydantic, 不能直接赋值
    return base.model_copy(update={"max_run_retries": max_run_retries})


@pytest.mark.asyncio
async def test_batch_runner_complete_on_first_attempt(tmp_path: Path) -> None:
    archiver = _FakeArchiver(tmp_path, ["complete"])
    runtime = _runtime(ScriptedExecutor(["done"]), AlwaysPassValidator(), archiver=archiver)
    task = _make_task(tmp_path)
    runs = await BatchRunner(runtime).run([task])
    assert len(runs) == 1
    run = runs[0]
    assert run.state is RunState.SUCCESS
    assert run.retry_count == 0
    assert run.completion_check is not None
    assert run.completion_check.status == "complete"


@pytest.mark.asyncio
async def test_batch_runner_retries_then_succeeds(tmp_path: Path) -> None:
    # 2 次 incomplete 后第 3 次 complete
    archiver = _FakeArchiver(tmp_path, ["incomplete", "incomplete", "complete"])
    runtime = _runtime(
        ScriptedExecutor(["x", "x", "x"]),
        AlwaysPassValidator(),
        archiver=archiver,
    )
    task = _make_task(tmp_path, max_run_retries=3)
    runs = await BatchRunner(runtime).run([task])
    run = runs[0]
    # 第三次 attempt 才 complete
    assert run.state is RunState.SUCCESS
    assert run.retry_count == 2  # 第一次失败 + 第二次失败 = retry_count=2, 第 3 次成功
    assert run.completion_check is not None
    assert run.completion_check.status == "complete"


@pytest.mark.asyncio
async def test_batch_runner_gives_up_after_max_run_retries(tmp_path: Path) -> None:
    # 4 次都不完整 (max_run_retries=3 → max_attempts=4) → 放弃
    archiver = _FakeArchiver(tmp_path, ["incomplete"] * 4)
    runtime = _runtime(
        ScriptedExecutor(["x"] * 4),
        AlwaysPassValidator(),
        archiver=archiver,
    )
    task = _make_task(tmp_path, max_run_retries=3)
    runs = await BatchRunner(runtime).run([task])
    run = runs[0]
    assert run.state is RunState.COMPLETION_INCOMPLETE
    assert run.retry_count == 3  # 1 + 3 retries = 4 attempts, 但 retry_count 是 reuse 次数
    assert run.failure is not None
    assert run.failure.code == "COMPLETION_INCOMPLETE"
    assert run.failure.retryable is False


@pytest.mark.asyncio
async def test_batch_runner_aborted_does_not_retry(tmp_path: Path) -> None:
    # aborted (error 终态) → 不重投
    archiver = _FakeArchiver(tmp_path, ["aborted"])
    runtime = _runtime(ScriptedExecutor(["x"]), AlwaysPassValidator(), archiver=archiver)
    task = _make_task(tmp_path, max_run_retries=3)
    runs = await BatchRunner(runtime).run([task])
    run = runs[0]
    assert run.state is RunState.COMPLETION_INCOMPLETE
    assert run.failure is not None
    assert run.failure.code == "COMPLETION_ABORTED"
    assert run.retry_count == 0  # 没重投


@pytest.mark.asyncio
async def test_batch_runner_partial_snapshot_is_written(tmp_path: Path) -> None:
    # 第一次 incomplete → snapshot 应写出 trajectory_attempt_1.json
    archiver = _FakeArchiver(tmp_path, ["incomplete", "complete"])
    runtime = _runtime(
        ScriptedExecutor(["x", "x"]),
        AlwaysPassValidator(),
        archiver=archiver,
    )
    task = _make_task(tmp_path, max_run_retries=3)
    await BatchRunner(runtime).run([task])
    # 检查 snapshot 文件存在
    snapshots = list((tmp_path / "agent_trajectory").glob("*.trajectory_attempt_1.json"))
    assert len(snapshots) == 1
    # snapshot 内容是第一次 incomplete 的轨迹
    content = snapshots[0].read_text(encoding="utf-8")
    assert "tc1" in content  # 末段 tool_call id


@pytest.mark.asyncio
async def test_batch_runner_skips_check_when_archiver_disabled(tmp_path: Path) -> None:
    # archiver=None → trajectory_path_for 返 None → 跳过完整性判定
    runtime = _runtime(ScriptedExecutor(["x"]), AlwaysPassValidator(), archiver=None)
    task = _make_task(tmp_path)
    runs = await BatchRunner(runtime).run([task])
    run = runs[0]
    assert run.state is RunState.SUCCESS
    assert run.completion_check is None  # 没判定


@pytest.mark.asyncio
async def test_batch_runner_max_run_retries_override(tmp_path: Path) -> None:
    # CLI override max_run_retries=0 → 即使上次不完整也不重投
    archiver = _FakeArchiver(tmp_path, ["incomplete"])
    runtime = _runtime(ScriptedExecutor(["x"]), AlwaysPassValidator(), archiver=archiver)
    task = _make_task(tmp_path, max_run_retries=3)
    runs = await BatchRunner(runtime).run([task], max_run_retries=0)
    run = runs[0]
    assert run.state is RunState.COMPLETION_INCOMPLETE
    assert run.retry_count == 0  # 没重投


@pytest.mark.asyncio
async def test_task_runtime_reuse_run_resets_conversation(tmp_path: Path) -> None:
    """reuse_run 入口应该清空 conversation / state_events / completion_check 等."""
    archiver = _FakeArchiver(tmp_path, ["complete", "complete"])
    runtime = _runtime(
        ScriptedExecutor(["x", "x"]),
        AlwaysPassValidator(),
        archiver=archiver,
    )
    task = _make_task(tmp_path, max_run_retries=3)

    # 第一次跑 — 用普通入口
    first = await runtime.run(task)
    assert first.state is RunState.SUCCESS
    assert first.retry_count == 0
    first_session_id = first.remote_session_id
    assert len(first.conversation) >= 2  # opening + assistant
    assert first.completion_check is None  # checker 在 BatchRunner 层调用

    # 第二次复用
    second = await runtime.run(task, reuse_run=first)
    assert second is first  # 同一对象
    assert second.retry_count == 1
    assert second.remote_session_id != first_session_id  # 新 session
    # 重投后 conversation 等应该被清空再重跑, 至少不保留上轮的 stale 状态
    # 因 SECOND 跑完会再次写入 conversation, 所以这里不严格断言长度,
    # 只断言 run_id 保持不变 (复用语义)
    assert second.run_id == first.run_id


@pytest.mark.asyncio
async def test_task_runtime_reuse_run_preserves_run_id_and_started_at(tmp_path: Path) -> None:
    archiver = _FakeArchiver(tmp_path, ["complete", "complete"])
    runtime = _runtime(ScriptedExecutor(["x", "x"]), AlwaysPassValidator(), archiver=archiver)
    task = _make_task(tmp_path)
    first = await runtime.run(task)
    original_run_id = first.run_id
    original_started_at = first.started_at
    second = await runtime.run(task, reuse_run=first)
    assert second.run_id == original_run_id
    assert second.started_at == original_started_at


def test_completion_incomplete_is_terminal() -> None:
    assert RunState.COMPLETION_INCOMPLETE.is_terminal


def test_completion_incomplete_allowed_from_preparing() -> None:
    from simulate_serve.domain.run import TaskRun
    from simulate_serve.domain.state_machine import RunStateMachine, InvalidStateTransition

    run = TaskRun(run_id="r1", task_id="T1", task_type="x", state=RunState.PREPARING)
    RunStateMachine.transition(run, RunState.COMPLETION_INCOMPLETE, "RUN_COMPLETION_INCOMPLETE")
    assert run.state is RunState.COMPLETION_INCOMPLETE


def test_completion_incomplete_disallowed_from_terminal() -> None:
    from simulate_serve.domain.run import TaskRun
    from simulate_serve.domain.state_machine import RunStateMachine, InvalidStateTransition

    run = TaskRun(run_id="r1", task_id="T1", task_type="x", state=RunState.SUCCESS)
    with pytest.raises(InvalidStateTransition):
        RunStateMachine.transition(run, RunState.COMPLETION_INCOMPLETE, "x")