"""orchestration.workers.qf_worker 单元测试."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from orchestration.queue import (
    STATE_DEAD,
    STATE_PENDING,
    STATE_PENDING_GDR,
    SQLiteQueue,
    Task,
)
from orchestration.workers.qf_worker import QfWorker


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _trajectory(session_id: str = "sess-1") -> dict:
    """最小有效 trajectory（含 user + assistant）."""
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


@pytest.fixture
def env(tmp_path: Path):
    """每个测试一个独立 db + qf_output_dir."""
    queue = SQLiteQueue(tmp_path / "q.db", max_retry_qf=2, max_retry_gdr=2)
    qf_output_dir = tmp_path / "qf_out"
    return queue, qf_output_dir, tmp_path


def _seed_trajectory(queue: SQLiteQueue, tmp_path: Path, name: str, session_id: str) -> int:
    fp = tmp_path / name
    fp.write_text(json.dumps(_trajectory(session_id), ensure_ascii=False), encoding="utf-8")
    tid, inserted = queue.insert(src_path=fp, run_id="r1", session_id=session_id, batch_id=1)
    assert inserted
    return tid


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------

def test_construct_default_template(env) -> None:
    """默认 template_path 应指向仓库内 chat_template.jinja."""
    queue, qf_out, _ = env
    QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out)  # 不抛错


def test_construct_with_explicit_template(env) -> None:
    queue, qf_out, _ = env
    QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, template_str="dummy")


# ---------------------------------------------------------------------------
# pull / process / mark_done
# ---------------------------------------------------------------------------

def test_pull_returns_pending_only(env) -> None:
    queue, qf_out, tmp_path = env
    _seed_trajectory(queue, tmp_path, "r__a.json", "a")
    _seed_trajectory(queue, tmp_path, "r__b.json", "b")

    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, n=10)
    tasks = w.pull()
    assert len(tasks) == 2
    assert all(t.state == "qf_processing" for t in tasks)
    assert all(t.locked_by == "w" for t in tasks)


def test_process_writes_qf_output(env) -> None:
    queue, qf_out, tmp_path = env
    _seed_trajectory(queue, tmp_path, "r__sess.json", "sess")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out)

    [task] = w.pull()
    out_path = w.process(task)
    assert out_path.exists()
    assert out_path == qf_out / "sess.json"

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["session_id"] == "sess"
    assert payload["summary"] == "test"
    # messages (blocks) 保留
    assert len(payload["messages"]) == 2
    # metadata 完整
    md = payload["metadata"]
    assert "openai_messages" in md
    assert "tools" in md
    assert "qf_text" in md
    assert md["openai_messages"][0]["role"] == "user"


def test_process_creates_qf_output_dir(tmp_path: Path) -> None:
    """qf_output_dir 不存在时应自动创建."""
    queue = SQLiteQueue(tmp_path / "q.db")
    qf_out = tmp_path / "deep" / "nested" / "qf_out"  # 不存在
    _seed_trajectory(queue, tmp_path, "r__sess.json", "sess")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out)

    [task] = w.pull()
    out_path = w.process(task)
    assert out_path.exists()


def test_mark_done_transitions_to_pending_gdr(env) -> None:
    queue, qf_out, tmp_path = env
    tid = _seed_trajectory(queue, tmp_path, "r__sess.json", "sess")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out)

    [task] = w.pull()
    out_path = w.process(task)
    w.mark_done(task, out_path)

    refreshed = queue.get(tid)
    assert refreshed is not None
    assert refreshed.state == STATE_PENDING_GDR
    assert refreshed.qf_output_path == str(out_path)


# ---------------------------------------------------------------------------
# run_once 端到端
# ---------------------------------------------------------------------------

def test_run_once_processes_all_pulled(env) -> None:
    queue, qf_out, tmp_path = env
    _seed_trajectory(queue, tmp_path, "r__a.json", "a")
    _seed_trajectory(queue, tmp_path, "r__b.json", "b")
    _seed_trajectory(queue, tmp_path, "r__c.json", "c")

    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, n=10)
    success = w.run_once()
    assert success == 3
    counts = queue.count_by_state()
    assert counts.get(STATE_PENDING_GDR) == 3
    assert counts.get(STATE_PENDING, 0) == 0
    # qf_out 应有 3 个文件
    assert len(list(qf_out.glob("*.json"))) == 3


def test_run_once_no_tasks_returns_zero(env) -> None:
    queue, qf_out, _ = env
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out)
    assert w.run_once() == 0


# ---------------------------------------------------------------------------
# 失败 / 重试 / dead
# ---------------------------------------------------------------------------

def test_run_once_handles_process_failure_via_mark_failed(env) -> None:
    """process 抛异常 → mark_failed(stage=qf) → attempts_qf=1, state=pending."""
    queue, qf_out, tmp_path = env
    tid = _seed_trajectory(queue, tmp_path, "r__a.json", "a")
    # 让 trajectory 内容为非法 JSON → process 抛 json.JSONDecodeError
    (tmp_path / "r__a.json").write_text("{not json", encoding="utf-8")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out)
    success = w.run_once()
    assert success == 0  # 失败不计成功

    refreshed = queue.get(tid)
    assert refreshed is not None
    assert refreshed.state == STATE_PENDING
    assert refreshed.attempts_qf == 1
    assert "JSONDecodeError" in (refreshed.error_msg or "")


def test_run_once_dead_after_max_retries(env) -> None:
    queue, qf_out, tmp_path = env
    tid = _seed_trajectory(queue, tmp_path, "r__a.json", "a")
    (tmp_path / "r__a.json").write_text("{bad", encoding="utf-8")

    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out)
    # max_retry_qf=2 → 第 3 次失败入 dead
    w.run_once()  # attempts 0→1
    w.run_once()  # attempts 1→2
    w.run_once()  # attempts 2→3 → dead (3 > 2)

    refreshed = queue.get(tid)
    assert refreshed is not None
    assert refreshed.state == STATE_DEAD
    assert refreshed.attempts_qf == 3


# ---------------------------------------------------------------------------
# run_forever
# ---------------------------------------------------------------------------

def test_run_forever_exits_on_stop_event(env) -> None:
    queue, qf_out, tmp_path = env
    _seed_trajectory(queue, tmp_path, "r__a.json", "a")

    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, poll_seconds=0.05)
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert queue.count_pending_gdr() == 1


def test_run_forever_processes_later_added_tasks(env) -> None:
    queue, qf_out, tmp_path = env
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, poll_seconds=0.05)
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()

    time.sleep(0.1)
    _seed_trajectory(queue, tmp_path, "r__late.json", "late")
    time.sleep(0.2)
    stop.set()
    t.join(timeout=1.0)

    assert queue.count_pending_gdr() == 1
    assert (qf_out / "late.json").exists()