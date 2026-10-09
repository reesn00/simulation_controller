"""失败注入 / 恢复路径测试 (新架构 simulation server → gdr → etl).

新架构下"失败恢复"的语义变化:

* 不再有 ``*_processing`` 中间状态 / lock — 全部任务在子进程内
  顺序跑完 simulate → gdr → etl, Pool 管理并发。
* 不再有 ``batch`` 概念 — Master.run(task_ids) 一次性提交全部。
* 子进程崩溃由 ``PipelineExecutor`` 的 ``future.get()`` 捕获,
  mark_failed + dead++ 兜底。
* 阶段内重试在 ``task_pipeline._run_one_task_pipeline`` 内做
  (max_retry_gdr / max_retry_etl)。
* ``failure_handler.reap_dead`` 负责把 dead task 的产物归档到 dead_dir,
  ``queue.requeue_dead`` 把 dead 重置为 pending (replay)。

本文件覆盖:

* 端到端失败注入 (Master.run 路径): gdr / etl 永久失败 → dead
* 子进程崩溃 → 主进程兜底 mark_failed + 计入 dead
* ``reap_dead`` 把 dead task 产物移到 dead_dir (新命名: <task_id>__<src_basename>)
* ``requeue_dead`` (replay) 把 dead 重置为 pending
"""

from __future__ import annotations

import json
import multiprocessing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gdr.config.settings import Settings as GdrSettings
from orchestration.config_loader import OrchestrationConfig
from orchestration.failure_handler import reap_dead
from orchestration.master import Master
from orchestration.queue import (
    PHASE_DEAD,
    PHASE_DONE,
    PHASE_PENDING,
    SQLiteQueue,
)
from orchestration.settings import Paths, PipelineSettings


# ---------------------------------------------------------------------------
# fixtures & helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> Paths:
    for sub in (
        "trajectory_dir", "runs_dir", "refined_dir",
        "etl_outputs_dir", "dead_dir", "log_dir",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "sim.yaml").write_text("{}", encoding="utf-8")
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


def _make_cfg(tmp_path: Path) -> OrchestrationConfig:
    paths = _make_paths(tmp_path)
    settings = PipelineSettings(
        max_parallelism=4,
        max_retry_gdr=2,
        max_retry_etl=2,
        retry_poll_seconds=0.05,
    )
    gdr_settings = GdrSettings(
        batch_output_dir=paths.refined_dir,
        workers=1, llm_concurrency=1, max_files=1,
    )
    return OrchestrationConfig(
        settings=settings, paths=paths,
        gdr_settings=gdr_settings, source_path="",
    )


# ---------------------------------------------------------------------------
# FakePool: 模拟 multiprocessing.Pool, 用 _BEHAVIORS 控制每个 task 的命运
# ---------------------------------------------------------------------------


_BEHAVIORS: dict[str, dict[str, Any]] = {}


def _reset_behaviors() -> None:
    _BEHAVIORS.clear()


@dataclass
class _FakeTaskRun:
    run_id: str
    remote_session_id: str
    state: str = "success"


@dataclass
class _FakeGdrResult:
    refined_path: Path
    task_id: str
    session_id: str
    duration_seconds: float = 0.0


@dataclass
class _FakeEtlOutputs:
    messages_path: Path
    openai_path: Path
    qwenjina_path: Path | None
    meta_path: Path
    task_id: str
    session_id: str
    duration_seconds: float = 0.0


