"""Langfuse observability client factory.

Goal: zero-impact default (enabled=False), three-stage pipeline
(simulate_serve → gdr → etl) all upload to the same Langfuse project, and
all helpers fail-safe so any Langfuse exception never breaks business flow.

Design contract (frozen by PR 1, see docs/observability-langfuse-plan.md §4):

* :func:`get_client` — duck-typed config in (nested pydantic / flat pydantic /
  frozen dataclass). Returns a process-local ``Langfuse`` singleton, or
  ``None`` when disabled / misconfigured / SDK missing.
* :func:`stage_trace` — outer trace span (no-op if client is None).
* :func:`step_span` — child span under the current observation context.
* :func:`snapshot` — deep-copy helper for input/output isolation.
* :func:`shutdown` — flush + reset the process-local singleton.

Per-span exception handling marks ``level="ERROR"`` + ``status_message`` on
the span and re-raises for the caller; business exceptions flow through
without breaking Langfuse upload of the parent trace.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

try:
    from langfuse import Langfuse
    from langfuse import propagate_attributes
except Exception:  # pragma: no cover - SDK not installed
    Langfuse = None  # type: ignore[assignment]
    propagate_attributes = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Process-local singleton + fork-safe reset
# ---------------------------------------------------------------------------

_client: Any | None = None
_lock = threading.Lock()


def _reset_for_fork() -> None:
    """Reset the singleton so the next ``get_client`` rebuilds after fork.

    Multiprocessing.Pool workers fork (or spawn) and must not reuse the
    parent process's Langfuse socket / background threads.
    """
    global _client
    with _lock:
        _client = None


def _extract_langfuse_fields(cfg: Any) -> dict[str, Any]:
    """鸭子类型:支持嵌套 pydantic / 扁平 pydantic / dataclass 三种 LangfuseConfig 形态.

    Resolution order per field:

    1. Nested attribute ``cfg.<key>`` (pydantic nested / dataclass).
    2. Flat attribute ``cfg.langfuse_<key>`` (gdr Settings style).
    3. Built-in default.
    """
    keys = [
        "enabled", "public_key", "secret_key", "base_url",
        "environment", "release", "sample_rate", "flush_at",
        "flush_interval", "timeout", "upload_payload", "max_payload_bytes",
        "max_block_payload_bytes",
    ]
    defaults: dict[str, Any] = {
        "enabled": False,
        "public_key": "",
        "secret_key": "",
        "base_url": "https://cloud.langfuse.com",
        "environment": "dev",
        "release": "local",
        "sample_rate": 1.0,
        "flush_at": 512,
        "flush_interval": 5.0,
        "timeout": 10,
        "upload_payload": "full",
        "max_payload_bytes": 0,
        "max_block_payload_bytes": 0,
    }
    out: dict[str, Any] = {}
    for k in keys:
        v = getattr(cfg, k, None)
        if v is None or v == "":
            v = getattr(cfg, f"langfuse_{k}", None)
        if v is None or v == "" or v is False:
            v = defaults[k]
        # Coerce numeric fields defensively (pydantic may hand back None for
        # optional ints; we still want the default).
        if k in ("sample_rate", "flush_interval", "timeout") and not isinstance(v, (int, float)):
            v = defaults[k]
        if k in ("flush_at", "max_payload_bytes", "max_block_payload_bytes") and not isinstance(v, int):
            v = defaults[k]
        out[k] = v
    return out


def get_client(cfg: Any) -> Any | None:
    """Return a process-local Langfuse client or ``None`` (disabled / invalid).

    Safe to call repeatedly; only the first successful call instantiates the
    SDK. All exceptions are swallowed and logged at WARNING level.
    """
    global _client
    if cfg is None:
        return None
    fields = _extract_langfuse_fields(cfg)
    if not fields["enabled"]:
        return None
    if not fields["public_key"] or not fields["secret_key"]:
        return None
    if Langfuse is None:
        return None
    with _lock:
        if _client is not None:
            return _client
        try:
            _client = Langfuse(
                public_key=fields["public_key"],
                secret_key=fields["secret_key"],
                host=fields["base_url"],
                environment=fields["environment"] or None,
                release=fields["release"] or None,
                sample_rate=fields["sample_rate"],
                flush_at=fields["flush_at"],
                flush_interval=fields["flush_interval"],
                timeout=fields["timeout"],
            )
        except Exception as exc:
            logger.warning("Langfuse init failed; observability disabled: %s", exc)
            _client = None
            return None
        return _client


def shutdown() -> None:
    """Flush + reset the process-local Langfuse client.

    Called at application close (simulate_serve ApplicationServices.close)
    and at worker process shutdown (etl atexit handler).
    """
    global _client
    with _lock:
        if _client is None:
            return
        try:
            _client.flush()
        except Exception as exc:
            logger.warning("Langfuse flush failed during shutdown: %s", exc)
        try:
            _client.shutdown()
        except Exception as exc:
            logger.warning("Langfuse shutdown failed: %s", exc)
        _client = None


# ---------------------------------------------------------------------------
# Payload helpers (deep copy, JSON-safe, size truncation)
# ---------------------------------------------------------------------------


def snapshot(obj: Any) -> Any:
    """Deep copy an object so caller mutations do not affect the captured snapshot."""
    try:
        return copy.deepcopy(obj)
    except Exception:
        # Fallback for objects deepcopy cannot handle (e.g. locks); return
        # a best-effort repr so callers still get *something* traceable.
        try:
            return _to_jsonable(obj)
        except Exception:
            return {"_unrepr": str(obj)[:1000]}


def _to_jsonable(obj: Any) -> Any:
    """Recursively coerce an object into JSON-serializable primitives.

    Handles pydantic v2 models (model_dump), dataclasses, dicts, lists.
    Non-standard objects fall back to ``str(obj)``.
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json", exclude_none=True)
        except Exception:
            pass
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "__dict__") and vars(obj):
        try:
            return {k: _to_jsonable(v) for k, v in vars(obj).items()
                    if not k.startswith("_")}
        except Exception:
            pass
    return str(obj)


