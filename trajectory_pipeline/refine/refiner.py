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
from trajectory_pipeline.llm.client import DEFAULT_MAX_TOKENS
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

#: 行号上限：单次请求最多给模型多少候选行。超了先按「相关度」裁，
#: 否则 prompt 会长到后端截断（而截断的表现是「输出不完整」，
#: 不是「输入太长了」，没人会去查它）。
MAX_CANDIDATES = 200

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
#: ``TRAJECTORY_LLM_MAX_TOKENS``（缺省 8192）。本模块只在**两处**用它：
#: ① 重试时的预算下界；② CLI 缺预算时的提示。
#: 换模型/换机器要重新实测（症状：``budget:`` 开头的失败）。
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
BUDGET_RETRY_FACTOR = 2


@dataclass(frozen=True, slots=True)
class RefineOutcome:
    """一条观察的精炼结果。

    ``source`` 三值：
    - ``not_needed``：候选行本来就在预算内，**没发请求**；
    - ``llm``：精炼成功，``lines`` 是保留的行（逐字来自原文）；
    - ``failed``：出问题了，``error`` 是原因，``lines`` 为空
      ⇒ **渲染层走原路径**。
    """

    source: str
    lines: tuple[str, ...] = ()
    dropped: int = 0
    candidates: int = 0
    error: str = ""
    call: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.source == "llm" and bool(self.lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "lines": list(self.lines),
            "dropped": self.dropped,
            "candidates": self.candidates,
            "error": self.error,
            "call": dict(self.call),
        }


@dataclass(frozen=True, slots=True)
class RefineStep:
    """旁挂里的一条记录（对应一条观察）。"""

    body_key: str
    source: str
    kept: tuple[str, ...] = ()
    dropped: int = 0
    candidates: int = 0
    error: str = ""
    call: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "body_key": self.body_key,
            "source": self.source,
            "kept": list(self.kept),
            "dropped": self.dropped,
            "candidates": self.candidates,
            "error": self.error,
            "call": dict(self.call),
        }


def estimate_max_lines(candidates: Sequence[str], limit: int) -> int:
    """给模型的行数上限（提示用，**不是硬约束**）。

    按候选行的平均长度估算「多少行能填满预算」。估得不准不要紧：
    渲染层还有一道硬顶（``observation_view`` 的 ``VIEW_BODY_CHARS``），
    多选的行会被那道顶按相关度裁掉，动作目标永远排在最前。
    """
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


def validate_selection(
    picked: Sequence[int],
    candidates: Sequence[str],
    *,
    focus: Sequence[str] = (),
    keep: Sequence[str] = (),
) -> str:
    """选得对不对。返回空串 = 通过；否则是失败原因。

    两条保真判据：

    - **动作目标必须保住**：下一步要点的那个控件/网址所在的行被丢掉，
      训练样本里就出现「点了一个观察里没有的东西」——那是在教模型编目标
      （与 ``observation_view`` 的 ``keep`` 同款纪律）。
    - **任务词命中的行至少留一条**：全丢掉等于把这一页的核心信息删干净了。

    ⚠️ 判据只在**候选里确实有**那条线时才生效：页面上根本没有目标
    文本，不是「被模型丢了」。
    """
    if not picked:
        return "一个编号都没选"
    chosen = set(picked)
    for target in keep:
        if not target:
            continue
        hits = [i for i, text in enumerate(candidates) if target in text]
        if not hits:
            continue
        if not any((i + 1) in chosen for i in hits):
            return f"下一步操作目标没保住：{target}"
    if focus:
        hits = [
            i for i, text in enumerate(candidates)
            if any(f and f in text for f in focus)
        ]
        if hits and not any((i + 1) in chosen for i in hits):
            return "任务关键词命中的行全被丢掉"
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


