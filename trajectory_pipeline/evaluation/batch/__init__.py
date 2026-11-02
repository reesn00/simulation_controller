"""批次级断言层（rubric v1.3 的 B-1..B-5）——**一批 = 一个 run**。

rubric v1.3 首次把「批」写死为**一个 run**：一批 = **一次运行的存档**
（``run_id``），批次级断言表**每个 run 一份**。理由是批次级断言量的是
**一次运行的结构属性**（动作空间、候选处置、异常处置、结论错率），
这些属性只在 run 之间可比；把多个 run 合成一批再算比率，分母就把不同
运行的处置密度混在一起，读数**没有对象**。

模块入口在 :mod:`trajectory_pipeline.evaluation.batch.asserts`。
"""

from __future__ import annotations

__all__ = ["asserts"]
