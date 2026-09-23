"""LangfuseConfig (orchestration view) + lazy root-config loader.

Lives under ``orchestration/observability/`` because ``run_etl_once`` (a
worker function called from the orchestration Pool) needs a lightweight
data container; the pydantic class in ``simulate_serve.config`` is the
schema of record, this dataclass is the worker-side handle.

Field names mirror ``simulate_serve.observability.langfuse_client.
_extract_langfuse_fields`` so the factory's duck-typing adapter accepts
this dataclass unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared_config import find_root_config, load_yaml_file


@dataclass(frozen=True)
class LangfuseConfig:
    """Worker-side view of the Langfuse observability settings."""

    enabled: bool = False
    public_key: str = ""
    secret_key: str = ""
    base_url: str = "https://cloud.langfuse.com"
    environment: str = "dev"
    release: str = "local"
    sample_rate: float = 1.0
    flush_at: int = 512
    flush_interval: float = 5.0
    timeout: int = 10
    upload_payload: str = "full"
    max_payload_bytes: int = 0
    max_block_payload_bytes: int = 0
    # Stage-level override: when False, this stage's spans are suppressed
    # even if the root ``enabled`` is True. Defaults to True so opting in
    # is explicit at the root.
    per_step_span: bool = True


def load_langfuse_config(config_path: str | Path | None = None) -> LangfuseConfig:
    """Lazily load the ``langfuse:`` section from the unified root config.

    Returns a fully-defaulted dataclass when the root config is missing or
    the ``langfuse:`` section is absent. Never raises.
    """
    try:
        path = Path(config_path) if config_path else find_root_config()
        if path is None:
            return LangfuseConfig()
        raw = load_yaml_file(path) or {}
    except Exception:
        return LangfuseConfig()
    if not isinstance(raw, dict):
        return LangfuseConfig()
    section = raw.get("langfuse")
    if not isinstance(section, dict):
        return LangfuseConfig()
    stages = section.get("stages")
    etl_stage = stages.get("etl") if isinstance(stages, dict) else None
    return LangfuseConfig(
        enabled=bool(section.get("enabled", False)),
        public_key=str(section.get("public_key", "")),
        secret_key=str(section.get("secret_key", "")),
        base_url=str(section.get("base_url", "https://cloud.langfuse.com")),
        environment=str(section.get("environment", "dev")),
        release=str(section.get("release", "local")),
        sample_rate=_safe_float(section.get("sample_rate"), 1.0),
        flush_at=_safe_int(section.get("flush_at"), 512),
        flush_interval=_safe_float(section.get("flush_interval"), 5.0),
        timeout=_safe_int(section.get("timeout"), 10),
        upload_payload=str(section.get("upload_payload", "full")),
        max_payload_bytes=_safe_int(section.get("max_payload_bytes"), 0),
        max_block_payload_bytes=_safe_int(section.get("max_block_payload_bytes"), 0),
        per_step_span=bool(
            (etl_stage or {}).get("per_step_span", True)
            if isinstance(etl_stage, dict) else True
        ),
    )


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