class _FakeAsyncResult:
    def __init__(self, task_id: str, paths: Paths, behavior: dict[str, Any]) -> None:
        self._task_id = task_id
        self._paths = paths
        self._behavior = behavior
        self._result: dict | None = None
        self._exc: BaseException | None = None
        self._ready_flag = False
        self._compute()

    def _compute(self) -> None:
        behavior = self._behavior
        tid = self._task_id
        paths = self._paths
        try:
            queue = SQLiteQueue(paths.sqlite_db)
            queue.upsert_task(tid)
            queue.mark_phase(tid, new_phase="simulate")

            simulate_state = behavior.get("simulate_state", "success")
            if simulate_state != "success":
                queue.mark_failed(
                    tid, stage="simulate",
                    error_msg=f"simulate={simulate_state}",
                )
                self._result = {
                    "task_id": tid, "phase": "dead",
                    "stage": "simulate",
                    "error": f"simulate={simulate_state}",
                }
                self._ready_flag = True
                return

            # simulate 成功, 写 trajectory
            traj_dir = paths.trajectory_dir
            traj_dir.mkdir(parents=True, exist_ok=True)
            traj_path = traj_dir / f"{tid}__session_{tid}.json"
            traj_path.write_text("{}", encoding="utf-8")
            queue.mark_phase(
                tid, new_phase="gdr",
                run_id=tid, session_id=f"session_{tid}",
                src_path=traj_path,
            )

            # gdr 阶段
            if behavior.get("gdr_fail", False):
                # 失败前可选择写半成品 refined (模拟部分进度)
                if behavior.get("gdr_partial_artifact", False):
                    refined_path = paths.refined_dir / f"{tid}__partial.json"
                    refined_path.parent.mkdir(parents=True, exist_ok=True)
                    refined_path.write_text("{}", encoding="utf-8")
                    queue.mark_phase(
                        tid, new_phase="gdr",
                        gdr_refined_path=refined_path,
                    )
                # attempts 累加 + 标记 dead
                queue.increment_attempts(tid, stage="gdr")
                queue.mark_failed(tid, stage="gdr", error_msg="gdr=fail")
                self._result = {
                    "task_id": tid, "phase": "dead",
                    "stage": "gdr", "error": "gdr=fail",
                }
                self._ready_flag = True
                return

            # gdr 成功
            refined_path = paths.refined_dir / f"{tid}__refined.json"
            refined_path.parent.mkdir(parents=True, exist_ok=True)
            refined_path.write_text("{}", encoding="utf-8")
            queue.mark_phase(tid, new_phase="etl", gdr_refined_path=refined_path)

            # etl 阶段
            if behavior.get("etl_fail", False):
                queue.increment_attempts(tid, stage="etl")
                queue.mark_failed(tid, stage="etl", error_msg="etl=fail")
                self._result = {
                    "task_id": tid, "phase": "dead",
                    "stage": "etl", "error": "etl=fail",
                }
                self._ready_flag = True
                return

            # etl 成功, 写 4 视图 (此处简化为 3 视图; 缺 qwenjina 不影响 done)
            base = paths.etl_outputs_dir / f"{tid}"
            msgs = base.with_suffix(".messages.json")
            openai_p = base.with_suffix(".openai.json")
            meta = base.with_suffix(".meta.json")
            for p in (msgs, openai_p, meta):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}", encoding="utf-8")
            queue.mark_phase(
                tid, new_phase="done",
                etl_messages_path=msgs, etl_openai_path=openai_p,
                etl_qwenjina_path=None, etl_meta_path=meta,
            )
            self._result = {
                "task_id": tid, "phase": "done",
                "stage": "done", "error": None,
            }
            self._ready_flag = True
        except BaseException as exc:  # noqa: BLE001
            self._exc = exc
            self._ready_flag = True

    def ready(self, timeout=None):
        return self._ready_flag

    def get(self, timeout=None):
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


class _FakePool:
    """替换 multiprocessing.Pool: ``apply_async`` 同步返回 _FakeAsyncResult."""

    def __init__(self, *, processes, initializer, initargs):
        if initializer is not None:
            initializer(*initargs)

    def apply_async(self, fn, args):
        tid = args[0]
        paths_obj = args[1]
        behavior = _BEHAVIORS.get(tid, {})
        return _FakeAsyncResult(tid, paths_obj, behavior)

    def close(self):
        pass

    def join(self):
        pass


class _CrashPool:
    """替换 multiprocessing.Pool: apply_async 抛 Exception 模拟子进程崩溃."""

    def __init__(self, *, processes, initializer, initargs):
        if initializer is not None:
            initializer(*initargs)

    def apply_async(self, fn, args):
        tid = args[0]
        paths_obj = args[1]
        # 把 task upsert 一下, 让 mark_failed 有目标
        SQLiteQueue(paths_obj.sqlite_db).upsert_task(tid)

        class _Crashing:
            def __init__(self, task_id):
                self._tid = task_id
                self._ready = True

            def ready(self, timeout=None):
                return self._ready

            def get(self, timeout=None):
                raise RuntimeError(f"subprocess crashed for {self._tid}")

        return _Crashing(tid)

    def close(self):
        pass

    def join(self):
        pass


@pytest.fixture(autouse=True)
def _cleanup_behaviors():
    _reset_behaviors()
    yield
    _reset_behaviors()


@pytest.fixture
def fake_pool(monkeypatch):
    monkeypatch.setattr(multiprocessing, "Pool", _FakePool)
    return _FakePool


@pytest.fixture
def crash_pool(monkeypatch):
    monkeypatch.setattr(multiprocessing, "Pool", _CrashPool)
    return _CrashPool


# ---------------------------------------------------------------------------
# 端到端失败注入: gdr 永久失败 → dead
# ---------------------------------------------------------------------------


