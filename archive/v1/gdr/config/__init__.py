"""config: pydantic-settings + 工具白名单加载。

`config.settings` 持有强类型 `Settings`; `config.tools.yaml` 维护工具白名单 + 幻觉 API 黑名单。
通过 `__init__.py` 重新导出, 兼容 `from config import Settings, load_tools` 写法。
"""
from gdr.config.settings import Settings, load_tools

__all__ = ["Settings", "load_tools"]