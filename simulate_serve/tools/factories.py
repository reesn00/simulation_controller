from simulate_serve.config import ModelConfig

from .browser.browser_use import BrowserUseProvider
from .browser.camoufox_driver import CamoufoxProvider
from .browser.playwright_mcp import PlaywrightMCPProvider
from .registry import ToolRegistry


def create_default_registry(model: ModelConfig | None = None) -> ToolRegistry:
    return ToolRegistry(
        {
            "playwright_mcp": PlaywrightMCPProvider,
            "camoufox": CamoufoxProvider,
            "browser_use": BrowserUseProvider,
        },
        model=model,
    )
