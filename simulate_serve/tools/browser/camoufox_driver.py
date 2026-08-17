from __future__ import annotations

import re
import uuid
from typing import Any

from camel.toolkits import FunctionTool

from simulate_serve.domain.evidence import EvidenceConfidence
from simulate_serve.tools.descriptor import ToolDescriptor
from simulate_serve.tools.registry import ProviderProbeError

from .models import BarrierObservation, BrowserInspectionRequest, BrowserInspectionResult, PageObservation
from .policy import validate_public_url


class CamoufoxProvider:
    descriptor: ToolDescriptor

    def __init__(self, descriptor: ToolDescriptor):
        self.descriptor = descriptor
        self.tools: list[Any] = []
        self._camoufox_type: Any = None

    async def start(self) -> list[Any]:
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError as exc:
            raise ModuleNotFoundError("cloverlabs-camoufox is not installed") from exc
        self._camoufox_type = AsyncCamoufox
        try:
            async with AsyncCamoufox(headless=True, humanize=False) as browser:
                page = await browser.new_page()
                await page.goto("about:blank")
                await page.close()
        except Exception as exc:
            raise ProviderProbeError(f"Camoufox local browser probe failed: {exc}") from exc
        self.tools = [FunctionTool(self.inspect_url)]
        return self.tools

    async def inspect_url(self, request: BrowserInspectionRequest) -> BrowserInspectionResult:
        if self._camoufox_type is None:
            raise RuntimeError("Camoufox provider is not started")
        await validate_public_url(request.url)
        os_name = str(self.descriptor.config.get("os", "windows"))
        locale = str(self.descriptor.config.get("locale", "zh-CN"))
        async with self._camoufox_type(headless=True, humanize=False, os=os_name, locale=locale) as browser:
            page = await browser.new_page()
            try:
                response = await page.goto(request.url, wait_until="domcontentloaded", timeout=int(request.timeout_seconds * 1000))
                title = await page.title()
                body = await page.locator("body").inner_text(timeout=int(request.timeout_seconds * 1000))
                links = tuple(await page.locator("a[href]").evaluate_all("els => els.slice(0,100).map(e => e.href)"))
                media_count = await page.locator("video, audio").count()
                progressed = False
                if media_count and "playback_probe" in request.allowed_actions:
                    progressed = bool(
                        await page.locator("video, audio").first.evaluate(
                            "async el => { const before=el.currentTime; el.muted=true; try { await el.play(); await new Promise(r=>setTimeout(r,1000)); } catch {} return el.currentTime>before; }"
                        )
                    )
                final_url = page.url
                await validate_public_url(final_url)
                lower = body.casefold()
                barriers = BarrierObservation(
                    login=bool(re.search(r"登录|注册|sign\s*in|log\s*in", lower)),
                    membership=bool(re.search(r"会员|vip|subscription", lower)),
                    paywall=bool(re.search(r"付费|购买|paywall|purchase", lower)),
                    captcha=bool(re.search(r"验证码|captcha", lower)),
                    region_restricted=bool(re.search(r"地区限制|not available in your region", lower)),
                )
                status = response.status if response else None
                return BrowserInspectionResult(
                    provider=self.descriptor.name,
                    evidence_id=f"ev_{uuid.uuid4().hex}",
                    page=PageObservation(final_url=final_url, status=status, title=title, text_summary=body[:2000]),
                    links=links,
                    media_count=media_count,
                    media_progress_observed=progressed,
                    barriers=barriers,
                    confidence=EvidenceConfidence.CONFIRMED if progressed and status and status < 400 and not barriers.blocked else EvidenceConfidence.SUPPORTED,
                    summary="Camoufox page inspection completed" if not barriers.blocked else "Page is gated",
                )
            finally:
                await page.close()

    async def close(self) -> None:
        self.tools = []
        self._camoufox_type = None
