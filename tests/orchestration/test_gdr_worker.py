"""orchestration.workers.gdr_worker 单元测试 (ST-3).

通过 monkeypatch ``gdr.pipeline.runner._process_one_file`` 避免依赖真实 LLM endpoint.

新接口::

    run_gdr_once(*, src_path, refined_dir, gdr_settings, task_id, session_id)
        -> GdrResult
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gdr.config.settings import Settings
from orchestration.workers.gdr_worker import (
    GdrNonRetryableError,
    GdrResult,
    RetryableGdrError,
    run_gdr_once,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path):
    refined_dir = tmp_path / "refined"
    settings = Settings(
        llm_base_url="http://localhost:0/v1",
        llm_api_key="x",
        main_model="m",
        tool_model="m",
        judge_model="m",
        workers=2,          # 显式传非 1, 测试 run_gdr_once 强制 workers=1
        llm_concurrency=4,
        max_files=99,       # 显式传非 1, 测试强制 max_files=1
    )
    return tmp_path, refined_dir, settings


def _make_trajectory(tmp_path: Path, session_id: str) -> Path:
    """造一个最小 trajectory JSONL (gdr 输入). 文件存在即可, 解析 stub 在 monkeypatch 里."""
    fp = tmp_path / f"{session_id}.json"
    fp.write_text("{}\n", encoding="utf-8")
    return fp


def _write_c2_output(output_path: Path) -> dict:
    """模拟真实 gdr: 写 C2 refined Session 单文件并返回 result dict."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        '{"session_id":"s1","messages":[],"schema_version":"refined_session.v1"}',
        encoding="utf-8",
    )
    return {"status": "success", "output": str(output_path)}


def _mock_gdr(monkeypatch: pytest.MonkeyPatch, fake_process_one: Any) -> None:
    """同时 mock ``from_trajectory`` (run_gdr_once 前置校验) + ``_process_one_file``."""
    monkeypatch.setattr(
        "orchestration.workers.gdr_worker.from_trajectory",
        lambda path: None,
    )
    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        fake_process_one,
    )


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


def test_run_gdr_once_success(env, monkeypatch) -> None:
    """run_gdr_once 成功: 写入 C2, 返 GdrResult 含 refined_path / duration."""
    tmp_path, refined_dir, settings = env
    src = _make_trajectory(tmp_path, "s1")

    captured: dict = {}

    def fake_process_one(input_path, output_path, cfg):
        captured["input"] = input_path
        captured["output"] = output_path
        captured["cfg_workers"] = cfg.workers
        captured["cfg_max_files"] = cfg.max_files
        captured["cfg_batch_output_dir"] = cfg.batch_output_dir
        return _write_c2_output(output_path)

    _mock_gdr(monkeypatch, fake_process_one)

    result = run_gdr_once(
        src_path=src, refined_dir=refined_dir, gdr_settings=settings,
        task_id="T001", session_id="s1",
    )

    assert isinstance(result, GdrResult)
    assert result.task_id == "T001"
    assert result.session_id == "s1"
    assert result.refined_path.name == "T001__s1.json"
    assert result.refined_path.exists()
    assert result.duration_seconds >= 0
    # 输入路径透传
    assert captured["input"] == src
    # 强制 cfg.workers=1 / max_files=1
    assert captured["cfg_workers"] == 1
    assert captured["cfg_max_files"] == 1
    # batch_output_dir 锚到 refined_dir
    assert Path(captured["cfg_batch_output_dir"]) == refined_dir


def test_run_gdr_once_creates_refined_dir(env, monkeypatch) -> None:
    """refined_dir 不存在时, run_gdr_once 应自动创建."""
    tmp_path, refined_dir, settings = env
    src = _make_trajectory(tmp_path, "s1")
    # refined_dir 不创建, 让 run_gdr_once 内部创建
    assert not refined_dir.exists()

    _mock_gdr(monkeypatch, lambda i, o, c: _write_c2_output(o))

    result = run_gdr_once(
        src_path=src, refined_dir=refined_dir, gdr_settings=settings,
        task_id="T001", session_id="s1",
    )
    assert refined_dir.exists()
    assert result.refined_path.exists()


