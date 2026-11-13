"""OpenAI 兼容 chat-completions 客户端——全树唯一的 LLM 出口。

**凭据从环境变量读，绝不进代码/测试/文档/日志。** 本模块的
:func:`LLMConfig.__repr__` 刻意不吐 key，异常消息也只带 base_url 与
model：异常会被打进日志和终端，那是凭据最容易泄漏的地方。

为什么单开一层而不是各处直接 httpx：已实测本项目后端**不强制
``json_schema``**，模型会漂字段名、套 ``<think>``、首轮跑偏。解析、
重试、fail-soft 必须是共享资产——写在 :mod:`~trajectory_pipeline.llm.schema_parse`
里，各模块只管调 :meth:`LLMClient.complete` 再把文本交给它。

四条纪律：

1. **失败不是异常，是返回值。** :meth:`complete` 返回 ``None`` 而非抛异常。
   调用方（感知层）的契约是 I4 fail-closed，它需要「判不出来」这个信号；
   让异常穿透会把「后端挂了」变成「整个批次崩了」。
2. **不重试到改变结论。** 重试只针对**传输层失败**（连不上/超时/5xx）；
   解析失败不重试——模型稳定地返回同一种坏格式，重试只是烧时间。
3. **超时是必填，不是默认值。** 本机推理可能跑几十秒，而 executor 的
   单站超时是 40s 量级；LLM 超时必须短于它，否则感知层先超时、
   判定根本没回来。
4. **懒加载 httpx。** 缺 httpx 时本模块仍可 import（``from_env`` 与
   :class:`LLMConfig` 都能用），只有真要发请求才报错。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

#: 环境变量名前缀。带 ``TRAJECTORY_`` 前缀是为了**不与存量 v1 的
#: ``LLM_*`` 撞名**——两棵树在同一台机器上跑，共用变量名会让一次
#: 调参同时改动两边，而 v1 是冻结的，改了不该生效却生效了。
ENV_BASE_URL = "TRAJECTORY_LLM_BASE_URL"
ENV_MODEL = "TRAJECTORY_LLM_MODEL"
ENV_API_KEY = "TRAJECTORY_LLM_API_KEY"
ENV_TIMEOUT_S = "TRAJECTORY_LLM_TIMEOUT_S"
ENV_MAX_TOKENS = "TRAJECTORY_LLM_MAX_TOKENS"
#: **熔断阈值**（H-31）。连续这么多次不可重试的 4xx ⇒ 判定「本批 LLM
#: 不可用」⇒ 该批判分不可信、不许并池。
ENV_CIRCUIT_THRESHOLD = "TRAJECTORY_LLM_CIRCUIT_THRESHOLD"

#: 升配后端（方案 13 §3.5 G2）。EscalateModel 处置触发时 agent 循环从主
#: 后端切到这一个——**全部可选**：一个都没配 = 无升配能力（处置降级为
#: 只落事件不切后端，不 fail）；只配了一半（有 BASE_URL 没 MODEL 或反之）
#: = 配置错误，**报错不猜**——与主后端同一条「缺件即报错」纪律：猜一个
#: 默认模型的后果是升配悄悄失效，而报表上只会看到「这批 unresolved 多」，
#: 没人会想到去查升配压根没接上。
#:
#: 刻意**不复用主后端的三键**：主后端与升配后端是两台机器两份模型
#: （典型形态：主 = 本地小模型省钱，升配 = 远端大模型救场），共用键名
#: 就意味着升配只能升向「同一台机器的另一个模型」，那不是能力限制，
#: 是键名偷换了设计。
ENV_ESCALATE_BASE_URL = "TRAJECTORY_LLM_ESCALATE_BASE_URL"
ENV_ESCALATE_MODEL = "TRAJECTORY_LLM_ESCALATE_MODEL"
ENV_ESCALATE_API_KEY = "TRAJECTORY_LLM_ESCALATE_API_KEY"

#: 默认超时。**跑批校准值，30 → 90**（2026-10-11）：
#:
#: 30s 对判断点 ① 不够——① 的请求送进模型的是整个搜索结果链接窗口
#: （实测 5 条里超时 3 条，见 docs/04-运行手册.md），而 ① 发生在
#: **搜索阶段**、不受 ``RunConfig.per_site_timeout_s`` 的单站遍历约束，
#: 给 90s 不会拖垮整批。站点内的 ②③④ 每次只送一个页面，正常远快于
#: 90s；万一异常拖满，由 ``per_site_timeout_s``（90s，``executor/
#: orchestrator.py``）整体兜底记 ``unresolved``——止损优先，不猜。
#:
#: 环境变量 ``TRAJECTORY_LLM_TIMEOUT_S`` 仍可覆盖此默认值。
DEFAULT_TIMEOUT_S = 90.0

#: 本机 vLLM / llama.cpp 不校验 key 时占位用。**不是**真实凭据。
NO_KEY = "not-needed"

#: 单次请求的输出预算。**默认值是照着推理模型定的，不是照着非推理模型**。
#:
#: 实测（2026-10-09，本机 `MiniMax-M3.1-Flash-Preview`）同一个判断点 ①
#: 的请求，只改这一个数：
#:
#:     max_tokens= 600  finish_reason=length  content=null    （599 tok 全花在思考上）
#:     max_tokens=2048  finish_reason=length  content 有但被截断
#:     max_tokens=8192  finish_reason=stop    content 干净（2096 tok）
#:
#: 原来的 600 是按「回一个几百字的 JSON」估的，而**推理模型的思考与正文
#: 共用同一个预算**：思考没跑完，正文就一个字都吐不出来。而客户端按红线
#: **故意不读** ``reasoning_content``（那是 raw CoT），于是正文为空这件事
#: 最终只表现为一句 ``shape:ValueError``——看不出是「预算不够」还是
#: 「后端坏了」，两者要采取的动作完全不同。
#:
#: 非推理模型用这个值只是白花点额度，不会有别的坏处。
DEFAULT_MAX_TOKENS = 8192


class LLMUnavailable(RuntimeError):
    """后端不可达或配置缺失。

    只在**构造**阶段抛出（配置问题，改环境变量即可）。
    请求期的失败不走异常，见 :meth:`LLMClient.complete` 的契约。
    """


class _BudgetExhausted(ValueError):
    """输出预算用尽：``finish_reason == "length"`` 且正文为空。

    **单独一个异常类型，因为处置动作完全不同。** 同一现象在
    ``_BudgetExhausted`` 下要调大 :data:`ENV_MAX_TOKENS`，在普通
    ``ValueError`` 下要去查后端——而混成一句 ``shape:ValueError``
    时，两者看起来一模一样（都是「调用成功、拿不到内容」），
    于是每道题都返回 ``None``，症状像「LLM 判不了」实则预算不够。

    继承 ``ValueError`` 是为了不改动 :meth:`LLMClient.chat` 既有的
    兜底 ``except`` 列表；但 :meth:`chat` **先**接这一类。
    """


@dataclass(frozen=True, slots=True)
class ChatResult:
    """一轮**多轮**对话的结果——:meth:`LLMClient.chat_messages` 的返回类型。

    与 :meth:`LLMClient.chat` 的差别只在**返回得更多**，失败语义一字不差：
    ``content is None`` 与 ``chat`` 返回 ``None`` 同义（判不出来 / 后端
    不可用），调用方一律按 fail-closed 处理。⚠️ 在 agent 循环里尤其不能把
    ``None`` 当成「模型说放弃」——那是两种事实：「没作答」（要重试引导）
    与「答了放弃」（要收口 unresolved）。

    ``usage`` / ``latency_ms`` 是给 ``RunRecord.llm_calls[]`` 账本的
    （方案 13 §3.6）：agent 循环每步一次调用，没有 token 与耗时就**比不出
    批次成本**，而比不出来的 prompt 调优只能停在拍脑袋阶段。
    """

    content: str | None
    #: ``{"prompt_tokens": int, "completion_tokens": int}``。后端没给就是
    #: ``None``——**不补 0**，理由见 :func:`_usage_of`。
    usage: dict[str, int] | None
    latency_ms: int
    #: 与 :attr:`LLMClient.last_error` 同格式（``transport:`` / ``http:`` /
    #: ``shape:`` / ``budget:``）。**空串 = 成功**。
    error: str = ""


#: **熔断阈值**：连续 N 次「不可重试的 4xx」⇒ 判定「本批 LLM 不可用」。
#:
#: ⚠️ **为什么是 3**：401 这类失败**重发一次结果一样**（key 错了）⇒
#: 连着 3 次还错就不是偶发，是**配置/后端真的坏了** ⇒ 继续发只是把
#: 每一道题都拖到超时（all80 批次里 401 期间 **170 次**空请求、
#: 判分全程 fail-closed，而那批的字节量占全批 **35 %**——结构完整、
#: 看着最像正常轨迹、实则判分不可信）。
#:
#: 阈值取 3 而不是 1：偶发一次 4xx（后端重启、网关抖动）不该让整批判废。
CIRCUIT_BREAKER_THRESHOLD = 3

#: **不计入熔断的 4xx**：它们说的是「慢一点 / 再等会儿」，不是「你错了」。
#:
#: - ``408`` Request Timeout：请求超时⇒换个时间能成；
#: - ``429`` Too Many Requests：**限流**⇒退避后能成。
#:
#: ⚠️ 把 429 算进熔断会让「后端限流」被误判成「后端坏了」⇒
#: 整批判分不可信——**判错的代价比多发几次请求大得多**。
NON_TRIPPING_4XX: frozenset[int] = frozenset({408, 429})


class CircuitBreaker:
    """**连续 4xx ⇒ 熔断**（2026-11-11，H-31 · all80 事故 P1-3）。

    熔断的语义是「**别再发了**」，不是「重试」——
    :meth:`LLMClient._exchange` 对 4xx **不重试**那个决策是对的
    （key 错了重发无用），缺的是「**别再发**」。

    ⚠️ **为什么熔断器是独立对象、而不是客户端的一个字段**：
    客户端是**每 task 一个**（见 :func:`trajectory_pipeline.perception.factory.build_perceptor`
    与 CLI 的 rationale writer），而熔断要拦的是**整批**——
    all80 那 170 次 401 是**跨 6 条 task** 打空的，逐 task 计数拦不住。
    ⇒ 熔断器由**批次层**建一个，传给这一批的每个客户端。

    ⚠️ **fail-closed**：熔断打开后 :meth:`LLMClient.chat` 返回 ``None``
    （**不是抛异常**）——本模块的契约是「失败不是异常，是返回值」
    （见模块 docstring 第 1 条），而调用方（感知层）的契约是 I4
    fail-closed，它需要「判不出来」这个信号。
    """

    def __init__(self, threshold: int = CIRCUIT_BREAKER_THRESHOLD) -> None:
        #: 连续失败几次就熔断。**至少 1**（0 会让第一次失败就熔断，
        #: 而 :meth:`record_4xx` 自己会挡掉非正阈值）。
        self.threshold = max(1, int(threshold))
        #: 当前连续失败数。
        self.consecutive_4xx = 0
        #: 熔断发生时**最后那个** 4xx 的状态码（诊断用；0 = 没熔断过）。
        self.last_status = 0
        #: 熔断发生过几次（**只增不减**——它是「这批出过事」的读数）。
        self.tripped = 0

    @property
    def open(self) -> bool:
        """熔断是否已打开。**打开 ⇒ 后续调用一律不发请求**。"""
        return self.consecutive_4xx >= self.threshold

    def record_4xx(self, status: int) -> None:
        """记一次「不可重试的 4xx」。"""
        if status in NON_TRIPPING_4XX:
            return
        self.consecutive_4xx += 1
        self.last_status = int(status)
        if self.open and self.tripped == 0:
            self.tripped = 1

    def record_ok(self) -> None:
        """记一次成功。**连续计数清零**（后端恢复了 ⇒ 前面的失败不算数）。"""
        self.consecutive_4xx = 0

    def reset(self) -> None:
        """手动复位（操作员修好配置后重跑时用）。"""
        self.consecutive_4xx = 0
        self.last_status = 0

    def reason(self) -> str:
        """一句话说明熔断原因（进日志与存档）。"""
        if not self.open:
            return ""
        return (f"LLM 熔断：连续 {self.consecutive_4xx} 次 4xx"
                f"（最后 http:{self.last_status}）")


# ════════════════════════════════════════════════════════════════════
# 失败归因：错误码 → 分类 / 一句话说明
# ════════════════════════════════════════════════════════════════════
#
# ⚠️ **这里是错误码语义的唯一真源**。错误码的形状由
# :meth:`LLMClient._exchange` 决定（``circuit:`` / ``budget:`` /
# ``shape:`` / ``http:<status>`` / ``transport:<ExcType>``），而
# 「这个码意味着什么」只有这里知道。下游（scorer / scout / 报告侧）
# **只消费分类**，不许各造一份码表——抄两份必然漂，而漂了的代价是
# 「后端 500」与「后端超时」在报告上长得一模一样（2026-11-11 那批
# 578 次失败全被压成一句「LLM 返回空」，就是这个漂）。

#: 没失败（错误码为空串）。
LLM_FAILURE_NONE = ""
#: 熔断打开——别再发了。
LLM_FAILURE_CIRCUIT = "circuit"
#: ``max_tokens`` 用尽（推理模型的思考与正文共用这一个预算）。
LLM_FAILURE_BUDGET = "budget"
#: 200 但结构不对 / 正文为空。
LLM_FAILURE_SHAPE = "shape"
#: 4xx（除 429/408）——请求本身的问题（key 错、模型名错、参数错）。
LLM_FAILURE_CLIENT = "client"
#: 5xx——后端过载 / 崩溃。
LLM_FAILURE_SERVER = "server"
#: 429 限流——**可能自愈**，退避后还有机会。
LLM_FAILURE_RATE_LIMIT = "rate_limit"
#: 408 或传输层超时。
LLM_FAILURE_TIMEOUT = "timeout"
#: 连不上 / DNS 等传输层失败（非超时）。
LLM_FAILURE_TRANSPORT = "transport"
#: 认不出的码——**不猜**，按 unknown 报。
LLM_FAILURE_UNKNOWN = "unknown"


def classify_llm_failure(code: str) -> str:
    """错误码 → 归因分类。

    Args:
        code: :attr:`LLMClient.last_call_error` 的值（空串 = 没失败）。

    Returns:
        :data:`LLM_FAILURE_*` 之一。**认不出的码返回**
        :data:`LLM_FAILURE_UNKNOWN`——不猜它属于哪一类（猜了就会把
        「后端挂了」说成「参数错了」，处置方向正好相反）。
    """
    c = (code or "").strip()
    if not c:
        return LLM_FAILURE_NONE
    if c == "circuit:open":
        return LLM_FAILURE_CIRCUIT
    if c.startswith("budget:"):
        return LLM_FAILURE_BUDGET
    if c.startswith("shape:"):
        return LLM_FAILURE_SHAPE
    if c.startswith("http:"):
        try:
            status = int(c.split(":", 1)[1])
        except ValueError:
            return LLM_FAILURE_UNKNOWN
        if status == 429:
            return LLM_FAILURE_RATE_LIMIT
        if status == 408:
            return LLM_FAILURE_TIMEOUT
        if 400 <= status < 500:
            return LLM_FAILURE_CLIENT
        if status >= 500:
            return LLM_FAILURE_SERVER
        return LLM_FAILURE_UNKNOWN
    if c.startswith("transport:"):
        if "timeout" in c.lower():
            return LLM_FAILURE_TIMEOUT
        return LLM_FAILURE_TRANSPORT
    return LLM_FAILURE_UNKNOWN


def describe_llm_failure(code: str) -> str:
    """错误码 → 一句中文说明（进 reason / 报告 / 存档）。

    空码 → 「LLM 返回空」——**保留旧口径**：调用点（与既有测试）认这句，
    而「为什么返回空」由码本身说明（带码时才有）。
    """
    kind = classify_llm_failure(code)
    c = (code or "").strip()
    if kind == LLM_FAILURE_NONE:
        return "LLM 返回空"
    if kind == LLM_FAILURE_CIRCUIT:
        return f"LLM 熔断（{c}）"
    if kind == LLM_FAILURE_BUDGET:
        return f"LLM 预算耗尽（{c}）"
    if kind == LLM_FAILURE_SHAPE:
        return f"LLM 返回形状异常（{c}）"
    if kind == LLM_FAILURE_CLIENT:
        return f"LLM 请求被拒（{c}）"
    if kind == LLM_FAILURE_SERVER:
        return f"LLM 后端错误（{c}）"
    if kind == LLM_FAILURE_RATE_LIMIT:
        return f"LLM 后端限流（{c}）"
    if kind == LLM_FAILURE_TIMEOUT:
        return f"LLM 调用超时（{c}）"
    if kind == LLM_FAILURE_TRANSPORT:
        return f"LLM 连接失败（{c}）"
    return f"LLM 调用失败（{c}）"


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """后端连接参数。``api_key`` 刻意不进 ``__repr__``。"""

    base_url: str
    model: str
    api_key: str = field(repr=False, default=NO_KEY)
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_retries: int = 2
    temperature: float = 0.0
    max_tokens: int = DEFAULT_MAX_TOKENS
    #: **熔断阈值**（H-31）。连续这么多次不可重试的 4xx ⇒ 熔断。
    #: 客户端**不自带**这个判据——它由批次层建的 :class:`CircuitBreaker`
    #: 持有（一个批次一个），配置里的这个数只是它的**默认值**。
    circuit_breaker_threshold: int = CIRCUIT_BREAKER_THRESHOLD

    def __repr__(self) -> str:
        # 覆盖 dataclass 生成的 repr：它会把 api_key 打进 repr，
        # 而 config 一旦被 log 或 traceback 带出去就等于泄漏凭据。
        return (
            f"LLMConfig(base_url={self.base_url!r}, model={self.model!r}, "
            f"api_key=<redacted>, timeout_s={self.timeout_s})"
        )

    @property
    def endpoint(self) -> str:
        """chat-completions 完整地址。容忍尾斜杠与缺 ``/v1`` 两种写法。"""
        base = self.base_url.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return f"{base}/chat/completions"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LLMConfig:
        """从环境变量读配置。**缺 base_url 或 model 即报错，不猜。**

        与 ``OBSCURA_EXE`` 同一条纪律：猜一个默认端点意味着「配置错了
        但程序照跑」，而它连的是哪台机器只有猜的人才知道——这类失败会
        一路静默到「判定全是 None」，报表上看起来像 LLM 判不了，
        实际是根本没连上。
        """
        e = os.environ if env is None else env
        base_url = (e.get(ENV_BASE_URL) or "").strip()
        model = (e.get(ENV_MODEL) or "").strip()
        missing = [
            n for n, v in ((ENV_BASE_URL, base_url), (ENV_MODEL, model)) if not v
        ]
        if missing:
            raise LLMUnavailable(
                f"LLM 后端未配置：缺少环境变量 {'、'.join(missing)}。"
                f"本项目**不提供默认端点**——猜一个的后果是判定全部落 None，"
                f"而报表上看不出「没连上」与「判不了」的区别。"
            )
        raw_timeout = (e.get(ENV_TIMEOUT_S) or "").strip()
        try:
            timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_S
        except ValueError as exc:
            raise LLMUnavailable(f"{ENV_TIMEOUT_S}={raw_timeout!r} 不是数字") from exc
        if timeout <= 0:
            raise LLMUnavailable(f"{ENV_TIMEOUT_S}={timeout} 必须为正")
        raw_tokens = (e.get(ENV_MAX_TOKENS) or "").strip()
        try:
            tokens = int(raw_tokens) if raw_tokens else DEFAULT_MAX_TOKENS
        except ValueError as exc:
            raise LLMUnavailable(f"{ENV_MAX_TOKENS}={raw_tokens!r} 不是整数") from exc
        if tokens <= 0:
            raise LLMUnavailable(f"{ENV_MAX_TOKENS}={tokens} 必须为正")
        raw_cb = (e.get(ENV_CIRCUIT_THRESHOLD) or "").strip()
        try:
            threshold = int(raw_cb) if raw_cb else CIRCUIT_BREAKER_THRESHOLD
        except ValueError as exc:
            raise LLMUnavailable(
                f"{ENV_CIRCUIT_THRESHOLD}={raw_cb!r} 不是整数"
            ) from exc
        if threshold <= 0:
            raise LLMUnavailable(f"{ENV_CIRCUIT_THRESHOLD}={threshold} 必须为正")
        return cls(
            base_url=base_url,
            model=model,
            api_key=(e.get(ENV_API_KEY) or "").strip() or NO_KEY,
            timeout_s=timeout,
            max_tokens=tokens,
            circuit_breaker_threshold=threshold,
        )

    @classmethod
    def escalate_from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        base: "LLMConfig | None" = None,
    ) -> "LLMConfig | None":
        """读升配后端配置（方案 13 §3.5 G2，A2a-4）。

        三态语义，与主后端的「缺件即报错」同纪律但**多一个「没配」档**——
        升配是能力不是依赖（§3.5），未配置不 fail：

        - 三个键**一个都没配** → ``None`` = 无升配。EscalateModel 处置
          降级为只落事件不切后端，agent 循环在 ``escalate=None`` 时本来就
          这么跑（``_maybe_escalate``），所以这里返回 None 是**闭环的**，
          不是半个状态；
        - BASE_URL 与 MODEL**只配了一半** → 抛 :class:`LLMUnavailable`。
          半配是配置错误：用户显然**想**升配但没配齐，静默当无升配处理，
          那一半配置就成了永远没人读的尸体——而想升配的人看到「升配没
          生效」时不会去查「是不是只配了一半」；
        - API_KEY 没配 → :data:`NO_KEY` 占位（与主后端同款：本机
          vLLM / llama.cpp 不校验 key）。

        ``base`` 给了就**继承 ``timeout_s`` / ``max_tokens``**：升配后端
        接的是同一类请求（agent 动作指令 + 推理模型的思考预算），主后端
        上校准过的输出预算原样带过去。继承的是「已校准值」而不是
        「类默认值」——升配通常是远端大模型，烧的每一 token 都更贵，
        预算被类默认值悄悄放大或缩小的代价都是真金白银。timeout 同理：
        executor 的 ``per_site_timeout_s`` 是按主后端 90s 校准的链路
        预算，升配后端不该比它更松。
        """
        e = os.environ if env is None else env
        base_url = (e.get(ENV_ESCALATE_BASE_URL) or "").strip()
        model = (e.get(ENV_ESCALATE_MODEL) or "").strip()
        api_key = (e.get(ENV_ESCALATE_API_KEY) or "").strip() or NO_KEY
        if not base_url and not model and api_key == NO_KEY:
            return None
        missing = [
            n for n, v in (
                (ENV_ESCALATE_BASE_URL, base_url),
                (ENV_ESCALATE_MODEL, model),
            )
            if not v
        ]
        if missing:
            raise LLMUnavailable(
                f"升配后端配置不完整：缺少环境变量 {'、'.join(missing)}。"
                f"三个 {ENV_ESCALATE_BASE_URL.rsplit('_BASE_URL', 1)[0]}_* 键"
                f"要么全配、要么全不配——只配一半说明想升配但没配齐，"
                f"静默当无升配处理，已配的那一半就成了没人读的尸体配置。"
            )
        kwargs: dict[str, Any] = {}
        if base is not None:
            kwargs.update(timeout_s=base.timeout_s, max_tokens=base.max_tokens)
        return cls(base_url=base_url, model=model, api_key=api_key, **kwargs)


class LLMClient:
    """同步 HTTP 客户端。协议 :class:`chat`。

    刻意**同步**：W3 的调用点全在 ``async def`` 里但都是串行的
    （一道题答完才问下一道），引入异步只会让「超时/重试/降级」
    三个 fail-closed 分支各自多一套异常路径。
    """

    def __init__(
        self,
        cfg: LLMConfig,
        *,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.cfg = cfg
        self._client: Any = None  # 懒建，见 _ensure_client
        #: **熔断器**（H-31）。不给就自带一个——但**自带的那个只管
        #: 这一个客户端**，而客户端是**每 task 一个** ⇒ 跨 task 的
        #: 连续失败拦不住（all80 那 170 次 401 就是跨 task 打空的）。
        #: ⇒ **批次层必须显式传一个共享的进来**。
        self.breaker = breaker if breaker is not None else CircuitBreaker(
            cfg.circuit_breaker_threshold
        )
        #: 上一次失败的**简短原因**，供排障。不含响应体、不含 key——
        #: 后端回的错误正文偶尔带凭据片段，而异常与诊断都会进日志。
        self.last_error: str = ""
        #: **最近一次调用**的账本读数（2026-11-05，P0-1）。``chat`` 与
        #: ``chat_messages`` 每次调用后都会刷新这三个字段，供不走
        #: ``ChatResult`` 的调用点（感知层、rationale writer）把成本汇入
        #: ``RunRecord.llm_calls``。
        #:
        #: ⚠️ **它们只描述「本客户端最近一次调用」**：同一个客户端被两个
        #: 线程并发使用时会互相覆盖。并发调用点必须各用各的客户端
        #: （writer 与 reflection 已按此拆分，见 ``executor/cli.py``）。
        self.last_latency_ms: int = 0
        #: ``{"prompt_tokens": int, "completion_tokens": int}``；后端没给
        #: 就是 ``None``——**不补 0**，与 :func:`_usage_of` 同口径。
        self.last_usage: dict[str, int] | None = None
        #: 最近一次调用的错误码（空串 = 成功）。与 :attr:`last_error` 的
        #: 区别：后者只在失败时写、成功不清空（排障语义），这里每次都写。
        self.last_call_error: str = ""

    def __repr__(self) -> str:
        return f"LLMClient({self.cfg!r})"

    # ── 能力自述 ──────────────────────────────────────────────────

    def health(self) -> tuple[bool, str]:
        """连通性自述。不发请求——只校验配置齐全。

        真正的「后端活着吗」要发一次请求才知道，而那属于
        :meth:`complete` 的失败语义，不该由 health 代劳。
        """
        return True, f"{self.cfg.model} @ {self.cfg.base_url}"

    # ── 主入口 ────────────────────────────────────────────────────

    def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
    ) -> str | None:
        """发一轮对话，返回**模型原文**，或 ``None``（不可用）。

        返回原文而不是解析后的结构体：本模块不懂「什么是 judgment」，
        那是 :mod:`~trajectory_pipeline.llm.schema_parse` 与感知层的事。
        中间这层一旦替调用方猜结构，解析失败就会退化成「悄悄少一个字段」。

        ``max_tokens`` **默认走 :attr:`LLMConfig.max_tokens`，不给函数签名
        一个数字默认值**。给了的话调用点就会各写各的（本项目就曾写死 600），
        而推理模型思考与正文共用这一个预算，写死的数字必然在某个后端上
        不够——症状是「正文为空」，不是「这个数小了」，所以没人会去改它。
        传 ``None`` 之外的整数只为**排查**用（量边界），生产调用点不该带它。

        .. note::

           2026-10-17 A2a-1：请求循环抽到 :meth:`_exchange`，本方法成了
           薄壳（组装两条消息 + 取 ``content``）。**外部契约一字未动**
           ——十一道判断题与 ``FakeLLM`` 都还走这里。
        """
        started = time.perf_counter()
        content, usage, error = self._exchange(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
        )
        self.last_latency_ms = int((time.perf_counter() - started) * 1000)
        self.last_usage = usage
        self.last_call_error = error
        if error:
            # 只在失败时写：``last_error`` 的语义是「上一次失败的原因」，
            # 成功不清空（原实现即是如此，diag 路径依赖这个语义）。
            self.last_error = error
        return content

    def chat_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """发一轮**多轮**对话。agent 循环（方案 13 §3.2）的调用入口。

        与 :meth:`chat` 是**同一个出口、同一套失败语义**，差别只有两点：
        消息由调用方给全、返回值多带账本字段。十一道判断题仍走 ``chat``
        （单轮两消息形态被它们共用，不动）。

        三个刻意的设计：

        1. **不加 ``tools`` / ``tool_choice``。** D-B1 拍板用 JSON 动作
           指令走文本通道：原生 tool_calls 要改 payload 与响应解析两处，
           而 P3 视图由 ``views.py`` 从 Action 渲染，JSON 指令经
           Action→ActionView 与 scripted **完全同形**，P3 契约零风险。
        2. **``messages`` 原样透传**（OpenAI 形态的 ``{role, content}``）。
           客户端不合并 system、不裁剪历史——那是 agent_loop 的事。在这里
           加工会让「模型实际看到了什么」有**两份真相**：一份在调用方
           组装的列表里，一份在客户端加工之后。
        3. **失败不抛，仍是返回值。** 与 :meth:`chat` 同一条纪律（模块头
           第 1 条）：调用方要的是「判不出来」这个信号，不是异常。

        ⚠️ ``content is None`` 时账本里 ``usage`` 也必然是 ``None``
        ——一次没跑完的请求没有可信的 token 数。
        """
        started = time.perf_counter()
        content, usage, error = self._exchange(messages, max_tokens=max_tokens)
        latency_ms = int((time.perf_counter() - started) * 1000)
        # 与 ``chat`` 共用同一组「最近一次调用」读数：本方法虽自带
        # ``ChatResult``，但把字段刷新一遍，客户端上就不会残留上一条
        # 单轮调用的旧数。
        self.last_latency_ms = latency_ms
        self.last_usage = usage
        self.last_call_error = error
        if error:
            self.last_error = error
        return ChatResult(
            content=content,
            usage=usage,
            latency_ms=latency_ms,
            error=error,
        )

    # ── 内部 ──────────────────────────────────────────────────────

    def _exchange(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int | None,
    ) -> tuple[str | None, dict[str, int] | None, str]:
        """发一轮请求，返回 ``(content, usage, error)``。

        :meth:`chat` 与 :meth:`chat_messages` **共用这一份**重试与失败分类：
        它们只有「消息从哪来」「返回多少」两点不同，而重试策略分叉的后果
        是「某一类失败只在其中一条路径上被正确处理」——这类缺陷要等真实
        后端出状况才暴露，代价是一次批次。
        """
        if not messages:
            # 调用方的装配错误，不是后端的问题。发一次 90s 超时只为
            # 换一个 400，不值。
            return None, None, "shape:empty_messages"
        if self.breaker.open:
            # ⚠️ **熔断已打开 ⇒ 一个请求都不发**（H-31 · all80 事故 P1-3）。
            #
            # all80 批次里 401 从 08:48:39 持续到收尾，共 **170 次**空请求
            # ——每次都等一个超时。⇒ 判分全程 fail-closed，那批的
            # ``unresolved`` 占 83 %、**字节量占全批 35 %**：结构完整、
            # 看着最像正常轨迹、实则判分不可信。
            #
            # ⚠️ **fail-closed，不抛异常**：本模块的契约是「失败不是异常，
            # 是返回值」（模块 docstring 第 1 条），调用方（感知层）的
            # 契约是 I4 fail-closed，它需要「判不出来」这个信号。
            # ⇒ 返回 ``None`` + ``circuit:open``，而 ``circuit:open``
            # 会原样进 ``llm_calls[].error`` ⇒ 读档的人看得见
            # 「**不是判不了，是熔断了**」。
            return None, None, "circuit:open"
        payload = {
            "model": self.cfg.model,
            "messages": list(messages),
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
        }
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key and self.cfg.api_key != NO_KEY:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"

        error = ""
        for attempt in range(self.cfg.max_retries + 1):
            try:
                resp = self._ensure_client().post(
                    self.cfg.endpoint, json=payload, headers=headers
                )
            except Exception as exc:
                # 传输层异常：连接失败/超时/DNS/后端进程挂了。重试有意义。
                # 只留异常**类型名**——httpx 的异常串里可能带 URL 里的
                # 查询参数，而 key 有时被塞在 URL 上而不是 header 里。
                error = f"transport:{type(exc).__name__}"
            else:
                if resp.status_code >= 400:
                    error = f"http:{resp.status_code}"
                    # 4xx 与 5xx 都不重试，但原因不同：
                    #   4xx = 请求本身的问题（401 key 错、404 模型名错、400 参数错），
                    #         再发一次结果一样，纯烧时间。
                    #   5xx = 后端过载/崩溃，重试有机会成。
                    if resp.status_code < 500:
                        # ⚠️ **记一次熔断计数**（H-31）。4xx 不重试是对的，
                        # 但**「不再发」也要有**——否则 401 期间每一道题
                        # 都各发一次（all80：170 次）。
                        self.breaker.record_4xx(resp.status_code)
                        return None, None, error
                else:
                    try:
                        data = resp.json()
                        usage = _usage_of(data)
                        content = _content_of(data)
                    except _BudgetExhausted as exc:
                        # 预算用尽：**换参数就能好**，所以不重试同一请求
                        # （重发一次还是同一个预算、还是空）。给的是能照着
                        # 做的提示，不是异常类名。
                        error = f"budget:{exc}"
                        return None, None, error
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        # 后端回了 200 但内容结构不对。**不重试**：
                        # 解析失败几乎总是源于模型稳定输出的某种坏格式，
                        # 再问一次它还是那样，重试只是把 30s 超时翻倍。
                        error = f"shape:{type(exc).__name__}"
                        return None, None, error
                    # ⚠️ **成功 ⇒ 连续失败计数清零**（后端恢复了）。
                    # 放在解析之后：拿到内容才算「这次成功」。
                    self.breaker.record_ok()
                    return content, usage, ""
            if attempt < self.cfg.max_retries:
                time.sleep(min(2.0 * (attempt + 1), 5.0))

        # 重试用尽。返回 None 而不是抛异常：调用方（感知层 / agent 循环）
        # 的契约是 I4 fail-closed，它需要「判不出来」这个信号。
        return None, None, error

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - 环境问题
                raise LLMUnavailable("需要 httpx>=0.27") from exc
            self._client = httpx.Client(timeout=self.cfg.timeout_s)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _content_of(data: Any) -> str:
    """从 OpenAI 兼容响应里取 ``choices[0].message.content``。

    刻意**不兜底到 ``choices[0].text`` 或 ``message.reasoning_content``**：
    后者是要被丢掉的 raw CoT（CLAUDE.md 红线），取它等于把思维链
    送进训练数据。

    正文为空时按 ``finish_reason`` 分两类：``"length"`` 说明预算被
    ``max_tokens`` 吃光（推理模型的思考也走这个预算，见
    :data:`DEFAULT_MAX_TOKENS`），抛 :class:`_BudgetExhausted`；
    其余按结构不对处理。
    """
    choice = data["choices"][0]
    finish = choice.get("finish_reason")
    content = choice["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        if finish == "length":
            raise _BudgetExhausted(
                f"max_tokens 用尽（finish_reason=length，content 为空）；"
                f"调大 {ENV_MAX_TOKENS}"
            )
        raise ValueError("content 为空")
    return content


def _usage_of(data: Any) -> dict[str, int] | None:
    """从响应里取 token 计数。**取不到就 None，不补零。**

    为什么不补 0：账本上「0」与「后端没给」是两个事实。补零后「这一批
    压根没记到 token」会看起来像「这一批一个 token 没花」，于是成本对比
    先错在**分母**上，而这个错不报任何警——它只是让比分看起来很好看。

    只认 ``int``，且**显式排除 ``bool``**（它是 ``int`` 的子类）：字符串
    数字与浮点一律当没给。转换规则一旦开始放宽，各后端各写一种写法，
    账本就变成不可比的——而账本唯一的价值就是可比。

    ``total_tokens`` 刻意不取：它能由两者相加得到，记下来就多一份要与之
    同步的东西（方案 13 §3.6 的账本只声明了两个键）。
    """
    if not isinstance(data, Mapping):
        return None
    raw = data.get("usage")
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        out[key] = value
    return out or None


def probe(cfg: LLMConfig, timeout_s: float = 5.0) -> dict[str, Any]:
    """一次性连通性探针——``/models`` 拿模型清单。

    存在的理由与 ``executor`` 下的 ``mcp_probe`` 同源：**上游漂移要能
    立刻看出来**。模型名写错时的症状是每道题都返回 ``None``，
    看起来像「LLM 判不了」，实际是 404。
    """
    try:
        import httpx
    except ImportError as exc:
        raise LLMUnavailable("需要 httpx>=0.27") from exc
    headers = {}
    if cfg.api_key and cfg.api_key != NO_KEY:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    base = cfg.base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    with httpx.Client(timeout=timeout_s) as c:
        resp = c.get(f"{base}/models", headers=headers)
        resp.raise_for_status()
        return resp.json()


def dump_for_debug(cfg: LLMConfig) -> str:
    """给日志用的配置摘要——**永远不含 key**。"""
    return f"{cfg.model} @ {cfg.base_url} (timeout={cfg.timeout_s}s)"
