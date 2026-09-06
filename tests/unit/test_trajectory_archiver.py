from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import simulate_serve.infrastructure.trajectory_archiver as trajectory_archiver_module
from simulate_serve.infrastructure.trajectory_archiver import (
    QwenPawTrajectoryArchiver,
    _trajectory_last_event_type,
    default_qwenpaw_trajectory_dir,
    trajectory_filename,
)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(trajectory_archiver_module, "_COPY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(trajectory_archiver_module, "_COMPLETION_POLL_S", 0)
    monkeypatch.setattr(trajectory_archiver_module, "_COMPLETION_WAIT_BUDGET_S", 0.5)


def _write_source(source_dir, user_id: str, session_id: str, payload: dict) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / f"{session_id}.jsonl").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_default_trajectory_dir_uses_agent_workspace() -> None:
    assert default_qwenpaw_trajectory_dir("agentX") == (
        Path.home() / ".qwenpaw" / "workspaces" / "agentX" / "trajectory"
    )


def test_default_trajectory_dir_falls_back_to_default_workspace() -> None:
    assert default_qwenpaw_trajectory_dir("") == (
        Path.home() / ".qwenpaw" / "workspaces" / "default" / "trajectory"
    )


def test_trajectory_filename_embeds_run_and_session() -> None:
    assert trajectory_filename("run_abc", "useramulation-123") == "run_abc__useramulation-123.json"


def test_trajectory_filename_sanitizes_unsafe_characters() -> None:
    name = trajectory_filename("run/with:bad*chars", "session id")
    assert "/" not in name
    assert ":" not in name
    assert "*" not in name
    assert name.endswith(".json")


def test_archive_copies_and_renames(tmp_path) -> None:
    source_dir = tmp_path / "console"
    _write_source(source_dir, "useramulation", "useramulation-abc", {"agent": {"state": {}}})
    archiver = QwenPawTrajectoryArchiver(tmp_path / "output", user_id="useramulation", source_dir=source_dir)

    archiver.archive("run_1", "agentX", "useramulation-abc")

    target = tmp_path / "output" / "agent_trajectory" / "run_1__useramulation-abc.json"
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == {"agent": {"state": {}}}


def test_archive_overwrites_with_latest_state_on_multi_turn_run(tmp_path) -> None:
    source_dir = tmp_path / "console"
    archiver = QwenPawTrajectoryArchiver(tmp_path / "output", user_id="u", source_dir=source_dir)

    _write_source(source_dir, "u", "s1", {"turn": 1})
    archiver.archive("run_1", "agentX", "s1")
    _write_source(source_dir, "u", "s1", {"turn": 2})
    archiver.archive("run_1", "agentX", "s1")

    target = tmp_path / "output" / "agent_trajectory" / "run_1__s1.json"
    assert json.loads(target.read_text(encoding="utf-8")) == {"turn": 2}


def test_archive_missing_file_warns_once_then_debug(tmp_path, caplog) -> None:
    archiver = QwenPawTrajectoryArchiver(tmp_path / "output", user_id="u", source_dir=tmp_path / "console")

    with caplog.at_level(logging.DEBUG, logger="simulate_serve.infrastructure.trajectory_archiver"):
        archiver.archive("run_1", "agentX", "missing")
        archiver.archive("run_1", "agentX", "missing")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "not found" in warnings[0].getMessage()


