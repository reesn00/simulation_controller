from __future__ import annotations

from typing import Any

from camel.toolkits import BaseToolkit, FunctionTool


class ProviderToolkit(BaseToolkit):
    def __init__(self, tools: list[FunctionTool], timeout: float | None = None):
        super().__init__(timeout=timeout)
        self._tools = tools

    def get_tools(self) -> list[FunctionTool]:
        return list(self._tools)


def as_function_tools(value: list[Any]) -> list[FunctionTool]:
    return [item for item in value if isinstance(item, FunctionTool)]
