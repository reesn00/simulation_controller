from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Poll "finished" only after the remote side persisted its result, but the
# session file flush can still lag the HTTP response by a moment; retry a few
# times before giving up (capture failures are logged, never raised).
_COPY_ATTEMPTS = 3
_COPY_RETRY_DELAY_SECONDS = 0.5

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def default_qwenpaw_trajectory_dir(agent_id: str) -> Path:
    """Per-agent trajectory directory inside the local QwenPaw home.

    QwenPaw falls back to the ``default`` workspace when no agent id is sent,
    so an unset ``execution_agent_id`` maps to ``workspaces/default``.
    """
    workspace = agent_id.strip() or "default"
    return Path.home() / ".qwenpaw" / "workspaces" / workspace / "trajectory"


def sanitize_filename_part(value: str) -> str:
    """Make an arbitrary id safe for embedding in a file name.

    Public single source of truth: orchestration writes run_id -> task_id
    mappings keyed by the *sanitized* run id (what the trajectory watcher
    reads back from file names), so both sides must use this exact function.
    """
    return _UNSAFE_FILENAME_CHARS.sub("_", value)


def trajectory_filename(run_id: str, session_id: str) -> str:
    """Target file name embedding both the run and the remote session id."""
    safe_run = sanitize_filename_part(run_id) or "run"
    safe_session = sanitize_filename_part(session_id) or "session"
    return f"{safe_run}__{safe_session}.json"


class QwenPawTrajectoryArchiver:
    """Copy QwenPaw's per-session trajectory JSONL into output/agent_trajectory.

    Source file convention: ``{session_id}.jsonl`` inside the agent's
    trajectory directory. The copy is overwritten on every archive call
    so multi-turn runs keep the latest state under one stable, self-describing
    name (``{run_id}__{session_id}.json``).
    """

    def __init__(
        self,
        output_dir: str | Path,
        *,
        user_id: str,
        source_dir: str | Path | None = None,
    ):
        self._user_id = user_id
        self._source_override = Path(source_dir) if source_dir else None
        self.output_dir = Path(output_dir) / "agent_trajectory"
        self._warned_missing: set[str] = set()

    def archive(self, run_id: str, agent_id: str, session_id: str) -> None:
        if not session_id:
            return
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            source = self._source_path(agent_id, session_id)
            target = self.output_dir / trajectory_filename(run_id, session_id)
            self._copy_with_retry(source, target, session_id)
        except OSError as exc:
            self._warn(session_id, "trajectory copy failed for session %s: %s", session_id, exc)
        except Exception as exc:  # auxiliary capture must never fail a run
            self._warn(session_id, "unexpected trajectory capture error for session %s: %s", session_id, exc)

    def _source_path(self, agent_id: str, session_id: str) -> Path:
        base = self._source_override or default_qwenpaw_trajectory_dir(agent_id)
        return base / f"{session_id}.jsonl"

    def _copy_with_retry(self, source: Path, target: Path, session_id: str) -> None:
        for attempt in range(_COPY_ATTEMPTS):
            if source.is_file():
                shutil.copy2(source, target)
                return
            if attempt < _COPY_ATTEMPTS - 1:
                time.sleep(_COPY_RETRY_DELAY_SECONDS)
        self._warn(
            session_id,
            "remote trajectory file not found for session %s: %s",
            session_id,
            source,
        )

    def _warn(self, session_id: str, message: str, *args: object) -> None:
        # Warn once per session; repeated attempts over the same multi-turn
        # session would otherwise spam the log every round.
        if session_id in self._warned_missing:
            logger.debug(message, *args)
            return
        self._warned_missing.add(session_id)
        logger.warning(message, *args)
