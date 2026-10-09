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
async def test_catalog_runtime_validation_persists_run_only(project_root: Path, tmp_path: Path) -> None:
    """新架构下 simulation server 只写 ``runs/``; SFT 数据由 gdr/etl 末端产 (C3)."""
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
    # 旧 export 位置 datasets/ 不再被写
    assert not (tmp_path / "datasets").exists()
    assert not (tmp_path / "reports").exists()
    # runs/ 审计数据在
    run_id = runs[0].run_id
    assert (tmp_path / "runs" / run_id / "run.json").exists()
    assert (tmp_path / "runs" / run_id / "events.jsonl").exists()


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
                    # A scenario-vocabulary code: the follow-up must speak the
                    # scenario's variant phrasing, not the criterion text.
                    reason_code="EMPTY_RESULTS" if self.calls == 1 else "PASSED",
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
    # P1-1: the follow-up speaks the scenario variant pool (EMPTY_RESULTS),
    # never the criterion remediation text or the judge's technical message.
    assert (
        "换关键词或换来源" in executor.messages[1] or "换个查法" in executor.messages[1]
    )
    assert "没有说明恢复策略" not in executor.messages[1]


@pytest.mark.asyncio
@pytest.mark.functional
async def test_advisory_excluded_platforms_does_not_dead_run(project_root: Path) -> None:
    """excluded_platforms_severity=advisory 时, 即使 Agent 4 轮都给出含
    排除平台的回复, run 不进 dead (T001 历史 guide_exhausted 回归验证)。

    与 strict 行为对比: 派生 ``derived.<task>.excluded-platforms`` 硬 FAIL 准则,
    命中 SOURCE_EXCLUDED -> retryable gap -> 4 轮耗尽 -> GUIDE_EXHAUSTED。
    advisory 行为: 不派生准则 -> 该点不阻塞 verdict。
    """

    class PassAllJudge:
        """所有非语义 validator PASS, semantic 准则 PASS, 模拟 fixture
        完整自检路径 (避开 DEFERRED_AFTER_HARD_FAIL 的环境缺失分支)."""

        async def judge(self, task, response_text, criteria):
            return tuple(
                CriterionResult(
                    criterion_id=item.criterion_id,
                    verdict=Verdict.PASS,
                    reason_code="PASSED",
                    message="ok",
                    retryable=False,
                )
                for item in criteria
            )

    manager = TaskManager("tasks.yaml", "scenarios.yaml", config_dir=project_root / "simulate_serve" / "config")
    task = next(item for item in manager.compiled_tasks if item.task_id == "T001")
    # 关键断言: advisory 模式不派生硬 FAIL criterion
    assert task.excluded_platforms
    assert not any(
        c.criterion_id.endswith(".excluded-platforms") for c in task.criteria
    )
    # Agent 4 轮都返回含腾讯/爱奇艺的回复 — advisory 模式不该死信
    # (每次回复用不同编号避免 RESPONSE_LOOP_DETECTED guard 提前终止)
    replies = [
        "1. 腾讯视频 https://v.qq.com/a 1-80集\n"
        "2. 爱奇艺 https://www.iqiyi.com/b 1-80集\n"
        "3. 优酷 https://youku.com/c 1-80集\n"
        "4. 哔哩哔哩 https://www.bilibili.com/d 1-80集\n"
        "5. 央视网 https://tv.cctv.com/e 1-80集",
        "5 个链接如下:\n"
        "- 腾讯视频 https://v.qq.com/1 1-80集\n"
        "- 爱奇艺 https://www.iqiyi.com/2 1-80集\n"
        "- 优酷 https://youku.com/3 1-80集\n"
        "- 哔哩哔哩 https://www.bilibili.com/4 1-80集\n"
        "- 央视网 https://tv.cctv.com/5 1-80集",
        "1) 腾讯视频 https://v.qq.com/6 1-80集\n"
        "2) 爱奇艺 https://www.iqiyi.com/7 1-80集\n"
        "3) 优酷 https://youku.com/8 1-80集\n"
        "4) 哔哩哔哩 https://www.bilibili.com/9 1-80集\n"
        "5) 央视网 https://tv.cctv.com/10 1-80集",
        "整理后的链接:\n"
        "* 腾讯视频 https://v.qq.com/11 1-80集\n"
        "* 爱奇艺 https://www.iqiyi.com/12 1-80集\n"
        "* 优酷 https://youku.com/13 1-80集\n"
        "* 哔哩哔哩 https://www.bilibili.com/14 1-80集\n"
        "* 央视网 https://tv.cctv.com/15 1-80集",
    ]
    executor = ScriptedExecutor(replies)
    run = await TaskRuntime(
        executor,
        DeterministicInteractionActor(),
        ValidationPipeline(judge=PassAllJudge()),
    ).run(task)
    # 核心断言: 不进 GUIDE_EXHAUSTED / dead (历史 T001 死信回归验证)。
    # INCONCLUSIVE 在本地无 browser 工具时是合法终点, 不算 dead。
    assert run.state is not RunState.GUIDE_EXHAUSTED
    assert run.state is not RunState.EXECUTOR_ERROR
    assert run.state in (RunState.SUCCESS, RunState.INCONCLUSIVE)
    # 同时确认 excluded_platforms 不在 missing_items (即未阻塞失败)
    assert not any(
        "被排除的平台" in item or "SOURCE_EXCLUDED" in item
        for item in run.failure.message.split("; ") if run.failure
    )
