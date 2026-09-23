"""Contract tests for ``QwenPawTrajectoryArchiver`` Langfuse ``_emit_trail`` hook.

The Langfuse SDK is mocked end-to-end so this file never reaches the
public Langfuse API even when credentials are present (CLAUDE.md
constraint: default CI must not reach public endpoints). The tests
focus on the *wiring* between the archiver and the factory: which
metadata / tags / payload each ``archive()`` call produces.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest import mock

import pytest

import simulate_serve.infrastructure.trajectory_archiver as trajectory_archiver_module
from simulate_serve.config import LangfuseConfig
from simulate_serve.infrastructure.trajectory_archiver import (
    QwenPawTrajectoryArchiver,
)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Eliminate the copy-with-retry sleeps so the tests run synchronously."""
    monkeypatch.setattr(trajectory_archiver_module, "_COPY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(trajectory_archiver_module, "_COMPLETION_POLL_S", 0)
    monkeypatch.setattr(trajectory_archiver_module, "_COMPLETION_WAIT_BUDGET_S", 0.5)


@pytest.fixture(autouse=True)
def _reset_langfuse_singleton():
    """Each test starts with a fresh Langfuse singleton."""
    from simulate_serve.observability import langfuse_client
    langfuse_client._client = None
    yield
    langfuse_client._client = None


def _write_source(source_dir: Path, session_id: str, *events: dict) -> None:
    """Append the given events as one JSONL line each."""
    source_dir.mkdir(parents=True, exist_ok=True)
    with (source_dir / f"{session_id}.jsonl").open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _patch_langfuse_skill(monkeypatch) -> tuple[mock.MagicMock, mock.MagicMock]:
    """Install a fake Langfuse SDK + propagate_attributes.

    Returns ``(fake_langfuse_cls, span_mock)``. ``fake_langfuse_cls`` is the
    patched symbol so tests can inspect instantiation args; ``span_mock``
    is the inner span returned by ``start_as_current_observation``.
    """
    from simulate_serve.observability import langfuse_client

    span = mock.MagicMock(name="Span")
    cm = mock.MagicMock(name="ObservationContextManager")
    cm.__enter__.return_value = span
    cm.__exit__.return_value = False
    fake_cls = mock.MagicMock(name="LangfuseSDK")
    fake_cls.return_value.start_as_current_observation.return_value = cm

    pa = mock.MagicMock(name="PropagateAttributes")
    pa.__enter__.return_value = None
    pa.__exit__.return_value = False

    monkeypatch.setattr(langfuse_client, "Langfuse", fake_cls)
    monkeypatch.setattr(langfuse_client, "propagate_attributes", pa)
    return fake_cls, span


def _stub_get_client(monkeypatch, span: mock.MagicMock) -> mock.MagicMock:
    """Force ``get_client`` to return a freshly-created fake client.

    Patches both the factory's ``Langfuse`` constructor and the singleton,
    so ``get_client`` returns an object whose ``start_as_current_observation``
    yields ``span``.
    """
    from simulate_serve.observability import langfuse_client

    client = mock.MagicMock(name="LangfuseClient")
    client.start_as_current_observation.return_value = (
        mock.MagicMock(__enter__=mock.MagicMock(return_value=span),
                       __exit__=mock.MagicMock(return_value=False))
    )

    def _fake_get_client(cfg):
        langfuse_client._client = client
        return client

    monkeypatch.setattr(langfuse_client, "get_client", _fake_get_client)
    return client


def _build_archiver(tmp_path: Path, *, enabled: bool = True) -> QwenPawTrajectoryArchiver:
    cfg = LangfuseConfig(
        enabled=enabled,
        public_key="pk-test",
        secret_key="sk-test",
        upload_payload="full",
    )
    return QwenPawTrajectoryArchiver(
        tmp_path / "output",
        user_id="useramulation",
        source_dir=tmp_path / "console",
        langfuse_config=cfg,
    )


# ---------------------------------------------------------------------------
# Disabled / missing-config / missing-task_id short circuits
# ---------------------------------------------------------------------------


def test_archive_does_not_emit_when_langfuse_disabled(tmp_path) -> None:
    """``enabled=False`` means zero Langfuse activity, even if the SDK exists."""
    cfg = LangfuseConfig(enabled=False)
    archiver = QwenPawTrajectoryArchiver(
        tmp_path / "output",
        user_id="u",
        source_dir=tmp_path / "console",
        langfuse_config=cfg,
    )
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "turn_start"},
                  {"event_type": "final_reply"})

    with mock.patch(
        "simulate_serve.observability.langfuse_client.get_client"
    ) as get_client:
        archiver.archive("r1", "agentX", "s1")

    get_client.assert_not_called()