def test_run_gdr_once_filename_sanitizes_unsafe_chars(env, monkeypatch) -> None:
    """文件名非法字符 (含 ``/`` 等) 应 sanitize 为 ``_``."""
    tmp_path, refined_dir, settings = env
    # 不走 _make_trajectory (该辅助函数自己就会因 session_id 含 '/' 报错);
    # 直接造一个物理文件 + 传一个虚拟 src_path 给 run_gdr_once,
    # 焦点是 task_id / session_id 的 sanitize 行为, 不是 src 文件内容.
    safe_src = _make_trajectory(tmp_path, "s1")
    src = tmp_path / "s" / "1.json"  # 物理上不存在的路径, 仅用于验证 sanitize 命名
    _mock_gdr(monkeypatch, lambda i, o, c: _write_c2_output(o))

    result = run_gdr_once(
        src_path=safe_src, refined_dir=refined_dir, gdr_settings=settings,
        task_id="T/001", session_id="s/1",
    )
    # 文件名应 sanitize
    assert "/" not in result.refined_path.name
    assert result.refined_path.name == "T_001__s_1.json"


# ---------------------------------------------------------------------------
# 异常路径 - 永久性 (GdrNonRetryableError)
# ---------------------------------------------------------------------------


def test_run_gdr_once_missing_src_raises_non_retryable(env, monkeypatch) -> None:
    """src 不存在 → GdrNonRetryableError (永久)."""
    tmp_path, refined_dir, settings = env
    src = tmp_path / "missing.json"  # 不创建

    _mock_gdr(monkeypatch, lambda *a, **kw: {"status": "success"})

    with pytest.raises(GdrNonRetryableError, match="trajectory missing"):
        run_gdr_once(
            src_path=src, refined_dir=refined_dir, gdr_settings=settings,
            task_id="T001", session_id="s1",
        )


def test_run_gdr_once_trajectory_parse_error_is_non_retryable(env, monkeypatch) -> None:
    """from_trajectory 抛 ValueError → GdrNonRetryableError (永久)."""
    tmp_path, refined_dir, settings = env
    src = _make_trajectory(tmp_path, "s1")

    def boom_from_trajectory(_path):
        raise ValueError("bad json")
    monkeypatch.setattr(
        "orchestration.workers.gdr_worker.from_trajectory",
        boom_from_trajectory,
    )
    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        lambda *a, **kw: {"status": "success"},
    )

    with pytest.raises(GdrNonRetryableError, match="parse failed"):
        run_gdr_once(
            src_path=src, refined_dir=refined_dir, gdr_settings=settings,
            task_id="T001", session_id="s1",
        )


def test_run_gdr_once_load_error_status_is_non_retryable(env, monkeypatch) -> None:
    """gdr.status='load_error' → GdrNonRetryableError (永久)."""
    tmp_path, refined_dir, settings = env
    src = _make_trajectory(tmp_path, "s1")
    _mock_gdr(monkeypatch, lambda i, o, c: {"status": "load_error", "error": "bad"})

    with pytest.raises(GdrNonRetryableError, match="load_error"):
        run_gdr_once(
            src_path=src, refined_dir=refined_dir, gdr_settings=settings,
            task_id="T001", session_id="s1",
        )


def test_run_gdr_once_discard_status_is_non_retryable(env, monkeypatch) -> None:
    """gdr.status='discard' → GdrNonRetryableError (永久)."""
    tmp_path, refined_dir, settings = env
    src = _make_trajectory(tmp_path, "s1")
    _mock_gdr(monkeypatch, lambda i, o, c: {"status": "discard"})

    with pytest.raises(GdrNonRetryableError, match="discard"):
        run_gdr_once(
            src_path=src, refined_dir=refined_dir, gdr_settings=settings,
            task_id="T001", session_id="s1",
        )


def test_run_gdr_once_incomplete_status_is_non_retryable(env, monkeypatch) -> None:
    """gdr.status='incomplete' → GdrNonRetryableError (永久)."""
    tmp_path, refined_dir, settings = env
    src = _make_trajectory(tmp_path, "s1")
    _mock_gdr(monkeypatch, lambda i, o, c: {"status": "incomplete"})

    with pytest.raises(GdrNonRetryableError, match="incomplete"):
        run_gdr_once(
            src_path=src, refined_dir=refined_dir, gdr_settings=settings,
            task_id="T001", session_id="s1",
        )


# ---------------------------------------------------------------------------
# 异常路径 - 可重试 (RetryableGdrError)
# ---------------------------------------------------------------------------


