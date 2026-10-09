"""执行层：obscura（MCP，stdio）——决策 D7 已定稿。

obscura-mcp 0.2.4 / 协议 2024-11-05 / 37 tools / 会话式。
实测清单存档 ``output/pipeline/obscura_tools.json``，
由 ``mcp_probe.py`` 生成，可重跑做版本回归（obscura 升级会改 tool 签名）。

三层结构：
    page_driver.py    PageDriver 协议——唯一稳定契约，只暴露原子原语
    mcp_client.py     MCP stdio 客户端
    obscura_driver.py obscura 接入实现

红线（继承 CLAUDE.md）：**不暴露 cookie / storage state 原语**，
在接口层堵死，不依赖调用方自觉。
"""
