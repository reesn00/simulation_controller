from __future__ import annotations

import json
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

# QwenPaw may report HTTP `finished` *before* it has flushed the last few
# events (model_response / tool_execution / final_reply) to disk. Copying at
# that instant yields a partial trajectory whose `final_reply` is missing —
# downstream QF + GDR stages then see "no assistant message at all" and the
# session is hard-filtered out. To avoid losing the assistant turn we wait
# for the file's last event to be a terminal type before finalizing.
#
# Budget rationale: an LLM-driven agent with multi-turn tool use routinely
# takes 30+ seconds to flush the final ``final_reply`` after the HTTP response
# returns. A 2-second budget was empirically too short (the copy landed
# mid-conversation and downstream qf_out had no assistant text). 60 seconds
# is generous enough for any realistic run while keeping the master from
# stalling indefinitely on a truly stuck QwenPaw process.
_COMPLETION_POLL_S = 0.1
_COMPLETION_WAIT_BUDGET_S = 60.0
_TERMINAL_EVENT_TYPES = frozenset({"final_reply", "error", "cancel"})

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
        """Copy ``source`` → ``target``, waiting for the trajectory to finish.

        Two race windows must be covered:

          1. Source file does not exist yet (``_COPY_ATTEMPTS`` retries).
          2. Source file exists but is still being appended — QwenPaw can
             report HTTP ``finished`` before the trailing
             ``tool_execution`` / ``model_response`` / ``final_reply`` events
             are flushed. We re-copy until the source's last event is a
             terminal type (``final_reply`` / ``error`` / ``cancel``) or
             until ``_COMPLETION_WAIT_BUDGET_S`` elapses, whichever comes
             first. A trailing partial copy is kept and logged as a warning
             so downstream stages can still audit the data we have.
        """
        for attempt in range(_COPY_ATTEMPTS):
            if source.is_file():
                shutil.copy2(source, target)
                break
            if attempt < _COPY_ATTEMPTS - 1:
                time.sleep(_COPY_RETRY_DELAY_SECONDS)
        else:  # for-else: source never appeared during the existence retries
            self._warn(
                session_id,
                "remote trajectory file not found for session %s: %s",
                session_id,
                source,
            )
            return

        # Source appeared and an initial copy landed. Now make sure that copy
        # captured all events — wait briefly for the trajectory to reach a
        # terminal event before finalizing.
        deadline = time.monotonic() + _COMPLETION_WAIT_BUDGET_S
        last_et = _trajectory_last_event_type(source)
        while last_et not in _TERMINAL_EVENT_TYPES and time.monotonic() < deadline:
            time.sleep(_COMPLETION_POLL_S)
            try:
                if source.is_file():
                    shutil.copy2(source, target)
            except OSError as exc:
                # Capture must never raise (port contract); log and keep what we have.
                self._warn(session_id, "trajectory re-copy failed for session %s: %s", session_id, exc)
                return
            last_et = _trajectory_last_event_type(source)

        if last_et not in _TERMINAL_EVENT_TYPES:
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            self._warn(
                session_id,
                "trajectory for session %s did not reach a terminal event "
                "(final_reply/error/cancel) within %.1fs; keeping partial copy "
                "(%d bytes, last_event=%s)",
                session_id, _COMPLETION_WAIT_BUDGET_S, size, last_et,
            )

    def _warn(self, session_id: str, message: str, *args: object) -> None:
        # Warn once per session; repeated attempts over the same multi-turn
        # session would otherwise spam the log every round.
        if session_id in self._warned_missing:
            logger.debug(message, *args)
            return
        self._warned_missing.add(session_id)
        logger.warning(message, *args)


def _trajectory_last_event_type(path: Path) -> str | None:
    """Return the ``event_type`` of the last JSON object in a trajectory file.

    The trajectory is JSONL with one event per line, but a single event may
    itself contain embedded newlines (e.g. ``tool_execution`` payload output),
    so naive line-splitting can land mid-object. We scan with a brace-depth
    counter that respects JSON string boundaries and parse the last
    fully-balanced object we find.

    Implementation notes:

    * We scan the **whole file** rather than a fixed-size tail window.
      Trajectory files cap out at a few hundred KB in practice (well under
      1 MB even for long sessions); full scans run in sub-millisecond time
      and — critically — the scan **must** start at a byte that is at an
      object boundary, otherwise the brace counter ends up unbalanced at
      EOF and reports a phantom ``None``. A 64 KB tail window cannot
      guarantee event-boundary alignment and was empirically observed to
      misclassify a 173 KB trajectory (final_reply present, helper returns
      None).
    * We use ``decode("utf-8")`` strict mode so we never silently drop
      bytes (which would also desynchronize the counter). Multi-byte UTF-8
      splits are not expected inside a single trajectory event, so a decode
      failure indicates a truncated write and we return ``None``.

    Returns ``None`` when:
      * the file is missing, empty, or unreadable;
      * the file contains no parseable JSON object (e.g. partial write).
    """
    try:
        with path.open("rb") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None

    depth = 0
    in_string = False
    escape = False
    obj_start = -1
    last_obj_start = -1
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start >= 0:
                last_obj_start = obj_start
                obj_start = -1

    if last_obj_start < 0:
        return None
    try:
        ev = json.loads(text[last_obj_start:])
    except (json.JSONDecodeError, ValueError):
        return None
    return ev.get("event_type") if isinstance(ev, dict) else None
