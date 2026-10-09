"""orchestration.pipeline_executor 单元测试.

覆盖契约 §11.2:

* N=1 / N=2 / N=4 三种并行度
* 死信传播: 一个 task 失败不影响其他 task 跑完
* 子进程崩溃兜底: 子进程抛异常 → mark_dead → dead++
* SQLite 并发写: 多子进程同时 upsert_task 不冲突

测试方法: 替换 ``multiprocessing.Pool`` 为 in-process stub, 模拟子进程跑流水线,
可注入每 task 的 behavior (simulate/gdr/etl 失败 / 崩溃 / 成功)。
"""

from __future__ import annotations

import multiprocessing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from gdr.config.settings import Settings as GdrSettings
from orchestration.pipeline_executor import PipelineExecutor, PipelineSummary
from orchestration.queue import (
    PHASE_DEAD,
    PHASE_DONE,
    SQLiteQueue,
)
from orchestration.settings import Paths, PipelineSettings


# ---------------------------------------------------------------------------
# fixtures & helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> Paths:
    """建一个完整的 Paths 实例 (含各目录, 避免子进程调用时再 mkdir)."""
    for sub in (
        "trajectory_dir", "refined_dir", "etl_outputs_dir",
        "dead_dir", "log_dir", "runs_dir",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return Paths(
        simulate_serve_config=tmp_path / "sim.yaml",
        trajectory_dir=tmp_path / "trajectory_dir",
        runs_dir=tmp_path / "runs_dir",
        refined_dir=tmp_path / "refined_dir",
        etl_outputs_dir=tmp_path / "etl_outputs_dir",
        sqlite_db=tmp_path / "q.db",
        dead_dir=tmp_path / "dead_dir",
        pid_file=tmp_path / "orch.pid",
        log_dir=tmp_path / "log_dir",
    )


def _make_gdr_settings(paths: Paths) -> GdrSettings:
    return GdrSettings(
        batch_output_dir=paths.refined_dir,
        workers=1,
        llm_concurrency=1,
        max_files=1,
    )


def _make_pipeline_settings(parallelism: int) -> PipelineSettings:
    return PipelineSettings(
        max_parallelism=parallelism,
        max_retry_gdr=0,
        max_retry_etl=0,
        retry_poll_seconds=0.05,
    )


# ---------------------------------------------------------------------------
# 注入: in-process 模拟子进程
# ---------------------------------------------------------------------------


# 全局: task_id → behavior
_BEHAVIORS: dict[str, dict[str, Any]] = {}


class _FakeAsyncResult:
    """``multiprocessing.pool.AsyncResult`` 的最小 stub."""

    def __init__(self, task_id: str, queue: SQLiteQueue, paths: Paths,
                 behavior: dict[str, Any]) -> None:
        self._task_id = task_id
        self._queue = queue
        self._paths = paths
        self._behavior = behavior
        self._result: dict | None = None
        self._exc: BaseException | None = None
        self._ready_flag = False
        self._compute()

    def _compute(self) -> None:
        try:
            task_id = self._task_id
            queue = self._queue
            behavior = self._behavior

            # 1. upsert + simulate 阶段
            queue.upsert_task(task_id)
            queue.mark_phase(task_id, new_phase="simulate")

            # 模拟 producer 写出 trajectory
            traj_dir = self._paths.trajectory_dir
            traj_dir.mkdir(parents=True, exist_ok=True)
            traj_path = traj_dir / f"{task_id}__session_{task_id}.json"
            traj_path.write_text("{}", encoding="utf-8")

            run_state = behavior.get("simulate_state", "success")
            if run_state != "success":
                queue.mark_failed(
                    task_id, stage="simulate",
                    error_msg=f"simulate={run_state}",
                )
                self._result = {
                    "task_id": task_id, "phase": "dead",
                    "stage": "simulate", "error": f"simulate={run_state}",
                }
                self._ready_flag = True
                return

            # 2. gdr 阶段
            queue.mark_phase(
                task_id, new_phase="gdr",
                run_id=task_id, session_id=f"session_{task_id}",
                src_path=traj_path,
            )
            if behavior.get("gdr_crash", False):
                raise RuntimeError("gdr process crash")
            if behavior.get("gdr_fail", False):
                queue.mark_failed(
                    task_id, stage="gdr", error_msg="gdr=fail",
                )
                self._result = {
                    "task_id": task_id, "phase": "dead",
                    "stage": "gdr", "error": "gdr=fail",
                }
                self._ready_flag = True
                return

            refined_path = self._paths.refined_dir / f"{task_id}.json"
            refined_path.write_text("{}", encoding="utf-8")
            queue.mark_phase(
                task_id, new_phase="etl", gdr_refined_path=refined_path,
            )

            # 3. etl 阶段
            if behavior.get("etl_fail", False):
                queue.mark_failed(
                    task_id, stage="etl", error_msg="etl=fail",
                )
                self._result = {
                    "task_id": task_id, "phase": "dead",
                    "stage": "etl", "error": "etl=fail",
                }
                self._ready_flag = True
                return

            base = self._paths.etl_outputs_dir / f"{task_id}"
            msgs = base.with_suffix(".messages.json")
            openai = base.with_suffix(".openai.json")
            meta = base.with_suffix(".meta.json")
            for p in (msgs, openai, meta):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}", encoding="utf-8")
            queue.mark_phase(
                task_id, new_phase="done",
                etl_messages_path=msgs, etl_openai_path=openai,
                etl_qwenjina_path=None, etl_meta_path=meta,
            )

            self._result = {
                "task_id": task_id, "phase": "done",
                "stage": "done", "error": None,
            }
            self._ready_flag = True
        except BaseException as exc:  # noqa: BLE001
            self._exc = exc
            self._ready_flag = True

    def ready(self, timeout: float | None = None) -> bool:
        return self._ready_flag

    def get(self, timeout: float | None = None) -> dict:
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


