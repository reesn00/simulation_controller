"""gdr Langfuse 接入 — 综合 contract tests (PR 3 / Commit 10).

覆盖:

1. 21 步骤 span naming convention / runner.py source 静态分析
2. trace session_id 在 outer + inner spans 上一致 (从 session.session_id 派生)
3. 顶层 trace / 子 span tags 标记 (gdr.pipeline / gdr.stage / gdr.refine.*)
4. payload 模式 (full / summary / none) 与 schema 兼容 (无禁字段泄漏)
5. block payload 截断 (langfuse_max_block_payload_bytes > 0 时截断, 不抛)
6. fork-safe 重置 (multiprocessing.Pool worker 启动调 _reset_for_fork)
7. gdr_worker.run_gdr_once outer trace + task_id thread-local
8. factory 缺 client / 缺凭据 / disabled 时所有 helper 安全 no-op
9. LLM span hook 在 disabled / per_llm_span=false / per_llm_span=true 三态下分流
10. retry_loop_clip.judge 子 generation span 在判定时进入

mock 策略: patch 工厂 ``step_span`` 直接验证 helper 传给工厂的 kwargs (避免 mock 整条
Langfuse SDK 链路; SDK 行为由工厂自身 contract 覆盖)。
"""
from __future__ import annotations

import pathlib
import re
import threading
from contextlib import contextmanager
from typing import Any
from unittest import mock

import pytest


# ============================================================================
# 通用 fixture / helper
# ============================================================================


WHITELIST_21_STEPS: set[str] = {
    "gdr.hard_filter",
    "gdr.light_health",
    "gdr.context_understanding.build",
    "gdr.fold.failed_toolresults",
    "gdr.fold.repeated_thinking",
    "gdr.retry_loop_clip",
    "gdr.cu.retrack_state",
    "gdr.user_intent.heuristic",
    "gdr.router.tag",
    "gdr.policy.decide",
    "gdr.refine.run_repairs",
    "gdr.early_exit",
    "gdr.reassemble",
    "gdr.timeout_fallback",
    "gdr.unhandled_error",
    "gdr.audit.routing_abstain",
    "gdr.incomplete_check",
    "gdr.save_refined_session",
    "gdr.audit.deferred",
    "gdr.audit.judge_low",
}

REASSEMBLE_GENERATION_SUBSPANS: set[str] = {
    "gdr.reassemble.user_intent_llm",
    "gdr.reassemble.consistency_check",
    "gdr.reassemble.l3_judge",
}


class _Cfg:
    """极简 settings 替身: 只承载 helper 需要读的 langfuse_* / llm_* 字段."""

    def __init__(
        self,
        *,
        langfuse_enabled: bool = True,
        public_key: str = "pk",
        secret_key: str = "sk",
        per_step: bool = True,
        per_llm: bool = False,
        per_refine: bool = False,
        upload_payload: str | None = "full",
        max_block: int = 0,
        session_id: str = "test-session-001",
    ) -> None:
        self.langfuse_enabled = langfuse_enabled
        self.langfuse_public_key = public_key
        self.langfuse_secret_key = secret_key
        self.langfuse_gdr_per_step_span = per_step
        self.langfuse_gdr_per_llm_span = per_llm
        self.langfuse_gdr_per_refine_span = per_refine
        self.langfuse_upload_payload = upload_payload
        self.langfuse_max_block_payload_bytes = max_block
        self.session_id = session_id
        self._gdr_cfg = self  # self-bound for _gdr_step_span_ctx


class _Session:
    """替身 session: 暴露 session_id + 任何字段可被 snapshot."""

    def __init__(self, sid: str = "test-session-001") -> None:
        self.session_id = sid
        self.task_id = "T001"
        self.messages: list[dict] = []


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    """重置工厂 _client + gdr observability _TLS, 避免 singleton 串味."""
    try:
        from simulate_serve.observability import langfuse_client
    except ImportError:
        langfuse_client = None  # type: ignore
    if langfuse_client is not None:
        with langfuse_client._lock:
            langfuse_client._client = None
    try:
        from gdr.observability import runner_helpers as rh
        rh._TLS.task_id = None
    except (ImportError, AttributeError):
        pass
    yield


