"""Orchestration-layer Langfuse configuration (frozen dataclass).

The factory lives in ``simulate_serve.observability.langfuse_client`` and
accepts duck-typed configs; this module is just the orchestration-side
data class + lazy loader from the unified root config.
"""
