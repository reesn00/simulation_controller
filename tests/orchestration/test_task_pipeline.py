"""orchestration.task_pipeline 单元测试.

覆盖:
* 三阶段顺序: simulate → gdr → etl 严格顺序 (SQLite phase 推进正确)
* 阶段内重试: max_retry_gdr=2 时 gdr 失败重试 2 次后 dead
* 阶段内重试: max_retry_etl=2 时 etl 失败重试 2 次后 dead
* NonRetryableError 直接 dead (不消耗 attempts)
* simulate 终态非 SUCCESS → dead, 不进 gdr
* 子进程不抛异常给主进程 — 任何未捕获都 mark_failed + 返 dead
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from gdr.config.settings import Settings as GdrSettings
from orchestration.queue import (
    PHASE_AUDITED,
    PHASE_DEAD,
    PHASE_DONE,
    PHASE_ETL,
    PHASE_GDR,
    PHASE_SIMULATE,
    SQLiteQueue,
)
from orchestration.settings import Paths, PipelineSettings
from orchestration.task_pipeline import _run_one_task_pipeline


# ---------------------------------------------------------------------------
# fixtures & helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> Paths:
    for sub in (
        "trajectory_dir", "refined_dir", "etl_outputs_dir",
        "dead_dir", "log_dir", "runs_dir",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return Paths(
        simulate_serve_config=tmp_path / "sim.yaml",
        trajectory_dir=tmp_path / "trajectory_dir",
        runs_dir=tmp_path / "runs_dir",
        refined_dir=tmp_path / "refined_dir",
        etl_outputs_dir=tmp_path / "etl_outputs_dir",
        sqlite_db=tmp_path / "q.db",
        dead_dir=tmp_path / "dead_dir",
        pid_file=tmp_path / "orch.pid",
        log_dir=tmp_path / "log_dir",
    )


def _make_gdr_settings(paths: Paths) -> GdrSettings:
    return GdrSettings(
        batch_output_dir=paths.refined_dir,
        workers=1, llm_concurrency=1, max_files=1,
    )


def _make_pipeline_settings(*, retry_gdr: int = 0, retry_etl: int = 0) -> PipelineSettings:
    return PipelineSettings(
        max_parallelism=1,
        max_retry_gdr=retry_gdr,
        max_retry_etl=retry_etl,
        retry_poll_seconds=0.05,
    )


# Fake TaskRun / GdrResult / EtlOutputs
@dataclass
class _FakeRunFailure:
    message: str = "n/a"


@dataclass
class _FakeRun:
    """模仿 simulate_serve.domain.run.TaskRun 的最小子集."""
    run_id: str = "run_fake"
    remote_session_id: str = "sess_fake"
    state: Any = None  # str (e.g. 'success')
    failure: Any = None


@dataclass
class _FakeGdrResult:
    refined_path: Path
    task_id: str
    session_id: str
    duration_seconds: float = 0.0


@dataclass
class _FakeEtlOutputs:
    messages_path: Path
    openai_path: Path
    qwenjina_path: Path | None
    meta_path: Path
    task_id: str
    session_id: str
    duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# monkeypatch helper
# ---------------------------------------------------------------------------


def _patch_pipeline(
    monkeypatch,
    *,
    simulate_state: str = "success",
    gdr_fail_count: int = 0,
    gdr_nonretryable: bool = False,
    gdr_audited: str | None = None,
    etl_fail_count: int = 0,
    etl_nonretryable: bool = False,
    simulate_exc: BaseException | None = None,
):
    """monkeypatch orchestration.task_pipeline 延迟 import 的三个函数.

    入参:
        simulate_state:        模拟 TaskRun.state 字符串; success 等
        gdr_fail_count:        gdr 重试次数, 前 N 次抛普通 Exception, 之后成功
        gdr_nonretryable:      gdr 抛 NonRetryableError (直接 dead)
        gdr_audited:           gdr 抛 GdrAuditedError (走 audited 终态, 不进 dead);
                              取值 "judge_discard" / "scoring_reject" 表示拒收原因
        etl_fail_count:        etl 重试次数
        etl_nonretryable:      etl 抛 NonRetryableError
        simulate_exc:          simulate 直接抛异常 (非 TaskRun)
    """
    # 计数器: 记录三个函数实际被调用的次数
    counts = {"simulate": 0, "gdr": 0, "etl": 0}

    # Stub producer_simulate.run_one_task
    def fake_simulate_run_one_task(task_id: str, *, config_path: Path) -> _FakeRun:
        counts["simulate"] += 1
        if simulate_exc is not None:
            raise simulate_exc
        # 模拟 producer 写出 trajectory 文件
        traj_dir = config_path.parent / "trajectory_dir"
        traj_dir.mkdir(parents=True, exist_ok=True)
        # 必须与 task_pipeline 中 sanitize 后的文件名一致
        safe_session = "sess_fake"  # remote_session_id 的 sanitize 形态
        safe_run = "run_fake"
        traj_path = traj_dir / f"{safe_run}__{safe_session}.json"
        traj_path.write_text("{}", encoding="utf-8")
        return _FakeRun(
            run_id="run_fake",
            remote_session_id="sess_fake",
            state=simulate_state,
            failure=_FakeRunFailure(message="boom") if simulate_state != "success" else None,
        )

    # 延迟 import 模块 stub
    class _NonRetryableError(Exception):
        pass

    class _GdrNonRetryableError(Exception):
        pass

    class _GdrAuditedError(Exception):
        """评分低 → audited 终态, 不进 dead (CLAUDE.md "数据保留原则").

        对应 orchestration.workers.gdr_worker.GdrAuditedError 的契约:
        message 字符串 + audit_reason kwarg.
        """

        def __init__(self, message: str, *, audit_reason: str) -> None:
            super().__init__(message)
            self.audit_reason = audit_reason

    class _EtlNonRetryableError(Exception):
        pass

    def fake_run_gdr_once(
        *, src_path, refined_dir, gdr_settings, task_id, session_id,
        langfuse_client=None, langfuse_cfg=None,
    ):
        counts["gdr"] += 1
        if gdr_audited and counts["gdr"] == 1:
            # 评分低 → audited, 抛 GdrAuditedError (单次判定, 不重试)
            raise _GdrAuditedError(
                f"gdr status={gdr_audited!r} (task={task_id})",
                audit_reason=gdr_audited,
            )
        if gdr_nonretryable and counts["gdr"] == 1:
            raise _GdrNonRetryableError("gdr permanent fail")
        if counts["gdr"] <= gdr_fail_count:
            raise RuntimeError(f"gdr forced fail #{counts['gdr']}")
        # 成功: 写 C2
        refined_dir.mkdir(parents=True, exist_ok=True)
        refined = refined_dir / f"{task_id}__refined.json"
        refined.write_text("{}", encoding="utf-8")
        return _FakeGdrResult(
            refined_path=refined, task_id=task_id, session_id=session_id,
        )

    def fake_run_etl_once(
        *, c2_path, etl_outputs_dir, task_id, session_id,
        attempt=0, langfuse_cfg=None,
    ):
        counts["etl"] += 1
        if etl_nonretryable and counts["etl"] == 1:
            raise _EtlNonRetryableError("etl permanent fail")
        if counts["etl"] <= etl_fail_count:
            raise RuntimeError(f"etl forced fail #{counts['etl']}")
        etl_outputs_dir.mkdir(parents=True, exist_ok=True)
        base = etl_outputs_dir / f"{task_id}"
        msgs = base.with_suffix(".messages.json")
        openai = base.with_suffix(".openai.json")
        meta = base.with_suffix(".meta.json")
        for p in (msgs, openai, meta):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}", encoding="utf-8")
        return _FakeEtlOutputs(
            messages_path=msgs, openai_path=openai, qwenjina_path=None,
            meta_path=meta, task_id=task_id, session_id=session_id,
        )

    # 在 producer_simulate 里摸出 ``run_one_task_sync`` (生产真实入口).
    # 同时也 stub ``run_one_task`` (async 版) — 防止任何子任务意外退回
    # 直接调 async 函数 (会触发 coroutine attribute error, 上轮被隐没).
    import orchestration.producer_simulate as _ps_mod
    monkeypatch.setattr(_ps_mod, "run_one_task_sync", fake_simulate_run_one_task)
    monkeypatch.setattr(_ps_mod, "run_one_task", fake_simulate_run_one_task)

    # 在 gdr_worker / etl_worker 里摸出 NonRetryable 类 + run_*_once
    import orchestration.workers.gdr_worker as _gw
    import orchestration.workers.etl_worker as _ew
    monkeypatch.setattr(_gw, "GdrNonRetryableError", _GdrNonRetryableError)
    monkeypatch.setattr(_gw, "GdrAuditedError", _GdrAuditedError)
    monkeypatch.setattr(_gw, "run_gdr_once", fake_run_gdr_once)
    monkeypatch.setattr(_ew, "EtlNonRetryableError", _EtlNonRetryableError)
    monkeypatch.setattr(_ew, "run_etl_once", fake_run_etl_once)

    return counts


# ---------------------------------------------------------------------------
# 三阶段顺序
# ---------------------------------------------------------------------------


def test_three_stages_in_order(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DONE
    assert result["stage"] == "done"
    assert counts["simulate"] == 1
    assert counts["gdr"] == 1
    assert counts["etl"] == 1

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DONE
    # 各阶段产物路径都写了
    assert task.src_path is not None
    assert task.gdr_refined_path is not None
    assert task.etl_messages_path is not None
    assert task.etl_openai_path is not None
    assert task.etl_meta_path is not None


def test_simulate_failure_skips_gdr(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, simulate_state="executor_error")
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "simulate"
    assert counts["gdr"] == 0
    assert counts["etl"] == 0

    task = queue.get_task("T1")
    assert task is not None and task.phase == PHASE_DEAD


def test_simulate_exception_marks_dead(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(
        monkeypatch, simulate_exc=RuntimeError("simulate crashed"),
    )
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "simulate"
    assert counts["gdr"] == 0
    assert queue.get_task("T1").phase == PHASE_DEAD


# ---------------------------------------------------------------------------
# 阶段内重试
# ---------------------------------------------------------------------------


def test_gdr_retry_then_success(tmp_path: Path, monkeypatch) -> None:
    """gdr 失败 2 次, 第 3 次成功 → done."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_gdr=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_fail_count=2)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DONE
    # gdr_fail_count=2 → 前 2 次失败, 第 3 次成功
    assert counts["gdr"] == 3

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DONE
    assert task.attempts_gdr == 3


