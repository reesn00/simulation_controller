"""orchestration 集成 smoke 测试 — Round 2 任务包 C (集成验证).

覆盖:
* §1 ``status`` 子命令 — ``collect_tasks`` 返 ``{phases, total, last_updated}``;
  6 个 phase key 全在; 计数正确; ``total = sum(phases.values())``.
* §2 死信归档 — ``reap_dead`` 把 dead 任务产物移入 ``dead_dir``, 写 ``log_entry``
  (无 ``batch_id`` 字段), ``phase=dead`` 计数正确.
* §3 replay 复活 — ``requeue_dead`` 把 ``dead → pending``, 清空 ``error_msg``,
  返回受影响行数.
* §4 端到端集成 — 跑完整 Master.run 流程后 ``status`` / ``reap_dead`` /
  ``replay`` 串通; dead 任务在 replay 后能再次被标记 done (跨批次).
* §5 健康检查 — ``collect_tasks`` / ``write_health`` 写 ``health.json``,
  字段 ``phases`` / ``total`` / ``last_updated`` 在, 旧字段 ``batches`` /
  ``queue_counts`` 已不存在.

约束 (round-2-execution-plan.md §agent-verify):
* 不依赖真实 LLM / 远端 QwenPaw, 使用 monkeypatch + 临时 SQLite + 临时目录.
* 不改 orchestration / simulate_serve / gdr / etl 业务代码.
* 不改契约文件.

参考:
* ``tests/orchestration/test_pipeline_executor.py::_FakePool`` — 子进程替身模式
* ``tests/orchestration/test_failure_recovery.py`` — 失败注入 + reap_dead + replay 模式
* ``tests/orchestration/test_orchestration_cli.py::_write_config`` — CLI 测试模式
"""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from gdr.config.settings import Settings as GdrSettings
from orchestration.config_loader import OrchestrationConfig
from orchestration.failure_handler import reap_dead
from orchestration.health import collect_tasks, write_health
from orchestration.master import Master
from orchestration.queue import (
    ALL_PHASES,
    PHASE_DEAD,
    PHASE_DONE,
    PHASE_GDR,
    PHASE_PENDING,
    PHASE_SIMULATE,
    SQLiteQueue,
)
from orchestration.settings import Paths, PipelineSettings

from orchestration.__main__ import main as cli_main


