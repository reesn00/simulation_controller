"""精炼提示词——**筛选器**，不是摘要器。

为什么是「筛选」不是「摘要」
--------------------------

摘要让模型**改写**：改写就会引入原文没有的词，于是

- 事实层被污染（闸门拿改写后的文字当页面证据）；
- 实体检索失效（``rationale.lexicon.match_key`` 只抹排版不抹词义，
  改写后的词在原文里查不到 ⇒ 真实实体被判成幻觉）；
- 不可复现（同一份正文两次摘要措辞不同 ⇒ 语料不可比）。

筛选让模型**只报编号**：最终文本由渲染层**从 P1 原文行重建**，
于是模型只可能**漏**（少选一行），不可能**造**（加一个原文没有的词）。
这是本方案全部防幻觉保证的来源——不是靠提示词约束，是靠结构。

⚠️ 提示词里**不许**出现「总结」「概括」「提炼」这类词：它们会把模型
从「选行」推向「写摘要」，而摘要的失效模式是不可验证的。

三段协议（H-41 阶段 2 · 方案 26 §6.3 #11）
----------------------------------------

``refine.v1`` 只有一个维度：**正文行**。阶段 2 起，**元素与链接也由
模型选**——否则「语义渲染」里那 10 / 4 的元素与链接上限就还是**规则硬
顶**，而硬顶就是截断。

三段共用**一个连续编号空间**：

====================  ==========================  ====================
段                    编号                          渲染层怎么核对
====================  ==========================  ====================
正文候选行            ``1 … N``                    逐字 ∈ 原文行集
交互元素              ``N+1 … N+M``                逐字 ∈ 原文 ``(role, name)``
链接                  ``N+M+1 … N+M+L``            逐字 ∈ 原文 ``(text, href)``
====================  ==========================  ====================

⚠️ **编号空间必须连续且互不重叠**——解析层靠**区间**把编号分流到三段。
"""

from __future__ import annotations

from typing import Sequence

#: 提示词版本。**换提示词即升版本**，写进旁挂里——
#: 不升版本的话，同一批语料里两套提示词产出的精炼混在一起，
#: 而报表上分不出来（这与感知层 ``prompt_schema_version`` 同款纪律）。
#:
#: ⚠️ **两个版本轴别混**（H-41 阶段 2）：
#:
#: - **本常量** = *提示词*版本（``refine.v1`` → ``refine.v2``，三段协议）；
#: - :data:`trajectory_pipeline.common.refine_sidecar.SIDECAR_SCHEMA_VERSION`
#:   = *旁挂文件格式*版本（``refine.v2`` → ``refine.v3`` 条目多两个维度 →
#:   ``refine.v4`` 条目的键换成**候选集哈希**，H-41 阶段 3）。
#:
#: 两个轴**各自独立升**——提示词改了不必动文件格式，文件格式改了也不必
#: 动提示词。混成一个数就会出现「升了提示词 ⇒ 老旁挂全废」这种
#: 不该有的连带失效。
REFINE_PROMPT_VERSION = "refine.v2"

#: 系统提示词。**只输出编号**这条规则写死三遍——它是本方案的结构性
#: 前提（输出里混进任何非数字内容，解析阶段就会整条作废）。
#:
#: ⚠️ **这段字符串里不许出现「总结 / 概括 / 提炼 / 摘要」**：
#: 那几个词会把模型从「选行」推向「写摘要」，而摘要的失效模式是
#: **不可验证**的（改写后的正文读起来完全通顺，报表上看不出异常）。
#: 由 ``tests/unit/test_refine.py::Test提示词口径`` 盯着。
REFINE_SYSTEM = """你是网页内容筛选器。你不写正文，只挑。

给你三组候选，每项一个编号：
【正文候选行】——网页正文里的一行。
【交互元素】——页面上可点的控件。
【链接】——页面上的链接。

你的任务只有一个：**挑出对当前任务有用的项，报出它们的编号**。

硬规则：
1. 只输出编号，用逗号分隔，例如：3,7,12。
   不要输出解释、不要改写原文、不要补充原文没有的字、不要加标题。
2. 必须保留与任务直接相关的项：片名、集数、播放入口、简介、页面当前状态。
3. 必须保留「下一步操作目标」所在的那一项——模型马上就要点它或跳过去。
4. 不要保留：广告、推广、推荐流、热搜榜、相关推荐、版权备案、导航、页脚、
   登录注册之外的无关模块。
5. 正文行里标着「规则判为噪声，可推翻」的：如果它其实是正文里的关键内容
   （例如简介里恰好出现了「广告」两个字），**就把它选上**；确实是模板噪声
   才不选。元素与链接同理——标签看着像导航、但其实是这一页的关键入口时，
   照样选。
6. 数量上限见用户消息。宁可少选，也不要选无关项。
7. 一个编号都不选时输出空字符串——不要输出「无」之类的字。
"""