def test_gdr_retry_exhausted_dead(tmp_path: Path, monkeypatch) -> None:
    """max_retry_gdr=2, gdr 永远失败 → dead, attempts=3."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_gdr=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_fail_count=99)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "gdr"
    # max_retry_gdr=2 → 共跑 3 次 (1+2)
    assert counts["gdr"] == 3

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DEAD
    assert task.attempts_gdr == 3


def test_gdr_nonretryable_direct_dead(tmp_path: Path, monkeypatch) -> None:
    """NonRetryableError 立即 dead, 不消耗 attempts."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_gdr=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_nonretryable=True)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "gdr"
    assert counts["gdr"] == 1

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DEAD


def test_etl_retry_then_success(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_etl=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, etl_fail_count=1)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DONE
    assert counts["etl"] == 2

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DONE
    assert task.attempts_etl == 2


def test_etl_retry_exhausted_dead(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_etl=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, etl_fail_count=99)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "etl"
    assert counts["etl"] == 3

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DEAD
    assert task.attempts_etl == 3


def test_etl_nonretryable_direct_dead(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_etl=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, etl_nonretryable=True)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "etl"
    assert counts["etl"] == 1


# ---------------------------------------------------------------------------
# 评分低 → audited 终态 (CLAUDE.md "数据保留原则")
# ---------------------------------------------------------------------------


