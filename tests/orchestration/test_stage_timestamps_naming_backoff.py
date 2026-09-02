"""回归: #8 批阶段时间戳 / #9 产物文件名 task_id 前缀 / #7 轮询空闲退避与降噪.

#8: batches.qf/gdr_started_at 由首次 pull 打点 (COALESCE 不覆盖),
    *_done_at 由批内该阶段清空时打点; dead 收尾补戳; 未开始的阶段不打收尾戳;
    旧 db 自动迁移。
#9: producer 写 run_tasks 映射 (sanitize 后的 run_id 为键); worker 产物文件名
    ``{task_id}__{session}{suffix}.json``; 无映射回退旧命名。
#7: worker/watcher 空轮指数退避、有活复位; daemon.setup_logging 降噪 httpx。
"""

from __future__ import annotations

import json
import logging
import sqlite3

from orchestration.health import collect_batches
from orchestration.queue import SQLiteQueue
from orchestration.watcher import TrajectoryWatcher
from orchestration.workers.qf_worker import QfWorker
from orchestration.workers.gdr_worker import GdrWorker


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _trajectory(session_id: str = "sess-1") -> dict:
    return {
        "session_id": session_id,
        "summary": "test",
        "messages": [
            {"role": "user", "name": "user", "id": "u1",
             "blocks": [{"type": "text", "text": "hi"}], "metadata": {}},
            {"role": "assistant", "name": "Default", "id": "a1",
             "blocks": [
                 {"type": "thinking", "thinking": "think"},
                 {"type": "text", "text": "hello"},
             ], "metadata": {}},
        ],
    }


def _seed(queue: SQLiteQueue, tmp_path: Path, name: str, session_id: str,
          *, batch_id: int = 1, run_id: str = "r") -> int:
    fp = tmp_path / name
    fp.write_text(json.dumps(_trajectory(session_id), ensure_ascii=False), encoding="utf-8")
    tid, inserted = queue.insert(src_path=fp, run_id=run_id, session_id=session_id,
                                 batch_id=batch_id)
    assert inserted
    return tid


def _batch_row(queue: SQLiteQueue, batch_id: int) -> dict:
    return collect_batches(queue)[batch_id]


class _FakeEvent:
    """记录每轮 wait 时长；攒够 max_waits 轮后 is_set 变 True."""

    def __init__(self, max_waits: int) -> None:
        self.waits: list[float] = []
        self._max = max_waits

    def is_set(self) -> bool:
        return len(self.waits) >= self._max

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return False


# ---------------------------------------------------------------------------
# #8: 阶段时间戳全链路
# ---------------------------------------------------------------------------

