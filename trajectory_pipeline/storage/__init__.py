"""新契约读写（P1/P2/P3），与存量 C1/C2/C3 完全隔离。

⚠️ 本模块**待建**，当前只有 P1 的**落点定义**在
:mod:`trajectory_pipeline.executor.archive`；P2 / P3 尚无代码。
路径形状以该模块为准，本文件不重复定义字面量。

    P1  output/pipeline/<task_id>__<run_id>.json    动作+工具参数+观察原文
    P2  output/pipeline/samples/<id>.json            六件套 + provenance + gate
    P3  output/pipeline/views/<id>_{messages,openai,meta}.json
    负样本池  output/pipeline/negative.jsonl         带 branch 字段

隔离约束：**禁止读取仓库根 output/**。一读就继承存量那套隐性耦合。

设计方案 §4 早期把 P1 写成 ``observations/<run_id>/``（每 run 一个目录，
为动作流的分片存档预留）。**实际实现是平铺单文件**，两处已对齐到实现——
理由与「未来何时改回目录」见 :data:`~trajectory_pipeline.executor.archive.DEFAULT_ROOT`
的长注释。改动前请先读那段，它列了 4 处会被牵动的消费方。
"""