def format_element_candidate(role: str, name: str) -> str:
    """元素候选的一行写法——``[button] 立即播放``。

    角色用方括号（不是尖括号）：尖括号会被读成 HTML 标签，而这里是
    **ARIA / HTML 两种方言混排**的清单，方括号只表示「这一段是角色」。
    """
    return f"[{role}] {name}".rstrip()


def format_link_candidate(text: str, href: str) -> str:
    """链接候选的一行写法——``正片 → https://…``。"""
    return f"{text or '(无文字)'} → {href}"


def build_user_payload(
    candidates: Sequence[str],
    *,
    focus: Sequence[str] = (),
    keep: Sequence[str] = (),
    url: str = "",
    title: str = "",
    max_lines: int = 0,
    noise_flags: Sequence[bool] = (),
    elements: Sequence[tuple[str, str]] = (),
    links: Sequence[tuple[str, str]] = (),
) -> str:
    """候选 + 任务上下文 → 用户消息。

    正文候选行**编号从 1 开始**，与提示词里的「编号」一一对应。
    行内容原样给（不截断）——截了会让模型选到半截句子，
    而半截句子在渲染层仍然按原文重建（编号对得上就行）。

    ⚠️ **三段共用一个连续编号空间**：正文 ``1…N`` → 元素 ``N+1…N+M``
    → 链接 ``N+M+1…N+M+L``。解析层靠区间分流，所以**编号必须连续**
    （跳号会让解析与渲染对不上，而那正是「模型选了第 7 号、渲染层
    拿第 7 号去找别的东西」那类静默错位的来源）。

    ``noise_flags`` 与 ``candidates`` **按下标对齐**：``True`` = 规则判
    这一行是噪声（**可推翻**，见方案 26 §6.3 #12）。
    """
    head = [
        f"当前网址：{url or '（无）'}",
        f"页面标题：{title or '（无）'}",
        f"任务关键词：{'、'.join(x for x in focus if x) or '（无）'}",
        f"下一步操作目标：{'、'.join(x for x in keep if x) or '（无）'}",
    ]
    if max_lines > 0:
        head.append(f"最多保留 {max_lines} 行。")
    else:
        head.append("行数不限。")

    n_body = len(candidates)
    n_elements = len(elements)
    n_links = len(links)

    parts: list[str] = [*head, ""]

    parts.append(
        f"【正文候选行】（编号 1-{n_body}）：" if n_body
        else "【正文候选行】（无）："
    )
    for i, line in enumerate(candidates, 1):
        flag = (
            "（规则判为噪声，可推翻）"
            if i - 1 < len(noise_flags) and noise_flags[i - 1]
            else ""
        )
        parts.append(f"{i}. {line}{flag}")
    if not n_body:
        parts.append("（无）")

    if n_elements:
        parts.append("")
        parts.append(
            f"【交互元素】（编号 {n_body + 1}-{n_body + n_elements}）："
        )
        for i, (role, name) in enumerate(elements, n_body + 1):
            parts.append(f"{i}. {format_element_candidate(role, name)}")
    else:
        parts.append("")
        parts.append("【交互元素】（无）")

    if n_links:
        parts.append("")
        parts.append(
            f"【链接】（编号 {n_body + n_elements + 1}-"
            f"{n_body + n_elements + n_links}）："
        )
        for i, (text, href) in enumerate(links, n_body + n_elements + 1):
            parts.append(f"{i}. {format_link_candidate(text, href)}")
    else:
        parts.append("")
        parts.append("【链接】（无）")

    return "\n".join(parts)


__all__ = [
    "REFINE_PROMPT_VERSION",
    "REFINE_SYSTEM",
    "build_user_payload",
    "format_element_candidate",
    "format_link_candidate",
]
