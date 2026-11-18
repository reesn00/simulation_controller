"""正文精炼——**让模型做选择，不让它写摘要**。

这一层的全部契约
----------------
1. **只可能漏，不可能造。** 模型只报**行号**；最终文本由渲染层从
   P1 原文行重建（:mod:`trajectory_pipeline.assembler.observation_view`
   的 ``refined`` 分支）。模型加不了一个原文没有的词。
2. **失败即回退，不猜。** 解析不出编号 / 行号越界 / 动作目标没保住 /
   任务词命中行全丢 ⇒ 整条精炼作废，渲染层走原路径（规则裁剪）。
   **绝不「差不多地」用一半结果**——半份精炼比不精炼更难查。
3. **不选就没有。** 候选行本来就在预算内 ⇒ 不发请求，记 ``not_needed``。
   成本只花在真会被裁的地方。
4. **不动事实层。** 本模块只产出**旁挂数据**（:mod:`trajectory_pipeline.
   common.refine_sidecar`），P1 一个字不改。

降级链（每一级都留痕）
----------------------
``not_needed``（规则层够用）→ ``llm``（成功）→ ``failed``（原因见
``error``）。三者在旁挂里分得开，报表上不会出现「精炼了但不知道是哪
几条」的糊涂账。

另有一条**只针对一类失败**的重试：输出预算用尽（``budget:`` 开头）
⇒ **换大预算重发一次**。理由见 :data:`BUDGET_RETRY_FACTOR`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from trajectory_pipeline.common import body_lines
from trajectory_pipeline.llm.client import DEFAULT_MAX_TOKENS, MAX_OUTPUT_TOKENS
from trajectory_pipeline.refine.prompt import (
    REFINE_PROMPT_VERSION,
    REFINE_SYSTEM,
    build_user_payload,
)

#: 模型输出里允许出现的字符：**只有数字与分隔符**。
#:
#: 严格到这份程度是被迫的：一旦输出里混进「共 15 行」「好的」这类
#: 文字，就分不清「15」是行号还是行数——而选错一行是**静默**的
#: （渲染层照样重建出一份合法正文，只是内容不对）。宁可整条作废。
_ALLOWED_RE = re.compile(r"^[\d\s,，、;；]+$")

#: 模型偶尔给输出套代码块围栏。剥掉再判——围栏是包装，不是内容。
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

#: 候选行送给模型时的**字符预算**（H-41）。超了先按「相关度」裁。
#:
#: ⚠️ **这里原来写的是「最多 200 行」——口径错了**（行数不映射 token）：
#: 200 行可以是 400 字，也可以是 20 万字；而「输入太长」这件事的
#: 唯一真实单位是**字符 / token**。更要命的是它**静默丢行**：
#: 第 201 行之后的候选行模型**根本没看到**，却仍然算进 ``dropped``
#: ⇒ 账上写着「模型丢的」，实际是「我们自己没给它看」——两种完全不同的
#: 结论，在同一个数字里分不开。
#:
#: 现在改成字符预算，并且**分两笔账**（``candidates`` 与 ``candidates_shown``）：
#: 「一共有多少候选」与「实际给了模型多少」是**两个事实**。
#:
#: 为什么是 10 万字符：输入窗口 20 万 token，中文约 1～1.5 字符/token，
#: 候选占一半、另一半留给系统提示与余量 ⇒ 不会顶到窗口。
MAX_CANDIDATE_CHARS = 100_000

#: **元素 / 链接**候选的字符预算（两段合计，各算各的）。
#:
#: 为什么也要有预算：输入窗口是 **200k token**（owner 授权），而正文
#: 候选已经吃掉 :data:`MAX_CANDIDATE_CHARS`（10 万字符）。元素与链接
#: 各 200 条、名字 120 字、链接文字 80 字、href 不限长 ⇒ 最坏情况
#: 还能吃掉好几万字符。⇒ 给它们**各**一个预算，别让三段一起顶爆窗口。
#:
#: ⚠️ 这个数**不是**「裁掉了多少」的账——被预算裁掉的元素 / 链接模型
#: **根本没看到**，所以它与 ``candidates`` / ``candidates_shown`` 是
#: 同一笔账的两个数字，**不许混算**。
_ITEM_CHAR_BUDGET = 40_000

#: 提示给模型的行数上限的上下限。
_MIN_MAX_LINES = 3
_MAX_MAX_LINES = 40

#: **实测够用的输出预算**（``TRAJECTORY_LLM_MAX_TOKENS``）。
#:
#: 为什么要单独记一个数：推理模型（``MiniMax-M3.1-Flash``）的**思考与正文
#: 共用**这一个预算，思考吃光 ⇒ ``finish_reason=length`` + ``content=null``
#: ——**请求成功、正文为空**，看起来像「后端坏了」，其实是「这个数小了」。
#: 实测（运行手册 §环境变量 / 方案 23 §666）：600 思考吃光、2048 正文被截断、
#: 8192 在小输入有干净正文但**真实场景仍被吃光**、**16384 恢复正常**。
#:
#: ⚠️ 这是**实测推荐值**，不是权威——权威是环境变量
#: ``TRAJECTORY_LLM_MAX_TOKENS``（缺省见
#: :data:`trajectory_pipeline.llm.client.DEFAULT_MAX_TOKENS`）。本模块只在
#: **两处**用它：① 重试时的预算**下界**；② CLI 缺预算时的提示。
#: 换模型/换机器要重新实测（症状：``budget:`` 开头的失败）。
#:
#: ⚠️ H-41 后它只剩「下界」这一层意思了：缺省预算已抬到
#: :data:`trajectory_pipeline.llm.client.MAX_OUTPUT_TOKENS`，
#: ``max(缺省 × 2, 16384)`` 会被硬顶截回缺省 ⇒ **缺省状态下不再有
#: 「换大预算」这一步**（已经在顶 ⇒ 重发同一个预算结果一样 ⇒ 不烧钱）。
#: 这条路的正确用法是**显式把环境变量调小**（让缺省留出抬升空间）。
RECOMMENDED_MAX_TOKENS = 16384

#: 预算用尽时**重试一次**用的输出预算倍数。
#:
#: 为什么重试而不是直接失败：这一类失败**换预算就能好**，而**重发同一个
#: 预算结果一样**——客户端内部也是这么判的（``budget:`` 不进重试循环，
#: 见 ``llm/client.py::_exchange``）。所以「重试」在这里的具体含义是
#: **换大预算再发一次**，不是原样重发。
#:
#: 只重试**一次**：预算一直不够说明这个模型在这个输入上就是话多，
#: 再试就是烧钱。第二次还失败 ⇒ 整条作废 ⇒ 渲染层走规则裁剪（零回归）。
#:
#: ⚠️ H-41：**重试预算要封顶**（:data:`MAX_OUTPUT_TOKENS`），且**只在
#: 预算真的更大时才重试**——已经在顶 ⇒ 重发同一个预算结果一样 ⇒
#: 白烧一次钱、还多一条失败记录。
BUDGET_RETRY_FACTOR = 2

#: 「输出被截断」在**选行类调用**里的归一错误码（H-41）。
#:
#: ⚠️ **必须带 ``budget:`` 前缀**：它是 :func:`_is_budget_exhausted` 的
#: 唯一判据 ⇒ 归一之后才能走上「换大预算重试一次」那条路。
#: 没有前缀的话，截断会落到「模型没回话」上——**静默退回规则裁剪**，
#: 而账上看起来像「模型不想要这些行」。
_TRUNCATED_ERR = (
    "budget:max_tokens 用尽（finish_reason=length，选行结果被截断）；"
    "调大 TRAJECTORY_LLM_MAX_TOKENS"
)


@dataclass(frozen=True, slots=True)
class RefineOutcome:
    """一条观察的精炼结果。

    ``source`` 三值：
    - ``not_needed``：候选行本来就在预算内，**没发请求**；
    - ``llm``：精炼成功，``lines`` 是保留的行（逐字来自原文）；
    - ``failed``：出问题了，``error`` 是原因，``lines`` 为空
      ⇒ **渲染层走原路径**。

    ⚠️ **``elements`` / ``links`` 是 H-41 阶段 2 加的**：语义模式下
    元素与链接**也由模型选**（否则那 10 / 4 的规则上限就还是截断）。
    两者与 ``lines`` 一样**逐字来自原文**（``(role, name)`` /
    ``(text, href)``），渲染层据此**逐字核对重建**。
    """

    source: str
    lines: tuple[str, ...] = ()
    #: **模型选的元素**（``(role, name)``）。
    elements: tuple[tuple[str, str], ...] = ()
    #: **模型选的链接**（``(text, href)``）。
    links: tuple[tuple[str, str], ...] = ()
    dropped: int = 0
    candidates: int = 0
    #: **实际给了模型多少候选行**（H-41）。与 :attr:`candidates` 是
    #: **两笔账**：``candidates - candidates_shown`` = 「我们自己没给它看」
    #: （被字符预算裁掉的），``candidates_shown - len(lines)`` = 「它看了但不要」。
    #: 混算成 ``dropped`` 就分不出这两种完全不同的结论。
    candidates_shown: int = 0
    error: str = ""
    call: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.source == "llm" and bool(self.lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "lines": list(self.lines),
            "elements": [{"role": r, "name": n} for r, n in self.elements],
            "links": [{"text": t, "href": h} for t, h in self.links],
            "dropped": self.dropped,
            "candidates": self.candidates,
            "candidates_shown": self.candidates_shown,
            "error": self.error,
            "call": dict(self.call),
        }


@dataclass(frozen=True, slots=True)
class RefineStep:
    """旁挂里的一条记录（对应一条观察）。

    ⚠️ **两个哈希不是一回事**（H-41 阶段 3）：

    - ``selection_key`` = **索引键** = **候选集哈希**（正文 + 元素候选 +
      链接候选）——**定位靠它**。三段协议选的是那三样 ⇒ 键必须覆盖那三样
      （只按正文定位 = 把 A 步的选择贴到 B 步上 ⇒ 整条回退）。
    - ``body_key`` = **正文哈希**——**只作留痕**（同一份正文可以有多条记录）。
    """

    #: **索引键**（候选集哈希）——`common.refine_sidecar.selection_key`。
    selection_key: str
    #: **正文哈希**——只作留痕，不是索引键。
    body_key: str
    source: str
    kept: tuple[str, ...] = ()
    #: **模型选的元素 / 链接**（H-41 阶段 2）。
    elements: tuple[tuple[str, str], ...] = ()
    links: tuple[tuple[str, str], ...] = ()
    dropped: int = 0
    candidates: int = 0
    #: 实际给了模型多少候选行（见 :class:`RefineOutcome` 的同名键）。
    candidates_shown: int = 0
    error: str = ""
    call: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "selection_key": self.selection_key,
            "body_key": self.body_key,
            "source": self.source,
            "kept": list(self.kept),
            "elements": [{"role": r, "name": n} for r, n in self.elements],
            "links": [{"text": t, "href": h} for t, h in self.links],
            "dropped": self.dropped,
            "candidates": self.candidates,
            "candidates_shown": self.candidates_shown,
            "error": self.error,
            "call": dict(self.call),
        }


def estimate_max_lines(
    candidates: Sequence[str],
    limit: int | None,
    *,
    semantic: bool = False,
) -> int:
    """给模型的行数上限（提示用，**不是硬约束**）。

    按候选行的平均长度估算「多少行能填满预算」。估得不准不要紧：
    渲染层还有一道硬顶（``observation_view`` 的 ``VIEW_BODY_CHARS``），
    多选的行会被那道顶按相关度裁掉，动作目标永远排在最前。

    ⚠️ **语义模式放开这个上限**（H-41 阶段 2 · 方案 26 §6.3 #5）：
    语义模式下**没有字符硬顶**，所以「按预算估行数」这件事**没有对象**
    ——估出来的 12 行只是把模型框死在 12 行，而那正是「让模型选核心
    内容」要避免的。⇒ 返回 ``0`` = 「行数不限」（提示词里原话）。

    ⚠️ ``limit=None``（语义模式给的）同样 ⇒ ``0`` = 不限。

    ⚠️ ``0`` 与 ``_MIN_MAX_LINES`` 的区别：``0`` 是**不限**，``3`` 是
    「至少给 3 行」。别把两者混成一个「默认」。
    """
    if semantic or limit is None:
        return 0
    if not candidates:
        return _MIN_MAX_LINES
    total = sum(len(c) + 1 for c in candidates)
    avg = max(1, total // len(candidates))
    return max(_MIN_MAX_LINES, min(_MAX_MAX_LINES, limit // avg))


def parse_selection(raw: str | None, count: int) -> tuple[list[int], str]:
    """模型原文 → 行号表（1 基）。

    Returns:
        ``(行号表, 错误原因)``——错误原因非空时行号表必为空。
    """
    if raw is None or not raw.strip():
        return [], "模型没回话"
    text = _FENCE_RE.sub("", raw.strip()).strip()
    text = text.strip("[]()（）【】")
    if not text:
        return [], "模型没回话"
    if not _ALLOWED_RE.match(text):
        return [], f"输出不是纯编号：{text[:40]!r}"
    nums = [int(x) for x in re.findall(r"\d+", text)]
    if not nums:
        return [], "输出里没有编号"
    picked: list[int] = []
    for n in nums:
        if n < 1 or n > count:
            # 越界 = 模型看错了行数表。**整条作废**：宁可不精炼，
            # 也不能把「第 37 行」当成「第 7 行」——那会选出一份
            # 看起来合法、实际与任务无关的正文。
            return [], f"行号越界：{n}（候选 {count} 行）"
        if n not in picked:
            picked.append(n)
    return picked, ""


def split_selection(
    picked: Sequence[int],
    *,
    n_body: int,
    n_elements: int,
    n_links: int,
) -> tuple[list[int], list[int], list[int]]:
    """全局编号表 → **三段各自的 1 基下标**。

    编号空间是**连续**的（正文 ``1…N`` → 元素 ``N+1…N+M`` →
    链接 ``N+M+1…N+M+L``），所以分流就是**按区间切**。

    ⚠️ 三段的下标都是**段内 1 基**——渲染层与校验层都按段内下标说话
    （「第 3 个元素」而不是「第 17 号」），段内下标与候选清单的顺序
    一一对应 ⇒ 错位无从发生。
    """
    body: list[int] = []
    elements: list[int] = []
    links: list[int] = []
    e_end = n_body + n_elements
    for n in picked:
        if n <= n_body:
            body.append(n)
        elif n <= e_end:
            elements.append(n - n_body)
        else:
            links.append(n - e_end)
    return body, elements, links


def validate_selection(
    picked: Sequence[int],
    candidates: Sequence[str],
    *,
    focus: Sequence[str] = (),
    keep: Sequence[str] = (),
    element_picked: Sequence[int] = (),
    elements: Sequence[tuple[str, str]] = (),
    link_picked: Sequence[int] = (),
    links: Sequence[tuple[str, str]] = (),
) -> str:
    """选得对不对。返回空串 = 通过；否则是失败原因。

    两条保真判据：

    - **动作目标必须保住**：下一步要点的那个控件/网址所在的项被丢掉，
      训练样本里就出现「点了一个观察里没有的东西」——那是在教模型编目标
      （与 ``observation_view`` 的 ``keep`` 同款纪律）。
    - **任务词命中的行至少留一条**：全丢掉等于把这一页的核心信息删干净了。

    ⚠️ 判据只在**候选里确实有**那条线时才生效：页面上根本没有目标
    文本，不是「被模型丢了」。

    ⚠️ **动作目标跨三段判**（H-41 阶段 2）：目标文本出现在**任一段**的
    候选里时，它必须被**选中**（在**哪一段**选中都算——渲染层会把目标项
    无条件补回观察，所以「看得见」就够了）。⚠️ 这里**不能**按段各判各的：
    目标常常是**元素名**而不是正文行，按段判会让「正文里没有目标行」被
    误判成「模型把目标丢了」⇒ 整条作废 ⇒ 白花一次调用。
    """
    if not picked and not element_picked and not link_picked:
        return "一个编号都没选"
    chosen = set(picked)
    e_chosen = set(element_picked)
    l_chosen = set(link_picked)
    for target in keep:
        if not target:
            continue
        body_h = [i for i, text in enumerate(candidates) if target in text]
        e_hits = [i for i, (_role, name) in enumerate(elements) if target in name]
        l_hits = [i for i, (_text, href) in enumerate(links) if target in href]
        if not (body_h or e_hits or l_hits):
            # 页面上根本没有目标文本 ⇒ 不是「被模型丢了」。
            continue
        if (
            any((i + 1) in chosen for i in body_h)
            or any((i + 1) in e_chosen for i in e_hits)
            or any((i + 1) in l_chosen for i in l_hits)
        ):
            continue
        return f"下一步操作目标没保住：{target}"
    if focus:
        body_h = [
            i for i, text in enumerate(candidates)
            if any(f and f in text for f in focus)
        ]
        e_hits = [
            i for i, (_role, name) in enumerate(elements)
            if any(f and f in name for f in focus)
        ]
        l_hits = [
            i for i, (_text, href) in enumerate(links)
            if any(f and f in href for f in focus)
        ]
        if (body_h or e_hits or l_hits) and not (
            any((i + 1) in chosen for i in body_h)
            or any((i + 1) in e_chosen for i in e_hits)
            or any((i + 1) in l_chosen for i in l_hits)
        ):
            return "任务关键词命中的项全被丢掉"
    return ""


def _call_ledger(client: Any) -> dict[str, Any]:
    """把**刚刚那次**调用折成一行账（不含 caller——caller 是调用点的语义）。"""
    usage = getattr(client, "last_usage", None) or {}
    cfg = getattr(client, "cfg", None)
    return {
        "model": str(getattr(cfg, "model", "") or ""),
        "latency_ms": int(getattr(client, "last_latency_ms", 0) or 0),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "error": str(getattr(client, "last_call_error", "") or ""),
        # ⚠️ **``finish_reason`` 必须进账**（H-41）：选行类调用里
        # ``"length"`` 意味着「编号表只回了一半」——它和「模型回得不好」
        # 是两种失败，处置也不同（前者换预算能好）。
        "finish_reason": str(getattr(client, "last_finish_reason", "") or ""),
    }


def _is_budget_exhausted(error: str) -> bool:
    """这一条失败是不是「输出预算用尽」。

    判据是客户端给的**前缀**（``budget:``）——它是
    :class:`trajectory_pipeline.llm.client._BudgetExhausted` 抛出来的
    唯一标记，与别的失败（``http:500`` / ``shape:ValueError``）**分得开**。

    ⚠️ 只在这一类上重试：``http:500`` 是后端过载（客户端内部已经重试过
    两次），``shape:`` 是模型稳定输出坏格式（重发还是那样）——
    这两类换预算都没用。
    """
    return str(error or "").startswith("budget:")


def _is_transport_error(error: str) -> bool:
    """这一条失败是不是**传输层**失败（超时 / 连不上 / DNS）。

    判据同样取客户端给的**前缀**（``transport:``）——它与 ``budget:``
    是**两个完全不同的轴**：

    - ``budget:`` = **输出不够** ⇒ 换大预算**能好**；
    - ``transport:`` = **没拿到响应** ⇒ 换预算**不能好**（而且预算越大
      越容易超时——**反效果**）。

    ⚠️ 所以超时那条路的重试**不换预算**（见 :func:`refine_body`）。
    """
    return str(error or "").startswith("transport:")


def _current_max_tokens(client: Any) -> int:
    """**这一次调用实际用的**输出预算。

    从客户端自己的配置推（``chat`` 不带 ``max_tokens`` 时就用
    ``cfg.max_tokens``），不在调用点写死数字——写死的数字必然在某个
    后端上不够。
    """
    cfg = getattr(client, "cfg", None)
    base = int(getattr(cfg, "max_tokens", 0) or 0)
    return base if base > 0 else DEFAULT_MAX_TOKENS


def _retry_max_tokens(client: Any) -> int:
    """重试时用的输出预算（**封顶**在 :data:`MAX_OUTPUT_TOKENS`）。

    **从客户端自己的配置推**，不在调用点写死数字——写死的数字必然在某个
    后端上不够（``llm/client.py`` 模块头第 2 条：重试不改变结论、
    写死的预算会骗人）。

    取 ``min(max(配置值 × 2, 实测够用值), 硬顶)``：
    - 配置 8192（缺省）⇒ **16384**（实测恢复值）；
    - 配置 16384 ⇒ 32768（模型话更多时才需要）；
    - 配置 32768 ⇒ 65536；
    - 配置已到硬顶 ⇒ **就是硬顶**（见下）。

    ⚠️ **封顶不是「省事」**：``max_tokens`` 原样下发后端，抬过某些后端
    的单请求上限 ⇒ **400** ⇒ 客户端 4xx **不重试** ⇒ 一次可恢复的
    失败被变成一次不可恢复的失败，还会**推进熔断计数**。
    """
    base = _current_max_tokens(client)
    want = max(base * BUDGET_RETRY_FACTOR, RECOMMENDED_MAX_TOKENS)
    return min(want, MAX_OUTPUT_TOKENS)


def _select_shown(
    candidates: Sequence[str],
    *,
    focus: Sequence[str],
    keep: Sequence[str],
    char_budget: int = MAX_CANDIDATE_CHARS,
) -> list[str]:
    """候选行 → **送给模型的那一批**（按字符预算裁，按页面顺序呈现）。

    ⚠️ **裁的依据是字符不是行数**（H-41）：行数不映射 token，而
    「输入太长」这件事只有一个真实单位。原来的「200 行上限」还有个
    副作用——被裁掉的行**模型根本没看到**，却算在 ``dropped`` 里，
    于是「模型不想要」与「我们没给它看」混成同一个数字。

    三条规则：

    1. **动作目标行（``keep`` 命中）无条件入选**——它们是保真口径的
       底线。没给模型看 ⇒ 模型不可能选它 ⇒ 整条精炼必然作废
       （白花一次调用，还多一条「模型不听话」的假账）。
    2. **其余按相关度填**，直到字符预算用完；单行放不下就**跳过它**
       （不是 ``break``）——后面可能有更短的行放得下。
    3. **最终按页面顺序呈现**：模型看到的行号与页面从上到下一致，
       页面结构信号不丢。（渲染层最终按原文位置重建，见
       :func:`trajectory_pipeline.assembler.observation_view.
       _rebuild_from_refined`，所以这里的呈现顺序不影响最终文本。）

    ⚠️ **动作目标行可能顶破预算**：保真口径优先于输入长度——宁可让
    这一次请求长一点，也不能让保真目标从候选里消失。
    """
    total = sum(len(c) + 1 for c in candidates)
    if total <= char_budget:
        return list(candidates)
    scored = [
        (i, body_lines.line_score(line, focus, keep), line)
        for i, line in enumerate(candidates)
    ]
    must = [(i, line) for i, score, line in scored if score >= 4]
    rest = sorted(
        ((i, score, line) for i, score, line in scored if score < 4),
        key=lambda t: (-t[1], t[0]),
    )
    chosen: list[tuple[int, str]] = list(must)
    used = sum(len(line) + 1 for _, line in chosen)
    for i, _score, line in rest:
        if used + len(line) + 1 > char_budget:
            continue
        chosen.append((i, line))
        used += len(line) + 1
    chosen.sort(key=lambda t: t[0])
    return [line for _, line in chosen]


def _merge_ledgers(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    retry_max_tokens: int,
) -> dict[str, Any]:
    """两次调用的账**合并成一行**。

    为什么要合并：重试是**两次真实调用**（两次都花了钱与时间），
    只记第二次就等于把第一次藏起来——而「这个条目为什么慢」这种问题
    只有在账里能看出来。合并后的账多两个键：``retried`` /
    ``retry_max_tokens``（重试过没有、重试用多大预算）。
    """
    merged = dict(first)
    merged["latency_ms"] = (
        int(first.get("latency_ms") or 0) + int(second.get("latency_ms") or 0)
    )
    for key in ("prompt_tokens", "completion_tokens"):
        a = first.get(key)
        b = second.get(key)
        if a is None or b is None:
            merged[key] = a if a is not None else b
        else:
            merged[key] = int(a) + int(b)
    merged["error"] = str(second.get("error") or "")
    # ⚠️ **``finish_reason`` 取第二次的**（H-41）：账本描述的是
    # 「最后一次调用」的状态。两次都记会让人以为「第一次也 length」——
    # 那是靠 ``retried`` 与 ``retry_max_tokens`` 两个键说的事。
    merged["finish_reason"] = str(second.get("finish_reason") or "")
    merged["retried"] = True
    merged["retry_max_tokens"] = int(retry_max_tokens)
    return merged


def needs_refine(
    body: str,
    limit: int | None,
    *,
    semantic: bool = False,
    elements: Sequence[tuple[str, str]] = (),
    links: Sequence[tuple[str, str]] = (),
) -> bool:
    """这份观察**会不会被裁**（⇒ 值不值得发一次请求）。

    与 :func:`refine_body` 里的判据**同款**（同一份
    :func:`trajectory_pipeline.common.body_lines.candidate_lines`）。
    单独暴露它，是为了让调用方能在**发请求之前**知道要不要花这次调用
    （批次预算）——判据算两遍，但它是纯字符串操作，而预算判断与真调用
    两处口径分叉的代价是「超支了才知道」。

    ``limit`` 由调用方给（普通步 320 / 读步 2000，权威在
    :mod:`trajectory_pipeline.assembler.observation_view`）——本模块
    **不 import 装配层**（方向不能反）。

    ⚠️ **语义模式下判据反过来**（H-41 阶段 2）：语义模式**没有字符
    硬顶** ⇒ 正文**一定**会被裁（裁的是模型没选的那些行）⇒ 只要
    **有候选**（正文行 / 元素 / 链接任一）就值得发一次请求。

    ⚠️ 语义模式下 ``limit`` 是 ``None``（**没有上限**这个概念）——
    传进来也不看。
    """
    if semantic:
        return bool(
            body_lines.candidate_lines_with_noise(body) or elements or links
        )
    if limit is None:
        # 没有上限 ⇒ 什么都算「会被裁」。
        return bool(body_lines.candidate_lines(body))
    candidates = body_lines.candidate_lines(body)
    if not candidates:
        return False
    return sum(len(c) + 1 for c in candidates) > limit


def _fit_items(
    items: Sequence[tuple[str, str]],
    *,
    keep: Sequence[str],
    char_budget: int,
) -> list[tuple[str, str]]:
    """元素 / 链接候选：按**字符预算**裁（``keep`` 命中的无条件入选）。

    与 :func:`_select_shown` 同款纪律：**保真目标优先于输入长度**——
    没给模型看 ⇒ 模型不可能选它 ⇒ 整条精炼必然作废。

    ⚠️ 其余项按**页面顺序**填（不按相关度）——元素 / 链接的「相关度」
    是任务相关的，而这一层**不知道**任务上下文（它只有候选清单）；
    按顺序填至少保证「页面前面的东西先给模型」，而模型自己会挑。
    """
    total = sum(len(a) + len(b) + 2 for a, b in items)
    if total <= char_budget:
        return list(items)
    must = [
        (i, item) for i, item in enumerate(items)
        if any(k and k in item[1] for k in keep)
    ]
    rest = [
        (i, item) for i, item in enumerate(items)
        if not any(k and k in item[1] for k in keep)
    ]
    chosen: list[tuple[int, tuple[str, str]]] = list(must)
    used = sum(len(a) + len(b) + 2 for _, (a, b) in chosen)
    for i, item in rest:
        cost = len(item[0]) + len(item[1]) + 2
        if used + cost > char_budget:
            continue
        chosen.append((i, item))
        used += cost
    chosen.sort(key=lambda t: t[0])
    return [item for _, item in chosen]


def refine_body(
    client: Any,
    body: str,
    *,
    focus: Sequence[str] = (),
    keep: Sequence[str] = (),
    url: str = "",
    title: str = "",
    limit: int | None,
    elements: Sequence[tuple[str, str]] = (),
    links: Sequence[tuple[str, str]] = (),
    semantic: bool = False,
) -> RefineOutcome:
    """精炼一份正文（**外加**元素与链接的选择）。

    Args:
        client: :class:`~trajectory_pipeline.llm.client.LLMClient`（**只读**：
            本函数不碰它的连接状态）。正常**只发一次** ``chat``；只有
            「输出预算用尽」那一种失败会**换大预算再发一次**
            （见 :data:`BUDGET_RETRY_FACTOR`）。
        body: **取证态全文**（P1 的 ``body_text``，不截断）。
        limit: 训练态正文的字数上限（普通步 320 / 读步 2000）。
            **只影响「要不要精炼」与提示的行数**，最终裁剪仍由渲染层做。
            **必填**：本模块不 import 装配层，数字的权威在那里
            （:mod:`trajectory_pipeline.assembler.observation_view`），
            在这里写死 320 就是第二份真源。
            ⚠️ **语义模式下是 ``None``**——语义模式**没有字数上限**
            （那正是被禁掉的那种截断），给个数字反而会让人以为还有顶。
        elements: **元素候选**（``(role, name)``）——摆给模型挑的清单，
            由调用方按 :mod:`trajectory_pipeline.common.view_candidates`
            构造（**唯一真源**，渲染层用同一份）。
        links: **链接候选**（``(text, href)``）——同上。
        semantic: **语义模式**（H-41 阶段 2）——① 候选行里**连被判噪声
            的行一起给**（标「可推翻」）；② **不设行数上限**；③ 元素与
            链接**也参与选择**。

    Returns:
        :class:`RefineOutcome`。``source == "failed"`` ⇒ 渲染层走原路径。
    """
    if semantic:
        pairs = body_lines.candidate_lines_with_noise(body)
        noise_by_line = {line: flag for line, flag in pairs}
        candidates = [line for line, _flag in pairs]
    else:
        candidates = body_lines.candidate_lines(body)
        noise_by_line = {}
    if not candidates and not elements and not links:
        # ⚠️ **「没有候选」不是失败**（H-41 阶段 3）：语义模式下「这条观察
        # 压根没有可精炼的东西」是**正常状态**（读步只采了一块，正文是空的；
        # 元素 / 链接也一个都没有）⇒ 记 ``not_needed``。
        # 记成 ``failed`` 会让报表把「没东西可选」说成「精炼失败」——
        # 而这两件事的处置完全不同（一个是本来就该走规则裁剪，一个是
        # 后端出了问题）。
        if semantic:
            return RefineOutcome(
                source="not_needed", error="没有可精炼的候选（正文 / 元素 / 链接都空）",
            )
        return RefineOutcome(source="failed", error="正文没有候选行")
    budget = sum(len(c) + 1 for c in candidates)
    if not semantic and limit is not None and budget <= limit:
        # 规则层够用 ⇒ **不发请求**。成本只花在真会被裁的地方。
        return RefineOutcome(
            source="not_needed", candidates=len(candidates),
            candidates_shown=len(candidates),
        )

    shown = _select_shown(candidates, focus=focus, keep=keep)
    noise_flags = [noise_by_line.get(line, False) for line in shown]
    elem_shown = _fit_items(elements, keep=keep, char_budget=_ITEM_CHAR_BUDGET)
    link_shown = _fit_items(links, keep=keep, char_budget=_ITEM_CHAR_BUDGET)
    max_lines = estimate_max_lines(shown, limit, semantic=semantic)
    payload = build_user_payload(
        shown, focus=focus, keep=keep, url=url, title=title, max_lines=max_lines,
        noise_flags=noise_flags, elements=elem_shown, links=link_shown,
    )
    raw = client.chat(REFINE_SYSTEM, payload)
    ledger = _call_ledger(client)

    # ⚠️ **``finish_reason == "length"`` 在这里是失败**（H-41）。
    # 客户端**刻意**不因它作废（``chat()`` 的返回行为一字不动），
    # 因为散文类调用拿到半截文字也有用。但**选行类调用不行**：
    # 编号表只回了一半 ⇒ 剩下的行模型**根本没表态** ⇒ 把它当成
    # 「模型只想要第 1 行」就会**静默丢掉**后面所有内容。
    # ⇒ 归一成 ``budget:`` 错误 ⇒ 走「换大预算重试一次」这条路。
    if raw is not None and str(ledger.get("finish_reason") or "") == "length":
        raw = None
        ledger["error"] = _TRUNCATED_ERR

    if raw is None and _is_budget_exhausted(ledger.get("error")):
        # 预算用尽：**换预算能好**，重发同一个预算不能（客户端内部也这么
        # 判：`budget:` 不进重试循环）。所以这里换大预算**重试一次**。
        #
        # ⚠️ 只一次：预算一直不够说明这个模型在这个输入上就是话多，
        # 再试就是烧钱。第二次还失败 ⇒ 整条作废 ⇒ 走规则裁剪（零回归）。
        retry_tokens = _retry_max_tokens(client)
        if retry_tokens <= _current_max_tokens(client):
            # ⚠️ **已经在顶**（H-41）：换不出更大的预算 ⇒ 重发同一个预算
            # 结果必然一样 ⇒ **不烧钱**，整条直接作废（走规则裁剪）。
            pass
        else:
            try:
                raw = client.chat(
                    REFINE_SYSTEM, payload, max_tokens=retry_tokens,
                )
            except TypeError:
                # 客户端不认 ``max_tokens``（第三方实现/假客户端）⇒ 这次重试
                # **根本没发生** ⇒ 账本**不动**（标成 ``retried`` 就是撒谎）。
                raw = None
            else:
                ledger = _merge_ledgers(
                    ledger, _call_ledger(client), retry_max_tokens=retry_tokens,
                )
                # 重试**也**被截断 ⇒ 换预算这条路已经走到头 ⇒ 整条作废。
                if (raw is not None
                        and str(ledger.get("finish_reason") or "") == "length"):
                    raw = None
                    ledger["error"] = (
                        "budget:max_tokens 用尽（finish_reason=length，"
                        "换大预算重试后选行结果仍被截断）"
                    )
    elif raw is None and _is_transport_error(ledger.get("error")):
        # ⚠️ **超时也要重试**（H-41 阶段 2 · 方案 26 §6.3 #8）。
        #
        # 此前只有 ``budget:`` 触发重试 ⇒ ``transport:``（超时）直接
        # **静默退回规则裁剪**——报表上只多一行「失败」，看不出
        # 「预算变了 / 输入变大了」这个真正的原因（这正是阶段 1 把
        # ``DEFAULT_TIMEOUT_S`` 从 90 抬到 600 的动机）。
        #
        # ⚠️ **不换预算**：超时是「没等到响应」，不是「输出不够」——
        # 换大输出预算**只会更容易超时**（反效果）。这里换的是
        # **时间**：同 payload、同预算，再发一次。
        #
        # ⚠️ **客户端内部已经重试过**（``max_retries + 1`` 次），所以
        # 这一层的重试是**加倍**⇒ 最坏耗时翻倍。批次的总上限由
        # ``cli refine --max-calls`` 兜着。
        #
        # ⚠️ 只一次：重试还不成 ⇒ 整条作废 ⇒ 走规则裁剪（零回归）。
        try:
            raw = client.chat(REFINE_SYSTEM, payload)
        except TypeError:
            raw = None
        else:
            ledger = _merge_ledgers(
                ledger, _call_ledger(client),
                retry_max_tokens=_current_max_tokens(client),
            )
            if raw is not None and str(ledger.get("finish_reason") or "") == "length":
                raw = None
                ledger["error"] = _TRUNCATED_ERR

    if raw is None:
        return RefineOutcome(
            source="failed", candidates=len(candidates),
            candidates_shown=len(shown),
            error=ledger.get("error") or "模型没回话", call=ledger,
        )
    # ⚠️ **编号空间是三段连续的**（正文 → 元素 → 链接），所以
    # ``parse_selection`` 只需要知道**总数**——越界判定仍然成立
    # （「第 999 号」在任何一段里都不存在 ⇒ 整条作废）。
    picked, err = parse_selection(
        raw, len(shown) + len(elem_shown) + len(link_shown),
    )
    if err:
        return RefineOutcome(
            source="failed", candidates=len(candidates),
            candidates_shown=len(shown), error=err, call=ledger,
        )
    body_picked, elem_picked, link_picked = split_selection(
        picked,
        n_body=len(shown), n_elements=len(elem_shown), n_links=len(link_shown),
    )
    err = validate_selection(
        body_picked, shown, focus=focus, keep=keep,
        element_picked=elem_picked, elements=elem_shown,
        link_picked=link_picked, links=link_shown,
    )
    if err:
        return RefineOutcome(
            source="failed", candidates=len(candidates),
            candidates_shown=len(shown), error=err, call=ledger,
        )

    lines = tuple(shown[i - 1] for i in body_picked)
    return RefineOutcome(
        source="llm",
        lines=lines,
        elements=tuple(elem_shown[i - 1] for i in elem_picked),
        links=tuple(link_shown[i - 1] for i in link_picked),
        dropped=len(candidates) - len(lines),
        candidates=len(candidates),
        candidates_shown=len(shown),
        call=ledger,
    )


__all__ = [
    "BUDGET_RETRY_FACTOR",
    "MAX_CANDIDATE_CHARS",
    "RECOMMENDED_MAX_TOKENS",
    "REFINE_PROMPT_VERSION",
    "RefineOutcome",
    "RefineStep",
    "estimate_max_lines",
    "needs_refine",
    "parse_selection",
    "refine_body",
    "split_selection",
    "validate_selection",
]