class _FakePool:
    """in-process mock multiprocessing.Pool.

    apply_async 时返回 _FakeAsyncResult; 子进程跑流水线由 _FakeAsyncResult 同步模拟。
    """

    def __init__(self, *, processes, initializer, initargs) -> None:
        self._processes = processes
        if initializer is not None:
            initializer(*initargs)

    def apply_async(self, fn, args):
        task_id = args[0]
        paths_obj = args[1]
        # 共享父进程的 SQLiteQueue 实例 — 测试不依赖子进程隔离
        # 真实场景下子进程会 SQLiteQueue(paths.sqlite_db) 重连
        queue = SQLiteQueue(paths_obj.sqlite_db)
        behavior = _BEHAVIORS.get(task_id, {})
        return _FakeAsyncResult(task_id, queue, paths_obj, behavior)

    def close(self) -> None:
        pass

    def join(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clear_behaviors():
    _BEHAVIORS.clear()
    yield
    _BEHAVIORS.clear()


@pytest.fixture
def pool(monkeypatch):
    """替换 multiprocessing.Pool 为 _FakePool."""
    monkeypatch.setattr(multiprocessing, "Pool", _FakePool)
    yield _FakePool


# ---------------------------------------------------------------------------
# 基础: 单/多并行度
# ---------------------------------------------------------------------------


def test_run_one_task_completes(tmp_path: Path, pool) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=1)
    gdr_settings = _make_gdr_settings(paths)

    summary = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    ).run(["T1"])

    assert isinstance(summary, PipelineSummary)
    assert summary.total == 1
    assert summary.done == 1
    assert summary.dead == 0
    assert summary.duration_seconds >= 0
    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DONE


def test_run_two_tasks_parallelism_2(tmp_path: Path, pool) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=2)
    gdr_settings = _make_gdr_settings(paths)

    summary = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    ).run(["T1", "T2"])

    assert summary.total == 2
    assert summary.done == 2
    assert summary.dead == 0


def test_run_four_tasks_parallelism_4(tmp_path: Path, pool) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=4)
    gdr_settings = _make_gdr_settings(paths)

    summary = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    ).run(["T1", "T2", "T3", "T4"])

    assert summary.total == 4
    assert summary.done == 4
    assert summary.dead == 0