def test_run_gdr_once_save_error_is_retryable(env, monkeypatch) -> None:
    """gdr.status='save_error' → RetryableGdrError (可重试)."""
    tmp_path, refined_dir, settings = env
    src = _make_trajectory(tmp_path, "s1")
    _mock_gdr(monkeypatch, lambda i, o, c: {"status": "save_error", "error": "io"})

    with pytest.raises(RetryableGdrError, match="save_error"):
        run_gdr_once(
            src_path=src, refined_dir=refined_dir, gdr_settings=settings,
            task_id="T001", session_id="s1",
        )


def test_run_gdr_once_returns_none_is_retryable(env, monkeypatch) -> None:
    """gdr 返回 None (软超时部分保存) → RetryableGdrError (可重试)."""
    tmp_path, refined_dir, settings = env
    src = _make_trajectory(tmp_path, "s1")
    _mock_gdr(monkeypatch, lambda i, o, c: None)

    with pytest.raises(RetryableGdrError, match="None"):
        run_gdr_once(
            src_path=src, refined_dir=refined_dir, gdr_settings=settings,
            task_id="T001", session_id="s1",
        )


def test_run_gdr_once_unknown_status_is_retryable(env, monkeypatch) -> None:
    """gdr.status 未知字符串 → RetryableGdrError (保守)."""
    tmp_path, refined_dir, settings = env
    src = _make_trajectory(tmp_path, "s1")
    _mock_gdr(monkeypatch, lambda i, o, c: {"status": "something_weird"})

    with pytest.raises(RetryableGdrError, match="something_weird"):
        run_gdr_once(
            src_path=src, refined_dir=refined_dir, gdr_settings=settings,
            task_id="T001", session_id="s1",
        )


# ---------------------------------------------------------------------------
# 边界场景
# ---------------------------------------------------------------------------


def test_run_gdr_once_unexpected_exception_propagates(env, monkeypatch) -> None:
    """_process_one_file 抛未捕获异常 → 透传 (PipelineExecutor 兜底)."""
    tmp_path, refined_dir, settings = env
    src = _make_trajectory(tmp_path, "s1")

    def boom(*a, **kw):
        raise RuntimeError("gdr exploded")
    _mock_gdr(monkeypatch, boom)

    with pytest.raises(RuntimeError, match="gdr exploded"):
        run_gdr_once(
            src_path=src, refined_dir=refined_dir, gdr_settings=settings,
            task_id="T001", session_id="s1",
        )


def test_run_gdr_once_success_uses_settings_output_path(env, monkeypatch) -> None:
    """gdr 返回 status='success' 但 output field 缺失时, 退回计算的 out_path."""
    tmp_path, refined_dir, settings = env
    src = _make_trajectory(tmp_path, "s1")

    # 返回 success 不带 output, run_gdr_once 应兜底到本地 out_path
    monkeypatch.setattr(
        "orchestration.workers.gdr_worker.from_trajectory",
        lambda path: None,
    )
    def fake_no_output(input_path, output_path, cfg):
        # 模拟 gdr 写文件但不返回 output field
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("{}", encoding="utf-8")
        return {"status": "success"}  # 没有 output field
    monkeypatch.setattr(
        "orchestration.workers.gdr_worker._process_one_file",
        fake_no_output,
    )

    result = run_gdr_once(
        src_path=src, refined_dir=refined_dir, gdr_settings=settings,
        task_id="T001", session_id="s1",
    )
    # 兜底到本地计算路径
    assert result.refined_path.name == "T001__s1.json"
    assert result.refined_path.exists()


def test_run_gdr_once_is_pure_module_function(env, monkeypatch) -> None:
    """run_gdr_once 不维护任何状态; 多次调用互不影响."""
    tmp_path, refined_dir, settings = env
    src_a = _make_trajectory(tmp_path, "s_a")
    src_b = _make_trajectory(tmp_path, "s_b")
    _mock_gdr(monkeypatch, lambda i, o, c: _write_c2_output(o))

    r_a = run_gdr_once(
        src_path=src_a, refined_dir=refined_dir, gdr_settings=settings,
        task_id="T_A", session_id="s_a",
    )
    r_b = run_gdr_once(
        src_path=src_b, refined_dir=refined_dir, gdr_settings=settings,
        task_id="T_B", session_id="s_b",
    )

    assert r_a.refined_path != r_b.refined_path
    assert r_a.task_id == "T_A"
    assert r_b.task_id == "T_B"
    # 两个 C2 文件都存在
    assert r_a.refined_path.exists()
    assert r_b.refined_path.exists()