"""Unit tests for the Langfuse factory + simulate_serve config defaults.

These tests mock the Langfuse SDK so they do not touch the network even
when credentials are present (CLAUDE.md constraint: default CI must not
reach public endpoints).
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from typing import Any
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """Reset the process-local Langfuse singleton between tests."""
    from simulate_serve.observability import langfuse_client
    langfuse_client._client = None
    yield
    langfuse_client._client = None


# ---------------------------------------------------------------------------
# simulate_serve.config.LangfuseConfig defaults
# ---------------------------------------------------------------------------


def test_simulate_serve_langfuse_config_defaults_disabled() -> None:
    """Default ``enabled=False`` — zero-impact on legacy batches."""
    from simulate_serve.config import LangfuseConfig
    cfg = LangfuseConfig()
    assert cfg.enabled is False
    assert cfg.public_key == ""
    assert cfg.secret_key == ""
    assert cfg.base_url == "https://cloud.langfuse.com"
    assert cfg.upload_payload == "full"


def test_simulate_serve_langfuse_config_rejects_invalid_sample_rate() -> None:
    """sample_rate is bounded [0, 1]."""
    from pydantic import ValidationError

    from simulate_serve.config import LangfuseConfig
    with pytest.raises(ValidationError):
        LangfuseConfig(sample_rate=1.5)
    with pytest.raises(ValidationError):
        LangfuseConfig(sample_rate=-0.1)


def test_simulate_serve_appconfig_default_has_langfuse() -> None:
    """AppConfig always carries a LangfuseConfig (default factory)."""
    from simulate_serve.config import AppConfig
    cfg = AppConfig()
    assert cfg.langfuse.enabled is False


def test_simulate_serve_load_config_pulls_root_langfuse_section(tmp_path) -> None:
    """Root ``langfuse:`` section flows into ``AppConfig.langfuse`` via ``_parse_config_raw``.

    Real root config shape: ``simulate_serve:`` is required (the parser
    triggers the section-extraction path when this top-level key is
    present); root ``langfuse:`` is then mapped to ``simulate_serve.langfuse``
    when the latter is absent. ``llm:`` is consumed by ``llm_defaults()`` to
    fill ``model.model_name/api_key/base_url`` and is NOT forwarded as an
    AppConfig field (AppConfig is extra_forbid).
    """
    import yaml

    from simulate_serve.config import load_config
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({
        "llm": {"base_url": "http://llm/v1", "api_key": "k", "model": "m1"},
        "simulate_serve": {},   # empty simulate_serve so _parse_config_raw returns the section
        "langfuse": {
            "enabled": True,
            "public_key": "pk-test",
            "secret_key": "sk-test",
            "environment": "ci",
        },
    }), encoding="utf-8")
    cfg = load_config(str(fp))
    assert cfg.langfuse.enabled is True
    assert cfg.langfuse.public_key == "pk-test"
    assert cfg.langfuse.environment == "ci"
    # llm: section flowed into model.* per the existing _parse_config_raw contract
    assert cfg.model.model_name == "m1"


def test_simulate_serve_load_config_simulate_serve_langfuse_overrides_root(
    tmp_path,
) -> None:
    """``simulate_serve.langfuse:`` overrides root ``langfuse:``."""
    import yaml

    from simulate_serve.config import load_config
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({
        "langfuse": {"enabled": True, "public_key": "pk-root"},
        "simulate_serve": {
            "langfuse": {"enabled": False, "public_key": "pk-local"},
        },
    }), encoding="utf-8")
    cfg = load_config(str(fp))
    assert cfg.langfuse.enabled is False
    assert cfg.langfuse.public_key == "pk-local"


# ---------------------------------------------------------------------------
# _extract_langfuse_fields — duck typing
# ---------------------------------------------------------------------------


def test_extract_fields_from_nested_pydantic_config() -> None:
    from simulate_serve.config import LangfuseConfig
    from simulate_serve.observability.langfuse_client import _extract_langfuse_fields
    cfg = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
    fields = _extract_langfuse_fields(cfg)
    assert fields["enabled"] is True
    assert fields["public_key"] == "pk"
    assert fields["secret_key"] == "sk"
    assert fields["base_url"] == "https://cloud.langfuse.com"


def test_extract_fields_from_flat_pydantic_settings_style() -> None:
    """gdr Settings style: flat ``langfuse_*`` attributes."""
    from simulate_serve.observability.langfuse_client import _extract_langfuse_fields

    @dataclass
    class FlatCfg:
        langfuse_enabled: bool = True
        langfuse_public_key: str = "pk-flat"
        langfuse_secret_key: str = "sk-flat"
        langfuse_upload_payload: str = "summary"

    fields = _extract_langfuse_fields(FlatCfg())
    assert fields["enabled"] is True
    assert fields["public_key"] == "pk-flat"
    assert fields["secret_key"] == "sk-flat"
    assert fields["upload_payload"] == "summary"


def test_extract_fields_from_frozen_dataclass() -> None:
    """orchestration LangfuseConfig (frozen dataclass) is accepted."""
    from orchestration.observability.langfuse_config import LangfuseConfig
    from simulate_serve.observability.langfuse_client import _extract_langfuse_fields
    cfg = LangfuseConfig(enabled=True, public_key="pk-d", secret_key="sk-d")
    fields = _extract_langfuse_fields(cfg)
    assert fields["enabled"] is True
    assert fields["public_key"] == "pk-d"
    assert fields["secret_key"] == "sk-d"


def test_extract_fields_applies_defaults_when_blank() -> None:
    """Empty / missing fields fall back to safe defaults."""
    from simulate_serve.observability.langfuse_client import _extract_langfuse_fields

    class Empty:
        pass

    fields = _extract_langfuse_fields(Empty())
    assert fields["enabled"] is False
    assert fields["base_url"] == "https://cloud.langfuse.com"
    assert fields["sample_rate"] == 1.0
    assert fields["flush_at"] == 512
    assert fields["flush_interval"] == 5.0
    assert fields["timeout"] == 10
    assert fields["upload_payload"] == "full"
    assert fields["max_payload_bytes"] == 0
    assert fields["max_block_payload_bytes"] == 0


def test_extract_fields_handles_max_block_payload_bytes_flat() -> None:
    """``langfuse_max_block_payload_bytes`` (gdr flat style) is mapped."""
    from simulate_serve.observability.langfuse_client import _extract_langfuse_fields

    @dataclass
    class FlatCfg:
        langfuse_enabled: bool = True
        langfuse_max_block_payload_bytes: int = 204800

    fields = _extract_langfuse_fields(FlatCfg())
    assert fields["max_block_payload_bytes"] == 204800


def test_extract_fields_handles_none_for_numeric_fields() -> None:
    """Pydantic may hand back None for unset optional ints; defaults still apply."""
    from simulate_serve.observability.langfuse_client import _extract_langfuse_fields

    class Partial:
        enabled = True
        public_key = "pk"
        secret_key = "sk"
        flush_at = None           # pydantic Optional[int] default
        timeout = None
        sample_rate = None

    fields = _extract_langfuse_fields(Partial())
    assert fields["flush_at"] == 512
    assert fields["timeout"] == 10
    assert fields["sample_rate"] == 1.0


# ---------------------------------------------------------------------------
# get_client — disabled / missing creds / SDK init / fork-safety
# ---------------------------------------------------------------------------


def test_get_client_returns_none_when_disabled() -> None:
    from simulate_serve.config import LangfuseConfig
    from simulate_serve.observability.langfuse_client import get_client
    assert get_client(LangfuseConfig(enabled=False)) is None


def test_get_client_returns_none_when_credentials_missing() -> None:
    from simulate_serve.config import LangfuseConfig
    from simulate_serve.observability.langfuse_client import get_client
    assert get_client(LangfuseConfig(enabled=True, public_key="", secret_key="")) is None


def test_get_client_returns_none_when_sdk_missing() -> None:
    """When ``from langfuse import Langfuse`` failed at module import, get_client returns None."""
    from simulate_serve.config import LangfuseConfig
    from simulate_serve.observability import langfuse_client
    with mock.patch.object(langfuse_client, "Langfuse", None):
        # Need to bust the singleton since previous tests may have populated it.
        langfuse_client._client = None
        assert langfuse_client.get_client(
            LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        ) is None


def test_get_client_returns_singleton_when_valid(monkeypatch) -> None:
    """First call instantiates; subsequent calls reuse."""
    from simulate_serve.config import LangfuseConfig
    from simulate_serve.observability import langfuse_client

    fake = mock.MagicMock(name="Langfuse")
    with mock.patch.object(langfuse_client, "Langfuse", fake):
        langfuse_client._client = None
        cfg = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        c1 = langfuse_client.get_client(cfg)
        c2 = langfuse_client.get_client(cfg)
    assert c1 is c2
    assert fake.call_count == 1


def test_get_client_swallows_sdk_init_failure() -> None:
    """SDK raising at construction time must NOT crash — return None + WARNING."""
    from simulate_serve.config import LangfuseConfig
    from simulate_serve.observability import langfuse_client

    class Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("network down")

    with mock.patch.object(langfuse_client, "Langfuse", Boom):
        langfuse_client._client = None
        cfg = LangfuseConfig(enabled=True, public_key="pk", secret_key="sk")
        assert langfuse_client.get_client(cfg) is None


def test_reset_for_fork_clears_singleton() -> None:
    from simulate_serve.observability import langfuse_client
    langfuse_client._client = mock.MagicMock(name="client")
    langfuse_client._reset_for_fork()
    assert langfuse_client._client is None


def test_shutdown_flushes_and_resets(monkeypatch) -> None:
    from simulate_serve.observability import langfuse_client

    fake = mock.MagicMock(name="client")
    langfuse_client._client = fake
    langfuse_client.shutdown()
    assert fake.flush.called
    assert fake.shutdown.called
    assert langfuse_client._client is None


# ---------------------------------------------------------------------------
# snapshot / _to_jsonable / _maybe_truncate
# ---------------------------------------------------------------------------


def test_snapshot_deep_copies_pydantic_model() -> None:
    from simulate_serve.observability.langfuse_client import snapshot
    src = {"messages": [{"role": "user", "text": "hi"}]}
    snap = snapshot(src)
    snap["messages"].append({"role": "user", "text": "world"})
    assert src["messages"] == [{"role": "user", "text": "hi"}]


def test_to_jsonable_handles_pydantic_v2_model() -> None:
    from pydantic import BaseModel
    from simulate_serve.observability.langfuse_client import _to_jsonable

    class Msg(BaseModel):
        role: str
        text: str

    out = _to_jsonable({"first": Msg(role="user", text="hi")})
    assert out == {"first": {"role": "user", "text": "hi"}}


def test_to_jsonable_falls_back_to_str_for_unknown() -> None:
    from simulate_serve.observability.langfuse_client import _to_jsonable

    class Weird:
        def __str__(self):
            return "<weird>"

    assert _to_jsonable(Weird()) == "<weird>"


def test_maybe_truncate_returns_placeholder_when_over_limit() -> None:
    from simulate_serve.observability.langfuse_client import _maybe_truncate
    big = {"k": "v" * 5000}
    out = _maybe_truncate(big, max_bytes=128)
    assert isinstance(out, dict)
    assert out.get("_truncated") is True
    assert out.get("max") == 128
    assert out.get("keys") == ["k"]


def test_maybe_truncate_returns_obj_when_under_limit() -> None:
    from simulate_serve.observability.langfuse_client import _maybe_truncate
    small = {"k": "v"}
    assert _maybe_truncate(small, max_bytes=1024) == {"k": "v"}


def test_maybe_truncate_disabled_when_max_bytes_zero() -> None:
    from simulate_serve.observability.langfuse_client import _maybe_truncate
    big = {"k": "v" * 10000}
    out = _maybe_truncate(big, max_bytes=0)
    assert out == {"k": "v" * 10000}


# ---------------------------------------------------------------------------
# stage_trace / step_span — context managers + level=ERROR
# ---------------------------------------------------------------------------


def _patch_langfuse_module(monkeypatch) -> mock.MagicMock:
    """Install a fresh Langfuse SDK mock so get_client returns a known object."""
    from simulate_serve.observability import langfuse_client

    fake = mock.MagicMock(name="LangfuseSDK")
    # ``start_as_current_observation`` returns a context manager whose
    # __enter__ returns a span object exposing ``update``.
    span = mock.MagicMock(name="Span")
    cm = mock.MagicMock(name="ContextManager")
    cm.__enter__.return_value = span
    cm.__exit__.return_value = False
    fake.start_as_current_observation.return_value = cm

    # propagate_attributes is a context manager too
    pa = mock.MagicMock(name="PropagateAttributes")
    pa.__enter__.return_value = None
    pa.__exit__.return_value = False

    monkeypatch.setattr(langfuse_client, "Langfuse", fake)
    monkeypatch.setattr(langfuse_client, "propagate_attributes", pa)
    langfuse_client._client = None
    return fake


def test_stage_trace_yields_none_when_client_none() -> None:
    from simulate_serve.observability.langfuse_client import stage_trace
    with stage_trace(None, session_id="s1", name="x") as span:
        assert span is None


def test_stage_trace_calls_sdk_when_enabled(monkeypatch) -> None:
    fake = _patch_langfuse_module(monkeypatch)
    from simulate_serve.observability.langfuse_client import stage_trace

    with stage_trace(
        fake, session_id="s1", name="simulate_serve:T001",
        user_id="user1", task_id="T001",
        tags=["stage:simulate_serve", "task:T001"],
        metadata={"run_id": "r1"},
        input_data={"k": "v"},
    ) as span:
        assert span is not None
    fake.start_as_current_observation.assert_called_once()


def test_stage_trace_marks_error_on_exception(monkeypatch) -> None:
    fake = _patch_langfuse_module(monkeypatch)
    from simulate_serve.observability.langfuse_client import stage_trace

    span_mock = fake.start_as_current_observation.return_value.__enter__.return_value
    with pytest.raises(RuntimeError, match="boom"):
        with stage_trace(fake, session_id="s1", name="x"):
            raise RuntimeError("boom")
    # span.update must have been called with level=ERROR + status_message
    update_calls = span_mock.update.call_args_list
    error_call = next(
        (c for c in update_calls if c.kwargs.get("level") == "ERROR"),
        None,
    )
    assert error_call is not None, f"no level=ERROR update in {update_calls}"
    assert "RuntimeError" in error_call.kwargs["status_message"]
    assert "boom" in error_call.kwargs["status_message"]


def test_stage_trace_payload_none_mode_skips_input_output(monkeypatch) -> None:
    fake = _patch_langfuse_module(monkeypatch)
    from simulate_serve.observability.langfuse_client import stage_trace

    span_mock = fake.start_as_current_observation.return_value.__enter__.return_value
    with stage_trace(
        fake, session_id="s1", name="x",
        payload_mode="none",
        input_data={"k": "v"},
    ):
        pass
    # input update must be None
    input_calls = [c for c in span_mock.update.call_args_list if "input" in c.kwargs]
    assert all(c.kwargs["input"] is None for c in input_calls)


def test_stage_trace_payload_summary_mode(monkeypatch) -> None:
    fake = _patch_langfuse_module(monkeypatch)
    from simulate_serve.observability.langfuse_client import stage_trace

    span_mock = fake.start_as_current_observation.return_value.__enter__.return_value
    with stage_trace(
        fake, session_id="s1", name="x",
        payload_mode="summary",
        input_data={"k": "v" * 100},
    ):
        pass
    input_calls = [c for c in span_mock.update.call_args_list if "input" in c.kwargs]
    assert input_calls, "expected at least one input update"
    summary = input_calls[0].kwargs["input"]
    assert summary["summary"] is True


def test_stage_trace_payload_truncates_when_over_limit(monkeypatch) -> None:
    fake = _patch_langfuse_module(monkeypatch)
    from simulate_serve.observability.langfuse_client import stage_trace

    span_mock = fake.start_as_current_observation.return_value.__enter__.return_value
    big = {"k": "x" * 10000}
    with stage_trace(
        fake, session_id="s1", name="x",
        input_data=big, max_payload_bytes=256,
    ):
        pass
    input_calls = [c for c in span_mock.update.call_args_list if "input" in c.kwargs]
    assert input_calls[0].kwargs["input"].get("_truncated") is True


def test_step_span_propagates_session_id(monkeypatch) -> None:
    fake = _patch_langfuse_module(monkeypatch)
    from simulate_serve.observability.langfuse_client import step_span

    pa_mock = fake.start_as_current_observation.return_value.__enter__.return_value  # not used
    # Use the real propagate_attributes mock
    from simulate_serve.observability import langfuse_client

    pa = mock.MagicMock(name="PropagateAttributes")
    pa.__enter__.return_value = None
    pa.__exit__.return_value = False
    monkeypatch.setattr(langfuse_client, "propagate_attributes", pa)

    with step_span(
        fake, name="gdr.process_one",
        session_id="sess-1", task_id="T001",
        metadata={"step": "load"},
    ):
        pass
    # propagate_attributes must have received session_id
    assert pa.called
    kwargs = pa.call_args.kwargs
    assert kwargs["session_id"] == "sess-1"


def test_step_span_marks_error_on_exception(monkeypatch) -> None:
    fake = _patch_langfuse_module(monkeypatch)
    from simulate_serve.observability.langfuse_client import step_span

    span_mock = fake.start_as_current_observation.return_value.__enter__.return_value
    with pytest.raises(ValueError, match="bad"):
        with step_span(fake, name="x"):
            raise ValueError("bad")
    update_calls = span_mock.update.call_args_list
    error_call = next(
        (c for c in update_calls if c.kwargs.get("level") == "ERROR"),
        None,
    )
    assert error_call is not None
    assert "ValueError" in error_call.kwargs["status_message"]


def test_step_span_as_type_generation(monkeypatch) -> None:
    fake = _patch_langfuse_module(monkeypatch)
    from simulate_serve.observability.langfuse_client import step_span

    with step_span(fake, name="gdr.reassemble.l3_judge", as_type="generation"):
        pass
    kwargs = fake.start_as_current_observation.call_args.kwargs
    assert kwargs["as_type"] == "generation"


# ---------------------------------------------------------------------------
# orchestration LangfuseConfig loader
# ---------------------------------------------------------------------------


def test_orchestration_load_langfuse_config_returns_defaults_when_no_root(
    monkeypatch,
) -> None:
    """When root config is missing, returns safe defaults (enabled=False)."""
    from orchestration.observability.langfuse_config import load_langfuse_config
    monkeypatch.setattr(
        "orchestration.observability.langfuse_config.find_root_config",
        lambda: None,
    )
    cfg = load_langfuse_config()
    assert cfg.enabled is False
    assert cfg.upload_payload == "full"


def test_orchestration_load_langfuse_config_reads_section(tmp_path) -> None:
    """Loader reads the ``langfuse:`` section from the root config."""
    import yaml

    from orchestration.observability.langfuse_config import load_langfuse_config
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({
        "langfuse": {
            "enabled": True,
            "public_key": "pk",
            "secret_key": "sk",
            "environment": "prod",
            "stages": {"etl": {"per_step_span": False}},
        },
    }), encoding="utf-8")
    cfg = load_langfuse_config(str(fp))
    assert cfg.enabled is True
    assert cfg.environment == "prod"
    assert cfg.per_step_span is False


def test_orchestration_load_langfuse_config_invalid_yaml_returns_defaults(
    tmp_path,
) -> None:
    """Loader never raises on malformed input — falls back to defaults."""
    from orchestration.observability.langfuse_config import load_langfuse_config
    fp = tmp_path / "root.yaml"
    fp.write_text("[unterminated", encoding="utf-8")
    cfg = load_langfuse_config(str(fp))
    assert cfg.enabled is False


# ---------------------------------------------------------------------------
# PR 5: start_as_current_observation / cm.__enter__ try/except
# ---------------------------------------------------------------------------


def test_stage_trace_start_observation_exception_does_not_propagate(
    monkeypatch,
) -> None:
    """``client.start_as_current_observation(...)`` 抛 RuntimeError → 业务不感知.

    PR 4 报告建议 #2:工厂 ``_open_observation`` 内 ``start_as_current_observation``
    未被 try/except 包裹,与 ``_resolve_payload`` / ``span.update`` /
    ``client.flush`` fail-safe 不对称。 PR 5 起 SDK init 异常 fail-safe.
    """
    from simulate_serve.observability import langfuse_client
    from simulate_serve.observability.langfuse_client import stage_trace

    fake = mock.MagicMock(name="LangfuseSDK")
    fake.start_as_current_observation.side_effect = RuntimeError("sdk init dead")
    pa = mock.MagicMock(name="PA")
    pa.__enter__.return_value = None
    pa.__exit__.return_value = False
    monkeypatch.setattr(langfuse_client, "Langfuse", fake)
    monkeypatch.setattr(langfuse_client, "propagate_attributes", pa)
    langfuse_client._client = fake  # get_client 短路

    with stage_trace(fake, session_id="s1", name="x") as span:
        # 业务路径:span 应为 None,yield 出来不影响业务
        assert span is None


def test_step_span_cm_enter_exception_does_not_propagate(
    monkeypatch,
) -> None:
    """``cm.__enter__()`` 抛 RuntimeError → 业务不感知,yield None."""
    from simulate_serve.observability import langfuse_client
    from simulate_serve.observability.langfuse_client import step_span

    fake = mock.MagicMock(name="LangfuseSDK")
    boom_cm = mock.MagicMock(name="BoomCM")
    boom_cm.__enter__.side_effect = RuntimeError("__enter__ dead")
    fake.start_as_current_observation.return_value = boom_cm
    pa = mock.MagicMock(name="PA")
    pa.__enter__.return_value = None
    pa.__exit__.return_value = False
    monkeypatch.setattr(langfuse_client, "Langfuse", fake)
    monkeypatch.setattr(langfuse_client, "propagate_attributes", pa)
    langfuse_client._client = fake

    with step_span(fake, name="x") as span:
        assert span is None


def test_stage_trace_cm_enter_exception_does_not_propagate(
    monkeypatch,
) -> None:
    """``stage_trace`` 也对称保护:cm.__enter__ 抛异常 → yield None."""
    from simulate_serve.observability import langfuse_client
    from simulate_serve.observability.langfuse_client import stage_trace

    fake = mock.MagicMock(name="LangfuseSDK")
    boom_cm = mock.MagicMock(name="BoomCM")
    boom_cm.__enter__.side_effect = RuntimeError("__enter__ dead")
    fake.start_as_current_observation.return_value = boom_cm
    pa = mock.MagicMock(name="PA")
    pa.__enter__.return_value = None
    pa.__exit__.return_value = False
    monkeypatch.setattr(langfuse_client, "Langfuse", fake)
    monkeypatch.setattr(langfuse_client, "propagate_attributes", pa)
    langfuse_client._client = fake

    with stage_trace(fake, session_id="s1", name="x") as span:
        assert span is None


def test_propagate_attributes_enter_exception_does_not_propagate(
    monkeypatch,
) -> None:
    """``propagate_attributes.__enter__()`` 抛异常 → 主路径不感知."""
    from simulate_serve.observability import langfuse_client
    from simulate_serve.observability.langfuse_client import stage_trace

    fake = mock.MagicMock(name="LangfuseSDK")
    span = mock.MagicMock(name="Span")
    cm = mock.MagicMock(name="CM")
    cm.__enter__.return_value = span
    cm.__exit__.return_value = False
    fake.start_as_current_observation.return_value = cm

    pa = mock.MagicMock(name="BoomPA")
    pa.__enter__.side_effect = RuntimeError("pa enter dead")
    pa.__exit__.return_value = False
    monkeypatch.setattr(langfuse_client, "Langfuse", fake)
    monkeypatch.setattr(langfuse_client, "propagate_attributes", pa)
    langfuse_client._client = fake

    with stage_trace(fake, session_id="s1", name="x") as s:
        # pa 失败 → 主路径仍可拿到 span
        assert s is not None


# ---------------------------------------------------------------------------
# Import smoke
# ---------------------------------------------------------------------------


def test_observability_public_imports_resolve() -> None:
    """``from simulate_serve.observability import ...`` must work end-to-end."""
    from simulate_serve.observability import (
        _extract_langfuse_fields,
        _maybe_truncate,
        _to_jsonable,
        get_client,
        shutdown,
        snapshot,
        stage_trace,
        step_span,
    )
    assert callable(get_client)
    assert callable(stage_trace)
    assert callable(step_span)
    assert callable(snapshot)
    assert callable(shutdown)
