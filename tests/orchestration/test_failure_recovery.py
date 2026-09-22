"""失败注入 / 崩溃恢复测试.

覆盖 ``docs/orchestration-design.md`` §10：

* 失败注入：mock gdr 抛异常 → attempts_gdr 累加 → 第 max+1 次入 dead；产物被 reap_dead 移到 dead_dir
* 崩溃恢复：worker 拿到锁后挂掉，reap_stale 把 ``*_processing`` 退回 pending / pending_etl
* 凑批：单个 task 时 gdr worker 不阻塞，按 1 个处理
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from orchestration.config_loader import OrchestrationConfig
from orchestration.master import Master
from orchestration.queue import (
    STATE_DEAD,
    STATE_DONE,
    STATE_PENDING,
    STATE_PENDING_ETL,
    SQLiteQueue,
)
from orchestration.workers.etl_worker import EtlWorker
from orchestration.workers.gdr_worker import GdrWorker
from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.state_machine import RunState


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    queue = SQLiteQueue(tmp_path / "q.db", max_retry_gdr=2, max_retry_etl=2)
    paths = {
        "simulate_serve_config": str(tmp_path / "sim.yaml"),
        "trajectory_dir": str(tmp_path / "traj"),
        "refined_dir": str(tmp_path / "refined"),
        "etl_outputs_dir": str(tmp_path / "etl_outputs"),
        "sqlite_db": str(tmp_path / "q.db"),
        "dead_dir": str(tmp_path / "dead"),
        "log_dir": str(tmp_path / "logs"),
        "runs_dir": str(tmp_path / "runs"),
    }
    for sub in (paths["trajectory_dir"], paths["runs_dir"], paths["refined_dir"],
                paths["etl_outputs_dir"], paths["dead_dir"], paths["log_dir"]):
        Path(sub).mkdir(parents=True, exist_ok=True)
    (tmp_path / "sim.yaml").write_text("{}", encoding="utf-8")

    cfg = OrchestrationConfig.from_raw({
        "orchestration": {
            "batch_size": 3,
            "etl_workers": 1,
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


@pytest.fixture
def register_batches():
    """工厂: 拿 master 后注册一批 batch_id, 让 worker 能拉到对应 task.
    修复方向 B 后, 直接 ``start_workers()`` 不会自动加入活跃集合 (避免
    跨 batch 偷拉), 测试需要显式注册."""
    def _reg(m: Master, *batch_ids: int) -> None:
        for bid in batch_ids:
            m.register_active_batch(bid)
    return _reg


def _patch_gdr(monkeypatch, fail_for: set[str] | None = None):
    """让 GdrWorker.process 立即返回 (不实际跑 LLM); 只看 pull 行为."""
    fail_for = fail_for or set()

    def fake(self, task):
        if task.run_id in fail_for:
            raise RuntimeError("gdr injected failure")
        session = task.session_id or task.src_path.stem
        out = self._refined_dir / f"{session}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            '{"session_id":"' + session + '","messages":[],"schema_version":"refined_session.v1"}',
            encoding="utf-8",
        )
        self._queue.mark_gdr_done(task.id, gdr_refined_path=out)
        return out

    monkeypatch.setattr(GdrWorker, "process", fake)


def _patch_etl(monkeypatch):
    """让 EtlWorker.process 立即返回 (不实际拆 4 视图); 只看 pull 行为."""

    def fake(self, task):
        c2 = Path(task.gdr_refined_path)
        base = self._outputs_dir / c2.stem
        paths = {
            "messages": base.with_suffix(".messages.json"),
            "openai": base.with_suffix(".openai.json"),
            "qwenjina": base.with_suffix(".qwenjina.txt"),
            "meta": base.with_suffix(".meta.json"),
        }
        for p in paths.values():
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            Path(p).write_text("{}", encoding="utf-8")
        from types import SimpleNamespace
        self._last_outputs = SimpleNamespace(**paths)
        return Path(paths["messages"])

    monkeypatch.setattr(EtlWorker, "process", fake)


def _seed_gdr_done(tmp_path: Path, queue: SQLiteQueue, run_id: str,
                   session_id: str, batch_id: int = 1) -> int:
    """登记 task, 模拟 gdr 已完成 → state=pending_etl."""
    src = tmp_path / "traj" / f"{run_id}__{session_id}.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("{}", encoding="utf-8")
    c2 = tmp_path / "refined" / f"{session_id}.json"
    c2.parent.mkdir(parents=True, exist_ok=True)
    c2.write_text(
        '{"session_id":"' + session_id + '","messages":[],"schema_version":"refined_session.v1"}',
        encoding="utf-8",
    )
    tid, _ = queue.insert(src_path=src, run_id=run_id,
                          session_id=session_id, batch_id=batch_id)
    queue.pull_pending_gdr(worker_id="seed", n=1)
    queue.mark_gdr_done(tid, gdr_refined_path=c2)
    return tid


# ---------------------------------------------------------------------------
# 失败注入：gdr 多次失败 → dead
# ---------------------------------------------------------------------------

def test_failure_injection_gdr_reaches_dead(env, monkeypatch, register_batches) -> None:
    """gdr 一直抛异常 → attempts_gdr 累加 → 第 max+1 次入 dead."""
    _tmp, queue, m, _paths = env
    _patch_gdr(monkeypatch, fail_for={"T_BAD"})
    _patch_etl(monkeypatch)

    # T_BAD 是 pending, T_OK 也是 pending; gdr worker 拉两个都失败; T_BAD 进 dead, T_OK 完成
    src_bad = _tmp / "traj" / "T_BAD__sess_BAD.json"
    src_bad.parent.mkdir(parents=True, exist_ok=True)
    src_bad.write_text("{}", encoding="utf-8")
    queue.insert(src_path=src_bad, run_id="T_BAD", session_id="sess_BAD", batch_id=1)

    src_ok = _tmp / "traj" / "T_OK__sess_OK.json"
    src_ok.write_text("{}", encoding="utf-8")
    queue.insert(src_path=src_ok, run_id="T_OK", session_id="sess_OK", batch_id=1)

    register_batches(m, 1)

    m.start_workers()
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


def test_failure_injection_dead_archived(env, monkeypatch, register_batches) -> None:
    """dead 任务被 reap_dead 移动到 dead_dir."""
    _tmp, queue, m, paths = env
    _patch_gdr(monkeypatch, fail_for={"T_X"})
    _patch_etl(monkeypatch)

    src = _tmp / "traj" / "T_X__sess_X.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("{}", encoding="utf-8")
    queue.insert(src_path=src, run_id="T_X", session_id="sess_X", batch_id=7)
    register_batches(m, 7)

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

def test_recovery_reaper_unlocks_stale_gdr_processing(env, monkeypatch) -> None:
    """模拟 gdr worker 拿锁后挂掉：task 卡在 gdr_processing；reaper 把它退回 pending.

    本测试只验证 reaper 行为 (gdr_processing → pending). 让 GdrWorker.run_once
    空转, 避免 reaper 解锁后 worker 立即重新拉到任务、再次失败、最终
    进 dead —— 让测试焦点保持在 reaper 上, 不受 gdr 处理语义牵连.
    """
    _tmp, queue, m, _ = env
    # 隔离 gdr worker: 不让 run_once 真的处理任何任务
    monkeypatch.setattr(GdrWorker, "run_once", lambda self: 0)
    src = _tmp / "traj" / "r__s.json"
    src.write_text("{}", encoding="utf-8")
    tid, _ = queue.insert(src_path=src, run_id="r", session_id="s", batch_id=1)
    # 模拟 gdr worker 拿锁后崩溃：把 locked_at 设为很久以前
    queue.pull_pending_gdr(worker_id="dead_worker", n=1)
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


def test_recovery_reaper_unlocks_stale_etl_processing(env, monkeypatch) -> None:
    """模拟 etl worker 拿锁后挂掉：task 卡在 etl_processing；reaper 把它退回 pending_etl."""
    _tmp, queue, m, _ = env
    # 隔离 etl worker: 不让 run_once 真的抢回 pending_etl 并处理
    monkeypatch.setattr(EtlWorker, "run_once", lambda self: 0)
    tid = _seed_gdr_done(_tmp, queue, "r", "s", batch_id=1)
    # 让 task 进入 etl_processing (模拟 etl worker 拿锁后崩溃)
    queue.pull_pending_etl(worker_id="dead_etl", n=1)
    with queue._conn() as conn:
        conn.execute(
            "UPDATE tasks SET locked_at = '2000-01-01T00:00:00.000000Z' WHERE id = ?",
            (tid,),
        )
    object.__setattr__(m._cfg.settings, "reap_stale_seconds", 1)
    m.start_workers()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if queue.get(tid).state == STATE_PENDING_ETL:
            break
        time.sleep(0.05)
    m.shutdown(timeout=2.0)
    assert queue.get(tid).state == STATE_PENDING_ETL


# ---------------------------------------------------------------------------
# 凑批：单 task 时不阻塞，按 1 个处理
# ---------------------------------------------------------------------------

def test_batch_drain_single_task_does_not_block(env, monkeypatch, register_batches) -> None:
    """单 task 时 batch_drain 等 worker 处理完，不阻塞."""
    _tmp, queue, m, _ = env
    _patch_gdr(monkeypatch)
    _patch_etl(monkeypatch)
    src = _tmp / "traj" / "lonely__sess_lonely.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("{}", encoding="utf-8")
    queue.insert(src_path=src, run_id="lonely", session_id="sess_lonely", batch_id=1)
    register_batches(m, 1)

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
    _patch_gdr(monkeypatch, fail_for={"T_BAD"})
    _patch_etl(monkeypatch)
    traj_dir = Path(paths["trajectory_dir"])
    runs_dir = Path(paths["runs_dir"])
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