def test_failure_injection_gdr_reaches_dead_via_master(
    tmp_path: Path, fake_pool,
) -> None:
    """gdr 永久失败 → 任务 dead, attempts_gdr 累加, Master summary.dead=1."""
    cfg = _make_cfg(tmp_path)
    _BEHAVIORS["T_BAD"] = {"gdr_fail": True}
    master = Master(cfg=cfg)

    summary = master.run(["T_OK", "T_BAD"])

    assert summary.total == 2
    assert summary.done == 1
    assert summary.dead == 1

    queue = SQLiteQueue(cfg.paths.sqlite_db)
    good = queue.get_task("T_OK")
    bad = queue.get_task("T_BAD")
    assert good is not None and good.phase == PHASE_DONE
    assert bad is not None and bad.phase == PHASE_DEAD
    # gdr 阶段尝试次数 = 0 (FakePool 走单次失败路径)
    assert bad.attempts_gdr == 1


def test_failure_injection_etl_reaches_dead_via_master(
    tmp_path: Path, fake_pool,
) -> None:
    """etl 永久失败 → 任务 dead, attempts_etl 累加."""
    cfg = _make_cfg(tmp_path)
    _BEHAVIORS["T_BAD"] = {"etl_fail": True}
    master = Master(cfg=cfg)

    summary = master.run(["T_OK", "T_BAD"])

    assert summary.done == 1
    assert summary.dead == 1
    queue = SQLiteQueue(cfg.paths.sqlite_db)
    bad = queue.get_task("T_BAD")
    assert bad is not None
    assert bad.phase == PHASE_DEAD
    assert bad.attempts_etl == 1
    # gdr 通过, 留下了 refined_path; 后续 reap_dead 可移
    assert bad.gdr_refined_path is not None


# ---------------------------------------------------------------------------
# 子进程崩溃兜底: crash_pool
# ---------------------------------------------------------------------------


def test_subprocess_crash_marked_dead_by_master(
    tmp_path: Path, crash_pool,
) -> None:
    """子进程抛未捕获异常 → PipelineExecutor 兜底 mark_failed + dead++."""
    cfg = _make_cfg(tmp_path)
    master = Master(cfg=cfg)

    summary = master.run(["T_CRASH"])

    assert summary.total == 1
    assert summary.done == 0
    assert summary.dead == 1

    queue = SQLiteQueue(cfg.paths.sqlite_db)
    t = queue.get_task("T_CRASH")
    assert t is not None
    # 子进程崩溃 → 主进程按契约 §5.2 把 stage 标 simulate (兜底默认阶段)
    # + error_msg 含 "pipeline crashed" 前缀
    assert t.phase == PHASE_DEAD
    assert t.error_msg is not None
    assert "pipeline crashed" in t.error_msg


def test_subprocess_crash_partial_run_does_not_block(
    tmp_path: Path, crash_pool,
) -> None:
    """多个 task 中部分崩溃: 后续 task 仍能跑 (在 crash_pool 下都崩, 但总账正确)."""
    cfg = _make_cfg(tmp_path)
    master = Master(cfg=cfg)

    summary = master.run(["T_A", "T_B", "T_C"])

    assert summary.total == 3
    assert summary.done == 0
    assert summary.dead == 3
    queue = SQLiteQueue(cfg.paths.sqlite_db)
    counts = queue.count_by_phase()
    assert counts[PHASE_DEAD] == 3


# ---------------------------------------------------------------------------
# reap_dead: 把 dead task 产物移入 dead_dir (新 task_id 命名)
# ---------------------------------------------------------------------------


def test_dead_artifact_archived_to_dead_dir(
    tmp_path: Path, fake_pool,
) -> None:
    """gdr 失败且 partial artifact 写入 → reap_dead 把它移到 dead_dir.

    新架构下文件名格式: ``<task_id>__<src_basename>`` (不再带 batch_id)。
    """
    cfg = _make_cfg(tmp_path)
    paths = cfg.paths
    _BEHAVIORS["T_X"] = {"gdr_fail": True, "gdr_partial_artifact": True}
    master = Master(cfg=cfg)
    master.run(["T_X"])

    queue = SQLiteQueue(paths.sqlite_db)
    t = queue.get_task("T_X")
    assert t is not None and t.phase == PHASE_DEAD
    # refined 部分产物 + src 都在
    assert t.src_path is not None
    assert Path(t.src_path).is_file()
    assert t.gdr_refined_path is not None
    assert Path(t.gdr_refined_path).is_file()

    dead_dir = Path(paths.dead_dir)
    archives = reap_dead(queue, dead_dir=dead_dir)
    moved = [a for a in archives if a.moved_to]
    assert len(moved) == 1

    # 命名格式: <digits>__<src_basename>, 数字是 SQLite rowid (reap_dead 实现),
    # 没有 batch_id 前缀.
    moved_paths = moved[0].moved_to
    names = [Path(p).name for p in moved_paths]
    import re
    for name in names:
        m = re.match(r"^(\d+)__(.+)$", name)
        assert m, f"unexpected name format {name!r}"
        assert "batch_" not in name


