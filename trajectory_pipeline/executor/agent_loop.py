"""agent_loop——**站内导航语义段**的自主循环（方案 12 阶段 A2 / 方案 13 §3）。

三种任务，一个循环（A2b）
==========================
:data:`TASK_KINDS` 三个值共用**同一份动作契约、同一段流水线、同一套
拦截与预算**，换的只有任务目标句与终局产出形态：

===== ============ ==================== ==================
kind 目标          终局                  产出
===== ============ ==================== ==================
play 站内找片      到达正片播放页        ``final_obs``
entries 聚合站找入口 找到详情页入口链接   ``selected``
referral 文章找引荐  找到引荐外链         ``selected``
===== ============ ==================== ==================

分成三个模块各自维护会让它们在某一天分叉，而分叉那天两个任务的轨迹
就不再可比——§4「同一循环换任务指令」说的就是这个。

职责边界（D-A5 的落地形态）
==========================
agent 包「进站后 → 走到目标 / 报出链接」这一段。它的权力只有一项：**页面内
选哪个元素、goto 哪个链接**。两端都在代码：

- **进站前**：可达性 probe 与进站拦截由调用方（A2a-3 的 ``agent_visit``）
  完成——硬不可达与反爬不该进 agent 对话，那是确定性事实，问模型也白烧
  一次 llm_call；
- **点击后**：复观 + ``PLAYER_OK`` 判断题照旧由代码做——**成功判定不交给
  agent**。

所以本模块的产物不是分支，是 :class:`AgentOutcome`——「到了 / 放弃了 /
被代码收口了」三种事实之一。分支折算 :

- ``reached=True``  → **不落分支**（``branch=""``），由调用方拿去跑
  ``PLAYER_OK`` 判断题，成功/``player_unverified``/负分支都是那条链的产出；
- ``gave_up``       → ``unresolved``（**无证据不记负样本**）；
- ``fail_closed``   → 查 :data:`_FALCLOSED_BRANCH` 映射表。

**agent 永远不直接产出分支**（§3.3 核心规则）。这不是洁癖：22 分支封闭集
是 P2 / P3 与 d5_scorer 的共同地基，让它随 LLM 输出浮动，等于把评估口径
交给不可复现的东西。

确定性纪律（方案 12 §5.3 第 1 条）
================================
非确定性被锁死在「选哪个元素」这一段，其余全是代码：

- 步数预算、llm_calls 预算、终止判定、处置映射、事件落盘——代码；
- 每步先 observe 再决策，**ref 白名单在工具层强制**（I6），不是提示词层；
- 处置只有三档（``InjectGuidance`` / ``EscalateModel`` / ``FailClosed``），
  没有静默自愈（D-A2：「被拦 → 修复 → 重试」本身就是训练信号，抹掉等于
  伪造轨迹）。三档全部落事件，一条不落。

依赖方向：为什么本模块不 import ``llm``
======================================
``common ← llm ← {taskgen, perception, rationale}``，而 ``executor`` 只依赖
``perception`` 的协议（01-架构总览 L26-50）。动作指令的 **text → JSON**
属于 llm 侧的共享资产（``llm/schema_parse`` 的 ``strip_think`` +
``extract_json``），直接 import 会给 ``executor → llm`` 新增一条边。
所以按 :data:`~trajectory_pipeline.executor.orchestrator.Orchestrator` 里
``rationale_writer`` 同款姿势**由调用方注入** ``json_of``：

- **格式契约（schema）在 loop 侧**：字段缺不缺、ref 在不在白名单——这是
  执行器的事，它才知道哪些 ref 是真的；
- **文本解析在 llm 侧**：剥 ``<think>``、抠 fence、宽容修复——那是共享
  资产，本模块复制一份就会有两份解析，而两份解析必然在某天分叉。

顺带一条红线性质的收益：**模型原文从不进本模块**。进对话历史的是解析后
**重新序列化**的规范 JSON（:func:`_canonical`），未解析成功的输出根本不
落。于是 raw CoT 没有路径从 LLM 流进 P1/P2——不靠调用方记得先剥。

同步纪律
========
:meth:`LLMClient.chat_messages` 是**同步**的（httpx 同步客户端），在 async
循环里直接调用——与 ``perceptor.decide`` 在 ``visit_site`` 里的姿势一致。
包成 ``asyncio.to_thread`` 的改造列入 O 阶段（Langfuse 前置）扩展项。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Final, Literal, Mapping

from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.executor.gatekeeper import (
    DeadLoopDetector,
    EscalateModel,
    FailClosed,
    InjectGuidance,
    Verdict,
    intercept,
)
from trajectory_pipeline.perception.base import Observation

if TYPE_CHECKING:  # 依赖方向：executor 不 import llm（见模块 docstring）
    from trajectory_pipeline.executor.browser.page_driver import PageDriver
    from trajectory_pipeline.llm.client import ChatResult, LLMClient
else:
    PageDriver = Any
    ChatResult = Any
    LLMClient = Any


# ═══════════════════════════════════════════════════════════════════════
# 动作空间
# ═══════════════════════════════════════════════════════════════════════

#: agent 的四工具（D-B1 + §5）：与
#: :func:`assembler.schema.ToolSpec.visible_to_model` 的可见集**同一个集合**
#: ——``new_tab`` / ``close_tab`` 是执行器自己的会话管理动作，模型不该选，
#: 选了就是协议违反（不是「先记下来」）。A2 不引入任何 P1 未见过的新动作。
#: 两处漂移由
#: ``tests/unit/test_agent_loop.py::Test契约漂移`` 双向盯住。
AGENT_TOOLS: Final = ("goto", "click", "fill", "press")

#: 输出契约原文（§3.1 wire format）。**随 system prompt 注入**——契约与
#: 提示词同源，改一处改两处必然分叉，而分叉时模型看到的是旧契约。
_WIRE_FORMAT: Final = (
    '{"think": "一句话：我现在看到什么、打算做什么",'
    ' "tool": "goto|click|fill|press",'
    ' "ref": "e5",'
    ' "params": {"url": "..."},'
    ' "done": false,'
    ' "report": null}'
)

#: 每个工具的 params 契约。``parse_instruction`` 只管形状，语义校验在
#: :func:`_validate`——两步分开是因为 I6 需要观察上下文，而解析不需要。
_PARAM_KEYS: Final[Mapping[str, tuple[str, ...]]] = {
    "goto": ("url",),
    "click": (),
    "fill": ("text",),
    "press": ("key",),
}


# ═══════════════════════════════════════════════════════════════════════
# 任务类型（A2b：任务②③④共用一个循环，换的是任务指令）
# ═══════════════════════════════════════════════════════════════════════
#
# 三个 kind **共用同一份动作契约与同一段流水线**——A2b 的工作量因此
# 几乎是纯任务指令（§4：③④ 是「同一循环换任务指令」的增量）。它们的
# 差异只有两处：目标句，以及终局的产出形态。

#: 任务②：在站点内找正片播放页并点开播放控件。
TASK_PLAY: Final = "play"
#: 任务③：在聚合站里找指向正式影视站的详情页入口。
TASK_ENTRIES: Final = "entries"
#: 任务④：在文章正文里找指向影视站的引荐外链。
TASK_REFERRAL: Final = "referral"

#: 封闭取值集。**三个值各代表一种终局形态**，没有「通用任务」。
TASK_KINDS: Final = (TASK_PLAY, TASK_ENTRIES, TASK_REFERRAL)

#: 终局是**一串链接**而不是**一个位置**的那两类。
#: 任务②的「到了」是一个页面，报不上链接；③④ 的产出就是链接本身。
#: 这个区别决定了终局自报要不要带 ``selected``（:func:`parse_instruction`）。
_LINK_TASKS: Final = frozenset({TASK_ENTRIES, TASK_REFERRAL})

#: 每类任务的目标句（system prompt 第一句，``{title}`` 是目标片名）。
_TASK_GOAL: Final[Mapping[str, str]] = {
    TASK_PLAY: "找到《{title}》的正片播放页并点开播放控件",
    TASK_ENTRIES: "在这个聚合站里找到指向《{title}》的正式影视站详情页入口",
    TASK_REFERRAL: "在这篇关于《{title}》的文章正文里找到指向影视站的引荐外链",
}

#: 每类任务的终局自报契约原文（**与 system prompt 同源注入**）。
_REPORT_FORMAT: Final[Mapping[str, str]] = {
    TASK_PLAY: (
        '{"reached": true, "why": "已点开播放控件，画面在放正片"}'
    ),
    TASK_ENTRIES: (
        '{"reached": true, "why": "找到了详情页入口",'
        ' "selected": [{"url": "https://...", "why": "..."}]}'
    ),
    TASK_REFERRAL: (
        '{"reached": true, "why": "找到了正文里的引荐外链",'
        ' "selected": [{"url": "https://...", "why": "..."}]}'
    ),
}

#: ``selected`` 的附加说明（进 :func:`_system_prompt`）。
#: 与 :data:`_REPORT_FORMAT` 分开是因为它要插进纪律段，而契约例子要
#: 单独成行——模型对「例子」和「要求」的读法不同，混在一段里它会把
#: 例子当成要求。
#:
#: ``<max_links>`` 是**占位符不是格式化字段**：这段文案里带着 JSON
#: 示例的花括号，走 ``.format()`` 会把示例一起吃掉。
_SELECTED_RULE: Final = (
    "找到后不要继续导航：done=true 且 reached=true 时 report 必带 "
    "selected（每条 {\"url\": \"...\", \"why\": \"...\"}，最多 "
    "<max_links> 条），url 必须是你在观察的 links 里**真的见过**的。"
)


# ═══════════════════════════════════════════════════════════════════════
# 观察投影（agent 与判断题看到的同一份页面）
# ═══════════════════════════════════════════════════════════════════════

#: 送进对话的链接条数。沿用
#: :data:`~trajectory_pipeline.executor.orchestrator.PERCEPTION_LINK_WINDOW`
#: 的口径——**agent 与十一道判断题看到的候选窗口一样大**，观察口径不分叉，
#: 否则「判断题判不出的页面」与「agent 走不通的页面」就变成两种东西。
_LINK_WINDOW: Final = 20

#: 送进对话的可交互元素条数（= 驱动层 ``interactive`` 的默认上限）。
_INTERACTIVE_WINDOW: Final = 50

#: 送进对话的正文字符上限。**只裁对话，不裁存档**：P1 落盘的是
#: :class:`~trajectory_pipeline.perception.base.Observation` 原值，这里
#: 裁的只是喂模型的那一份投影。
_BODY_WINDOW: Final = 4000


def _visible_interactive(obs: Observation) -> tuple[Any, ...]:
    """**渲染进对话的那一份**可交互元素。

    I6 的白名单必须取自这个函数而不是 ``obs.interactive_elements`` 全体：
    模型没见过的 ref 不算「可引用」，否则白名单只能挡住最笨的那次编造
    （编 e999），挡不住「编一个在第 51 位、屏幕上确实存在但没给他看的
    e51」——而后者才是真会污染存档的那种。
    """
    return tuple(obs.interactive_elements[:_INTERACTIVE_WINDOW])


def _render_observation(obs: Observation) -> str:
    """把观察投影成 agent 看到的 JSON 文本。

    ``degraded`` 与 ``truncated`` 一并进投影：**降级事实让 agent 自己看到**
    （§3.2 第 2 条：DEGRADED 不拦，观察降级照进对话）。它自己看着降级
    的观察去决定换路，比代码替它遮掉更接近真实——轨迹里那一步也确实
    「看到的是残骸」。
    """
    return json.dumps(
        {
            "url": obs.url,
            "title": obs.page_title,
            "body": (obs.body_text or "")[:_BODY_WINDOW],
            "links": [
                {"text": item.text, "href": item.href}
                for item in obs.links[:_LINK_WINDOW]
            ],
            "interactive": [
                {"ref": el.ref, "tag": el.tag, "label": el.label}
                for el in _visible_interactive(obs)
            ],
            "counts": {"video": obs.video_tag_count, "iframe": obs.iframe_count},
            "truncated": obs.truncated,
            "degraded": list(obs.degraded),
        },
        ensure_ascii=False,
    )


# ═══════════════════════════════════════════════════════════════════════
# 任务上下文与预算
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class SelectedRef:
    """终局自报的一条链接（任务③④ 的产出单元）。

    ``url`` 是**唯一的可执行部分**，``why`` 是纯审计字段（落 P1 的
    text、进 rationale 的素材）。与
    :func:`~.steps.aggregator.selected_links` 从感知层 ``Decision`` 里
    取出的 ``(href, text)`` 是**同一个二元组**——那条链的上游是
    ``SELECT_SITE_ENTRIES`` 判断题，这条链的上游是 agent 的自报。

    ⚠️ 它不是 :class:`~.steps.search.Candidate`：候选是**外链校验通过
    之后**的产物（``resolve_referral`` 决定它存不存在），而它是校验
    之前的**自报**，可能一条都活不下来。取名 Ref 就是为了让它看着
    不像一个站。
    """

    url: str
    why: str = ""


@dataclass(frozen=True, slots=True)
class AgentTask:
    """任务②③④的目标上下文——**进站前就已经知道**的那些事实。

    agent 的初始条件只有这些。刻意不含「候选分类结果」「站点质量分」
    之类下游产物：那是代码判断的结论，喂回去等于让模型学「照着结论
    倒推动作」，而结论本身可能是错的。
    """

    title: str
    url: str
    #: 任务类型（:data:`TASK_KINDS` 之一）。**它决定终局的产出形态**，
    #: 不决定动作空间——三类的工具四件套与每步流水线完全同一份。
    kind: str = TASK_PLAY
    #: 站点标题（候选来源给的，可能为空）。**只是提示**，agent 看到的
    #: 页面标题以观察里的 ``title`` 为准——候选标题是检索页给的，
    #: 落地的那页可能已经换了一个。
    site_title: str = ""
    #: 终局最多自报几条链接（任务③④）。**与外链校验的预算同一个数**：
    #: 多报一条就要多开一个 tab 真实访问一次，那是真金白银的往返。
    max_selected: int = 5

    def __post_init__(self) -> None:
        """未知的 kind **在构造期报错**。

        沉到循环里再发现的后果是：kind 拼错会让终局按任务②的口径解析
        （因为不在 :data:`_LINK_TASKS` 里），于是任务③的「到了」报不出
        链接、被判成契约违反、收口 ``unresolved``——而那一批的报表上
        只会显示「聚合站没找到入口」，看不出那是打错了字。
        """
        if self.kind not in TASK_KINDS:
            raise ValueError(
                f"未知任务类型 {self.kind!r}；可用：{TASK_KINDS}。"
                f"拼错会让任务按 {TASK_PLAY!r} 的终局口径解析，"
                f"到了归档那天没人分得清那是没找到还是类型写错了。"
            )


@dataclass(frozen=True, slots=True)
class AgentBudget:
    """两层预算：**单站点内**的步数与 llm_calls。

    它是「第二层」预算：外层调度（``max_intermediates`` / 候选数）不动，
    这一层只管一个站点内 agent 能走几步。两层互不侵蚀——外层是「
    这一批跑几个候选」，内层是「这一个候选里问几次模型」。
    """

    #: 允许执行的动作步数。8 是按任务②的实测形态定的：首页 → 搜索/列表
    #: → 详情页 → 播放控件，正常 3-5 步；给到 8 是为了容纳「点错了退回来
    #: 再试一次」，再多就是打转（有 :class:`DeadLoopDetector` 兜着）。
    max_steps: int = 8
    #: 允许的 llm_calls 次数。**刻意比 max_steps 多**：格式纠正、I6 纠正、
    #: 升配后的重问都要算进去，按 1.5 倍留余量。它才是真花钱的那个闸。
    max_llm_calls: int = 12


@dataclass(frozen=True, slots=True)
class AgentBackend:
    """注入给 :func:`~.steps.agent_visit.agent_visit` 的后端四件套。

    与 ``orchestrator`` 里 ``rationale_writer`` 同一条纪律（依赖方向见
    模块 docstring）：``executor`` 不 import ``llm``，所以 LLM 客户端与
    「text → dict」的解析器都由 **CLI 在边界组装**好再整体注入。

    四个东西生命周期相同（一个 run 一份），收成一个对象穿过三层调用
    （CLI → Orchestrator → ``agent_visit``）比让四个参数各自传下去更
    不容易漏——漏一个的后果是运行时 ``AttributeError``，而那要等到
    真跑起来才会被发现。
    """

    #: 主后端。须有 ``chat_messages`` 方法，签名同
    #: :meth:`trajectory_pipeline.llm.client.LLMClient.chat_messages`。
    client: Any
    #: text → dict 的解析器。**必填且不做默认**——默认成「原样返回」
    #: 会让带 ``<think>`` 的输出直接进协议解析，而那条路径的失败形态
    #: 是「模型答了但不合契约」，看着像模型笨，其实是没剥。
    json_of: Callable[[str], Mapping[str, Any] | None]
    #: 升配后端。``None`` = 未配置（EscalateModel 只落事件、不切后端）。
    escalate: Any = None
    budget: AgentBudget = field(default_factory=AgentBudget)


# ═══════════════════════════════════════════════════════════════════════
# 分支收敛映射（§3.3）
# ═══════════════════════════════════════════════════════════════════════

#: **拦截类** Verdict → 22 分支封闭集里的名字。
#:
#: 这是 agent 模式的**执行层**知识：与感知层的 ``questions.REGISTRY`` 无关
#: （那边是「哪道题产出哪支分支」，这边是「代码判出来的拦截事实算哪支」），
#: 所以放本模块，**不进 branches.py 也不进 questions.py**。
#:
#: 双向钉死（见 ``test_agent_loop.py::Test分支收敛映射``）：
#:
#: - 本表的键 ∪ :data:`_NONTERMINATING_VERDICTS` 恰好等于 :class:`Verdict`
#:   的七值——给 Verdict 加值的人必须来这里登记，否则测试红；
#: - 表里的值全部 ∈ :data:`branches.BRANCH_LABELS`（22 分支封闭集）。
#:
#: 四条映射的共同口径：**拦截是代码判的确定性证据，所以可以记负样本**；
#: 而行为类失败（死循环/预算/I6/格式）不是页面属性，**一律 unresolved，
#: 不入负样本池**——三支「没判出来」分立的纪律在 agent 模式照旧。
_FALLCLOSED_BRANCH: Final[Mapping[Verdict, str]] = {
    Verdict.LOGIN_WALL: "login_required",
    Verdict.PAYWALL: "subscription_wall",
    Verdict.BLOCKED_CF: "blocked",
    Verdict.CAPTCHA: "blocked",
    Verdict.DEAD_LOOP: "unresolved",
}

#: 这些 Verdict **不收口**——它们是「继续走」的分类陈述，不是终止理由。
#: 显列出来的意义在于：``_FALLCLOSED_BRANCH`` 的键 + 它 == Verdict 七值，
#: 少一个都说明有人给 Verdict 加了值却没来决定它该怎么收口。
_NONTERMINATING_VERDICTS: Final = frozenset({Verdict.NORMAL, Verdict.DEGRADED})

#: 非拦截类收口（预算 / I6 / 格式 / 动作执行）统一落这支。
#: 它们共同的性质是「不确定页面到底能不能看」，与
#: :data:`~trajectory_pipeline.executor.branches.NON_SAMPLE_BRANCHES`
#: 里 unresolved 的成因一致（代码能力不足，不是页面负样本）。
#: 对外公开：``agent_visit`` 与 P1 契约测试都要引用它，而三个模块里
#: 各写一个 ``"unresolved"`` 字面量，是迟早会漂移的那种重复。
UNRESOLVED: Final = "unresolved"


@dataclass(frozen=True, slots=True)
class _Terminal:
    """收口信号（本模块私有）。

    为什么不直接用 :class:`~.gatekeeper.FailClosed`：那个对象是**事件形态**
    （它的 ``branch`` 字段是为了落 ``to_event()``），而循环内部流转需要的是
    「哪个 Verdict 触发的」——分支由 :func:`_branch_of` 在唯一一处折算，
    不在事件对象和内部信号之间搬来搬去。
    """

    verdict: Verdict | None
    reason: str


def _branch_of(verdict: Verdict | None) -> str:
    """分支折算的**唯一一处**。有拦截证据查表，没有就落 unresolved。"""
    if verdict is None:
        return UNRESOLVED
    return _FALLCLOSED_BRANCH.get(verdict, UNRESOLVED)


# ═══════════════════════════════════════════════════════════════════════
# 引导文案（InjectGuidance 的对话注入，§3.4）
# ═══════════════════════════════════════════════════════════════════════
#
# 每条都是「事实 + 该做什么」，不写「你应该」。它们作为 **user 消息** 进
# 对话历史（不是 system 重写、也不是静默吞掉）：D-A2 删除 L1 的本意就是
# 「被拦 → 引导 → 重规划」这条链要**留在轨迹里**——它就是要教给模型的
# 正确行为，抹掉等于把轨迹伪造成「从未发生拦截」。

_GUIDANCE_FORMAT: Final = (
    "（上一步的输出不是合法的动作指令 JSON）请严格按输出契约重新输出**一个** "
    "JSON 对象，不要任何解释文字。"
)
_GUIDANCE_BAD_TOOL: Final = (
    "（tool={tool!r} 不是可用工具）可用工具只有 {tools}。请重新输出。"
)
_GUIDANCE_BAD_REF: Final = (
    "（ref={ref!r} 不在最近一次观察的 interactive 列表里）ref 只能用观察里"
    "出现过的那些：{refs}。不要自己编号，请重新选择。"
)
_GUIDANCE_NO_PARAM: Final = (
    "（tool={tool} 缺少 params.{key}）按契约补上再输出，不要留空。"
)
_GUIDANCE_ACTION_FAILED: Final = (
    "（上一步 {tool} 执行失败：{error}）请换一个动作，或如实报告放弃。"
)
_GUIDANCE_OBS_FAILED: Final = (
    "（上一步动作后取不到页面快照：{error}）请换一个动作，或如实报告放弃。"
)
#: 终局自报了一条没见过的 URL。与 :data:`_GUIDANCE_BAD_REF` 是**同一条
#: 纪律的两端**：那个管「点哪个元素」（动作期、可逆性为零），这个管
#: 「报哪条链接」（终局期、进了 P2 就不可逆）。
_GUIDANCE_INVENTED_URL: Final = (
    "（selected 里的 {url!r} 不在你观察过的 links 里）url 只能从观察里"
    "出现过的链接取，不要自己拼。请重新报告，或如实报告放弃。"
)

#: 拦截类 Verdict 的引导原文。``DEAD_LOOP`` 的那条带具体原因
#: （``repeat_action`` / ``empty_body``）——告诉模型它卡在哪一种，
#: 它才知道该换动作还是换目标。
_INTERCEPT_GUIDANCE: Final[Mapping[Verdict, str]] = {
    Verdict.LOGIN_WALL: (
        "（当前页面是登录墙）继续导航没有意义。请换一条路径，"
        "或如实报告放弃（done=true + report.reached=false）。"
    ),
    Verdict.PAYWALL: (
        "（当前页面被付费墙挡住）继续导航没有意义。请换一条路径，"
        "或如实报告放弃（done=true + report.reached=false）。"
    ),
    Verdict.DEAD_LOOP: (
        "（最近几步在原地打转：{reason}）请换一个动作或换一个目标，"
        "或如实报告放弃（done=true + report.reached=false）。"
    ),
}


# ═══════════════════════════════════════════════════════════════════════
# 一步动作指令
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ActionInstruction:
    """一步动作指令（§3.1 wire format 的解析结果）。

    ``ref`` **刻意保留**（与
    :class:`~trajectory_pipeline.executor.actions.Action` 相反）：它是
    I6 校验的对象与驱动层的输入，而一旦进了存档就成了会话内随机数。
    所以它在 :func:`_build_action` 里被 :func:`actions.target_of` 换成
    语义目标**之后就消失**——这条边界是本模块唯一持有它的地方。
    """

    tool: str
    ref: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    #: 本步的思考原文（§3.1 的 ``think``）。是**设计出来的短句**，与
    #: ``<think>`` 包裹的 raw CoT 两回事——后者在 llm 侧就被剥掉了。
    think: str = ""
    done: bool = False
    #: ``done`` 时的报告原文 ``{"reached": bool, "why": str,
    #: "selected": [{"url": ..., "why": ...}] | null}``。
    report: Mapping[str, Any] | None = None
    #: 终局自报的链接（只有 :data:`_LINK_TASKS` 的两类任务有）。
    #: 空元组 = 本次没有自报链接——放弃形态也是空的。
    selected: tuple[SelectedRef, ...] = ()

    @property
    def reached(self) -> bool | None:
        """:attr:`report` 里的 ``reached``。没报告就是 ``None``——
        **不把「没报告」当成「没到达」**。"""
        if not self.report:
            return None
        return self.report.get("reached")

    @property
    def why(self) -> str:
        return str(self.report.get("why") or "") if self.report else ""


def _think_payload(think: str) -> dict[str, Any]:
    """把一步 ``think`` 装进 P1 ``steps[i].rationale`` 槽位的 payload。

    形状与 :meth:`trajectory_pipeline.rationale.writer.WriteResult.to_json`
    的 ``written`` 形态**全等**（``status`` / ``rationale`` / ``reason`` /
    ``degraded`` 四键）——同一张 P1 表，scripted 装 writer 产物、agent 装
    原生 think，下游（P2 对齐、闸门、D-9）不需要知道是谁跑的。形状同步
    不靠 executor import rationale（依赖方向纪律），靠
    ``tests/contract`` 的比对测试钉住。

    ``reason`` 恒为 ``"native-think"``：writer 成功时它写 ``""``，两个
    取值不冲突，而「这条 rationale 是原生 think 还是 writer 产物」在
    P1 里就有了机器可读的来源标记——A3 的同分不同质核对（scripted 事后
    总结 vs agent 原生动机）读的就是它。
    """
    return {
        "status": "written",
        "rationale": {"text": think, "conclusion": ""},
        "reason": "native-think",
        "degraded": [],
    }


def _canonical(instr: ActionInstruction) -> str:
    """把指令**重新序列化**成规范 JSON——这是进对话历史的那个文本。

    不是原样回吐模型原文，而是按 :data:`_WIRE_FORMAT` 的字段重排：
    多余的字段（包括任何夹带进来的长文本）在重新序列化时自然丢弃，
    raw CoT 因此没有路径流进 P1/P2。
    """
    report = dict(instr.report) if instr.report else None
    if report is not None:
        # 用**归一化后的** selected 覆盖原文里那一段：原文里可能夹着
        # 模型自己加的字段（甚至一整段理由），重新序列化时按契约重排
        # 才丢得掉。这是 raw CoT 不进 P1/P2 的同一道闸。
        report["selected"] = [
            {"url": r.url, "why": r.why} for r in instr.selected
        ] or None
    return json.dumps(
        {
            "think": instr.think,
            "tool": instr.tool,
            "ref": instr.ref,
            "params": dict(instr.params),
            "done": instr.done,
            "report": report,
        },
        ensure_ascii=False,
    )


def parse_instruction(
    data: Mapping[str, Any] | None,
    kind: str = TASK_PLAY,
) -> ActionInstruction | None:
    """校验一步指令的**形状**。不合契约返回 ``None``（不猜、不补默认值）。

    与 :func:`_validate` 的分工：这里只管「字段在不在、类型对不对」，
    不管「ref 是不是真的、params 够不够、url 见没见过」——后两者需要
    观察上下文。两步分开是因为失败文案要给不同的引导：形状错了教契约，
    引用错了教白名单。

    **契约里写了的字段必须在、且类型必须对**——一律不得补默认值。
    这条是「格式纠正」能教出东西的前提：悄悄替模型补上一个 ``done``，
    它就永远不知道自己漏了字段，而漏字段的下一步是漏更关键的字段。

    三处刻意的不对称：

    1. ``done=true`` 时 ``report`` **必填且必须合法**（含 ``reached`` 布尔
       与 ``why`` 字符串）。缺了它代码无从判断「到了没有」，只能整步作废；
    2. ``done=true`` 时 ``tool`` / ``ref`` / ``params`` **不校验**——那一轮
       不执行动作，那些字段是空壳；
    3. ``done=false`` 时多了个 ``report`` **忽略它**。动作本身没有歧义，
       为一段多余文字废掉一步不划算。

    ``selected`` 的三条口径（A2b）：

    - :data:`_LINK_TASKS` 报 ``reached=true`` 时必填且**每条都要有
      非空 url**——那是唯一会被执行的部分，缺了它这条自报等于没报；
    - 报 ``reached=false``（放弃）时**忽略**：放弃不需要交链接；
    - 任务② 报了 ``selected`` 也**忽略**：那类任务的终局是一个位置，
      多报一串链接不影响「到了没到」。

    工具名的取值**不在本函数限定**（越界交给 :func:`_validate`）——那边
    拿得到工具清单，才说得出「可用工具只有……」这句具体引导。
    """
    if not isinstance(data, Mapping) or not data:
        return None
    done = data.get("done")
    if not isinstance(done, bool):
        return None
    if done:
        report = data.get("report")
        if not isinstance(report, Mapping):
            return None
        if not isinstance(report.get("reached"), bool):
            return None
        if not isinstance(report.get("why"), str):
            return None
        selected = _parse_selected(report, kind)
        if selected is None:
            return None  # 链接类任务报「到了」却交不出链接 = 整步作废
        return ActionInstruction(
            tool="",
            params={},
            think=_as_text(data.get("think")),
            done=True,
            report=dict(report),
            selected=selected,
        )
    tool = data.get("tool")
    if not isinstance(tool, str) or not tool:
        return None
    params = data.get("params")
    if params is not None and not isinstance(params, Mapping):
        return None
    return ActionInstruction(
        tool=tool,
        ref=_as_text(data.get("ref")),
        params=dict(params or {}),
        think=_as_text(data.get("think")),
        done=False,
        # 多了的 report 在解析结果里就当不存在（上面的第 3 条不对称）——
        # 于是重新序列化进对话时它是 ``null``，模型看到的是契约形态。
        report=None,
    )


def _parse_selected(
    report: Mapping[str, Any], kind: str
) -> tuple[SelectedRef, ...] | None:
    """解析终局自报的链接。``None`` = 契约违反（整步作废）。

    与 :func:`_validate_selected` 的分工与 :func:`parse_instruction` /
    :func:`_validate` 那一对同款：这里只管形状（是不是列表、每条有
    没有 url），那边管真值（url 见没见过）。
    """
    if kind not in _LINK_TASKS:
        # 任务② 的终局不是一个链接集合（见 parse_instruction 的第 3 条
        # 不对称）——报了也当没报，不至于为一段多余内容废掉整步。
        return ()
    if report.get("reached") is not True:
        return ()  # 放弃形态不交链接
    rows = report.get("selected")
    if not isinstance(rows, (list, tuple)) or not rows:
        return None
    out: list[SelectedRef] = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        url = _as_text(row.get("url"))
        if not url:
            return None  # 没有 url 的自报等于没报，不猜它想说哪条
        out.append(SelectedRef(url=url, why=_as_text(row.get("why"))))
    return tuple(out)


def _as_text(value: Any) -> str:
    """字段转字符串。非标量一律空串——**不 ``str()`` 一个 dict**。"""
    return value if isinstance(value, str) else ""


def _validate(instr: ActionInstruction, refs: frozenset[str]) -> str:
    """工具层校验（I6 在这里强制）。返回引导文案，空串 = 通过。

    三条判据，顺序即严重性：

    1. **工具名越界** → 模型选了执行器自己的动作（``new_tab`` /
       ``close_tab``）或不存在的工具。这是协议违反，不是能力问题；
    2. **params 缺必需键** → goto 无 url、fill 无 text、press 无 key。
       缺了就没法执行，补默认值等于替模型编 URL/编按键；
    3. **ref 不在白名单** → I6 违反。**编 ref 是不可逆错误**：点了就
       是点错页面，且改不回来。所以根本不执行，直接回炉。
    """
    if instr.tool not in AGENT_TOOLS:
        return _GUIDANCE_BAD_TOOL.format(
            tool=instr.tool, tools="/".join(AGENT_TOOLS)
        )
    for key in _PARAM_KEYS.get(instr.tool, ()):
        if not _as_text(instr.params.get(key)):
            return _GUIDANCE_NO_PARAM.format(tool=instr.tool, key=key)
    if instr.tool in ("click", "fill") and instr.ref not in refs:
        return _GUIDANCE_BAD_REF.format(
            ref=instr.ref or "(空)",
            refs=", ".join(sorted(refs)) or "(本页没有可交互元素)",
        )
    # press 的 ref 可空（回车提交是全局按键），但**给了就必须是真的**。
    if instr.tool == "press" and instr.ref and instr.ref not in refs:
        return _GUIDANCE_BAD_REF.format(
            ref=instr.ref, refs=", ".join(sorted(refs))
        )
    return ""


@dataclass(frozen=True, slots=True)
class RejectedReport:
    """反驳一次终局自报（A2b）——第四种处置对象。

    为什么不复用 :class:`~.gatekeeper.InjectGuidance`：那个是**页面事实**
    触发的（登录墙 / 付费墙），有 :class:`Verdict` 可审计；这个是**自报
    内容与轨迹不符**触发的，没有页面 Verdict 可言——页面本身没问题，
    是模型报了一条它没见过的链接。

    它也要落 ``gatekeeper_events``，理由与 A3 §5 的 goto 溯源断言同源：
    批次级「这个模型今天编了多少条 URL」的统计，只有这里记了才查得到。
    循环内挡下来了不等于没发生过——**挡下来是处置，记账是事实**，两件
    事都要有，否则 A3 拿到的幻觉计数恒为 0。
    """

    #: 被反驳的 URL 原文（审计用：要能看出它编的是哪一种形态）。
    url: str

    def to_event(self) -> dict[str, str]:
        return {"kind": "reject_report", "url": self.url}


def _invented_url(selected: tuple[SelectedRef, ...], seen: set[str]) -> str:
    """第一条没见过的 URL。空串 = 全部见过。

    与 :func:`_validate_selected` 分开，是因为**反驳要知道是哪一条**
    （落 :class:`RejectedReport` 要带原文），而提示词只需要一句文案。
    """
    for ref in selected:
        if ref.url not in seen:
            return ref.url
    return ""


def _validate_selected(
    selected: tuple[SelectedRef, ...], seen: set[str]
) -> str:
    """终局自报的 URL 必须在轨迹里**真的见过**。返回引导文案，空串 = 通过。

    与 I6（:func:`_validate` 第 3 条）是同一条纪律的两端，差别在**为什么
    这一端反而更该强制**：

    - I6 挡的是动作期编 ref——点错了还能退回来，模型自己看得见走错了；
    - 这里挡的是终局编 URL——自报一条它没见过的链接，会直接产出一条
      **根本不存在的候选**进 P2，而且循环已经结束，没有退路。

    所以顺序上是：I6 挡住的编造进引导重试（可自愈），这里挡住的进
    引导重试 + **一条 RejectedReport 事件**（不可自愈，必须留痕）。

    ⚠️ 与 A3 的 goto 溯源断言是**两件事**：那边统计的是 goto 的 url
    （循环里有意为之的不拦——编了会走到 404，agent 自己会换路），这边
    拦的是终局产物。A3 的批次级幻觉计数读的就是本函数落的事件。
    """
    for ref in selected:
        if ref.url not in seen:
            return _GUIDANCE_INVENTED_URL.format(url=ref.url)
    return ""


def _build_action(
    instr: ActionInstruction, obs: Observation
) -> "actions.Action":
    """把指令落成 :class:`~trajectory_pipeline.executor.actions.Action`。

    ``ref`` 在这里被换成语义目标后消失（见 :class:`ActionInstruction`
    的 docstring）。:func:`actions.target_of` 解不出时返回 ``target=None``
    照实记——但 I6 已保证它能解出，那条 ``None`` 分支留着是为了不靠
    「上游一定对」这种假设。
    """
    target = actions.target_of(instr.ref, obs.interactive_elements)
    if instr.tool == "goto":
        return actions.goto(_as_text(instr.params.get("url")))
    if instr.tool == "click":
        return actions.click(target)
    if instr.tool == "fill":
        return actions.fill(target, _as_text(instr.params.get("text")))
    return actions.press(
        _as_text(instr.params.get("key")),
        target,
    )


def _action_key(instr: ActionInstruction) -> str:
    """死循环检测用的动作键。

    ``(host, action)`` 全同才算重复，所以键里要有**目标**：只在同一个
    元素上反复点击才是打转，在不同链接间跳是正常探索
    （:class:`DeadLoopDetector` 的保守口径就落在这里）。
    """
    target = instr.ref or (
        _as_text(instr.params.get("url"))
        or _as_text(instr.params.get("key"))
        or _as_text(instr.params.get("text"))
    )
    return f"{instr.tool}:{target}"


# ═══════════════════════════════════════════════════════════════════════
# 出口：AgentOutcome
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    """agent 循环的**唯一出口**。三种形态，分支由代码折算（§3.3）。

    ``branch`` 是本模块唯一沾「分支」的字段，且它是**查表查出来的**
    （:data:`_FALCLOSED_BRANCH`），不是模型说的。``branch=""``（reached
    形态）是刻意的空值：那表示「这一支不由 agent 定」，调用方要拿它去
    跑 ``PLAYER_OK`` 判断题。
    """

    kind: Literal["reached", "gave_up", "fail_closed"]
    #: 人读的终局说明。放弃形态是模型的 ``why`` 原文，收口形态是代码的
    #: 机械事实描述——两种来源都进这一个字段，读档的人要看的是
    #: 「为什么这条轨迹到此为止」，来源是谁不该影响他怎么读。
    reason: str = ""
    #: 收口形态下触发它的 :class:`Verdict` 值。空串 = 非拦截收口
    #: （预算 / I6 / 格式 / 动作执行失败）——有拦截证据的收口与没有的
    #: 在记账上不同（前者可记负样本），必须能分开。
    verdict: str = ""
    #: 折算出的分支名。``""`` = 不落分支（交由 PLAYER_OK）。
    branch: str = ""
    steps_taken: int = 0
    llm_calls: int = 0
    #: 完整对话（含观察回执与引导注入）。**逐步对齐的原生 CoT 载体**：
    #: D-9 / rationale 的事后对齐要用它，这里只负责留全。
    transcript: tuple[Mapping[str, str], ...] = ()
    #: 终局自报的链接（任务③④ 的 reached 形态）。**只有 :data:`_LINK_TASKS`
    #: 的两类任务会带它**，任务② 恒为空。
    #:
    #: ⚠️ 它们是**自报**，不等于候选：要经过 :func:`~.steps.link_check.
    #: resolve_referral` 的外链校验才变成
    #: :class:`~.steps.search.Candidate`（A2b「与 scripted link_check 并存期」
    #: 的那一半）。留着自报形态是为了让 P1 看得出「模型报了什么」与
    #: 「代码放行了什么」是两件事。
    selected: tuple[SelectedRef, ...] = ()
    #: 报「到了」时 agent 正看着的那一页。**只有 reached 形态带它**。
    #:
    #: 存在这里的理由不是省一次 ``observe``：``PLAYER_OK`` 必须判在
    #: **agent 据以决策的那一页**上。循环外再取一次快照，判的就是另一份
    #: 快照了——播放器会自转、广告会轮换、倒计时会走，而 D-2/D-5 判的
    #: 正是观察正文。判据与决策依据分叉，是最难查的一类不一致。
    final_obs: Observation | None = None

    @property
    def reached(self) -> bool:
        """是否报「到了播放页」。

        ⚠️ 这不是「成功了」——成功是 ``PLAYER_OK`` 判断题的事（D-A5）。
        它只回答「导航这一段走完了没有」。
        """
        return self.kind == "reached"

    @property
    def fail_closed(self) -> bool:
        return self.kind == "fail_closed"

    @classmethod
    def reach(
        cls,
        *,
        steps: int,
        calls: int,
        transcript: tuple,
        obs: Observation,
        selected: tuple[SelectedRef, ...] = (),
    ) -> "AgentOutcome":
        return cls(kind="reached", steps_taken=steps, llm_calls=calls,
                   transcript=transcript, final_obs=obs, selected=selected)

    @classmethod
    def give_up(
        cls, *, why: str, steps: int, calls: int, transcript: tuple
    ) -> "AgentOutcome":
        """自报放弃——**一律 unresolved，不入负样本池**（§3.3）。

        agent 说不清的，不进负样本池：它可能是站里真没有，也可能是模型
        没找到，两种成因在数据上完全同形，合并成一支负样本就是把
        「没找到」记成「这里看不了」。
        """
        return cls(kind="gave_up", reason=why, branch=UNRESOLVED,
                   steps_taken=steps, llm_calls=calls, transcript=transcript)

    @classmethod
    def close(
        cls,
        *,
        reason: str,
        verdict: Verdict | None,
        steps: int,
        calls: int,
        transcript: tuple,
    ) -> "AgentOutcome":
        """代码收口。有拦截证据查映射表，没有就落 ``unresolved``。"""
        return cls(
            kind="fail_closed",
            reason=reason,
            verdict=verdict.value if verdict is not None else "",
            branch=_branch_of(verdict),
            steps_taken=steps,
            llm_calls=calls,
            transcript=transcript,
        )


# ═══════════════════════════════════════════════════════════════════════
# 循环
# ═══════════════════════════════════════════════════════════════════════

#: 连续几次「格式/I6 类失败」触发升配（§3.4 A2a 最小版）。
#: 2 而不是 3：一次是手滑，两次是「这个模型啃不动这个页面」的信号；
#: 等到第三次，预算已经烧掉三分之一。
_ESCALATE_AFTER: Final = 2

#: 一次决策最多问几轮。两轮是**格式纠正**（第二次还错说明该升配或收口），
#: 第三轮**只在刚升配过时发生**——不是给同一个后端第三次机会，而是
#: 「换个更强的后端再问一次」，那才是升配的意义。
_MAX_ATTEMPTS: Final = 3

#: 同一类拦截第几次触发就收口（防引导循环）。
#: 第二次就收而不是第三次：InjectGuidance 的目的是给模型一次换路的机会，
#: 换完还是撞上同一堵墙，说明这个站没路可走——继续引导只是烧 llm_calls。
_GUIDANCE_LIMIT: Final = 2


def _rules_of(task: AgentTask) -> str:
    """纪律清单（system prompt 的最后一段）。

    两类任务共用前两条与最后一条，:data:`_LINK_TASKS` 的两类**多一条
    selected 规则**。多出来的那条插在「如实放弃」之前而不是追加在末尾：
    顺序是按**危害大小**排的（ref 点错不可逆 → 终局报错进了 P2 也不可逆
    → 编造 → 猜 URL），追加在末尾会让模型以为它是最不重要的一条。

    编号在 :func:`_system_prompt` 里统一加，不在逐个字符串里写死——
    插一条时不用手工重排后面三条的序号。
    """
    rules = [
        "ref 只能用最近一次观察的 interactive 列表里出现过的，绝不自己编号。",
    ]
    if task.kind in _LINK_TASKS:
        rules.append(
            _SELECTED_RULE.replace("<max_links>", str(task.max_selected))
        )
    rules.append(
        "找不到就如实报告放弃（done=true + report.reached=false + why），"
        "不要编造。"
    )
    rules.append(
        "不要猜网址：goto 的 url 只从观察里的 links 或任务给的网址取。"
    )
    return "".join(f"{i}. {r}\n" for i, r in enumerate(rules, 1))


def _system_prompt(task: AgentTask, budget: AgentBudget) -> str:
    """system：任务目标 + 工具四件套 + 输出契约 + 纪律（§3.8，A2b 表驱动）。

    三类任务**只换目标句与终局契约**，其余同一个模板：动作空间、观察
    口径、流水线三处共用，是 A2b 敢把 ③④ 并进同一个循环的前提——
    换成三条 prompt 各自维护，它们会在某一天分叉，而分叉那天两个任务的
    轨迹就不再可比（§4 的「同一循环换任务指令」）。

    契约例子**随提示词注入**（:data:`_WIRE_FORMAT` / :data:`_REPORT_FORMAT`）：
    契约与提示词同源，改一处改两处必然分叉，而分叉时模型看到的是旧契约。
    """
    goal = _TASK_GOAL[task.kind].format(title=task.title)
    return (
        f"你在一个页面内导航，目标是{goal}。\n"
        f"可用工具（每步只能选一个）：\n"
        f"- goto：打开网址（params.url）\n"
        f"- click：点击一个可交互元素（ref 来自观察的 interactive 列表）\n"
        f"- fill：往输入框填文本（ref 同上，params.text）\n"
        f"- press：按一个键（params.key，如 Enter）\n"
        f"每步输出且只输出一个 JSON 对象：\n{_WIRE_FORMAT}\n"
        f"终局格式（done=true 时 report 必填）：\n{_REPORT_FORMAT[task.kind]}\n"
        f"- done=false 时 report 必须是 null\n"
        f"- 步数预算 {budget.max_steps} 步，用完就结束\n"
        f"纪律：\n{_rules_of(task)}"
    )


def _task_view(task: AgentTask, budget: AgentBudget) -> str:
    """user_0：任务上下文（§3.8）。**只有进站前就知道的事实**。"""
    return json.dumps(
        {
            "task": _TASK_GOAL[task.kind].format(title=task.title),
            "kind": task.kind,
            "candidate_url": task.url,
            "site_title": task.site_title,
            "step_budget": budget.max_steps,
            "max_selected": task.max_selected,
        },
        ensure_ascii=False,
    )


class _Loop:
    """``agent_loop`` 的可变状态与每步流水线。**私有**：对外只有
    :func:`agent_loop` 一个入口。

    为什么状态收在一个对象里：每步流水线七步，十来个可变状态在七个函数
    间传参会让每个签名都比函数体长，而它们确实共享同一份生命周期
    （一个候选一次）。
    """

    __slots__ = (
        "driver", "escalate", "task", "steps", "budget",
        "events", "calls", "json_of", "max_chars", "dead_loop",
        "obs", "messages", "_pending_obs", "_pending_notes",
        "_guidance_hits", "_consecutive_failures", "_steps_taken",
        "_calls_made", "_backend", "_escalated", "_seen_hrefs",
    )

    def __init__(
        self,
        *,
        driver: "PageDriver",
        client: "LLMClient",
        escalate: "LLMClient | None",
        task: AgentTask,
        steps: StepLog,
        budget: AgentBudget,
        events: list[dict[str, Any]],
        calls: list[dict[str, Any]],
        json_of: Callable[[str], Mapping[str, Any] | None],
        initial_obs: Observation,
        max_chars: int | None,
        dead_loop: DeadLoopDetector | None,
    ) -> None:
        self.driver = driver
        self.escalate = escalate
        self.task = task
        self.steps = steps
        self.budget = budget
        self.events = events
        self.calls = calls
        self.json_of = json_of
        self.max_chars = max_chars
        self.dead_loop = dead_loop if dead_loop is not None else DeadLoopDetector()
        self.obs = initial_obs
        self.messages: list[dict[str, str]] = [
            {"role": "system", "content": _system_prompt(task, budget)},
            {"role": "user", "content": _task_view(task, budget)},
        ]
        self._pending_obs: Observation | None = None
        self._pending_notes: list[str] = []
        self._guidance_hits: dict[str, int] = {}
        self._consecutive_failures = 0
        self._steps_taken = 0
        self._calls_made = 0
        self._backend = client
        #: 升配**每个候选只考虑一次**。见 :meth:`_maybe_escalate`。
        self._escalated = False
        #: 轨迹内**真的见过**的 href 全集。终局自报的 URL 白名单
        #: （:func:`_validate_selected`）——它与 I6 的 ref 白名单是两
        #: 个集合：那个是「最近一页的可交互元素」，这个是「整条轨迹的
        #: 链接」，因为终局可能在好几个页面之后才报。
        self._seen_hrefs: set[str] = set()
        self._absorb_links(initial_obs)
        self._seen_hrefs.add(task.url)

    def _absorb_links(self, obs: Observation) -> None:
        """把一份观察里的链接并进白名单（**累积**，不替换）。"""
        for link in obs.links or ():
            if link.href:
                self._seen_hrefs.add(link.href)

    # ── 每步流水线 ────────────────────────────────────────────

    async def run(self) -> AgentOutcome:
        """七步流水线，直到出口。**本函数不抛异常**（与 visit_site 同纪律）。"""
        # 进站首观察也要过一次拦截：候选 URL 落地可能直接落到付费墙/登录
        # 墙上（重定向后的页面与候选 URL 毫无关系），那是代码能一眼看出
        # 的事实，让模型先撞一次没有意义。
        disp = self._intercept(self.obs)
        if disp is not None:
            return self._close(reason=disp.reason, verdict=disp.verdict)
        self._pending_obs = self.obs

        while True:
            # ① 预算闸放在**花钱之前**：步数用完就不再烧下一次 llm_call。
            if self._steps_taken >= self.budget.max_steps:
                return self._close(reason="步数预算耗尽", verdict=None)
            if self._calls_made >= self.budget.max_llm_calls:
                return self._close(reason="llm_calls 预算耗尽", verdict=None)

            # ② 决策（内部含一次格式/I6 纠正重试）
            got = await self._decide()
            if isinstance(got, AgentOutcome):
                return got
            instr = got

            # ③ done=true → 出口
            if instr.done:
                if instr.reached is True:
                    return AgentOutcome.reach(
                        steps=self._steps_taken,
                        calls=self._calls_made,
                        transcript=self._transcript(),
                        obs=self.obs,
                        selected=instr.selected,
                    )
                return AgentOutcome.give_up(
                    why=instr.why or "（模型未给出放弃原因）",
                    steps=self._steps_taken,
                    calls=self._calls_made,
                    transcript=self._transcript(),
                )

            # ④ 执行 → ⑤ 复观察 → ⑥ 记账 → ⑦ 死循环 + 拦截
            out = await self._execute(instr)
            if out is not None:
                return out

    async def _decide(self) -> ActionInstruction | AgentOutcome:
        """组 prompt → 调 LLM → 解析 → I6 校验。

        **「没作答」与「答错了」分两条路走**（A2a-1 留下的契约提醒）：
        后端没作答直接收口，模型答得不合契约才给重试机会——两者都叫
        「没拿到可用指令」，但一个该去查运维、一个该去改提示词，
        混成一条路就会在后端挂掉的时候白烧一轮 llm_call 再升一次配。

        重试次数**有限且有上限**：无限重试会把 llm_calls 预算吃光在
        「问同一句话」上——那是最贵的死循环。第三轮的准入条件见
        :meth:`_maybe_escalate`（只有刚换了更强后端时才发生）。
        """
        for _attempt in range(_MAX_ATTEMPTS):
            self._flush_user()
            result: "ChatResult" = self._backend.chat_messages(self.messages)
            self._record(result)
            self._calls_made += 1

            if result.content is None:
                # **后端没作答**（transport / http / 预算 / 形状）与
                # 「模型答了但不合契约」是两种故障，处置方向相反：
                # 前者是运维问题——client 内部已经重试过了，再问一遍
                # 结果一样；升配更是南辕北辙（更强的模型也连不上）。
                # 所以直接收口，把错误码留在 reason 里。
                return self._close(
                    reason=f"后端未作答（{result.error}）", verdict=None
                )
            # ``kind`` 决定终局的产出形态（链接类必带 selected），
            # **必须传**：不传就是按任务②解析，链接类的「到了」会
            # 因为交不出 selected 而被当成格式违反——那是把类型写错
            # 伪装成模型答错。
            instr = parse_instruction(
                self.json_of(result.content), self.task.kind
            )
            if instr is None:
                # 模型答了，但答的不是契约里的东西——这是**能教**的：
                # 注入契约原文再给一次机会。
                self._note(_GUIDANCE_FORMAT)
                self._consecutive_failures += 1
                if self._maybe_escalate():
                    continue  # 换了更强的后端，再问一次
                if _attempt == 0:
                    continue  # 格式纠正：第二次机会
                break

            if instr.done:
                # done 形态**不校验工具与 ref**：那一轮不执行任何动作，
                # 工具字段是空壳（契约里它只在 done=false 时有意义）。
                # 校验它会让「报到了」被自己的工具字段卡住。
                invented = _invented_url(instr.selected, self._seen_hrefs)
                if invented:
                    # 终局报了一条它没见过的链接。**比 I6 编 ref 更该拦**：
                    # 编 ref 点错了还能退回来（页面还在，agent 看得到），
                    # 终局报出来的 URL 会直接过外链校验变成候选进 P2，
                    # 而循环已经结束，没有退路。
                    #
                    # 挡下来与记账是两件事：挡是处置，RejectedReport 是
                    # 事实——A3 的批次级幻觉计数读的就是这条事件。
                    self._emit(RejectedReport(url=invented))
                    self._note(_GUIDANCE_INVENTED_URL.format(url=invented))
                    self._consecutive_failures += 1
                    if self._maybe_escalate():
                        continue
                    if _attempt == 0:
                        continue
                    break
                self.messages.append(
                    {"role": "assistant", "content": _canonical(instr)}
                )
                return instr
            guidance = _validate(instr, self._ref_whitelist())
            if guidance:
                # 坏输出**不进对话**（见 :func:`_canonical` 的理由），
                # 但「这一步被拒了、因为什么」必须进——那是模型下一轮
                # 修正的依据，也是轨迹里「被拦→纠正→重试」的上半截。
                self._note(guidance)
                self._consecutive_failures += 1
                if self._maybe_escalate():
                    continue
                if _attempt == 0:
                    continue
                break

            self._consecutive_failures = 0
            self.messages.append({"role": "assistant", "content": _canonical(instr)})
            return instr

        return self._close(reason="动作指令连续解析失败", verdict=None)

    async def _execute(self, instr: ActionInstruction) -> AgentOutcome | None:
        """执行一步 + 复观察 + 死循环记账 + 拦截。返回非 None = 该收口。

        与 :meth:`_decide` 的返回约定同款（``None`` = 继续），为的是
        ``run`` 里不用 ``try/except`` 做控制流。

        ⚠️ **失败的两条路径都不消耗步数**（步数记的是「走到的页面数」）。
        这是刻意的取舍：动作失败给 agent 换路的机会，但那机会不能无限
        ——由 llm_calls 预算兜底（换路也要问模型）。把失败也记成一步，
        会让「点了三次都点不动」直接吃掉整个步数预算，而那三次其实
        **哪儿都没去**。
        """
        action = _build_action(instr, self.obs)
        index = self.steps.act(action)
        if instr.think.strip():
            # think 是**动作前动机**（决定这一步时想的），与执行成败无关：
            # 失败步的动机同样是 D-9 的判据——「点了但点不动」那一步，
            # 模型当时为什么点，正是该被核查的原文。空 think 不挂：
            # 「没说话」保持缺件形态，不与「说了但没落」相混。
            self.steps.attach_rationale(index, _think_payload(instr.think))
        try:
            await _apply(self.driver, instr)
        except DriverError as exc:
            # 动作执行失败**不收口**：让 agent 看着失败原因自己换路。
            # 写进 P1 的是 ``error`` 非空的那一步——「点了但点不动」正是
            # 负样本最该留的证据。
            self.steps.settle(index, error=f"{instr.tool} 失败: {exc}")
            self._note(
                _GUIDANCE_ACTION_FAILED.format(tool=instr.tool, error=exc)
            )
            return None
        try:
            obs = await self.driver.observe(max_chars=self.max_chars)
        except DriverError as exc:
            # 快照取不到是**能力问题**（I4 fail-closed），但这里不整站收口：
            # 页面还在，agent 可以换个动作继续。落 P1 的是
            # 「动过、但没看到结果」，与「没动」是两回事。
            self.steps.settle(index, error=f"动作后快照失败: {exc}")
            self._note(_GUIDANCE_OBS_FAILED.format(error=exc))
            return None

        self.steps.settle(index, obs)
        self.obs = obs
        self._pending_obs = obs
        self._absorb_links(obs)
        self._steps_taken += 1
        self.dead_loop.record(
            obs.url, _action_key(instr), len((obs.body_text or "").strip())
        )

        disp = self._intercept(obs)
        if disp is not None:
            return self._close(reason=disp.reason, verdict=disp.verdict)
        return None

    # ── 拦截与处置 ────────────────────────────────────────────

    def _intercept(self, obs: Observation) -> _Terminal | None:
        """每份**新**观察过一次拦截。返回收口信号，``None`` = 继续。

        ⚠️ 拦截只在新观察到达时触发，**不在每轮循环开始时触发**。同一份
        观察被拦两次会让「格式纠正重试」意外吃满 :data:`_GUIDANCE_LIMIT`
        ——模型还没来得及换路就被判成二次拦截收口了。
        """
        verdict = intercept(obs, self.dead_loop)
        if verdict in _NONTERMINATING_VERDICTS:
            # DEGRADED 是分类陈述，不是处置：降级事实已经写进观察投影里
            # 让 agent 自己看（§3.2 第 2 条），这里不额外注入。
            return None
        if verdict in (Verdict.BLOCKED_CF, Verdict.CAPTCHA):
            # 反爬/验证码：agent 没有登录态也没有指纹可换，**问它也没用**。
            # 一次性收口，不给引导机会——引导只会烧一次 llm_call 然后
            # 得到「我换不了」。
            reason = f"环境级阻断：{verdict.value}"
            self._emit(
                FailClosed(reason=reason, branch=_branch_of(verdict)),
                verdict=verdict,
            )
            return _Terminal(verdict=verdict, reason=reason)
        hits = self._guidance_hits.get(verdict.value, 0) + 1
        self._guidance_hits[verdict.value] = hits
        if hits >= _GUIDANCE_LIMIT:
            # 第二次同类拦截就收口（防引导循环）：换过一次路还是撞上同一
            # 堵墙，说明这个站没路可走，继续引导只是烧 llm_calls。
            reason = f"{verdict.value} 二次拦截"
            self._emit(
                FailClosed(reason=reason, branch=_branch_of(verdict)),
                verdict=verdict,
            )
            return _Terminal(verdict=verdict, reason=reason)
        text = _INTERCEPT_GUIDANCE.get(verdict, "").format(
            reason=self.dead_loop.detect()
        )
        self._emit(InjectGuidance(verdict=verdict, guidance=text))
        self._note(text)
        return None

    def _maybe_escalate(self) -> bool:
        """连续失败到阈值就升配（§3.4 A2a 最小版）。

        Returns:
            ``True`` = **刚换成更强的后端**，调用方应当再问一次——
            升配的意义就是「换个能啃动的模型再试」，而不是「记录一下
            然后照旧收口」。这是 :data:`_MAX_ATTEMPTS` 里第三轮的唯一
            准入条件。

        **升配是能力不是依赖**：``escalate`` 是 ``None`` 时只落事件、不切
        后端（未配置不该 fail）。作用域是**当前候选的剩余步骤**（后端是
        实例状态，切了就一直在），下一个候选由调用方重新构造循环回到
        主后端——不全局切换，否则一次啃不动的页面会把整批成本抬上去。
        """
        if self._consecutive_failures < _ESCALATE_AFTER or self._escalated:
            return False
        # 每个候选只升一次：升完还啃不动，就说明不是模型不够强的问题，
        # 再升一次只是把成本翻倍。
        self._escalated = True
        reason = f"连续 {self._consecutive_failures} 次动作指令不可解析"
        if self.escalate is None:
            self._emit(EscalateModel(reason=reason))
            return False
        self._backend = self.escalate
        self._emit(
            EscalateModel(
                reason=reason,
                backend=getattr(getattr(self.escalate, "cfg", None), "model", ""),
            )
        )
        return True

    # ── 记账 ──────────────────────────────────────────────────

    def _record(self, result: "ChatResult") -> None:
        """llm_calls[] 记账（§3.6）。

        **只记 agent_loop 的调用**：感知层十一题的调用不汇入（Decision
        已有 latency_ms，把账本实例传进 perceptor 是对感知层的侵入），
        列为 O 阶段扩展项。

        ``usage`` 缺失时两个 token 字段落 ``None`` 而不是 0——后端没给的
        数写成 0，批次成本就会被那几个 0 拉低，报表上「便宜」是假的。

        ``ts`` 不在本模块盖：循环体不引入时间源（否则每条记账都变成
        时间相关的不可测代码），由 orchestrator 在候选访问收口时统一加盖
        （``orchestrator._stamp_llm_calls``，候选级粒度口径见
        ``RunRecord.llm_calls`` 的字段 docstring）。
        """
        usage = getattr(result, "usage", None) or {}
        self.calls.append(
            {
                "caller": "agent_loop",
                "candidate": self.task.url,
                "model": getattr(
                    getattr(self._backend, "cfg", None), "model", ""
                ),
                "latency_ms": getattr(result, "latency_ms", 0),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "error": getattr(result, "error", ""),
            }
        )

    def _emit(
        self, disposition: Any, verdict: Verdict | None = None
    ) -> None:
        """落 gatekeeper 事件。**与 orchestrator 的搜索页拦截同格式**
        （``stage/verdict/reason/disposition``）——两处格式分叉会让读账本
        的人得写两份解析。

        ⚠️ 每一档处置都要落，包括 InjectGuidance（D-A2 之后没有静默路径：
        凡是代码插手过的，账本上必须留一条）。而 :class:`FailClosed` 的
        ``branch`` 在这里就填满——事件是给人看的，读它的人不该还要去
        查一遍映射表才知道这一条收口去了哪支分支。

        第四档 :class:`RejectedReport` 是 A2b 加的：它没有 body 级
        ``reason``（被反驳的是一条 URL，不是一次请求），所以下面那句
        取 reason 时会退到它的 ``url``——**空的 reason 比空的账本行更难读**，
        而这条事件恰恰是 A3 幻觉计数的唯一数据源。
        """
        # ``InjectGuidance`` 自带 verdict，其余几档没有——统一在这里
        # 补到事件顶层，读账本的人才不必去 ``disposition`` 里翻。
        got = verdict if verdict is not None else getattr(disposition, "verdict", None)
        self.events.append(
            {
                "stage": "agent",
                "verdict": got.value if isinstance(got, Verdict) else "",
                "reason": str(
                    getattr(disposition, "reason", "")
                    or getattr(disposition, "url", "")
                    or ""
                ),
                "disposition": disposition.to_event(),
            }
        )

    # ── 小工具 ────────────────────────────────────────────────

    def _note(self, text: str) -> None:
        self._pending_notes.append(text)

    def _flush_user(self) -> None:
        """把待发的观察 / 引导拼成**一条 user 消息**。没内容就不发。

        观察与引导合并成一条而不是两条：模型拿到的是「你看到的是这个
        页面，并且上一件事是这样的」——一个当前状态的两个部分。分成两条
        会让最新那条被当成唯一事实，而观察才是主体。
        """
        parts: list[str] = []
        if self._pending_obs is not None:
            parts.append(_render_observation(self._pending_obs))
            self._pending_obs = None
        if self._pending_notes:
            parts.extend(self._pending_notes)
            self._pending_notes.clear()
        if parts:
            self.messages.append({"role": "user", "content": "\n".join(parts)})

    def _ref_whitelist(self) -> frozenset[str]:
        """I6 白名单——**取自渲染进对话的那一份**（见
        :func:`_visible_interactive`）。"""
        return frozenset(
            el.ref for el in _visible_interactive(self.obs)
        )

    def _close(self, *, reason: str, verdict: Verdict | None) -> AgentOutcome:
        return AgentOutcome.close(
            reason=reason,
            verdict=verdict,
            steps=self._steps_taken,
            calls=self._calls_made,
            transcript=self._transcript(),
        )

    def _transcript(self) -> tuple[Mapping[str, str], ...]:
        return tuple(dict(m) for m in self.messages)


async def _apply(driver: "PageDriver", instr: ActionInstruction) -> None:
    """把指令分发到驱动原语。**只分发，不含任何业务语义**。

    与 ``actions.TOOLS`` 的语义层名字对齐：``fill`` → ``type_text``、
    ``press`` → ``press_key``。驱动层的名字是引擎的事，进存档的是语义层
    名字（换引擎不必重造历史数据）。

    Raises:
        DriverError: 导航失败 / 连接断开。点击不存在**不抛**（那是业务
            事实），但导航失败是能力问题，必须 fail-closed。
    """
    if instr.tool == "goto":
        await driver.goto(_as_text(instr.params.get("url")))
    elif instr.tool == "click":
        await driver.click(ref=instr.ref or None)
    elif instr.tool == "fill":
        await driver.type_text(
            text=_as_text(instr.params.get("text")), ref=instr.ref or None
        )
    else:
        await driver.press_key(_as_text(instr.params.get("key")))


async def agent_loop(
    driver: "PageDriver",
    client: "LLMClient",
    escalate: "LLMClient | None",
    task: AgentTask,
    steps: StepLog,
    budget: AgentBudget,
    *,
    initial_obs: Observation,
    json_of: Callable[[str], Mapping[str, Any] | None],
    gatekeeper_events: list[dict[str, Any]] | None = None,
    llm_calls: list[dict[str, Any]] | None = None,
    max_chars: int | None = None,
    dead_loop: DeadLoopDetector | None = None,
) -> AgentOutcome:
    """在一个候选站点内**自主导航**，直到「到了 / 放弃 / 被收口」。

    Args:
        driver: 页面驱动。**进站动作已由调用方做完**——本循环不负责
            probe 与首个 goto，也不负责把起始观察 ``settle`` 到动作流上
            （那是 :func:`_open_tab` 那一段的活）。
        client: 主后端（须有 ``chat_messages`` 方法，与
            :meth:`trajectory_pipeline.llm.client.LLMClient.chat_messages`
            同签名）。
        escalate: 升配后端。``None`` = 未配置（升配处置只落事件不切后端）。
        task: 目标上下文。
        steps: 站点级动作流。本循环往里 ``act`` + ``settle``，
            **不碰已有的步**（进站那一步是调用方的）。
        budget: 两层预算。
        initial_obs: 起始观察。**必给**——没有观察就没有可决策的东西，
            而「取不到起始观察」是调用方已经收口的 ``unreachable_hard``，
            不该由本循环二次猜测。
        json_of: **text → dict 的解析器，由调用方注入**（依赖方向见模块
            docstring）。生产侧传
            ``lambda raw: extract_json(strip_think(raw))``。
        gatekeeper_events: ``RunRecord.gatekeeper_events`` 的透传容器。
            ``None`` = 丢弃（单测用）。
        llm_calls: llm_calls 账本容器。``None`` = 丢弃（单测用）。
        max_chars: 传给 ``driver.observe`` 的正文预算。
        dead_loop: 外部传入的死循环检测器（跨步共享时用）。``None`` =
            本循环自建一个。

    Returns:
        :class:`AgentOutcome`。**本函数不抛异常**，也不产分支——
        记账与折算由调用方（``agent_visit``）拿着 outcome 去做。
    """
    loop = _Loop(
        driver=driver,
        client=client,
        escalate=escalate,
        task=task,
        steps=steps,
        budget=budget,
        events=gatekeeper_events if gatekeeper_events is not None else [],
        calls=llm_calls if llm_calls is not None else [],
        json_of=json_of,
        initial_obs=initial_obs,
        max_chars=max_chars,
        dead_loop=dead_loop,
    )
    return await loop.run()


__all__ = [
    "AGENT_TOOLS",
    "ActionInstruction",
    "AgentBackend",
    "AgentBudget",
    "AgentOutcome",
    "AgentTask",
    "RejectedReport",
    "SelectedRef",
    "TASK_ENTRIES",
    "TASK_KINDS",
    "TASK_PLAY",
    "TASK_REFERRAL",
    "UNRESOLVED",
    "agent_loop",
    "parse_instruction",
]