def _maybe_truncate(obj: Any, max_bytes: int) -> Any:
    """Return ``obj`` (JSON-coerced) if its serialized size ≤ max_bytes.

    Otherwise return a placeholder dict indicating truncation. ``max_bytes=0``
    disables truncation and returns the original object unchanged.
    """
    if not max_bytes or obj is None:
        return _to_jsonable(obj) if obj is not None else obj
    try:
        s = json.dumps(_to_jsonable(obj), ensure_ascii=False)
        size = len(s.encode("utf-8"))
        if size <= max_bytes:
            return _to_jsonable(obj)
        keys = list(obj.keys()) if isinstance(obj, dict) else None
        return {"_truncated": True, "size": size, "max": max_bytes, "keys": keys}
    except Exception:
        return _to_jsonable(obj)


# ---------------------------------------------------------------------------
# Span contexts (outer trace + child step)
# ---------------------------------------------------------------------------


@contextmanager
def stage_trace(
    client: Any | None,
    *,
    session_id: str,
    name: str,
    user_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    input_data: Any = None,
    output_capture: Callable[[], Any] | None = None,
    payload_mode: str = "full",
    max_payload_bytes: int = 0,
) -> Iterator[Any]:
    """Open an outer trace span; yields the span (or ``None`` when disabled).

    Business exceptions are NOT caught: the caller's flow is unchanged.
    On exception, the span is annotated with ``level="ERROR"`` and a
    short ``status_message`` before the exception propagates.
    """
    if client is None:
        yield None
        return

    md = dict(metadata or {})
    if task_id and "task_id" not in md:
        md["task_id"] = task_id
    safe_user_id = user_id if user_id is not None else session_id

    def _input():
        return _resolve_payload(input_data, payload_mode, max_payload_bytes)

    def _output():
        if output_capture is None:
            return None
        try:
            return _resolve_payload(output_capture(), payload_mode, max_payload_bytes)
        except Exception as exc:
            return {"_output_capture_error": f"{type(exc).__name__}: {exc}"}

    span_cm = _open_observation(
        client,
        as_type=None,
        name=name,
        session_id=session_id,
        user_id=safe_user_id,
        tags=tags,
        metadata=md,
        input_fn=_input,
        output_fn=_output,
    )
    try:
        with span_cm as span:
            yield span
    except Exception as exc:
        try:
            if span is not None:
                span.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        raise