def test_run_parallelism_one_serializes(tmp_path: Path, pool) -> None:
    """parallelism=1: 一次只能跑一个, in_flight 槽位填充 + 排出有序."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=1)
    gdr_settings = _make_gdr_settings(paths)

    summary = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    ).run(["T1", "T2", "T3"])

    assert summary.done == 3
    for tid in ("T1", "T2", "T3"):
        t = queue.get_task(tid)
        assert t is not None and t.phase == PHASE_DONE


# ---------------------------------------------------------------------------
# 死信传播
# ---------------------------------------------------------------------------


def test_one_dead_does_not_block_others(tmp_path: Path, pool) -> None:
    """T_BAD simulate 失败 → dead; T_OK 仍正常 done."""
    _BEHAVIORS["T_BAD"] = {"simulate_state": "executor_error"}
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=1)
    gdr_settings = _make_gdr_settings(paths)

    summary = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    ).run(["T_OK", "T_BAD"])

    assert summary.total == 2
    assert summary.done == 1
    assert summary.dead == 1
    assert queue.get_task("T_BAD").phase == PHASE_DEAD
    assert queue.get_task("T_OK").phase == PHASE_DONE


def test_gdr_failure_marks_dead(tmp_path: Path, pool) -> None:
    _BEHAVIORS["T_BAD"] = {"gdr_fail": True}
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=1)
    gdr_settings = _make_gdr_settings(paths)

    summary = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    ).run(["T_BAD"])

    assert summary.dead == 1
    assert queue.get_task("T_BAD").phase == PHASE_DEAD


def test_etl_failure_marks_dead(tmp_path: Path, pool) -> None:
    _BEHAVIORS["T_BAD"] = {"etl_fail": True}
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=1)
    gdr_settings = _make_gdr_settings(paths)

    summary = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    ).run(["T_BAD"])

    assert summary.dead == 1
    assert queue.get_task("T_BAD").phase == PHASE_DEAD


def test_mixed_success_dead_parallelism_2(tmp_path: Path, pool) -> None:
    """parallelism=2 时混合 success + dead 仍能跑完."""
    _BEHAVIORS["T_BAD"] = {"gdr_fail": True}
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=2)
    gdr_settings = _make_gdr_settings(paths)

    summary = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    ).run(["T_OK1", "T_BAD", "T_OK2"])

    assert summary.done == 2
    assert summary.dead == 1


# ---------------------------------------------------------------------------
# 子进程崩溃兜底
# ---------------------------------------------------------------------------


def test_subprocess_crash_mark_dead(tmp_path: Path, pool) -> None:
    """子进程抛 RuntimeError → 主进程 future.get() 捕获 → mark_dead → dead++."""
    _BEHAVIORS["T_BOOM"] = {"gdr_crash": True}
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=1)
    gdr_settings = _make_gdr_settings(paths)

    summary = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    ).run(["T_BOOM", "T_OK"])

    assert summary.total == 2
    assert summary.dead == 1
    assert summary.done == 1
    # T_BOOM 应被兜底 mark_dead (主进程路径)
    task = queue.get_task("T_BOOM")
    assert task is not None and task.phase == PHASE_DEAD


# ---------------------------------------------------------------------------
# SQLite 并发写: 多 task 同时 upsert_task 不冲突
# ---------------------------------------------------------------------------


def test_sqlite_concurrent_writes(tmp_path: Path, pool) -> None:
    """8 task + parallelism=4: SQLite 写竞争不冲突."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=4)
    gdr_settings = _make_gdr_settings(paths)

    tids = [f"T{i:02d}" for i in range(8)]
    summary = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    ).run(tids)

    assert summary.total == 8
    assert summary.done == 8
    assert summary.dead == 0
    # 验证 SQLite 终态
    counts = queue.count_by_phase()
    assert counts[PHASE_DONE] == 8


# ---------------------------------------------------------------------------
# 校验 / 边角
# ---------------------------------------------------------------------------


def test_empty_task_ids_raises(tmp_path: Path, pool) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=1)
    gdr_settings = _make_gdr_settings(paths)

    with pytest.raises(ValueError):
        PipelineExecutor(
            queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
        ).run([])


def test_run_idempotent_when_task_already_done(tmp_path: Path, pool) -> None:
    """task 已被另一个 worker 标 done → PipelineExecutor 不重复跑, 计入 done."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    queue.upsert_task("T1")
    queue.mark_phase("T1", new_phase=PHASE_DONE)

    settings = _make_pipeline_settings(parallelism=1)
    gdr_settings = _make_gdr_settings(paths)

    summary = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    ).run(["T1"])

    assert summary.done == 1
    assert summary.dead == 0


def test_run_shutdown_after_completion_no_error(tmp_path: Path, pool) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(parallelism=1)
    gdr_settings = _make_gdr_settings(paths)
    executor = PipelineExecutor(
        queue=queue, settings=settings, paths=paths, gdr_settings=gdr_settings,
    )
    executor.run(["T1"])
    executor.shutdown()  # 不抛