def test_pull_stamps_stage_started_once_not_overwritten(tmp_path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    queue.insert_batch(["T1"])
    _seed(queue, tmp_path, "r__a.json", "a", batch_id=1)
    _seed(queue, tmp_path, "r__b.json", "b", batch_id=1)

    queue.pull_pending_qf(worker_id="w", n=1)
    first = _batch_row(queue, 1)["qf_started_at"]
    assert first is not None
    assert _batch_row(queue, 1)["gdr_started_at"] is None

    queue.pull_pending_qf(worker_id="w", n=1)
    assert _batch_row(queue, 1)["qf_started_at"] == first, "第二次 pull 不得覆盖首戳"


def test_stage_done_only_after_batch_drains_stage(tmp_path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    queue.insert_batch(["T1"])
    t1 = _seed(queue, tmp_path, "r__a.json", "a", batch_id=1)
    t2 = _seed(queue, tmp_path, "r__b.json", "b", batch_id=1)

    queue.pull_pending_qf(worker_id="w", n=2)
    queue.mark_qf_done(t1, qf_output_path=tmp_path / "a.json")
    assert _batch_row(queue, 1)["qf_done_at"] is None, "批内仍有 qf_processing 不得收尾"

    queue.mark_qf_done(t2, qf_output_path=tmp_path / "b.json")
    assert _batch_row(queue, 1)["qf_done_at"] is not None
    assert _batch_row(queue, 1)["gdr_done_at"] is None

    queue.pull_pending_gdr(worker_id="w", n=2)
    assert _batch_row(queue, 1)["gdr_started_at"] is not None
    queue.mark_gdr_done(t1, gdr_output_path=tmp_path / "a_r.json")
    assert _batch_row(queue, 1)["gdr_done_at"] is None
    queue.mark_gdr_done(t2, gdr_output_path=tmp_path / "b_r.json")
    row = _batch_row(queue, 1)
    assert row["gdr_done_at"] is not None
    assert row["qf_started_at"] <= row["qf_done_at"] <= row["gdr_started_at"] <= row["gdr_done_at"]


def test_dead_last_task_stamps_qf_done_but_not_gdr(tmp_path) -> None:
    """最后一条 task 死于 qf: qf_done_at 补戳; gdr 从未开始则不打凭空收尾戳."""
    queue = SQLiteQueue(tmp_path / "q.db", max_retry_qf=0)
    queue.insert_batch(["T1"])
    t1 = _seed(queue, tmp_path, "r__a.json", "a", batch_id=1)
    queue.pull_pending_qf(worker_id="w", n=1)
    new_state = queue.mark_failed(t1, stage="qf", error_msg="boom")
    assert new_state == "dead"
    row = _batch_row(queue, 1)
    assert row["qf_done_at"] is not None
    assert row["gdr_started_at"] is None
    assert row["gdr_done_at"] is None, "未开始的阶段不得有 done 戳"


def test_mark_dead_before_any_pull_stamps_nothing(tmp_path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    queue.insert_batch(["T1"])
    t1 = _seed(queue, tmp_path, "r__a.json", "a", batch_id=1)
    queue.mark_dead(t1, error_msg="watcher parse fail")
    row = _batch_row(queue, 1)
    assert row["qf_started_at"] is None and row["qf_done_at"] is None


def test_legacy_db_migrated_with_new_columns(tmp_path) -> None:
    """旧库 (batches 无阶段戳列) 打开时幂等 ALTER 补列, 且 collect/打标可用."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          src_path TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, session_id TEXT,
          batch_id INTEGER NOT NULL, state TEXT NOT NULL,
          attempts_qf INTEGER NOT NULL DEFAULT 0, attempts_gdr INTEGER NOT NULL DEFAULT 0,
          qf_output_path TEXT, gdr_output_path TEXT, error_msg TEXT,
          locked_by TEXT, locked_at TEXT,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
          updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE batches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_ids TEXT NOT NULL,
          simulate_started_at TEXT, simulate_done_at TEXT,
          qf_count INTEGER NOT NULL DEFAULT 0, gdr_count INTEGER NOT NULL DEFAULT 0,
          dead_count INTEGER NOT NULL DEFAULT 0, status TEXT
        );
    """)
    conn.commit()
    conn.close()

    queue = SQLiteQueue(db)
    queue.insert_batch(["T1"])
    t1 = _seed(queue, tmp_path, "r__a.json", "a", batch_id=1)
    queue.pull_pending_qf(worker_id="w", n=1)
    assert _batch_row(queue, 1)["qf_started_at"] is not None
    queue.mark_qf_done(t1, qf_output_path=tmp_path / "a.json")
    assert _batch_row(queue, 1)["qf_done_at"] is not None


# ---------------------------------------------------------------------------
# #9: run_tasks 映射 + 产物文件名
# ---------------------------------------------------------------------------

def test_run_task_map_insert_lookup_and_overwrite(tmp_path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    assert queue.lookup_task_id("nope") is None
    queue.insert_run_task_map([("run_1", "T1", 1), ("run_2", "T2", 1)])
    assert queue.lookup_task_id("run_1") == "T1"
    queue.insert_run_task_map([("run_1", "T1_re", 2)])  # 重跑覆盖
    assert queue.lookup_task_id("run_1") == "T1_re"
    queue.insert_run_task_map([])  # 空入参不炸


def test_output_name_prefixed_when_mapped(tmp_path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    queue.insert_batch(["T42"])
    tid = _seed(queue, tmp_path, "runx__sess.json", "sess", batch_id=1, run_id="runx")
    queue.insert_run_task_map([("runx", "zh_travel_01", 1)])

    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=tmp_path / "qf_out")
    [task] = w.pull()
    out_path = w.process(task)
    assert out_path == tmp_path / "qf_out" / "zh_travel_01__sess.json"
    assert out_path.exists()
    # gdr 命名同源: 直接校验共享 helper (不跑真实 _process_one_file)
    g = GdrWorker(queue=queue, worker_id="g", gdr_output_dir=tmp_path / "gdr_out")
    assert g._output_name(task, "sess", suffix="_refined") == "zh_travel_01__sess_refined.json"  # noqa: SLF001


def test_output_name_falls_back_without_mapping(tmp_path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    _seed(queue, tmp_path, "runx__sess.json", "sess", run_id="runx")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=tmp_path / "qf_out")
    [task] = w.pull()
    assert w._output_name(task, "sess", suffix="") == "sess.json"  # noqa: SLF001

    class _NoLookupQueue(SQLiteQueue):
        def lookup_task_id(self, run_id):  # 模拟查询炸掉 (旧库竞态等)
            raise RuntimeError("db locked")

    q2 = _NoLookupQueue(tmp_path / "q2.db")
    w2 = QfWorker(queue=q2, worker_id="w", qf_output_dir=tmp_path / "qf_out2")
    assert w2._output_name(task, "sess", suffix="") == "sess.json"  # noqa: SLF001


def test_producer_run_batch_writes_map(tmp_path, monkeypatch) -> None:
    """run_batch 结束后 run_tasks 表含 sanitize(run_id)→task_id 映射."""
    import orchestration.producer_simulate as prod
    from simulate_serve.domain.run import TaskRun
    from simulate_serve.domain.state_machine import RunState
    # 同目录 helper (tests/orchestration 无 __init__.py, pytest 已把该目录入 sys.path)
    from test_producer_simulate import _FakeServices, _make_task

    queue = SQLiteQueue(tmp_path / "q.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}", encoding="utf-8")
    t1 = _make_task("T1")
    r1 = TaskRun(run_id="run/2026:09 #1", task_id="T1", task_type="test",
                 state=RunState.SUCCESS)  # 含文件名非法字符, 验证 sanitize 键
    fake = _FakeServices([t1], [r1])

    async def fake_build(_cfg):
        return fake

    monkeypatch.setattr("orchestration.producer_simulate.build_application", fake_build)
    batch_id, runs = prod.run_batch(config_path=config_path, task_ids=["T1"],
                                    limit=1, queue=queue)
    assert runs[0].run_id == "run/2026:09 #1"  # 原 run_id 不被篡改
    # "run/2026:09 #1" → 连续非法字符折叠为单个 "_": run_2026_09_1
    assert queue.lookup_task_id("run_2026_09_1") == "T1"
    assert queue.lookup_task_id("run/2026:09 #1") is None
    with queue._conn() as conn:  # 映射带 batch_id 便于按批清理/审计
        row = conn.execute(
            "SELECT batch_id FROM run_tasks WHERE run_id = ?", ("run_2026_09_1",)
        ).fetchone()
    assert int(row["batch_id"]) == batch_id


# ---------------------------------------------------------------------------
# #7: 空闲退避
# ---------------------------------------------------------------------------

def test_worker_backoff_grows_and_caps(tmp_path, monkeypatch) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=tmp_path / "o",
                 poll_seconds=1.0)
    monkeypatch.setattr(w, "run_once", lambda: 0)
    ev = _FakeEvent(4)
    w.run_forever(ev, max_poll_seconds=5.0)
    assert ev.waits == [1.0, 2.0, 4.0, 5.0]


def test_worker_backoff_resets_after_processing(tmp_path, monkeypatch) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=tmp_path / "o",
                 poll_seconds=1.0)
    seq = iter([0, 0, 2, 0, 0, 0])
    monkeypatch.setattr(w, "run_once", lambda: next(seq))
    ev = _FakeEvent(6)
    w.run_forever(ev, max_poll_seconds=100.0)
    # 空1→1.0, 空2→2.0, 有活→复位1.0, 空→1.0, 2.0, 4.0
    assert ev.waits == [1.0, 2.0, 1.0, 1.0, 2.0, 4.0]


def test_worker_backoff_disabled_with_zero_cap(tmp_path, monkeypatch) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=tmp_path / "o",
                 poll_seconds=0.5)
    monkeypatch.setattr(w, "run_once", lambda: 0)
    ev = _FakeEvent(3)
    w.run_forever(ev, max_poll_seconds=0)
    assert ev.waits == [0.5, 0.5, 0.5]


