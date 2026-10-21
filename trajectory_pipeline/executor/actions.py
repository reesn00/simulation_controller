"""动作流——**动作 + 工具参数**的唯一定义处，P1 的 ``steps[]`` 与 P2 六件套
第 ④⑤ 件都从这里出。

为什么现在才建
--------------
P1 的设计一直是「动作 + 工具参数 + 观察原文」，而落地时只有观察：
:func:`~trajectory_pipeline.executor.steps.visit.visit_site` 里
``driver.click(ref=ref)`` 的 ``ref`` 是栈帧上的局部变量，出函数即消失。
后果不是「存档少个字段」，而是 **P2 造不出来**——六件套里
「工具参数列表」「动作+参数」两件没有数据源。

三条设计口径
------------

1. **``observe`` 不是动作。** 它是环境对上一个动作的回执。
   记成动作会让模型学出「调用 observe」这种它永远不该发出的动作，
   而训练集里每一步都多一个非选择项。约定：**一次动作 + 随后那次 observe
   = 一个 :class:`ActionStep`**。

2. **动作里没有 ``ref``，是结构保证不是过滤规则。**
   ``ref`` 是 obscura 的**会话内句柄**（见
   :class:`~trajectory_pipeline.perception.base.InteractiveElement`：
   「导航前有效」），换个 session 就失效——它进训练数据等于让模型学一个
   随机数，且样本永远不可回放。所以 :class:`Action` 的字段里压根没有它的位置，
   点击目标一律走语义化的 :class:`ActionTarget`（``role`` + ``name``；
   obscura 面那两个槽里装的是 HTML 标签名与可见文字——**槽位名统一、
   值域随方言**，见 :mod:`trajectory_pipeline.perception.base` 的
   ``ELEMENT_VOCAB_*``）。

   ⚠️ 准确地说，这是**避免新造一个**，不是移除一个：实测 4 份真实存档里
   ``ref=`` 出现 **0 次**——``ref`` 从来没进过 P1（成功路径只记 ``PLAYER_OK``
   的 evidence，而带 ref 的是 ``FIND_PLAY_CONTROL`` 的结论，那条在成功时
   不记账）。所以动作流的 ``tag`` + ``label`` 比 P1 原来有的**更多**，
   而不是更少：原来「点了哪个元素」在存档里根本没留。

3. **``origin`` 区分「模型会选」与「执行器自己做的」。** 会话隔离用的
   ``new_tab`` 是执行器的实现细节，模型永远不该输出它——但它是真实发生过
   的动作，删掉会让 P1 无法回放。两种做法都全，各取一半：记下来并打标，
   由 assembler 决定哪些进训练动作流。

模块位置
--------
放在 ``executor/`` 而不是 ``assembler/``：动作词汇表由**唯一执行动作的一方**
拥有（「遍历谁、按什么顺序、点哪个、记什么，全部是代码」）。assembler 将来
**不 import 本模块**——它读 P1 的 JSON，与 :mod:`trajectory_pipeline.executor.plan`
消费模块 1 的计划是同一条纪律：**交接面是文件格式，不是 import**。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal, Mapping

from trajectory_pipeline.common.elements import (
    ELEMENT_VOCAB_ARIA_ROLE,
    ELEMENT_VOCAB_HTML_TAG,
    element_keys,
)
from trajectory_pipeline.perception.base import (
    Observation,
)

#: **当前控制流会发出的**动作原语名。刻意用
#: :class:`~trajectory_pipeline.executor.browser.page_driver.PageDriver`
#: 的协议名，**不是 obscura 的 tool 名**——把上游实现焊进训练集，
#: 换一个浏览器引擎就得重造全部历史数据。
#:
#: ⚠️ 这里列的是「**控制流现在真的会发出去的**」，不是 PageDriver 的全部原语
#: （``evaluate`` 不在内）。检索走 ``goto(search_url)`` 不走「输入 + 提交」，
#: 所以 `fill` / `press` 的**驱动原语**至今没有直接产地——2026-10-16 路径 B/C
#: 的站内搜索链给它们补上了语义层包装：``fill``（把片名填进搜索框）与
#: ``press``（回车提交）；``close_tab`` 同轮随外链校验（``new_tab`` 开、
#: 看完关）获得产地。``observe`` 按设计**不是动作**（见下）。
#:
#: 2026-10-20（P2）起，这里列的名字与
#: :class:`~trajectory_pipeline.executor.browser.page_driver.PageDriver`
#: 的**驱动原语名逐字相同**（旧的 `type_text` / `press_key` 已改名为 `fill` / `press`）。
#: 换句话说**语义层与驱动层之间不再有翻译表**——那张表必须存在时就是 bug 的信号。
#:
#: 收窄是刻意的：P2 会拿这份清单当**训练动作空间**。列宽了就会训出
#: 一批执行流从未产生过的动作，而那类样本永远不会被真实运行验证过。
#: 控制流哪天真的发了新原语，这里跟着加——加的时候那条原语就有真实存档兜底。
TOOLS: tuple[str, ...] = ("goto", "click", "new_tab", "fill", "press", "close_tab")

#: 动作来源。``model`` = 模型会选的动作；``infrastructure`` = 执行器自己做的。
ActionOrigin = Literal["model", "infrastructure"]


@dataclass(frozen=True, slots=True)
class ActionTarget:
    """语义化的交互目标。**没有 ref 字段，且刻意不加。**

    ``role`` / ``name`` 是**槽位名**，不是值域保证：值属于哪套词表由这批
    存档的执行层决定（obscura 面的 ``role`` 装 HTML 标签名，camoufox 面
    装 ARIA role，见 :data:`perception.base.ELEMENT_VOCAB_HTML_TAG`）。
    **存档里的键名也跟着方言走**——:meth:`to_json` 收词汇表，按它投影；
    统一键名而不统一值域，等于把两个方言写成同一种数据。

    ``name`` **不在此处截断**：P1 是证据，截断丢的是人工排障要用的原文。
    训练态的裁剪是 assembler 的活（那是喂模型的那一份，与取证那份不同物）。
    """

    role: str
    name: str

    def to_json(self, vocabulary: str = ELEMENT_VOCAB_HTML_TAG) -> dict[str, Any]:
        kind_key, text_key = element_keys(vocabulary)
        return {kind_key: self.role, text_key: self.name}


def target_of(
    ref: str, elements: tuple[Any, ...]
) -> ActionTarget | None:
    """把 ``ref`` 解成语义目标。解不出返回 ``None``（**不猜**）。

    走 ``elements`` 而不是让感知层在 payload 里回传 label：不变式 I6 已经
    保证 ``ref`` 可溯源到 :attr:`Observation.interactive_elements`，
    在这里查就是复用那条契约，不必给七不变式再加一条、也不必改感知层的
    payload 形状（W3 的 ``LLMPerceptor`` 接上时少一处要对齐）。
    """
    if not ref:
        return None
    for el in elements or ():
        if getattr(el, "ref", "") == ref:
            # 槽位搬过去，**值不动**：``role`` 在 obscura 面就是 HTML 标签名。
            # 这里不做「标签名 → ARIA role」的翻译——那要猜 ``<input>`` 的
            # type，猜错就是声明与真实调用对不上，而报告上看不出异常。
            return ActionTarget(role=str(getattr(el, "role", "")),
                                name=str(getattr(el, "name", "")))
    return None


class UnaddressableTarget(RuntimeError):
    """有动作要发，但**这个面拿不出可定位的目标**（P5）。

    只可能出在 ``aria_role`` 面：那一面按 ``role+name`` 定位，语义目标缺失
    就没有可发的参数。``html_tag`` 面有 ``ref`` 兜底，目标缺失只影响
    ``steps[]`` 的记账，不影响动作执行。

    **不是接线错误，所以不抛穿**：``ref`` 溯源不到（I6 存疑）是一次真实的
    运行事实，按「这个动作没做成」记账即可——与跨面调用抛 ``ValueError``
    是两码事（那个是编程错误，必须炸出来）。
    """


def target_params(
    vocabulary: str,
    target: ActionTarget | None,
    *,
    ref: str | None = None,
) -> dict[str, Any]:
    """「语义目标 + ref」→ **该方言面**接受的驱动参数（P5 的唯一派发点）。

    | 面 | 参数 |
    |---|---|
    | ``html_tag``（obscura） | ``{"ref": …}`` |
    | ``aria_role``（camoufox） | ``{"role": …, "name": …}`` |

    ``vocabulary`` 从观察自带（``Observation.element_vocabulary``），
    不是调用方猜的第二个来源——与 ``_obs_json`` 同一条纪律。

    Raises:
        UnaddressableTarget: ``aria_role`` 面但 ``target`` 为 ``None``。
        ValueError: 认不出的词汇表。**不回落成 ref**——两面的参数名不同，
            回落等于发一个对面不认的参数（驱动会抛跨面 ``ValueError``，
            而那个异常长得像接线 bug）。
    """
    if vocabulary == ELEMENT_VOCAB_HTML_TAG:
        return {"ref": ref}
    if vocabulary == ELEMENT_VOCAB_ARIA_ROLE:
        if target is None:
            raise UnaddressableTarget(
                "aria_role 面按 role+name 定位，但这次动作没有语义目标"
                "（ref 溯源不到 interactive_elements，I6 存疑）"
            )
        # 槽位原样搬过去，**不做值翻译**：target.role 在 camoufox 面
        # 本来就是 ARIA role（值为证的方言见 common/elements.py）。
        return {"role": target.role, "name": target.name}
    raise ValueError(
        f"认不出的元素词汇表 {vocabulary!r}——动作参数无从确定。"
        f"已知的：{ELEMENT_VOCAB_HTML_TAG} / {ELEMENT_VOCAB_ARIA_ROLE}"
    )


@dataclass(frozen=True, slots=True)
class Action:
    """一次动作。**字段里没有页面句柄，也没有 ref。**"""

    tool: str
    params: Mapping[str, Any]
    origin: ActionOrigin = "model"
    target: ActionTarget | None = None

    def to_json(self, vocabulary: str = ELEMENT_VOCAB_HTML_TAG) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "params": dict(self.params),
            "origin": self.origin,
            "target": self.target.to_json(vocabulary) if self.target else None,
        }


@dataclass(frozen=True, slots=True)
class ActionStep:
    """一个动作 + **紧随其后那次 observe 的结果**。

    ``observation`` 为空且 ``error`` 非空 = 动作本身失败了（导航不通、
    点击报错、快照取不到）。这两种「空」必须能分开：前一种是证据，
    后一种是采集失败，混成 ``None`` 就丢了区分。

    ``rationale`` 是本步的思考+结论（模块 4）。**一步是它的天然对齐单位**
    ——而 P2 六件套第 ⑥ 件「与动作对齐的一条」就是这一端的另一半。
    **生成在执行期、核查在组装期**：这里只落生成结果，
    一致性由 :mod:`trajectory_pipeline.assembler` 在切条后跑闸门。

    值是 ``dict`` 而不是某个 ``Rationale`` 类型：``executor`` 不得 import
    ``assembler``（依赖方向单向），形状由
    ``docs/contracts/P1-observation.md`` 钉住。
    """

    action: Action
    observation: Observation | None = None
    error: str = ""
    rationale: Mapping[str, Any] | None = None
    #: 本步的墙钟耗时（毫秒），从动作发起到观察/失败回填。
    #:
    #: **为什么必须有它**（2026-10-21）：camoufox 面的点击超时是 5s，
    #: 而「窗口不可渲染」时实测单次点击要 5–13s（研究产物
    #: ``research/probes/click_occluded.json``）——**同一个 ``error`` 文本**
    #: 背后是「刚好慢一点」还是「慢到离谱」，没有耗时字段就分不出来，
    #: 事后读存档只能靠猜。
    #:
    #: 口径是**整步**（动作 + 随后的观察），不是「动作单独那几秒」：
    #: 失败路径上动作一抛就收口，整步 ≈ 动作耗时——而失败路径正是要诊断的那条。
    #: 成功路径上它含观察耗时，**别当成动作耗时读**。
    #: ``None`` = 这一步没走到 settle（中途异常），**不与「耗时 0」相混**。
    duration_ms: int | None = None

    def to_json(
        self,
        obs_json: Callable[[Observation | None], dict[str, Any] | None],
        vocabulary: str = ELEMENT_VOCAB_HTML_TAG,
    ) -> dict[str, Any]:
        return {
            "action": self.action.to_json(vocabulary),
            "observation": obs_json(self.observation),
            "error": self.error,
            "rationale": dict(self.rationale) if self.rationale else None,
            "duration_ms": self.duration_ms,
        }


class StepLog:
    """动作流记录器。挂在 :class:`~trajectory_pipeline.executor.steps.visit.VisitResult`
    与 :class:`~trajectory_pipeline.executor.orchestrator.RunRecord` 上。

    刻意**两段式**（``act`` 拿下标、``settle`` 回填观察）：导航先发生、
    观察后拿到，中途失败也要留下「试过且失败」这一步。不两段式的话，
    失败分支在动作流里会整段消失——而「点了但点不动」正是负样本最该留的证据。

    **计时也挂在这一对方法上**（``act`` 起表、``settle`` 收表），所以
    :class:`ActionStep` 的 ``duration_ms`` 不需要每个调用点自己测——
    调用点多到会漏（八处 ``settle``），漏一处就少一条耗时证据。
    """

    __slots__ = ("steps", "_started")

    def __init__(self) -> None:
        self.steps: list[ActionStep] = []
        #: 每步的动作发起时刻（``time.perf_counter``）。与 ``steps`` 同下标。
        #:
        #: ⚠️ **用 ``perf_counter`` 而不是 ``monotonic``**：Windows 上
        #: ``monotonic`` 走 ``GetTickCount64``，**分辨率 15.625ms**——量出来的
        #: 耗时会被量化到 15.6ms 的刻度上，量 30ms 可能读出 15ms。
        #: ``perf_counter`` 走 ``QueryPerformanceCounter``，分辨率 1e-07。
        #: 两者都单调，**只做同进程内相减**，换过去不引入任何语义变化。
        self._started: list[float] = []

    def act(self, action: Action) -> int:
        """记一个动作，返回下标；观察随后用 :meth:`settle` 挂上去。"""
        self.steps.append(ActionStep(action=action))
        self._started.append(time.perf_counter())
        return len(self.steps) - 1

    def settle(
        self,
        index: int,
        observation: Observation | None = None,
        *,
        error: str = "",
    ) -> None:
        """给第 ``index`` 步回填观察（或失败原因）。

        耗时在**第一次** settle 时定下——之后再 settle 不改写它：
        这一步的「结束时刻」是第一次收口那一刻，不是最后一次。
        """
        step = self.steps[index]
        duration = step.duration_ms
        if duration is None and 0 <= index < len(self._started):
            duration = int((time.perf_counter() - self._started[index]) * 1000)
        self.steps[index] = replace(
            step,
            observation=observation,
            error=error,
            duration_ms=duration,
        )

    def attach_rationale(self, index: int, rationale: Mapping[str, Any] | None) -> None:
        """挂第 ``index`` 步的 rationale。

        **刻意与 :meth:`settle` 分开**：``settle`` 是采集的结果（执行器
        拿到的硬事实），``attach_rationale`` 是生成的产物（LLM 写的）。
        两者混在一个方法里，调用方就会以为「挂上就说明这一步成功」——
        而 writer 的三种状态（``written`` / ``unavailable`` / ``empty``）
        与这一步有没有错无关。
        """
        self.steps[index] = replace(self.steps[index], rationale=rationale)


def goto(url: str) -> Action:
    """导航。``origin="model"``：选不选这个站是模型的判断。"""
    return Action(tool="goto", params={"url": url}, origin="model")


def new_tab(url: str) -> Action:
    """开独立 tab。``infrastructure``：会话隔离，模型不选它。"""
    return Action(tool="new_tab", params={"url": url}, origin="infrastructure")


def click(target: ActionTarget | None) -> Action:
    """点击。目标为 ``None`` 时照实记——那是 I6 契约被违反的现场，
    不在这里编一个目标补上（fail-closed：能力不可用就记不可用）。"""
    return Action(tool="click", params={}, origin="model", target=target)


def fill(target: ActionTarget | None, text: str) -> Action:
    """往输入框里填文本（站内搜索：把片名填进搜索框）。

    ``origin="model"``：选哪个框填什么词是感知层判断的落地。目标为
    ``None`` 时与 :func:`click` 同口径——照实记，不编目标。
    **名字与驱动原语同名同义**（``PageDriver.fill`` = 设值，不是追加）。
    """
    return Action(tool="fill", params={"text": text}, origin="model", target=target)


def press(key: str, target: ActionTarget | None = None) -> Action:
    """按键（站内搜索：回车提交检索）。

    ``target`` 可空：回车提交是全局按键，不指向具体元素；点「搜索」
    按钮提交时才带目标（那一路驱动层走 :func:`click`，不走这里）。
    """
    return Action(tool="press", params={"key": key}, origin="model", target=target)


def close_tab() -> Action:
    """关掉当前 tab。``infrastructure``：外链校验的开合（``new_tab`` 的对侧），
    模型不选它。"""
    return Action(tool="close_tab", params={}, origin="infrastructure")


__all__ = [
    "TOOLS", "ActionOrigin", "ActionTarget", "Action", "ActionStep",
    "StepLog", "target_of", "target_params", "UnaddressableTarget",
    "goto", "new_tab", "click", "fill", "press", "close_tab",
]