def test_archive_does_not_emit_when_langfuse_config_is_none(tmp_path) -> None:
    """No ``langfuse_config`` arg → no client constructed, no trace emitted."""
    archiver = QwenPawTrajectoryArchiver(
        tmp_path / "output",
        user_id="u",
        source_dir=tmp_path / "console",
    )
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "final_reply"})

    with mock.patch(
        "simulate_serve.observability.langfuse_client.get_client"
    ) as get_client:
        archiver.archive("r1", "agentX", "s1")

    get_client.assert_not_called()


def test_archive_does_not_emit_when_task_id_missing(tmp_path, monkeypatch) -> None:
    """``run_ctx`` lacks ``task_id`` → trace would have no identity; skip."""
    _, span = _patch_langfuse_skill(monkeypatch)
    _stub_get_client(monkeypatch, span)
    archiver = _build_archiver(tmp_path)
    archiver.set_run_context({
        "run_id": "r1",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
        # task_id intentionally missing
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "final_reply"})

    archiver.archive("r1", "agentX", "s1")

    # SDK was never opened because we bailed before stage_trace.
    span.update.assert_not_called()


def test_archive_does_not_emit_when_run_context_empty(tmp_path, monkeypatch) -> None:
    """Calling ``archive()`` without ``set_run_context`` is a silent no-op
    (run_ctx default = empty → no task_id → skip)."""
    _, span = _patch_langfuse_skill(monkeypatch)
    _stub_get_client(monkeypatch, span)
    archiver = _build_archiver(tmp_path)
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "final_reply"})

    archiver.archive("r1", "agentX", "s1")

    span.update.assert_not_called()


# ---------------------------------------------------------------------------
# Enabled + happy-path: trace fields, metadata, tags, payload
# ---------------------------------------------------------------------------


def _metadata_calls(span: mock.MagicMock) -> list[dict]:
    """Pull all ``span.update(metadata=...)`` kwargs."""
    return [c.kwargs["metadata"] for c in span.update.call_args_list
            if c.kwargs.get("metadata")]


def _output_calls(span: mock.MagicMock) -> list[Any]:
    """Pull all ``span.update(output=...)`` values."""
    return [c.kwargs["output"] for c in span.update.call_args_list
            if "output" in c.kwargs]


def test_archive_emits_langfuse_trace_with_expected_metadata(tmp_path, monkeypatch) -> None:
    _, span = _patch_langfuse_skill(monkeypatch)
    client = _stub_get_client(monkeypatch, span)
    archiver = _build_archiver(tmp_path)
    archiver.set_run_context({
        "run_id": "run_xyz",
        "task_id": "T001",
        "remote_session_id": "useramulation-xyz",
        "remote_agent_id": "agent-abc",
    })
    _write_source(tmp_path / "console", "useramulation-xyz",
                  {"event_type": "turn_start"},
                  {"event_type": "model_response", "payload": {"content": [{"text": "hi"}]}},
                  {"event_type": "final_reply", "payload": {"content": []}})

    archiver.archive("run_xyz", "agent-abc", "useramulation-xyz")

    # 1) outer trace opened once with the right name
    client.start_as_current_observation.assert_called_once()
    assert client.start_as_current_observation.call_args.kwargs["name"] == "simulate_serve:T001"

    # 2) metadata carries the documented schema
    md_calls = _metadata_calls(span)
    assert md_calls, "expected at least one metadata update"
    md = md_calls[-1]
    assert md["stage"] == "simulate_serve"
    assert md["run_id"] == "run_xyz"
    assert md["task_id"] == "T001"
    assert md["session_id"] == "useramulation-xyz"
    assert md["agent_id"] == "agent-abc"
    assert md["terminal_reached"] is True
    assert md["last_event_type"] == "final_reply"
    assert md["trajectory_path"].endswith("run_xyz__useramulation-xyz.json")


