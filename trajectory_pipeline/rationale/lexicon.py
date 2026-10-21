"""rationale 的词法层——**实体抽取 + 套话 + 后见之明**。

模块 4 的一致性闸门三查（见 :mod:`trajectory_pipeline.rationale.gates`）
全都建在这一层之上。它是**纯代码、零 LLM 调用**，所以
「实体核查 100% 覆盖」这条承诺靠的是这里而不是模型。

三条设计决定
============

1. **不用中文分词。**
   中文没有词边界，切词工具换一版就换一套结果，而闸门要跨批次可比。
   同 ``taskgen/persona/renderer`` 的 ``VERBAL_TAILS`` 契约——
   「这条判据**不用中文分词**（太脆）」。所以实体只抽**两类高置信形态**：
   URL 与成对引号片段。抽不出的就不抽，**宁可漏不可滥**：
   漏判的代价是放过一条幻觉，滥判的代价是把正确 rationale 判成幻觉，
   后者会污染负样本池。

2. **任务给的实体是豁免域，不是幻觉。**
   rationale 提到片名「功夫」而观察里没有它，**不是幻觉**——
   片名来自任务（:attr:`Sample.title` / ``user_prompt``），不是模型看来的。
   把任务给的词当成「模型引入的实体」去核，会把**每一条**提到片名的
   rationale 都判成幻觉。这一条是本模块最容易犯的错。

3. **套话先过滤，再核实体。**
   「无法确认」「需要进一步核实」这类短语也带引号、也不在观察里，
   按实体去核就是**假阳性**。所以抽取结果要先过
   :func:`is_boilerplate`，剩下的才交给闸门。两层的分工写在下面。

为什么要「套话」这一层
----------------------

rubric D-9 锚点 2 是「系统性不符或**套话替代推理**」。套话不是幻觉
（它没编造事实），但它同样没有信息——一条通篇「页面显示播放按钮」的
thinking 与一条真读了观察的 thinking，在训练信号上等价于没有。

而套话**必须**被识别出来而不是让模型自觉：套话是**训练数据里最常见的
LLM 输出形态**，模型会朝着有 rationale 的样本学，所以 rationale 里
越多套话，模型越会产出套话。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal, Mapping

from trajectory_pipeline.common.elements import (
    element_name,
    element_role,
)

#: 实体种类。**只有两类**——见模块 docstring 第 1 条。
EntityKind = Literal["url", "quoted"]


@dataclass(frozen=True, slots=True)
class Entity:
    """一个从 rationale 文本里抽出的实体。

    ``text`` 是**归一化后**的（去首尾空白、去成对引号本身），
    这样闸门拿它去观察里检索时不必再处理引号。
    """

    kind: EntityKind
    text: str

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "text": self.text}


# ── URL ───────────────────────────────────────────────────────────

#: URL。终止符里刻意包含中文标点——中文文本里 URL 后面常常紧跟「，」
#: 或「。」，而 ``[^\s]+`` 会把后半个句子一起吞进去，于是「这个 URL
#: 不在观察里」永远成立，闸门会把每一条含 URL 的 rationale 判成幻觉。
_URL_RE = re.compile(r"https?://[^\s，。；、！？）】」』《》【】]+")

#: 裸域名（``qq.com`` / ``www.iqiyi.com``）——真实文本里很常见，
#: 而它**未必**带 scheme。只认「含至少一个点 + 顶级域形状」，
#: 否则「3.5 秒」「第 2 集」这类会被当域名。
_BARE_HOST_RE = re.compile(
    r"(?<![\w.@/-])"
    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:com|cn|net|org|tv|cc|me|io|co|app|info|xyz|top|vip|wang)"
    r"(?![\w-])",
    re.IGNORECASE,
)

# ── 成对引号片段 ──────────────────────────────────────────────────

#: 成对引号。**刻意不含 ASCII 双引号**——obscura 的 label 格式是
#: ``"文本" 属性="值"``（见 ``executor/dom.py``），label 里本来就带
#: ASCII 引号，把它当引号对会切出 ``属性=值`` 这种毫无意义的片段。
#: 中文文本的引号对足够覆盖真实场景。
#:
#: **下界是 2 不是 1**：单个汉字不是有意义的实体核查单位——「有」「的」
#: 这种字几乎不可能出现在观察里，判它「不在观察里」就是纯假阳性。
#: 上界 80 是防「整段正文被一对引号包起来」的畸形输入。
_QUOTED_RES = (
    re.compile(r"「([^「」]{2,80})」"),
    re.compile(r"『([^『』]{2,80})』"),
    re.compile(r"《([^《》]{2,80})》"),
    re.compile(r"【([^【】]{2,80})】"),
)

#: 图标字体的私有区字符。obscura 的 ``browser_interactive_elements`` 的
#: label 里混着这些（实测 ``U+E000–U+F8FF``），而它们**主动误导**模型去
#: 找这个实体。抽取前先剥掉——否则「这个实体在观察里检索不到」判的是
#: 一个谁都复制不出来的字符。
_PUA_RE = re.compile(r"[-]")
_LONE_PUNCT_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)


def strip_noise(text: str) -> str:
    """剥掉 PUA 区字符与孤立标点，供实体文本归一化。"""
    return _LONE_PUNCT_RE.sub("", _PUA_RE.sub("", text or "")).strip()


def extract_entities(text: str) -> tuple[Entity, ...]:
    """从 rationale 文本里抽实体。**保序、去重。**

    抽不到就返回空元组——**不猜、不放宽**。闸门拿空结果去核对，
    结论是「这一条没有可核实体」而不是「这一条没有幻觉」。
    """
    if not text:
        return ()
    out: list[Entity] = []
    seen: set[tuple[str, str]] = set()

    def _push(kind: EntityKind, raw: str) -> None:
        norm = strip_noise(raw)
        if not norm or len(norm) < 2:
            return
        key = (kind, norm)
        if key in seen:
            return
        seen.add(key)
        out.append(Entity(kind, norm))

    for m in _URL_RE.finditer(text):
        _push("url", m.group(0))
    for m in _BARE_HOST_RE.finditer(text):
        _push("url", m.group(0))
    for rx in _QUOTED_RES:
        for m in rx.finditer(text):
            _push("quoted", m.group(1))
    return tuple(out)


# ── 套话 ──────────────────────────────────────────────────────────

#: **套话**：不编造事实、但也没有信息量的表述。
#:
#: ⚠️ 这张表**只收「在任何观察下都检索不到、且不携带判断」**的短语。
#: 一条具体判断（「正文写着『当前设备不支持播放』」）即便含「无法确认」
#: 也不是套话——所以判定是**整条匹配**而不是子串包含。
#:
#: 每一条都必须能回答「它出现在一个真读了观察的 rationale 里合理吗」。
BOILERPLATE: tuple[str, ...] = (
    # 「我不能判断」类——把不确定性当结论写
    "无法确认", "无法判断", "不能确定", "不确定", "有待确认", "尚不确定",
    "需要进一步核实", "需要进一步观察", "需要更多证据", "需要更多信息",
    # 「我要去看」类——把意图当事实写
    "让我看看", "接下来我要", "我需要去", "我打算", "我准备",
    # 「按规则」类——把推理过程写成流程复述
    "根据规则", "按照规则", "根据上述", "如上所述", "根据观察可以",
    # 空转的评价类
    "情况比较复杂", "需要谨慎判断", "有待进一步观察",
)

#: 套话的**宽松后缀**：某些短语总是接在套话后面（「无法确认是否可用」
#: 的「是否可用」），单独抽出来判没有意义。
_BOILERPLATE_TAIL = re.compile(r"^是否.*$|^能不能.*$|^可不可以.*$")


def is_boilerplate(text: str) -> bool:
    """这段文本是套话吗。

    **整条匹配**，不做子串包含——见 :data:`BOILERPLATE` 的警告。
    宽松后缀单独放行：它从不独立出现，跟着套话走时不该被当成实体核。
    """
    norm = strip_noise(text)
    if not norm:
        return True                       # 空文本没有信息量
    if _BOILERPLATE_TAIL.match(norm):
        return True
    return norm in BOILERPLATE


def filter_entities(entities: Iterable[Entity]) -> tuple[Entity, ...]:
    """滤掉套话实体。**闸门核的是这个结果**，不是原始抽取结果。"""
    return tuple(e for e in entities if not is_boilerplate(e.text))


# ── 合成实体 ──────────────────────────────────────────────────────

#: **并列分隔符**。模型把观察里两处出现的内容**合并**成一个引号串时
#: 用的连接符：观察里有「越剧特别版」和「越剧特别季」两处，模型写成
#: 「越剧特别版/越剧特别季」。见 :func:`split_composite`。
#:
#: 只收**并列**分隔符（斜杠 / 竖线 / 顿号），**不收逗号与分号**——
#: 那是句内停顿，「发生了错误，请稍后再试」不是两个并列实体。
COMPOSITE_SEP_RE = re.compile(r"\s*[/／|｜、]\s*")

#: 拆分后当成独立成分去核的**最短长度**。低于它的是一个字/一个标点，
#: 拿去 haystack 里做子串匹配几乎必然命中，等于把核查放空。
_MIN_PART_CHARS = 2


def split_composite(text: str) -> tuple[str, ...]:
    """合成实体的成分拆分。**拆不出就返回空元组**，调用方退回整串匹配。

    INC46 实测出来的形态：B 站播放页观察里 ``page_title`` 与正文各出现
    一次「越剧特别版」「越剧特别季」，模型合并写成
    ``《中国好声音——越剧特别版/越剧特别季》``。整串子串匹配**必然落空**
    ——观察里没有那个斜杠串——于是判幻觉。而真相是模型引用的每个成分
    都真实存在，只是写法合并了。

    为什么是「拆开逐段核」而不是「放宽成模糊匹配」：模糊匹配无法回答
    「这一段到底看没看到」，而逐段核的判据是**每段都必须命中**——
    少一段命中就仍是幻觉，收紧的方向没变。
    """
    parts = tuple(p.strip() for p in COMPOSITE_SEP_RE.split(text or ""))
    parts = tuple(p for p in parts if len(strip_noise(p)) >= _MIN_PART_CHARS)
    return parts if len(parts) >= 2 else ()


# ── 后见之明 ──────────────────────────────────────────────────────

#: **后见之明词典**：只在结果已知之后才成立的表述。
#:
#: ⚠️ 这是一层**辅助信号**，最强的后见之明判据是**时序核查**
#: （rationale 引用了只在后续步才出现的实体，见
#: :mod:`trajectory_pipeline.rationale.gates`），那个是纯代码、可确证。
#: 词典判的是「措辞上就已经是事后口吻」，两者互补而不互相替代。
#:
#: 每条都刻意收得**窄**：「其实」这类高频词不在表里——
#: 中文里「其实」大量用于句中转折（「其实这个站可以看」），
#: 把它判成后见之明会误伤一大片正常表达。
HINDSIGHT: tuple[str, ...] = (
    "不出所料", "果然如我所料", "果然不出所料",
    "早就知道", "早该想到", "早知道", "事后看来", "回头看",
    "现在终于明白", "这时候才意识到", "难怪后来",
)


def hindsight_spans(text: str) -> tuple[str, ...]:
    """文本里的后见之明表述。**按在文本中出现的位置排序**，去重。

    按位置而非按 :data:`HINDSIGHT` 的字典序——读 findings 的人是顺着
    thinking 读的，返回「早知道、回头看」而原文是「回头看，早知道」
    会让他去原文里找错位置。
    """
    if not text:
        return ()
    norm = strip_noise(text)
    hits = [(norm.find(h), h) for h in HINDSIGHT if h in norm]
    return tuple(dict.fromkeys(h for _, h in sorted(hits)))


# ── 语气：G-5 的前置 ──────────────────────────────────────────────

#: **确定语气**标记。G-5：「允许思考表达不确定；**确定语气陈述未观察事实
#: = 幻觉**」。所以这句式检查是实体核查的放大器——
#: 同一段话，「未见 video 标签」合格，「页面有 video 标签」是幻觉。
CERTAIN_MARKERS: tuple[str, ...] = (
    "页面有", "页面上有", "已经看到", "看到了", "确认有", "确实有",
    "可以播放", "能播放", "可以看", "成功播放", "正在播放",
)

#: **不确定语气**标记。与 :data:`CERTAIN_MARKERS` 同等重要——
#: 有它的句子即便引用了观察外的实体也**不判幻觉**（G-5 原文）。
UNCERTAIN_MARKERS: tuple[str, ...] = (
    "无法确认", "无法判断", "不确定", "可能", "似乎", "疑似", "待验证",
    "需要进一步", "没有观察到", "未见", "未发现", "看不到", "看不出",
)


def has_certain_tone(text: str) -> bool:
    """这段话里有确定语气吗。"""
    norm = strip_noise(text)
    return any(m in norm for m in CERTAIN_MARKERS)


def has_uncertain_tone(text: str) -> bool:
    """这段话里有不确定语气吗。**G-5 的合格线**。"""
    norm = strip_noise(text)
    return any(m in norm for m in UNCERTAIN_MARKERS)


def resettable_text(text: str) -> str:
    """把 ``Observation`` 的自由文本拼成**可检索袋**。

    闸门拿 rationale 实体去这里检索。**不是一个自由文本搜索**——
    是把 ``url`` / ``page_title`` / ``body_text`` / 元素 label / 链接
    文本与 href 全部并进去。少并一项就多一条假幻觉（实测元素 label
    里的播放按钮名是最常被引用的实体）。
    """
    if isinstance(text, Mapping):
        parts = [
            str(text.get("url") or ""),
            str(text.get("page_title") or ""),
            str(text.get("body_text") or ""),
        ]
        for el in text.get("interactive_elements") or ():
            # 两槽都并进去，与键名无关：漏并一槽就多一条假幻觉
            # （实测元素里的播放按钮名是最常被引用的实体），而键名
            # 跟着方言走（P4）——写死 ``tag``/``label`` 会让 camoufox
            # 批次的元素名整批查不到，实体全判成幻觉。
            parts.append(element_role(el))
            parts.append(element_name(el))
        for lk in text.get("links") or ():
            parts.append(str((lk or {}).get("text") or ""))
            parts.append(str((lk or {}).get("href") or ""))
    else:
        parts = [str(text or "")]
    return strip_noise("\n".join(p for p in parts if p))


__all__ = [
    "EntityKind", "Entity",
    "strip_noise", "extract_entities", "filter_entities",
    "COMPOSITE_SEP_RE", "split_composite",
    "BOILERPLATE", "is_boilerplate",
    "HINDSIGHT", "hindsight_spans",
    "CERTAIN_MARKERS", "UNCERTAIN_MARKERS",
    "has_certain_tone", "has_uncertain_tone",
    "resettable_text",
]
