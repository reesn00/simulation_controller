"""元素方言（dialect）——**槽位名固定，值域随执行层变**。

这一层只做一件纯格式的事：把「一个交互元素的 kind 槽与 text 槽」读出来、
按**给定词汇表**写成存档形状。它不含任何业务判断，所以落在地基
``common``：能被任何模块 import，且**谁都不必为了读两个键去 import 上游**。

为什么不放在 ``perception.base``（那里的 :class:`InteractiveElement`
值对象仍从这里取定义）
--------------------------------------------------------------
``rationale`` 与 ``assembler`` 都有「不 import 上游」的硬规矩
（``tests/unit/test_writer.py::TestNoUpstreamImport``、T 的模块图契约）：
交接面是**格式**不是对象。方言键名正是格式——半年前那批存档写
``tag``/``label``，新批次写 ``role``/``name``，读档的人必须两套都读得出来，
而这件事跟「感知层版本」无关。放进 ``perception`` 会把「读一个存档键」
变成对上游的依赖，于是每次执行层改版都要重建 rationale。

词汇表
------
- ``html_tag``：HTML 标签名（``input`` / ``button`` / ``a``）。obscura 面；
- ``aria_role``：ARIA role（``textbox`` / ``button`` / ``link``）。aria 面
  （camoufox / camofox，D12）。

**不可混池**：同一条 ``"button"`` 在两面分别指「标签叫 button」与
「ARIA role 是 button」，而 ``"input"`` / ``"textbox"`` 在两个词表里
根本对不上。下游凡是要按词表筛元素的地方，都必须先拿到词汇表
（``Observation.element_vocabulary`` 或存档自己的键名，
:func:`vocabulary_of_archive_elements`），**各取一套词**——把两套并成
一个集合会让「换了执行层」与「这页真没有」在观察里同形，那是最贵的
一类静默退化。

逐面的键名投影在 :data:`trajectory_pipeline.toolcatalog.DIALECTS`
（那边是方言的唯一来源，由 P1 的 ``driver`` 一对一折算）；
本模块是它的读取侧，两处由契约测试对齐。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

ELEMENT_VOCAB_HTML_TAG = "html_tag"
ELEMENT_VOCAB_ARIA_ROLE = "aria_role"

#: 词汇表的全集中。**认不出的词汇表直接抛**，不回落（回落 = 猜）。
ELEMENT_VOCABULARIES: frozenset[str] = frozenset({
    ELEMENT_VOCAB_HTML_TAG, ELEMENT_VOCAB_ARIA_ROLE,
})

#: 词汇表 → 该词表下元素的**存档键名**（``(kind, text)`` 顺序）。
#:
#: 与 :data:`ELEMENT_VOCABULARIES` 同一个轴的两个投影：键名跟着词表走，
#: 因为「键叫 ``tag``、值是 ARIA role」这种组合会让读档人无从判断该按哪套
#: 词表解释它。
ELEMENT_KEYS: Mapping[str, tuple[str, str]] = MappingProxyType({
    ELEMENT_VOCAB_HTML_TAG: ("tag", "label"),
    ELEMENT_VOCAB_ARIA_ROLE: ("role", "name"),
})


def element_role(element: Any) -> str:
    """元素的 **kind 槽**，无论它是值对象还是**存档里的字典**。

    这是「方言读取」的**唯一一处**：新存档写 ``role``/``name``、
    历史存档写 ``tag``/``label``，两边都得读得出来。分散在各处写
    ``e.get("tag") or e.get("role")`` 就等于把方言知识抄 N 份，
    而其中一份迟早漏掉一半（症状：某些样本的元素清单恒空）。
    """
    return _first_present(element, "role", "tag")


def element_name(element: Any) -> str:
    """元素的 **text 槽**（可访问名 / 可见文字），方言读取同 :func:`element_role`。"""
    return _first_present(element, "name", "label")


def _first_present(element: Any, *keys: str) -> str:
    for key in keys:
        if isinstance(element, Mapping):
            value = element.get(key)
        else:
            value = getattr(element, key, None)
        if value is not None:
            return str(value)
    return ""


def element_json(
    element: Any, vocabulary: str, *, name_limit: int | None = None
) -> dict[str, str]:
    """元素 → **该词汇表下的存档形状**。

    键名跟着词表走（:data:`ELEMENT_KEYS`），值原样搬——**不翻译值**。
    ``"input"`` 不会变成 ``"textbox"``：那需要猜 ``<input>`` 的 type，
    猜错就是「声明与真实调用对不上」，而报告上看不出异常。

    ``name_limit``：P1 落盘时 text 槽裁到 120 字符（截断丢的是人工排障
    要用的原文，所以只裁存档、不裁内存里的那份）。
    """
    kind_key, text_key = element_keys(vocabulary)
    text = element_name(element)
    if name_limit is not None:
        text = text[:name_limit]
    return {
        "ref": _ref_of(element),
        kind_key: element_role(element),
        text_key: text,
    }


def vocabulary_of_archive_elements(elements: Any) -> str:
    """**存档里的**元素清单 → 它的词汇表。

    只看键名（``role`` ⇒ ``aria_role``，否则 ``html_tag``）。这不是
    第二处推断源：键名本来就是同一个轴（存档的 ``driver``）写下的，
    这里是把写下去的那个事实读回来——读档的渲染层拿不到 ``driver`` 时
    只有这条路，而它有据可依，不是猜。

    空清单返回 ``html_tag``：没有条目就无从选键名，也不影响任何输出
    （没有东西可渲染）。
    """
    for element in elements or ():
        if isinstance(element, Mapping):
            if "role" in element and "tag" not in element:
                return ELEMENT_VOCAB_ARIA_ROLE
            if "tag" in element:
                return ELEMENT_VOCAB_HTML_TAG
    return ELEMENT_VOCAB_HTML_TAG


def element_keys(vocabulary: str) -> tuple[str, str]:
    """词汇表 → 存档键名 ``(kind, text)``。认不出**抛**，不猜。"""
    try:
        return ELEMENT_KEYS[vocabulary]
    except KeyError:
        raise ValueError(
            f"认不出的元素词汇表 {vocabulary!r}——已知的：{sorted(ELEMENT_KEYS)}"
        ) from None


def _ref_of(element: Any) -> str:
    if isinstance(element, Mapping):
        return str(element.get("ref") or "")
    return str(getattr(element, "ref", "") or "")


__all__ = [
    "ELEMENT_KEYS",
    "ELEMENT_VOCABULARIES",
    "ELEMENT_VOCAB_ARIA_ROLE",
    "ELEMENT_VOCAB_HTML_TAG",
    "element_json",
    "element_keys",
    "element_name",
    "element_role",
    "vocabulary_of_archive_elements",
]