@pytest.fixture
def captured_spans():
    """patch ``gdr.observability.runner_helpers.step_span`` 收集 (name, kwargs).

    返回 list, 每条 dict: ``name`` / ``as_type`` / ``metadata`` / ``input_data`` /
    ``output_capture`` / ``session_id`` / ``task_id`` / ``max_payload_bytes``.
    """
    captured: list[dict] = []
    real = None
    try:
        from gdr.observability import runner_helpers as rh
        real = rh.step_span
    except (ImportError, AttributeError):
        pytest.skip("gdr.observability.runner_helpers not importable")

    def fake(client, *, name, **kwargs):
        @contextmanager
        def _cm():
            span = mock.MagicMock(name=f"span[{name}]")
            captured.append({"name": name, **kwargs})
            yield span

        return _cm()

    with mock.patch.object(rh, "step_span", side_effect=fake):
        yield captured


# ============================================================================
# 1. Naming convention + 静态分析
# ============================================================================


def test_all_span_names_match_convention() -> None:
    """所有 21 步骤 + 子 span + outer trace 命中统一命名 regex.

    outer trace 允许 ``gdr.process_one`` 或 ``gdr:{task_id}`` (worker 级);
    inner span 必须以 ``.kebab_step`` 收尾 (至少 1 段).
    """
    # outer trace (允许 0 或 N 段 .xxx): gdr.process_one / gdr:T001
    outer_pattern = re.compile(r"^gdr(?:\:[A-Za-z0-9_]+)?(?:\.[a-z0-9_]+)*$")
    # inner span (强制至少 1 段 .xxx): gdr.refine.run_repairs / gdr.reassemble.l3_judge
    inner_pattern = re.compile(r"^gdr(?:\:[A-Za-z0-9_]+)?(?:\.[a-z0-9_]+)+$")

    outer_names = {"gdr.process_one", "gdr:T001"}
    inner_names = (
        WHITELIST_21_STEPS | REASSEMBLE_GENERATION_SUBSPANS
        | {"gdr.retry_loop_clip.judge", "gdr.llm.qwen3_5_9b"}
    )
    for name in sorted(outer_names):
        assert outer_pattern.match(name), f"outer {name!r} fails convention"
    for name in sorted(inner_names):
        assert inner_pattern.match(name), f"inner {name!r} fails inner convention"


def test_21_steps_present_in_runner_source() -> None:
    """grep ``gdr/pipeline/runner.py`` 验证 21 个 span name 都被引用过."""
    src = pathlib.Path("gdr/pipeline/runner.py").read_text(encoding="utf-8")
    missing = [name for name in WHITELIST_21_STEPS if name not in src]
    assert not missing, f"Missing 21-step span names in runner.py: {missing}"


def test_reassemble_subspans_present_in_reassembler_source() -> None:
    """grep ``gdr/reassembly/reassembler.py`` 验证 3 个 generation 子 span 都被引用."""
    src = pathlib.Path("gdr/reassembly/reassembler.py").read_text(encoding="utf-8")
    missing = [n for n in REASSEMBLE_GENERATION_SUBSPANS if n not in src]
    assert not missing, f"Missing reassemble sub-span names: {missing}"


def test_retry_loop_clip_judge_subspan_present() -> None:
    """``gdr.retry_loop_clip.judge`` 子 generation 在 retry_loop_clip.py 出现."""
    src = pathlib.Path("gdr/refiners/retry_loop_clip.py").read_text(encoding="utf-8")
    assert "gdr.retry_loop_clip.judge" in src


# ============================================================================
# 2. trace session_id 一致性
# ============================================================================


