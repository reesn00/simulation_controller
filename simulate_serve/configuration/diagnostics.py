from enum import Enum

from pydantic import BaseModel, ConfigDict


class DiagnosticSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class CatalogDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: DiagnosticSeverity
    code: str
    message: str
    source: str
    path: str = ""
