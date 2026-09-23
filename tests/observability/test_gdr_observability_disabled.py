"""gdr Langfuse 可观测性 disabled 状态测试 (PR 3 / Commit 1).

边界: ``langfuse_enabled=False`` 时
1. ``_gdr_step_span_ctx`` yield None (业务流程零侵入)
2. ``get_client`` 返回 None (无 SDK 调用)
3. ``_payload_mode`` / ``_per_step_enabled`` / ``_max_block_payload_bytes``
   按扁平字段语义返回
4. ``set_current_task_id`` / ``_current_task_id`` 线程隔离 (thread-local)
5. ``snapshot`` 不在 disabled 路径上调用 (零开销)

mock 策略: ``simulate_serve.observability.langfuse_client.Langfuse`` 替换
为 MagicMock, 单例清空避免跨测试污染。
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """测试间清空 process-local singleton; mock 工厂 SDK。"""
    from simulate_serve.observability import langfuse_client
    langfuse_client._client = None
    yield
    langfuse_client._client = None


@pytest.fixture
def fake_langfuse(monkeypatch):
    """把工厂内的 Langfuse SDK 替换为 mock, 并返回 fake 句柄便于断言。"""
    from simulate_serve.observability import langfuse_client
    fake = mock.MagicMock(name="LangfuseSDK")
    span = mock.MagicMock(name="Span")
    cm = mock.MagicMock(name="ContextManager")
    cm.__enter__.return_value = span
    cm.__exit__.return_value = False
    fake.start_as_current_observation.return_value = cm

    pa = mock.MagicMock(name="PropagateAttributes")
    pa.__enter__.return_value = None
    pa.__exit__.return_value = False

    monkeypatch.setattr(langfuse_client, "Langfuse", fake)
    monkeypatch.setattr(langfuse_client, "propagate_attributes", pa)
    return fake


# ---------------------------------------------------------------------------
# 测试 1: langfuse_enabled=False → _gdr_step_span_ctx yield None
# ---------------------------------------------------------------------------


def test_step_span_ctx_yields_none_when_disabled(fake_langfuse) -> None:
    """``langfuse_enabled=False`` 时 21 步骤 helper 不构造任何 span。"""
    from gdr.observability import _gdr_step_span_ctx

    @dataclass
    class _Cfg:
        langfuse_enabled: bool = False
        langfuse_gdr_per_step_span: bool = True

    class _Session:
        _gdr_cfg = _Cfg()
        session_id = "s1"

    with _gdr_step_span_ctx("gdr.hard_filter", _Session()) as span:
        assert span is None
    # SDK 没被调用
    assert fake_langfuse.start_as_current_observation.call_count == 0


# ---------------------------------------------------------------------------
# 测试 2: per_step=False → helper 不构造 span
# ---------------------------------------------------------------------------


def test_step_span_ctx_yields_none_when_per_step_disabled(fake_langfuse) -> None:
    """``langfuse_enabled=True`` 但 ``per_step_span=False`` 时也不起 span。"""
    from gdr.observability import _gdr_step_span_ctx

    @dataclass
    class _Cfg:
        langfuse_enabled: bool = True
        langfuse_gdr_per_step_span: bool = False
        langfuse_public_key: str = "pk"
        langfuse_secret_key: str = "sk"

    class _Session:
        _gdr_cfg = _Cfg()
        session_id = "s1"

    with _gdr_step_span_ctx("gdr.hard_filter", _Session()) as span:
        assert span is None
    assert fake_langfuse.start_as_current_observation.call_count == 0


# ---------------------------------------------------------------------------
# 测试 3: 缺 _gdr_cfg → yield None
# ---------------------------------------------------------------------------


def test_step_span_ctx_yields_none_when_session_lacks_cfg(fake_langfuse) -> None:
    """``session._gdr_cfg`` 未绑定时 yield None (兜底, 不抛)。"""
    from gdr.observability import _gdr_step_span_ctx

    class _Session:
        # 注意: 没有 _gdr_cfg 属性
        session_id = "s1"

    s = _Session()
    with _gdr_step_span_ctx("gdr.hard_filter", s) as span:
        assert span is None


# ---------------------------------------------------------------------------
# 测试 4: _payload_mode 扁平字段优先
# ---------------------------------------------------------------------------


def test_payload_mode_prefers_flat_langfuse_field() -> None:
    from gdr.observability import _payload_mode

    @dataclass
    class _Cfg:
        langfuse_upload_payload: str = "summary"
        upload_payload: str = "full"  # 回退目标, 不应被读到

    assert _payload_mode(_Cfg()) == "summary"


def test_payload_mode_falls_back_to_nested_upload_payload() -> None:
    """扁平字段为 None 时回退嵌套 ``upload_payload`` (兼容旧 Settings)。"""
    from gdr.observability import _payload_mode

    @dataclass
    class _Cfg:
        langfuse_upload_payload: str | None = None
        upload_payload: str = "summary"

    assert _payload_mode(_Cfg()) == "summary"


def test_payload_mode_defaults_to_full() -> None:
    """都没有时默认 ``full`` (工厂 ``_resolve_payload`` 默认行为一致)。"""
    from gdr.observability import _payload_mode

    @dataclass
    class _Empty:
        pass

    assert _payload_mode(_Empty()) == "full"


# ---------------------------------------------------------------------------
# 测试 5: _per_step_enabled 复合判定
# ---------------------------------------------------------------------------


def test_per_step_enabled_requires_both_flags() -> None:
    from gdr.observability import _per_step_enabled

    @dataclass
    class _Cfg1:
        langfuse_enabled: bool = True
        langfuse_gdr_per_step_span: bool = True

    @dataclass
    class _Cfg2:
        langfuse_enabled: bool = False
        langfuse_gdr_per_step_span: bool = True

    @dataclass
    class _Cfg3:
        langfuse_enabled: bool = True
        langfuse_gdr_per_step_span: bool = False

    assert _per_step_enabled(_Cfg1()) is True
    assert _per_step_enabled(_Cfg2()) is False
    assert _per_step_enabled(_Cfg3()) is False


def test_per_step_enabled_defaults_true_when_only_enabled_set() -> None:
    """``langfuse_gdr_per_step_span`` 缺省 True, ``langfuse_enabled=True`` 时即可开启。"""
    from gdr.observability import _per_step_enabled

    @dataclass
    class _Cfg:
        langfuse_enabled: bool = True

    assert _per_step_enabled(_Cfg()) is True


# ---------------------------------------------------------------------------
# 测试 6: _max_block_payload_bytes 整数转换
# ---------------------------------------------------------------------------


def test_max_block_payload_bytes_int_coercion() -> None:
    from gdr.observability import _max_block_payload_bytes

    @dataclass
    class _Cfg:
        langfuse_max_block_payload_bytes: int = 2048

    assert _max_block_payload_bytes(_Cfg()) == 2048
    assert _max_block_payload_bytes(_Cfg(langfuse_max_block_payload_bytes=0)) == 0


def test_max_block_payload_bytes_default_zero() -> None:
    """未设时默认 0 (= 不截断)。"""
    from gdr.observability import _max_block_payload_bytes

    @dataclass
    class _Empty:
        pass

    assert _max_block_payload_bytes(_Empty()) == 0


# ---------------------------------------------------------------------------
# 测试 7: set_current_task_id 线程隔离
# ---------------------------------------------------------------------------


def test_set_current_task_id_thread_isolation() -> None:
    """thread-local: 不同线程独立设置互不污染。"""
    import threading

    from gdr.observability import _current_task_id, set_current_task_id

    barrier = threading.Barrier(2)
    seen = {}

    def worker(name: str, tid: str) -> None:
        set_current_task_id(tid)
        barrier.wait()  # 等另一线程也设完
        seen[name] = _current_task_id()
        set_current_task_id(None)

    t1 = threading.Thread(target=worker, args=("t1", "T001"))
    t2 = threading.Thread(target=worker, args=("t2", "T002"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert seen == {"t1": "T001", "t2": "T002"}


def test_set_current_task_id_clear() -> None:
    """finally 清理: 设 None 后 _current_task_id 返回 None。"""
    from gdr.observability import _current_task_id, set_current_task_id

    set_current_task_id("TXXX")
    assert _current_task_id() == "TXXX"
    set_current_task_id(None)
    assert _current_task_id() is None


# ---------------------------------------------------------------------------
# 测试 8: 零开销验证 — disabled 时 deepcopy 不被调
# ---------------------------------------------------------------------------


def test_disabled_zero_overhead_no_snapshot(fake_langfuse, monkeypatch) -> None:
    """disabled 路径上不调 ``snapshot`` (避免 21 步 × O(N) 开销)。"""
    from gdr.observability import runner_helpers

    calls = {"snapshot": 0}

    def counting_snapshot(obj):
        calls["snapshot"] += 1
        return obj

    monkeypatch.setattr(runner_helpers, "snapshot", counting_snapshot)

    from gdr.observability import _gdr_step_span_ctx

    @dataclass
    class _Cfg:
        langfuse_enabled: bool = False

    class _Session:
        _gdr_cfg = _Cfg()
        session_id = "s1"

    with _gdr_step_span_ctx("gdr.hard_filter", _Session()) as span:
        assert span is None
    assert calls["snapshot"] == 0


# ---------------------------------------------------------------------------
# 测试 9: 工厂 get_client 仍生效 (workspace 成员 import 验证)
# ---------------------------------------------------------------------------


def test_gdr_can_import_factory() -> None:
    """gdr 能 import 工厂 (PR 1 工厂公开 API)。"""
    from simulate_serve.observability.langfuse_client import (
        get_client,
        snapshot,
        step_span,
    )
    assert callable(get_client)
    assert callable(snapshot)
    assert callable(step_span)
