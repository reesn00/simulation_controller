"""新契约读写（P1/P2/P3），与存量 C1/C2/C3 完全隔离。

⚠️ 本模块**待建**，当前只有 P1 的**落点定义**在
:mod:`trajectory_pipeline.executor.archive`；P2 的**形状与切条**在
:mod:`trajectory_pipeline.assembler.schema`（但尚不落盘），P3 无代码。
路径形状以 executor 侧为准，本文件不重复定义字面量。

    P1  output/pipeline/<task_id>__<run_id>.json    动作+工具参数+观察原文
    P2  output/pipeline/samples/<id>.json            六件套 + provenance + gate
    P3  output/pipeline/views/<id>_{messages,openai,meta}.json
    负样本池  output/pipeline/negative.jsonl         带 branch 字段

P1 的「动作 + 工具参数」落在 ``steps[]``（顶层是搜索阶段，各 ``visits[i]``
里是站点阶段），语义见 :mod:`trajectory_pipeline.executor.actions`。三条
口径在那里写死了，这里只提醒最容易踩的一条：

    **动作里没有 ``ref``，因为 ``ref`` 是会话内句柄。**
    点击目标一律是语义化的 ``{tag, label}``。它是结构保证——
    :class:`~trajectory_pipeline.executor.actions.Action` 的字段里没有
    可放 ref 的位置，不是序列化时滤掉的。assembler 侧的
    :class:`~trajectory_pipeline.assembler.schema.ActionView` 同款，
    观察视图里也把 ``ref`` 剥掉（否则模型会学着去对齐两套编号）。

P1 另有一个易被忽略的字段：``user_prompt``——用户**开口说的那句**
（persona 渲染后的原文）。它与 ``query``（拿去搜的检索串）是两件事。
六件套第 ③ 件要的是前者；它在 2026-10-09 之前只活在计划文件里，
于是 P2 拿不到用户轮次，而存档里看不出任何异常。

P2 读 P1 是**按文件格式读**，不 import ``executor``（与
:mod:`trajectory_pipeline.executor.plan` 不 import taskgen 同一条纪律：
交接面是格式，格式才冻得住）。由
``tests/contract/test_p2_contract.py::TestNoExecutorImport`` 强制。

隔离约束：**禁止读取仓库根 output/**。一读就继承存量那套隐性耦合。

设计方案 §4 早期把 P1 写成 ``observations/<run_id>/``（每 run 一个目录，
为动作流的分片存档预留）。**实际实现是平铺单文件**，两处已对齐到实现——
理由与「未来何时改回目录」见 :data:`~trajectory_pipeline.executor.archive.DEFAULT_ROOT`
的长注释。改动前请先读那段，它列了 4 处会被牵动的消费方。
"""