def test_dead_with_no_artifacts_does_not_crash_reap(
    tmp_path: Path, fake_pool,
) -> None:
    """dead task 但 src_path 为空 (例如 simulate 阶段直接死) → reap 不抛."""
    cfg = _make_cfg(tmp_path)
    _BEHAVIORS["T_NOSRC"] = {"simulate_state": "executor_error"}
    master = Master(cfg=cfg)
    master.run(["T_NOSRC"])

    queue = SQLiteQueue(cfg.paths.sqlite_db)
    t = queue.get_task("T_NOSRC")
    assert t is not None and t.phase == PHASE_DEAD
    # reap_dead 应当返回归档记录, 但 moved_to 为空
    archives = reap_dead(queue, dead_dir=Path(cfg.paths.dead_dir))
    assert len(archives) == 1
    assert archives[0].moved_to == []


# ---------------------------------------------------------------------------
# requeue_dead (replay) — 把 dead 重置为 pending
# ---------------------------------------------------------------------------


def test_requeue_dead_resets_phase_and_attempts(
    tmp_path: Path, fake_pool,
) -> None:
    """replay 子命令: dead → pending; attempts_gdr / attempts_etl 应当保留
    (契约: requeue_dead 只清 phase, 不清 attempts; attempts 是历史证据)."""
    cfg = _make_cfg(tmp_path)
    _BEHAVIORS["T_DEAD"] = {"gdr_fail": True}
    master = Master(cfg=cfg)
    master.run(["T_DEAD"])

    queue = SQLiteQueue(cfg.paths.sqlite_db)
    t = queue.get_task("T_DEAD")
    assert t is not None and t.phase == PHASE_DEAD

    n = queue.requeue_dead()
    assert n == 1

    refreshed = queue.get_task("T_DEAD")
    assert refreshed is not None
    assert refreshed.phase == PHASE_PENDING
    # attempts 不应被清零 (契约: 历史诊断信息保留)
    assert refreshed.attempts_gdr == 1


def test_requeue_dead_only_targets_dead_phase(
    tmp_path: Path, fake_pool,
) -> None:
    """requeue_dead 不应误碰 done 任务."""
    cfg = _make_cfg(tmp_path)
    _BEHAVIORS["T_BAD"] = {"gdr_fail": True}

    # 一次性跑混合 done + dead (行为在跑前已设好)
    master = Master(cfg=cfg)
    master.run(["T_OK", "T_BAD"])

    queue = SQLiteQueue(cfg.paths.sqlite_db)
    counts_before = queue.count_by_phase()
    assert counts_before[PHASE_DONE] == 1
    assert counts_before[PHASE_DEAD] == 1

    n = queue.requeue_dead()
    assert n == 1

    counts_after = queue.count_by_phase()
    # done 仍是 1 (不重置)
    assert counts_after[PHASE_DONE] == 1
    assert counts_after[PHASE_PENDING] == 1
    assert counts_after[PHASE_DEAD] == 0


# ---------------------------------------------------------------------------
# 混合 done + dead 的 end-to-end 批次
# ---------------------------------------------------------------------------


def test_master_run_mixed_done_and_dead_via_executor(
    tmp_path: Path, fake_pool,
) -> None:
    """混合 done / dead 的一次性提交: summary + queue phases 全部正确."""
    cfg = _make_cfg(tmp_path)
    _BEHAVIORS["T_BAD_S"] = {"simulate_state": "executor_error"}
    _BEHAVIORS["T_BAD_G"] = {"gdr_fail": True}
    _BEHAVIORS["T_BAD_E"] = {"etl_fail": True}
    master = Master(cfg=cfg)

    summary = master.run(["T_OK_1", "T_OK_2", "T_BAD_S", "T_BAD_G", "T_BAD_E"])

    assert summary.total == 5
    assert summary.done == 2
    assert summary.dead == 3

    queue = SQLiteQueue(cfg.paths.sqlite_db)
    counts = queue.count_by_phase()
    assert counts[PHASE_DONE] == 2
    assert counts[PHASE_DEAD] == 3

    # dead.log / health.json 都应被 master 写出
    health = Path(cfg.paths.log_dir) / "health.json"
    assert health.exists()
    data = json.loads(health.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["summary"]["done"] == 2
    assert data["summary"]["dead"] == 3