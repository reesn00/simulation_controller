"""orchestration.workers.gdr_worker 单元测试.

通过 monkeypatch ``gdr.pipeline.runner._process_one_file`` 避免依赖真实 LLM endpoint.

新架构下 gdr 是首阶段:
    - 输入: trajectory JSONL (``task.src_path``)
    - 输出: C2 refined Session 单文件 (``gdr_refined_path``)
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from orchestration.errors import NonRetryableError
from orchestration.queue import (
    STATE_DEAD,
    STATE_DONE,
    STATE_PENDING,
    STATE_PENDING_ETL,
    SQLiteQueue,
)
from orchestration.workers.gdr_worker import GdrWorker


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path: Path):
    queue = SQLiteQueue(tmp_path / "q.db", max_retry_gdr=2, max_retry_etl=2)
    refined_dir = tmp_path / "refined"
    return queue, refined_dir, tmp_path


def _make_trajectory(tmp_path: Path, session_id: str) -> Path:
    """造一个最小 trajectory JSONL (gdr 输入). 文件存在即可, 解析 stub 在 monkeypatch 里."""
    fp = tmp_path / f"{session_id}.json"
    fp.write_text("{}\n", encoding="utf-8")
    return fp


def _seed_gdr_task(queue: SQLiteQueue, src_path: Path, session_id: str) -> int:
    """登记 task 并保持 ``state=pending`` (让 GdrWorker.pull() 拉到)."""
    tid, _ = queue.insert(src_path=src_path, run_id="r1", session_id=session_id, batch_id=1)
    return tid


def _write_c2_output(output_path: Path) -> dict:
    """模拟真实 gdr: 写 C2 refined Session 单文件并返回 result dict."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        '{"session_id":"s1","messages":[],"schema_version":"refined_session.v1"}',
        encoding="utf-8",
    )
    return {"status": "success", "output": str(output_path)}


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------

def test_construct_default(env) -> None:
    queue, refined_dir, _ = env
    GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)


def test_construct_with_settings(env) -> None:
    queue, refined_dir, _ = env
    from gdr.config.settings import Settings
    cfg = Settings(workers=2, llm_concurrency=4)
    GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir, gdr_settings=cfg)


# ---------------------------------------------------------------------------
# pull / process / mark_done
# ---------------------------------------------------------------------------

def _mock_gdr(monkeypatch, fake_process_one):
    """同时 mock ``from_trajectory`` (GdrWorker 前置校验) + ``_process_one_file``."""
    monkeypatch.setattr(
        "orchestration.workers.gdr_worker.from_trajectory",
        lambda path: None,
    )
    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        fake_process_one,
    )


def test_pull_returns_pending_only(env) -> None:
    """GdrWorker 从 ``state=pending`` 拉 (新架构首阶段), 不应拉到 pending_etl."""
    queue, refined_dir, tmp_path = env
    # 先插 other (id=1) → setup 拉到 (id=1, s1 待会儿才插, 此时 other 唯一 pending)
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    queue.insert(src_path=other, run_id="r2", session_id="other", batch_id=1)
    [seed_task] = queue.pull_pending_gdr(worker_id="setup", n=1)
    queue.mark_gdr_done(seed_task.id, gdr_refined_path=other.parent / "other_refined.json")

    # 后插 s1 → 唯一 pending
    src = _make_trajectory(tmp_path, "s1")
    _seed_gdr_task(queue, src, "s1")

    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    tasks = w.pull()
    # s1 是 pending → 拉到; other 是 pending_etl → 不会拉到
    assert len(tasks) == 1
    assert tasks[0].state == "gdr_processing"
    assert tasks[0].session_id == "s1"


