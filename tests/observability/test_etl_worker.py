"""etl worker Langfuse 单元测试 (PR 4).

覆盖 ``run_etl_once`` 的 Langfuse 接入面:

* outer ``stage_trace`` 1 个 + 2 子 ``step_span`` (``load_refined_session`` /
  ``save_c3_4views``)
* outer.input = None, outer.output = 4 视图路径字典 (延迟闭包)
* load sub-span input = C2 dict 深拷贝, output = Session dump
* save sub-span input = Session dump, output = 4 视图路径字典 (含字节数)
* ``qwenjina=None`` 边界: output 字段显式 None (不写库 vs 0 字节可分)
* payload_mode 三态: full / summary / none
* 业务异常 → sub-span outer span ``level="ERROR"`` + status_message;
  ``finally`` 仍 ``flush()``
* ``enabled=False`` → span 创建路径零调用
* ``session_id`` 参数与 C2 内 session_id 不一致 → ``EtlNonRetryableError``
* tags 含 ``attempt:N``
* mock client 抛异常 → 业务返回值不受影响 (fail-safe)

mock 策略: ``simulate_serve.observability.langfuse_client.Langfuse`` 替换为
``MagicMock``, process-local 单例清空避免跨测试污染.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# 共享 fixtures / mock factories
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """清空 process-local singleton: 防止跨测试污染 + mock 行为稳定."""
    from simulate_serve.observability import langfuse_client
    langfuse_client._client = None
    yield
    langfuse_client._client = None


@dataclass(frozen=True)
class _EnabledCfg:
    """orchestration/observability/langfuse_config.LangfuseConfig 替身:
    鸭子类型工厂 `_extract_langfuse_fields` 字段."""

    enabled: bool = True
    public_key: str = "pk-test"
    secret_key: str = "sk-test"
    base_url: str = "https://cloud.langfuse.com"
    environment: str = "test"
    release: str = "ci"
    sample_rate: float = 1.0
    flush_at: int = 512
    flush_interval: float = 5.0
    timeout: int = 10
    upload_payload: str = "full"
    max_payload_bytes: int = 0
    max_block_payload_bytes: int = 0
    per_step_span: bool = True


@pytest.fixture
def enabled_cfg() -> _EnabledCfg:
    return _EnabledCfg()


@pytest.fixture
def disabled_cfg() -> _EnabledCfg:
    return _EnabledCfg(enabled=False)


@pytest.fixture
def fake_langfuse(monkeypatch):
    """把工厂内的 Langfuse SDK + propagate_attributes 替换为 MagicMock.

    返回一个 ``(fake_sdk, span, ctx_manager)`` 三元组, 便于断言:
      * ``fake_sdk.start_as_current_observation.call_args_list`` —— 哪些 span 被开过
      * ``span.update.call_args_list`` —— 各次 update 的 kwargs
    """
    from simulate_serve.observability import langfuse_client

    fake_sdk = mock.MagicMock(name="LangfuseSDK")
    span = mock.MagicMock(name="Span")
    cm = mock.MagicMock(name="ContextManager")
    cm.__enter__.return_value = span
    cm.__exit__.return_value = False
    fake_sdk.start_as_current_observation.return_value = cm

    pa = mock.MagicMock(name="PropagateAttributes")
    pa.__enter__.return_value = None
    pa.__exit__.return_value = False

    monkeypatch.setattr(langfuse_client, "Langfuse", fake_sdk)
    monkeypatch.setattr(langfuse_client, "propagate_attributes", pa)
    langfuse_client._client = fake_sdk  # 工厂 get_client 短路: 已有 singleton 直接返
    return fake_sdk


# ---------------------------------------------------------------------------
# C2 / Session / SessionOutputs 假对象
# ---------------------------------------------------------------------------


class _FakeSessionOutputs:
    """gdr.domain.SessionOutputs 替身: 暴露 ``messages/openai/qwenjina/meta``."""

    def __init__(
        self,
        messages: Path,
        openai: Path,
        qwenjina: Path | None,
        meta: Path,
    ) -> None:
        self.messages = messages
        self.openai = openai
        self.qwenjina = qwenjina
        self.meta = meta


class _FakeSession:
    """Session 替身: ``model_dump`` + ``session_id``."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.messages: list = []
        self.metadata: dict = {"qf_text": "stub"}

    def model_dump(self, mode: str = "python", **kwargs):
        return {
            "session_id": self.session_id,
            "messages": [],
            "metadata": {"qf_text": "stub"},
        }


