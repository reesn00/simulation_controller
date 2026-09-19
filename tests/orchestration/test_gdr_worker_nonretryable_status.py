"""回归测试: gdr worker 把 incomplete 状态视为 NonRetryableError.

复现 2026-09-19 T001 useramulation-fe81...:
  - 第一轮 gdr refine: incomplete detector 触发 → status='incomplete' →
    旁路到 refine_data/incomplete.jsonl → refine_data 跳过
  - gdr worker.process() 当时只把 load_error/discard 当 NonRetryableError,
    status='incomplete' 走 RuntimeError → mark_failed(stage=gdr, retryable=True)
    → 重试一次
  - 第二轮 refine 又失败 (discard, consistency_score=2) → dead
  - 浪费 ~5 分钟 LLM 调用 + judge, 且 incomplete.jsonl 重复写

修复: gdr_worker.process() 把 'incomplete' 加入 NonRetryableError 白名单,
     与 load_error / discard 并列; 一旦命中就 dead, 不再 refine_data 重跑.

覆盖:
  - status='incomplete' → NonRetryableError 抛出
  - status='discard'    → NonRetryableError 抛出 (回归, 不应被白名单改动破坏)
  - status='load_error' → NonRetryableError 抛出 (回归)
  - status='save_error' → RuntimeError 抛出 (回归, 仍可重试)
  - status='success'    → 不抛 (返回 outputs)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestration.errors import NonRetryableError
from orchestration.queue import SQLiteQueue, Task
from orchestration.workers.gdr_worker import GdrWorker


class _StubTask:
    """最小可用 Task, 只覆盖 gdr_worker.process 读到的字段."""

    def __init__(self, qf_path: Path, task_id: int = 1):
        self.id = task_id
        self.src_path = qf_path
        self.qf_output_path = str(qf_path)
        self.session_id = "test_session"
        self.run_id = "test_run"
        self.batch_id = 1


def _make_worker(tmp_path: Path, *, qf_file: Path) -> GdrWorker:
    """构造一个 GdrWorker, process() 内部会读 qf_file."""
    db = tmp_path / "q.db"
    queue = SQLiteQueue(db, max_retry_qf=1, max_retry_gdr=1)
    return GdrWorker(
        queue=queue,
        worker_id="gdr_test",
        gdr_output_dir=tmp_path / "gdr_out",
    ), queue


def _patch_process(monkeypatch, status: str, error: str = ""):
    """让 _process_one_file 返回指定 status 的 dict."""
    def fake(_qf_input, _base_path, _cfg):
        return {"status": status, "error": error, "outputs": {}}
    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        fake,
    )


# ---------------------------------------------------------------------------
# 白名单内: load_error / discard / incomplete → NonRetryableError
# ---------------------------------------------------------------------------


def test_incomplete_status_raises_nonretryable(tmp_path, monkeypatch):
    """status='incomplete' → NonRetryableError, 不重试.

    复现: incomplete detector 已旁路到 incomplete.jsonl, 重跑只会再命中一次,
    不改变 outcome. NonRetryable 标记后 attempts 不递增, task 直接 dead.
    """
    qf = tmp_path / "qf_out.json"
    qf.write_text("{}", encoding="utf-8")
    worker, _q = _make_worker(tmp_path, qf_file=qf)
    _patch_process(monkeypatch, status="incomplete")

    with pytest.raises(NonRetryableError) as exc_info:
        worker.process(_StubTask(qf))
    msg = str(exc_info.value)
    assert "incomplete" in msg
    assert "gdr status" in msg


def test_discard_status_raises_nonretryable(tmp_path, monkeypatch):
    """回归: status='discard' 仍走 NonRetryableError (白名单改动不应破坏)."""
    qf = tmp_path / "qf_out.json"
    qf.write_text("{}", encoding="utf-8")
    worker, _q = _make_worker(tmp_path, qf_file=qf)
    _patch_process(monkeypatch, status="discard", error="consistency_score=2 < 7")

    with pytest.raises(NonRetryableError) as exc_info:
        worker.process(_StubTask(qf))
    assert "discard" in str(exc_info.value)


def test_load_error_status_raises_nonretryable(tmp_path, monkeypatch):
    """回归: status='load_error' 仍走 NonRetryableError."""
    qf = tmp_path / "qf_out.json"
    qf.write_text("{}", encoding="utf-8")
    worker, _q = _make_worker(tmp_path, qf_file=qf)
    _patch_process(monkeypatch, status="load_error", error="bad json")

    with pytest.raises(NonRetryableError) as exc_info:
        worker.process(_StubTask(qf))
    assert "load_error" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 非白名单: save_error 等仍走 RuntimeError (可重试)
# ---------------------------------------------------------------------------


def test_save_error_status_raises_runtimeerror_retryable(tmp_path, monkeypatch):
    """回归: status='save_error' 走 RuntimeError (可重试, 白名单不应误伤)."""
    qf = tmp_path / "qf_out.json"
    qf.write_text("{}", encoding="utf-8")
    worker, _q = _make_worker(tmp_path, qf_file=qf)
    _patch_process(monkeypatch, status="save_error", error="disk full")

    with pytest.raises(RuntimeError) as exc_info:
        worker.process(_StubTask(qf))
    assert not isinstance(exc_info.value, NonRetryableError), (
        "save_error 应保持可重试 (RuntimeError), 不应被新白名单误伤"
    )
    assert "save_error" in str(exc_info.value)


def test_unknown_status_raises_runtimeerror_retryable(tmp_path, monkeypatch):
    """未知 status 走 RuntimeError (可重试), 给运维重命名/迁移留时间窗."""
    qf = tmp_path / "qf_out.json"
    qf.write_text("{}", encoding="utf-8")
    worker, _q = _make_worker(tmp_path, qf_file=qf)
    _patch_process(monkeypatch, status="weird_new_status", error="?")

    with pytest.raises(RuntimeError) as exc_info:
        worker.process(_StubTask(qf))
    assert not isinstance(exc_info.value, NonRetryableError)
    assert "weird_new_status" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 成功路径不应触发异常
# ---------------------------------------------------------------------------


def test_success_status_returns_outputs(tmp_path, monkeypatch):
    """回归: status='success' 不抛, 走正常 mark_done 路径."""
    qf = tmp_path / "qf_out.json"
    qf.write_text("{}", encoding="utf-8")
    gdr_out = tmp_path / "gdr_out"
    gdr_out.mkdir()
    messages_path = gdr_out / "session_refined.messages.json"
    openai_path = gdr_out / "session_refined.openai.json"
    meta_path = gdr_out / "session_refined.meta.json"
    for p in (messages_path, openai_path, meta_path):
        p.write_text("{}", encoding="utf-8")

    worker, _q = _make_worker(tmp_path, qf_file=qf)
    worker._last_outputs = {
        "messages": str(messages_path),
        "openai": str(openai_path),
        "qwenjina": None,
        "meta": str(meta_path),
    }

    def fake(_qf_input, _base_path, _cfg):
        return {
            "status": "success",
            "outputs": {
                "messages": str(messages_path),
                "openai": str(openai_path),
                "qwenjina": None,
                "meta": str(meta_path),
            },
        }
    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        fake,
    )

    result = worker.process(_StubTask(qf))
    assert result == Path(messages_path)


# ---------------------------------------------------------------------------
# NonRetryableError 落到 mark_failed 时 attempts 不递增
# ---------------------------------------------------------------------------


def test_incomplete_does_not_consume_retry_budget(tmp_path, monkeypatch):
    """NonRetryableError 走 mark_failed 时 attempts 不递增, 直接 dead.

    验证修复的副作用: incomplete 一次就 dead, 不浪费 retry 配额去跑第二轮
    (第二轮 refine 必然再命中 detector 或同类问题).
    """
    qf = tmp_path / "qf_out.json"
    qf.write_text("{}", encoding="utf-8")
    worker, queue = _make_worker(tmp_path, qf_file=qf)
    _patch_process(monkeypatch, status="incomplete")

    # 队列里塞一条 task
    src = tmp_path / "src.json"
    src.write_text("{}", encoding="utf-8")
    task_id, _ = queue.insert(
        src_path=src, run_id="r", session_id="s", batch_id=1,
    )
    queue.pull_pending_qf(worker_id="seed", n=1)
    queue.mark_qf_done(task_id, qf_output_path=qf)

    # 跑 process → NonRetryableError → _handle_failure → mark_failed
    from orchestration.workers.base_worker import BaseWorker
    task = queue.get(task_id)
    # 把 qf_output_path 指到 qf, 因为 Task dataclass 用过时的 schema
    # 这里直接构造一个 _StubTask 给 worker.process
    fake_task = _StubTask(qf, task_id=task_id)
    with pytest.raises(NonRetryableError):
        worker.process(fake_task)
    BaseWorker._handle_failure(worker, fake_task, NonRetryableError("gdr status='incomplete'"))

    # attempts_gdr 不应递增 (NonRetryable 标记), state 应直接 dead
    row = queue.get(task_id)
    assert row.attempts_gdr == 0, (
        f"incomplete 走 NonRetryableError, attempts 不应递增, 实际 {row.attempts_gdr}"
    )
    assert row.state == "dead"