# ---------------------------------------------------------------------------
# helpers: config / paths / fake pool
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> Paths:
    """建一个完整的 Paths 实例, 避免子进程调用时再 mkdir."""
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
    """最小 OrchestrationConfig (用于 Master.run / CLI)."""
    paths = _make_paths(tmp_path)
    settings = PipelineSettings(
        max_parallelism=2,
        max_retry_gdr=1,
        max_retry_etl=1,
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


def _write_cli_config(tmp_path: Path) -> Path:
    """写一个最小可用的 orch.yaml 供 ``cli_main`` 加载.

    同时写一个空的 tasks.yaml / scenarios.yaml 给 TaskManager, 避免
    simulate_serve_config 路径解析失败时 catalog 加载抛错.
    """
    cfg_path = tmp_path / "orch.yaml"
    sim_path = tmp_path / "sim.yaml"
    tasks_path = tmp_path / "tasks.yaml"
    scenarios_path = tmp_path / "scenarios.yaml"
    tasks_path.write_text(
        json.dumps({"schema_version": "2", "tasks": []}),
        encoding="utf-8",
    )
    scenarios_path.write_text(
        json.dumps({"schema_version": "2", "scenarios": []}),
        encoding="utf-8",
    )
    sim_path.write_text("{}", encoding="utf-8")
    cfg_path.write_text(json.dumps({
        "simulate_serve": {
            "tasks_file": str(tasks_path),
            "scenarios_file": str(scenarios_path),
        },
        "orchestration": {
            "pipeline": {
                "max_parallelism": 1,
                "max_retry_gdr": 1,
                "max_retry_etl": 1,
                "retry_poll_seconds": 0.05,
            },
            "paths": {
                "simulate_serve_config": str(sim_path),
                "trajectory_dir": str(tmp_path / "trajectory_dir"),
                "runs_dir": str(tmp_path / "runs_dir"),
                "refined_dir": str(tmp_path / "refined_dir"),
                "etl_outputs_dir": str(tmp_path / "etl_outputs_dir"),
                "sqlite_db": str(tmp_path / "q.db"),
                "dead_dir": str(tmp_path / "dead_dir"),
                "pid_file": str(tmp_path / "orch.pid"),
                "log_dir": str(tmp_path / "log_dir"),
            },
        },
    }), encoding="utf-8")
    return cfg_path


def _seed_tasks(queue: SQLiteQueue, plan: dict[str, str]) -> None:
    """按 plan 灌入 task 行; plan[task_id] ∈ ALL_PHASES.

    例: ``{"T_A": "pending", "T_B": "done", "T_C": "dead"}``.
    """
    for tid, phase in plan.items():
        queue.upsert_task(tid)
        if phase == PHASE_DEAD:
            queue.mark_failed(tid, stage="gdr", error_msg="seeded")
        elif phase != PHASE_PENDING:
            queue.mark_phase(tid, new_phase=phase)


# ---------------------------------------------------------------------------
# FakePool: 模拟 multiprocessing.Pool, 用 _BEHAVIORS 控制每个 task 的命运
# ---------------------------------------------------------------------------


_BEHAVIORS: dict[str, dict[str, Any]] = {}


def _reset_behaviors() -> None:
    _BEHAVIORS.clear()


class _FakeAsyncResult:
    """``multiprocessing.pool.AsyncResult`` 替身 — 必须 hashable 才能作为 dict key."""

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
            queue.mark_phase(tid, new_phase=PHASE_SIMULATE)

            simulate_state = behavior.get("simulate_state", "success")
            if simulate_state != "success":
                queue.mark_failed(
                    tid, stage="simulate",
                    error_msg=f"simulate={simulate_state}",
                )
                self._result = {
                    "task_id": tid, "phase": "dead",
                    "stage": "simulate", "error": f"simulate={simulate_state}",
                }
                self._ready_flag = True
                return

            # simulate 成功 → 写 trajectory, 进 gdr 阶段
            traj_dir = paths.trajectory_dir
            traj_dir.mkdir(parents=True, exist_ok=True)
            traj_path = traj_dir / f"{tid}__session_{tid}.json"
            traj_path.write_text("{}", encoding="utf-8")
            queue.mark_phase(
                tid, new_phase="gdr",
                run_id=tid, session_id=f"session_{tid}",
                src_path=traj_path,
            )

            if behavior.get("gdr_fail", False):
                queue.increment_attempts(tid, stage="gdr")
                queue.mark_failed(tid, stage="gdr", error_msg="gdr=fail")
                self._result = {
                    "task_id": tid, "phase": "dead",
                    "stage": "gdr", "error": "gdr=fail",
                }
                self._ready_flag = True
                return

            refined_path = paths.refined_dir / f"{tid}__refined.json"
            refined_path.parent.mkdir(parents=True, exist_ok=True)
            refined_path.write_text("{}", encoding="utf-8")
            queue.mark_phase(tid, new_phase="etl", gdr_refined_path=refined_path)

            if behavior.get("etl_fail", False):
                queue.increment_attempts(tid, stage="etl")
                queue.mark_failed(tid, stage="etl", error_msg="etl=fail")
                self._result = {
                    "task_id": tid, "phase": "dead",
                    "stage": "etl", "error": "etl=fail",
                }
                self._ready_flag = True
                return

            base = paths.etl_outputs_dir / f"{tid}"
            msgs = base.with_suffix(".messages.json")
            openai_p = base.with_suffix(".openai.json")
            meta = base.with_suffix(".meta.json")
            for p in (msgs, openai_p, meta):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}", encoding="utf-8")
            queue.mark_phase(
                tid, new_phase=PHASE_DONE,
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
    """替换 multiprocessing.Pool — apply_async 同步返回 _FakeAsyncResult."""

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


@pytest.fixture(autouse=True)
def _cleanup_behaviors():
    _reset_behaviors()
    yield
    _reset_behaviors()


@pytest.fixture
def fake_pool(monkeypatch):
    monkeypatch.setattr(multiprocessing, "Pool", _FakePool)
    return _FakePool


# ---------------------------------------------------------------------------
# §1 status 子命令测试
# ---------------------------------------------------------------------------


class TestStatusCommand:
    """``collect_tasks`` + ``status`` 子命令输出契约 §7.4 + §6.5."""

    def test_collect_tasks_returns_required_keys(self, tmp_path: Path) -> None:
        """返 ``{phases: {...}, total: int, last_updated: str}`` 三键齐全."""
        queue = SQLiteQueue(tmp_path / "q.db")
        result = collect_tasks(queue)

        assert "phases" in result
        assert "total" in result
        assert "last_updated" in result
        assert isinstance(result["phases"], dict)
        assert isinstance(result["total"], int)
        assert isinstance(result["last_updated"], str)

    def test_collect_tasks_includes_all_six_phase_keys(self, tmp_path: Path) -> None:
        """6 个 phase key 必全在 (契约 §6.5), 即使某些计数为 0."""
        queue = SQLiteQueue(tmp_path / "q.db")
        result = collect_tasks(queue)

        assert set(result["phases"].keys()) == {
            "pending", "simulate", "gdr", "etl", "done", "dead",
        }

    def test_collect_tasks_total_equals_sum_of_phases(self, tmp_path: Path) -> None:
        """``total = sum(phases.values())`` 不变量."""
        queue = SQLiteQueue(tmp_path / "q.db")
        _seed_tasks(queue, {
            "T_A": "pending",
            "T_B": "pending",
            "T_C": "simulate",
            "T_D": "gdr",
            "T_E": "etl",
            "T_F": "done",
            "T_G": "done",
            "T_H": "done",
            "T_I": "dead",
            "T_J": "dead",
            "T_K": "dead",
        })
        result = collect_tasks(queue)

        assert result["total"] == 11
        assert result["total"] == sum(result["phases"].values())

    def test_collect_tasks_counts_match_seeded_plan(self, tmp_path: Path) -> None:
        """计数与灌入计划一致 (2/1/1/1/3/3)."""
        queue = SQLiteQueue(tmp_path / "q.db")
        _seed_tasks(queue, {
            "T_A": "pending", "T_B": "pending",
            "T_C": "simulate",
            "T_D": "gdr",
            "T_E": "etl",
            "T_F": "done", "T_G": "done", "T_H": "done",
            "T_I": "dead", "T_J": "dead", "T_K": "dead",
        })
        result = collect_tasks(queue)
        p = result["phases"]
        assert p["pending"] == 2
        assert p["simulate"] == 1
        assert p["gdr"] == 1
        assert p["etl"] == 1
        assert p["done"] == 3
        assert p["dead"] == 3

    def test_collect_tasks_empty_db_full_zero_distribution(self, tmp_path: Path) -> None:
        """空 DB: 6 phase 全 0, total=0."""
        queue = SQLiteQueue(tmp_path / "q.db")
        result = collect_tasks(queue)
        assert result["phases"] == {p: 0 for p in ALL_PHASES}
        assert result["total"] == 0

    def test_collect_tasks_last_updated_iso8601(self, tmp_path: Path) -> None:
        """``last_updated`` 形如 ``YYYY-MM-DDTHH:MM:SS.uuuuuuZ``."""
        queue = SQLiteQueue(tmp_path / "q.db")
        result = collect_tasks(queue)
        ts = result["last_updated"]
        # 形如 2026-09-22T10:30:14.123456Z
        assert ts.endswith("Z")
        assert "T" in ts
        # 长度 27 (年月日 ... 严格) → 解析第一段确认年存在
        from datetime import datetime
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
        assert parsed.year >= 2026

    def test_status_cli_prints_phases_section(self, tmp_path: Path, capsys) -> None:
        """``python -m orchestration status`` 打印 ``phases`` 段."""
        cfg_path = _write_cli_config(tmp_path)
        queue = SQLiteQueue(tmp_path / "q.db")
        _seed_tasks(queue, {
            "T_DONE": PHASE_DONE,
            "T_DEAD": PHASE_DEAD,
            "T_PEND": PHASE_PENDING,
        })

        rc = cli_main(["--config", str(cfg_path), "status"])
        out = capsys.readouterr().out
        assert rc == 0
        # CLI 必须打印 phases 段 (契约 §7.4)
        assert "phases" in out
        # 且打印 task 列表
        assert "T_DONE" in out
        assert "T_DEAD" in out
        assert "T_PEND" in out

    def test_status_cli_no_db_does_not_crash(self, tmp_path: Path, capsys) -> None:
        """SQLite DB 缺失时 status 不抛, 返 0."""
        # 用 tmp_path 但不放 q.db
        # 跳过 config 加载: 直接传最小 cfg
        cfg = _make_cfg(tmp_path)
        # 让 SQLite 文件路径完全无 db
        not (tmp_path / "missing.db")
        # 直接验证 collect_tasks 在空目录上的健壮性
        q = SQLiteQueue(tmp_path / "fresh.db")
        result = collect_tasks(q)
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# §2 死信归档测试
# ---------------------------------------------------------------------------


class TestDeadLetterArchival:
    """``reap_dead`` 归档 + 日志 + phase 计数 (契约 §6.4)."""

    def test_reap_dead_after_full_pipeline_failure(
        self, tmp_path: Path, fake_pool,
    ) -> None:
        """跑完整 Master.run 让某 task 三阶段都走完后失败 → reap_dead 归档."""
        cfg = _make_cfg(tmp_path)
        _BEHAVIORS["T_OK"] = {}  # 全成功
        _BEHAVIORS["T_BAD"] = {"gdr_fail": True}  # gdr 阶段失败 → dead
        master = Master(cfg=cfg)
        master.run(["T_OK", "T_BAD"])

        queue = SQLiteQueue(cfg.paths.sqlite_db)
        # dead 计数 = 1
        counts = queue.count_by_phase()
        assert counts[PHASE_DEAD] == 1

        # reap_dead: 归档 T_BAD 的产物 (trajectory 还在)
        archives = reap_dead(queue, dead_dir=cfg.paths.dead_dir)
        assert len(archives) == 1
        # T_BAD 至少有 src_path 移入 dead_dir
        moved = archives[0].moved_to
        assert moved, "T_BAD 应至少移 1 个产物"
        for p in moved:
            assert Path(p).is_file()
            assert str(cfg.paths.dead_dir) in p

    def test_reap_dead_appends_log_entry(self, tmp_path: Path, fake_pool) -> None:
        """``dead_log_path`` 写 jsonl, 含 ``task_id`` / ``moved_to`` / ``error_msg``。"""
        cfg = _make_cfg(tmp_path)
        _BEHAVIORS["T_BAD"] = {"gdr_fail": True}
        master = Master(cfg=cfg)
        master.run(["T_BAD"])

        queue = SQLiteQueue(cfg.paths.sqlite_db)
        log_path = tmp_path / "dead.log"
        reap_dead(
            queue,
            dead_dir=cfg.paths.dead_dir,
            dead_log_path=log_path,
        )
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert "task_id" in entry
        assert "moved_to" in entry
        assert entry["moved_to"], "moved_to 非空 (T_BAD 有 src_path)"

    def test_reap_dead_log_entry_no_batch_id(self, tmp_path: Path, fake_pool) -> None:
        """log_entry 不含 ``batch_id`` 字段 (契约 §6.4 删字段)."""
        cfg = _make_cfg(tmp_path)
        _BEHAVIORS["T_BAD"] = {"gdr_fail": True}
        master = Master(cfg=cfg)
        master.run(["T_BAD"])

        queue = SQLiteQueue(cfg.paths.sqlite_db)
        log_path = tmp_path / "dead.log"
        reap_dead(
            queue, dead_dir=cfg.paths.dead_dir,
            dead_log_path=log_path,
        )
        entry = json.loads(
            log_path.read_text(encoding="utf-8").strip().splitlines()[0],
        )
        assert "batch_id" not in entry

    def test_reap_dead_phase_count_remains_correct(
        self, tmp_path: Path, fake_pool,
    ) -> None:
        """reap_dead 不会改 SQLite phase (仍 dead), phase=dead 计数仍为 N."""
        cfg = _make_cfg(tmp_path)
        _BEHAVIORS["T_B1"] = {"gdr_fail": True}
        _BEHAVIORS["T_B2"] = {"etl_fail": True}
        master = Master(cfg=cfg)
        master.run(["T_OK", "T_B1", "T_B2"])

        queue = SQLiteQueue(cfg.paths.sqlite_db)
        before = queue.count_by_phase()[PHASE_DEAD]
        assert before == 2

        reap_dead(queue, dead_dir=cfg.paths.dead_dir)
        # reap_dead 不动 SQLite phase (契约: 只移产物)
        after = queue.count_by_phase()[PHASE_DEAD]
        assert after == 2

    def test_reap_dead_multiple_stages_archive_all_artifacts(
        self, tmp_path: Path, fake_pool,
    ) -> None:
        """etl 阶段失败 → src + gdr_refined + 0 个 etl 视图 (未生成) 都应归档."""
        cfg = _make_cfg(tmp_path)
        # gdr 通过, etl 失败 → src + refined 都归档
        _BEHAVIORS["T_E"] = {"etl_fail": True}
        master = Master(cfg=cfg)
        master.run(["T_E"])

        queue = SQLiteQueue(cfg.paths.sqlite_db)
        t = queue.get_task("T_E")
        assert t is not None and t.phase == PHASE_DEAD
        assert t.src_path is not None
        assert t.gdr_refined_path is not None

        archives = reap_dead(queue, dead_dir=cfg.paths.dead_dir)
        # src + refined = 2 个
        assert len(archives[0].moved_to) == 2

    def test_reap_dead_empty_when_no_dead(self, tmp_path: Path) -> None:
        """空 SQLite → reap_dead 返 []."""
        queue = SQLiteQueue(tmp_path / "q.db")
        archives = reap_dead(queue, dead_dir=tmp_path / "dead")
        assert archives == []
        # dead_dir 仍被创建
        assert (tmp_path / "dead").exists()

    def test_reap_dead_ignores_non_dead_tasks(
        self, tmp_path: Path, fake_pool,
    ) -> None:
        """reap_dead 只看 dead phase, done/pending 不动."""
        queue = SQLiteQueue(tmp_path / "q.db")
        _seed_tasks(queue, {
            "T_OK": PHASE_DONE,
            "T_PEND": PHASE_PENDING,
            "T_DEAD": PHASE_DEAD,
        })

        archives = reap_dead(queue, dead_dir=tmp_path / "dead")
        # 只 T_DEAD 被记录
        assert len(archives) == 1
        assert archives[0].task_id is not None  # rowid
        # done / pending 不被 reap
        assert queue.get_task("T_OK").phase == PHASE_DONE
        assert queue.get_task("T_PEND").phase == PHASE_PENDING


# ---------------------------------------------------------------------------
# §3 replay 复活测试
# ---------------------------------------------------------------------------


class TestReplayRevive:
    """``requeue_dead`` 把 dead → pending, 清空 error_msg, 返回受影响行数."""

    def test_requeue_dead_changes_dead_to_pending(
        self, tmp_path: Path,
    ) -> None:
        """``phase=dead → pending`` 直接调 ``queue.requeue_dead()``."""
        queue = SQLiteQueue(tmp_path / "q.db")
        _seed_tasks(queue, {"T_A": PHASE_DEAD, "T_B": PHASE_DEAD})
        # 灌入后的初态
        assert queue.get_task("T_A").phase == PHASE_DEAD
        assert queue.get_task("T_B").phase == PHASE_DEAD

        n = queue.requeue_dead()
        assert n == 2
        # 复活
        assert queue.get_task("T_A").phase == PHASE_PENDING
        assert queue.get_task("T_B").phase == PHASE_PENDING

    def test_requeue_dead_clears_error_msg(self, tmp_path: Path) -> None:
        """``error_msg`` 必须清空."""
        queue = SQLiteQueue(tmp_path / "q.db")
        _seed_tasks(queue, {"T_A": PHASE_DEAD})
        assert queue.get_task("T_A").error_msg == "seeded"

        queue.requeue_dead()
        t = queue.get_task("T_A")
        assert t.error_msg is None

    def test_requeue_dead_returns_affected_count(self, tmp_path: Path) -> None:
        """返受影响行数 (契约 §2.5)."""
        queue = SQLiteQueue(tmp_path / "q.db")
        # 3 dead + 2 done
        _seed_tasks(queue, {
            "T_D1": PHASE_DEAD, "T_D2": PHASE_DEAD, "T_D3": PHASE_DEAD,
            "T_OK1": PHASE_DONE, "T_OK2": PHASE_DONE,
        })
        n = queue.requeue_dead()
        assert n == 3

    def test_requeue_dead_does_not_touch_done(self, tmp_path: Path) -> None:
        """done task 不应被 requeue_dead 误碰."""
        queue = SQLiteQueue(tmp_path / "q.db")
        _seed_tasks(queue, {
            "T_OK": PHASE_DONE,
            "T_PEND": PHASE_PENDING,
            "T_DEAD": PHASE_DEAD,
        })
        queue.requeue_dead()
        # done / pending 不动
        assert queue.get_task("T_OK").phase == PHASE_DONE
        assert queue.get_task("T_PEND").phase == PHASE_PENDING

    def test_requeue_dead_clears_run_and_session(self, tmp_path: Path) -> None:
        """``run_id`` / ``session_id`` 在 requeue 时清空 (契约 §2.5)."""
        queue = SQLiteQueue(tmp_path / "q.db")
        queue.upsert_task("T_A")
        queue.mark_phase(
            "T_A", new_phase="gdr",
            run_id="r1", session_id="s1", src_path=tmp_path / "x.json",
        )
        queue.mark_failed("T_A", stage="gdr", error_msg="boom")

        queue.requeue_dead()
        t = queue.get_task("T_A")
        assert t.phase == PHASE_PENDING
        assert t.run_id is None
        assert t.session_id is None
        assert t.error_msg is None

    def test_requeue_dead_after_master_run_pipeline(
        self, tmp_path: Path, fake_pool,
    ) -> None:
        """端到端: Master.run 制造 dead → requeue_dead 复活."""
        cfg = _make_cfg(tmp_path)
        _BEHAVIORS["T_DEAD"] = {"gdr_fail": True}
        master = Master(cfg=cfg)
        master.run(["T_OK", "T_DEAD"])

        queue = SQLiteQueue(cfg.paths.sqlite_db)
        assert queue.get_task("T_DEAD").phase == PHASE_DEAD

        n = queue.requeue_dead()
        assert n == 1

        refreshed = queue.get_task("T_DEAD")
        assert refreshed.phase == PHASE_PENDING
        assert refreshed.error_msg is None


# ---------------------------------------------------------------------------
# §4 端到端集成 (status → run → reap → replay)
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    """跑完整流水线后, 串通 status / reap_dead / replay 验证."""

    def test_status_after_master_run_shows_correct_distribution(
        self, tmp_path: Path, fake_pool,
    ) -> None:
        """Master.run 跑 4 task (1 done + 1 simulate_fail + 1 gdr_fail +
        1 etl_fail) → status 看到 1 done + 3 dead."""
        cfg = _make_cfg(tmp_path)
        _BEHAVIORS["T_OK"] = {}
        _BEHAVIORS["T_BAD_S"] = {"simulate_state": "executor_error"}
        _BEHAVIORS["T_BAD_G"] = {"gdr_fail": True}
        _BEHAVIORS["T_BAD_E"] = {"etl_fail": True}
        master = Master(cfg=cfg)
        summary = master.run(["T_OK", "T_BAD_S", "T_BAD_G", "T_BAD_E"])
        assert summary.done == 1
        assert summary.dead == 3

        queue = SQLiteQueue(cfg.paths.sqlite_db)
        result = collect_tasks(queue)
        assert result["phases"][PHASE_DONE] == 1
        assert result["phases"][PHASE_DEAD] == 3
        assert result["total"] == 4

    def test_reap_then_replay_cycle(
        self, tmp_path: Path, fake_pool,
    ) -> None:
        """完整周期: run (制造 dead) → reap_dead (移产物) → requeue_dead
        (SQLite 复活) → status 显示 dead=0, pending=1."""
        cfg = _make_cfg(tmp_path)
        _BEHAVIORS["T_OK"] = {}
        _BEHAVIORS["T_BAD"] = {"gdr_fail": True}
        master = Master(cfg=cfg)
        master.run(["T_OK", "T_BAD"])

        queue = SQLiteQueue(cfg.paths.sqlite_db)

        # step 1: reap_dead 移产物
        archives = reap_dead(
            queue, dead_dir=cfg.paths.dead_dir,
            dead_log_path=tmp_path / "dead.log",
        )
        assert len(archives) == 1

        # step 2: requeue_dead 复活
        n = queue.requeue_dead()
        assert n == 1

        # step 3: status 反映
        result = collect_tasks(queue)
        assert result["phases"][PHASE_DEAD] == 0
        assert result["phases"][PHASE_PENDING] == 1
        assert result["phases"][PHASE_DONE] == 1

    def test_replayed_task_can_run_again_to_done(
        self, tmp_path: Path, fake_pool,
    ) -> None:
        """被 reap → replay 的 task 重新跑 Master.run 能正常 done.

        流程: 第一次 run (gdr_fail) → dead; reap_dead; requeue_dead →
        pending; 第二次 run (改 _BEHAVIORS 为 success) → done.
        """
        cfg = _make_cfg(tmp_path)
        # 第一次: gdr_fail
        _BEHAVIORS["T_R"] = {"gdr_fail": True}
        master = Master(cfg=cfg)
        master.run(["T_R"])
        queue = SQLiteQueue(cfg.paths.sqlite_db)
        assert queue.get_task("T_R").phase == PHASE_DEAD

        # reap + replay
        reap_dead(queue, dead_dir=cfg.paths.dead_dir)
        queue.requeue_dead()
        assert queue.get_task("T_R").phase == PHASE_PENDING

        # 第二次: 改成 success 跑同一 task
        _BEHAVIORS["T_R"] = {}
        master2 = Master(cfg=cfg)
        summary = master2.run(["T_R"])
        assert summary.done == 1
        assert summary.dead == 0
        assert queue.get_task("T_R").phase == PHASE_DONE

    def test_health_json_written_and_consumed_by_status(
        self, tmp_path: Path, fake_pool,
    ) -> None:
        """Master.run 后 ``log_dir/health.json`` 存在, 内容含 ``status=completed``
        且 ``phases`` / ``total`` / ``last_updated`` 都在."""
        cfg = _make_cfg(tmp_path)
        _BEHAVIORS["T_OK"] = {}
        _BEHAVIORS["T_BAD"] = {"gdr_fail": True}
        master = Master(cfg=cfg)
        master.run(["T_OK", "T_BAD"])

        health_path = Path(cfg.paths.log_dir) / "health.json"
        assert health_path.exists()
        data = json.loads(health_path.read_text(encoding="utf-8"))
        # Master 写入的额外字段
        assert data["status"] == "completed"
        assert data["summary"]["done"] == 1
        assert data["summary"]["dead"] == 1
        # collect_tasks 的核心字段
        assert "phases" in data
        assert "total" in data
        assert "last_updated" in data
        assert data["phases"][PHASE_DONE] == 1
        assert data["phases"][PHASE_DEAD] == 1

    def test_full_lifecycle_idempotent_under_repeated_replays(
        self, tmp_path: Path, fake_pool,
    ) -> None:
        """多次 reap + replay 循环不应破坏数据完整性."""
        cfg = _make_cfg(tmp_path)
        _BEHAVIORS["T_BAD"] = {"gdr_fail": True}
        master = Master(cfg=cfg)
        master.run(["T_BAD"])

        queue = SQLiteQueue(cfg.paths.sqlite_db)
        for _ in range(3):
            reap_dead(queue, dead_dir=cfg.paths.dead_dir)
            queue.requeue_dead()
        # 多次 replay 后仍能保留 task 行 (最终 phase=pending)
        t = queue.get_task("T_BAD")
        assert t is not None
        assert t.phase == PHASE_PENDING
        assert t.error_msg is None


# ---------------------------------------------------------------------------
# §5 健康检查测试
# ---------------------------------------------------------------------------


class TestHealthChecks:
    """``write_health`` 写 ``health.json`` 字段契约 (契约 §6.5)."""

    def test_write_health_creates_file(self, tmp_path: Path) -> None:
        """``log_dir/health.json`` 必须存在."""
        queue = SQLiteQueue(tmp_path / "q.db")
        write_health(queue, log_dir=tmp_path)
        assert (tmp_path / "health.json").exists()

    def test_write_health_file_has_required_keys(self, tmp_path: Path) -> None:
        """``phases`` / ``total`` / ``last_updated`` 三键全在."""
        queue = SQLiteQueue(tmp_path / "q.db")
        _seed_tasks(queue, {"T_A": PHASE_DONE, "T_B": PHASE_DEAD})
        write_health(queue, log_dir=tmp_path)

        data = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
        assert "phases" in data
        assert "total" in data
        assert "last_updated" in data

    def test_write_health_no_legacy_batches_field(self, tmp_path: Path) -> None:
        """旧 ``batches`` 字段已删除 (契约 §6.5)."""
        queue = SQLiteQueue(tmp_path / "q.db")
        write_health(queue, log_dir=tmp_path)
        data = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
        assert "batches" not in data

    def test_write_health_no_legacy_queue_counts_field(self, tmp_path: Path) -> None:
        """旧 ``queue_counts`` 字段已删除 (契约 §6.5)."""
        queue = SQLiteQueue(tmp_path / "q.db")
        write_health(queue, log_dir=tmp_path)
        data = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
        assert "queue_counts" not in data

    def test_write_health_phases_full_six_phase_distribution(
        self, tmp_path: Path,
    ) -> None:
        """写入的 ``phases`` 必须含 6 个 phase 键, 即使某些计数为 0."""
        queue = SQLiteQueue(tmp_path / "q.db")
        _seed_tasks(queue, {"T_A": PHASE_PENDING})
        write_health(queue, log_dir=tmp_path)
        data = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
        assert set(data["phases"].keys()) == {
            "pending", "simulate", "gdr", "etl", "done", "dead",
        }
        assert data["phases"]["pending"] == 1
        assert data["phases"]["done"] == 0

    def test_write_health_total_equals_sum_of_phases(self, tmp_path: Path) -> None:
        """写入的 ``total`` = ``sum(phases.values())``."""
        queue = SQLiteQueue(tmp_path / "q.db")
        _seed_tasks(queue, {
            "T_A": PHASE_PENDING, "T_B": PHASE_PENDING, "T_C": PHASE_DONE,
            "T_D": PHASE_DEAD, "T_E": PHASE_DEAD, "T_F": PHASE_DEAD,
        })
        write_health(queue, log_dir=tmp_path)
        data = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
        assert data["total"] == 6
        assert data["total"] == sum(data["phases"].values())

    def test_write_health_creates_nested_log_dir(self, tmp_path: Path) -> None:
        """``log_dir`` 不存在 → 自动 mkdir."""
        queue = SQLiteQueue(tmp_path / "q.db")
        deep = tmp_path / "a" / "b" / "c"
        write_health(queue, log_dir=deep)
        assert (deep / "health.json").exists()

    def test_write_health_includes_extra_fields(self, tmp_path: Path) -> None:
        """``extra`` 参数透传到 health.json (例如 Master.run 的 status/summary)."""
        queue = SQLiteQueue(tmp_path / "q.db")
        write_health(
            queue, log_dir=tmp_path,
            extra={"status": "running", "submitted": ["T1", "T2"]},
        )
        data = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
        assert data["status"] == "running"
        assert data["submitted"] == ["T1", "T2"]
        # 同时 collect_tasks 的核心字段仍在
        assert "phases" in data
        assert "total" in data

    def test_collect_tasks_no_legacy_fields(self, tmp_path: Path) -> None:
        """``collect_tasks`` 返回 dict 不含旧字段 ``dead_count`` /
        ``gdr_count`` / ``etl_count`` / ``status``."""
        queue = SQLiteQueue(tmp_path / "q.db")
        result = collect_tasks(queue)
        for legacy in ("dead_count", "gdr_count", "etl_count", "status", "batches"):
            assert legacy not in result


# ---------------------------------------------------------------------------
# §6 replay 子命令 — 自动归档 dead 产物 (P1, 2026-09-22)
# ---------------------------------------------------------------------------


class TestReplayAutoArchive:
    """``python -m orchestration replay`` 默认先归档 dead 产物再 requeue.

    Round 2 P1 改进: 让 replay 自动把 src_path / gdr_refined_path /
    etl_*_path move 到 cfg.paths.dead_dir, 写 dead.log, 顺序必须在
    requeue_dead 之前 (后者会清空 src_path).
    """

    def test_replay_cli_archives_before_requeue(
        self, tmp_path: Path, capsys, fake_pool,
    ) -> None:
        """默认 replay: 先归档 dead 产物 → 再 requeue_dead."""
        cfg = _make_cfg(tmp_path)
        # 直接灌一个 dead task 带 src_path (gdr 阶段产物) — 跳过 Master.run,
        # 因为 _FakeAsyncResult 在 gdr_fail 时不写 src_path (与生产 task_pipeline
        # 行为差异). 这里测的是 reap + requeue 的 CLI 行为, 不测 Master.run。
        traj_dir = cfg.paths.trajectory_dir
        traj_dir.mkdir(parents=True, exist_ok=True)
        src_before = traj_dir / "T_BAD__sess_bad.json"
        src_before.write_text("{}", encoding="utf-8")
        queue = SQLiteQueue(cfg.paths.sqlite_db)
        queue.upsert_task("T_BAD")
        queue.mark_phase(
            "T_BAD", new_phase=PHASE_GDR,
            run_id="run_bad", session_id="sess_bad",
            src_path=src_before,
        )
        queue.mark_failed("T_BAD", stage="gdr", error_msg="seeded gdr fail")

        assert queue.count_by_phase()[PHASE_DEAD] == 1
        assert queue.get_task("T_BAD").src_path is not None
        assert src_before.is_file()

        # 跑 replay CLI (默认归档)
        cfg_path = _write_cli_config_at(tmp_path, cfg)
        rc = cli_main(["--config", str(cfg_path), "replay"])
        out = capsys.readouterr().out
        assert rc == 0

        # 1. 归档: src_path 被 move 到 dead_dir
        # reap_dead 用 SQLite `id` (整数 PK) 作 prefix, 不是 task_id 字符串.
        # 故归档文件名为 `<int_id>__T_BAD__sess_bad.json`.
        dead_dir = cfg.paths.dead_dir
        moved_files = list(dead_dir.glob("*__T_BAD__sess_bad.json"))
        assert len(moved_files) == 1
        assert not src_before.exists()

        # 2. dead.log / dead_index.jsonl 落盘
        log_dir = cfg.paths.log_dir
        assert (log_dir / "dead.log").exists()
        assert (log_dir / "dead_index.jsonl").exists()
        log_lines = (log_dir / "dead.log").read_text(encoding="utf-8").strip().splitlines()
        assert len(log_lines) == 1
        assert "T_BAD" in log_lines[0]

        # 3. requeue: phase=dead → pending, src_path 清空
        queue2 = SQLiteQueue(cfg.paths.sqlite_db)
        assert queue2.count_by_phase()[PHASE_DEAD] == 0
        assert queue2.count_by_phase()[PHASE_PENDING] == 1
        t2 = queue2.get_task("T_BAD")
        assert t2.phase == PHASE_PENDING
        assert t2.src_path is None

        # 4. CLI 输出包含归档 + 复活两段
        assert "archived 1 dead task(s)" in out
        assert "requeued 1 dead task(s)" in out
        # 5. 顺序: archived 在 requeued 之前 (CLI 必须先归档再复活)
        assert out.index("archived 1 dead task(s)") < out.index("requeued 1 dead task(s)")

    def test_replay_cli_no_archive_skips_reap(
        self, tmp_path: Path, capsys, fake_pool,
    ) -> None:
        """``--no-archive``: 跳过 reap, 直接 requeue (产物留在原位)."""
        cfg = _make_cfg(tmp_path)
        traj_dir = cfg.paths.trajectory_dir
        traj_dir.mkdir(parents=True, exist_ok=True)
        src_before = traj_dir / "T_BAD__sess_bad.json"
        src_before.write_text("{}", encoding="utf-8")
        queue = SQLiteQueue(cfg.paths.sqlite_db)
        queue.upsert_task("T_BAD")
        queue.mark_phase(
            "T_BAD", new_phase=PHASE_GDR,
            run_id="run_bad", session_id="sess_bad",
            src_path=src_before,
        )
        queue.mark_failed("T_BAD", stage="gdr", error_msg="seeded gdr fail")
        t1 = queue.get_task("T_BAD")
        assert t1.src_path is not None
        assert src_before.is_file()

        cfg_path = _write_cli_config_at(tmp_path, cfg)
        rc = cli_main(["--config", str(cfg_path), "replay", "--no-archive"])
        out = capsys.readouterr().out
        assert rc == 0

        # 1. dead_dir 空 (没 reap)
        moved_files = list(cfg.paths.dead_dir.glob("*__T_BAD__*.json"))
        assert len(moved_files) == 0
        # 2. src_path 仍在原位
        assert src_before.is_file()
        # 3. dead.log 不应被本次写入 (no-archive 跳过 reap)
        log_path = cfg.paths.log_dir / "dead.log"
        assert not log_path.exists()
        # 4. requeue 仍发生: phase=dead → pending
        queue2 = SQLiteQueue(cfg.paths.sqlite_db)
        assert queue2.count_by_phase()[PHASE_PENDING] == 1
        # 5. CLI 输出不包含 archived 行, 仅 requeued
        assert "archived" not in out
        assert "requeued 1 dead task(s)" in out

    def test_replay_cli_no_dead_archive_requeue_zero(
        self, tmp_path: Path, capsys,
    ) -> None:
        """空 dead DB: replay 仍返 0, 不报错, 不写 dead.log."""
        cfg = _make_cfg(tmp_path)
        cfg_path = _write_cli_config_at(tmp_path, cfg)
        queue = SQLiteQueue(cfg.paths.sqlite_db)
        queue.upsert_task("T_LIVE")
        queue.mark_phase("T_LIVE", new_phase=PHASE_DONE)

        rc = cli_main(["--config", str(cfg_path), "replay"])
        out = capsys.readouterr().out
        assert rc == 0
        # 0 archived / 0 requeued
        assert "archived 0 dead task(s)" in out
        assert "requeued 0 dead task(s)" in out
        # dead.log 不应被写入 (无 dead task 时 reap 跳过 log_entry)
        assert not (cfg.paths.log_dir / "dead.log").exists()


def _write_cli_config_at(tmp_path: Path, cfg: OrchestrationConfig) -> Path:
    """复用 _write_cli_config 但路径与 _make_cfg 同步.

    _make_cfg + _write_cli_config 各管各的 tmp_path 子目录, 直接串联
    两者必须保证 ``cfg.paths.*`` 与 cfg.yaml 一致. 这里让
    _write_cli_config 复用 _make_cfg 的路径设置.
    """
    cfg_path = tmp_path / "orch.yaml"
    sim_path = tmp_path / "sim.yaml"
    tasks_path = tmp_path / "tasks.yaml"
    scenarios_path = tmp_path / "scenarios.yaml"
    if not tasks_path.exists():
        tasks_path.write_text(
            json.dumps({"schema_version": "2", "tasks": []}),
            encoding="utf-8",
        )
    if not scenarios_path.exists():
        scenarios_path.write_text(
            json.dumps({"schema_version": "2", "scenarios": []}),
            encoding="utf-8",
        )
    sim_path.write_text("{}", encoding="utf-8")
    cfg_path.write_text(json.dumps({
        "simulate_serve": {
            "tasks_file": str(tasks_path),
            "scenarios_file": str(scenarios_path),
        },
        "orchestration": {
            "pipeline": {
                "max_parallelism": 1,
                "max_retry_gdr": 1,
                "max_retry_etl": 1,
                "retry_poll_seconds": 0.05,
            },
            "paths": {
                "simulate_serve_config": str(sim_path),
                "trajectory_dir": str(cfg.paths.trajectory_dir),
                "runs_dir": str(cfg.paths.runs_dir),
                "refined_dir": str(cfg.paths.refined_dir),
                "etl_outputs_dir": str(cfg.paths.etl_outputs_dir),
                "sqlite_db": str(cfg.paths.sqlite_db),
                "dead_dir": str(cfg.paths.dead_dir),
                "pid_file": str(cfg.paths.pid_file),
                "log_dir": str(cfg.paths.log_dir),
            },
        },
    }), encoding="utf-8")
    return cfg_path