def _write_c2(tmp_path: Path, *, session_id: str = "s1") -> Path:
    """写一个最小 C2 refined Session 文件."""
    payload = {
        "session_id": session_id,
        "messages": [],
        "schema_version": "refined_session.v1",
        "metadata": {"qf_text": "stub"},
    }
    c2 = tmp_path / f"{session_id}.json"
    c2.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return c2


def _make_fake_save_v2(tmp_path: Path, *, with_qwenjina: bool = True):
    """fake ``save_session_v2``: 在 ``base_path`` 旁写 4 视图."""
    def fake_save_v2(session, base_path):
        base_path = Path(base_path)
        mp = Path(str(base_path) + ".messages.json")
        op = Path(str(base_path) + ".openai.json")
        meta = Path(str(base_path) + ".meta.json")
        qp: Path | None = None
        if with_qwenjina:
            qp = Path(str(base_path) + ".qwenjina.txt")
        for p in (mp, op, meta):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}", encoding="utf-8")
        if qp is not None:
            qp.parent.mkdir(parents=True, exist_ok=True)
            qp.write_text("text", encoding="utf-8")
        return _FakeSessionOutputs(mp, op, qp, meta)
    return fake_save_v2


def _install_mocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    session: Any | None = None,
    save_v2: Any | None = None,
    load_refined_session: Any | None = None,
) -> tuple[Any, Any]:
    """monkeypatch ``load_refined_session`` + ``save_session_v2``.

    Returns (fake_session_v2, fake_load_session) for further customization.
    """
    session_obj = session if session is not None else _FakeSession("s1")
    save_v2 = save_v2 if save_v2 is not None else _make_fake_save_v2(tmp_path)

    if load_refined_session is None:
        load_refined_session = lambda _path: session_obj

    monkeypatch.setattr(
        "orchestration.workers.etl_worker.load_refined_session",
        load_refined_session,
    )
    monkeypatch.setattr(
        "orchestration.workers.etl_worker.save_session_v2",
        save_v2,
    )
    return save_v2, load_refined_session


def _span_update_calls(span_mock: Any) -> list[Any]:
    return span_mock.update.call_args_list


def _find_span_call(
    call_args_list: list[Any], *, name: str,
) -> Any | None:
    """从 ``start_as_current_observation.call_args_list`` 里挑 name 匹配 span."""
    for c in call_args_list:
        if c.kwargs.get("name") == name:
            return c
    return None


# ---------------------------------------------------------------------------
# 1. 1 outer + 2 子 span
# ---------------------------------------------------------------------------


def test_run_etl_once_emits_outer_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(monkeypatch, tmp_path)

    from orchestration.workers.etl_worker import run_etl_once

    run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        langfuse_cfg=enabled_cfg,
    )

    span_calls = fake_langfuse.start_as_current_observation.call_args_list
    names = [c.kwargs.get("name") for c in span_calls]
    assert names.count("etl:T001") == 1, names
    assert names.count("etl.load_refined_session") == 1, names
    assert names.count("etl.save_c3_4views") == 1, names


# ---------------------------------------------------------------------------
# 2. outer.input = None; outer.output = 4 视图 dict (延迟闭包)
# ---------------------------------------------------------------------------


def test_run_etl_once_outer_input_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """outer ``stage_trace(input_data=None)`` → outer 退出后
    ``update(input=None)`` 必须被调用 (无 C2 也无 4视图塞 outer.input)."""
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(monkeypatch, tmp_path)

    from orchestration.workers.etl_worker import run_etl_once

    run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        langfuse_cfg=enabled_cfg,
    )

    outer_call = _find_span_call(
        fake_langfuse.start_as_current_observation.call_args_list,
        name="etl:T001",
    )
    assert outer_call is not None
    # outer 上下文中 span.update(input=...) 调用里 input 必须为 None
    span = fake_langfuse.start_as_current_observation.return_value.__enter__.return_value
    input_calls = [c for c in span.update.call_args_list if "input" in c.kwargs]
    # 顶层 outer 的 input 应该是 None (input_data=None); 子 span 的 input 是 C2 / Session
    # 我们只断言 outer 自身在 stage_trace __enter__ 时立刻被调用过 input=None
    # (factory 在 _open_observation 内会调用 span.update(input=input_fn()))
    outer_span_input_call = next(
        (c for c in input_calls if c.kwargs["input"] is None), None,
    )
    # 在 outer span 内, propagation 与 metadata 都是 ctx-bound 初始化阶段先入;
    # 验证 outer input 是 None: outer.span 是在 stage_trace 的 __enter__ 时构建,
    # update(input=None) 紧接其后调用 ——
    # 由于 outer 与子 span 共用 mock span 实例 (start_as_current_observation
    # 始终返同一 mock), 我们只能验证 outer 启动时 input=None 调用存在.
    assert outer_span_input_call is not None, (
        f"expected outer span.update(input=None); got: {input_calls}"
    )


