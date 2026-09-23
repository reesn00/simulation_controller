"""Langfuse observability: shared factory used by simulate_serve / gdr / etl.

The factory lives in simulate_serve because simulate_serve is the original
sender; gdr / etl import from ``simulate_serve.observability.langfuse_client``
without coupling to simulate_serve business logic. All helpers are no-ops
when Langfuse is disabled or credentials are missing.
"""
from simulate_serve.observability.langfuse_client import (
    _extract_langfuse_fields,
    _maybe_truncate,
    _to_jsonable,
    get_client,
    shutdown,
    snapshot,
    stage_trace,
    step_span,
)

__all__ = [
    "_extract_langfuse_fields",
    "_maybe_truncate",
    "_to_jsonable",
    "get_client",
    "shutdown",
    "snapshot",
    "stage_trace",
    "step_span",
]