@contextmanager
def step_span(
    client: Any | None,
    *,
    name: str,
    payload_mode: str = "full",
    input_data: Any = None,
    output_capture: Callable[[], Any] | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    max_payload_bytes: int = 0,
    metadata: dict[str, Any] | None = None,
    as_type: str | None = None,
) -> Iterator[Any]:
    """Open a child span under the current observation context.

    ``as_type="generation"`` produces a Langfuse generation (records LLM
    usage); ``None`` or omitted produces a generic span. On business
    exception, the span is annotated ``level="ERROR"`` before propagating.
    """
    if client is None:
        yield None
        return

    md = dict(metadata or {})
    if task_id and "task_id" not in md:
        md["task_id"] = task_id

    def _input():
        return _resolve_payload(input_data, payload_mode, max_payload_bytes)

    def _output():
        if output_capture is None:
            return None
        try:
            return _resolve_payload(output_capture(), payload_mode, max_payload_bytes)
        except Exception as exc:
            return {"_output_capture_error": f"{type(exc).__name__}: {exc}"}

    span_cm = _open_observation(
        client,
        as_type=as_type,
        name=name,
        session_id=session_id,
        user_id=session_id,
        tags=None,
        metadata=md,
        input_fn=_input,
        output_fn=_output,
    )
    try:
        with span_cm as span:
            yield span
    except Exception as exc:
        try:
            if span is not None:
                span.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        raise


@contextmanager
def _open_observation(
    client: Any,
    *,
    as_type: str | None,
    name: str,
    session_id: str | None,
    user_id: str | None,
    tags: list[str] | None,
    metadata: dict[str, Any] | None,
    input_fn: Callable[[], Any],
    output_fn: Callable[[], Any],
) -> Iterator[Any]:
    """Open a Langfuse observation; tag/metadata/input/output applied in __exit__.

    Uses ``start_as_current_observation`` (the recommended context manager
    for the v3 SDK) and ``propagate_attributes`` so nested child spans
    inherit session_id / user_id / tags.
    """
    pa_ctx = None
    if propagate_attributes is not None:
        pa_kwargs: dict[str, Any] = {}
        if session_id:
            pa_kwargs["session_id"] = session_id
        if user_id:
            pa_kwargs["user_id"] = user_id
        if tags:
            pa_kwargs["tags"] = list(tags)
        if pa_kwargs:
            pa_ctx = propagate_attributes(**pa_kwargs)

    span: Any = None
    cm: Any = None
    try:
        if pa_ctx is not None:
            try:
                pa_ctx.__enter__()
            except Exception:
                # propagate_attributes 内部 SDK 异常吞掉,与 start_observation
                # 异常对称;pa_ctx 失败不影响主路径.
                pa_ctx = None
        kwargs: dict[str, Any] = {"name": name}
        if as_type:
            kwargs["as_type"] = as_type
        try:
            cm = client.start_as_current_observation(**kwargs)
        except Exception as exc:
            logger.warning("langfuse start_observation failed: %s", exc)
            yield None
            return
        try:
            span = cm.__enter__()
        except Exception as exc:
            logger.warning("langfuse observation __enter__ failed: %s", exc)
            yield None
            return
        try:
            if metadata:
                span.update(metadata=metadata)
            try:
                span.update(input=input_fn())
            except Exception:
                pass
        except Exception:
            pass
        yield span
        try:
            out = output_fn()
            if out is not None:
                span.update(output=out)
        except Exception:
            pass
    finally:
        if span is not None and cm is not None:
            try:
                cm.__exit__(None, None, None)  # type: ignore[possibly-undefined]
            except Exception:
                pass
        if pa_ctx is not None:
            try:
                pa_ctx.__exit__(None, None, None)
            except Exception:
                pass


def _resolve_payload(data: Any, mode: str, max_bytes: int) -> Any:
    """Map ``upload_payload`` mode + size cap to a concrete value.

    - ``"none"``: return ``None`` (no input/output uploaded).
    - ``"summary"``: return ``{"summary": True, "size_hint": ...}``.
    - ``"full"`` (default): return the full JSON-coerced payload, truncated
      if it exceeds ``max_bytes``.
    """
    if mode == "none":
        return None
    if mode == "summary":
        try:
            s = json.dumps(_to_jsonable(data), ensure_ascii=False)
            return {"summary": True, "size_hint": len(s.encode("utf-8"))}
        except Exception:
            return {"summary": True, "size_hint": 0}
    # full
    if max_bytes:
        return _maybe_truncate(data, max_bytes)
    return _to_jsonable(data)