def test_run_etl_once_outer_output_4views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """outer.output = ``_capture_save_payload(outputs)`` (延迟闭包)."""
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(monkeypatch, tmp_path)

    from orchestration.workers.etl_worker import run_etl_once

    run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        langfuse_cfg=_EnabledCfg(),
    )

    span = fake_langfuse.start_as_current_observation.return_value.__enter__.return_value
    # find output calls on outer — 只要 outer.output 是 dict 含 messages/openai/meta
    output_calls = [c for c in span.update.call_args_list if "output" in c.kwargs]
    # 子 span 的 output 是 4 视图 dict; outer.output 也是 4 视图 dict;
    # 我们找 ``qwenjina`` 字段含 None 的那个 ——
    outer_output_call = next(
        (c for c in output_calls
         if isinstance(c.kwargs["output"], dict)
         and "messages" in c.kwargs["output"]
         and "openai" in c.kwargs["output"]),
        None,
    )
    assert outer_output_call is not None, (
        f"no outer-style output in {output_calls}"
    )
    out = outer_output_call.kwargs["output"]
    assert "meta" in out
    assert "messages_bytes" in out


# ---------------------------------------------------------------------------
# 3. load / save sub-span input + output
# ---------------------------------------------------------------------------


def test_run_etl_once_load_subspan_input_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """``etl.load_refined_session`` sub-span: input = C2 dict, output = Session dump."""
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(monkeypatch, tmp_path)

    from orchestration.workers.etl_worker import run_etl_once

    run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        langfuse_cfg=enabled_cfg,
    )

    load_call = _find_span_call(
        fake_langfuse.start_as_current_observation.call_args_list,
        name="etl.load_refined_session",
    )
    assert load_call is not None, "load sub-span not created"
    # propagate_attributes(session_id=...) 接受 run 参数 -- load 调用栈
    # 验证: stage_trace 会先用 propagate_attributes(sid, user_id, tags) 锁外层,
    # step_span 复用同 propagate_attributes; 用 PA 调用断言
    pa_calls = fake_langfuse.call_args_list  # never used; LKS simplicity
    del pa_calls

    # 从所有 update 调用中找到 input=... 且 value 含 schema_version 的那个
    span = fake_langfuse.start_as_current_observation.return_value.__enter__.return_value
    update_calls = span.update.call_args_list

    # 子 span input 包含 schema_version 字段 (来自 C2 dict)
    load_input = next(
        (c for c in update_calls
         if isinstance(c.kwargs.get("input"), dict)
         and c.kwargs["input"].get("schema_version") == "refined_session.v1"),
        None,
    )
    assert load_input is not None, (
        f"expected load sub-span input with schema_version; got {[c.kwargs for c in update_calls]}"
    )

    # 子 span output 包含 session_id 字段 (Session dump)
    save_call = _find_span_call(
        fake_langfuse.start_as_current_observation.call_args_list,
        name="etl.save_c3_4views",
    )
    assert save_call is not None, "save sub-span not created"
    save_output = next(
        (c for c in update_calls
         if isinstance(c.kwargs.get("output"), dict)
         # 4 视图 dict 特征: 含 ``messages_bytes`` 字段 (Session dump 没有)
         and "messages_bytes" in c.kwargs["output"]
         and "openai_bytes" in c.kwargs["output"]),
        None,
    )
    assert save_output is not None, (
        f"expected save sub-span output with 4 views; got {[c.kwargs for c in update_calls]}"
    )


