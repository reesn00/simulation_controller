from __future__ import annotations

from .models import BrowserInspectionRequest, BrowserInspectionResult
from .protocol import BrowserEvidenceProvider

_FALLBACK_CODES = frozenset({"bot_blocked", "renderer_crash", "browser_incompatible"})


class BrowserProviderSelector:
    def __init__(self, primary: BrowserEvidenceProvider, fallback: BrowserEvidenceProvider | None = None):
        self.primary = primary
        self.fallback = fallback

    async def inspect_url(self, request: BrowserInspectionRequest) -> BrowserInspectionResult:
        result = await self.primary.inspect_url(request)
        if result.error_code in _FALLBACK_CODES and self.fallback:
            secondary = await self.fallback.inspect_url(request)
            if secondary.confidence.value == "confirmed" or result.confidence.value == "none":
                return secondary
        if (
            self.fallback
            and request.evidence_depth == "status_required"
            and result.confidence.value == "supported"
            and (result.page is None or result.page.status is None)
        ):
            return await self.fallback.inspect_url(request)
        return result