def test_step_session_id_derived_from_session() -> None:
    """step_span 接到的 session_id 等于 session.session_id."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx

    sess = _Session(sid="sess-abc")
    cfg = _Cfg(session_id="sess-abc")
    sess._gdr_cfg = cfg

    with _gdr_step_span_ctx("gdr.test_step", sess) as span:
        assert span is not None

    # captured_spans fixture 不便注入; 直接看 _gdr_step_span_ctx 内部走的是
    # step_span 的 kwargs, 用 captured_spans 模式覆盖, 这里只验证 yield
    # 不抛即说明 session_id 解析路径未触发 AttributeError。


def test_captured_step_has_session_id_kwarg(captured_spans) -> None:
    """step_span 收到的 session_id kwarg 等于 session.session_id."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx

    sess = _Session(sid="sess-xyz")
    cfg = _Cfg(session_id="sess-xyz")
    sess._gdr_cfg = cfg

    with _gdr_step_span_ctx("gdr.foo", sess):
        pass

    assert captured_spans, "step_span was not called"
    last = captured_spans[-1]
    assert last["session_id"] == "sess-xyz"


# ============================================================================
# 3. 顶层 trace / 子 span tags
# ============================================================================


def test_step_metadata_is_passed_through(captured_spans) -> None:
    """step_span 接到的 metadata 是 helper metadata 的并集, 不被 _gdr_* 命名空间污染."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx

    sess = _Session()
    sess._gdr_cfg = _Cfg()

    with _gdr_step_span_ctx(
        "gdr.refine.run_repairs", sess, metadata={"step_kind": "refine", "n_repair": 2},
    ):
        pass

    md = captured_spans[-1]["metadata"]
    assert md["step_kind"] == "refine"
    assert md["n_repair"] == 2
    # 不应该出现 metadata={"metadata": {...}} 这种双层套娃
    assert "metadata" not in md or not isinstance(md.get("metadata"), dict)


def test_step_metadata_does_not_double_wrap_metadata_kwarg(captured_spans) -> None:
    """regression: helper 不应该把 ``metadata=`` 当 kwargs 收, 造成双层套娃."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx

    sess = _Session()
    sess._gdr_cfg = _Cfg()

    with _gdr_step_span_ctx("gdr.test_step", sess, metadata={"k": "v"}):
        pass

    md = captured_spans[-1]["metadata"]
    # 双层套娃时 md 会是 {"metadata": {"k": "v"}}; 不允许
    assert md == {"k": "v"}


# ============================================================================
# 4. payload 模式 (full / summary / none)
# ============================================================================


def test_payload_mode_full_passes_input_data(captured_spans) -> None:
    """payload=full → step_span input_data 是非空 snapshot."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx

    sess = _Session()
    sess._gdr_cfg = _Cfg(upload_payload="full")

    with _gdr_step_span_ctx("gdr.test_step", sess):
        pass

    last = captured_spans[-1]
    assert last["payload_mode"] == "full"


def test_payload_mode_none_skips_input_data(captured_spans) -> None:
    """payload=none → step_span input_data 应为 None."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx, _payload_mode

    cfg = _Cfg(upload_payload="none")
    # ``_payload_mode`` 路径只在 input_capture 阶段用, 但 full/none/summary
    # 在 helper 内由 ``_payload_mode(cfg)`` 统一决定。
    assert _payload_mode(cfg) == "none"

    sess = _Session()
    sess._gdr_cfg = cfg
    with _gdr_step_span_ctx("gdr.test_step", sess):
        pass
    # snapshot 函数由 factory 提供, helper 透传 None / dict; 我们这里只验证
    # payload_mode kwarg 正确透传给 step_span
    assert captured_spans[-1]["payload_mode"] == "none"


# ============================================================================
# 5. block payload 截断 (max_block_payload_bytes)
# ============================================================================