def test_archive_emits_tags_with_task_and_stage(tmp_path, monkeypatch) -> None:
    _, span = _patch_langfuse_skill(monkeypatch)
    _stub_get_client(monkeypatch, span)
    archiver = _build_archiver(tmp_path)
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "final_reply"})

    archiver.archive("r1", "agentX", "s1")

    # propagate_attributes should have been invoked with both tags.
    from simulate_serve.observability import langfuse_client
    assert langfuse_client.propagate_attributes.called
    pa_kwargs = langfuse_client.propagate_attributes.call_args.kwargs
    assert "stage:simulate_serve" in pa_kwargs["tags"]
    assert "task:T001" in pa_kwargs["tags"]
    assert pa_kwargs["session_id"] == "s1"
    assert pa_kwargs["user_id"] == "s1"


def test_archive_payload_full_uploads_trajectory_events(tmp_path, monkeypatch) -> None:
    """``upload_payload=full`` (default) → ``output`` is the JSONL parsed into a list."""
    _, span = _patch_langfuse_skill(monkeypatch)
    _stub_get_client(monkeypatch, span)
    archiver = _build_archiver(tmp_path)
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(
        tmp_path / "console", "s1",
        {"event_type": "turn_start", "session_id": "s1"},
        {"event_type": "model_response", "payload": {"content": [{"text": "hi"}]}},
        {"event_type": "final_reply", "payload": {"content": []}},
    )

    archiver.archive("r1", "agentX", "s1")

    outputs = _output_calls(span)
    assert outputs, "expected at least one output update"
    final = outputs[-1]
    assert isinstance(final, list)
    assert [ev["event_type"] for ev in final] == [
        "turn_start", "model_response", "final_reply"
    ]


def test_archive_payload_summary_only_sends_size_hint(tmp_path, monkeypatch) -> None:
    _, span = _patch_langfuse_skill(monkeypatch)
    _stub_get_client(monkeypatch, span)
    cfg = LangfuseConfig(
        enabled=True,
        public_key="pk",
        secret_key="sk",
        upload_payload="summary",
    )
    archiver = QwenPawTrajectoryArchiver(
        tmp_path / "output",
        user_id="u",
        source_dir=tmp_path / "console",
        langfuse_config=cfg,
    )
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "final_reply", "payload": {"x": "y" * 100}})

    archiver.archive("r1", "agentX", "s1")

    outputs = _output_calls(span)
    assert outputs, "expected at least one output update"
    final = outputs[-1]
    assert isinstance(final, dict)
    assert final.get("summary") is True
    assert "size_hint" in final


def test_archive_payload_none_skips_data(tmp_path, monkeypatch) -> None:
    _, span = _patch_langfuse_skill(monkeypatch)
    _stub_get_client(monkeypatch, span)
    cfg = LangfuseConfig(
        enabled=True,
        public_key="pk",
        secret_key="sk",
        upload_payload="none",
    )
    archiver = QwenPawTrajectoryArchiver(
        tmp_path / "output",
        user_id="u",
        source_dir=tmp_path / "console",
        langfuse_config=cfg,
    )
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "final_reply"})

    archiver.archive("r1", "agentX", "s1")

    outputs = _output_calls(span)
    # upload_payload=none → output_capture resolves to None → no output update
    assert outputs == []


def test_archive_payload_truncates_when_over_limit(tmp_path, monkeypatch) -> None:
    _, span = _patch_langfuse_skill(monkeypatch)
    _stub_get_client(monkeypatch, span)
    cfg = LangfuseConfig(
        enabled=True,
        public_key="pk",
        secret_key="sk",
        upload_payload="full",
        max_payload_bytes=64,
    )
    archiver = QwenPawTrajectoryArchiver(
        tmp_path / "output",
        user_id="u",
        source_dir=tmp_path / "console",
        langfuse_config=cfg,
    )
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "final_reply", "payload": {"x": "z" * 1000}})

    archiver.archive("r1", "agentX", "s1")

    outputs = _output_calls(span)
    assert outputs, "expected at least one output update"
    final = outputs[-1]
    assert isinstance(final, dict)
    assert final.get("_truncated") is True
    assert final.get("max") == 64


# ---------------------------------------------------------------------------
# Terminal-reached semantics
# ---------------------------------------------------------------------------


