"""routing: 缺陷路由 (规则层 + LLM 3-票投票层)。

通过 `__init__.py` 重新导出 `Router`, 兼容 `from router import Router` 旧写法。
"""
from routing.router import Router

__all__ = ["Router"]