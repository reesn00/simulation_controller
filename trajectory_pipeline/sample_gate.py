"""「这份存档切不出样本」的**唯一判据**——P2 与 T 共用（H-31 第 4 层 / H-33）。

**为什么挂在顶层**
----------------

P2 侧在 :mod:`trajectory_pipeline.assembler.schema`（切条），
T 侧在 :mod:`trajectory_pipeline.assembler.task_view`（渲染训练视图）——
**两边互不 import**（契约 D14.3：``T 不读 P2``，连 ``SYSTEM_PROMPT`` 与
``TOOL_SPECS`` 都自己拷）。所以「同一条判据」不能靠在一边 import 另一边，
得有个**两边都能用**的地方——同 :mod:`trajectory_pipeline.projection` 与
:mod:`trajectory_pipeline.toolcatalog` 的先例。

⚠️ **本模块只放纯函数**：不 import 任何功能模块（同 ``projection.py`` 的
纪律——地基变成依赖源就完了），不碰盘上任何东西。

**为什么判据必须只有一份**
--------------------------

「P2 不出样本 ⇒ T 不出训练视图」（H-31 第 4 层）这条纪律，
**两边各判一次就会分叉**，而分叉的后果不是报错，是**交付目录里多出
训练集里没有的东西**：

- 2026-11-11 实测：T 侧只挡了「判分不可信」，**漏了「搜索阶段没进决策」**
  ⇒ T 多渲染 **53 份**退化空壳（``outcomes=0`` · ``steps=1`` ·
  ``candidates=0``），而 P2 侧一份都不出 ⇒ **报告上零处异常**；
- 判据分叉的另一半更疼：一份被判「不进训练集」的档，
  它的 **P3 视图残留照样进 SFT 语料**（``storage.read_p3.read_views``
  按 ``sample_id`` **直读目录**、**不问 P2 在不在**）。

⇒ 所以这里的每个判据都写成「**读存档自身的字段**」——
两边读**同一份 P1** 的**同一个键**，口径自然同源，不需要谁 import 谁。
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["sample_blocked_reason", "search_not_decided_reason"]


def _search_collection_failed(archive: Mapping[str, Any]) -> bool:
    """搜索页采集是否因异常失败（网络错误等），而非正常搜到再被拦。

    与 ``search_blocked`` 一样是「采集/决策**尚未发生**」的形态：
    executor 在搜索页 ``goto``/``observe`` 抛异常时提前 return，只留一条
    ``搜索页采集失败: <ExcType>: <exc>`` warning，没有任何搜索观察或访问
    记录。此时切不出样本**不是错误**——被遍历时抛出会让整批陪葬
    （同 ``search_blocked`` 的豁免逻辑）。
    """
    return any(
        str(w).startswith("搜索页采集失败") for w in archive.get("warnings") or ()
    )


def search_not_decided_reason(archive: Mapping[str, Any]) -> str | None:
    """搜索阶段没进决策 ⇒ 这份 run **产出零条**（正常形态，不是错误）。

    两个形态：**被反爬拦**（``search_blocked``，观察是验证码页）与
    **搜索页采集失败**（网络错误等，只有 warning 没有搜索观察）。

    Returns:
        可印给人看的原因（``None`` = 搜索阶段进决策了，能切出样本）。
    """
    if archive.get("search_blocked"):
        return "反爬拦截：搜索阶段没进决策——不进训练集"
    if _search_collection_failed(archive):
        return "搜索页采集失败：搜索阶段没进决策——不进训练集"
    return None


def sample_blocked_reason(archive: Mapping[str, Any]) -> str | None:
    """这份存档**切不出样本**的原因（``None`` = 能切出）。

    ⚠️ **这是 P2 与 T 的同一判据**：P2 侧由
    :func:`trajectory_pipeline.assembler.schema.split_archive_report` 消费，
    T 侧由 :func:`trajectory_pipeline.assembler.task_view.build_task_view`
    消费。**两边各写一份就会漂**——漂出来的表现是「交付目录里的东西比
    训练集多」，而报告上看不出任何异常。

    三个档（**都是正常形态，不是错误**）：

    1. **判分不可信**（``judgement_trustworthy is False``，H-31）：
       判断点全程 fail-closed ⇒ 切出来的分支**不是站点的性质**，是「判不出来」
       ⇒ 切成训练样本就是教模型「这些站看不了」，而真相是「我们这边判不出来」。
       ⚠️ 判据用「**显式 False**」：历史存档没有这个键 ⇒ 缺键 = 不知道
       ≠ 不可信（「并池是写入时的动作」，不回填历史存档）。
    2. **反爬拦截**（:func:`search_not_decided_reason`）。
    3. **搜索页采集失败**（同上）。

    Returns:
        可印给人看的原因（``None`` = 能切出样本）。
    """
    if archive.get("judgement_trustworthy") is False:
        return "判分不可信：LLM 熔断，H-31——不进训练集"
    return search_not_decided_reason(archive)
