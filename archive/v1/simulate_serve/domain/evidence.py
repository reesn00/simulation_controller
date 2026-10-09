from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvidenceStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class EvidenceConfidence(str, Enum):
    CONFIRMED = "confirmed"
    SUPPORTED = "supported"
    WEAK = "weak"
    NONE = "none"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    source: str
    tool_name: str
    capability: str
    status: EvidenceStatus
    summary: str
    confidence: EvidenceConfidence = EvidenceConfidence.NONE
    artifact_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: int = 0
