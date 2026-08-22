from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from simulate_serve.domain.evidence import EvidenceConfidence
from simulate_serve.tools.descriptor import ToolDescriptor
from simulate_serve.tools.registry import ProviderProbeError

from .models import BrowserInspectionRequest, BrowserInspectionResult, PageObservation, detect_barriers
from .policy import UrlPolicyError, validate_public_url


class BrowserUseProvider:
    descriptor: ToolDescriptor

    def __init__(self, descriptor: ToolDescriptor):
        self.descriptor = descriptor
        self.tools: list[Any] = []
        self._agent: Any = None
        self._browser: Any = None
        self._llm: Any = None

    def _resolve_llm_config(self) -> tuple[str, str, str | None]:
        config = self.descriptor.config or {}
        model = getattr(self.descriptor, "model", None)

        llm_model = config.get("llm_model") or (model.model_name if model else None)
        llm_api_key = config.get("llm_api_key") or (model.api_key if model else None) or os.environ.get("OPENAI_API_KEY")
        llm_base_url = config.get("llm_base_url") or (model.base_url if model else None) or os.environ.get("OPENAI_BASE_URL")

        if model is not None and (model.model_type or "OPENAI").upper() == "ANTHROPIC":
            raise ConnectionError("browser-use does not support ANTHROPIC model type; use an OpenAI-compatible model")

        missing = []
        if not llm_model:
            missing.append("llm_model")
        if not llm_api_key:
            missing.append("llm_api_key")
        if missing:
            raise ConnectionError(f"browser-use LLM config missing: {', '.join(missing)}")

        return str(llm_model), str(llm_api_key), str(llm_base_url) if llm_base_url else None

    async def start(self) -> list[Any]:
        try:
            import browser_use  # noqa: F401
        except ImportError as exc:
            raise ModuleNotFoundError("browser-use is not installed") from exc

        from browser_use import Agent, Browser  # noqa: F811
        from langchain_openai import ChatOpenAI

        llm_model, llm_api_key, llm_base_url = self._resolve_llm_config()

        llm_kwargs: dict[str, Any] = {"model": llm_model, "api_key": llm_api_key}
        if llm_base_url:
            llm_kwargs["base_url"] = llm_base_url

        try:
            llm = ChatOpenAI(**llm_kwargs)
            await llm.ainvoke("ping")
        except Exception as exc:
            raise ConnectionError(f"LLM connection probe failed: {exc}") from exc
        self._llm = llm

        headless = bool(self.descriptor.config.get("headless", True))
        viewport_width = int(self.descriptor.config.get("viewport_width", 1280))
        viewport_height = int(self.descriptor.config.get("viewport_height", 720))

        try:
            browser = Browser(
                headless=headless,
                viewport={"width": viewport_width, "height": viewport_height},
            )
            await browser.start()
            page = await browser.get_current_page()
            await page.goto("about:blank")
            await page.close()
            await browser.stop()
        except Exception as exc:
            raise ProviderProbeError(f"Browser launch probe failed: {exc}") from exc

        browser = Browser(
            headless=headless,
            viewport={"width": viewport_width, "height": viewport_height},
        )
        await browser.start()
        self._browser = browser

        agent = Agent(
            task="",
            llm=llm,
            browser=browser,
        )
        self._agent = agent
        self.tools = []
        return self.tools

    async def inspect_url(self, request: BrowserInspectionRequest) -> BrowserInspectionResult:
        if self._agent is None or self._browser is None:
            raise RuntimeError("BrowserUse provider is not started")

        try:
            await validate_public_url(request.url)
        except UrlPolicyError:
            raise

        task = self._build_task(request)
        timeout_seconds = self.descriptor.call_timeout_seconds

        try:
            from browser_use import Agent

            self._agent.task = task
            result = await asyncio.wait_for(self._agent.run(max_steps=5), timeout=timeout_seconds)
        except Exception as exc:
            return self._failure_result(str(exc))

        try:
            final_url = result.final_url if hasattr(result, "final_url") and result.final_url else request.url
        except Exception:
            final_url = request.url
        try:
            title = result.title if hasattr(result, "title") and result.title else ""
        except Exception:
            title = ""
        try:
            page_text = result.text_summary if hasattr(result, "text_summary") and result.text_summary else ""
        except Exception:
            page_text = ""

        lower = page_text.casefold()
        media_count = 0
        progressed = False
        if "playback_probe" in request.allowed_actions:
            try:
                page = await self._browser.get_current_page()
                media_count = await page.locator("video, audio").count()
                if media_count:
                    progressed = bool(
                        await page.locator("video, audio").first.evaluate(
                            "async el => { const before=el.currentTime; el.muted=true; try { await el.play(); await new Promise(r=>setTimeout(r,1000)); } catch {} return el.currentTime>before; }"
                        )
                    )
            except Exception:
                pass

        barriers = detect_barriers(lower)
        confidence = EvidenceConfidence.CONFIRMED if not barriers.blocked else EvidenceConfidence.SUPPORTED

        return BrowserInspectionResult(
            provider=self.descriptor.name,
            evidence_id=f"ev_{uuid.uuid4().hex}",
            page=PageObservation(final_url=final_url, title=title, text_summary=page_text[:2000]),
            media_count=media_count,
            media_progress_observed=progressed,
            barriers=barriers,
            confidence=confidence,
            summary=f"browser-use agent inspection completed ({len(page_text)} chars)" if not barriers.blocked else "Page is gated",
        )

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.stop()
                self._browser = None
        except Exception:
            pass
        try:
            self._agent = None
        except Exception:
            pass
        try:
            self._llm = None
        except Exception:
            pass
        self.tools = []

    @staticmethod
    def _build_task(request: BrowserInspectionRequest) -> str:
        task = (
            f"Navigate to the URL: {request.url}\n"
            "Collect the following information only:\n"
            "1. The final URL after navigation (final_url)\n"
            "2. The page title (title)\n"
            "3. A summary of the visible text content on the page (text_summary)\n\n"
            "Do NOT click any links.\n"
            "Do NOT fill in or submit any forms.\n"
            "Do NOT type anything.\n"
            "Do NOT download any files.\n"
            "Only read and observe the page content."
        )
        return task

    @staticmethod
    def _failure_result(message: str) -> BrowserInspectionResult:
        lowered = message.casefold()
        if any(token in lowered for token in ("access denied", "bot", "403")):
            code = "bot_blocked"
            retryable = False
        elif any(token in lowered for token in ("crash", "renderer", "target closed")):
            code = "renderer_crash"
            retryable = True
        elif any(token in lowered for token in ("unsupported", "not implemented")):
            code = "browser_incompatible"
            retryable = False
        else:
            code = "tool_error"
            retryable = True
        return BrowserInspectionResult(
            provider="browser_use",
            evidence_id=f"ev_{uuid.uuid4().hex}",
            confidence=EvidenceConfidence.NONE,
            summary=message[:500],
            error_code=code,
            retryable=retryable,
        )