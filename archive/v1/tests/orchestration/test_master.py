"""orchestration.master 单元测试 (新架构 simulation server → gdr → etl).

新 Master 仅持有配置 / queue / stop_event, 调度全部委托给 PipelineExecutor.

测试覆盖:
* Master.run 委托 PipelineExecutor → 返回 PipelineSummary
* Master.status 读 phases / total / last_updated
* Master.shutdown 设 stop_event
* Master._build_gdr_settings 锚定路径 + workers=1
* write_health 在 run 前 / 后都被调用
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
from orchestration.master import Master
from orchestration.pipeline_executor import PipelineSummary
from orchestration.queue import (
    PHASE_DEAD,
    PHASE_DONE,
    SQLiteQueue,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> dict[str, str]:
    paths = {
        "simulate_serve_config": str(tmp_path / "sim.yaml"),
        "trajectory_dir": str(tmp_path / "traj"),
        "refined_dir": str(tmp_path / "refined"),
        "etl_outputs_dir": str(tmp_path / "etl_outputs"),
        "sqlite_db": str(tmp_path / "q.db"),
        "dead_dir": str(tmp_path / "dead"),
        "log_dir": str(tmp_path / "logs"),
        "runs_dir": str(tmp_path / "runs"),
        "pid_file": str(tmp_path / "orch.pid"),
    }
    for sub in ("traj", "runs", "refined", "etl_outputs", "dead", "logs"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "sim.yaml").write_text("{}", encoding="utf-8")
    return paths


def _make_cfg(tmp_path: Path) -> OrchestrationConfig:
    """直接构造 OrchestrationConfig, 不走 load_config (避免根 config.yaml 依赖)."""
    from orchestration.settings import Paths, PipelineSettings
    paths_dict = _make_paths(tmp_path)
    paths_obj = Paths(
        simulate_serve_config=Path(paths_dict["simulate_serve_config"]),
        trajectory_dir=Path(paths_dict["trajectory_dir"]),
        runs_dir=Path(paths_dict["runs_dir"]),
        refined_dir=Path(paths_dict["refined_dir"]),
        etl_outputs_dir=Path(paths_dict["etl_outputs_dir"]),
        sqlite_db=Path(paths_dict["sqlite_db"]),
        dead_dir=Path(paths_dict["dead_dir"]),
        pid_file=Path(paths_dict["pid_file"]),
        log_dir=Path(paths_dict["log_dir"]),
    )
    settings_obj = PipelineSettings(
        max_parallelism=4,
        max_retry_gdr=1,
        max_retry_etl=1,
        retry_poll_seconds=0.05,
    )
    gdr_settings = GdrSettings(
        batch_output_dir=paths_obj.refined_dir, workers=1,
        llm_concurrency=1, max_files=1,
    )
    cfg = OrchestrationConfig(
        settings=settings_obj,
        paths=paths_obj,
        gdr_settings=gdr_settings,
        source_path="",
    )
    return cfg


# ---------------------------------------------------------------------------
# PipelineExecutor stub (替换 multiprocessing.Pool)
# ---------------------------------------------------------------------------


_BEHAVIORS: dict[str, dict[str, Any]] = {}


class _FakeAsyncResult:
    def __init__(self, task_id, queue, paths, behavior):
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
            queue = self._queue
            tid = self._task_id
            behavior = self._behavior

            queue.upsert_task(tid)
            queue.mark_phase(tid, new_phase="simulate")

            traj_dir = self._paths.trajectory_dir
            traj_dir.mkdir(parents=True, exist_ok=True)
            traj_path = traj_dir / f"{tid}__session_{tid}.json"
            traj_path.write_text("{}", encoding="utf-8")

            run_state = behavior.get("simulate_state", "success")
            if run_state != "success":
                queue.mark_failed(
                    tid, stage="simulate", error_msg=f"simulate={run_state}",
                )
                self._result = {
                    "task_id": tid, "phase": "dead",
                    "stage": "simulate", "error": f"simulate={run_state}",
                }
                self._ready_flag = True
                return

            queue.mark_phase(
                tid, new_phase="gdr",
                run_id=tid, session_id=f"session_{tid}", src_path=traj_path,
            )

            if behavior.get("gdr_fail", False):
                queue.mark_failed(tid, stage="gdr", error_msg="gdr=fail")
                self._result = {
                    "task_id": tid, "phase": "dead",
                    "stage": "gdr", "error": "gdr=fail",
                }
                self._ready_flag = True
                return

            refined_path = self._paths.refined_dir / f"{tid}.json"
            refined_path.write_text("{}", encoding="utf-8")
            queue.mark_phase(tid, new_phase="etl", gdr_refined_path=refined_path)

            if behavior.get("etl_fail", False):
                queue.mark_failed(tid, stage="etl", error_msg="etl=fail")
                self._result = {
                    "task_id": tid, "phase": "dead",
                    "stage": "etl", "error": "etl=fail",
                }
                self._ready_flag = True
                return

            base = self._paths.etl_outputs_dir / f"{tid}"
            msgs = base.with_suffix(".messages.json")
            openai = base.with_suffix(".openai.json")
            meta = base.with_suffix(".meta.json")
            for p in (msgs, openai, meta):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}", encoding="utf-8")
            queue.mark_phase(
                tid, new_phase="done",
                etl_messages_path=msgs, etl_openai_path=openai,
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
    def __init__(self, *, processes, initializer, initargs):
        if initializer is not None:
            initializer(*initargs)

    def apply_async(self, fn, args):
        tid = args[0]
        paths_obj = args[1]
        queue = SQLiteQueue(paths_obj.sqlite_db)
        behavior = _BEHAVIORS.get(tid, {})
        return _FakeAsyncResult(tid, queue, paths_obj, behavior)

    def close(self):
        pass

    def join(self):
        pass


@pytest.fixture(autouse=True)
def _clear_behaviors():
    _BEHAVIORS.clear()
    yield
    _BEHAVIORS.clear()


@pytest.fixture
def fake_pool(monkeypatch):
    monkeypatch.setattr(multiprocessing, "Pool", _FakePool)
    return _FakePool


# ---------------------------------------------------------------------------
# 主循环: Master.run → PipelineExecutor.run
# ---------------------------------------------------------------------------


def test_master_run_delegates_to_pipeline_executor(tmp_path: Path, fake_pool) -> None:
    """Master.run 直接返回 PipelineExecutor.run 的结果."""
    cfg = _make_cfg(tmp_path)
    master = Master(cfg=cfg)

    summary = master.run(["T1", "T2"])
    assert isinstance(summary, PipelineSummary)
    assert summary.total == 2
    assert summary.done == 2


def test_master_run_returns_summary_with_dead(tmp_path: Path, fake_pool) -> None:
    _BEHAVIORS["T_BAD"] = {"gdr_fail": True}
    cfg = _make_cfg(tmp_path)
    master = Master(cfg=cfg)

    summary = master.run(["T_OK", "T_BAD"])
    assert summary.done == 1
    assert summary.dead == 1


# ---------------------------------------------------------------------------
# write_health 在 run 前 / 后都被调用
# ---------------------------------------------------------------------------


def test_master_run_writes_health_before_and_after(tmp_path: Path, fake_pool) -> None:
    cfg = _make_cfg(tmp_path)
    master = Master(cfg=cfg)

    master.run(["T1", "T2"])

    health_path = Path(cfg.paths.log_dir) / "health.json"
    assert health_path.exists()
    data = json.loads(health_path.read_text(encoding="utf-8"))
    # run 后 health 含 completed + summary
    assert data.get("status") == "completed"
    assert "summary" in data
    assert data["summary"]["done"] == 2
    assert data["summary"]["dead"] == 0
    # phases 也写入了
    assert "phases" in data
    assert data["phases"]["done"] == 2


# ---------------------------------------------------------------------------
# Master.status
# ---------------------------------------------------------------------------


def test_master_status_returns_phases_and_total(tmp_path: Path, fake_pool) -> None:
    cfg = _make_cfg(tmp_path)
    master = Master(cfg=cfg)
    # 跑完后查 status
    master.run(["T1"])
    status = master.status()
    assert "phases" in status
    assert "total" in status
    assert "last_updated" in status
    assert status["phases"]["done"] == 1
    assert status["total"] == 1


def test_master_status_initial_state(tmp_path: Path, fake_pool) -> None:
    """未跑任何 task 前 status 应含 7 个 phase 全 0 字段 (含 audited).

    audited = 评分低但结构合格 session 的终态, 见 CLAUDE.md "数据保留原则".
    """
    cfg = _make_cfg(tmp_path)
    master = Master(cfg=cfg)
    status = master.status()
    assert set(status["phases"].keys()) == {
        "pending", "simulate", "gdr", "etl", "done", "dead", "audited",
    }
    assert status["total"] == 0


# ---------------------------------------------------------------------------
# Master.shutdown
# ---------------------------------------------------------------------------


def test_master_shutdown_sets_stop_event(tmp_path: Path, fake_pool) -> None:
    cfg = _make_cfg(tmp_path)
    master = Master(cfg=cfg)
    assert not master._stop_event.is_set()
    master.shutdown()
    assert master._stop_event.is_set()


# ---------------------------------------------------------------------------
# _build_gdr_settings
# ---------------------------------------------------------------------------


def test_master_build_gdr_settings_anchored(tmp_path: Path, fake_pool) -> None:
    """_build_gdr_settings 必须 workers=1 + 锚到 refined_dir/log_dir."""
    cfg = _make_cfg(tmp_path)
    master = Master(cfg=cfg)
    settings = master._build_gdr_settings()
    assert settings.workers == 1
    assert Path(settings.batch_output_dir) == Path(cfg.paths.refined_dir)
    assert Path(settings.log_dir) == Path(cfg.paths.log_dir)


# ---------------------------------------------------------------------------
# 旧 batch 相关方法已删除
# ---------------------------------------------------------------------------


def test_master_no_batch_methods(tmp_path: Path) -> None:
    """契约 §6.3: Master 不再持有 batch 相关方法."""
    cfg = _make_cfg(tmp_path)
    master = Master(cfg=cfg)
    # 这些方法必须不存在
    for name in (
        "start_workers", "_add_thread", "_start_batch_watcher",
        "_first_scan_watcher", "wait_batch_drained",
        "register_active_batch", "unregister_active_batch",
        "_reaper_loop", "_reaper_thread", "_run_one_batch",
        "_count_terminal_for_batch",
    ):
        assert not hasattr(master, name), f"Master.{name} should be deleted"

    # 这些字段也不应存在
    for attr in ("_threads", "_workers_started", "_active_batch_ids"):
        assert not hasattr(master, attr), f"Master.{attr} should be deleted"


def test_master_no_alive_workers_property(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    master = Master(cfg=cfg)
    assert not hasattr(master, "alive_workers")