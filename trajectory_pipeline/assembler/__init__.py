"""模块 5 · 组装与切分。

样本六件套：系统提示词 / 工具参数列表 / 任务提示词(含 provenance) /
动作+参数 / 观察 / 思考+结论。

切分粒度：**一个决策单元一条**——搜索阶段一条，每个站点一条。
（设计方案 §3.5 早期写着「单站点验证、单搜索任务各自独立成条」，
同一节的「前几步走摘要」与它互相矛盾；按决策单元切则唯一确定，
且避免把「选哪个站」与「这个站行不行」压进同一次预测。）

当前状态
--------
- :mod:`~trajectory_pipeline.assembler.schema` —— 六件套值对象 + **P1 读取器**。
- :mod:`~trajectory_pipeline.assembler.observation_view` —— 训练态观察渲染（纯代码）。
- ``builder.py`` / ``splitter.py`` / ``views.py``（落 P2 / P3 文件）**待建**。

所以现在还不能产出 P2/P3 文件；切条结果只在内存里。
闸门一律 ``not_run``——模块 4（rationale）未落地，**没跑过不等于通过**。

两条不能破的纪律
--------------
1. **读 P1 按文件格式，不 import ``executor``。** 与
   :mod:`trajectory_pipeline.executor.plan` 消费模块 1 的计划同一条：
   交接面是格式，不是 import。理由是 P1 贵、P2 便宜——重建一批 P2 必须
   独立于 executor 的当前版本。由
   ``tests/contract/test_p2_contract.py::TestNoExecutorImport`` 用 AST 强制。
2. **训练动作空间在这里冻结**，不从 P1 反推也不从 executor import。
   漂移由同一份契约测试对着 ``executor/actions.py`` 的 ``TOOLS``
   字面量查（AST 读，不 import）。
"""
