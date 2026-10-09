"""测试树，与存量 tests/ 并列。

分工：
    unit/        单模块逻辑
    contract/    **边界契约**——perception 五不变式、P1/P2/P3 schema、
                 PageDriver 原语映射、LLM schema 解析鲁棒性
    functional/  离线端到端

门禁：本树已并入根 pyproject.toml 的 testpaths。
（教训：存量 gdr/tests 曾长期不在 testpaths 内，360+ 用例不跑，
  掩盖了 3 处陈旧导入和 2 处生产代码静默失效。**门禁漏掉一棵树 = 这些全部看不见。**）

纪律：验证改动必须**串行**跑——并发 pytest 抢 ``.pytest-tmp`` 会连锁打挂无关测试。
"""
