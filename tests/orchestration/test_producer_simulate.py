"""orchestration.producer_simulate 单元测试.

mock ``simulate_serve.bootstrap.build_application``，避免真实 QwenPaw 连接.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest

from orchestration.producer_simulate import run_batch
from orchestration.queue import SQLiteQueue
from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.state_machine import RunState
from simulate_serve.domain.task import CompiledTask


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path: Path):
    queue = SQLiteQueue(tmp_path / "q.db")
    config_path = tmp_path / "config.yaml"
    # simulate_serve 的 load_config 不依赖 yaml 字段（默认即可），空文件可读
    config_path.write_text("{}", encoding="utf-8")
    return queue, config_path, tmp_path


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
    return TaskRun(run_id=f"run_{task_id}", task_id=task_id, task_type="test", state=state)


class _FakeServices:
    def __init__(self, tasks: Sequence[CompiledTask], runs: Sequence[TaskRun]):
        self.task_manager = type("TM", (), {"compiled_tasks": list(tasks)})()
        self.batch_runner = type(
            "BR", (),
            {"_runs": list(runs), "_tasks": list(tasks),
             "run": _fake_run_async},
        )()
        self._closed = False

    async def close(self) -> None:
        self._closed = True


async def _fake_run_async(self, tasks, *, limit=0):
    """返回与传入 tasks 对应的预设 runs（按 task_id 匹配）."""
    by_id = {r.task_id: r for r in self._runs}
    return [by_id[t.task_id] for t in tasks if t.task_id in by_id]


# ---------------------------------------------------------------------------
# run_batch
# ---------------------------------------------------------------------------

def test_run_batch_writes_batches_row(env, monkeypatch) -> None:
    queue, config_path, _ = env
    t1 = _make_task("T1")
    t2 = _make_task("T2")
    r1 = _make_run("T1", RunState.SUCCESS)
    r2 = _make_run("T2", RunState.GUIDE_EXHAUSTED)
    fake = _FakeServices([t1, t2], [r1, r2])

    async def fake_build_application(_cfg):
        return fake

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    batch_id, runs = run_batch(
        config_path=config_path, task_ids=["T1", "T2"], limit=2, queue=queue,
    )
    assert batch_id > 0
    assert len(runs) == 2
    assert {r.task_id for r in runs} == {"T1", "T2"}
    assert fake._closed, "services.close() 必须被调"

    # SQLite batches 行验证
    with queue._conn() as conn:
        row = conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,),
        ).fetchone()
    assert row is not None
    task_ids_csv = row["task_ids"]
    assert set(task_ids_csv.split(",")) == {"T1", "T2"}
    assert row["simulate_started_at"] is not None
    assert row["simulate_done_at"] is not None
    assert row["simulate_started_at"] <= row["simulate_done_at"]


def test_run_batch_limit_truncates(env, monkeypatch) -> None:
    """limit < len(task_ids) 时 batch_runner.run 的 limit 参数生效."""
    queue, config_path, _ = env
    tasks = [_make_task(f"T{i}") for i in range(5)]
    runs = [_make_run(f"T{i}", RunState.SUCCESS) for i in range(5)]
    fake = _FakeServices(tasks, runs)
    captured: dict = {}

    async def fake_build_application(_cfg):
        return fake

    async def capturing_run(self, tasks_arg, *, limit=0):
        captured["limit"] = limit
        captured["len"] = len(tasks_arg)
        # 模仿真 BatchRunner.run 的切片行为
        selected = list(tasks_arg[:limit] if limit > 0 else tasks_arg)
        by_id = {r.task_id: r for r in self._runs}
        return [by_id[t.task_id] for t in selected if t.task_id in by_id]

    fake.batch_runner = type(
        "BR", (),
        {"_runs": list(runs), "run": capturing_run},
    )()

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    batch_id, runs = run_batch(
        config_path=config_path,
        task_ids=[f"T{i}" for i in range(5)], limit=3, queue=queue,
    )
    assert captured["limit"] == 3
    assert len(runs) == 3


def test_run_batch_unknown_task_id_raises(env, monkeypatch) -> None:
    queue, config_path, _ = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1")])

    async def fake_build_application(_cfg):
        return fake

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    with pytest.raises(KeyError, match="T_MISSING"):
        run_batch(
            config_path=config_path,
            task_ids=["T1", "T_MISSING"], limit=2, queue=queue,
        )


def test_run_batch_services_close_called_even_on_error(env, monkeypatch) -> None:
    queue, config_path, _ = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1")])

    async def fake_build_application(_cfg):
        return fake

    async def boom(self, _tasks, *, limit=0):
        raise RuntimeError("simulate_serve internal failure")

    fake.batch_runner = type("BR", (), {"run": boom})()

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    with pytest.raises(RuntimeError, match="simulate_serve internal failure"):
        run_batch(
            config_path=config_path, task_ids=["T1"], limit=1, queue=queue,
        )
    assert fake._closed, "services.close() 在异常分支仍要执行"


def test_run_batch_each_call_gets_new_batch_id(env, monkeypatch) -> None:
    queue, config_path, _ = env
    t1 = _make_task("T1")
    fake = _FakeServices([t1], [_make_run("T1")])

    async def fake_build_application(_cfg):
        return fake

    monkeypatch.setattr(
        "orchestration.producer_simulate.build_application", fake_build_application,
    )

    bid1, _ = run_batch(
        config_path=config_path, task_ids=["T1"], limit=1, queue=queue,
    )
    bid2, _ = run_batch(
        config_path=config_path, task_ids=["T1"], limit=1, queue=queue,
    )
    assert bid1 != bid2
    assert bid2 == bid1 + 1
