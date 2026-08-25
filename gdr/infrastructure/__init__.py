"""infrastructure: 跨模块的基础设施组件。

- `infrastructure.logging`: 双通道日志 (console + JSONL RotatingFile)。
- `infrastructure.llm_client`: HTTP OpenAI 兼容 LLM 客户端 (历史名 LlamaCppClient)。

通过 `__init__.py` 重新导出, 兼容 `from logger import setup_logger` / `from refiners.base import LlamaCppClient` 旧写法。
"""
from infrastructure.logging import setup_logger
from infrastructure.llm_client import LlamaCppClient

__all__ = ["setup_logger", "LlamaCppClient"]