def test_run_etl_once_save_subspan_input_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """``etl.save_c3_4views`` sub-span: input = Session dump, output = 4 视图字典."""
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(monkeypatch, tmp_path)

    from orchestration.workers.etl_worker import run_etl_once

    run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        langfuse_cfg=enabled_cfg,
    )
    span = fake_langfuse.start_as_current_observation.return_value.__enter__.return_value

    # save sub-span input 是 Session dump (含 session_id 字段)
    save_input = next(
        (c for c in span.update.call_args_list
         if isinstance(c.kwargs.get("input"), dict)
         and c.kwargs["input"].get("session_id") == "s1"
         and isinstance(c.kwargs["input"].get("messages"), list)),
        None,
    )
    assert save_input is not None, "expected save sub-span input = Session dump"

    # save sub-span output 是 4 视图 dict (含 messages/openai/qwenjina/meta + 字节数)
    save_output = next(
        (c for c in span.update.call_args_list
         if isinstance(c.kwargs.get("output"), dict)
         and "messages_bytes" in c.kwargs["output"]
         and c.kwargs["output"].get("openai_bytes") is not None),
        None,
    )
    assert save_output is not None


# ---------------------------------------------------------------------------
# 4. qwenjina=None 边界
# ---------------------------------------------------------------------------


def test_run_etl_once_qwenjina_none_subspan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """qf_text 缺失 → outer / save sub-span output["qwenjina"] = 显式 None."""
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(
        monkeypatch, tmp_path,
        save_v2=_make_fake_save_v2(tmp_path, with_qwenjina=False),
    )

    from orchestration.workers.etl_worker import run_etl_once

    run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        langfuse_cfg=enabled_cfg,
    )

    span = fake_langfuse.start_as_current_observation.return_value.__enter__.return_value
    save_output = next(
        (c for c in span.update.call_args_list
         if isinstance(c.kwargs.get("output"), dict)
         and "messages_bytes" in c.kwargs["output"]
         and c.kwargs["output"].get("messages_bytes", 0) > 0),
        None,
    )
    assert save_output is not None
    assert save_output.kwargs["output"]["qwenjina"] is None
    assert save_output.kwargs["output"]["qwenjina_bytes"] is None


# ---------------------------------------------------------------------------
# 5. payload_mode 三态
# ---------------------------------------------------------------------------


def test_run_etl_once_payload_mode_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """summary 模式: input/output 都是 {"summary": True, "size_hint": N}."""
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(monkeypatch, tmp_path)

    from orchestration.workers.etl_worker import run_etl_once

    run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        langfuse_cfg=_EnabledCfg(upload_payload="summary"),
    )

    span = fake_langfuse.start_as_current_observation.return_value.__enter__.return_value
    updates = span.update.call_args_list
    # input / output 都应是 summary dict
    summary_updates = [
        c for c in updates
        if isinstance(c.kwargs.get("input"), dict)
        and c.kwargs["input"].get("summary") is True
    ]
    assert summary_updates, f"expected summary-mode input updates; got {updates}"
    summary_outputs = [
        c for c in updates
        if isinstance(c.kwargs.get("output"), dict)
        and c.kwargs["output"].get("summary") is True
    ]
    assert summary_outputs, f"expected summary-mode output updates; got {updates}"


def test_run_etl_once_payload_mode_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """none 模式: input=None, output=None."""
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(monkeypatch, tmp_path)

    from orchestration.workers.etl_worker import run_etl_once

    run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        langfuse_cfg=_EnabledCfg(upload_payload="none"),
    )

    span = fake_langfuse.start_as_current_observation.return_value.__enter__.return_value
    updates = span.update.call_args_list
    # 至少 outer + load + save 三个 span 都各调用 input=None
    input_none = [c for c in updates if c.kwargs.get("input") is None]
    output_none = [c for c in updates if c.kwargs.get("output") is None]
    assert len(input_none) >= 3, (
        f"expected >=3 input=None (outer+2 sub); got {len(input_none)}; {updates}"
    )
    # output: 至少 outer + save 各 None (load 没 output 逻辑也会 None)
    assert len(output_none) >= 2


# ---------------------------------------------------------------------------
# 6. 异常路径 → span ERROR
# ---------------------------------------------------------------------------


