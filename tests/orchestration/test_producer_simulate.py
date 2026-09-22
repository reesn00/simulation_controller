"""orchestration.producer_simulate 单元测试 (ST-4).

mock ``simulate_serve.bootstrap.build_application``，避免真实 QwenPaw 连接.

新接口::

    async run_one_task(task_id, *, config_path) -> TaskRun
    run_one_task_sync(task_id, *, config_path) -> TaskRun   # asyncio.run 包装
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Sequence

import pytest

from orchestration.producer_simulate import (
    run_one_task,
    run_one_task_sync,
)
from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.state_machine import RunState
from simulate_serve.domain.task import CompiledTask


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path):
    # simulate_serve.load_config 读 AppConfig; 空 yaml 可读.
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}", encoding="utf-8")
    return tmp_path, config_path


def _make_task(task_id: str) -> CompiledTask:
    from simulate_serve.domain.provenance import SourceRef, TaskProvenance
    from simulate_serve.domain.task import (
        AcceptanceCriterion,
        InteractionPolicy,
        PersonaSpec,
        ValidationPolicy,
    )
    return CompiledTask(
        task_id=task_id,
        task_type="test",
        dimension="test",
        explain=f"task {task_id}",
        task_prompt="do",
        persona=PersonaSpec(),
        criteria=(
            AcceptanceCriterion(
                criterion_id="c.text",
                description="non-empty",
                validator="format",
                parameters={"format": "text"},
                source=SourceRef(source_type="task", source_id=task_id, path="acceptance_criteria[0]"),
            ),
        ),
        interaction_policy=InteractionPolicy(),
        validation_policy=ValidationPolicy(),
        provenance=TaskProvenance(),
    )


def _make_run(task_id: str, state: RunState = RunState.SUCCESS) -> TaskRun:
    return TaskRun(
        run_id=f"run_{task_id}", task_id=task_id, task_type="test", state=state,
    )


class _FakeServices:
    def __init__(
        self,
        tasks: Sequence[CompiledTask],
        runs: Sequence[TaskRun],
    ):
        self.task_manager = type("TM", (), {"compiled_tasks": list(tasks)})()
        self.batch_runner = type(
            "BR", (),
            {"_runs": list(runs), "run": _fake_run_async},
        )()
        self._closed = False

    async def close(self) -> None:
        self._closed = True


async def _fake_run_async(self, tasks, *, limit=0, rerun_of=None, max_run_retries=None):
    """返回与传入 tasks 对应的预设 runs（按 task_id 匹配）."""
    by_id = {r.task_id: r for r in self._runs}
    return [by_id[t.task_id] for t in tasks if t.task_id in by_id]


# ---------------------------------------------------------------------------
# run_one_task (async)
# ---------------------------------------------------------------------------


def test_run_one_task_returns_task_run(env, monkeypatch) -> None:
    """run_one_task: 找 task → 跑 batch_runner → 返 runs[0]."""
    _, config_path = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1", RunState.SUCCESS)])

    async def fake_build_application(_cfg):
        return fake

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    run = asyncio.run(
        run_one_task("T1", config_path=config_path)
    )
    assert isinstance(run, TaskRun)
    assert run.task_id == "T1"
    assert run.run_id == "run_T1"
    assert run.state == RunState.SUCCESS
    # services.close() 必须被调 (释放 QwenPaw executor / ToolRegistry)
    assert fake._closed


def test_run_one_task_raises_keyerror_for_unknown_task(env, monkeypatch) -> None:
    """task_id 不在 catalog → KeyError (契约 §4.2)."""
    _, config_path = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1")])

    async def fake_build_application(_cfg):
        return fake

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    with pytest.raises(KeyError, match="T_MISSING"):
        asyncio.run(
            run_one_task("T_MISSING", config_path=config_path)
        )
    # close 仍在异常分支执行
    assert fake._closed, "services.close() 在异常分支仍要执行"


def test_run_one_task_propagates_batch_runner_exception(env, monkeypatch) -> None:
    """batch_runner.run 抛异常 → run_one_task 透传 (PipelineExecutor 兜底)."""
    _, config_path = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1")])

    async def boom(self, _tasks, *, limit=0, rerun_of=None, max_run_retries=None):
        raise RuntimeError("simulate_serve internal failure")
    fake.batch_runner = type("BR", (), {"run": boom})()

    async def fake_build_application(_cfg):
        return fake

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    with pytest.raises(RuntimeError, match="simulate_serve internal failure"):
        asyncio.run(run_one_task("T1", config_path=config_path))
    assert fake._closed


def test_run_one_task_does_not_write_sqlite(env, monkeypatch) -> None:
    """run_one_task 不写 SQLite —— 由 PipelineExecutor 调 mark_phase."""
    _, config_path = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1")])

    async def fake_build_application(_cfg):
        return fake

    # 关键断言: producer_simulate 模块不能 import SQLiteQueue
    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )
    import orchestration.producer_simulate as psm
    assert not hasattr(psm, "SQLiteQueue"), (
        "run_one_task 必须不依赖 SQLiteQueue (PipelineExecutor 负责 phase 推进)"
    )

    asyncio.run(run_one_task("T1", config_path=config_path))


def test_run_one_task_returns_failure_state(env, monkeypatch) -> None:
    """run_one_task 返 run.state == FAIL (executor 失败) → caller 据此标 dead."""
    _, config_path = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1", RunState.GUIDE_EXHAUSTED)])

    async def fake_build_application(_cfg):
        return fake

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    run = asyncio.run(run_one_task("T1", config_path=config_path))
    assert run.state == RunState.GUIDE_EXHAUSTED
    assert run.is_terminal


def test_run_one_task_no_limit_parameter(env, monkeypatch) -> None:
    """run_one_task 不接受 limit 参数 (单 task 入口, 契约 §4.4)."""
    _, config_path = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1")])

    async def fake_build_application(_cfg):
        return fake

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    # limit 参数应不存在 (用 inspect 静态验证)
    import inspect
    sig = inspect.signature(run_one_task)
    assert "limit" not in sig.parameters
    assert "queue" not in sig.parameters


# ---------------------------------------------------------------------------
# run_one_task_sync (同步包装)
# ---------------------------------------------------------------------------


def test_run_one_task_sync_returns_task_run(env, monkeypatch) -> None:
    """run_one_task_sync: 同步包装版, 供子进程入口调."""
    _, config_path = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1")])

    async def fake_build_application(_cfg):
        return fake

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    run = run_one_task_sync("T1", config_path=config_path)
    assert isinstance(run, TaskRun)
    assert run.task_id == "T1"


def test_run_one_task_sync_propagates_exception(env, monkeypatch) -> None:
    """run_one_task_sync: 异常透传."""
    _, config_path = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1")])

    async def fake_build_application(_cfg):
        return fake

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    with pytest.raises(KeyError, match="T_MISSING"):
        run_one_task_sync("T_MISSING", config_path=config_path)


# ---------------------------------------------------------------------------
# BatchRunner.run 不接受 limit (契约 §4.4 删除)
# ---------------------------------------------------------------------------


def test_batch_runner_limit_still_defaulted_in_services(env, monkeypatch) -> None:
    """BatchRunner.run 仍接受 limit (默认 0 = 不限); run_one_task 用默认 0 调用."""
    _, config_path = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1")])

    captured: dict = {}

    async def capturing_run(self, tasks, *, limit=0, rerun_of=None, max_run_retries=None):
        captured["limit"] = limit
        return [_make_run("T1")]

    fake.batch_runner = type(
        "BR", (), {"_runs": [_make_run("T1")], "run": capturing_run},
    )()

    async def fake_build_application(_cfg):
        return fake

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    asyncio.run(run_one_task("T1", config_path=config_path))
    # run_one_task 不传 limit, BatchRunner.run 拿到 limit=0 (默认 = 不限).
    assert captured["limit"] == 0