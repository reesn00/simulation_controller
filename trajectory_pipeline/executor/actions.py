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
   点击目标一律走语义化的 :class:`ActionTarget`（``tag`` + ``label``）。

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

from dataclasses import dataclass, replace
from typing import Any, Callable, Literal, Mapping

from trajectory_pipeline.perception.base import Observation

#: **当前控制流会发出的**动作原语名。刻意用
#: :class:`~trajectory_pipeline.executor.browser.page_driver.PageDriver`
#: 的协议名，**不是 obscura 的 tool 名**——把上游实现焊进训练集，
#: 换一个浏览器引擎就得重造全部历史数据。
#:
#: ⚠️ 这里列的是「**控制流现在真的会发出去的**」，不是 PageDriver 的全部原语
#: （``type_text`` / ``press_key`` / ``evaluate`` / ``close_tab`` 都不在内）。
#: 检索走 ``goto(search_url)`` 不走「输入 + 提交」，所以 ``type_text``
#: 至今没有产地；``observe`` 按设计**不是动作**（见下）。
#:
#: 收窄是刻意的：P2 会拿这份清单当**训练动作空间**。列宽了就会训出
#: 一批执行流从未产生过的动作，而那类样本永远不会被真实运行验证过。
#: 控制流哪天真的发了新原语，这里跟着加——加的时候那条原语就有真实存档兜底。
TOOLS: tuple[str, ...] = ("goto", "click", "new_tab")

#: 动作来源。``model`` = 模型会选的动作；``infrastructure`` = 执行器自己做的。
ActionOrigin = Literal["model", "infrastructure"]


@dataclass(frozen=True, slots=True)
class ActionTarget:
    """语义化的交互目标。**没有 ref 字段，且刻意不加。**

    ``label`` **不在此处截断**：P1 是证据，截断丢的是人工排障要用的原文。
    训练态的裁剪是 assembler 的活（那是喂模型的那一份，与取证那份不同物）。
    """

    tag: str
    label: str

    def to_json(self) -> dict[str, Any]:
        return {"tag": self.tag, "label": self.label}


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
            return ActionTarget(tag=str(getattr(el, "tag", "")),
                                label=str(getattr(el, "label", "")))
    return None


@dataclass(frozen=True, slots=True)
class Action:
    """一次动作。**字段里没有页面句柄，也没有 ref。**"""

    tool: str
    params: Mapping[str, Any]
    origin: ActionOrigin = "model"
    target: ActionTarget | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "params": dict(self.params),
            "origin": self.origin,
            "target": self.target.to_json() if self.target else None,
        }


@dataclass(frozen=True, slots=True)
class ActionStep:
    """一个动作 + **紧随其后那次 observe 的结果**。

    ``observation`` 为空且 ``error`` 非空 = 动作本身失败了（导航不通、
    点击报错、快照取不到）。这两种「空」必须能分开：前一种是证据，
    后一种是采集失败，混成 ``None`` 就丢了区分。
    """

    action: Action
    observation: Observation | None = None
    error: str = ""

    def to_json(
        self, obs_json: Callable[[Observation | None], dict[str, Any] | None]
    ) -> dict[str, Any]:
        return {
            "action": self.action.to_json(),
            "observation": obs_json(self.observation),
            "error": self.error,
        }


class StepLog:
    """动作流记录器。挂在 :class:`~trajectory_pipeline.executor.steps.visit.VisitResult`
    与 :class:`~trajectory_pipeline.executor.orchestrator.RunRecord` 上。

    刻意**两段式**（``act`` 拿下标、``settle`` 回填观察）：导航先发生、
    观察后拿到，中途失败也要留下「试过且失败」这一步。不两段式的话，
    失败分支在动作流里会整段消失——而「点了但点不动」正是负样本最该留的证据。
    """

    __slots__ = ("steps",)

    def __init__(self) -> None:
        self.steps: list[ActionStep] = []

    def act(self, action: Action) -> int:
        """记一个动作，返回下标；观察随后用 :meth:`settle` 挂上去。"""
        self.steps.append(ActionStep(action=action))
        return len(self.steps) - 1

    def settle(
        self,
        index: int,
        observation: Observation | None = None,
        *,
        error: str = "",
    ) -> None:
        """给第 ``index`` 步回填观察（或失败原因）。"""
        self.steps[index] = replace(
            self.steps[index], observation=observation, error=error
        )


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


__all__ = [
    "TOOLS", "ActionOrigin", "ActionTarget", "Action", "ActionStep",
    "StepLog", "target_of", "goto", "new_tab", "click",
]
