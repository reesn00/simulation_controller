"""orchestration.master 单元测试.

通过 monkeypatch ``QfWorker.process`` / ``GdrWorker.process`` 避免真实 LLM 调用;
producer 注入 fake 让 batch_id 走完整路径.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Sequence

import pytest

# 显式 import，确保 QfWorker/GdrWorker 在 monkeypatch 之前已注册
from orchestration.config_loader import OrchestrationConfig
from orchestration.master import Master
from orchestration.queue import (
    STATE_DEAD,
    STATE_DONE,
    STATE_PENDING_GDR,
    SQLiteQueue,
)
from orchestration.workers.gdr_worker import GdrWorker
from orchestration.workers.qf_worker import QfWorker
from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.state_machine import RunState


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> tuple[Path, SQLiteQueue, Master]:
    queue = SQLiteQueue(tmp_path / "q.db")
    traj_dir = tmp_path / "traj"
    traj_dir.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    qf_out = tmp_path / "qf_out"
    gdr_out = tmp_path / "gdr_out"
    dead_dir = tmp_path / "dead"
    log_dir = tmp_path / "logs"
    config_path = tmp_path / "sim.yaml"
    config_path.write_text("{}", encoding="utf-8")

    cfg = OrchestrationConfig.from_raw({
        "orchestration": {
            "batch_size": 3,
            "qf_workers": 1,
            "gdr_workers": 1,
            "max_retry_qf": 1,
            "max_retry_gdr": 1,
            "watcher_poll_seconds": 0.02,
            "reap_stale_interval_seconds": 60,
            "reap_stale_seconds": 60,
            "batch_drain_poll_seconds": 0.02,
            "batch_drain_timeout_seconds": 5.0,
        },
        "paths": {
            "simulate_serve_config": str(config_path),
            "trajectory_dir": str(traj_dir),
            "qf_output_dir": str(qf_out),
            "gdr_output_dir": str(gdr_out),
            "sqlite_db": str(tmp_path / "q.db"),
            "dead_dir": str(dead_dir),
            "log_dir": str(log_dir),
            "runs_dir": str(runs_dir),
        },
    })

    queue = SQLiteQueue(
        tmp_path / "q.db",
        max_retry_qf=1,
        max_retry_gdr=1,
    )
    m = Master(cfg=cfg, queue=queue)
    yield tmp_path, queue, m
    m.shutdown(timeout=2.0)


def _make_fake_producer(
    queue: SQLiteQueue,
    traj_dir: Path,
    runs_dir: Path,
    trajectory_files: dict[str, str] | None = None,
):
    """返回一个 fake producer_runner: 写 SQLite batches + 落地 run.json + 放 trajectory + 预占位 tasks.

    约定：run_id == task_id（简化测试语义）。

    注意：fake 预占位 tasks（state=pending, src_path=trajectory 路径）让
    ``wait_batch_drained`` 能在 watcher 实际登记前看到正确数量的 task；
    watcher 后续 scan 同样 src_path 时 INSERT UNIQUE 冲突 → skip.
    """
    def producer(*, config_path, task_ids, limit, queue):
        bid = queue.insert_batch(task_ids)
        queue.update_batch(bid, simulate_started_at="2026-09-01T00:00:00Z")
        runs: list[TaskRun] = []
        for tid in task_ids[:limit]:
            run_id = tid
            tr = TaskRun(run_id=run_id, task_id=tid, task_type="test",
                         state=RunState.SUCCESS)
            runs.append(tr)
            rd = runs_dir / run_id
            rd.mkdir(parents=True, exist_ok=True)
            (rd / "run.json").write_text(
                json.dumps({"run_id": run_id, "state": "success"}), encoding="utf-8",
            )
            session = (trajectory_files or {}).get(tid, f"session_{tid}")
            traj_path = traj_dir / f"{run_id}__{session}.json"
            traj_path.write_text("{}", encoding="utf-8")
            # 预占位：让 wait_batch_drained 立刻看到 N 条 pending task
            queue.insert(src_path=traj_path, run_id=run_id,
                         session_id=session, batch_id=bid)
        queue.update_batch(bid, simulate_done_at="2026-09-01T00:00:01Z")
        return bid, runs

    return producer


def _patch_qf_process(monkeypatch, fail_for: set[str] | None = None):
    """monkeypatch QfWorker.process."""
    fail_for = fail_for or set()

    def fake_process(self, task):
        if task.run_id in fail_for:
            raise RuntimeError("qf forced fail")
        session = task.session_id or task.src_path.stem
        out = self._qf_output_dir / f"{session}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("{}", encoding="utf-8")
        self._queue.mark_qf_done(task.id, qf_output_path=out)
        return out

    monkeypatch.setattr(QfWorker, "process", fake_process)


def _patch_gdr_process(monkeypatch):
    def fake_process(self, task):
        session = task.session_id or task.src_path.stem
        out = self._gdr_output_dir / f"{session}_refined.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("{}", encoding="utf-8")
        self._queue.mark_gdr_done(task.id, gdr_output_path=out)
        return out

    monkeypatch.setattr(GdrWorker, "process", fake_process)


# ---------------------------------------------------------------------------
# start_workers / shutdown
# ---------------------------------------------------------------------------

def test_start_workers_spawns_threads(env, monkeypatch) -> None:
    _tmp, queue, m = env
    _patch_qf_process(monkeypatch)
    _patch_gdr_process(monkeypatch)
    m.start_workers()
    assert len(m.alive_workers) == 2
    assert any(name == "qf_0" for name in m.alive_workers)
    assert any(name == "gdr_0" for name in m.alive_workers)
    with pytest.raises(Exception):
        m.start_workers()


def test_shutdown_stops_all_workers(env, monkeypatch) -> None:
    _tmp, queue, m = env
    _patch_qf_process(monkeypatch)
    _patch_gdr_process(monkeypatch)
    m.start_workers()
    assert len(m.alive_workers) == 2
    m.shutdown(timeout=2.0)
    assert all(not t.is_alive() for _n, t, _ev in m._threads)


# ---------------------------------------------------------------------------
# 主循环：单批 / 多批
# ---------------------------------------------------------------------------

def test_run_one_batch_end_to_end(env, monkeypatch) -> None:
    _tmp, queue, m = env
    _patch_qf_process(monkeypatch)
    _patch_gdr_process(monkeypatch)

    traj_dir = Path(m._cfg.paths.trajectory_dir)
    runs_dir = Path(m._cfg.paths.runs_dir)
    producer = _make_fake_producer(queue, traj_dir, runs_dir,
                                   {"T1": "sess_T1", "T2": "sess_T2"})
    m._producer_runner = producer

    summaries = m.run([["T1", "T2"]])
    assert len(summaries) == 1
    s = summaries[0]
    assert s.batch_id > 0
    assert set(s.run_ids) == {"T1", "T2"}
    assert s.drained is True
    assert s.dead_count == 0

    tasks = queue.list_tasks_for_batch(s.batch_id)
    assert len(tasks) == 2, f"expected 2 tasks, got {len(tasks)}; counts={queue.count_by_state()}"
    assert {t.run_id for t in tasks} == {"T1", "T2"}
    states = {t.run_id: t.state for t in tasks}
    assert states == {"T1": STATE_DONE, "T2": STATE_DONE}, f"states={states}"


def test_run_multiple_batches(env, monkeypatch) -> None:
    _tmp, queue, m = env
    _patch_qf_process(monkeypatch)
    _patch_gdr_process(monkeypatch)
    traj_dir = Path(m._cfg.paths.trajectory_dir)
    runs_dir = Path(m._cfg.paths.runs_dir)
    producer = _make_fake_producer(queue, traj_dir, runs_dir)
    m._producer_runner = producer

    summaries = m.run([["T1"], ["T2"], ["T3"]])
    assert len(summaries) == 3
    assert summaries[0].batch_id != summaries[1].batch_id
    assert all(s.drained for s in summaries)
    assert all(len(queue.list_tasks_for_batch(s.batch_id)) == 1 for s in summaries)


# ---------------------------------------------------------------------------
# 主循环：含 dead
# ---------------------------------------------------------------------------

def test_run_one_batch_with_dead_task(env, monkeypatch) -> None:
    _tmp, queue, m = env
    _patch_gdr_process(monkeypatch)
    _patch_qf_process(monkeypatch, fail_for={"T_BAD"})
    traj_dir = Path(m._cfg.paths.trajectory_dir)
    runs_dir = Path(m._cfg.paths.runs_dir)
    producer = _make_fake_producer(queue, traj_dir, runs_dir,
                                   {"T_OK": "s1", "T_BAD": "s2"})
    m._producer_runner = producer

    summaries = m.run([["T_OK", "T_BAD"]])
    assert len(summaries) == 1
    s = summaries[0]
    states = {t.run_id: t.state for t in queue.list_tasks_for_batch(s.batch_id)}
    assert states["T_OK"] == STATE_DONE
    assert states["T_BAD"] == STATE_DEAD
    assert s.dead_count == 1
    dead_dir = Path(m._cfg.paths.dead_dir)
    moved = list(dead_dir.glob("*.json"))
    assert len(moved) >= 1
    assert any("T_BAD" in p.name or "s2" in p.name for p in moved)


# ---------------------------------------------------------------------------
# wait_batch_drained
# ---------------------------------------------------------------------------

def test_wait_batch_drained_returns_true_when_all_done(env) -> None:
    _tmp, queue, m = env
    fp = env[0] / "t.json"
    fp.write_text("{}", encoding="utf-8")
    tid, _ = queue.insert(src_path=fp, run_id="r", session_id="s", batch_id=1)
    queue.pull_pending_qf(worker_id="w", n=1)
    queue.mark_qf_done(tid, qf_output_path=fp)
    queue.pull_pending_gdr(worker_id="w", n=1)
    queue.mark_gdr_done(tid, gdr_output_path=fp)

    bid = queue.insert_batch(["r"])
    assert m.wait_batch_drained(bid, poll_seconds=0.05) is True


def test_wait_batch_drained_timeout_when_stuck(env) -> None:
    _tmp, queue, m = env
    fp = env[0] / "t.json"
    fp.write_text("{}", encoding="utf-8")
    queue.insert(src_path=fp, run_id="r", session_id="s", batch_id=1)
    bid = queue.insert_batch(["r"])
    assert m.wait_batch_drained(bid, poll_seconds=0.05, timeout=0.2) is False


def test_wait_batch_drained_empty_batch(env) -> None:
    _tmp, queue, m = env
    bid = queue.insert_batch(["nobody"])
    assert m.wait_batch_drained(bid, poll_seconds=0.05) is True


def test_wait_batch_drained_returns_false_on_stop(env) -> None:
    """stop_event 置位 → 不再等 timeout，立即返回 False."""
    _tmp, queue, m = env
    fp = env[0] / "t.json"
    fp.write_text("{}", encoding="utf-8")
    queue.insert(src_path=fp, run_id="r", session_id="s", batch_id=1)
    bid = queue.insert_batch(["r"])
    m._stop_event.set()
    start = time.monotonic()
    assert m.wait_batch_drained(bid, poll_seconds=0.05, timeout=60.0) is False
    assert time.monotonic() - start < 1.0


# ---------------------------------------------------------------------------
# 停止请求中断批循环
# ---------------------------------------------------------------------------

def test_run_stops_mid_batch_via_batch_tracker(env, monkeypatch) -> None:
    """run.json 永不终态 + stop_event 置位 → BatchTrackerStopped 传播，
    批循环中断，后续批不跑."""
    _tmp, queue, m = env
    _patch_qf_process(monkeypatch)
    _patch_gdr_process(monkeypatch)

    traj_dir = Path(m._cfg.paths.trajectory_dir)
    runs_dir = Path(m._cfg.paths.runs_dir)

    def producer(*, config_path, task_ids, limit, queue):
        bid = queue.insert_batch(task_ids)
        # 写 trajectory 但**不写 run.json**：wait_for_terminal 会卡住
        # （run_id 非空才会真的进入轮询循环）
        runs = []
        for tid in task_ids:
            (traj_dir / f"{tid}__sess.json").write_text("{}", encoding="utf-8")
            runs.append(TaskRun(run_id=tid, task_id=tid, task_type="test",
                                state=RunState.VALIDATING))
        return bid, runs

    m._producer_runner = producer

    def stopper():
        time.sleep(0.2)
        m._stop_event.set()

    threading.Thread(target=stopper, daemon=True).start()
    start = time.monotonic()
    summaries = m.run([["T1"], ["T2"]])
    # T1 批未走完（被停止打断），T2 批不该开跑
    assert summaries == []
    assert time.monotonic() - start < 5.0


def test_run_completed_batches_not_affected_by_stop(env, monkeypatch) -> None:
    """停止发生在批间：已完成批的 summary 保留."""
    _tmp, queue, m = env
    _patch_qf_process(monkeypatch)
    _patch_gdr_process(monkeypatch)

    traj_dir = Path(m._cfg.paths.trajectory_dir)
    runs_dir = Path(m._cfg.paths.runs_dir)
    producer = _make_fake_producer(queue, traj_dir, runs_dir)
    m._producer_runner = producer

    real_one_batch = m._run_one_batch
    calls = {"n": 0}

    def counted_one_batch(task_ids):
        calls["n"] += 1
        result = real_one_batch(task_ids)
        if calls["n"] == 1:
            m._stop_event.set()  # 第一批完成后请求停止
        return result

    m._run_one_batch = counted_one_batch
    summaries = m.run([["T1"], ["T2"]])
    assert calls["n"] == 1
    assert len(summaries) == 1
    assert summaries[0].drained is True


# ---------------------------------------------------------------------------
# reap_stale 周期
# ---------------------------------------------------------------------------

def test_reaper_loop_calls_reap_stale(env, monkeypatch) -> None:
    _tmp, queue, m = env
    raw = m._cfg.settings
    object.__setattr__(raw, "reap_stale_interval_seconds", 0.1)
    object.__setattr__(raw, "reap_stale_seconds", 0)
    calls = {"n": 0}
    real = queue.reap_stale
    def spy(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)
    monkeypatch.setattr(queue, "reap_stale", spy)
    _patch_qf_process(monkeypatch)
    _patch_gdr_process(monkeypatch)
    m.start_workers()
    time.sleep(0.3)
    m.shutdown(timeout=1.0)
    assert calls["n"] >= 1
