"""感知层契约：值对象 + 协议 + 判断题标识。

本模块是 ``perception`` 与 ``executor`` 之间**唯一的契约面**。
executor 只 import 本模块（值对象与协议），不 import 任何 Perceptor 实现——
这是 W1 规则版与 W3 LLM 版能无痛替换的唯一保证。

字段设计全部基于 obscura MCP 0.2.4 的**实测返回格式**
（存档 ``output/pipeline/obscura_returns_*.json``），不是按 tool 描述推测：

    browser_snapshot          ``URL: <url>\\nTitle: <title>\\n\\n<body>``
    browser_links             NDJSON，每行 ``{"text":..., "href":...}``；
                              空时哨兵文本 ``No links found.``
    browser_interactive_elements  每行 ``ref=e1    <tag>    "<text>"``（列间多空格分隔，
                              text 含 JSON 转义引号）；空时哨兵 ``No interactive elements on this page.``
    browser_count             **JSON 数字**，如 ``70``（注意会带 ``.0``，实测返回 ``474.0``）
    browser_evaluate          返回表达式的 JSON 值；``document.body.innerText``
                              是**正文的主来源**——snapshot/markdown 的正文会被
                              CSS 抢光预算（实测 baidu 首页 6000 字符预算里
                              正文字数为 0）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

# ═══════════════════════════════════════════════════════════════════════
# 观察层值对象
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class LinkItem:
    """搜索结果页 / 站点页上的一个链接。"""

    text: str
    href: str


@dataclass(frozen=True, slots=True)
class InteractiveElement:
    """可点击 / 可输入元素，带**稳定 ref**。

    ref 来自 ``browser_interactive_elements``，形如 ``e1``，**导航前有效**。
    它是判断点 ③ 回传的内容——代码拿它直接 click，不再自己猜"哪个是播放按钮"。
    """

    ref: str
    tag: str
    label: str


@dataclass(frozen=True, slots=True)
class Observation:
    """DOM 预处理后的结构化观察——perception 层的唯一输入。

    不变式（由 ``executor/dom.py`` 保证）：
      - 可 JSON 序列化（存档与契约测试共用同一份语料）
      - 不含任何页面句柄——perception 无权触网
      - ``body_text`` 已剥离 ``<style>`` / ``<script>``，且默认来自
        ``document.body.innerText`` 而非 snapshot 的 HTML 正文——
        实测 baidu 首页 snapshot 的 6000 字符预算被 16675 字符的样式块
        全部吃光，正文字数为 0（详见 ``executor/dom.py``）
      - ``truncated`` 显式记录截断状态——rationale 引用的实体若落在截断外，
        **不能判成幻觉**（见 rationale/gates.py 与方案 §3.2 变更④）
    """

    url: str
    page_title: str
    body_text: str
    interactive_elements: tuple[InteractiveElement, ...] = ()
    links: tuple[LinkItem, ...] = ()
    video_tag_count: int = 0
    iframe_count: int = 0    # browser_count 接受任意 CSS selector，直接用 "iframe"

    # ── 截断与污染诊断（元数据，不参与语义判断）─────────────────────
    max_chars: int | None = None
    truncated: bool = False
    raw_len: int = 0          # 预处理前的正文长度
    stripped_ratio: float = 1.0   # 剥离 CSS/JS 后的保留比例；<0.5 说明页面脏
    body_source: str = "inner_text"
    #   正文实际来源（"inner_text" | "snapshot"）。这是**可审计字段**：
    #   "snapshot" 意味着正文是被 CSS 抢过预算的残骸，落在它上的
    #   语义判定可信度更低。人工复核 P1 存档时要先看这一列。
    degraded: tuple[str, ...] = ()
    #   本次采集**降级**的项（links / interactive / counts…）。非空表示
    #   至少一项没采到，字段值可能是缺失而非「确实为空」。
    #   **这个字段是 fail-closed 的前提**：没有它，「没采到交互元素」与
    #   「页面上确实没有交互元素」在观察里完全一样，
    #   规则版会把基础设施故障判成「该站没有播放控件」——
    #   故障被写进业务结论，报表上看不出来。
    #
    #   ⚠️ 这里**刻意没有** ``blocked_reason`` 字段。反爬判定
    #   （:func:`executor.dom.detect_block`）由**控制流**在使用点直接做，
    #   不在采集层落字段：早一个版本把判定放进 driver 里，于是
    #   ``FakeDriver`` 构造的观察带不上该字段，"被拦"与"没拦"
    #   在测试与真实路径上行为不一致。**判定逻辑与使用点必须同处一处。**

    @property
    def pollute_ratio(self) -> float:
        """被剥离掉的比例（0~1）。用于评估该站点的 DOM 脏度。"""
        return max(0.0, min(1.0, 1.0 - self.stripped_ratio))


# ═══════════════════════════════════════════════════════════════════════
# 判定层值对象
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Question:
    """一类判断的定义与阈值。

    ``negative_branch`` 是关键字段：它把「判断题」与「负样本池」显式绑定，
    每个 Question 都必须回答「我判 false 时，这条样本归到哪个失败分支」。
    从类型层面堵死「失败分支静默消失」。
    """

    id: str
    prompt: str
    answer_type: Literal["bool", "decision"]   # decision = 除布尔外还回传 payload
    schema_hint: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    confidence_threshold: float = 0.7
    evidence_required: bool = True
    negative_branch: str | None = None
    needs_llm: bool = True          # False = 该题 W1 即由规则确定性判定
    prompt_schema_version: str = "v1"


@dataclass(frozen=True, slots=True)
class Decision:
    """一次判定的结果。

    ``payload`` 承载 ``answer_type="decision"`` 题的结构化回传
    （① 的 selected/rejected、③ 的 ref/trailer_only），代码直接消费，
    不经二次解析。
    """

    question: str
    answer: bool | None            # None = unresolved（I4 fail-closed）
    confidence: float
    evidence: str
    source: Literal["rule", "llm", "model"]
    payload: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    latency_ms: int = 0
    fallback_used: bool = False    # 本次判定是否走了降级路径

    @property
    def is_unresolved(self) -> bool:
        return self.answer is None


class Q:
    """判断题注册表的属性访问简写。

    纯字符串常量，**不做查找**——查找发生在 ``decide()`` 内部，
    拼错 id 时由注册表 KeyError 暴露，而不是静默取到 None。
    """

    SELECT_PLAY_SITES = "select.play_sites"
    IS_REACHABLE = "site.is_reachable"
    FIND_PLAY_CONTROL = "site.find_play_control"
    PLAYER_OK = "player.ok"


# ═══════════════════════════════════════════════════════════════════════
# 协议
# ═══════════════════════════════════════════════════════════════════════


@runtime_checkable
class Perceptor(Protocol):
    """语义感知实现。W1 = RulePerceptor，W3 起 = LLMPerceptor。

    五不变式由契约测试强制（见方案 01 号文档 §3）：
        I1 evidence 可溯源 / I2 confidence∈[0,1] / I3 幂等 / I4 fail-closed
        I5 无副作用
        I6 decision 型题 payload 完整且 ref 可溯源到 obs.interactive_elements
        I7 answer=True 时 payload 不得为空
    """

    name: str

    def decide(self, question: str, obs: Observation) -> Decision:
        """判定 ``question``。

        契约：
          - 只读 ``obs``，不触网、不持页面句柄（I5）
          - 同一 ``(question, obs)`` 重复调用返回相同 answer（I3）
          - 能力不可用时返回 ``answer=None``，**绝不猜**（I4）
          - 从不抛异常到调用方——失败一律转成 ``answer=None``
        """
        ...


class PerceptionHealth:
    """实现自述的健康状态，供 ``--check-tools`` 类命令汇总。"""

    def __init__(self, name: str, ready: bool, reason: str = "") -> None:
        self.name = name
        self.ready = ready
        self.reason = reason

    def __bool__(self) -> bool:
        return self.ready