from __future__ import annotations

import json
import re
import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from camel.toolkits import MCPToolkit

from simulate_serve.domain.evidence import EvidenceConfidence
from simulate_serve.tools.descriptor import ToolDescriptor
from simulate_serve.tools.registry import ProviderProbeError, ProviderSchemaError

from .models import BarrierObservation, BrowserInspectionRequest, BrowserInspectionResult, PageObservation, detect_barriers
from .policy import validate_public_url


class PlaywrightMCPProvider:
    descriptor: ToolDescriptor

    def __init__(self, descriptor: ToolDescriptor):
        self.descriptor = descriptor
        self.tools: list[Any] = []
        self._available_tools: set[str] = set()
        self._toolkit: MCPToolkit | None = None
        packaged_runtime = Path(__file__).parents[2] / "tool_runtime" / "playwright"
        repository_runtime = Path(__file__).parents[3] / "tool_runtime" / "playwright"
        self._runtime_dir = packaged_runtime if packaged_runtime.exists() else repository_runtime

    async def start(self) -> list[Any]:
        if not shutil.which("node") or not shutil.which("npx"):
            raise FileNotFoundError("Node.js/npx is missing")
        package = self._runtime_dir / "node_modules" / "@playwright" / "mcp" / "package.json"
        if not package.exists():
            raise FileNotFoundError(f"Playwright MCP is not installed; run npm ci in {self._runtime_dir}")
        installed = json.loads(package.read_text(encoding="utf-8"))
        if installed.get("version") != "0.0.78":
            raise RuntimeError(f"Playwright MCP version mismatch: expected 0.0.78, got {installed.get('version')}")

        output_dir = Path(self.descriptor.config.get("output_dir", "output/tool-artifacts/playwright")).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "--no-install",
            "@playwright/mcp@0.0.78",
            "--headless",
            "--isolated",
            "--block-service-workers",
            "--image-responses=omit",
            "--output-mode=file",
            f"--output-dir={output_dir}",
        ]
        allowed_origins = self.descriptor.config.get("allowed_origins")
        blocked_origins = self.descriptor.config.get("blocked_origins")
        if allowed_origins:
            args.append(f"--allowed-origins={allowed_origins}")
        if blocked_origins:
            args.append(f"--blocked-origins={blocked_origins}")
        config = {
            "mcpServers": {
                self.descriptor.name: {
                    "command": "npx",
                    "args": args,
                    "cwd": str(self._runtime_dir),
                }
            }
        }
        self._toolkit = MCPToolkit(
            config_dict=config,
            timeout=self.descriptor.call_timeout_seconds,
            skip_failed=False,
            max_retries=0,
        )
        await self._toolkit.connect()
        self.tools = self._toolkit.get_tools()
        available = self._toolkit.list_available_tools()
        flattened = {name for names in available.values() for name in names}
        self._available_tools = flattened
        required = {"browser_navigate", "browser_snapshot"}
        if not required.issubset(flattened):
            raise ProviderSchemaError(f"Playwright MCP missing required tools: {sorted(required - flattened)}")
        try:
            await self._toolkit.call_tool("browser_navigate", {"url": "about:blank"})
            await self._toolkit.call_tool("browser_snapshot", {})
        except Exception as exc:
            raise ProviderProbeError(f"Playwright local browser probe failed: {exc}") from exc
        return self.tools

    async def inspect_url(self, request: BrowserInspectionRequest) -> BrowserInspectionResult:
        if self._toolkit is None:
            raise RuntimeError("Playwright provider is not started")
        await validate_public_url(request.url)
        try:
            await self._toolkit.call_tool("browser_navigate", {"url": request.url})
            snapshot = await self._toolkit.call_tool("browser_snapshot", {})
        except Exception as exc:
            message = str(exc)
            lowered = message.casefold()
            if any(token in lowered for token in ("access denied", "bot detected", "http 403")):
                code = "bot_blocked"
            elif any(token in lowered for token in ("target closed", "browser crashed", "renderer")):
                code = "renderer_crash"
            elif any(token in lowered for token in ("unsupported", "not implemented")):
                code = "browser_incompatible"
            else:
                code = "tool_error"
            return BrowserInspectionResult(
                provider=self.descriptor.name,
                evidence_id=f"ev_{uuid.uuid4().hex}",
                confidence=EvidenceConfidence.NONE,
                summary=message[:500],
                error_code=code,
                retryable=code in {"renderer_crash", "tool_error"},
            )
        text = str(snapshot)
        lower = text.casefold()
        media_count = len(re.findall(r"\b(?:video|audio)\b", lower))
        progressed = False
        if "playback_probe" in request.allowed_actions and "browser_run_code" in self._available_tools:
            probe = await self._toolkit.call_tool(
                "browser_run_code",
                {
                    "code": "async (page) => { const m = page.locator('video, audio').first(); if (await m.count() === 0) return {media:false,progressed:false}; return await m.evaluate(async el => { const before=el.currentTime; el.muted=true; try { await el.play(); await new Promise(r=>setTimeout(r,1000)); } catch {} return {media:true,progressed:el.currentTime>before}; }); }"
                },
            )
            progressed = self._playback_progressed(probe)
        barriers = detect_barriers(lower)
        return BrowserInspectionResult(
            provider=self.descriptor.name,
            evidence_id=f"ev_{uuid.uuid4().hex}",
            page=PageObservation(final_url=request.url, text_summary=text[:2000]),
            media_count=media_count,
            media_progress_observed=progressed,
            barriers=barriers,
            confidence=EvidenceConfidence.CONFIRMED if progressed and not barriers.blocked else EvidenceConfidence.SUPPORTED,
            summary="Playwright MCP snapshot collected" if not barriers.blocked else "Page is gated",
        )

    @classmethod
    def _playback_progressed(cls, value: object) -> bool:
        """Extract the exact progressed boolean without matching unrelated true values."""
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if isinstance(value, Mapping):
            progressed = value.get("progressed")
            if isinstance(progressed, bool):
                return progressed
            return any(cls._playback_progressed(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(cls._playback_progressed(item) for item in value)
        match = re.search(r"[\"']?progressed[\"']?\s*[:=]\s*(true|false)", str(value), re.IGNORECASE)
        return bool(match and match.group(1).casefold() == "true")

    async def close(self) -> None:
        if self._toolkit is not None:
            await self._toolkit.disconnect()
            self._toolkit = None
            self.tools = []
            self._available_tools = set()
