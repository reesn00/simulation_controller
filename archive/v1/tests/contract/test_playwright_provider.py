from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from simulate_serve.tools.browser.models import BrowserInspectionRequest
from simulate_serve.tools.browser.playwright_mcp import PlaywrightMCPProvider
from simulate_serve.tools.descriptor import ToolDescriptor


class FakeMCPToolkit:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls: list[tuple[str, dict]] = []
        self.disconnected = False

    async def connect(self):
        return self

    def get_tools(self):
        return [SimpleNamespace(name="browser_navigate"), SimpleNamespace(name="browser_snapshot")]

    def list_available_tools(self):
        return {"playwright": ["browser_navigate", "browser_snapshot"]}

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        if name == "browser_snapshot":
            return "page title Demo body public content"
        return "ok"

    async def disconnect(self):
        self.disconnected = True


@pytest.mark.asyncio
@pytest.mark.contract
async def test_playwright_provider_connect_discover_probe_and_close(tmp_path: Path, monkeypatch) -> None:
    package = tmp_path / "node_modules" / "@playwright" / "mcp" / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"version": "0.0.78"}), encoding="utf-8")
    monkeypatch.setattr("simulate_serve.tools.browser.playwright_mcp.MCPToolkit", FakeMCPToolkit)
    monkeypatch.setattr("simulate_serve.tools.browser.playwright_mcp.validate_public_url", _accept_url)
    provider = PlaywrightMCPProvider(
        ToolDescriptor(
            name="playwright",
            provider_type="playwright_mcp",
            enabled=True,
            capabilities=frozenset({"browser.navigate", "browser.snapshot"}),
            config={"output_dir": str(tmp_path / "artifacts")},
        )
    )
    provider._runtime_dir = tmp_path
    tools = await provider.start()
    assert [item.name for item in tools] == ["browser_navigate", "browser_snapshot"]
    result = await provider.inspect_url(BrowserInspectionRequest(url="https://example.test", criterion_id="c"))
    assert result.confidence.value == "supported"
    toolkit = provider._toolkit
    await provider.close()
    assert toolkit.disconnected


async def _accept_url(url: str) -> None:
    return None
