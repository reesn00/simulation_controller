"""orchestration: 三阶段 Pipeline 顶层调度器.

把 simulate_serve / etl.qwenformat / gdr 三个独立子系统串成
trajectory -> qwenformat -> gdr 的流水线，详见
``docs/orchestration-design.md``。
"""

__all__: list[str] = []
