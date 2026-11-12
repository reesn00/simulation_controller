"""**观察降级标记：按「哪一块坏了」分类**（2026-11-11，H-32）。

为什么要有这个模块
------------------
``Observation.degraded`` 是一个**列表**，每个元素是一个**内容块**的降级
标记——它**本来就是按块记的**。问题出在**消费侧一刀切**：

    if obs.degraded:
        return []      # ← 任何一块坏了 ⇒ 整页作废

all80 批次实测：**13 条任务 0 候选空跑**，而它们的搜索页 links 明明有
**102–111 条**——只因**正文**被判乱码（``body_mojibake``）就整批候选
被清空（同一批里 T029 立刻取到 14 个候选 ⇒ **间歇性触发**，不是百度
页面的稳定属性）。

⇒ 降级是**内容块级**的事实，消费方必须**按块问**：「**我这一块**坏了吗？」
而不是「**有没有**块坏了」。

四块 + 兜底
-----------
``body``
    正文 / 标题（含解码损坏）。**与链接无关**——候选是从 links 提取的。
``links``
    链接表本身。**这一块坏了 ⇒ 候选无从谈起。**
``elements``
    交互元素枚举。与候选提取无关（候选走 links，不走元素表）。
``nav``
    导航 / 读数**跨了文档**（半成品观察）⇒ 链接可能来自另一个文档。
``other``
    其余（计数失败、按键不支持、**没见过的标记**…）。与候选提取无关。

⚠️ **未知标记归 ``other``——不猜它属于哪一块**：把一个没见过的标记猜成
「无害」会让它悄悄作废整批数据，猜成「致命」又会误杀。归 ``other`` =
**不否决候选**，但它**原样留在 ``degraded`` 里**，读档的人看得见。

⚠️ **否决与否不是「降级有没有被记录」**：真源仍是各驱动写进
``Observation.degraded`` 的那个列表——本模块**只做分类**，不造标记、
不改标记、不吞标记。
"""

from __future__ import annotations

from typing import Iterable

# ── 内容块 ────────────────────────────────────────────────────────

#: 正文 / 标题（含解码损坏）。
BLOCK_BODY = "body"
#: 链接表本身。
BLOCK_LINKS = "links"
#: 交互元素枚举。
BLOCK_ELEMENTS = "elements"
#: 导航 / 读数完整性（读数跨了文档）。
BLOCK_NAV = "nav"
#: 其余 / 兜底。
BLOCK_OTHER = "other"

# ── 驱动写出的降级标记（真源在驱动侧，分类在这里）──────────────

#: **读数跨了文档**（camofox 面）：导航是异步 commit 的，commit 落在
#: 两次往返之间时，这份观察就是**两个文档拼起来的**。
DEGRADED_NAV_UNSETTLED = "nav_unsettled"

#: **按键做不到**（能力降级，不是「元素不存在」）。
DEGRADED_PRESS_UNSUPPORTED = "press_unsupported"

#: **候选过滤器里的「非致命降级」记账键**（H-32）。
#:
#: ⚠️ **它不是「剔除了候选」**——它记的是「这一轮**有**降级，但它**不影响**
#: 候选提取」。⇒ 读 ``candidate_filter`` 的地方**不许**把它算进
#: 「已过滤」或「结构性剔除」——那会把「候选照取」读成「候选被剔掉了」。
#:
#: ⚠️ **真源在 `executor/steps/search.py`**（写入点），这里只放**键名**，
#: 好让读侧（批次层打印 · 批次级断言）与写侧**用同一个字面量**。
FILTER_KEY_DEGRADED_OTHER = "degraded_other"

#: 标记 → 内容块。**穷举当前各驱动实际写出的标记**。
#:
#: ⚠️ **这张表会漂**：驱动新增一个标记而这里没登记 ⇒ 它落到
#: :data:`BLOCK_OTHER`（不否决候选）。契约测试盯住这件事
#: （``tests/unit/test_degraded_blocks.py::test_驱动写的标记都在表里``）。
MARKER_BLOCKS: dict[str, str] = {
    # ── 正文 / 标题 ──
    "body_text": BLOCK_BODY,        # camofox / obscura：正文没采到
    "body_mojibake": BLOCK_BODY,    # 正文采到了但解码损坏
    "title_mojibake": BLOCK_BODY,   # 标题解码损坏（与正文分开记）
    "browser_evaluate": BLOCK_BODY,  # obscura：innerText tool 失败
    # ── 链接 ──
    "links": BLOCK_LINKS,           # camofox：链接这块没采到
    "browser_links": BLOCK_LINKS,   # obscura：链接 tool 失败
    # ── 交互元素 ──
    "interactive": BLOCK_ELEMENTS,  # camofox：元素枚举失败
    "browser_interactive_elements": BLOCK_ELEMENTS,  # obscura
    # ── 导航 / 读数完整性 ──
    DEGRADED_NAV_UNSETTLED: BLOCK_NAV,
}


def block_of(marker: str) -> str:
    """这个降级标记属于哪一块。**未知标记归** :data:`BLOCK_OTHER`。"""
    return MARKER_BLOCKS.get(str(marker), BLOCK_OTHER)


def blocks_of(markers: Iterable[str] | None) -> set[str]:
    """一组降级标记覆盖了哪几块。"""
    return {block_of(m) for m in (markers or ())}


def blocks_candidate_extraction(markers: Iterable[str] | None) -> bool:
    """这组降级**是否让「从 links 提候选」这件事不成立**。

    ⚠️ **只有 ``links`` 与 ``nav`` 两块算**：

    - ``links`` 块坏了 ⇒ 链接表本身没采到（此时 ``obs.links`` 通常也是
      空的 ⇒ 这个判断多半是**冗余**的——但它保住 ``candidate_filter``
      里 ``degraded`` 这个记账键）；
    - ``nav`` 块坏了 ⇒ 读数跨了文档，链接可能来自另一个文档。

    而 ``body`` / ``elements`` / ``other`` **与候选提取无关**：
    候选是从 ``obs.links`` 提取的，**正文乱码或元素枚举失败动不了它**
    （all80 实测 13 条任务因此 0 候选空跑，H-32）。
    """
    return bool(blocks_of(markers) & {BLOCK_LINKS, BLOCK_NAV})