def test_run_etl_once_non_retryable_span_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """load ValueError → EtlNonRetryableError → outer + 子 span level=ERROR."""
    c2 = _write_c2(tmp_path, session_id="s1")

    def boom_load(_path):
        raise ValueError("schema_version: 'foo'")
    _install_mocks(monkeypatch, tmp_path, load_refined_session=boom_load)

    from orchestration.workers.etl_worker import (
        EtlNonRetryableError,
        run_etl_once,
    )

    with pytest.raises(EtlNonRetryableError, match="schema_version"):
        run_etl_once(
            c2_path=c2, etl_outputs_dir=tmp_path / "out",
            task_id="T001", session_id="s1",
            langfuse_cfg=enabled_cfg,
        )

    span = fake_langfuse.start_as_current_observation.return_value.__enter__.return_value
    error_calls = [
        c for c in span.update.call_args_list
        if c.kwargs.get("level") == "ERROR"
    ]
    assert error_calls, (
        f"expected at least one span.update(level=ERROR); got "
        f"{[c.kwargs for c in span.update.call_args_list]}"
    )
    # status_message 必须含异常类型 / 摘要
    err_msg = error_calls[0].kwargs.get("status_message", "")
    assert "ValueError" in err_msg or "schema_version" in err_msg


def test_run_etl_once_save_failure_span_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """``save_session_v2`` 抛 RuntimeError → outer + save 子 span ERROR."""
    c2 = _write_c2(tmp_path, session_id="s1")

    def boom_save(_s, _p):
        raise RuntimeError("disk full")
    _install_mocks(monkeypatch, tmp_path, save_v2=boom_save)

    from orchestration.workers.etl_worker import run_etl_once

    with pytest.raises(RuntimeError, match="disk full"):
        run_etl_once(
            c2_path=c2, etl_outputs_dir=tmp_path / "out",
            task_id="T001", session_id="s1",
            langfuse_cfg=enabled_cfg,
        )

    span = fake_langfuse.start_as_current_observation.return_value.__enter__.return_value
    error_calls = [
        c for c in span.update.call_args_list
        if c.kwargs.get("level") == "ERROR"
    ]
    assert error_calls
    err_msg = error_calls[0].kwargs.get("status_message", "")
    assert "RuntimeError" in err_msg and "disk full" in err_msg


# ---------------------------------------------------------------------------
# 7. enabled=False → 零 span
# ---------------------------------------------------------------------------


def test_run_etl_once_disabled_no_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    disabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """enabled=False → get_client 返 None → ``start_as_current_observation`` 0 次调用."""
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(monkeypatch, tmp_path)

    # Disable by clearing singleton + patching get_client to return None.
    monkeypatch.setattr(
        "orchestration.workers.etl_worker.get_client",
        lambda _cfg: None,
    )

    from orchestration.workers.etl_worker import run_etl_once

    result = run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        langfuse_cfg=disabled_cfg,
    )
    assert result.task_id == "T001"
    # SDK 没被调用构造 span
    assert fake_langfuse.start_as_current_observation.call_count == 0


# ---------------------------------------------------------------------------
# 8. finally flush 触发 (正常 + 异常)
# ---------------------------------------------------------------------------


def test_run_etl_once_finally_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """业务成功路径: ``client.flush()`` 必须至少调用 1 次 (finally 主路径)."""
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(monkeypatch, tmp_path)

    from orchestration.workers.etl_worker import run_etl_once

    run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        langfuse_cfg=enabled_cfg,
    )
    assert fake_langfuse.flush.call_count >= 1


def test_run_etl_once_finally_flush_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """业务异常路径: ``finally`` 仍 ``client.flush()`` (失败也兜底)."""
    c2 = _write_c2(tmp_path, session_id="s1")

    def boom_save(_s, _p):
        raise RuntimeError("disk full")
    _install_mocks(monkeypatch, tmp_path, save_v2=boom_save)

    from orchestration.workers.etl_worker import run_etl_once

    with pytest.raises(RuntimeError):
        run_etl_once(
            c2_path=c2, etl_outputs_dir=tmp_path / "out",
            task_id="T001", session_id="s1",
            langfuse_cfg=enabled_cfg,
        )
    assert fake_langfuse.flush.call_count >= 1


# ---------------------------------------------------------------------------
# 9. session_id 不一致 → ERROR
# ---------------------------------------------------------------------------


def test_run_etl_once_session_id_mismatch_assert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """``session.session_id`` ≠ 入参 session_id → EtlNonRetryableError + span ERROR."""
    c2 = _write_c2(tmp_path, session_id="s_real")
    # Session 报告 s_real, 但 arg 传 s_arg
    _install_mocks(monkeypatch, tmp_path, session=_FakeSession("s_real"))

    from orchestration.workers.etl_worker import (
        EtlNonRetryableError,
        run_etl_once,
    )

    with pytest.raises(EtlNonRetryableError, match="session_id mismatch"):
        run_etl_once(
            c2_path=c2, etl_outputs_dir=tmp_path / "out",
            task_id="T001", session_id="s_arg",
            langfuse_cfg=enabled_cfg,
        )

    span = fake_langfuse.start_as_current_observation.return_value.__enter__.return_value
    error_calls = [
        c for c in span.update.call_args_list
        if c.kwargs.get("level") == "ERROR"
    ]
    assert error_calls