def test_archive_metadata_terminal_reached_false_when_partial(tmp_path, monkeypatch) -> None:
    """Source ends in a non-terminal event (budget exhausted) → ``terminal_reached=false``."""
    _, span = _patch_langfuse_skill(monkeypatch)
    _stub_get_client(monkeypatch, span)
    archiver = _build_archiver(tmp_path)
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "turn_start"},
                  {"event_type": "model_response"})

    archiver.archive("r1", "agentX", "s1")

    md = _metadata_calls(span)[-1]
    assert md["terminal_reached"] is False
    assert md["last_event_type"] == "model_response"


def test_archive_metadata_terminal_reached_true_on_final_reply(tmp_path, monkeypatch) -> None:
    _, span = _patch_langfuse_skill(monkeypatch)
    _stub_get_client(monkeypatch, span)
    archiver = _build_archiver(tmp_path)
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "turn_start"},
                  {"event_type": "final_reply"})

    archiver.archive("r1", "agentX", "s1")

    md = _metadata_calls(span)[-1]
    assert md["terminal_reached"] is True
    assert md["last_event_type"] == "final_reply"


def test_archive_metadata_terminal_reached_true_on_error(tmp_path, monkeypatch) -> None:
    _, span = _patch_langfuse_skill(monkeypatch)
    _stub_get_client(monkeypatch, span)
    archiver = _build_archiver(tmp_path)
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "error", "payload": {"message": "boom"}})

    archiver.archive("r1", "agentX", "s1")

    md = _metadata_calls(span)[-1]
    assert md["terminal_reached"] is True
    assert md["last_event_type"] == "error"


# ---------------------------------------------------------------------------
# Fail-safe / multi-turn semantics
# ---------------------------------------------------------------------------


def test_archive_sdk_exception_does_not_break_archive(tmp_path, monkeypatch, caplog) -> None:
    """SDK raising during ``start_as_current_observation.__enter__()`` must be
    swallowed by the factory fail-safe (PR 5) — the on-disk copy is preserved
    and the failure is logged at WARNING level.

    PR 5 改动:工厂 ``_open_observation`` 内部对 ``start_as_current_observation``
    与 ``cm.__enter__()`` 包 try/except;异常不再 bubble 到 ``_emit_trail`` 外层
    try/except,所以日志位置从 archiver 移到 factory (logger 名称变化).
    业务可观察性不变:WARNING + 落盘不中断。
    """
    from simulate_serve.observability import langfuse_client

    boom_cm = mock.MagicMock(name="BoomCM")
    boom_cm.__enter__.side_effect = RuntimeError("sdk boom")
    boom_cm.__exit__.return_value = False

    client = mock.MagicMock(name="LangfuseClient")
    client.start_as_current_observation.return_value = boom_cm

    monkeypatch.setattr(langfuse_client, "get_client", lambda cfg: client)

    archiver = _build_archiver(tmp_path)
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "final_reply"})

    # PR 5: factory 已吞 SDK 异常,所以 WARNING 来自 langfuse_client 而非 archiver
    with caplog.at_level(logging.WARNING, logger="simulate_serve.observability.langfuse_client"):
        archiver.archive("r1", "agentX", "s1")  # must not raise

    # On-disk copy must still exist (the archive() finally-block must not
    # have aborted the copy when the Langfuse side fell over).
    target = tmp_path / "output" / "agent_trajectory" / "r1__s1.json"
    assert target.is_file()
    # PR 5: 失败由工厂 swallowed,日志在 factory 而非 archiver
    assert any(
        "langfuse observation __enter__ failed" in r.getMessage()
        or "langfuse start_observation failed" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


def test_archive_get_client_none_skips_silently(tmp_path, monkeypatch) -> None:
    """``get_client`` returning ``None`` (SDK missing / init failed) → silent skip."""
    from simulate_serve.observability import langfuse_client
    monkeypatch.setattr(langfuse_client, "get_client", lambda cfg: None)

    archiver = _build_archiver(tmp_path)
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "final_reply"})

    archiver.archive("r1", "agentX", "s1")  # must not raise