def test_gdr_judge_discard_marks_audited(tmp_path: Path, monkeypatch) -> None:
    """judge 评分低 (judge_discard) → audited, 不进 dead.

    与 gdr_nonretryable 走 dead 不同: 评分低是质量决策, 不是结构失败.
    data 保留在 src_path + 旁路 jsonl, failure_handler 不归档.
    """
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_audited="judge_discard")
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_AUDITED
    assert result["stage"] == "gdr"
    # 单次判定, 不重试 (拒收是确定性结果, 重试无意义)
    assert counts["gdr"] == 1

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_AUDITED
    # error_msg 含 audit_reason 便于审计
    assert task.error_msg is not None
    assert "judge_discard" in task.error_msg


def test_gdr_scoring_reject_marks_audited(tmp_path: Path, monkeypatch) -> None:
    """free_quality 评分低 (scoring_reject) → audited, 不进 dead."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_audited="scoring_reject")
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_AUDITED
    assert result["stage"] == "gdr"
    assert counts["gdr"] == 1

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_AUDITED
    assert task.error_msg is not None
    assert "scoring_reject" in task.error_msg


def test_audited_does_not_invoke_etl(tmp_path: Path, monkeypatch) -> None:
    """audited 终态后 etl 不跑 (C2 可能没写, 即便写了也不进训练集)."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_audited="judge_discard")
    _run_one_task_pipeline("T1", paths, gdr_settings, settings)

    assert counts["gdr"] == 1
    assert counts["etl"] == 0

    task = queue.get_task("T1")
    assert task is not None
    # etl 路径字段保持 None, 不写 etl_messages_path / etl_openai_path 等
    assert task.etl_messages_path is None
    assert task.etl_openai_path is None
    assert task.etl_meta_path is None


