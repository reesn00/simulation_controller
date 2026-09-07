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

def _trajectory_jsonl(session_id: str = "sess-1") -> str:
    """新格式 trajectory: ``run_<run_id>__<session_id>.json`` JSONL 事件流.

    最小有效序列: turn_start → model_request → model_response → final_reply.
    """
    events = [
        {
            "trace_id": "t1", "span_id": "s1", "parent_span_id": None,
            "event_type": "turn_start", "timestamp": "2026-09-05T00:00:00+00:00",
            "session_id": session_id, "agent_id": "default", "user_id": "u",
            "channel": "console", "provider_id": "", "model_name": "m",
            "payload": {"input_text": "hi", "request_agent_id": "default", "agent_backend": "x"},
            "metadata": {},
        },
        {
            "trace_id": "t1", "span_id": "s2", "parent_span_id": None,
            "event_type": "model_request", "timestamp": "2026-09-05T00:00:00+00:00",
            "session_id": session_id, "agent_id": "default", "user_id": "u",
            "channel": "console", "provider_id": "p", "model_name": "m",
            "payload": {
                "messages": [{"role": "system", "content": [{"type": "text", "text": "sys"}]}],
                "tools": [],
            },
            "metadata": {},
        },
        {
            "trace_id": "t1", "span_id": "s3", "parent_span_id": "s2",
            "event_type": "model_response", "timestamp": "2026-09-05T00:00:01+00:00",
            "session_id": session_id, "agent_id": "default", "user_id": "u",
            "channel": "console", "provider_id": "p", "model_name": "m",
            "payload": {"usage": {"total_tokens": 10}}, "metadata": {"duration_ms": 1000},
        },
        {
            "trace_id": "t1", "span_id": "s4", "parent_span_id": None,
            "event_type": "final_reply", "timestamp": "2026-09-05T00:00:02+00:00",
            "session_id": session_id, "agent_id": "default", "user_id": "u",
            "channel": "console", "provider_id": "p", "model_name": "m",
            "payload": {
                "content": [
                    {"type": "reasoning", "content": [{"type": "text", "text": "think"}]},
                    {"type": "message", "content": [{"type": "text", "text": "hello"}]},
                ],
            },
            "metadata": {},
        },
    ]
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"


@pytest.fixture
def env(tmp_path: Path):
    """每个测试一个独立 db + qf_output_dir + system_templates_dir."""
    queue = SQLiteQueue(tmp_path / "q.db", max_retry_qf=2, max_retry_gdr=2)
    qf_output_dir = tmp_path / "qf_out"
    templates_dir = tmp_path / "templates"
    return queue, qf_output_dir, tmp_path, templates_dir


def _trajectory_jsonl_with_framework_system(session_id: str = "sess-1") -> str:
    """system prompt 含 AGENTS.md / SOUL.md / PROFILE.md 等框架段."""
    system_text = (
        "# Agent Identity\n\nYour agent id is `default`.\n\n"
        "# AGENTS.md\n\n## 安全\n- 不要泄露.\n\n"
        "# SOUL.md\n\n真心帮忙.\n\n"
        "# PROFILE.md\n\n## 身份\n- 名字\n\n"
        "检索标题（RETRIEVAL HEADLINE）。每个回复都必须追加 headline。\n\n"
        "# 长期记忆\n\n- `MEMORY.md` 是核心记忆.\n"
    )
    events = [
        {
            "trace_id": "t1", "span_id": "s1", "parent_span_id": None,
            "event_type": "turn_start", "timestamp": "2026-09-05T00:00:00+00:00",
            "session_id": session_id, "agent_id": "default", "user_id": "u",
            "channel": "console", "provider_id": "", "model_name": "m",
            "payload": {"input_text": "hi", "request_agent_id": "default", "agent_backend": "x"},
            "metadata": {},
        },
        {
            "trace_id": "t1", "span_id": "s2", "parent_span_id": None,
            "event_type": "model_request", "timestamp": "2026-09-05T00:00:00+00:00",
            "session_id": session_id, "agent_id": "default", "user_id": "u",
            "channel": "console", "provider_id": "p", "model_name": "m",
            "payload": {
                "messages": [{"role": "system", "content": [{"type": "text", "text": system_text}]}],
                "tools": [
                    {"type": "function", "function": {"name": "web_search", "description": "搜索"}},
                ],
            },
            "metadata": {},
        },
        {
            "trace_id": "t1", "span_id": "s3", "parent_span_id": "s2",
            "event_type": "model_response", "timestamp": "2026-09-05T00:00:01+00:00",
            "session_id": session_id, "agent_id": "default", "user_id": "u",
            "channel": "console", "provider_id": "p", "model_name": "m",
            "payload": {"usage": {"total_tokens": 10}}, "metadata": {"duration_ms": 1000},
        },
        {
            "trace_id": "t1", "span_id": "s4", "parent_span_id": None,
            "event_type": "final_reply", "timestamp": "2026-09-05T00:00:02+00:00",
            "session_id": session_id, "agent_id": "default", "user_id": "u",
            "channel": "console", "provider_id": "p", "model_name": "m",
            "payload": {
                "content": [
                    {"type": "reasoning", "content": [{"type": "text", "text": "think"}]},
                    {"type": "message", "content": [{"type": "text", "text": "hello"}]},
                ],
            },
            "metadata": {},
        },
    ]
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"


