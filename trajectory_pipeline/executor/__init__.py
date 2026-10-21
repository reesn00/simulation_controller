"""模块 2 · 代码执行循环——**新架构的心脏在这里**。

控制流（代码层做护栏，LLM 层做探索）::

    搜索 → 结果分派 → 逐站点：
        打开页面 → DOM 提取 → [Perceptor] 是否播放站？
            ├─ 否 → 跳过（负样本池）
            └─ 是 → 打开播放页 → [Perceptor] 有播放按钮/集数？
                    ├─ 否 → 排除（负样本池）
                    └─ 是 → [Perceptor] 有 video 标签/播放组件？
                            ├─ 否 → 标记「未验证」入库（负样本池）
                            └─ 是 → 记录成功样本

每条失败分支**必须在数据中有对应样本**——负样本不是副产品，是强制产出。

**两条链纪律**（2026-10-17 A2）：同一个候选站点有 scripted 与 agent
两种走法，**进站段与 ⑦ 段共用，站内那一段换人**::

    visit_site   ① ② ③ ③.5 ④ ⑤ ⑥ ⑦        七段固定序列
    agent_visit  ① ② ─── agent_loop ─── ⑦  站内交给模型
                 └ 共用 ┘                 └ 共用 ┘

``visit_site`` **不删不改**：A3 的 fallback 与黄金基线都是它。
模式是批次级属性（:class:`~.orchestrator.RunRecord` 的 ``executor_mode``），
分派只有 :meth:`~.orchestrator.Orchestrator._visit_guarded` 一处。

**唯一依赖纪律**：``executor`` 只 import ``perception.base`` 的**协议**，
不 import ``perception`` 的任何**实现**（rule / llm / factory），
也不 import ``llm``。这是 W1 规则版与 W3 LLM 版能无痛替换的唯一保证，
验收方式是切实现时本包的 git diff 必须为空。

.. note::

   2026-10-17 A2a-2/3：包内新增 :mod:`.agent_loop` 与
   :mod:`.steps.agent_visit`。它们的 LLM 依赖**不是 import 而是注入**
   （``json_of`` 与 LLM 客户端由 CLI 组装成 :class:`.agent_loop.AgentBackend`
   后整体传进来，与 ``rationale_writer`` 同款姿势）——``executor → llm``
   这条边在依赖图（01-架构总览 L26-50）里不存在，加它的代价是让执行层
   直接依赖 LLM 模块，与「切实现时 diff 为空」那条保证同源。
   有 AST 用例守着：运行时 import 里不得出现 ``trajectory_pipeline.llm``。
"""
