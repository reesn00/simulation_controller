from .browser.camoufox_driver import CamoufoxProvider
from .browser.playwright_mcp import PlaywrightMCPProvider
from .registry import ToolRegistry


def create_default_registry() -> ToolRegistry:
    return ToolRegistry(
        {
            "playwright_mcp": PlaywrightMCPProvider,
            "camoufox": CamoufoxProvider,
        }
    )