def _retry_max_tokens(client: Any) -> int:
    """重试时用的输出预算。

    **从客户端自己的配置推**，不在调用点写死数字——写死的数字必然在某个
    后端上不够（``llm/client.py`` 模块头第 2 条：重试不改变结论、
    写死的预算会骗人）。

    取 ``max(配置值 × 2, 实测够用值)``：
    - 配置 8192（缺省）⇒ **16384**（实测恢复值）；
    - 配置 16384 ⇒ 32768（模型话更多时才需要）；
    - 配置 32768 ⇒ 65536。
    """
    cfg = getattr(client, "cfg", None)
    base = int(getattr(cfg, "max_tokens", 0) or 0)
    if base <= 0:
        base = DEFAULT_MAX_TOKENS
    return max(base * BUDGET_RETRY_FACTOR, RECOMMENDED_MAX_TOKENS)


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
    merged["retried"] = True
    merged["retry_max_tokens"] = int(retry_max_tokens)
    return merged


def needs_refine(body: str, limit: int) -> bool:
    """这份正文**会不会被裁**（⇒ 值不值得发一次请求）。

    与 :func:`refine_body` 里的判据**同款**（同一份
    :func:`trajectory_pipeline.common.body_lines.candidate_lines`）。
    单独暴露它，是为了让调用方能在**发请求之前**知道要不要花这次调用
    （批次预算）——判据算两遍，但它是纯字符串操作，而预算判断与真调用
    两处口径分叉的代价是「超支了才知道」。

    ``limit`` 由调用方给（普通步 320 / 读步 2000，权威在
    :mod:`trajectory_pipeline.assembler.observation_view`）——本模块
    **不 import 装配层**（方向不能反）。
    """
    candidates = body_lines.candidate_lines(body)
    if not candidates:
        return False
    return sum(len(c) + 1 for c in candidates) > limit


def refine_body(
    client: Any,
    body: str,
    *,
    focus: Sequence[str] = (),
    keep: Sequence[str] = (),
    url: str = "",
    title: str = "",
    limit: int,
) -> RefineOutcome:
    """精炼一份正文。

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

    Returns:
        :class:`RefineOutcome`。``source == "failed"`` ⇒ 渲染层走原路径。
    """
    candidates = body_lines.candidate_lines(body)
    if not candidates:
        return RefineOutcome(source="failed", error="正文没有候选行")
    budget = sum(len(c) + 1 for c in candidates)
    if budget <= limit:
        # 规则层够用 ⇒ **不发请求**。成本只花在真会被裁的地方。
        return RefineOutcome(
            source="not_needed", candidates=len(candidates),
        )

    shown = candidates[:MAX_CANDIDATES]
    max_lines = estimate_max_lines(shown, limit)
    payload = build_user_payload(
        shown, focus=focus, keep=keep, url=url, title=title, max_lines=max_lines,
    )
    raw = client.chat(REFINE_SYSTEM, payload)
    ledger = _call_ledger(client)

    if raw is None and _is_budget_exhausted(ledger.get("error")):
        # 预算用尽：**换预算能好**，重发同一个预算不能（客户端内部也这么
        # 判：`budget:` 不进重试循环）。所以这里换大预算**重试一次**。
        #
        # ⚠️ 只一次：预算一直不够说明这个模型在这个输入上就是话多，
        # 再试就是烧钱。第二次还失败 ⇒ 整条作废 ⇒ 走规则裁剪（零回归）。
        retry_tokens = _retry_max_tokens(client)
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

    if raw is None:
        return RefineOutcome(
            source="failed", candidates=len(candidates),
            error=ledger.get("error") or "模型没回话", call=ledger,
        )
    picked, err = parse_selection(raw, len(shown))
    if err:
        return RefineOutcome(
            source="failed", candidates=len(candidates), error=err, call=ledger,
        )
    err = validate_selection(picked, shown, focus=focus, keep=keep)
    if err:
        return RefineOutcome(
            source="failed", candidates=len(candidates), error=err, call=ledger,
        )

    lines = tuple(shown[i - 1] for i in picked)
    return RefineOutcome(
        source="llm",
        lines=lines,
        dropped=len(candidates) - len(lines),
        candidates=len(candidates),
        call=ledger,
    )


__all__ = [
    "BUDGET_RETRY_FACTOR",
    "MAX_CANDIDATES",
    "RECOMMENDED_MAX_TOKENS",
    "REFINE_PROMPT_VERSION",
    "RefineOutcome",
    "RefineStep",
    "estimate_max_lines",
    "needs_refine",
    "parse_selection",
    "refine_body",
    "validate_selection",
]
