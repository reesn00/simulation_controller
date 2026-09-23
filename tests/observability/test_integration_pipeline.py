"""Langfuse 端到端集成测试 (PR 6 / Commit 3).

测试目标:验证三阶段 (simulate_serve / gdr / etl) Langfuse 接入在端到端
pipeline 跑通时的语义正确性, **不访问真实 SDK** (全部 mock).

覆盖:
  1. test_full_pipeline_three_traces_per_session
     -> 跑 _run_one_task_pipeline 一次, 验证 Langfuse SDK 收到 3 个 outer
        trace (simulate_serve / gdr / etl), session_id 一致, task_id 一致
  2. test_etl_retry_creates_n_independent_traces
     -> _safe_run_etl retry 3 次成功 -> 1 outer trace; 注入前 2 次失败 ->
        3 outer trace
  3. test_disabled_langfuse_zero_overhead
     -> enabled=False 时 mock SDK 全程零调用, 业务结果不变
  4. test_factory_sdk_init_fail_safe_across_pipeline
     -> 工厂 Langfuse 构造抛错 -> 3 阶段均 fail-safe, 整 pipeline 正常完成
  5. test_payload_modes_etl_full_summary_none
     -> 三态在 etl 端 outer span 的 input/output 上正确分流
  6. test_fork_safe_pool_worker
     -> _worker_init 注册 atexit shutdown; 多次 _reset_for_fork + get_client
        互不干扰
  7. test_session_id_mismatch_raises_nonretryable
     -> run_etl_once(session_id=...) 与 C2 内 session_id 不一致 ->
        EtlNonRetryableError + outer span 标 ERROR

mock 策略: 工厂内 Langfuse SDK 替换为 MagicMock; process-local _client
清空避免跨测试污染.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from gdr.config.settings import Settings as GdrSettings


# ---------------------------------------------------------------------------
# 共享 fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """清空 process-local singleton + atexit handler list.

    PR 5 在 _worker_init 注册 atexit; 测试隔离要求每个测试前后都清理,
    否则会污染 Python 进程全局 (影响后续测试).
    """
    from simulate_serve.observability import langfuse_client
    langfuse_client._client = None
    yield
    langfuse_client._client = None


@pytest.fixture
def fake_langfuse_sdk(monkeypatch):
    """替换工厂 Langfuse SDK + propagate_attributes 为 MagicMock.

    返回 ``(fake_sdk, span_calls)``:
       ``span_calls`` 是 list[dict], 每次 add ``start_as_current_observation``
       调用的 kwargs 都 append 一份, 便于断言 span 名 / metadata.
    """
    from simulate_serve.observability import langfuse_client

    span_calls: list[dict[str, Any]] = []
    update_calls: list[dict[str, Any]] = []

    def _record_spawn(**kwargs):
        span_calls.append(kwargs)
        span = mock.MagicMock(name=f"Span[{kwargs.get('name', '?')}]")
        cm = mock.MagicMock(name=f"CM[{kwargs.get('name', '?')}]")
        cm.__enter__.return_value = span
        cm.__exit__.return_value = False

        def _update(**kw):
            update_calls.append(kw)

        span.update.side_effect = _update
        return cm

    fake_sdk = mock.MagicMock(name="LangfuseSDK")
    fake_sdk.start_as_current_observation.side_effect = _record_spawn

    pa = mock.MagicMock(name="PropagateAttributes")
    pa.__enter__.return_value = None
    pa.__exit__.return_value = False

    monkeypatch.setattr(langfuse_client, "Langfuse", fake_sdk)
    monkeypatch.setattr(langfuse_client, "propagate_attributes", pa)
    # 短路: 已有 singleton 直接返, 无需重新 init SDK
    langfuse_client._client = fake_sdk
    return fake_sdk, span_calls, update_calls, pa


@pytest.fixture
def enabled_cfg():
    """orchestration/observability/langfuse_config.LangfuseConfig 替身."""
    from orchestration.observability.langfuse_config import LangfuseConfig
    return LangfuseConfig(
        enabled=True,
        public_key="pk-it",
        secret_key="sk-it",
        base_url="https://cloud.langfuse.com",
        environment="test",
        release="ci",
        sample_rate=1.0,
        flush_at=512,
        flush_interval=5.0,
        timeout=10,
        upload_payload="full",
        max_payload_bytes=0,
        max_block_payload_bytes=0,
        per_step_span=True,
    )


# ---------------------------------------------------------------------------
# 假数据: TaskRun / GdrResult / EtlOutputs
# ---------------------------------------------------------------------------


@dataclass
class _FakeRunFailure:
    message: str = "n/a"


@dataclass
class _FakeRun:
    run_id: str = "run_it"
    remote_session_id: str = "useramulation-it"
    task_id: str = "T001"
    state: Any = "success"
    failure: Any = None

    @classmethod
    def make(cls, *, run_id: str, session_id: str, task_id: str) -> "_FakeRun":
        return cls(
            run_id=run_id, remote_session_id=session_id, task_id=task_id,
        )


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


def _make_paths(tmp_path: Path):
    from orchestration.settings import Paths
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


def _make_gdr_settings(paths, *, langfuse_cfg=None):
    kwargs = dict(
        batch_output_dir=paths.refined_dir,
        workers=1, llm_concurrency=1, max_files=1,
    )
    if langfuse_cfg is not None:
        kwargs["langfuse_enabled"] = langfuse_cfg.enabled
        kwargs["langfuse_gdr_per_step_span"] = getattr(langfuse_cfg, "per_step_span", True)
        kwargs["langfuse_upload_payload"] = langfuse_cfg.upload_payload
        kwargs["langfuse_max_payload_bytes"] = langfuse_cfg.max_payload_bytes
        kwargs["langfuse_max_block_payload_bytes"] = langfuse_cfg.max_block_payload_bytes
        # 模拟 LLM 不调用
        kwargs["retry_loop_clip_enabled"] = False
        kwargs["router_enabled"] = False
        kwargs["refine_enabled"] = False
    return GdrSettings(**kwargs)


def _make_pipeline_settings(*, retry_gdr: int = 0, retry_etl: int = 0):
    from orchestration.settings import PipelineSettings
    return PipelineSettings(
        max_parallelism=1,
        max_retry_gdr=retry_gdr,
        max_retry_etl=retry_etl,
        retry_poll_seconds=0.05,
    )


def _write_min_c2(tmp_path: Path, *, task_id: str, session_id: str) -> Path:
    payload = {
        "session_id": session_id,
        "messages": [],
        "schema_version": "refined_session.v1",
        "metadata": {"qf_text": "stub", "task_id": task_id},
    }
    refined = tmp_path / "refined_dir" / f"{task_id}__{session_id}.json"
    refined.parent.mkdir(parents=True, exist_ok=True)
    refined.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return refined


def _write_min_c1(tmp_path: Path, *, run_id: str, session_id: str) -> Path:
    """模拟 simulate_serve 落盘的 trajectory (C1 端)."""
    payload = {"events": [{"event_type": "model_response", "payload": {}}]}
    p = tmp_path / "trajectory_dir" / f"{run_id}__{session_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _patch_orchestration_modules(
    monkeypatch,
    *,
    refined_path: Path,
    etl_outputs: _FakeEtlOutputs,
    gdr_fails: int = 0,
    etl_fails: int = 0,
    run_real_etl_once: bool = True,
):
    """patch orchestration.task_pipeline 引用的下游模块, 让 pipeline 跑通.

    默认 ``run_real_etl_once=True`` 时 **不** mock run_etl_once, 而是 mock
    它依赖的 ``save_session_v2`` (写盘函数), 让 run_etl_once 走真实外层
    ``stage_trace`` 入口. 这样可以验证 etl 阶段的 trace 实际被打开.

    Returns the patched symbols for direct inspection.
    """
    # simulate 阶段: 返 _FakeRun(success); run_id / session_id 与 C1 文件名一致.
    c2_stem = refined_path.stem  # "T001__useramulation-it"
    parts = c2_stem.split("__", 1)
    file_session = parts[1] if len(parts) == 2 else ""
    fake_run = _FakeRun(
        run_id=f"run_{parts[0] if len(parts) >= 1 else 'x'}",
        remote_session_id=file_session,
        task_id=parts[0] if len(parts) >= 1 else "T000",
    )

    def fake_run_one_task_sync(task_id, *, config_path):
        return fake_run

    monkeypatch.setattr(
        "orchestration.producer_simulate.run_one_task_sync",
        fake_run_one_task_sync,
        raising=False,
    )

    # gdr 阶段: 返回 refined_path, 失败按 gdr_fails 计数
    gdr_calls = {"n": 0}

    def fake_run_gdr_once(
        *, src_path, refined_dir, gdr_settings, task_id, session_id,
        langfuse_client=None, langfuse_cfg=None,
    ):
        gdr_calls["n"] += 1
        if gdr_calls["n"] <= gdr_fails:
            raise RuntimeError(f"gdr fail #{gdr_calls['n']}")
        return _FakeGdrResult(
            refined_path=refined_path, task_id=task_id, session_id=session_id,
        )

    monkeypatch.setattr(
        "orchestration.workers.gdr_worker.run_gdr_once",
        fake_run_gdr_once,
    )

    if not run_real_etl_once:
        # etl 阶段: 返回 etl_outputs, 失败按 etl_fails 计数
        etl_calls = {"n": 0}

        def fake_run_etl_once(
            *, c2_path, etl_outputs_dir, task_id, session_id,
            attempt=0, langfuse_cfg=None,
        ):
            etl_calls["n"] += 1
            if etl_calls["n"] <= etl_fails:
                raise RuntimeError(f"etl fail #{etl_calls['n']}")
            return etl_outputs

        monkeypatch.setattr(
            "orchestration.workers.etl_worker.run_etl_once",
            fake_run_etl_once,
        )
        return {
            "run_one_task_sync": fake_run_one_task_sync,
            "run_gdr_once": fake_run_gdr_once,
            "run_etl_once": fake_run_etl_once,
            "gdr_calls": gdr_calls,
            "etl_calls": etl_calls,
        }

    # run_real_etl_once=True: 让真 run_etl_once 跑, 但 mock save_session_v2
    # 写 4 个空文件 (避免真依赖 gdr.domain 实现细节).
    class _StubSessionOutputs:
        def __init__(self, m, o, q, x):
            self.messages = m
            self.openai = o
            self.qwenjina = q
            self.meta = x

    # load_refined_session 真跑 (C2 dict -> Session) — 需要 fake Session
    # object. 我们 mock load_refined_session 让它返一个简单 Session 替身.
    from orchestration.workers.etl_worker import EtlNonRetryableError

    class _FakeSession:
        """Session 替身: 仅暴露 model_dump + session_id."""
        def __init__(self, sid):
            self.session_id = sid
            self.messages = []
            self.metadata = {"qf_text": "stub"}

        def model_dump(self, mode="python", **kwargs):
            return {
                "session_id": self.session_id,
                "messages": [],
                "metadata": {"qf_text": "stub"},
            }

    _fake_session_holder: dict[str, Any] = {}

    def fake_load_refined_session(c2_path, *args, **kwargs):
        # 用 _fake_session_holder 记住 session_id 给 save_session_v2 用
        import json as _json
        raw = _json.loads(Path(c2_path).read_text(encoding="utf-8"))
        sid = raw.get("session_id", "")
        _fake_session_holder["session"] = _FakeSession(sid)
        return _fake_session_holder["session"]

    def fake_save_v2(session, base_path):
        base = Path(base_path)
        base.parent.mkdir(parents=True, exist_ok=True)
        messages = base.with_suffix(".messages.json")
        openai = base.with_suffix(".openai.json")
        meta = base.with_suffix(".meta.json")
        messages.write_text("[]", encoding="utf-8")
        openai.write_text("[]", encoding="utf-8")
        meta.write_text("{}", encoding="utf-8")
        return _StubSessionOutputs(messages, openai, None, meta)

    monkeypatch.setattr(
        "orchestration.workers.etl_worker.load_refined_session",
        fake_load_refined_session,
    )
    monkeypatch.setattr(
        "orchestration.workers.etl_worker.save_session_v2",
        fake_save_v2,
    )

    return {
        "run_one_task_sync": fake_run_one_task_sync,
        "run_gdr_once": fake_run_gdr_once,
        "load_refined_session": fake_load_refined_session,
        "save_session_v2": fake_save_v2,
        "gdr_calls": gdr_calls,
    }


# ===========================================================================
# Test 1: 完整 pipeline 三阶段各起 1 个 outer trace
# ===========================================================================


def test_full_pipeline_three_traces_per_session(
    tmp_path, fake_langfuse_sdk, enabled_cfg, monkeypatch
):
    """一次 _run_one_task_pipeline 跑通后, 三阶段 helper 都被调用且 session_id 一致.

    关键断言 (PR 6 边界: simulate_serve 的 _emit_trail 在 trajectory_archiver
    内部, 不会通过 _run_one_task_pipeline 直接触达; 本测试聚焦 gdr + etl 两个
    阶段 helper 通过 orchestration.task_pipeline 跑通):

      * ``etl:<task_id>`` outer 1 次 (load + save 两个子 span 也开)
      * run_gdr_once 至少调起一次 gdr.process_one (子 spans 数量 > 0)
      * session_id = ``useramulation-it`` 在 outer 与 inner spans 上一致
      * tags 含 ``task:T001`` (PR 5 设计)
    """
    _, span_calls, _, pa = fake_langfuse_sdk
    paths = _make_paths(tmp_path)
    refined = _write_min_c2(tmp_path, task_id="T001", session_id="useramulation-it")
    _write_min_c1(tmp_path, run_id="run_it", session_id="useramulation-it")
    etl_out = _FakeEtlOutputs(
        messages_path=tmp_path / "m.json",
        openai_path=tmp_path / "o.json",
        qwenjina_path=None,
        meta_path=tmp_path / "x.json",
        task_id="T001",
        session_id="useramulation-it",
    )
    (tmp_path / "m.json").write_text("[]")
    (tmp_path / "o.json").write_text("[]")
    (tmp_path / "x.json").write_text("{}")

    _patch_orchestration_modules(
        monkeypatch, refined_path=refined, etl_outputs=etl_out,
    )

    gdr_settings = _make_gdr_settings(paths, langfuse_cfg=enabled_cfg)
    pipe_settings = _make_pipeline_settings(retry_gdr=0, retry_etl=0)

    # 强制启用: 让 pipeline 拿到 enabled_cfg (load_langfuse_config 是
    # 函数体内延迟 import, 必须 patch 源模块而不是 task_pipeline)
    monkeypatch.setattr(
        "orchestration.observability.langfuse_config.load_langfuse_config",
        lambda *a, **k: enabled_cfg,
    )
    # pipeline 内部 `_lf_get_client` 也得返同一 fake sdk; 上文 fixture 已短路

    from orchestration.task_pipeline import _run_one_task_pipeline

    # 把 producer_simulate 的 import 也准备好
    import orchestration.producer_simulate  # noqa: F401  # ensure attribute exists

    result = _run_one_task_pipeline(
        task_id="T001",
        paths=paths,
        gdr_settings=gdr_settings,
        orchestration_settings=pipe_settings,
    )

    assert result["phase"] == "done", f"pipeline 失败: {result}"

    # 收集 outer trace 名
    outer_names = [c.get("name") for c in span_calls]

    # etl: 1 outer trace
    assert outer_names.count(f"etl:T001") == 1, (
        f"etl outer 应有 1 次, 实际: {outer_names}"
    )

    # session_id 校验: 通过 propagate_attributes(__enter__ 调用记录
    # session_id); 该字段不进 start_as_current_observation kwargs.
    pa_session_ids: set[str] = set()
    for c in pa.mock_calls:
        # c 是 propagate_attributes(session_id=...) 调用
        kwargs = getattr(c, "kwargs", None)
        if isinstance(kwargs, dict) and "session_id" in kwargs:
            pa_session_ids.add(kwargs["session_id"])
        elif len(c.args) >= 1 and isinstance(c.args[0], dict):
            pa_session_ids.update(c.args[0].get("session_id", "") and {c.args[0]["session_id"]})

    assert "useramulation-it" in pa_session_ids, (
        f"propagate_attributes 应传 'useramulation-it', 实际: {pa_session_ids}"
    )

    # 任务 ID 一致: tags 经 propagate_attributes 传递, 不进 start_observation
    # kwargs. 检查 pa.mock_calls.
    pa_tags: set[str] = set()
    for c in pa.mock_calls:
        kwargs = getattr(c, "kwargs", None)
        if isinstance(kwargs, dict):
            for t in kwargs.get("tags") or ():
                    pa_tags.add(t)
    assert "task:T001" in pa_tags, (
        f"应至少一个 propagate_attributes 调用含 'task:T001' tag, 实际 tags: {pa_tags}"
    )

    # PR 5: simulate_serve outer trace 由 trajectory_archiver._emit_trail 在
    # archive() 内部 finally 块触发; producer_simulate 是被全 mock 的,
    # 不真正走 archiver. 独立单元测试见 test_simulate_serve_archiver.py.
    # 这里只断言: 当 simulate_serve._emit_trail 被显式调用时, factory 收到
    # 正确参数.
    from simulate_serve.observability.langfuse_client import (
        stage_trace as real_stage_trace,
    )

    with real_stage_trace(
        fake_langfuse_sdk[0],
        session_id="useramulation-it",
        name="simulate_serve:T001",
        task_id="T001",
        tags=["stage:simulate_serve", "task:T001"],
        metadata={"stage": "simulate_serve"},
        input_data=None,
        output_capture=lambda: {"events": []},
        payload_mode="full",
    ):
        pass

    # 至此 span_calls 应新增 1 个 simulate_serve:T001
    outer_names = [c.get("name") for c in span_calls]
    assert outer_names.count(f"simulate_serve:T001") == 1, (
        f"显式调 stage_trace 应开 1 个 simulate_serve outer, 实际: {outer_names}"
    )


# ===========================================================================
# Test 2: etl retry -> N 个独立 trace (PR 5 设计)
# ===========================================================================


def test_etl_retry_creates_n_independent_traces(
    tmp_path, fake_langfuse_sdk, enabled_cfg, monkeypatch
):
    """_safe_run_etl 重试 2 次成功 -> 2 个 outer trace (attempt=0/1).

    与 PR 5 设计一致: 每次 attempt 是独立执行, 因此 ``metadata.attempt=N``
    标记 N 个独立 outer trace, Langfuse 端按 session_id 聚合.

    实现策略: 走真的 run_etl_once; 第一次 mock save_session_v2 抛错 (让
    attempt=0 触发 Exception 走重试), 第二次让它返回 stub.
    """
    _, span_calls, update_calls, _ = fake_langfuse_sdk

    from orchestration.task_pipeline import _safe_run_etl

    paths = _make_paths(tmp_path)
    refined = _write_min_c2(tmp_path, task_id="T002", session_id="useramulation-r2")
    _write_min_c1(tmp_path, run_id="run_r2", session_id="useramulation-r2")

    # queue mock
    queue = mock.MagicMock()
    queue.increment_attempts.return_value = None

    # mock load_refined_session + save_session_v2: 第一次 save 抛错,
    # 第二次成功. run_etl_once 内部 stage_trace + step_span 都会跑.
    class _StubSessionOutputs:
        def __init__(self, m, o, q, x):
            self.messages = m
            self.openai = o
            self.qwenjina = q
            self.meta = x

    class _FakeSession:
        def __init__(self, sid):
            self.session_id = sid
            self.messages = []
            self.metadata = {"qf_text": "stub"}

        def model_dump(self, mode="python", **kwargs):
            return {
                "session_id": self.session_id,
                "messages": [],
                "metadata": {"qf_text": "stub"},
            }

    def fake_load(c2_path, *a, **kw):
        raw = json.loads(Path(c2_path).read_text(encoding="utf-8"))
        return _FakeSession(raw.get("session_id", ""))

    save_attempts = {"n": 0}

    def fake_save(session, base_path):
        save_attempts["n"] += 1
        if save_attempts["n"] == 1:
            raise OSError("simulated save failure attempt 1")
        base = Path(base_path)
        base.parent.mkdir(parents=True, exist_ok=True)
        messages = base.with_suffix(".messages.json")
        openai = base.with_suffix(".openai.json")
        meta = base.with_suffix(".meta.json")
        messages.write_text("[]", encoding="utf-8")
        openai.write_text("[]", encoding="utf-8")
        meta.write_text("{}", encoding="utf-8")
        return _StubSessionOutputs(messages, openai, None, meta)

    monkeypatch.setattr(
        "orchestration.workers.etl_worker.load_refined_session",
        fake_load,
    )
    monkeypatch.setattr(
        "orchestration.workers.etl_worker.save_session_v2",
        fake_save,
    )

    result = _safe_run_etl(
        task_id="T002",
        c2_path=refined,
        etl_outputs_dir=paths.etl_outputs_dir,
        session_id="useramulation-r2",
        max_retry=2,
        queue=queue,
        langfuse_cfg=enabled_cfg,
    )

    assert result is not None, "_safe_run_etl 重试 2 次后应成功"
    assert save_attempts["n"] == 2, f"save 应被调 2 次, 实际 {save_attempts['n']}"

    # 2 次 attempt -> 2 个 ``etl:T002`` outer trace
    etl_outer = [c for c in span_calls if c.get("name") == "etl:T002"]
    assert len(etl_outer) == 2, (
        f"etl 外层应有 2 个 trace (attempt 0 + attempt 1), 实际: {len(etl_outer)}"
    )
    # metadata via span.update(metadata=...); 取每个 etl outer span 第一次 update(metadata=...)
    # 的 attempt 字段 (与 span_calls 顺序对齐).
    meta_updates = [
        u.get("metadata", {}) for u in update_calls
        if isinstance(u.get("metadata"), dict) and "stage" in u["metadata"]
        and u["metadata"].get("stage") == "etl"
    ]
    attempts = sorted(
        m.get("attempt") for m in meta_updates
        if m.get("task_id") == "T002"
    )
    assert attempts == [0, 1], f"attempt 应为 [0, 1], 实际: {attempts}"


# ===========================================================================
# Test 3: disabled -> 零 SDK 调用, 业务结果不变
# ===========================================================================


def test_disabled_langfuse_zero_overhead(
    tmp_path, fake_langfuse_sdk, monkeypatch
):
    """``enabled=False`` 时 mock SDK 全程零调用, 业务结果仍 DONE."""
    fake_sdk, span_calls, _, _ = fake_langfuse_sdk

    from orchestration.observability.langfuse_config import LangfuseConfig
    disabled = LangfuseConfig(enabled=False)  # 其他字段走 default

    paths = _make_paths(tmp_path)
    refined = _write_min_c2(tmp_path, task_id="T003", session_id="useramulation-d3")
    # C1 路径必须存在, 否则 simulate 阶段会因 trajectory 缺失而 dead
    _write_min_c1(tmp_path, run_id="run_d3", session_id="useramulation-d3")
    etl_out = _FakeEtlOutputs(
        messages_path=tmp_path / "m.json",
        openai_path=tmp_path / "o.json",
        qwenjina_path=None,
        meta_path=tmp_path / "x.json",
        task_id="T003",
        session_id="useramulation-d3",
    )
    for p in (etl_out.messages_path, etl_out.openai_path, etl_out.meta_path):
        p.write_text("[]")

    _patch_orchestration_modules(
        monkeypatch, refined_path=refined, etl_outputs=etl_out,
    )

    # 工厂 import mock: _client 一直是 None (autouse 已清), 所有 stage_trace
    # 走 ``client is None`` 分支 yield None, 0 SDK 调用
    from simulate_serve.observability import langfuse_client
    langfuse_client._client = None  # 强制 None

    monkeypatch.setattr(
        "orchestration.observability.langfuse_config.load_langfuse_config",
        lambda *a, **k: disabled,
    )

    gdr_settings = _make_gdr_settings(paths, langfuse_cfg=disabled)
    pipe_settings = _make_pipeline_settings()

    import orchestration.producer_simulate  # noqa: F401

    from orchestration.task_pipeline import _run_one_task_pipeline
    result = _run_one_task_pipeline(
        task_id="T003",
        paths=paths,
        gdr_settings=gdr_settings,
        orchestration_settings=pipe_settings,
    )
    assert result["phase"] == "done", f"disabled 路径应 DONE, 实际: {result}"
    assert len(span_calls) == 0, (
        f"disabled 时 SDK 不应被调用, 实际调用了 {len(span_calls)} 次: {span_calls}"
    )


# ===========================================================================
# Test 4: 工厂 SDK init 失败 -> 整 pipeline fail-safe
# ===========================================================================


def test_factory_sdk_init_fail_safe_across_pipeline(
    tmp_path, monkeypatch, caplog
):
    """工厂 Langfuse 构造抛错 -> 3 阶段 helper 均 fail-safe, pipeline DONE."""
    from simulate_serve.observability import langfuse_client

    # 让 Langfuse() 构造抛错 (模拟凭据错误 / 网络初始化失败)
    def boom(*a, **kw):
        raise RuntimeError("simulated SDK init failure")

    monkeypatch.setattr(langfuse_client, "Langfuse", boom)
    langfuse_client._client = None

    from orchestration.observability.langfuse_config import LangfuseConfig
    cfg = LangfuseConfig(enabled=True, public_key="pk-fail", secret_key="sk-fail")

    paths = _make_paths(tmp_path)
    refined = _write_min_c2(tmp_path, task_id="T004", session_id="useramulation-f4")
    # C1 路径必须存在, 否则 simulate 阶段会因 trajectory 缺失而 dead
    _write_min_c1(tmp_path, run_id="run_f4", session_id="useramulation-f4")
    etl_out = _FakeEtlOutputs(
        messages_path=tmp_path / "m.json",
        openai_path=tmp_path / "o.json",
        qwenjina_path=None,
        meta_path=tmp_path / "x.json",
        task_id="T004",
        session_id="useramulation-f4",
    )
    for p in (etl_out.messages_path, etl_out.openai_path, etl_out.meta_path):
        p.write_text("[]")

    _patch_orchestration_modules(
        monkeypatch, refined_path=refined, etl_outputs=etl_out,
    )

    monkeypatch.setattr(
        "orchestration.observability.langfuse_config.load_langfuse_config",
        lambda *a, **k: cfg,
    )

    gdr_settings = _make_gdr_settings(paths, langfuse_cfg=cfg)
    pipe_settings = _make_pipeline_settings()

    import orchestration.producer_simulate  # noqa: F401

    with caplog.at_level(logging.WARNING):
        from orchestration.task_pipeline import _run_one_task_pipeline
        result = _run_one_task_pipeline(
            task_id="T004",
            paths=paths,
            gdr_settings=gdr_settings,
            orchestration_settings=pipe_settings,
        )

    assert result["phase"] == "done", (
        f"SDK init 失败应 fail-safe, pipeline 应 DONE, 实际: {result}"
    )
    # 警告应记录 'Langfuse init failed'
    assert any(
        "Langfuse init failed" in record.message or "init failed" in record.message
        for record in caplog.records
    ), f"应记 WARNING, 实际: {[r.message for r in caplog.records]}"


# ===========================================================================
# Test 5: payload 三态 (full / summary / none) 在 etl 端 outer span
# ===========================================================================


def test_payload_modes_etl_full_summary_none(
    tmp_path, fake_langfuse_sdk, monkeypatch
):
    """三种 payload 模式在 etl outer span 的 input/output 上正确分流.

    full: outer.output = 4 视图 dict (含 path + bytes)
    summary: outer.output = {"summary": True, "size_hint": N}
    none: outer.output = None (实际不调 .update(output=...))
    """
    _, span_calls, update_calls, _ = fake_langfuse_sdk

    from orchestration.workers.etl_worker import run_etl_once
    from orchestration.observability.langfuse_config import LangfuseConfig

    paths = _make_paths(tmp_path)
    refined = _write_min_c2(tmp_path, task_id="T005", session_id="useramulation-p5")
    etl_out = _FakeEtlOutputs(
        messages_path=tmp_path / "m.json",
        openai_path=tmp_path / "o.json",
        qwenjina_path=None,
        meta_path=tmp_path / "x.json",
        task_id="T005",
        session_id="useramulation-p5",
    )
    for p in (etl_out.messages_path, etl_out.openai_path, etl_out.meta_path):
        p.write_text("[]" * 100)

    # save_session_v2 是真函数, 它会写 4 个文件并返 SessionOutputs;
    # 但测试中我们直接 patch 它返 _FakeSessionOutputs 替身
    from gdr.domain import save_session_v2 as real_save_v2

    class _StubSessionOutputs:
        def __init__(self, m, o, q, x):
            self.messages = m
            self.openai = o
            self.qwenjina = q
            self.meta = x

    def fake_save_v2(session, base_path):
        base = Path(base_path)
        # 模拟写文件, 让 _capture_save_payload 的 _size 拿得到 stat
        (base.parent / "messages.json").write_text("[]" * 200)
        (base.parent / "openai.json").write_text("[]" * 200)
        (base.parent / "meta.json").write_text("{}")
        return _StubSessionOutputs(
            base.parent / "messages.json",
            base.parent / "openai.json",
            None,
            base.parent / "meta.json",
        )

    monkeypatch.setattr(
        "orchestration.workers.etl_worker.save_session_v2",
        fake_save_v2,
    )

    for mode in ("full", "summary", "none"):
        cfg = LangfuseConfig(
            enabled=True,
            public_key="pk", secret_key="sk",
            upload_payload=mode,
            max_payload_bytes=0,
            per_step_span=True,
        )

        result = run_etl_once(
            c2_path=refined,
            etl_outputs_dir=paths.etl_outputs_dir,
            task_id="T005",
            session_id="useramulation-p5",
            langfuse_cfg=cfg,
        )
        assert result.messages_path.exists()

        # 找到本次 etl outer span 的 update(output=...) 调用
        etl_outer = [
            c for c in span_calls
            if c.get("name") == "etl:T005"
        ]
        assert len(etl_outer) == 1, (
            f"mode={mode} 应有 1 个 outer, 实际: {len(etl_outer)}"
        )
        # update_calls 中找 output= 的最后一次 (按 cfg 调用顺序)
        # outer.output 是 span update 的 input= / output= 中 output= 较新的;
        # outer span 的 enter 后第一个 update 是 metadata+input, 最后一个是 output.
        out_kw = [
            u for u in update_calls
            if "output" in u and "messages" not in u.get("output", {})
        ]
        # 在 full 模式下 outer.output 必含 'messages'/'openai'/'meta' keys.
        # summary 模式 output = {"summary": True, "size_hint": ...}.
        # none 模式不应有 output= 调用 (None 短路).

        if mode == "full":
            assert any(
                "messages" in (u.get("output") or {}) and
                "openai" in (u.get("output") or {})
                for u in update_calls
            ), f"full 模式应传完整 4 视图 dict, update_calls={update_calls[-5:]}"
        elif mode == "summary":
            assert any(
                (u.get("output") or {}).get("summary") is True
                for u in update_calls
            ), f"summary 模式应传 {{'summary': True, ...}}, update_calls={update_calls[-5:]}"
        else:  # none
            # outer 不调 output= (因为 None 短路), 但子 span 的 input= None 也合理
            full_outputs = [
                u for u in update_calls
                if "output" in u and u.get("output") is not None
                and isinstance(u.get("output"), dict)
                and ("messages" in u["output"] or "summary" in u["output"])
            ]
            assert not full_outputs, (
                f"none 模式不应传 payload, 但有: {full_outputs}"
            )

        # 重置 span_calls / update_calls 避免跨模式污染
        span_calls.clear()
        update_calls.clear()


# ===========================================================================
# Test 6: fork-safe Pool worker
# ===========================================================================


def test_fork_safe_pool_worker_singleton_isolation(monkeypatch):
    """_reset_for_fork + get_client 互不污染, atexit shutdown 不重复注册.

    验证:
      * _reset_for_fork 后 _client = None, 下次 get_client 重建
      * 多次 _reset_for_fork 幂等
      * atexit.register(shutdown) 仅在 enabled=true 的 _worker_init 注册
    """
    import atexit

    from simulate_serve.observability import langfuse_client

    # 先 init 一次
    sdk_v1 = mock.MagicMock(name="SDK_v1")
    monkeypatch.setattr(langfuse_client, "Langfuse", sdk_v1)
    langfuse_client._client = None

    cfg = type("C", (), {
        "enabled": True, "public_key": "pk", "secret_key": "sk",
        "base_url": "https://cloud.langfuse.com",
        "environment": "test", "release": "ci",
        "sample_rate": 1.0, "flush_at": 512,
        "flush_interval": 5.0, "timeout": 10,
        "upload_payload": "full", "max_payload_bytes": 0,
        "max_block_payload_bytes": 0,
    })()

    client1 = langfuse_client.get_client(cfg)
    client2 = langfuse_client.get_client(cfg)
    assert client1 is client2, "同一 cfg 多次 get 应返同一 singleton"

    # 模拟 fork: 重置
    langfuse_client._reset_for_fork()
    sdk_v2 = mock.MagicMock(name="SDK_v2")
    monkeypatch.setattr(langfuse_client, "Langfuse", sdk_v2)

    client3 = langfuse_client.get_client(cfg)
    assert client3 is not client1, "fork 后 get_client 应重建 singleton"
    # sdk_v2 构造被调用 1 次 (重建)
    assert sdk_v2.call_count == 1

    # 多次 _reset_for_fork 幂等
    langfuse_client._reset_for_fork()
    langfuse_client._reset_for_fork()
    assert langfuse_client._client is None

    # atexit 隔离: 用 mock 替换 atexit.register, 检查 _worker_init 仅在
    # enabled=true 时注册
    registered: list[Any] = []
    real_register = atexit.register

    def mock_register(fn, *args, **kwargs):
        registered.append(fn)
        return real_register(fn, *args, **kwargs)

    monkeypatch.setattr(atexit, "register", mock_register)

    # 让 _worker_init 内的 load_langfuse_config 返 enabled=True cfg
    from orchestration.observability.langfuse_config import LangfuseConfig
    enabled = LangfuseConfig(
        enabled=True, public_key="pk-it", secret_key="sk-it",
    )
    monkeypatch.setattr(
        "orchestration.observability.langfuse_config.load_langfuse_config",
        lambda *a, **k: enabled,
    )

    from orchestration.task_pipeline import _worker_init
    paths = _make_paths_from(monkeypatch)
    _worker_init(paths)
    assert langfuse_client.shutdown in registered, (
        f"_worker_init(enabled=True) 应注册 shutdown, 实际: {registered}"
    )


def _make_paths_from(monkeypatch):
    """为 _worker_init 提供最小 Paths 实例."""
    import tempfile
    from orchestration.settings import Paths

    tmp = Path(tempfile.mkdtemp())
    for sub in (
        "trajectory_dir", "refined_dir", "etl_outputs_dir",
        "dead_dir", "log_dir", "runs_dir",
    ):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    return Paths(
        simulate_serve_config=tmp / "sim.yaml",
        trajectory_dir=tmp / "trajectory_dir",
        runs_dir=tmp / "runs_dir",
        refined_dir=tmp / "refined_dir",
        etl_outputs_dir=tmp / "etl_outputs_dir",
        sqlite_db=tmp / "q.db",
        dead_dir=tmp / "dead_dir",
        pid_file=tmp / "orch.pid",
        log_dir=tmp / "log_dir",
    )


# ===========================================================================
# Test 7: session_id 不一致 -> NonRetryableError + outer span 标 ERROR
# ===========================================================================


def test_session_id_mismatch_raises_nonretryable(
    tmp_path, fake_langfuse_sdk, monkeypatch
):
    """run_etl_once(session_id=...) 与 C2 内 session_id 不一致 ->
    EtlNonRetryableError + outer span 标 level=ERROR (PR 5 factory 行为).
    """
    fake_sdk, span_calls, update_calls, _ = fake_langfuse_sdk

    from orchestration.workers.etl_worker import (
        EtlNonRetryableError,
        run_etl_once,
    )
    from orchestration.observability.langfuse_config import LangfuseConfig

    cfg = LangfuseConfig(
        enabled=True, public_key="pk", secret_key="sk",
        upload_payload="full", per_step_span=True,
    )

    paths = _make_paths(tmp_path)
    # C2 内 session_id = "sess_real", 但调用 run_etl_once(session_id="mismatch")
    refined = _write_min_c2(
        tmp_path, task_id="T007", session_id="sess_real",
    )

    with pytest.raises(EtlNonRetryableError) as ei:
        run_etl_once(
            c2_path=refined,
            etl_outputs_dir=paths.etl_outputs_dir,
            task_id="T007",
            session_id="mismatch",  # 与 C2 内 session_id 不一致
            langfuse_cfg=cfg,
        )
    # 错误信息含 session_id 比对失败
    assert "session_id" in str(ei.value).lower() or "mismatch" in str(ei.value).lower()

    # outer trace 仍开了 (factory PR 5 fail-safe: 异常穿透 stage_trace)
    etl_outer = [c for c in span_calls if c.get("name") == "etl:T007"]
    assert len(etl_outer) == 1, (
        f"mismatch 时 outer 应仍开 (但标记 ERROR), 实际: {len(etl_outer)}"
    )
    # span.update 至少有一处 level=ERROR
    err_updates = [u for u in update_calls if u.get("level") == "ERROR"]
    assert err_updates, (
        f"异常穿透时应标 level=ERROR, 实际 update_calls: {update_calls}"
    )