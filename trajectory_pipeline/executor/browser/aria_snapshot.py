"""``aria_snapshot()`` 文本的纯解析——**playwright 的格式知识，不碰浏览器**。

为什么单列一支
--------------
P-1/F5 的结论是**name 一律取 playwright 的结论**（[方案 04 §1.6 / §4.5.3](../../docs/设计方案/04-camoufox执行层替换方案.md)）：
采集层自己按 ``aria-label || innerText || placeholder …`` 重算，真实页面上只有
87.9%，而且图标按钮会算出**空 name**——空 name 的执行期结局是 ``count == 0``
不报错，与「元素不存在」同形。judged 不到的比对不在，但这条错会静默产出假负样本。

于是采集面只剩一件事：**把 playwright 吐出来的 ``(role, name)`` 读下来**。
读写格式是这里唯一的职责，纯函数、可离线单测。

格式（实测，``playwright 1.62.0``）
----------------------------------
::

    - button "立即播放"
    - button "关闭": X                     ← 冒号后是**行内文本**，不是名字的一部分
    - button "下载":
      - img "下载"                        ← 子节点，缩进两格
    - link:
      - /url: /b                          ← 没有可访问名时**不带引号**
    - checkbox "同意" [checked]            ← 名字后面跟状态，仍然以引号收尾
    - text: 不是可交互                     ← 纯文本节点，role 是 ``text``

四条实测坑，都在这儿处理：

1. **不许锚定行尾**：``- button "搜索结果": 忽略我`` 的行尾是页面文本，
   锚定就把这类节点判成解析失败（F5 探针踩过，整批用例被误分类）。
2. **引号内是 JSON 转义**：``- button "say \\"hi\\""``、反斜杠成对出现。
   用 :func:`json.loads` 还原而不是手工 ``replace``——手工替换对
   ``\\\\"`` 这种组合必然出错。
3. **role 后面可能直接跟冒号**（``- combobox:``）：role 不能把冒号吃进去，
   否则「无名字的 combobox」会被当成一个未知 role 丢掉。
4. **``- /url: /a`` 这类属性行不是节点**：它的「role」是 ``/url``，
   不在词表里自然被过滤——**不需要特判**，但解析时不能崩。

camofox 面的**行尾 ref**（2026-10-26）
------------------------------------
camofox-browser 把可访问性树**注上会话句柄**再吐出来：``- button "立即播放" [e3]``。
那个 ``[e3]`` 是**上游的定位句柄**——会话内有效，**进训练数据等于让模型学随机数**
（与 obscura 的 ``ref`` 同一条红线），所以它**只在本驱动的内部消化**。

⚠️ **ref 必须在「名字之后」那一段里找，不能在整行里找**：
名字本身可能含 ``[e3]``（``- button "标签 [e3]" [e7]``），
整行搜索会取到**名字里的那个**——于是点到了另一个元素，而且**报告上看不出异常**。
上游的注记格式保证 ref **紧跟在名字（或 role）之后**，
所以只在**行尾段**（suffix）里、且**只认第一个** ``[eN]``。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: 节点头：``- <role> "<name>"…``。name 可缺省（无名字），之后的内容一律忽略。
#:
#: role 的字符集**排除冒号**：``- combobox:`` 的 role 是 ``combobox`` 不是
#: ``combobox:``（坑 3）。也排除引号，避免把名字吞进 role。
#:
#: ``suffix`` 是**名字之后**的那一段（camofox 的 ``[e3]`` 就在这里），
#: 单独捕获是为了**只在行尾段里找 ref**——理由见模块 docstring。
_HEAD_RE = re.compile(
    r'^[ \t]*-\s+(?P<role>[^\s":]+)'
    r'(?:\s+"(?P<raw>(?:[^"\\]|\\.)*)")?'
    r'(?P<suffix>.*)$'
)

#: camofox 面的行尾 ref：``[e3]``。
#:
#: ⚠️ **只在上游保证的位置上认**：紧跟在名字（或 role）之后。
#: 上游的注记模板是 ``${role}${nameMatch || ''} [${refId}]${suffix}``，
#: 所以 ref 永远是 suffix 的**第一个** ``[eN]``。名字里出现的 ``[e3]``
#: 在引号内（name 段），不在 suffix 里——本正则只看 suffix，天然躲开。
_REF_RE = re.compile(r"^\s*\[(?P<ref>e\d+)\]")

__all__ = ["AriaNode", "parse_nodes"]


@dataclass(frozen=True, slots=True)
class AriaNode:
    """可访问性树里的一个节点。

    ``name is None`` 的含义是**该节点没有可访问名**（``- link:``），
    不是「名字是空字符串」——这两者在执行期同形（都定位不到），
    但在「页面为什么没有可交互元素」的诊断上不同，故保留 ``None``。

    ``ref`` 是**上游给的会话句柄**（camofox 面的 ``e3``；playwright 面恒为
    ``None``）。它**只在本驱动内部消化**，不进动作流、不进训练数据——
    与 obscura 的 ``ref`` 同一条红线。
    """

    role: str
    name: str | None
    ref: str | None = None


def _unescape(raw: str) -> str:
    """把引号内的 JSON 转义还原。

    还原失败**不抛**：那说明 playwright 换了转义口径，此时原样给出——
    调用方据此仍可能定位成功（多数名字里没有需要转义的字符），
    抛异常会让整页元素列表消失，代价更大。
    """
    try:
        value = json.loads(f'"{raw}"')
    except ValueError:
        return raw
    return value if isinstance(value, str) else raw


def parse_nodes(text: str) -> tuple[AriaNode, ...]:
    """把 ``aria_snapshot()`` 的文本解析成节点序列（**文档序**）。

    只取每行的节点头；缩进层级不保留——采集面要的是平铺的
    ``(role, name)``，树形结构对定位没有用（定位靠 role+name）。
    但**缩进的节点照样要取**：真实页面的可访问性树几乎全是嵌套的
    （``generic`` → ``listitem`` → ``link``），只认顶格会漏掉绝大多数元素。

    **camofox 面的行尾 ref 会被摘出来**（放进 :attr:`AriaNode.ref`），
    摘不到就是 ``None``——那意味着「这个节点点不到」（上游没给它句柄），
    调用方据此把它从动作面里剔掉，而不是拿一个不存在的句柄去点。
    """
    out: list[AriaNode] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = _HEAD_RE.match(line)
        if not m:
            continue                     # 缩进更深的结构行 / 属性行，跳过
        raw = m.group("raw")
        ref_match = _REF_RE.match(m.group("suffix") or "")
        out.append(
            AriaNode(
                role=m.group("role"),
                name=None if raw is None else _unescape(raw),
                ref=ref_match.group("ref") if ref_match else None,
            )
        )
    return tuple(out)