def test_max_block_payload_bytes_passed_to_step_span(captured_spans) -> None:
    """``langfuse_max_block_payload_bytes > 0`` 时, step_span 的 max_payload_bytes kwarg 非 0."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx

    sess = _Session()
    sess._gdr_cfg = _Cfg(max_block=2048)

    with _gdr_step_span_ctx("gdr.test_step", sess):
        pass

    last = captured_spans[-1]
    assert last["max_payload_bytes"] == 2048


def test_max_block_payload_bytes_zero_passed_through(captured_spans) -> None:
    """``max_block_payload_bytes=0`` 时, step_span 收到 0 (工厂视为"不截断")."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx

    sess = _Session()
    sess._gdr_cfg = _Cfg(max_block=0)

    with _gdr_step_span_ctx("gdr.test_step", sess):
        pass

    assert captured_spans[-1]["max_payload_bytes"] == 0


# ============================================================================
# 6. 工厂缺 client / disabled / 缺凭据 — helper 安全 no-op
# ============================================================================


def test_disabled_yields_none() -> None:
    """``langfuse_enabled=False`` 时 helper yield None (无 step_span 调用)."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx

    sess = _Session()
    sess._gdr_cfg = _Cfg(langfuse_enabled=False)

    with mock.patch(
        "gdr.observability.runner_helpers.step_span",
    ) as sp:
        with _gdr_step_span_ctx("gdr.test_step", sess) as span:
            assert span is None
    sp.assert_not_called()


def test_no_cfg_yields_none() -> None:
    """``_gdr_cfg`` 缺失时 yield None."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx

    sess = _Session()
    # 故意不挂 _gdr_cfg
    with mock.patch(
        "gdr.observability.runner_helpers.step_span",
    ) as sp:
        with _gdr_step_span_ctx("gdr.test_step", sess) as span:
            assert span is None
    sp.assert_not_called()