def test_archive_copy_failure_never_raises(tmp_path, monkeypatch, caplog) -> None:
    _write_source(tmp_path / "console", "u", "s1", {"turn": 1})

    def boom(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(trajectory_archiver_module.shutil, "copy2", boom)
    archiver = QwenPawTrajectoryArchiver(tmp_path / "output", user_id="u", source_dir=tmp_path / "console")

    archiver.archive("run_1", "agentX", "s1")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("copy failed" in r.getMessage() for r in warnings)


def test_archive_empty_session_is_noop(tmp_path) -> None:
    archiver = QwenPawTrajectoryArchiver(tmp_path / "output", user_id="u", source_dir=tmp_path / "console")

    archiver.archive("run_1", "agentX", "")

    assert not (tmp_path / "output" / "agent_trajectory").exists()


# ---------------------------------------------------------------------------
# 终态等待：QwenPaw 在 HTTP 'finished' 与落盘 final_reply 之间存在 race,
# archiver 必须等到终态事件出现才确认拷贝完成, 否则下游 qf/gdr 会看到
# "无 assistant 消息" 而硬丢弃。
# ---------------------------------------------------------------------------


def _append_event(source_dir, user_id: str, session_id: str, event_type: str, **extra) -> None:
    """Append a single trajectory event JSON line to ``<session_id>.jsonl``."""
    payload = {"event_type": event_type, "session_id": session_id, "x": extra}
    with (source_dir / f"{session_id}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def test_archive_waits_for_final_reply_before_finalizing(tmp_path, monkeypatch) -> None:
    """Source has all events on disk; we mock ``_trajectory_last_event_type``
    to simulate the race where QwenPaw has not yet flushed ``final_reply``
    when the archiver first inspects the file. The archiver must keep
    polling (re-copying) until the source's last event is terminal.

    Using a mock avoids the cross-platform file-locking pitfalls of having
    a writer thread append to the same file the archiver is reading on
    Windows; the production race is across processes anyway.
    """
    source_dir = tmp_path / "console"
    session_id = "useramulation-race-1"
    source_dir.mkdir(parents=True, exist_ok=True)
    _append_event(source_dir, "u", session_id, "turn_start")
    _append_event(source_dir, "u", session_id, "model_request")
    _append_event(source_dir, "u", session_id, "model_response")
    _append_event(source_dir, "u", session_id, "final_reply")

    real_last_event = trajectory_archiver_module._trajectory_last_event_type
    calls: list[Path] = []

    def fake_last_event(path: Path) -> str | None:
        calls.append(path)
        # First two polls: simulate still-growing file (no terminal event yet)
        if len(calls) < 3:
            return "model_response"
        return real_last_event(path)

    monkeypatch.setattr(trajectory_archiver_module, "_trajectory_last_event_type", fake_last_event)

    archiver = QwenPawTrajectoryArchiver(tmp_path / "output", user_id="u", source_dir=source_dir)
    target = tmp_path / "output" / "agent_trajectory" / f"run_1__{session_id}.json"

    archiver.archive("run_1", "agentX", session_id)

    content = target.read_text(encoding="utf-8")
    # final_reply must end up in the copied file
    assert '"event_type": "final_reply"' in content
    # and the polling loop must have actually run multiple times
    assert len(calls) >= 3


def test_archive_accepts_error_and_cancel_as_terminal(tmp_path) -> None:
    """``error`` and ``cancel`` are also terminal — no need to wait forever."""
    source_dir = tmp_path / "console"
    session_id = "useramulation-err"
    source_dir.mkdir(parents=True, exist_ok=True)
    _append_event(source_dir, "u", session_id, "turn_start")
    _append_event(source_dir, "u", session_id, "error")

    archiver = QwenPawTrajectoryArchiver(tmp_path / "output", user_id="u", source_dir=source_dir)
    target = tmp_path / "output" / "agent_trajectory" / f"run_1__{session_id}.json"

    archiver.archive("run_1", "agentX", session_id)

    assert '"event_type": "error"' in target.read_text(encoding="utf-8")


def test_archive_warns_when_trajectory_stays_partial(tmp_path, caplog) -> None:
    """If the source never grows a terminal event, the partial copy is kept
    and a warning is logged once — capture must still be best-effort."""
    source_dir = tmp_path / "console"
    session_id = "useramulation-stuck"
    source_dir.mkdir(parents=True, exist_ok=True)
    _append_event(source_dir, "u", session_id, "turn_start")
    _append_event(source_dir, "u", session_id, "model_request")
    # no final_reply / error / cancel — simulate QwenPaw stuck mid-run

    archiver = QwenPawTrajectoryArchiver(tmp_path / "output", user_id="u", source_dir=source_dir)
    target = tmp_path / "output" / "agent_trajectory" / f"run_1__{session_id}.json"

    with caplog.at_level(logging.WARNING, logger="simulate_serve.infrastructure.trajectory_archiver"):
        archiver.archive("run_1", "agentX", session_id)

    assert target.is_file()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("did not reach a terminal event" in r.getMessage() for r in warnings)


def test_trajectory_last_event_type_handles_embedded_newlines(tmp_path) -> None:
    """``tool_execution`` payloads contain raw newlines; the helper must
    locate the *last* JSON object regardless of where line splits land."""
    path = tmp_path / "traj.jsonl"
    path.write_text(
        json.dumps({"event_type": "turn_start", "session_id": "s1"}) + "\n"
        + json.dumps({
            "event_type": "tool_execution",
            "session_id": "s1",
            # embedded newline + quote inside the output string
            "payload": {"output": "line1\nline2 with \"quote\"\nline3"},
        }) + "\n"
        + json.dumps({"event_type": "final_reply", "session_id": "s1"}) + "\n",
        encoding="utf-8",
    )
    assert _trajectory_last_event_type(path) == "final_reply"


def test_trajectory_last_event_type_returns_none_for_partial_write(tmp_path) -> None:
    """A truncated file (last object not closed) must not raise."""
    path = tmp_path / "partial.jsonl"
    path.write_text(
        json.dumps({"event_type": "turn_start", "session_id": "s1"}) + "\n"
        + '{"event_type": "model_response", "x": ',  # truncated, missing closing braces
        encoding="utf-8",
    )
    # Either we find the parseable object (turn_start) or None — but never raise.
    result = _trajectory_last_event_type(path)
    assert result in (None, "turn_start")


def test_trajectory_last_event_type_handles_missing_and_empty(tmp_path) -> None:
    missing = tmp_path / "nope.jsonl"
    assert _trajectory_last_event_type(missing) is None

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert _trajectory_last_event_type(empty) is None
