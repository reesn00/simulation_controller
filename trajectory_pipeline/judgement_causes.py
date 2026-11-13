"""**缺判归因词表**——跨层地基，不属 ``evaluation`` 任何一个子包。

为什么放在顶层
--------------
「这条为什么没判出来」要**分栏报**：「素材缺件」与「裁判后端没答」是
两件事，处置方向相反（补数据 vs 修后端）。混在一个 ``quarantine`` 桶里
就等于把两者合并成一句「无法判定」——2026-11-11 那批 **578 次后端失败**
与 **478 次老存档缺字段**因此长得一模一样，报表上分不出来。

这套词表有**三处**要用：

- :mod:`trajectory_pipeline.evaluation.scorers.code_scorer` —— **产出**
  归因（D-2 判不出时写明是哪一类）；
- :mod:`trajectory_pipeline.evaluation.scout.io` /
  :mod:`trajectory_pipeline.evaluation.scout.aggregate` —— **汇总分栏**
  （「数据判不出」/「LLM 没答」两个读数）；
- :mod:`trajectory_pipeline.evaluation.ls_bridge.exporter` —— **闸门**
  （归因「LLM 没答」的记录不进人工队列）。

而 ``ls_bridge`` 的契约测试
（``tests/contract/test_ls_bridge_contract.py``）**只准**依赖 ``storage``
与 ``scout.types`` 这类纯类型/纯常量模块——``evaluation.scorers`` 不在
白名单里（它是评分器本体）。同 :mod:`trajectory_pipeline.projection` 的
处理：**放到顶层，只放字面量与纯函数，不 import 任何功能模块**。

⚠️ **这里是这套词表的唯一真源**——三处各写一份字符串必然漂（漂了之后
「LLM 没答」会被读成「素材差」，正好把要防的事做反了）。
"""

from __future__ import annotations

from typing import Any, Mapping

#: 已判出（无缺判）。
CAUSE_SCORED = ""
#: **数据判不出**——缺观察 / 无模型动作 / 老存档缺字段。与后端无关。
CAUSE_DATA = "data"
#: **LLM 没答**——超时 / 4xx / 5xx / 正文空 / 预算耗尽 / 熔断。
CAUSE_LLM = "llm"
#: **压根没配后端**——没发过请求。它也是「LLM 侧」的缺口，但**不是**
#: 「后端挂了」：熔断计数不能把它算进去（没发请求，熔断无意义）。
CAUSE_NO_CLIENT = "no_client"

#: 归因取值全集（读侧校验用）。
CAUSES: frozenset[str] = frozenset(
    {CAUSE_SCORED, CAUSE_DATA, CAUSE_LLM, CAUSE_NO_CLIENT}
)


def llm_unanswered(record: Mapping[str, Any]) -> bool:
    """这条记录是不是「**裁判后端没答**」造成的缺判。

    ⚠️ **为什么上传侧要认这个**：缺判有两种，处置方向相反——
    「素材缺件」要补数据，「后端没答」要修后端。把后者当前者推上 LS，
    只会让人工队列里多出一批「无法判定」，而它们的真实原因在标注页上
    **看不出来**（看上去就是素材差）。**2026-11-11 那批 578 条就是这样
    混在 quarantine 里被当成人工分析素材的。**

    ⚠️ **旧产物没有归因字段** ⇒ 一律返回 False（照旧推）——
    不能因为「读不出归因」就把历史产物全拦下来。

    Args:
        record: :class:`~trajectory_pipeline.evaluation.scout.types.ScoutRecord`
            或 :class:`~trajectory_pipeline.evaluation.scout.types.TrajectoryRecord`
            形态。
    """
    if str(record.get("undetermined_cause") or "") == CAUSE_LLM:
        return True
    for score in record.get("scores") or ():
        if str(score.get("cause") or "") in (CAUSE_LLM, CAUSE_NO_CLIENT):
            return True
    return False


__all__ = [
    "CAUSE_SCORED",
    "CAUSE_DATA",
    "CAUSE_LLM",
    "CAUSE_NO_CLIENT",
    "CAUSES",
    "llm_unanswered",
]
