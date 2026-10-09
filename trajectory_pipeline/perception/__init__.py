"""模块 3 · 语义感知——可替换插件。完整规格见 docs/设计方案/01-模块3-可替换感知层.md。

插件化的理由不是架构洁癖，是硬约束：W1 要在没有 LLM 的情况下跑出素材
（否则里程碑依赖倒置，W2 的失败归因会等 W3 的执行器）。

LLM 是传感器不是驾驶员：输入是 DOM 预处理后的结构化观察，
输出必须落在 Decision 的 schema 里供代码分支。

**唯一依赖纪律**：本包**不得 import executor**。输入只有 Observation 值对象，
不持有页面句柄、不回调控制流——否则插件退化成耦合。

五不变式（契约测试强制，见 01 号文档 §3）：
    I1 evidence 可溯源 / I2 confidence∈[0,1] / I3 幂等 / I4 fail-closed / I5 无副作用
其中 **I3 对 LLM 版要求 temperature=0**，重试只许同提示词重采样。
**I4 不可妥协**：没把握时返回 answer=None 走 unresolved 分支，绝不猜 False。
"""
