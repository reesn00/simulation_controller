"""observability 测试 conftest: 自动 skip langfuse_real 标记的测试.

PR 6 (Commit 4): 默认 ``uv run pytest -q`` 不跑真 Langfuse 测试,
仅当 ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` 环境变量都设置时才
会跑到 ``test_langfuse_real_smoke.py`` 内的 smoke 测试.

使用方式::

    # 默认 (跳过真 smoke):
    uv run pytest tests/ -q

    # 显式跑真 smoke:
    export LANGFUSE_PUBLIC_KEY=pk-lf-xxx
    export LANGFUSE_SECRET_KEY=sk-lf-xxx
    uv run pytest tests/observability/test_langfuse_real_smoke.py -m langfuse_real -v
"""
from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config, items):
    """对所有 ``langfuse_real`` 标记的测试, 如缺凭据自动 skip.

    注: ``test_langfuse_real_smoke.py`` 自身有 module-level skipif (双层防御),
    此处 conftest 是兜底: 即便用户绕过 module-level 标记也强制 skip.
    """
    has_creds = bool(
        os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    )
    if has_creds:
        return  # 不缺凭据, 让真 smoke 跑
    skip_marker = pytest.mark.skip(
        reason="langfuse_real: requires LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY",
    )
    for item in items:
        if "langfuse_real" in item.keywords:
            item.add_marker(skip_marker)