def test_process_calls_gdr_and_returns_c2(env, monkeypatch) -> None:
    queue, refined_dir, tmp_path = env
    src = _make_trajectory(tmp_path, "s1")
    _seed_gdr_task(queue, src, "s1")

    captured: dict = {}

    def fake_process_one(input_path, output_path, cfg):
        captured["input"] = input_path
        captured["output"] = output_path
        captured["cfg_workers"] = cfg.workers
        return _write_c2_output(output_path)

    _mock_gdr(monkeypatch, fake_process_one)

    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    [task] = w.pull()
    out_path = w.process(task)

    # 单 C2 文件 (无 _refined 后缀, etl 末端会加 .messages.json 等尾缀)
    assert out_path.name == "s1.json"
    assert out_path.exists()
    assert captured["input"] == src
    # 强制 cfg.workers=1 (避免 gdr 内部 Pool)
    assert captured["cfg_workers"] == 1


def test_process_uses_src_path_not_qf_output(env, monkeypatch) -> None:
    """新架构 gdr 从 ``task.src_path`` (trajectory) 读，不再读 qf_output."""
    queue, refined_dir, tmp_path = env
    src = _make_trajectory(tmp_path, "s1")
    _seed_gdr_task(queue, src, "s1")

    captured = {}
    def fake(input_path, output_path, cfg):
        captured["input"] = input_path
        return _write_c2_output(output_path)

    _mock_gdr(monkeypatch, fake)

    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    [task] = w.pull()
    w.process(task)
    assert captured["input"] == src


def test_process_missing_src_raises(env, monkeypatch) -> None:
    """trajectory 源文件缺失是永久性错误."""
    queue, refined_dir, tmp_path = env
    src = tmp_path / "missing.json"  # 不创建
    _seed_gdr_task(queue, src, "s1")

    _mock_gdr(monkeypatch, lambda *a, **kw: {"status": "success"})

    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    [task] = w.pull()
    with pytest.raises(NonRetryableError, match="trajectory missing"):
        w.process(task)


def test_process_gdr_returns_non_success_raises(env, monkeypatch) -> None:
    """可重试的非 success (如 save_error) → 普通 RuntimeError, 走重试."""
    queue, refined_dir, tmp_path = env
    src = _make_trajectory(tmp_path, "s1")
    _seed_gdr_task(queue, src, "s1")

    _mock_gdr(monkeypatch, lambda i, o, c: {"status": "save_error"})
    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    [task] = w.pull()
    with pytest.raises(RuntimeError, match="non-success"):
        w.process(task)


def test_process_gdr_permanent_status_raises_non_retryable(env, monkeypatch) -> None:
    """load_error / discard 是永久结果 → NonRetryableError (不消耗重试)."""
    queue, refined_dir, tmp_path = env
    src = _make_trajectory(tmp_path, "s1")
    _seed_gdr_task(queue, src, "s1")

    _mock_gdr(monkeypatch, lambda i, o, c: {"status": "discard"})
    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    [task] = w.pull()
    with pytest.raises(NonRetryableError, match="discard"):
        w.process(task)


def test_run_once_load_error_goes_dead_without_retry(env, monkeypatch) -> None:
    """load_error 经 run_once → 直接 dead, attempts_gdr 不增, 带 [non-retryable] 前缀."""
    queue, refined_dir, tmp_path = env
    src = _make_trajectory(tmp_path, "s1")
    tid = _seed_gdr_task(queue, src, "s1")

    _mock_gdr(monkeypatch, lambda i, o, c: {"status": "load_error", "error": "bad json"})
    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    assert w.run_once() == 0

    refreshed = queue.get(tid)
    assert refreshed is not None
    assert refreshed.state == STATE_DEAD
    assert refreshed.attempts_gdr == 0
    assert "[non-retryable]" in (refreshed.error_msg or "")


def test_process_gdr_returns_none_raises(env, monkeypatch) -> None:
    queue, refined_dir, tmp_path = env
    src = _make_trajectory(tmp_path, "s1")
    _seed_gdr_task(queue, src, "s1")

    _mock_gdr(monkeypatch, lambda i, o, c: None)
    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    [task] = w.pull()
    with pytest.raises(RuntimeError, match="status='None'"):
        w.process(task)