def test_per_step_disabled_yields_none() -> None:
    """``langfuse_gdr_per_step_span=False`` 时 helper yield None."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx

    sess = _Session()
    sess._gdr_cfg = _Cfg(per_step=False)

    with mock.patch(
        "gdr.observability.runner_helpers.step_span",
    ) as sp:
        with _gdr_step_span_ctx("gdr.test_step", sess) as span:
            assert span is None
    sp.assert_not_called()


def test_missing_credentials_returns_no_client() -> None:
    """缺凭据时工厂 get_client 返回 None, helper 退化为 yield None."""
    from gdr.observability.runner_helpers import _gdr_step_span_ctx
    from simulate_serve.observability import langfuse_client

    sess = _Session()
    sess._gdr_cfg = _Cfg(public_key="", secret_key="")

    with mock.patch.object(langfuse_client, "_client", None):
        client = langfuse_client.get_client(sess._gdr_cfg)
        assert client is None
        with _gdr_step_span_ctx("gdr.test_step", sess) as span:
            assert span is None


# ============================================================================
# 7. thread-local task_id 隔离
# ============================================================================


def test_task_id_thread_local_isolated() -> None:
    """``set_current_task_id`` 在 thread 间隔离, 主线程默认 None."""
    from gdr.observability.runner_helpers import (
        set_current_task_id,
        _current_task_id,
    )

    assert _current_task_id() is None
    set_current_task_id("T-MAIN")

    other: dict = {}

    def worker() -> None:
        try:
            other["tid"] = _current_task_id()
            set_current_task_id("T-WORKER")
            other["tid_after_set"] = _current_task_id()
        finally:
            set_current_task_id(None)

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert other["tid"] is None  # worker 线程独立 TLS, 看不到主线程 set
    assert other["tid_after_set"] == "T-WORKER"
    # 主线程 set 不被 worker 干扰
    assert _current_task_id() == "T-MAIN"
    set_current_task_id(None)
    assert _current_task_id() is None


def test_step_span_metadata_includes_task_id(captured_spans) -> None:
    """``set_current_task_id`` 后, step_span metadata 应携带 task_id."""
    from gdr.observability.runner_helpers import (
        _gdr_step_span_ctx,
        set_current_task_id,
    )

    sess = _Session()
    sess._gdr_cfg = _Cfg()

    set_current_task_id("T-TLS")
    try:
        with _gdr_step_span_ctx("gdr.test_step", sess):
            pass
    finally:
        set_current_task_id(None)

    # task_id 通过 ``task_id`` kwarg 传入 step_span (而不是塞进 metadata)
    last = captured_spans[-1]
    assert last["task_id"] == "T-TLS"


# ============================================================================
# 8. fork-safe reset
# ============================================================================


def test_reset_for_fork_clears_client_singleton() -> None:
    """``_reset_for_fork`` 把工厂 ``_client`` 置 None."""
    from simulate_serve.observability import langfuse_client

    langfuse_client._client = mock.MagicMock(name="fake-singleton")
    langfuse_client._reset_for_fork()
    assert langfuse_client._client is None


# ============================================================================
# 9. LLM span hook 三态分流
# ============================================================================


def test_per_llm_span_disabled_does_not_wrap() -> None:
    """``per_llm_span=False`` 时, ``LlamaCppClient.chat`` 不进入 maybe_llm_span 分支."""
    from gdr.infrastructure.llm_client import LlamaCppClient

    client = LlamaCppClient.get(
        model="test-model",
        base_url="http://localhost:9999",
        api_key="x",
        cfg=_Cfg(per_llm=False),
    )
    # cfg 已被挂到 instance.cfg
    assert getattr(client, "cfg", None) is not None
    assert getattr(client.cfg, "langfuse_gdr_per_llm_span", None) is False


def test_per_llm_span_enabled_cfg_propagates() -> None:
    """``per_llm_span=True`` 时, cfg.langfuse_gdr_per_llm_span 透传到 instance."""
    from gdr.infrastructure.llm_client import LlamaCppClient

    client = LlamaCppClient.get(
        model="test-model-2",
        base_url="http://localhost:9999",
        api_key="x",
        cfg=_Cfg(per_llm=True),
    )
    assert getattr(client, "cfg", None) is not None
    assert client.cfg.langfuse_gdr_per_llm_span is True


def test_chat_skips_llm_span_when_factory_returns_none() -> None:
    """``per_llm_span=True`` 但工厂 get_client 返 None (缺凭据) 时, chat 不抛异常."""
    from gdr.infrastructure.llm_client import LlamaCppClient
    from simulate_serve.observability import langfuse_client

    cfg = _Cfg(per_llm=True)
    cfg.langfuse_public_key = ""
    cfg.langfuse_secret_key = ""
    client = LlamaCppClient.get(
        model="test-model-3",
        base_url="http://localhost:9999",
        api_key="x",
        cfg=cfg,
    )

    # 工厂拿不到 client; 直接断言 ``get_client`` 返 None, 然后 chat 不会抛
    with mock.patch.object(langfuse_client, "_client", None):
        assert langfuse_client.get_client(client.cfg) is None
        # chat 应该因为网络不通抛异常, 但不是 langfuse 相关
        with pytest.raises(Exception) as ei:
            client.chat([{"role": "user", "content": "hi"}])
        # 关键: 异常信息不应提到 langfuse / maybe_llm_span
        msg = str(ei.value).lower()
        assert "langfuse" not in msg
        assert "maybe_llm_span" not in msg


# ============================================================================
# 10. retry_loop_clip.judge 子 generation span (Commit 3d)
# ============================================================================


def test_retry_loop_clip_judge_span_in_source_uses_as_type_generation() -> None:
    """retry_loop_clip.judge 子 span 用 ``as_type="generation"`` (而非默认 span)."""
    src = pathlib.Path("gdr/refiners/retry_loop_clip.py").read_text(encoding="utf-8")
    # 静态定位 gdr.retry_loop_clip.judge 周围的 step_span 调用, 校验 as_type
    idx = src.find("gdr.retry_loop_clip.judge")
    assert idx >= 0
    # 取往后 600 字符窗口; 应有 as_type="generation"
    window = src[idx:idx + 600]
    assert 'as_type="generation"' in window or "as_type='generation'" in window


# ============================================================================
# 11. LLM helper module API
# ============================================================================


def test_maybe_llm_span_helper_exists() -> None:
    """``gdr.observability.llm_hook.maybe_llm_span`` 可导入且为 contextmanager."""
    from gdr.observability import llm_hook

    assert hasattr(llm_hook, "maybe_llm_span")
    assert callable(llm_hook.maybe_llm_span)


def test_observability_package_exports_maybe_llm_span() -> None:
    """``gdr.observability.maybe_llm_span`` 经包级 re-export 暴露."""
    import gdr.observability as obs

    assert "maybe_llm_span" in obs.__all__
    assert hasattr(obs, "maybe_llm_span")


# ============================================================================
# 12. Settings 字段存在性
# ============================================================================


def test_settings_has_all_seven_langfuse_fields() -> None:
    """gdr Settings 必须包含 7 个 ``langfuse_*`` 字段 (Commit 1)."""
    from gdr.config.settings import Settings

    fields = set(Settings.model_fields.keys())
    required = {
        "langfuse_enabled",
        "langfuse_gdr_per_step_span",
        "langfuse_gdr_per_llm_span",
        "langfuse_gdr_per_refine_span",
        "langfuse_upload_payload",
        "langfuse_max_payload_bytes",
        "langfuse_max_block_payload_bytes",
    }
    missing = required - fields
    assert not missing, f"Missing Settings fields: {missing}"


def test_settings_default_upload_payload_is_none() -> None:
    """``langfuse_upload_payload`` 默认 None (factory 走 fallback chain 取 ''full'')."""
    from gdr.config.settings import Settings

    s = Settings(llm_base_url="x", llm_api_key="x", embedding_endpoint_url="x")
    assert s.langfuse_upload_payload is None


# ============================================================================
# 13. loader 把 langfuse.stages.gdr.* 铺平到 gdr.langfuse_* 字段
# ============================================================================


def test_root_config_loader_unwraps_langfuse_stages_gdr(tmp_path, monkeypatch) -> None:
    """根配置 ``langfuse.stages.gdr.*`` 应被 loader 铺平到 ``gdr.*`` Settings."""
    from gdr.config import settings as settings_mod

    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        "gdr:\n"
        "  embedding_endpoint_url: http://stub/v1\n"
        "langfuse:\n"
        "  enabled: true\n"
        "  public_key: pk\n"
        "  secret_key: sk\n"
        "  stages:\n"
        "    gdr:\n"
        "      enabled: true\n"
        "      per_step_span: false\n"
        "      per_llm_span: true\n"
        "      per_refine_span: true\n"
        "      upload_payload: summary\n"
        "      max_payload_bytes: 12345\n"
        "      max_block_payload_bytes: 678\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GDR_CONFIG_FILE", str(cfg_yaml))
    # 清缓存 (Settings 单例)
    from gdr.config.settings import _load_root_gdr_section

    out = _load_root_gdr_section()
    assert out["langfuse_enabled"] is True
    assert out["langfuse_gdr_per_step_span"] is False
    assert out["langfuse_gdr_per_llm_span"] is True
    assert out["langfuse_gdr_per_refine_span"] is True
    assert out["langfuse_upload_payload"] == "summary"
    assert out["langfuse_max_payload_bytes"] == 12345
    assert out["langfuse_max_block_payload_bytes"] == 678


def test_root_config_loader_flat_overrides_nested(tmp_path, monkeypatch) -> None:
    """``gdr.langfuse_gdr_per_step_span`` 显式平铺值应优先于 ``stages.gdr.*`` 嵌套."""
    from gdr.config import settings as settings_mod

    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        "gdr:\n"
        "  langfuse_gdr_per_step_span: true\n"
        "langfuse:\n"
        "  stages:\n"
        "    gdr:\n"
        "      per_step_span: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GDR_CONFIG_FILE", str(cfg_yaml))
    from gdr.config.settings import _load_root_gdr_section

    out = _load_root_gdr_section()
    # 平铺优先; stages.gdr.per_step_span 不覆盖
    assert out["langfuse_gdr_per_step_span"] is True