def test_watcher_backoff_grows_resets_on_registration(tmp_path, monkeypatch) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    w = TrajectoryWatcher(trajectory_dir=tmp_path / "no_dir", queue=queue,
                          batch_id=1, poll_seconds=2.0)
    seq = iter([
        {"registered": 0, "skipped": 0, "dead": 0},
        {"registered": 3, "skipped": 0, "dead": 0},
        {"registered": 0, "skipped": 1, "dead": 0},
        {"registered": 0, "skipped": 1, "dead": 0},
        {"registered": 0, "skipped": 1, "dead": 0},
    ])
    monkeypatch.setattr(w, "scan_once", lambda: next(seq))
    ev = _FakeEvent(5)
    w.run_forever(ev, max_poll_seconds=10.0)
    # 空→2, 有登记→复位2, 空→2, 空→4, 空→8
    assert ev.waits == [2.0, 2.0, 2.0, 4.0, 8.0]


def test_watcher_backoff_off_by_default(tmp_path, monkeypatch) -> None:
    """默认 max_poll_seconds=0: 恒定间隔, 与旧行为一致 (短超时测试不受影响)."""
    queue = SQLiteQueue(tmp_path / "q.db")
    w = TrajectoryWatcher(trajectory_dir=tmp_path / "no_dir", queue=queue,
                          batch_id=1, poll_seconds=0.5)
    monkeypatch.setattr(w, "scan_once", lambda: {"registered": 0, "skipped": 0, "dead": 0})
    ev = _FakeEvent(3)
    w.run_forever(ev)
    assert ev.waits == [0.5, 0.5, 0.5]


def test_worker_failure_rounds_do_not_backoff(tmp_path, monkeypatch) -> None:
    """拉到 task 但全部失败不算空闲: 重试节奏保持 poll 恒定 (drain 超时可预期)."""
    queue = SQLiteQueue(tmp_path / "q.db")

    class _W(QfWorker):
        def run_once(self):
            self._last_pulled = 1  # 模拟拉到 1 条但处理失败 (返回 0)
            return 0

    w = _W(queue=queue, worker_id="w", qf_output_dir=tmp_path / "o",
           poll_seconds=0.3)
    ev = _FakeEvent(4)
    w.run_forever(ev, max_poll_seconds=100.0)
    assert ev.waits == [0.3, 0.3, 0.3, 0.3]


def test_setup_logging_demotes_httpx(tmp_path) -> None:
    from orchestration.daemon import setup_logging

    before = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    try:
        setup_logging(tmp_path / "logs", log_file="m.log")
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        # 还原 root handler 与被调级别的 logger, 避免污染同进程其它测试
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler) and str(tmp_path) in str(getattr(h, "baseFilename", "")):
                h.close()
                root.removeHandler(h)
        for n, lvl in before.items():
            logging.getLogger(n).setLevel(lvl)


