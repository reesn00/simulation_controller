from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

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

    When ``langfuse_config`` is provided and enabled, each archive call also
    emits one Langfuse ``stage_trace`` per turn (overwriting semantics carry
    over: the trace's ``output`` reflects the latest on-disk copy at the
    moment of emission).
    """

    def __init__(
        self,
        output_dir: str | Path,
        *,
        user_id: str,
        source_dir: str | Path | None = None,
        langfuse_config: Any | None = None,
    ):
        self._user_id = user_id
        self._source_override = Path(source_dir) if source_dir else None
        self.output_dir = Path(output_dir) / "agent_trajectory"
        self._warned_missing: set[str] = set()
        # Langfuse observability hook (PR 2). None ⇒ no-op; the get_client
        # call inside ``_emit_trail`` returns None and short-circuits.
        self._langfuse_config = langfuse_config
        # Per-turn run context set by ``set_run_context`` before ``archive()``.
        # Read by ``_emit_trail`` to build trace metadata / tags.
        self._run_ctx: dict[str, Any] = {}

    def set_run_context(self, run_ctx: dict[str, Any]) -> None:
        """Set per-turn run context used by ``_emit_trail`` for Langfuse metadata.

        Called by ``TaskRuntime._archive_trajectory`` before each ``archive()``
        so the emitted trace carries the right ``run_id / task_id /
        remote_session_id / remote_agent_id``. The ``TrajectoryArchivePort``
        protocol does not require this method (old mocks may lack it) — the
        caller guards with ``hasattr`` before invoking.
        """
        self._run_ctx = dict(run_ctx)

    def archive(self, run_id: str, agent_id: str, session_id: str) -> None:
        if not session_id:
            return
        target: Path | None = None
        last_event_type: str | None = None
        terminal_reached = False
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            source = self._source_path(agent_id, session_id)
            target = self.output_dir / trajectory_filename(run_id, session_id)
            self._copy_with_retry(source, target, session_id)
            last_event_type = _trajectory_last_event_type(source)
            terminal_reached = last_event_type in _TERMINAL_EVENT_TYPES
        except OSError as exc:
            self._warn(session_id, "trajectory copy failed for session %s: %s", session_id, exc)
        except Exception as exc:  # auxiliary capture must never fail a run
            self._warn(session_id, "unexpected trajectory capture error for session %s: %s", session_id, exc)
        finally:
            # Langfuse trail emission (PR 2). Fail-safe: a Langfuse error
            # never propagates and never fails a run. ``target`` is None
            # only when ``session_id`` was empty (early return above), so
            # this branch only fires when we actually attempted a copy.
            if target is not None:
                self._emit_trail(target, last_event_type, terminal_reached)

    def _emit_trail(
        self,
        source: Path,
        last_event_type: str | None,
        terminal_reached: bool,
    ) -> None:
        """Emit one Langfuse ``stage_trace`` per archive call.

        No-op when:
          * ``langfuse_config`` is None or ``enabled=False``;
          * SDK is missing (``get_client`` returns None);
          * ``run_ctx`` lacks ``task_id`` (no trace identity → skip silently).

        ``stage_trace`` exceptions are swallowed here as a network-failure
        fail-safe; business exceptions are caught by ``archive()``'s outer
        ``except`` branches before reaching this method, so any exception
        observed here is purely an SDK / upload error.
        """
        cfg = self._langfuse_config
        if cfg is None or not getattr(cfg, "enabled", False):
            return
        # Local import keeps the door sealed for callers who never enable
        # Langfuse (the SDK import itself is the slow path).
        from simulate_serve.observability.langfuse_client import (
            get_client,
            stage_trace,
        )

        client = get_client(cfg)
        if client is None:
            return
        run = self._run_ctx
        session_id = run.get("remote_session_id") or run.get("run_id") or ""
        task_id = run.get("task_id") or ""
        if not task_id:
            return
        payload_mode = getattr(cfg, "upload_payload", "full")

        def _output_payload() -> Any:
            # Read the on-disk copy fresh inside the closure so the span
            # captures the file as of ``__exit__`` (the JSONL may have been
            # overwritten by a later ``archive()`` call before flush).
            if not source.exists():
                return {"_missing": True}
            try:
                return [
                    json.loads(line)
                    for line in source.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except Exception as exc:
                return {"_read_error": f"{type(exc).__name__}: {exc}"}

        try:
            with stage_trace(
                client,
                session_id=session_id,
                name=f"simulate_serve:{task_id}",
                user_id=session_id,
                task_id=task_id,
                tags=["stage:simulate_serve", f"task:{task_id}"],
                metadata={
                    "stage": "simulate_serve",
                    "run_id": run.get("run_id"),
                    "task_id": task_id,
                    "session_id": session_id,
                    "agent_id": run.get("remote_agent_id"),
                    "terminal_reached": terminal_reached,
                    "last_event_type": last_event_type,
                    "trajectory_path": str(source),
                },
                input_data=None,
                output_capture=_output_payload,
                payload_mode=payload_mode,
                max_payload_bytes=getattr(cfg, "max_payload_bytes", 0),
            ):
                pass
        except Exception as exc:  # network / SDK fail-safe; never break a run
            logger.warning(
                "Langfuse _emit_trail failed for session %s: %s",
                session_id,
                exc,
            )

    def trajectory_path(self, run_id: str, session_id: str) -> Path | None:
        """Return the on-disk trajectory target path; None when session_id is empty.

        Implements ``simulate_serve.application.ports.TrajectoryArchivePort``;
        callers (BatchRunner) use this to locate the file for
        ``simulate_serve.checker.check_completion``.
        """
        if not session_id:
            return None
        return self.output_dir / trajectory_filename(run_id, session_id)

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