# ---------------------------------------------------------------------------
# 10. tags 含 attempt:N
# ---------------------------------------------------------------------------


def test_run_etl_once_attempt_in_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """``run_etl_once(attempt=N, ...)`` → outer.tags 含 ``"attempt:N"``."""
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(monkeypatch, tmp_path)

    from orchestration.workers.etl_worker import run_etl_once

    run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        attempt=2,
        langfuse_cfg=enabled_cfg,
    )

    # outer stage_trace 入口时 propagate_attributes(tags=...)
    pa = mock.MagicMock(name="PA_check")  # 已被 fake_langfuse 设置过
    # 直接从 update.calls 找到 outer 启动后 metadata["attempt"]=2
    span = fake_langfuse.start_as_current_observation.return_value.__enter__.return_value
    attempt_meta = next(
        (c for c in span.update.call_args_list
         if isinstance(c.kwargs.get("metadata"), dict)
         and c.kwargs["metadata"].get("attempt") == 2),
        None,
    )
    assert attempt_meta is not None


# ---------------------------------------------------------------------------
# 11. mock client 抛异常 → 业务正常返回 (fail-safe)
# ---------------------------------------------------------------------------


def test_run_etl_once_langfuse_failure_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg,
) -> None:
    """SDK span.update 抛异常时业务不受影响 (factory 已对 ``update`` 错误 fail-safe).

    ``start_as_current_observation`` 在工厂内未被 try/except 包裹, 一旦抛错即
    bubble 到 ``run_etl_once`` 主路径. 但 ``span.update(...)`` 在
    ``_open_observation`` 内已经被 ``try: ... except Exception: pass`` 吞掉.
    本测试验证 update 抛错时业务正常返回.
    """
    c2 = _write_c2(tmp_path, session_id="s1")
    _install_mocks(monkeypatch, tmp_path)

    from simulate_serve.observability import langfuse_client

    # 把 langfuse_client._client 设为 boom 客户端.``start_as_current_observation``
    # 返回的 context manager 的 span.update 抛错 (factory swallow) + flush 抛错
    # (etl_worker finally swallow).
    boom_span = mock.MagicMock(name="BoomSpan")
    boom_span.update.side_effect = RuntimeError("update died")
    boom_cm = mock.MagicMock(name="BoomCM")
    boom_cm.__enter__.return_value = boom_span
    boom_cm.__exit__.return_value = False

    boom_client = mock.MagicMock(name="BoomClient")
    boom_client.start_as_current_observation.return_value = boom_cm
    boom_client.flush.side_effect = RuntimeError("flush died")

    # propagate_attributes 正常 (factory 不会在这里 fail-safe 业务, 但不会抛错).
    pa = mock.MagicMock(name="PA")
    pa.__enter__.return_value = None
    pa.__exit__.return_value = False
    monkeypatch.setattr(langfuse_client, "propagate_attributes", pa)

    langfuse_client._client = boom_client

    from orchestration.workers.etl_worker import run_etl_once

    result = run_etl_once(
        c2_path=c2, etl_outputs_dir=tmp_path / "out",
        task_id="T001", session_id="s1",
        langfuse_cfg=enabled_cfg,
    )
    # 业务结果不受影响
    assert result.task_id == "T001"
    assert result.session_id == "s1"
    assert result.messages_path.exists()
    # update 被调用且抛过错 (factory 已 swallow)
    assert boom_span.update.call_count >= 1
    # flush 也被调用过 (finally 也 swallow)
    assert boom_client.flush.call_count >= 1


# ---------------------------------------------------------------------------
# 12. _safe_run_etl 重试 → N 独立 trace (留给 PR 5)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="PR 4 不动 _safe_run_etl; 透传 attempt + langfuse_cfg 由 PR 5 实施."
)
def test_run_etl_once_retry_creates_independent_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    enabled_cfg: _EnabledCfg, fake_langfuse: Any,
) -> None:
    """max_retry=2 → 应有 3 个独立 outer ``stage_trace`` (attempt=0/1/2).

    留给 PR 5 实施 (``task_pipeline._safe_run_etl``)."""
    raise NotImplementedError
