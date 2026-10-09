from typing import Protocol

from .models import BrowserInspectionRequest, BrowserInspectionResult


class BrowserEvidenceProvider(Protocol):
    async def inspect_url(self, request: BrowserInspectionRequest) -> BrowserInspectionResult: ...
