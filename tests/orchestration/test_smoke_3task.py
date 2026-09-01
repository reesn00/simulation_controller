"""3 task 批次端到端 smoke test.

模拟 ``docs/orchestration-design.md`` §10 的 smoke 场景：
    * 3 task 一批跑通 producer → qf → gdr
    * batches 表 ``status='done'`` 且 ``dead_count=0``
    * 3 份 refined JSON 落盘
    * health.json 写入
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestration.config_loader import OrchestrationConfig
from orchestration.master import Master
from orchestration.queue import (
    STATE_DONE,
    SQLiteQueue,
)
from orchestration.workers.gdr_worker import GdrWorker
from orchestration.workers.qf_worker import QfWorker
from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.state_machine import RunState


def _patch_qf(monkeypatch):
    def fake(self, task):
        session = task.session_id or task.src_path.stem
        out = self._qf_output_dir / f"{session}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"ok": True}), encoding="utf-8")
        self._queue.mark_qf_done(task.id, qf_output_path=out)
        return out
    monkeypatch.setattr(QfWorker, "process", fake)


def _patch_gdr(monkeypatch):
    def fake(self, task):
        session = task.session_id or task.src_path.stem
        out = self._gdr_output_dir / f"{session}_refined.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"refined": True}), encoding="utf-8")
        self._queue.mark_gdr_done(task.id, gdr_output_path=out)
        return out
    monkeypatch.setattr(GdrWorker, "process", fake)


def test_smoke_3task_batch_end_to_end(tmp_path, monkeypatch) -> None:
    queue = SQLiteQueue(tmp_path / "q.db", max_retry_qf=2, max_retry_gdr=2)
    traj_dir = tmp_path / "traj"
    runs_dir = tmp_path / "runs"
    qf_out = tmp_path / "qf_out"
    gdr_out = tmp_path / "gdr_out"
    dead_dir = tmp_path / "dead"
    log_dir = tmp_path / "logs"
    for d in (traj_dir, runs_dir, qf_out, gdr_out, dead_dir, log_dir):
        d.mkdir()
    config_path = tmp_path / "sim.yaml"
    config_path.write_text("{}", encoding="utf-8")

    cfg = OrchestrationConfig.from_raw({
        "orchestration": {
            "batch_size": 3,
            "qf_workers": 2,
            "gdr_workers": 2,
            "max_retry_qf": 2,
            "max_retry_gdr": 2,
            "watcher_poll_seconds": 0.02,
            "reap_stale_interval_seconds": 60,
            "reap_stale_seconds": 60,
            "batch_drain_poll_seconds": 0.02,
            "batch_drain_timeout_seconds": 8.0,
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

    _patch_qf(monkeypatch)
    _patch_gdr(monkeypatch)

    def fake_producer(*, config_path, task_ids, limit, queue):
        bid = queue.insert_batch(task_ids)
        queue.update_batch(bid, simulate_started_at="2026-09-01T00:00:00Z")
        runs = []
        for tid in task_ids[:limit]:
            run_id = tid
            tr = TaskRun(run_id=run_id, task_id=tid, task_type="test",
                         state=RunState.SUCCESS)
            runs.append(tr)
            (runs_dir / run_id).mkdir(parents=True, exist_ok=True)
            (runs_dir / run_id / "run.json").write_text(
                json.dumps({"run_id": run_id, "state": "success"}), encoding="utf-8",
            )
            traj = traj_dir / f"{run_id}__session_{tid}.json"
            traj.write_text(json.dumps({"session_id": f"session_{tid}",
                                        "messages": []}), encoding="utf-8")
            queue.insert(src_path=traj, run_id=run_id,
                         session_id=f"session_{tid}", batch_id=bid)
        queue.update_batch(bid, simulate_done_at="2026-09-01T00:00:01Z")
        return bid, runs

    m = Master(cfg=cfg, queue=queue)
    m._producer_runner = fake_producer
    try:
        summaries = m.run([["T1", "T2", "T3"]])
    finally:
        m.shutdown(timeout=2.0)

    assert len(summaries) == 1
    s = summaries[0]
    assert s.batch_id > 0
    assert s.drained is True
    assert s.dead_count == 0

    # 3 个 task 全 done
    tasks = queue.list_tasks_for_batch(s.batch_id)
    assert len(tasks) == 3
    assert all(t.state == STATE_DONE for t in tasks)
    assert {t.run_id for t in tasks} == {"T1", "T2", "T3"}

    # batches 表 status='done'
    with queue._conn() as conn:
        row = conn.execute(
            "SELECT status, dead_count FROM batches WHERE id = ?",
            (s.batch_id,),
        ).fetchone()
    assert row["status"] == "done"
    assert int(row["dead_count"]) == 0

    # 3 份 refined JSON 落盘
    refined = sorted(p.name for p in gdr_out.glob("*_refined.json"))
    assert refined == [
        "session_T1_refined.json", "session_T2_refined.json", "session_T3_refined.json",
    ]

    # health.json 写入
    health_path = log_dir / "health.json"
    assert health_path.exists()
    health = json.loads(health_path.read_text(encoding="utf-8"))
    assert "queue_counts" in health
    assert "batches" in health
    assert health["queue_counts"].get("done") == 3
    assert health["queue_counts"].get("dead", 0) == 0


def test_smoke_multi_batch_sequential(tmp_path, monkeypatch) -> None:
    """3 个连续批次，每批 1 task."""
    queue = SQLiteQueue(tmp_path / "q.db", max_retry_qf=2, max_retry_gdr=2)
    traj_dir = tmp_path / "traj"
    runs_dir = tmp_path / "runs"
    qf_out = tmp_path / "qf_out"
    gdr_out = tmp_path / "gdr_out"
    dead_dir = tmp_path / "dead"
    log_dir = tmp_path / "logs"
    for d in (traj_dir, runs_dir, qf_out, gdr_out, dead_dir, log_dir):
        d.mkdir()
    config_path = tmp_path / "sim.yaml"
    config_path.write_text("{}", encoding="utf-8")

    cfg = OrchestrationConfig.from_raw({
        "orchestration": {
            "batch_size": 1,
            "qf_workers": 1,
            "gdr_workers": 1,
            "max_retry_qf": 2,
            "max_retry_gdr": 2,
            "watcher_poll_seconds": 0.02,
            "reap_stale_interval_seconds": 60,
            "reap_stale_seconds": 60,
            "batch_drain_poll_seconds": 0.02,
            "batch_drain_timeout_seconds": 8.0,
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

    _patch_qf(monkeypatch)
    _patch_gdr(monkeypatch)

    def fake_producer(*, config_path, task_ids, limit, queue):
        bid = queue.insert_batch(task_ids)
        queue.update_batch(bid, simulate_started_at="2026-09-01T00:00:00Z")
        runs = []
        for tid in task_ids[:limit]:
            run_id = tid
            tr = TaskRun(run_id=run_id, task_id=tid, task_type="test",
                         state=RunState.SUCCESS)
            runs.append(tr)
            (runs_dir / run_id).mkdir(parents=True, exist_ok=True)
            (runs_dir / run_id / "run.json").write_text(
                json.dumps({"run_id": run_id, "state": "success"}), encoding="utf-8",
            )
            traj = traj_dir / f"{run_id}__session_{tid}.json"
            traj.write_text("{}", encoding="utf-8")
            queue.insert(src_path=traj, run_id=run_id,
                         session_id=f"session_{tid}", batch_id=bid)
        queue.update_batch(bid, simulate_done_at="2026-09-01T00:00:01Z")
        return bid, runs

    m = Master(cfg=cfg, queue=queue)
    m._producer_runner = fake_producer
    try:
        summaries = m.run([["T1"], ["T2"], ["T3"]])
    finally:
        m.shutdown(timeout=2.0)

    assert len(summaries) == 3
    assert all(s.drained for s in summaries)
    assert all(s.dead_count == 0 for s in summaries)

    # 3 个 batch 全 status='done'
    with queue._conn() as conn:
        rows = conn.execute(
            "SELECT id, status FROM batches ORDER BY id",
        ).fetchall()
    assert [r["status"] for r in rows] == ["done", "done", "done"]