def _seed_trajectory(queue: SQLiteQueue, tmp_path: Path, name: str, session_id: str) -> int:
    fp = tmp_path / name
    fp.write_text(_trajectory_jsonl(session_id), encoding="utf-8")
    # 解析 run_id / session_id 与 watcher.parse_trajectory_filename 对齐
    if "__" in fp.stem:
        run_id, sess = fp.stem.split("__", 1)
    else:
        run_id, sess = fp.stem, ""
    tid, inserted = queue.insert(src_path=fp, run_id=run_id, session_id=sess or session_id, batch_id=1)
    assert inserted
    return tid


def _seed_trajectory_with_system(queue: SQLiteQueue, tmp_path: Path, name: str, session_id: str) -> int:
    fp = tmp_path / name
    fp.write_text(_trajectory_jsonl_with_framework_system(session_id), encoding="utf-8")
    if "__" in fp.stem:
        run_id, sess = fp.stem.split("__", 1)
    else:
        run_id, sess = fp.stem, ""
    tid, inserted = queue.insert(src_path=fp, run_id=run_id, session_id=sess or session_id, batch_id=1)
    assert inserted
    return tid


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------

def test_construct_default_template(env) -> None:
    """默认 template_path 应指向仓库内 chat_template.jinja."""
    queue, qf_out, _, templates_dir = env
    QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, system_templates_dir=templates_dir)  # 不抛错


def test_construct_with_explicit_template(env) -> None:
    queue, qf_out, _, templates_dir = env
    QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out,
             system_templates_dir=templates_dir, template_str="dummy")


# ---------------------------------------------------------------------------
# pull / process / mark_done
# ---------------------------------------------------------------------------

def test_pull_returns_pending_only(env) -> None:
    queue, qf_out, tmp_path, templates_dir = env
    _seed_trajectory(queue, tmp_path, "r1__a.json", "a")
    _seed_trajectory(queue, tmp_path, "r1__b.json", "b")

    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out,
                 system_templates_dir=templates_dir, n=10)
    tasks = w.pull()
    assert len(tasks) == 2
    assert all(t.state == "qf_processing" for t in tasks)
    assert all(t.locked_by == "w" for t in tasks)


def test_process_writes_qf_output(env) -> None:
    queue, qf_out, tmp_path, templates_dir = env
    _seed_trajectory(queue, tmp_path, "r1__sess.json", "sess")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, system_templates_dir=templates_dir)

    [task] = w.pull()
    out_path = w.process(task)
    assert out_path.exists()
    assert out_path == qf_out / "sess.json"

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["session_id"] == "sess"
    # messages (blocks) 保留; 早期事件流的 system prompt 现在会被补成 system message
    assert len(payload["messages"]) == 3
    assert payload["messages"][0]["role"] == "system"
    user_msg = payload["messages"][1]
    assert user_msg["role"] == "user"
    assert any(b.get("text") == "hi" for b in user_msg["blocks"])
    # metadata 完整
    md = payload["metadata"]
    assert "openai_messages" in md
    assert "tools" in md
    assert "qf_text" in md
    assert md["openai_messages"][0]["role"] == "system"


def test_process_cleans_framework_system_prompt(env) -> None:
    """qf_worker 应清洗 system prompt: 去掉 AGENTS.md/SOUL.md/PROFILE.md,
    保留 Agent Identity 与约束段, 并提取本地模板."""
    queue, qf_out, tmp_path, templates_dir = env
    tid = _seed_trajectory_with_system(queue, tmp_path, "r1__sess.json", "sess")
    templates_dir = tmp_path / "templates"
    w = QfWorker(
        queue=queue, worker_id="w", qf_output_dir=qf_out,
        system_templates_dir=templates_dir,
    )

    [task] = w.pull()
    out_path = w.process(task)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    # openai_messages 中 system 消息已被清洗
    oa = payload["metadata"]["openai_messages"]
    system_msg = next((m for m in oa if m["role"] == "system"), None)
    assert system_msg is not None
    cleaned = system_msg["content"]
    assert "Agent Identity" in cleaned
    assert "RETRIEVAL HEADLINE" in cleaned
    assert "长期记忆" in cleaned
    assert "AGENTS.md" not in cleaned
    assert "SOUL.md" not in cleaned
    assert "PROFILE.md" not in cleaned

    # 模板已持久化
    assert (templates_dir / "role" / "identity.txt").exists()
    assert (templates_dir / "constraints" / "retrieval_headline.txt").exists()
    assert (templates_dir / "tools" / "web_search.txt").exists()

    # tools 元数据仍保留完整 schema
    tools = payload["metadata"]["tools"]
    assert any(t["function"]["name"] == "web_search" for t in tools)

    # qf_stats 包含清洗统计
    assert payload["metadata"]["qf_stats"]["framework_sections"] > 0


