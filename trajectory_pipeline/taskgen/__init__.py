"""模块 1 · 任务生成：任务骨架 × persona 画像 × scenario 策略 → 任务表述。

persona 与存量 scenario **正交**，不混：
    persona  回答「谁在问」（用户画像，决定首轮表述）
    scenario 回答「被追问时怎么反应」（存量，只读复用）

铁律：persona 与改写**只改表述，不改判分标准**。判分维度从固定标准库枚举，
``normalizer.py`` 必须能把任意改写结果归一回去；归一失败即丢弃改写。

依赖约束：可 import ``common`` / ``llm``；**不得 import executor 及之后**。
"""
