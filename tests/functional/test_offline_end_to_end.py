from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest

from simulate_serve.application.ports import ExecutorResponse
from simulate_serve.application.run_batch import BatchRunner
from simulate_serve.application.run_task import TaskRuntime
from simulate_serve.domain.state_machine import RunState
from simulate_serve.infrastructure.json_run_repository import JsonRunRepository
from simulate_serve.interaction.actor import DeterministicInteractionActor
from simulate_serve.task_manager import TaskManager
from simulate_serve.validation.pipeline import ValidationPipeline
from simulate_serve.validation.semantic_judge import ScriptedSemanticJudge
from simulate_serve.domain.validation import CriterionResult, Verdict


class ScriptedExecutor:
    def __init__(self, replies: list[str]):
        self.replies = deque(replies)
        self.count = 0
        self.messages: list[str] = []

    async def open_session(self, message: str) -> ExecutorResponse:
        self.messages.append(message)
        return self._next()

    async def continue_session(self, session_id: str, message: str) -> ExecutorResponse:
        self.messages.append(message)
        return self._next()

    def _next(self) -> ExecutorResponse:
        self.count += 1
        return ExecutorResponse(
            text=self.replies.popleft(),
            session_id="offline-session",
            remote_task_id=f"remote-{self.count}",
            agent_id="offline-agent",
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.functional
async def test_catalog_runtime_validation_repository_export(project_root: Path, tmp_path: Path) -> None:
    manager = TaskManager("tasks.yaml", "scenarios.yaml", config_dir=project_root / "simulate_serve" / "config")
    task = next(item for item in manager.compiled_tasks if item.task_id == "T034")
    reply = "《功夫2》不存在，我不会编造播放链接；可以改看《功夫》的官方合法渠道。"
    repository = JsonRunRepository(tmp_path)
    runtime = TaskRuntime(
        ScriptedExecutor([reply]),
        DeterministicInteractionActor(),
        ValidationPipeline(judge=ScriptedSemanticJudge()),
        repository,
    )
    runs = await BatchRunner(runtime).run([task])
    assert runs[0].state is RunState.SUCCESS
    stats = repository.export(output_format="v2")
    assert stats["states"] == {"success": 1}
    assert (tmp_path / "datasets" / "distill_dataset.v2.jsonl").read_text(encoding="utf-8").strip()


@pytest.mark.asyncio
@pytest.mark.functional
async def test_file_operation_self_report_is_inconclusive_without_evidence_provider(project_root: Path) -> None:
    manager = TaskManager("tasks.yaml", "scenarios.yaml", config_dir=project_root / "simulate_serve" / "config")
    task = next(item for item in manager.compiled_tasks if item.task_id == "F001")
    reply = "已把桌面截图按日期整理完成，共处理 8 个文件。"
    runtime = TaskRuntime(
        ScriptedExecutor([reply]),
        DeterministicInteractionActor(),
        ValidationPipeline(judge=ScriptedSemanticJudge()),
    )

    run = await runtime.run(task)

    assert run.state is RunState.INCONCLUSIVE
    assert any(item.reason_code == "TOOL_UNAVAILABLE" for item in run.validation_rounds[-1].criteria)


@pytest.mark.asyncio
@pytest.mark.functional
async def test_v2_fixture_is_private_and_semantic_gap_drives_offline_followup(project_root: Path) -> None:
    class SequentialJudge:
        def __init__(self) -> None:
            self.calls = 0

        async def judge(self, task, response_text, criteria):
            self.calls += 1
            return tuple(
                CriterionResult(
                    criterion_id=item.criterion_id,
                    verdict=Verdict.FAIL if self.calls == 1 and item.criterion_id == "task.t052.outcome" else Verdict.PASS,
                    reason_code="RECOVERY_MISSING" if self.calls == 1 else "PASSED",
                    message="没有说明恢复策略" if self.calls == 1 else "已满足",
                    retryable=False,
                )
                for item in criteria
            )

    manager = TaskManager("tasks.yaml", "scenarios.yaml", config_dir=project_root / "simulate_serve" / "config")
    task = next(item for item in manager.compiled_tasks if item.task_id == "T052")
    executor = ScriptedExecutor(["没有结果。", "我换了关键词和来源，仍没有结果，当前被工具阻塞。"])
    run = await TaskRuntime(
        executor,
        DeterministicInteractionActor(),
        ValidationPipeline(judge=SequentialJudge()),
    ).run(task)

    assert run.state is RunState.SUCCESS
    assert run.guide_rounds == 1
    assert len(executor.messages) == 2
    assert "搜索工具首次返回空结果" not in executor.messages[0]
    assert "first_result" not in executor.messages[0]
    assert "空结果后调整关键词或来源" in executor.messages[1]
