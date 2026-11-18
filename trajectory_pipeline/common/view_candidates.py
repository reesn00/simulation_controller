"""元素 / 链接候选集——**唯一真源**（H-41 阶段 2）。

两处要用**同一份**候选集：

- :mod:`trajectory_pipeline.refine`——摆给 LLM 看的候选列表；
- :mod:`trajectory_pipeline.assembler.observation_view`——渲染层按
  模型选中的项**逐字核对重建**。

两份实现漂了**不报错**：模型选的是「第 7 号元素」，渲染层按**另一份**
候选表去找「第 7 号」⇒ 找出来的是别的东西 ⇒ 训练样本里出现
「点了观察里没有的东西」，而**全程没有任何异常**。

⇒ 候选集必须**同一份构造**：两边只 import 这里，谁都不许自己写一遍。

⚠️ **keep（下一步动作的目标）命中的项无条件保留**——不受导航词过滤。
这与渲染层规则路径**同款**：训练样本里不能出现「点了一个观察里没有的
东西」（方案 23 R1-2）。

⚠️ 本模块**只放纯函数**：不读文件、不发请求、不改状态（与
:mod:`trajectory_pipeline.common.body_lines` 同一纪律）。
"""

from __future__ import annotations

from typing import Any, Mapping, NamedTuple, Sequence

from trajectory_pipeline.common.body_lines import NAV_LINE_WORDS, NOISE_LINE_RE
from trajectory_pipeline.common.elements import (
    element_keys,
    vocabulary_of_archive_elements,
)


class ElementCandidate(NamedTuple):
    """一个**元素候选**。

    ``role`` / ``name`` 是**逐字**取自存档的元素身份；``index`` 是它在
    原始清单里的下标（**页面顺序**用它）；``element`` 是原始字典本身
    （渲染层重建时**原样搬**，不重新拼）。
    """

    role: str
    name: str
    index: int
    element: Mapping[str, Any]


class LinkCandidate(NamedTuple):
    """一个**链接候选**（``text`` / ``href`` / 原始下标 / 原始字典）。"""

    text: str
    href: str
    index: int
    link: Mapping[str, Any]


def read_blocks(read: str) -> tuple[bool, bool, bool]:
    """读标记 → **（正文 / 元素 / 链接）这三块这次渲染吗**。

    **唯一真源**（H-41 阶段 2）：精炼侧（摆给模型看的候选）与渲染侧
    （最终输出）必须对「这一条观察有哪几块」**完全一致**——否则模型
    选了渲染层压根不渲染的东西 ⇒ 逐字核对不过 ⇒ 整条回退规则裁剪
    （白跑一次调用，还多一条「模型不听话」的假账）。

    ⚠️ 读步只渲染**被读的那一块**（方案 23 R4-1）：``snapshot`` 只留正文、
    ``interactive`` 只留元素表、``links`` 只留链接表。
    """
    r = str(read or "")
    return (
        r in ("", "snapshot"),
        r in ("", "interactive"),
        r in ("", "links"),
    )


def element_matches_keep(name: str, keep: Sequence[str]) -> bool:
    """这个元素**是不是下一步动作的目标**。

    判据与渲染层规则路径**逐字同款**：目标名**等于**或**含在**元素名里
    （子串方向是「目标 ⊂ 元素名」——目标名短、元素名长）。
    """
    return any(k and (k == name or k in name) for k in keep)


def link_matches_keep(href: str, keep: Sequence[str]) -> bool:
    """这个链接**是不是下一步动作的目标**（判据同上，方向同）。"""
    return any(k and (k == href or k in href) for k in keep)


def element_candidates(
    elements: Sequence[Mapping[str, Any]],
    *,
    keep: Sequence[str] = (),
    vocabulary: str | None = None,
    filter_noise: bool = True,
) -> list[ElementCandidate]:
    """元素候选集——**页面顺序**，已剔噪去重。

    剔掉的：空名字 · 导航词 · 名字长度 ≤ 1 · 重复的 ``(role, name)``。

    ⚠️ **keep 命中的元素不参与剔噪**（名字在导航词里也留下）——它就是
    下一步要点的东西，丢了训练样本就没法自洽。

    ⚠️ **方言**跟着存档自己的键名走（``tag``/``label`` vs ``role``/``name``）
    ——与渲染层**同款**取法（:func:`trajectory_pipeline.common.elements.
    vocabulary_of_archive_elements`）。

    ⚠️ ``filter_noise=False`` = **不剔导航词**（H-41 阶段 2 的语义模式）：
    语义模式下「哪些控件与本任务有关」是**语义问题**，规则层不该替模型
    决定 ⇒ 候选清单里连导航项都列出来，**由模型选**。

    ⚠️ **两种模式下候选集必须逐字一致**（精炼侧与渲染侧调用的是**同一个
    函数**）——否则模型报「第 7 号」而渲染层找的是别的东西。
    """
    kind_key, text_key = element_keys(
        vocabulary or vocabulary_of_archive_elements(list(elements))
    )
    out: list[ElementCandidate] = []
    seen: set[tuple[str, str]] = set()
    for index, element in enumerate(elements or ()):
        if not isinstance(element, Mapping):
            continue
        role = str(element.get(kind_key) or "")
        name = str(element.get(text_key) or "").strip()
        if not name:
            continue
        key = (role, name)
        if key in seen:
            continue
        if len(name) <= 1 and not element_matches_keep(name, keep):
            # 「一个字符的可访问名」多半是图标 / 占位，**不是名字**——
            # 这不是语义判断，剔掉它与「谁重要」无关。
            # ⚠️ **keep 命中的不剔**：它就是下一步要点的东西。
            continue
        if filter_noise and not element_matches_keep(name, keep):
            if name in NAV_LINE_WORDS:
                continue
        seen.add(key)
        out.append(ElementCandidate(role, name, index, element))
    return out


def link_candidates(
    links: Sequence[Mapping[str, Any]],
    *,
    keep: Sequence[str] = (),
    filter_noise: bool = True,
) -> list[LinkCandidate]:
    """链接候选集——**页面顺序**，已剔噪去重。

    剔掉的：空 href · ``javascript:`` · ``#`` · 导航词 / 广告类文本 ·
    重复的 ``(text, href)``。

    ⚠️ **keep 命中的链接不参与文本剔噪**（与元素同款纪律）。

    ⚠️ ``filter_noise=False`` = 语义模式（同 :func:`element_candidates`）。
    """
    out: list[LinkCandidate] = []
    seen: set[tuple[str, str]] = set()
    for index, link in enumerate(links or ()):
        if not isinstance(link, Mapping):
            continue
        href = str(link.get("href") or "").strip()
        text = str(link.get("text") or "").strip()
        if not href or href.startswith("javascript:") or href == "#":
            continue
        key = (text, href)
        if key in seen:
            continue
        if filter_noise and not link_matches_keep(href, keep):
            if text in NAV_LINE_WORDS or NOISE_LINE_RE.search(text):
                continue
        seen.add(key)
        out.append(LinkCandidate(text, href, index, link))
    return out


__all__ = [
    "ElementCandidate",
    "LinkCandidate",
    "element_candidates",
    "element_matches_keep",
    "link_candidates",
    "link_matches_keep",
    "read_blocks",
]