def test_process_creates_qf_output_dir(tmp_path: Path) -> None:
    """qf_output_dir 不存在时应自动创建."""
    queue = SQLiteQueue(tmp_path / "q.db")
    qf_out = tmp_path / "deep" / "nested" / "qf_out"  # 不存在
    templates_dir = tmp_path / "templates"
    _seed_trajectory(queue, tmp_path, "r1__sess.json", "sess")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, system_templates_dir=templates_dir)

    [task] = w.pull()
    out_path = w.process(task)
    assert out_path.exists()


def test_mark_done_transitions_to_pending_gdr(env) -> None:
    queue, qf_out, tmp_path, templates_dir = env
    tid = _seed_trajectory(queue, tmp_path, "r1__sess.json", "sess")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, system_templates_dir=templates_dir)

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
    queue, qf_out, tmp_path, templates_dir = env
    _seed_trajectory(queue, tmp_path, "r1__a.json", "a")
    _seed_trajectory(queue, tmp_path, "r1__b.json", "b")
    _seed_trajectory(queue, tmp_path, "r1__c.json", "c")

    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out,
                 system_templates_dir=templates_dir, n=10)
    success = w.run_once()
    assert success == 3
    counts = queue.count_by_state()
    assert counts.get(STATE_PENDING_GDR) == 3
    assert counts.get(STATE_PENDING, 0) == 0
    # qf_out 应有 3 个文件
    assert len(list(qf_out.glob("*.json"))) == 3


def test_run_once_no_tasks_returns_zero(env) -> None:
    queue, qf_out, _, templates_dir = env
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, system_templates_dir=templates_dir)
    assert w.run_once() == 0


# ---------------------------------------------------------------------------
# 失败 / 重试 / dead
# ---------------------------------------------------------------------------

def test_run_once_handles_process_failure_via_mark_failed(env) -> None:
    """process 抛异常 → mark_failed(stage=qf) → attempts_qf=1, state=pending."""
    queue, qf_out, tmp_path, templates_dir = env
    tid = _seed_trajectory(queue, tmp_path, "r1__a.json", "a")
    # 让 trajectory 内容不可解析 → load_trajectory 抛 ValueError("no parseable events")
    # 旧 etl.pawsession 直接 json.loads 全文件 → JSONDecodeError; 新 loader 改用大括号
    # 深度计数 + 显式 ValueError, 错误更清晰且与 docstring 契约一致.
    (tmp_path / "r1__a.json").write_text("{not json", encoding="utf-8")
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, system_templates_dir=templates_dir)
    success = w.run_once()
    assert success == 0  # 失败不计成功

    refreshed = queue.get(tid)
    assert refreshed is not None
    assert refreshed.state == STATE_PENDING
    assert refreshed.attempts_qf == 1
    assert "no parseable events" in (refreshed.error_msg or "")


def test_run_once_dead_after_max_retries(env) -> None:
    queue, qf_out, tmp_path, templates_dir = env
    tid = _seed_trajectory(queue, tmp_path, "r1__a.json", "a")
    (tmp_path / "r1__a.json").write_text("{bad", encoding="utf-8")

    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out, system_templates_dir=templates_dir)
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
    queue, qf_out, tmp_path, templates_dir = env
    _seed_trajectory(queue, tmp_path, "r1__a.json", "a")

    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out,
                 system_templates_dir=templates_dir, poll_seconds=0.05)
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(timeout=1.0)

    assert not t.is_alive()
    assert queue.count_pending_gdr() == 1


def test_run_forever_processes_later_added_tasks(env) -> None:
    queue, qf_out, tmp_path, templates_dir = env
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out,
                 system_templates_dir=templates_dir, poll_seconds=0.05)
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()

    time.sleep(0.1)
    _seed_trajectory(queue, tmp_path, "r1__late.json", "late")
    time.sleep(0.2)
    stop.set()
    t.join(timeout=1.0)

    assert queue.count_pending_gdr() == 1
    assert (qf_out / "late.json").exists()