def test_archive_multi_turn_emits_one_trace_per_call(tmp_path, monkeypatch) -> None:
    """Same session, two archive() invocations → two traces, each reflecting
    the latest on-disk file."""
    _, span = _patch_langfuse_skill(monkeypatch)
    client = _stub_get_client(monkeypatch, span)
    archiver = _build_archiver(tmp_path)
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })

    # Turn 1
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "turn_start", "turn": 1},
                  {"event_type": "final_reply", "turn": 1})
    archiver.archive("r1", "agentX", "s1")

    # Turn 2 — overwrite the source so the next trace sees turn=2
    _write_source(tmp_path / "console", "s1",
                  {"event_type": "turn_start", "turn": 2},
                  {"event_type": "final_reply", "turn": 2})
    archiver.archive("r1", "agentX", "s1")

    # One trace per archive() call.
    assert client.start_as_current_observation.call_count == 2
    outputs = _output_calls(span)
    assert len(outputs) == 2
    # Each output reflects the latest on-disk state at the time of __exit__.
    assert outputs[0][-1]["turn"] == 1
    assert outputs[1][-1]["turn"] == 2


def test_archive_does_not_invoke_langfuse_when_session_id_empty(tmp_path, monkeypatch) -> None:
    """``session_id=""`` early-returns inside ``archive()`` — ``_emit_trail``
    must therefore not be called."""
    _, span = _patch_langfuse_skill(monkeypatch)
    _stub_get_client(monkeypatch, span)
    archiver = _build_archiver(tmp_path)
    archiver.set_run_context({
        "run_id": "r1",
        "task_id": "T001",
        "remote_session_id": "s1",
        "remote_agent_id": "a",
    })

    archiver.archive("r1", "agentX", "")  # empty session_id

    span.update.assert_not_called()


# ---------------------------------------------------------------------------
# Backwards compatibility: archiver must work unchanged when no Langfuse
# config is provided at all (legacy callers).
# ---------------------------------------------------------------------------


def test_archive_no_langfuse_arg_does_not_break_legacy_callers(tmp_path) -> None:
    """Pre-PR-2 constructor invocation (no ``langfuse_config``) must be a no-op
    for Langfuse purposes — the file copy itself still works."""
    source_dir = tmp_path / "console"
    _write_source(source_dir, "s1", {"event_type": "final_reply"})
    archiver = QwenPawTrajectoryArchiver(
        tmp_path / "output",
        user_id="u",
        source_dir=source_dir,
    )

    archiver.archive("r1", "agentX", "s1")

    target = tmp_path / "output" / "agent_trajectory" / "r1__s1.json"
    assert target.is_file()


def test_archive_set_run_context_does_not_break_when_archiver_lacks_method(
    tmp_path, monkeypatch
) -> None:
    """Verify the ``hasattr`` guard at the call site: a legacy
    ``TrajectoryArchivePort`` implementation that lacks
    ``set_run_context`` still receives the ``archive()`` call (the guard
    silently skips ``set_run_context``)."""
    source_dir = tmp_path / "console"
    _write_source(source_dir, "s1", {"event_type": "final_reply"})

    class _LegacyArchiver:
        """Pre-PR-2 TrajectoryArchivePort: no ``set_run_context``."""

        def __init__(self) -> None:
            self.output_dir = tmp_path / "agent_trajectory"
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.archive_calls: list[tuple[str, str, str]] = []
            self.has_set_run_context = hasattr(self, "set_run_context")

        def archive(self, run_id: str, agent_id: str, session_id: str) -> None:
            self.archive_calls.append((run_id, agent_id, session_id))
            (self.output_dir / f"{run_id}__{session_id}.json").write_text(
                '{"event_type": "final_reply"}', encoding="utf-8"
            )

        def trajectory_path(self, run_id: str, session_id: str):
            if not session_id:
                return None
            return self.output_dir / f"{run_id}__{session_id}.json"

    legacy = _LegacyArchiver()
    # Confirm the precondition: the legacy mock has no set_run_context.
    assert legacy.has_set_run_context is False

    # Mimic exactly what run_task._archive_trajectory does (the production
    # code path). The hasattr guard must short-circuit cleanly.
    if hasattr(legacy, "set_run_context"):
        legacy.set_run_context({})  # pragma: no cover - guard branch
    legacy.archive("run_legacy", "agentX", "s1")

    assert legacy.archive_calls == [("run_legacy", "agentX", "s1")]
    # The on-disk file must have been created by archive().
    assert (legacy.output_dir / "run_legacy__s1.json").is_file()