def test_mark_done_transitions_to_pending_etl(env, monkeypatch) -> None:
    """gdr 完成 → state=pending_etl, 写 C2 路径."""
    queue, refined_dir, tmp_path = env
    src = _make_trajectory(tmp_path, "s1")
    tid = _seed_gdr_task(queue, src, "s1")

    _mock_gdr(monkeypatch, lambda i, o, c: _write_c2_output(o))
    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    [task] = w.pull()
    out_path = w.process(task)
    w.mark_done(task, out_path)

    refreshed = queue.get(tid)
    assert refreshed is not None
    assert refreshed.state == STATE_PENDING_ETL
    assert refreshed.gdr_refined_path == str(out_path)


# ---------------------------------------------------------------------------
# 失败 / 重试 / dead
# ---------------------------------------------------------------------------

def test_run_once_marks_failed_on_process_error(env, monkeypatch) -> None:
    queue, refined_dir, tmp_path = env
    src = _make_trajectory(tmp_path, "s1")
    tid = _seed_gdr_task(queue, src, "s1")

    def boom(input_path, output_path, cfg):
        raise ValueError("gdr exploded")

    _mock_gdr(monkeypatch, boom)

    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    success = w.run_once()
    assert success == 0

    refreshed = queue.get(tid)
    assert refreshed is not None
    assert refreshed.state == STATE_PENDING  # 退回 pending (新架构首阶段), 可重试
    assert refreshed.attempts_gdr == 1
    assert "gdr exploded" in (refreshed.error_msg or "")


def test_run_once_dead_after_max_retries_gdr(env, monkeypatch) -> None:
    queue, refined_dir, tmp_path = env
    src = _make_trajectory(tmp_path, "s1")
    tid = _seed_gdr_task(queue, src, "s1")

    def boom(*a, **kw):
        raise ValueError("boom")
    _mock_gdr(monkeypatch, boom)
    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    w.run_once()  # 1
    w.run_once()  # 2
    w.run_once()  # 3 > max_retry_gdr=2 → dead

    refreshed = queue.get(tid)
    assert refreshed is not None
    assert refreshed.state == STATE_DEAD
    assert refreshed.attempts_gdr == 3


# ---------------------------------------------------------------------------
# 完整闭环 (gdr → etl)
# ---------------------------------------------------------------------------

def test_run_once_end_to_end_gdr_then_etl(env, monkeypatch) -> None:
    """gdr 完成 C2 后被 etl 消费的端到端 (etl 由 EtlWorker 消费, 这里只验证 gdr 末端)."""
    queue, refined_dir, tmp_path = env
    src = _make_trajectory(tmp_path, "s1")
    _seed_gdr_task(queue, src, "s1")

    _mock_gdr(monkeypatch, lambda i, o, c: _write_c2_output(o))

    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir)
    success = w.run_once()
    assert success == 1
    counts = queue.count_by_state()
    assert counts.get(STATE_PENDING_ETL) == 1
    assert counts.get(STATE_PENDING, 0) == 0


# ---------------------------------------------------------------------------
# run_forever
# ---------------------------------------------------------------------------

def test_run_forever_exits_on_stop_event(env, monkeypatch) -> None:
    queue, refined_dir, tmp_path = env
    src = _make_trajectory(tmp_path, "s1")
    _seed_gdr_task(queue, src, "s1")

    _mock_gdr(monkeypatch, lambda i, o, c: _write_c2_output(o))
    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir, poll_seconds=0.05)
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert queue.count_by_state().get(STATE_PENDING_ETL) == 1


def test_run_forever_processes_later_added_tasks(env, monkeypatch) -> None:
    queue, refined_dir, tmp_path = env

    _mock_gdr(monkeypatch, lambda i, o, c: _write_c2_output(o))
    w = GdrWorker(queue=queue, worker_id="w", refined_dir=refined_dir, poll_seconds=0.05)
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()

    time.sleep(0.1)
    src = _make_trajectory(tmp_path, "late")
    queue.insert(src_path=src, run_id="rl", session_id="late", batch_id=1)
    time.sleep(0.3)
    stop.set()
    t.join(timeout=1.0)

    assert queue.count_by_state().get(STATE_PENDING_ETL) == 1
    assert (refined_dir / "late.json").exists()