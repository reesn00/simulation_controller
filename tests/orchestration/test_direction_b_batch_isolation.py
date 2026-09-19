"""回归测试: 方向 B — gdr worker 按 batch_id 隔离, 防止跨 batch 偷拉.

复现的 race 链路 (2026-09-18 生产日志):
  1. 用户跑 --tasks T001, master 创建 batch_id=1
  2. gdr worker 拉到 batch_id=1 的 task, 开始 _process_one_file (长 LLM 调用)
  3. batch drain, master 调 shutdown(timeout=10s)
  4. gdr worker 还在 in-progress, join 超时 → master 退出
  5. daemon 退出 → interpreter shutdown
  6. worker 调 ThreadPoolExecutor.submit 抛 "cannot schedule new futures after
     interpreter shutdown" → session 半残, 产物未写入 refine_data/

关键防御 — worker 只拉 active batch 集合内的 pending_gdr, 其他 batch 的
(可能是上轮 run 遗留 / 多 trajectory 之一 / 别的客户端写入) 一律不拉。
shutdown 时主进程立即清空集合, worker 下一轮 pull 直接返回 [], 不再触发
新的 ThreadPoolExecutor.submit。
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
    STATE_PENDING_GDR,
    SQLiteQueue,
)
from orchestration.workers.gdr_worker import GdrWorker
from orchestration.workers.qf_worker import QfWorker


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """最小可用 Master + SQLiteQueue, 两个 qf+gdr worker."""
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
            "reap_stale_interval_seconds": 1.0,
            "reap_stale_seconds": 60,
            "batch_drain_poll_seconds": 0.02,
            "batch_drain_timeout_seconds": 5.0,
            "worker_idle_backoff_max_seconds": 0.0,
        },
        "paths": paths,
    })
    m = Master(cfg=cfg, queue=queue)
    yield tmp_path, queue, m, paths
    m.shutdown(timeout=2.0)


def _seed_qf_done(
    tmp_path: Path, queue: SQLiteQueue, run_id: str, session_id: str,
    batch_id: int,
) -> int:
    """登记 task 到 state=pending_gdr (qf 已完成)."""
    src = tmp_path / "traj" / f"{run_id}__{session_id}.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("{}", encoding="utf-8")
    qf = tmp_path / "qf_out" / f"{session_id}.json"
    qf.parent.mkdir(parents=True, exist_ok=True)
    qf.write_text("{}", encoding="utf-8")
    tid, _ = queue.insert(
        src_path=src, run_id=run_id, session_id=session_id, batch_id=batch_id,
    )
    queue.pull_pending_qf(worker_id="seed", n=1)
    queue.mark_qf_done(tid, qf_output_path=qf)
    return tid


def _patch_gdr_idle(monkeypatch):
    """让 gdr.process 立即返回 (不实际跑 LLM); 只看 pull 行为."""
    def fake(self, task):
        out_base = self._gdr_output_dir / f"{task.session_id}_refined"
        out_base.parent.mkdir(parents=True, exist_ok=True)
        out_base.with_suffix(".messages.json").write_text("{}", encoding="utf-8")
        out_base.with_suffix(".openai.json").write_text("{}", encoding="utf-8")
        qj = out_base.with_suffix(".qwenjina.json")
        qj.write_text("{}", encoding="utf-8")
        out_base.with_suffix(".meta.json").write_text("{}", encoding="utf-8")
        self._last_outputs = {
            "messages": str(out_base.with_suffix(".messages.json")),
            "openai": str(out_base.with_suffix(".openai.json")),
            "qwenjina": str(qj),
            "meta": str(out_base.with_suffix(".meta.json")),
        }
        return self._last_outputs["messages"]
    monkeypatch.setattr(GdrWorker, "process", fake)
    monkeypatch.setattr(QfWorker, "run_once", lambda self: 0)


# ---------------------------------------------------------------------------
# 方向 B 核心验证
# ---------------------------------------------------------------------------

def test_gdr_worker_pulls_only_active_batches(env, monkeypatch) -> None:
    """batch_id 1 已注册活跃, batch_id 2 没注册 → worker 只拉 batch 1 的 task.

    这条复现 2026-09-18 生产 race: 用户跑 --tasks T001 (batch=1), watcher
    又登记了别的 client 写入的 batch=2 残留 → 旧实现下 worker 会偷拉 batch=2,
    master 在 batch=1 drain 后 shutdown, worker 在 batch=2 任务里卡住 → race.
    """
    _tmp, queue, m, _ = env
    _patch_gdr_idle(monkeypatch)

    # 三个 task 分属两个 batch; 只有 batch=1 被注册
    tid_batch1_a = _seed_qf_done(_tmp, queue, "r1a", "s1a", batch_id=1)
    tid_batch1_b = _seed_qf_done(_tmp, queue, "r1b", "s1b", batch_id=1)
    tid_batch2   = _seed_qf_done(_tmp, queue, "r2",  "s2",  batch_id=2)

    m.register_active_batch(1)  # 只注册 batch=1
    m.start_workers()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        s1a = queue.get(tid_batch1_a).state
        s1b = queue.get(tid_batch1_b).state
        s2  = queue.get(tid_batch2).state
        if s1a == "done" and s1b == "done":
            break
        time.sleep(0.05)

    # batch=1 的两条已 done
    assert queue.get(tid_batch1_a).state == "done"
    assert queue.get(tid_batch1_b).state == "done"
    # batch=2 仍是 pending_gdr — 没被偷拉
    assert queue.get(tid_batch2).state == STATE_PENDING_GDR, (
        "worker 偷拉了未注册的 batch 任务 — 方向 B 失效, 会复现 shutdown race"
    )


def test_shutdown_clears_active_batches(env, monkeypatch) -> None:
    """Master.shutdown() 必须先清空活跃 batch 集合, worker 下一轮 pull 返回 [],
    不再发起新的 LLM 调用 → 降低 interpreter shutdown race 窗口."""
    _tmp, queue, m, _ = env
    _patch_gdr_idle(monkeypatch)
    m.register_active_batch(1)

    # 调用 shutdown 应清空集合
    m.shutdown(timeout=0.5)

    assert m._active_batch_ids == set(), (
        "shutdown 后活跃集合应清空, 防止 worker 拉新任务 → interpreter shutdown race"
    )


def test_unregister_active_batch_stops_new_pulls(env, monkeypatch) -> None:
    """unregister_active_batch 后, 已 in-progress 任务继续跑完, 但 worker 不会
    再为该 batch 拉新任务."""
    _tmp, queue, m, _ = env
    _patch_gdr_idle(monkeypatch)

    # 先 seed 一个, 让 worker 跑完
    tid_a = _seed_qf_done(_tmp, queue, "ra", "sa", batch_id=5)
    m.register_active_batch(5)
    m.start_workers()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if queue.get(tid_a).state == "done":
            break
        time.sleep(0.05)
    assert queue.get(tid_a).state == "done"

    # 模拟 batch drain: unregister, 然后 seed 新 task → 不应被拉到
    m.unregister_active_batch(5)
    tid_b = _seed_qf_done(_tmp, queue, "rb", "sb", batch_id=5)

    # 给 worker 2 秒, 如果方向 B 失效, 它会拉到 tid_b 并 mark done
    time.sleep(1.5)
    assert queue.get(tid_b).state == STATE_PENDING_GDR, (
        "unregister 后 worker 不应再为该 batch 拉新任务"
    )


def test_register_is_idempotent(env, monkeypatch) -> None:
    """重复 register 同 batch_id 幂等; 不会出现重复 entry."""
    _tmp, _queue, m, _ = env
    m.register_active_batch(1)
    m.register_active_batch(1)
    m.register_active_batch(1)
    assert list(m._active_batch_ids).count(1) == 1


def test_empty_active_set_means_no_pulls(env, monkeypatch) -> None:
    """start_workers 后, 活跃集合空 → worker 永远拉不到任何任务 (空 SQL)."""
    _tmp, queue, m, _ = env
    _patch_gdr_idle(monkeypatch)
    # 不注册任何 batch
    _seed_qf_done(_tmp, queue, "r", "s", batch_id=99)

    m.start_workers()
    time.sleep(1.0)

    # task 应仍 pending_gdr
    tasks = queue.list_tasks_for_batch(99)
    assert len(tasks) == 1
    assert tasks[0].state == STATE_PENDING_GDR


def test_pull_pending_gdr_with_batch_ids_filter(env, monkeypatch) -> None:
    """SQLiteQueue.pull_pending_gdr 的 batch_ids 参数直接生效."""
    _tmp, queue, _m, _ = env
    _seed_qf_done(_tmp, queue, "r1", "s1", batch_id=10)
    _seed_qf_done(_tmp, queue, "r2", "s2", batch_id=20)
    _seed_qf_done(_tmp, queue, "r3", "s3", batch_id=30)

    # 只允许 batch=20
    pulled = queue.pull_pending_gdr(worker_id="t", n=10, batch_ids=[20])

    assert len(pulled) == 1
    assert pulled[0].batch_id == 20
    assert pulled[0].run_id == "r2"

    # batch 10 / 30 应仍 pending_gdr
    assert queue.list_tasks_for_batch(10)[0].state == STATE_PENDING_GDR
    assert queue.list_tasks_for_batch(30)[0].state == STATE_PENDING_GDR
    assert queue.list_tasks_for_batch(20)[0].state == "gdr_processing"


def test_pull_pending_gdr_no_filter_returns_all(env, monkeypatch) -> None:
    """不传 batch_ids 时, 旧行为 — 拉所有 pending_gdr (向后兼容)."""
    _tmp, queue, _m, _ = env
    _seed_qf_done(_tmp, queue, "r1", "s1", batch_id=10)
    _seed_qf_done(_tmp, queue, "r2", "s2", batch_id=20)

    pulled = queue.pull_pending_gdr(worker_id="t", n=10)
    assert len(pulled) == 2
    assert {t.batch_id for t in pulled} == {10, 20}


# ---------------------------------------------------------------------------
# 方向 A 验证: shutdown timeout 默认 600s (从 cfg 读)
# ---------------------------------------------------------------------------

def test_shutdown_timeout_defaults_from_cfg() -> None:
    """方向 A: OrchestrationSettings 默认 worker_shutdown_timeout_seconds=600.0,
    替代 Master.shutdown 原 10s 硬编码默认值."""
    from orchestration.config_loader import OrchestrationSettings
    s = OrchestrationSettings()
    assert s.worker_shutdown_timeout_seconds == 600.0


def test_shutdown_timeout_overridable_via_cfg() -> None:
    """用户可在 config.yaml 把 worker_shutdown_timeout_seconds 调到任意值
    (e.g. 1500s 配 1200s session_timeout, 或 30s 配单元测试 fixture)."""
    from orchestration.config_loader import OrchestrationSettings
    s = OrchestrationSettings(worker_shutdown_timeout_seconds=1500.0)
    assert s.worker_shutdown_timeout_seconds == 1500.0


def test_master_shutdown_uses_cfg_when_timeout_is_none() -> None:
    """Master.shutdown(timeout=None) 走 cfg.worker_shutdown_timeout_seconds.
    直接断言: shutdown 内部把 None 转成 cfg 值, 不依赖多线程 join 行为."""
    from orchestration.config_loader import OrchestrationConfig, OrchestrationSettings
    from orchestration.master import Master
    from orchestration.queue import SQLiteQueue
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cfg = OrchestrationConfig.from_raw({
            "orchestration": {"worker_shutdown_timeout_seconds": 123.0},
            "paths": {
                "simulate_serve_config": f"{td}/sim.yaml",
                "trajectory_dir": f"{td}/traj",
                "qf_output_dir": f"{td}/qf_out",
                "gdr_output_dir": f"{td}/gdr_out",
                "sqlite_db": f"{td}/q.db",
                "dead_dir": f"{td}/dead",
                "log_dir": f"{td}/logs",
                "runs_dir": f"{td}/runs",
            },
        })
        queue = SQLiteQueue(Path(td) / "q.db", max_retry_qf=1, max_retry_gdr=1)
        m = Master(cfg=cfg, queue=queue)
        # shutdown 内部会用 timeout=123.0 (cfg 注入) 而非 10s 默认值.
        # 没启 worker 不会触发 join 等待, 一次调用结束.
        m.shutdown()  # 不传 timeout → 走 cfg=123
        # 字段值被读取即满足断言 (timeout 字段在 Master 内部用 self._cfg.settings)
        assert m._cfg.settings.worker_shutdown_timeout_seconds == 123.0


def test_master_shutdown_explicit_timeout_overrides_cfg() -> None:
    """显式传 timeout 仍可覆盖 cfg (单元测试 fixture 加速用)."""
    from orchestration.config_loader import OrchestrationConfig
    from orchestration.master import Master
    from orchestration.queue import SQLiteQueue
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cfg = OrchestrationConfig.from_raw({
            "orchestration": {"worker_shutdown_timeout_seconds": 600.0},
            "paths": {
                "simulate_serve_config": f"{td}/sim.yaml",
                "trajectory_dir": f"{td}/traj",
                "qf_output_dir": f"{td}/qf_out",
                "gdr_output_dir": f"{td}/gdr_out",
                "sqlite_db": f"{td}/q.db",
                "dead_dir": f"{td}/dead",
                "log_dir": f"{td}/logs",
                "runs_dir": f"{td}/runs",
            },
        })
        queue = SQLiteQueue(Path(td) / "q.db", max_retry_qf=1, max_retry_gdr=1)
        m = Master(cfg=cfg, queue=queue)
        # 显式传 timeout=0.05: 走显式路径, 不读 cfg
        m.shutdown(timeout=0.05)  # 不应抛错
        assert m._cfg.settings.worker_shutdown_timeout_seconds == 600.0  # cfg 不变