"""orchestration.workers.gdr_worker 单元测试.

通过 monkeypatch ``gdr.pipeline.runner._process_one_file`` 避免依赖真实 LLM endpoint.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from orchestration.queue import (
    STATE_DEAD,
    STATE_DONE,
    STATE_PENDING,
    STATE_PENDING_GDR,
    SQLiteQueue,
    Task,
)
from orchestration.workers.gdr_worker import GdrWorker


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path: Path):
    queue = SQLiteQueue(tmp_path / "q.db", max_retry_qf=2, max_retry_gdr=2)
    gdr_out = tmp_path / "gdr_out"
    return queue, gdr_out, tmp_path


def _make_qf_output(tmp_path: Path, session_id: str) -> Path:
    """造一个最小 Session JSON（含 metadata.openai_messages + tools + qf_text）."""
    fp = tmp_path / f"{session_id}.json"
    payload = {
        "session_id": session_id,
        "summary": "",
        "messages": [{"role": "user", "name": "user", "id": "u",
                      "blocks": [{"type": "text", "text": "hi"}], "metadata": {}}],
        "metadata": {
            "openai_messages": [{"role": "user", "content": "hi"}],
            "tools": [],
            "qf_text": "user\nhi",
        },
    }
    fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return fp


def _seed_qf_task(queue: SQLiteQueue, qf_path: Path, session_id: str) -> int:
    """登记 task 并推到 pending_gdr 状态.

    走完整的 qf 流程（pull_pending_qf → mark_qf_done），因为 mark_qf_done
    的 UPDATE 守卫要求 state='qf_processing'。
    """
    tid, _ = queue.insert(src_path=qf_path, run_id="r1", session_id=session_id, batch_id=1)
    [task] = queue.pull_pending_qf(worker_id="qf_seed", n=1)
    assert task.id == tid
    queue.mark_qf_done(tid, qf_output_path=qf_path)
    return tid


def _seed_qf_task_split(queue: SQLiteQueue, src_path: Path, qf_path: Path,
                        session_id: str) -> int:
    """登记 task 时 src 是 trajectory、qf_output 是另存文件（覆盖前者的语义）."""
    tid, _ = queue.insert(src_path=src_path, run_id="r1", session_id=session_id, batch_id=1)
    [task] = queue.pull_pending_qf(worker_id="qf_seed", n=1)
    assert task.id == tid
    queue.mark_qf_done(tid, qf_output_path=qf_path)
    return tid


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------

def test_construct_default(env) -> None:
    queue, gdr_out, _ = env
    GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out)


def test_construct_with_settings(env) -> None:
    queue, gdr_out, _ = env
    from gdr.config.settings import Settings
    cfg = Settings(workers=2, llm_concurrency=4)
    GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out, gdr_settings=cfg)


# ---------------------------------------------------------------------------
# pull / process / mark_done
# ---------------------------------------------------------------------------

def test_pull_returns_pending_gdr_only(env, monkeypatch) -> None:
    queue, gdr_out, tmp_path = env
    qf = _make_qf_output(tmp_path, "s1")
    _seed_qf_task(queue, qf, "s1")

    # 还登记一个 pending task（不应被 gdr worker 拉）
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    queue.insert(src_path=other, run_id="r2", session_id="other", batch_id=1)

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        lambda *a, **kw: {"status": "success", "input": str(a[0]), "output": str(a[1])},
    )

    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out)
    tasks = w.pull()
    assert len(tasks) == 1
    assert tasks[0].state == "gdr_processing"
    assert tasks[0].session_id == "s1"


def test_process_calls_gdr_and_returns_output(env, monkeypatch) -> None:
    queue, gdr_out, tmp_path = env
    qf = _make_qf_output(tmp_path, "s1")
    _seed_qf_task(queue, qf, "s1")

    captured: dict = {}

    def fake_process_one(input_path, output_path, cfg):
        captured["input"] = input_path
        captured["output"] = output_path
        captured["cfg_workers"] = cfg.workers
        # 模拟写出 output 文件
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("{}", encoding="utf-8")
        return {"status": "success"}

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        fake_process_one,
    )

    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out)
    [task] = w.pull()
    out_path = w.process(task)

    assert out_path == gdr_out / "s1_refined.json"
    assert out_path.exists()
    assert captured["input"] == qf
    # 强制 cfg.workers=1（避免 gdr 内部 Pool）
    assert captured["cfg_workers"] == 1


def test_process_uses_qf_output_path_not_src_path(env, monkeypatch) -> None:
    """qf 阶段已写入 qf_output_path；gdr 应从那里读，不是原始 trajectory."""
    queue, gdr_out, tmp_path = env
    src = tmp_path / "raw_traj.json"
    src.write_text("{}", encoding="utf-8")
    qf = _make_qf_output(tmp_path, "s1")
    _seed_qf_task_split(queue, src, qf, "s1")

    captured = {}
    def fake(input_path, output_path, cfg):
        captured["input"] = input_path
        output_path.write_text("{}", encoding="utf-8")
        return {"status": "success"}

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file", fake,
    )

    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out)
    [task] = w.pull()
    w.process(task)
    assert captured["input"] == qf


def test_process_missing_qf_output_raises(env, monkeypatch) -> None:
    queue, gdr_out, tmp_path = env
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    missing = tmp_path / "missing.json"  # 不存在
    _seed_qf_task_split(queue, src, missing, "s1")

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        lambda *a, **kw: {"status": "success"},
    )

    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out)
    [task] = w.pull()
    with pytest.raises(FileNotFoundError, match="qf output missing"):
        w.process(task)


def test_process_gdr_returns_non_success_raises(env, monkeypatch) -> None:
    queue, gdr_out, tmp_path = env
    qf = _make_qf_output(tmp_path, "s1")
    _seed_qf_task(queue, qf, "s1")

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        lambda i, o, c: {"status": "discard"},
    )
    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out)
    [task] = w.pull()
    with pytest.raises(RuntimeError, match="non-success"):
        w.process(task)


def test_process_gdr_returns_none_raises(env, monkeypatch) -> None:
    queue, gdr_out, tmp_path = env
    qf = _make_qf_output(tmp_path, "s1")
    _seed_qf_task(queue, qf, "s1")

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        lambda i, o, c: None,
    )
    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out)
    [task] = w.pull()
    with pytest.raises(RuntimeError, match="status='None'"):
        w.process(task)


def test_mark_done_transitions_to_done(env, monkeypatch) -> None:
    queue, gdr_out, tmp_path = env
    qf = _make_qf_output(tmp_path, "s1")
    tid = _seed_qf_task(queue, qf, "s1")

    def fake(i, o, c):
        o.write_text("{}", encoding="utf-8")
        return {"status": "success"}

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file", fake,
    )
    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out)
    [task] = w.pull()
    out_path = w.process(task)
    w.mark_done(task, out_path)

    refreshed = queue.get(tid)
    assert refreshed is not None
    assert refreshed.state == STATE_DONE
    assert refreshed.gdr_output_path == str(out_path)


# ---------------------------------------------------------------------------
# 失败 / 重试 / dead
# ---------------------------------------------------------------------------

def test_run_once_marks_failed_on_process_error(env, monkeypatch) -> None:
    queue, gdr_out, tmp_path = env
    qf = _make_qf_output(tmp_path, "s1")
    tid = _seed_qf_task(queue, qf, "s1")

    def boom(input_path, output_path, cfg):
        raise ValueError("gdr exploded")

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file", boom,
    )

    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out)
    success = w.run_once()
    assert success == 0

    refreshed = queue.get(tid)
    assert refreshed is not None
    assert refreshed.state == STATE_PENDING_GDR  # 退回 pending_gdr，可重试
    assert refreshed.attempts_gdr == 1
    assert "gdr exploded" in (refreshed.error_msg or "")


def test_run_once_dead_after_max_retries_gdr(env, monkeypatch) -> None:
    queue, gdr_out, tmp_path = env
    qf = _make_qf_output(tmp_path, "s1")
    tid = _seed_qf_task(queue, qf, "s1")

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom")),
    )
    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out)
    w.run_once()  # 1
    w.run_once()  # 2
    w.run_once()  # 3 > max_retry_gdr=2 → dead

    refreshed = queue.get(tid)
    assert refreshed is not None
    assert refreshed.state == STATE_DEAD
    assert refreshed.attempts_gdr == 3


# ---------------------------------------------------------------------------
# 完整闭环（qf → gdr）
# ---------------------------------------------------------------------------

def test_run_once_end_to_end_qf_then_gdr(env, monkeypatch) -> None:
    """qf 完成后再被 gdr 消费的端到端."""
    queue, gdr_out, tmp_path = env
    # 直接在 pending_gdr 状态登记（模拟 qf 已完成）
    qf = _make_qf_output(tmp_path, "s1")
    _seed_qf_task(queue, qf, "s1")

    def fake(i, o, c):
        o.write_text(json.dumps({"refined": True}), encoding="utf-8")
        return {"status": "success"}

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file", fake,
    )

    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out)
    success = w.run_once()
    assert success == 1
    counts = queue.count_by_state()
    assert counts.get(STATE_DONE) == 1
    assert counts.get(STATE_PENDING_GDR, 0) == 0
    assert counts.get(STATE_PENDING, 0) == 0


# ---------------------------------------------------------------------------
# run_forever
# ---------------------------------------------------------------------------

def test_run_forever_exits_on_stop_event(env, monkeypatch) -> None:
    queue, gdr_out, tmp_path = env
    qf = _make_qf_output(tmp_path, "s1")
    _seed_qf_task(queue, qf, "s1")

    def fake(i, o, c):
        o.write_text("{}", encoding="utf-8")
        return {"status": "success"}

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file", fake,
    )
    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out, poll_seconds=0.05)
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert queue.count_by_state().get(STATE_DONE) == 1


def test_run_forever_processes_later_added_tasks(env, monkeypatch) -> None:
    queue, gdr_out, tmp_path = env

    def fake(i, o, c):
        o.write_text("{}", encoding="utf-8")
        return {"status": "success"}

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file", fake,
    )
    w = GdrWorker(queue=queue, worker_id="w", gdr_output_dir=gdr_out, poll_seconds=0.05)
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()

    time.sleep(0.1)
    qf = _make_qf_output(tmp_path, "late")
    _seed_qf_task(queue, qf, "late")
    time.sleep(0.2)
    stop.set()
    t.join(timeout=1.0)

    assert queue.count_by_state().get(STATE_DONE) == 1
    assert (gdr_out / "late_refined.json").exists()