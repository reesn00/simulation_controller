from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import simulate_serve.infrastructure.trajectory_archiver as trajectory_archiver_module
from simulate_serve.infrastructure.trajectory_archiver import (
    QwenPawTrajectoryArchiver,
    default_qwenpaw_console_dir,
    trajectory_filename,
)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr(trajectory_archiver_module, "_COPY_RETRY_DELAY_SECONDS", 0)


def _write_source(source_dir, user_id: str, session_id: str, payload: dict) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / f"{user_id}_{session_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_default_console_dir_uses_agent_workspace() -> None:
    assert default_qwenpaw_console_dir("agentX") == (
        Path.home() / ".qwenpaw" / "workspaces" / "agentX" / "sessions" / "console"
    )


def test_default_console_dir_falls_back_to_default_workspace() -> None:
    assert default_qwenpaw_console_dir("") == (
        Path.home() / ".qwenpaw" / "workspaces" / "default" / "sessions" / "console"
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
