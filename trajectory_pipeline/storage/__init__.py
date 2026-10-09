"""新契约读写（P1/P2/P3），与存量 C1/C2/C3 完全隔离。

    P1  output/pipeline/observations/<run_id>/   动作+参数+观察原文（真实回放）
    P2  output/pipeline/samples/<id>.json        六件套 + provenance + gate
    P3  output/pipeline/views/<id>_{messages,openai,meta}.json
    负样本池  output/pipeline/negative.jsonl     带 branch 字段

隔离约束：**禁止读取仓库根 output/**。一读就继承存量那套隐性耦合。
"""
