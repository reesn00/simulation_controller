"""失败注入 / 崩溃恢复测试.

覆盖 ``docs/orchestration-design.md`` §10：

* 失败注入：mock gdr 抛异常 → attempts_gdr 累加 → 第 max+1 次入 dead；产物被 reap_dead 移到 dead_dir
* 崩溃恢复：worker 拿到锁后挂掉，reap_stale 把 ``*_processing`` 退回 pending / pending_gdr
* 凑批：单个 task 时 gdr worker 不阻塞，按 1 个处理
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from orchestration.config_loader import OrchestrationConfig
from orchestration.master import Master
from orchestration.queue import (
    STATE_DEAD,
    STATE_DONE,
    STATE_PENDING,
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
def env(tmp_path: Path, monkeypatch):
    queue = SQLiteQueue(tmp_path / "q.db", max_retry_qf=2, max_retry_gdr=2)
    paths = {
        "simulate_serve_config": str(tmp_path / "sim.yaml"),
        "trajectory_dir": str(tmp_path / "traj"),
        "qf_output_dir": str(tmp_path / "qf_out"),
        "gdr_output_dir": str(tmp_path / "gdr_out"),
        "sqlite_db": str(tmp_path / "q.db"),
        "dead_dir": str(tmp_path / "dead"),
        "log_dir": str(tmp_path / "logs"),
        "runs_dir": str(tmp_path / "runs"),
    }
    for sub in (paths["trajectory_dir"], paths["runs_dir"], paths["qf_output_dir"],
                paths["gdr_output_dir"], paths["dead_dir"], paths["log_dir"]):
        Path(sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "sim.yaml").write_text("{}", encoding="utf-8")

    cfg = OrchestrationConfig.from_raw({
        "orchestration": {
            "batch_size": 3,
            "qf_workers": 1,
            "gdr_workers": 1,
            "watcher_poll_seconds": 0.02,
            "reap_stale_interval_seconds": 0.1,
            "reap_stale_seconds": 0,
            "batch_drain_poll_seconds": 0.02,
            "batch_drain_timeout_seconds": 5.0,
        },
        "paths": paths,
    })
    m = Master(cfg=cfg, queue=queue)
    yield tmp_path, queue, m, paths
    m.shutdown(timeout=2.0)


def _patch_qf(monkeypatch):
    def fake(self, task):
        session = task.session_id or task.src_path.stem
        out = self._qf_output_dir / f"{session}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("{}", encoding="utf-8")
        self._queue.mark_qf_done(task.id, qf_output_path=out)
        return out
    monkeypatch.setattr(QfWorker, "process", fake)


def _patch_gdr(monkeypatch, fail_for: set[str] | None = None):
    fail_for = fail_for or set()
    def fake(self, task):
        if task.run_id in fail_for:
            raise RuntimeError("gdr injected failure")
        session = task.session_id or task.src_path.stem
        out = self._gdr_output_dir / f"{session}_refined.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("{}", encoding="utf-8")
        self._queue.mark_gdr_done(task.id, gdr_output_path=out)
        return out
    monkeypatch.setattr(GdrWorker, "process", fake)


def _seed_qf_done(tmp_path: Path, queue: SQLiteQueue, run_id: str,
                  session_id: str, batch_id: int = 1) -> int:
    """登记 task 到 state=pending_gdr（qf 已完成）."""
    src = tmp_path / "traj" / f"{run_id}__{session_id}.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("{}", encoding="utf-8")
    qf = tmp_path / "qf_out" / f"{session_id}.json"
    qf.parent.mkdir(parents=True, exist_ok=True)
    qf.write_text("{}", encoding="utf-8")
    tid, _ = queue.insert(src_path=src, run_id=run_id,
                         session_id=session_id, batch_id=batch_id)
    queue.pull_pending_qf(worker_id="seed", n=1)
    queue.mark_qf_done(tid, qf_output_path=qf)
    return tid


# ---------------------------------------------------------------------------
# 失败注入：gdr 多次失败 → dead
# ---------------------------------------------------------------------------

def test_failure_injection_gdr_reaches_dead(env, monkeypatch) -> None:
    """gdr 一直抛异常 → attempts_gdr 累加 → 第 max+1 次入 dead."""
    _tmp, queue, m, _paths = env
    _patch_qf(monkeypatch)
    _patch_gdr(monkeypatch, fail_for={"T_BAD"})
    _seed_qf_done(_tmp, queue, "T_BAD", "sess_BAD")
    _seed_qf_done(_tmp, queue, "T_OK", "sess_OK")

    m.start_workers()
    # 等到 T_BAD 入 dead + T_OK 完成
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        tasks = queue.list_tasks_for_batch(1)
        states = {t.run_id: t.state for t in tasks}
        if states.get("T_BAD") == STATE_DEAD and states.get("T_OK") == STATE_DONE:
            break
        time.sleep(0.05)
    m.shutdown(timeout=2.0)

    states = {t.run_id: t.state for t in queue.list_tasks_for_batch(1)}
    assert states["T_OK"] == STATE_DONE
    assert states["T_BAD"] == STATE_DEAD
    # attempts 超限（3 > max_retry_gdr=2）
    bad_task = next(t for t in queue.list_tasks_for_batch(1) if t.run_id == "T_BAD")
    assert bad_task.attempts_gdr >= 3


def test_failure_injection_dead_archived(env, monkeypatch) -> None:
    """dead 任务被 reap_dead 移动到 dead_dir."""
    _tmp, queue, m, paths = env
    _patch_qf(monkeypatch)
    _patch_gdr(monkeypatch, fail_for={"T_X"})
    _seed_qf_done(_tmp, queue, "T_X", "sess_X", batch_id=7)

    m.start_workers()
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if queue.list_tasks_for_batch(7)[0].state == STATE_DEAD:
            break
        time.sleep(0.05)
    m.shutdown(timeout=2.0)

    # 手动调一次 reap_dead（master 主循环是 batch 触发；这里直接验证）
    from orchestration.failure_handler import reap_dead
    archives = reap_dead(queue, dead_dir=Path(paths["dead_dir"]),
                         dead_log_path=Path(paths["log_dir"]) / "dead.log")
    moved = [a for a in archives if a.moved_to]
    assert len(moved) >= 1
    dead_dir = Path(paths["dead_dir"])
    assert any(p.is_file() for p in dead_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# 崩溃恢复：reap_stale 退回 processing 锁
# ---------------------------------------------------------------------------

def test_recovery_reaper_unlocks_stale_qf_processing(env, monkeypatch) -> None:
    """模拟一个 qf worker 拿锁后挂掉：task 卡在 qf_processing；reaper 把它退回 pending."""
    _tmp, queue, m, _ = env
    src = _tmp / "traj" / "r__s.json"
    src.write_text("{}", encoding="utf-8")
    tid, _ = queue.insert(src_path=src, run_id="r", session_id="s", batch_id=1)
    # 模拟 qf worker 拿锁后崩溃：把 locked_at 设为很久以前
    queue.pull_pending_qf(worker_id="dead_worker", n=1)
    with queue._conn() as conn:
        conn.execute(
            "UPDATE tasks SET locked_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
            (tid,),
        )
    # 改 reap_stale_seconds = 1；reap_stale_interval 已设 0.1
    object.__setattr__(m._cfg.settings, "reap_stale_seconds", 1)
    m.start_workers()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        refreshed = queue.get(tid)
        if refreshed.state == STATE_PENDING:
            break
        time.sleep(0.05)
    m.shutdown(timeout=2.0)
    refreshed = queue.get(tid)
    assert refreshed.state == STATE_PENDING, f"still {refreshed.state}"


def test_recovery_reaper_unlocks_stale_gdr_processing(env, monkeypatch) -> None:
    _tmp, queue, m, _ = env
    tid = _seed_qf_done(_tmp, queue, "r", "s", batch_id=1)
    queue.pull_pending_gdr(worker_id="dead_gdr", n=1)
    with queue._conn() as conn:
        conn.execute(
            "UPDATE tasks SET locked_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
            (tid,),
        )
    object.__setattr__(m._cfg.settings, "reap_stale_seconds", 1)
    m.start_workers()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if queue.get(tid).state == STATE_PENDING_GDR:
            break
        time.sleep(0.05)
    m.shutdown(timeout=2.0)
    assert queue.get(tid).state == STATE_PENDING_GDR


# ---------------------------------------------------------------------------
# 凑批：单 task 时不阻塞，按 1 个处理
# ---------------------------------------------------------------------------

def test_batch_drain_single_task_does_not_block(env, monkeypatch) -> None:
    """单 task 时 batch_drain 等 worker 处理完，不阻塞."""
    _tmp, queue, m, _ = env
    _patch_qf(monkeypatch)
    _patch_gdr(monkeypatch)
    _seed_qf_done(_tmp, queue, "lonely", "sess_lonely", batch_id=1)

    m.start_workers()
    drained = m.wait_batch_drained(1, poll_seconds=0.05, timeout=4.0)
    m.shutdown(timeout=2.0)
    assert drained is True
    states = {t.run_id: t.state for t in queue.list_tasks_for_batch(1)}
    assert states["lonely"] == STATE_DONE


# ---------------------------------------------------------------------------
# 集成：失败注入走完整 master 批次循环
# ---------------------------------------------------------------------------

def test_master_run_batch_with_gdr_failure(env, monkeypatch) -> None:
    """一个 batch 里混合 done + dead：master._run_one_batch 仍能跑完."""
    _tmp, queue, m, paths = env
    _patch_qf(monkeypatch)
    _patch_gdr(monkeypatch, fail_for={"T_BAD"})
    traj_dir = Path(paths["trajectory_dir"])
    runs_dir = Path(paths["runs_dir"])
    (tmp_path := _tmp / "_placeholder").parent  # noqa
    traj_dir.mkdir(exist_ok=True)
    runs_dir.mkdir(exist_ok=True)

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
            traj = traj_dir / f"{run_id}__sess_{tid}.json"
            traj.write_text("{}", encoding="utf-8")
            queue.insert(src_path=traj, run_id=run_id,
                         session_id=f"sess_{tid}", batch_id=bid)
        queue.update_batch(bid, simulate_done_at="2026-09-01T00:00:01Z")
        return bid, runs
    m._producer_runner = fake_producer

    summaries = m.run([["T_OK", "T_BAD"]])
    assert len(summaries) == 1
    s = summaries[0]
    assert s.dead_count == 1

    states = {t.run_id: t.state for t in queue.list_tasks_for_batch(s.batch_id)}
    assert states["T_OK"] == STATE_DONE
    assert states["T_BAD"] == STATE_DEAD
