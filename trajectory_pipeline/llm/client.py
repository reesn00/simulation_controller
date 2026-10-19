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
        return cls(
            base_url=base_url,
            model=model,
            api_key=(e.get(ENV_API_KEY) or "").strip() or NO_KEY,
            timeout_s=timeout,
            max_tokens=tokens,
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

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self._client: Any = None  # 懒建，见 _ensure_client
        #: 上一次失败的**简短原因**，供排障。不含响应体、不含 key——
        #: 后端回的错误正文偶尔带凭据片段，而异常与诊断都会进日志。
        self.last_error: str = ""

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
        content, _usage, error = self._exchange(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
        )
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
        if error:
            self.last_error = error
        return ChatResult(
            content=content,
            usage=usage,
            latency_ms=int((time.perf_counter() - started) * 1000),
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
                        return None, None, error
                else:
                    try:
                        data = resp.json()
                        return _content_of(data), _usage_of(data), ""
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
