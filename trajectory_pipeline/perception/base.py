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

# 元素方言的**定义**在 ``common/elements.py``（地基，谁都能 import）：
# rationale / assembler / T 都有「不 import 上游」的硬规矩，而读一个存档
# 键名是纯格式的事。本模块只**用**它，且**不转发**——两个导入路径迟早
# 各自漂，要读方言就 import 那个模块。
from trajectory_pipeline.common.elements import ELEMENT_VOCAB_HTML_TAG

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

    ``ref`` 来自驱动层的枚举，形如 ``e1`` / ``c3``，**只在会话内有效**
    （obscura 面导航前有效；camoufox 面的 ``{role,name}`` 更是只有**同一次
    枚举内**有效，见设计方案 04 §4.5.6）。它只活在 P1 与采集面，**不进
    训练动作流**。

    ⚠️ **``role`` / ``name`` 是槽位名，不是值域保证**：值的词表由
    :attr:`Observation.element_vocabulary` 决定（obscura 面的 ``role`` 槽
    装的是 HTML 标签名）。P4 起字段名统一，值域仍逐面不同义——
    见 :mod:`trajectory_pipeline.common.elements` 的模块 docstring。
    """

    ref: str
    role: str
    name: str


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

    #: **采集到的**交互元素 / 链接条数（已被驱动层 ``limit`` 截断）。
    #:
    #: **这两个字段是 D-2 打分的前提**，不是可选元数据。存档序列化时
    #: ``interactive_elements`` 与 ``links`` 各自截到 80 / 50 条，
    #: 而落盘的量不落盘就会让**两种相反的错误在数据上同形**：
    #:
    #:     目标真的存在于第 81 个元素之后   → 查不到
    #:     目标根本不存在（模型幻觉出来的） → 查不到
    #:
    #: 两者产出同一个「查不到」，评分器于是把采集器的取舍
    #: 记成轨迹的缺陷（rubric v1.1 D-2 边界：应判「不可判定（存档裁剪）」）。
    #:
    #: ⚠️ **不是页面上的真实总数**，是驱动层 ``DEFAULT_LIMIT`` 截断后
    #: 实际采到的条数。对 D-2 够用：判据是「total > 保留条数 ⇒ 不可判定」，
    #: 而真实总数只会让这个判据更保守，不会让它出错。
    #: 要真实总数得额外调一次 ``browser_count``——它只认 CSS selector，
    #: 数不出「可交互元素」，为此多一轮 MCP 往返不划算。
    elements_total: int = 0
    links_total: int = 0

    #: 交互元素用的**词汇表**（见 :data:`ELEMENT_VOCAB_HTML_TAG`）。
    #:
    #: 它**不是可选元数据**：按词表筛元素的地方（站内搜索框 / 搜索按钮 /
    #: 播放控件）换了执行层就必须换一套词，而两套词在值上根本对不上
    #: （``input`` vs ``textbox``）。没有这个字段时唯一的活法是「两套并集」，
    #: 那会让**同一份观察里的两个方言值同时命中**——症状是换了执行层之后
    #: 某条路径静默取回 0 个候选，与「这页确实没有」完全同形。
    #:
    #: 由**驱动实现**填（它就是方言本身），不手填、不从别处推导；
    #: 回放时由存档的 ``driver`` 折回来填（:func:`trajectory_pipeline
    #: .perception.replay.observation_of`），缺省 ``html_tag`` —— 历史批次
    #: 真实跑的就是 obscura。
    element_vocabulary: str = ELEMENT_VOCAB_HTML_TAG

    #: **跨面可比的**元素枚举账目（设计方案 04 §4.9.6，2026-10-21）。
    #:
    #: **它是新增的一列，不改 ``elements_total``。** ``elements_total``
    #: 记的是「**本面自己的**集合有多大」，两个面数的是两个不同义的东西
    #: （obscura = 上游工具给的交互元素；camoufox = ARIA 树里过了词表的
    #: 那些）——所以跨面直接比它，差值不可解释。
    #:
    #: 结构 = ``{"shared": {...}, "face": {...}}``——**两本账平级**：
    #:
    #: - ``shared``：**同一段 JS + 同一把尺子**在两个浏览器上各跑一次，
    #:   于是**按构造可比**。它是「同一个页面在两个引擎上差多少」的答案。
    #: - ``face``：本面自己的枚举账目。**键可以缺**（obscura 面的上游工具
    #:   给不出「role 命中几条 / 无名丢掉几条」）——缺就是缺，不填 0。
    #:
    #: ⚠️ ``None`` = **这一批没开 ``--element-basis``**，不是「页面上没有元素」。
    #: 读档的人要把这两种「没有」分开：一个是没测，一个是真没有。
    element_basis: Mapping[str, Any] | None = None

    #: **共享口径没测到的原因**（``None`` = 没开开关，或测到了）。
    #:
    #: ⚠️ **它不进 :attr:`degraded`**——那是**内容块**的降级通道，
    #: 下游只判它非空（``steps/search.extract_candidates`` 见到降级就
    #: **不取候选**、``gatekeeper`` 见到降级就拦）。共享口径是**测量仪器**，
    #: 它坏了只说明**这把尺子没量到**，不说明**这个页面不可信**——
    #: 把测量失败混进 ``degraded``，等于让一把坏尺子**作废整批数据**
    #: （2026-10-21 由 P0 等价性门禁的变异测试挖出）。
    #:
    #: 两种「没有」必须分开读：
    #:
    #: - ``element_basis is None`` 且 ``element_basis_error is None``
    #:   → **没开开关**；
    #: - ``element_basis`` 缺 ``shared`` 键、``element_basis_error`` 非空
    #:   → **开了，但共享口径没测到**（face 账目仍在）。
    element_basis_error: str | None = None

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

    def to_dict(self) -> dict[str, Any]:
        """L3 决策序列化；payload 转为可变 dict 以 JSON 化。

        增量二十六接入：dump_result 通过 ``result.l3_decision.to_dict()`` 落盘。
        其它 Perceptor（LLM/Rule）若沿用 Decision 也可直接复用。
        """
        payload = dict(self.payload) if self.payload else {}
        return {
            "question": self.question,
            "answer": self.answer,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "source": self.source,
            "payload": payload,
            "latency_ms": self.latency_ms,
            "fallback_used": self.fallback_used,
        }


class Q:
    """判断题注册表的属性访问简写。

    纯字符串常量，**不做查找**——查找发生在 ``decide()`` 内部，
    拼错 id 时由注册表 KeyError 暴露，而不是静默取到 None。
    """

    SELECT_PLAY_SITES = "select.play_sites"
    IS_REACHABLE = "site.is_reachable"
    FIND_PLAY_CONTROL = "site.find_play_control"
    PLAYER_OK = "player.ok"
    # ── 路径 B/C 发现链（2026-10-16）────────────────────────────────
    # 六道新题只扩「候选从哪来」，产出 Candidate 之后复用环节② visit 链。
    SELECT_AGGREGATOR_RESULTS = "select.aggregator_results"
    IS_AGGREGATOR = "site.is_aggregator"
    SELECT_SITE_ENTRIES = "select.site_entries"
    SELECT_ARTICLE_RESULTS = "select.article_results"
    SELECT_REFERRAL_LINKS = "select.referral_links"
    FIND_SITE_SEARCH = "site.find_site_search"
    # ── 方案 12 阶段 A1（2026-10-16）────────────────────────────────
    # 搜索页链接分类任务：一道题统一 ①/①B/①C 的「选某类链接」语义，
    # 输出四分类（video_site / aggregator / article_post / noise），
    # 分类决定分发路径。zhihu/douban/weibo 自结构排除表移出后正是
    # 从 article_post 这一维进候选池（路径 C 的扩展）。
    CLASSIFY_SEARCH_LINKS = "classify.search_links"


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