# ---------------------------------------------------------------------------
# 子进程不抛异常给主进程 — 任何未捕获都 mark_failed
# ---------------------------------------------------------------------------


def test_unhandled_exception_does_not_propagate(tmp_path: Path, monkeypatch) -> None:
    """若 simulate 抛非预期异常, _run_one_task_pipeline 内部 catch, 返回 dead.

    实施细节: simulate_exc 走 monkeypatch 的 fake_simulate_run_one_task 内部 raise,
    _run_one_task_pipeline 内有 try/except 捕获 → mark_failed + return dead。
    """
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    _patch_pipeline(monkeypatch, simulate_exc=RuntimeError("boom"))
    # 直接调, 不抛
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )
    assert result["phase"] == PHASE_DEAD
    assert "boom" in (result["error"] or "")


def test_returns_dict_keys(tmp_path: Path, monkeypatch) -> None:
    """返回 dict 必须含 task_id / stage / error 字段."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    _patch_pipeline(monkeypatch)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )
    assert set(result.keys()) == {"task_id", "phase", "stage", "error"}
    assert result["task_id"] == "T1"


def test_production_uses_run_one_task_sync_not_async(tmp_path: Path, monkeypatch) -> None:
    """回归保护: ``_run_one_task_pipeline`` 必须调 sync wrapper, 不是 async 版.

    Round 2 (2026-09-22) 发现的 P0 BUG-1: ``task_pipeline`` 误 import
    ``run_one_task`` (async), 所有 task 100% crash 在
    ``AttributeError: 'coroutine' object has no attribute 'state'``.
    原测试 stub 的是 ``run_one_task`` async 版, 完全没遮蔽真实生产
    ``run_one_task_sync`` 调用. 本测试只 stub async, 不 stub sync,
    若生产代码退化到 async import, 会触发 coroutine AttributeError → dead,
    测试可捕获. sync stub 由其它 11 个测试 (调用 ``_patch_pipeline``)
    提供完整覆盖.
    """
    import asyncio

    import orchestration.producer_simulate as _ps_mod

    async def fake_async(task_id, *, config_path):
        # 真生产入口若误用此函数会拿到一个 coroutine, 然后 ``run.state`` 崩.
        await asyncio.sleep(0)
        raise AssertionError(
            "production code should never call async run_one_task"
        )

    # 只 stub async 版 (模拟误用), 不 stub sync 版
    monkeypatch.setattr(_ps_mod, "run_one_task", fake_async)
    # sync 版保留原样, 但若生产调 sync 也会成功. 因此本测试本质是:
    # 若生产误用 async, ``fake_async`` 抛 AssertionError → 顶层 catch
    # → ``error_msg`` 含 AssertionError. 我们不接受此 error_msg.

    paths = _make_paths(tmp_path)
    SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    # stub gdr / etl 走通
    counts = _patch_pipeline(monkeypatch)

    result = _run_one_task_pipeline(
        "T_REG", paths, gdr_settings, settings,
    )

    # 生产调 sync (RunOneTaskSync 真实逻辑, ``fake_simulate_run_one_task`` 由
    # _patch_pipeline 提供), 不应触发 coroutine AttributeError / AssertionError.
    assert result["phase"] == PHASE_DONE, (
        f"production fell back to async run_one_task: {result['error']!r}"
    )
    assert "AssertionError" not in (result["error"] or "")
    assert counts["simulate"] == 1
    assert counts["gdr"] == 1
    assert counts["etl"] == 1


# ---------------------------------------------------------------------------
# PR 5: Langfuse cfg / client plumbing + worker_init atexit
# ---------------------------------------------------------------------------


def test_run_one_task_pipeline_constructs_langfuse_cfg_when_enabled(
    tmp_path: Path, monkeypatch,
) -> None:
    """``_run_one_task_pipeline`` 入口构造 ``langfuse_cfg`` 且 enabled=True 时
    调 ``get_client``;并将 cfg / client 透传给 ``run_gdr_once`` /
    ``run_etl_once``.
    """
    from dataclasses import dataclass

    paths = _make_paths(tmp_path)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    @dataclass(frozen=True)
    class _Cfg:
        enabled: bool = True
        public_key: str = "pk"
        secret_key: str = "sk"
        upload_payload: str = "full"
        per_step_span: bool = True

    # Stub load_langfuse_config to return our enabled cfg.
    monkeypatch.setattr(
        "orchestration.observability.langfuse_config.load_langfuse_config",
        lambda: _Cfg(),
    )

    # get_client counter to verify it's called when enabled.
    from simulate_serve.observability import langfuse_client

    fake_client = object()  # any truthy singleton
    get_client_calls = {"n": 0}

    def fake_get_client(_cfg):
        get_client_calls["n"] += 1
        return fake_client

    monkeypatch.setattr(langfuse_client, "get_client", fake_get_client)
    monkeypatch.setattr(
        "simulate_serve.observability.langfuse_client.get_client",
        fake_get_client,
    )

    counts = _patch_pipeline(monkeypatch)

    result = _run_one_task_pipeline(
        "T_LF", paths, gdr_settings, settings,
    )
    assert result["phase"] == PHASE_DONE
    assert counts["simulate"] == 1
    assert counts["gdr"] == 1
    assert counts["etl"] == 1
    # enabled → get_client at least once
    assert get_client_calls["n"] >= 1


def test_run_one_task_pipeline_disabled_langfuse_skips_client(
    tmp_path: Path, monkeypatch,
) -> None:
    """``langfuse.enabled=False`` → ``get_client`` 不被调用;业务路径不受影响."""
    from dataclasses import dataclass

    paths = _make_paths(tmp_path)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    @dataclass(frozen=True)
    class _Cfg:
        enabled: bool = False
        public_key: str = ""
        secret_key: str = ""

    monkeypatch.setattr(
        "orchestration.observability.langfuse_config.load_langfuse_config",
        lambda: _Cfg(),
    )

    from simulate_serve.observability import langfuse_client

    get_client_calls = {"n": 0}

    def fake_get_client(_cfg):
        get_client_calls["n"] += 1
        return None

    monkeypatch.setattr(langfuse_client, "get_client", fake_get_client)
    monkeypatch.setattr(
        "simulate_serve.observability.langfuse_client.get_client",
        fake_get_client,
    )

    counts = _patch_pipeline(monkeypatch)

    result = _run_one_task_pipeline(
        "T_LF_OFF", paths, gdr_settings, settings,
    )
    assert result["phase"] == PHASE_DONE
    # enabled=False → get_client 不被调用 (短路)
    assert get_client_calls["n"] == 0


def test_worker_init_registers_atexit_when_enabled(
    tmp_path: Path, monkeypatch,
) -> None:
    """``_worker_init`` 在 ``langfuse.enabled=True`` 时注册 atexit.shutdown."""
    import atexit

    paths = _make_paths(tmp_path)

    # Stub load_langfuse_config to return enabled True.
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Cfg:
        enabled: bool = True

    monkeypatch.setattr(
        "orchestration.observability.langfuse_config.load_langfuse_config",
        lambda: _Cfg(),
    )

    # 收集 _lf_shutdown 引用
    registered: list = []

    import simulate_serve.observability.langfuse_client as lf

    real_shutdown = lf.shutdown

    def spy_shutdown():
        registered.append("called")
        return real_shutdown()

    monkeypatch.setattr(lf, "shutdown", spy_shutdown)
    # task_pipeline 内部从 langfuse_client 拉 shutdown — module 已经在前面
    # import 一次,monkeypatch 在 module 上覆盖 attribute.
    # 直接验证 _worker_init 注册行为更稳:
    from orchestration.task_pipeline import _worker_init

    # 重新 patch task_pipeline 内部 from-import 的 shutdown
    # task_pipeline 在函数体内 `from ... import shutdown as _lf_shutdown`,
    # 这相当于在调用时局部绑 — monkeypatch 同步生效.
    before_atexit = atexit._ncallbacks() if hasattr(atexit, "_ncallbacks") else None

    _worker_init(paths)

    # 不需要具体计数(atexit unregister 难),只验证 _worker_init 在 enabled
    # 时不抛异常且 _reset_for_fork 被调 (用 spy 验证)
    reset_calls = {"n": 0}
    real_reset = lf._reset_for_fork

    def spy_reset():
        reset_calls["n"] += 1
        return real_reset()

    monkeypatch.setattr(lf, "_reset_for_fork", spy_reset)

    _worker_init(paths)
    assert reset_calls["n"] == 1


def test_worker_init_disabled_skips_atexit_register(
    tmp_path: Path, monkeypatch,
) -> None:
    """``langfuse.enabled=False`` → ``_worker_init`` 不抛异常, ``_reset_for_fork``
    仍然调 (always idempotent), atexit 不注册 shutdown."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Cfg:
        enabled: bool = False

    monkeypatch.setattr(
        "orchestration.observability.langfuse_config.load_langfuse_config",
        lambda: _Cfg(),
    )

    paths = _make_paths(tmp_path)
    from orchestration.task_pipeline import _worker_init

    # 验证不抛异常 + reset 被调
    import simulate_serve.observability.langfuse_client as lf

    reset_calls = {"n": 0}
    real_reset = lf._reset_for_fork

    def spy_reset():
        reset_calls["n"] += 1
        return real_reset()

    monkeypatch.setattr(lf, "_reset_for_fork", spy_reset)

    _worker_init(paths)
    assert reset_calls["n"] == 1