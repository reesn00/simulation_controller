"""模块 3 · 语义感知——可替换插件。完整规格见 docs/设计方案/01-模块3-可替换感知层.md。

插件化的理由不是架构洁癖，是硬约束：W1 要在没有 LLM 的情况下跑出素材
（否则里程碑依赖倒置，W2 的失败归因会等 W3 的执行器）。

LLM 是传感器不是驾驶员：输入是 DOM 预处理后的结构化观察，
输出必须落在 Decision 的 schema 里供代码分支。

**唯一依赖纪律**：本包**不得 import executor**。输入只有 Observation 值对象，
不持有页面句柄、不回调控制流——否则插件退化成耦合。

七不变式（``tests/contract/test_perception.py`` 强制，见 01 号文档 §3）：
    I1 evidence 可溯源 / I2 confidence∈[0,1] / I3 幂等 / I4 fail-closed
    I5 无副作用 / I6 decision 型题 payload 完整且 ref 可溯源 / I7 answer=True 时 payload 非空
其中 **I3 对 LLM 版要求 temperature=0**，重试只许同提示词重采样。
**I4 不可妥协**：没把握时返回 answer=None 走 unresolved 分支，绝不猜 False。

三个实现
--------
- :class:`~trajectory_pipeline.perception.rule_perceptor.RulePerceptor`
  —— W1。**故意做得很弱**：确定性事实（预告片词表、``<video>`` 存在性）
  之外一律 ``answer=None``。让规则版去猜语义，得到的是**假信号**，会污染
  负样本池——「这站没有播放控件」看起来有代码背书，实际是关键词匹配的幻觉。
- :class:`~trajectory_pipeline.perception.llm_perceptor.LLMPerceptor`
  —— W3。**混合体不是纯 LLM**：确定性事实优先（存在性不问模型）、
  采集充分性在代码层预检（问模型**之前**）、ref 白名单校验。
  三条都是实测逼出来的，最贵的一条见
  ``docs/02-避坑指南.md §5.5``：**模型不会替你 fail-closed**。
- :mod:`~trajectory_pipeline.perception.factory` —— 注入与灰度。
  灰度**只在构造期**，不做运行期切换（理由见该模块 docstring）。

契约测试对三者跑同一套不变式：``IMPLEMENTATIONS`` 里加一行即可，
所以**切实现时 executor 的 git diff 必须为空**。

第四个模块 :mod:`~trajectory_pipeline.perception.replay` 不是实现，是**回放**：
把 P1 存档里的真实观察原样喂给上面几个实现，不开浏览器。它把 P1 当
**数据文件**读（``json.load``），因此不 import executor——「插件退化成耦合」
的第一步就是让感知层知道控制流的存在。
"""