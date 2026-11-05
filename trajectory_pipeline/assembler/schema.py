"""P2 单条样本的形状——**六件套**的值对象与 P1 读取器。

六件套（设计方案 §3.5）::

    ① 系统提示词      SYSTEM_PROMPT
    ② 工具参数列表    tools: tuple[ToolSpec, ...]
    ③ 任务提示词      user_prompt + provenance
    ④ 动作+参数       actions: tuple[Action, ...]
    ⑤ 观察            observations: 训练态视图（见 observation_view）
    ⑥ 思考+结论       rationales: tuple[Rationale, ...]（与 actions 按 step_index 对齐）

四条决定

--------
1. **读取 P1 按文件格式，不 import ``executor``。**
   与 :mod:`trajectory_pipeline.executor.plan` 消费模块 1 的计划同一条纪律：
   **交接面是格式，不是 import**。理由是 P1 贵、P2 便宜——重建一批 P2 必须
   独立于 executor 的**当前版本**。一旦 ``import executor``，executor 改一个
   dataclass 字段就可能让半年前那批 P2 重建不出来，而重建不出来的那批就是
   永久损失。契约测试 ``tests/contract/test_assembler.py`` 盯着这条。

2. **一条样本 = 一个任务的一条轨迹**（2026-10-30 · 方案 23 改）。
   ⚠️ **2026-10-30 之前是「一条样本 = 一个决策单元」**（``search`` 阶段一条 /
   每个站点一条）——那是 2026-10-15 的设计（设计方案 §3.5「单站点验证、
   单搜索任务各自独立成条」），当时担心「合成一条长轨迹会把『选哪个站』和
   『这个站行不行』压进同一次预测里」。

   改判的理由：**下游真正要的是「一个任务一条轨迹」**——训练样本、
   Label Studio 上传的合成数据、黄金集三边对齐到同一个粒度键
   ``(task_id, run_id)``，而按站点切时三边对不齐（黄金集一直是站点级、
   LS 上传是轨迹级），对不齐就没法把「这条轨迹在训练里长什么样」与
   「人给它打了几分」对上。

   决策单元**没有消失**，它降级成**轨迹内的结构**：站点级结论保留在
   :attr:`Sample.site_outcomes` 里（裁定 ③），只是不再被切成独立样本。

3. **缺件必须看得见，不能是 ``None``。**
   W1 没有 rationale 模块（模块 4 待建），六件套第 ⑥ 件整个拿不到。
   若只写 ``rationale=None``，样本与「模型这一轮没说话」长得一样——
   而前者是**流水线没长出来**，后者是**数据事实**。所以另有
   :attr:`Sample.rationale_missing` 与 :class:`GateMark`：闸门与理由分列，
   读的人一眼能分出「没跑」与「没过」。

4. **工具清单不从 P1 反推，但也不在本模块声明。**
   P1 的 ``steps[]`` 只记**这一条样本实际用过**的动作，而「训练动作空间」
   必须是整个任务空间——从存档反推会让每条样本各带各的动作空间，
   模型看到的世界随样本变。反过来，从 ``executor.actions.TOOLS`` import
   又把 assembler 焊死在执行端（违背纪律 1）。
   取折中：**清单的唯一真源在 :mod:`trajectory_pipeline.toolcatalog`**
   （跨层地基，不 import 任何功能模块），本模块从它派生
   :data:`TOOL_SPECS`。P3（``assembler.views``）与 T（``assembler.task_view``）
   也从它取——三处不必各抄一份，漂移是**结构上不可能**的，而不只是
   「靠测试盯着」。
   由契约测试拿真实存档交叉验证没有漏项（漂移是测试的事，不是 import 的事）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from trajectory_pipeline import toolcatalog
from trajectory_pipeline import outcomerules
from trajectory_pipeline.assembler import closing, contracts, excluded

#: ① 系统提示词。**随样本一起冻结**（进 P2 文件），不是渲染时读全局常量——
#: 常量改一次，历史样本的系统提示词就全变了，而样本之间原本是可比的。
#:
#: 内容口径：只描述**这一条任务里模型的角色与硬约束**，不掺任何执行端实现
#: （不提 obscura、不提 MCP、不提 ref 句柄）。模型学到的是「遇到这样的用户
#: 与页面该怎么办」，不是「怎么驱动某个浏览器」。
SYSTEM_PROMPT = (
    "你是能帮用户找在线观看资源的助手。\n"
    "拿到用户的诉求、可用工具，以及操作后页面返回的观察。\n"
    "按下面的规则行动：\n"
    "1. 只根据观察里出现的东西判断，不要根据网址或片名猜。\n"
    "2. 找不到能看正片的资源时如实说找不到，不要编造链接。\n"
)


#: ② 训练动作空间。刻意**只列控制流真会发出的动作**——多列一个就会训出
#: 一批永远不会被真实运行验证过的动作，而那类样本在离线评测里是绿的。
#: ``new_tab`` 是**执行器自己的**会话隔离动作，模型永远不该选它；
#: 它仍列在这里是因为它是真实发生过的动作，删掉 P1 就无法回放。
#: 由 :meth:`ToolSpec.visible_to_model` 而非 ``origin`` 决定模型看不看得见。
@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个动作原语的训练侧声明。

    :attr:`params` 是**参数名元组**——参数级的说明与 ``required`` 集合在
    :mod:`trajectory_pipeline.toolcatalog` 里（那边是唯一真源），本类只
    保留 P2 契约需要的形状。

    :attr:`visibility` 决定这条进不进**模型可见**的工具声明。
    ``new_tab`` / ``close_tab`` 是执行器自己的会话管理动作（外链校验的
    开合），模型永远不该选它们——但它们**留在 P2 的动作空间里**
    （删了 P1 就无法回放）。
    """

    name: str
    params: tuple[str, ...]
    instruction: str
    visibility: str = "model"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": list(self.params),
            "instruction": self.instruction,
        }

    def visible_to_model(self) -> bool:
        """这条动作该不该出现在喂给模型的工具列表里。

        判据是 :attr:`visibility`（来自
        :mod:`trajectory_pipeline.toolcatalog` 的唯一真源），**不是名字
        黑名单**——按名字判的话，catalog 里新增一个 infrastructure 动作
        就会漏进模型可见集，而模型发一个它永远不该发的动作就是**教它
        越权**。
        """
        return self.visibility == "model"


#: ② 训练动作空间。**从 :mod:`trajectory_pipeline.toolcatalog` 派生**——
#: 那份是唯一真源，P3（:mod:`trajectory_pipeline.assembler.views`）与
#: T（:mod:`trajectory_pipeline.assembler.task_view`）也从它取，三处
#: 天然一致，不必再抄第二份第三份。
#:
#: ⚠️ 这份清单**不是**「执行端会发哪些动作」——那由
#: :data:`trajectory_pipeline.executor.actions.TOOLS` 拥有（**收窄是
#: 刻意的**，理由写在那条注释里）。本清单是**喂给模型的工具声明**：
#: 13 条里 11 条模型可见（其中 7 条是**能力声明**——本项目没有任何
#: 真实运行产生过它们的调用），2 条是执行器的 infrastructure 动作。
#: 两者差集由 ``tests/contract/test_tool_catalog_contract.py`` 机械盯着。
#:
#: 「只列控制流真会发出的动作」这条老口径**在本清单上已不成立**：
#: 它原本保护的是「模型发出一个永远不会被验证的动作」。而**声明**了
#: 但没人调用的工具不产生这种动作——它只让模型知道存在这个能力。
#: 真正要防的「模型发出未验证的调用」由
#: :attr:`ToolSpec.visibility`（infrastructure 挡在模型可见集外）与
#: 样本级的 ``meta.tools_unused``（人可复核）共同兜住。
TOOL_SPECS: tuple[ToolSpec, ...] = tuple(
    ToolSpec(
        name=d.name,
        params=tuple(p.name for p in d.params),
        instruction=d.instruction,
        visibility=d.visibility,
    )
    for d in toolcatalog.CATALOG
)

#: **逐工具面**的训练动作空间（2026-10-20 方案 15 §7 R1）。
#:
#: 为什么多这一层：``click`` 在 obscura 面是 ``click(tag,label)``、在
#: aria 面（camoufox / camofox）是 ``click(role,name)``——**同名不同参**。一份 P2 样本
#: 只对应一个面（由源存档的 ``driver`` 决定），所以六件套第 ② 件
#: (``tools``) 必须**按面**取，不能一份通吃。
#:
#: 一份存档只对应一个面 ⇒ **不存在「同一条样本混了两个方言」的状态**。
#: 两个面也**不混池**（方案 04 §4.5.5）：目标编码不同义。
TOOL_SPECS_BY_SURFACE: Mapping[str, tuple[ToolSpec, ...]] = {
    surface: tuple(
        ToolSpec(
            name=d.name,
            params=tuple(p.name for p in d.params),
            instruction=d.instruction,
            visibility=d.visibility,
        )
        for d in defs
    )
    for surface, defs in toolcatalog.SURFACES.items()
}


@dataclass(frozen=True, slots=True)
class ActionView:
    """④ 动作+参数（训练侧）。

    ``ref`` **不在字段里**，与 :class:`trajectory_pipeline.executor.actions.ActionTarget`
    同一个理由：``ref`` 是 obscura 的会话内句柄，换个 session 就失效，
    进训练数据等于让模型学一个随机数，且样本永远不可回放。点击目标走
    ``tag`` + ``label`` 两个可读的语义字段。
    """

    tool: str
    params: Mapping[str, Any] = field(default_factory=dict)
    target: Mapping[str, str] | None = None
    origin: str = "model"

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "params": dict(self.params),
            "target": dict(self.target) if self.target else None,
            "origin": self.origin,
        }


@dataclass(frozen=True, slots=True)
class Rationale:
    """⑥ 思考+结论（模块 4）——**与动作对齐的一条**。

    ``text`` 是 **refined CoT 的位点**，不是 raw CoT：raw CoT 受红线约束不得落盘，
    而训练需要 CoT SFT。这里留的是**结构**。

    ⚠️ **一条样本可能有多个动作，也就可能有多条 rationale**（2026-10-10 改）。
    原设计是「整条样本一条」，而执行期是**逐步**生成的
    （P1 的 ``steps[i].rationale``），两者对不上。改成按
    :attr:`step_index` 与 :class:`ActionView` 一一对齐后：

    - 训练态的思考与动作**逐步对应**，模型学到的是「这一步为什么这么做」；
    - 闸门能逐条核（时序核查本来就需要 ``step_index``）；
    - 长度对不上本身就是**要看得见的信号**（见 :func:`align_rationales`）。

    ``status`` 原样带 P1 的三态（``written`` / ``unavailable`` / ``empty``）。
    **不合成占位文本**：缺一条与写错一条在训练里不是同一件事。
    """

    step_index: int
    text: str
    conclusion: str = ""
    status: str = "written"
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "text": self.text,
            "conclusion": self.conclusion,
            "status": self.status,
            "reason": self.reason,
        }


def align_rationales(
    actions: tuple[ActionView, ...],
    raw: Sequence[Mapping[str, Any] | None],
) -> tuple[Rationale, ...]:
    """把 P1 的 ``steps[i].rationale`` 按动作对齐。

    **缺失就是缺失**——不补空串、不挪位、不按出现顺序猜。理由：

    - 补空串会让「模型没说话」与「有话说但没落」同形（§「三个状态而不是两个」）；
    - 按出现顺序猜会在中间缺一条时**整体错位**，而错位的 rationale
      会被时序核查当成幻觉——**凭空造出负样本**。

    ``raw[i]`` 按下标对应 ``actions[i]``；P1 里的 ``rationale`` 缺键或为
    ``None`` 时产出空元组，那一步就没有 rationale。
    """
    out: list[Rationale] = []
    for i, row in enumerate(raw[: len(actions)]):
        if not isinstance(row, Mapping):
            continue
        payload = row.get("rationale")
        if not isinstance(payload, Mapping):
            continue
        # P1 里落的是 ``WriteResult.to_json()`` 的形状——文本在
        # **``rationale`` 子对象**里，``status``/``reason`` 与它同级。
        # 这层不能拍平成 ``payload["text"]``：那样读出来恒为空，
        # 而症状是「模型什么都没写」，不是「读错了字段」。
        if payload.get("status") != "written":
            # 三态原样带下来：unavailable/empty 的文本位留空
            out.append(
                Rationale(
                    step_index=i,
                    text="",
                    conclusion="",
                    status=str(payload.get("status") or "unavailable"),
                    reason=str(payload.get("reason") or ""),
                )
            )
            continue
        draft = payload.get("rationale")
        draft = draft if isinstance(draft, Mapping) else {}
        out.append(
            Rationale(
                step_index=i,
                text=str(draft.get("text") or ""),
                conclusion=str(draft.get("conclusion") or ""),
                status="written",
            )
        )
    return tuple(out)


# ── rationale 口径（INC46 分代）─────────────────────────────────────

#: 第 ``i`` 步的 rationale 是**执行之前的动机**（INC46，2026-10-15 起）。
RATIONALE_ERA_PRE_ACTION = "pre_action"

#: 第 ``i`` 步的 rationale 是**看完本步页面之后的反思**（INC46 之前）。
#: 它描述的是 ``observation[i]``，而 P3 把它渲染在 ``action[i]`` **之前**
#: ——训练在教未卜先知。这份文本**不能**按新口径的闸门去核。
RATIONALE_ERA_POST_HOC = "post_hoc"


def gate_of(
    rationales: Sequence[Rationale],
    *,
    action_tools: Sequence[str] = (),
    observations: Sequence[Mapping[str, Any]] = (),
    exempt: str = "",
    rationale_era: str = RATIONALE_ERA_PRE_ACTION,
) -> GateMark:
    """跑闸门并汇总成**一条**标记。

    汇总口径（与 :func:`trajectory_pipeline.rationale.gates.gate` 的
    单步口径不同，这一层是新增的）：

    - 任一步 ``failed`` → 整条 ``failed``，**理由逐条列出**（定位到第几步）；
    - 有 ``written`` 且无 hard 发现 → ``passed``；
    - **一条 ``written`` 都没有** → ``not_run``；
    - **旧口径（``post_hoc``）→ ``not_run``**，理由写明「口径不适用」。

    最后两条是关键：**「没生成」绝不能渲染成「通过」**。而
    「生成了一部分」是 ``passed``/``failed`` 的一部分判定，
    没生成的那几步会通过 ``rationale_missing`` 单独可见——
    两个信号分开，别合成一个。

    ⚠️ INC46 口径：rationale 是**动作前动机**，所以 ``observations``
    传给闸门后，第 ``i`` 步的可见窗口是 ``[:i]``（**不含本步观察**）。
    本步的 ``step_index=i`` 语义不变——窗口的截取在
    :func:`~trajectory_pipeline.rationale.gates.gate` 里做，
    本函数只负责**逐条调用并汇总**。调用签名不变是刻意的：
    换口径不该让「闸门怎么跑」这件事在两层里各改一遍。

    ⚠️ **跨口径不判**（2026-10-25）：事后反思的文本本来就该描述
    ``observation[i]``，用 ``[:i]`` 的窗口去核它，**必然**把「第 0 步引用了
    本步页面内容」判成 ``temporal_leak``——实测 8 条样本这样被记成
    ``failed``（它们全是 INC46 之前的档），**凭空造出负样本**。反过来，
    窗口碰巧对得上的旧文本会被记 ``passed``——那是**假通过**。
    两个方向的错都是口径切换造出来的，不是 rationale 的缺陷，所以旧口径
    一律 ``not_run``：**不判通过、不判失败，先重生成再核**。
    """
    from trajectory_pipeline.rationale import gates

    if not rationales:
        return GateMark("not_run", ("本样本没有 rationale——闸门未运行",))
    statuses = [r.status for r in rationales]
    if "written" not in statuses:
        return GateMark(
            "not_run",
            (
                "rationale 一条都没写成（状态："
                + "、".join(sorted(set(statuses)))
                + "）——闸门未运行",
            ),
        )
    if rationale_era == RATIONALE_ERA_POST_HOC:
        return GateMark(
            "not_run",
            (
                "存量 rationale 是 INC46 之前的**事后反思**口径（本单元没有 "
                "unit 级 ``reflection``）——INC46 闸门的可见窗口 ``[:i]`` "
                "对它不适用，**不判通过、也不判失败**。要出分先跑 "
                "``regenerate-rationales`` 把这一档重生成成动作前动机",
            ),
        )

    failed: list[str] = []
    passed_steps = 0
    for r in rationales:
        if r.status != "written":
            continue
        # ⚠️ **用条目自带的 ``step_index``，不用列表下标**：
        # :func:`align_rationales` 的产出是**稀疏**的（没有 rationale 的那一步
        # 整条不出现），按下标取会让后面每一步整体前移一格——
        # 于是第 5 步的 rationale 拿第 4 步的动作与窗口去核。
        # 旧口径下它只是把动作名取错，INC46 起它还把**可见窗口**取错一格，
        # 而窗口错一格就是「把后见之明判成 anchored」或反过来。
        i = r.step_index
        res = gates.gate(
            r.text,
            action_tool=action_tools[i] if 0 <= i < len(action_tools) else "",
            observations=observations,
            step_index=i,
            exempt=exempt,
        )
        if res.status == "failed":
            for f in res.hard_findings:
                failed.append(f"第 {i} 步·{f.kind}：{f.detail}")
        else:
            passed_steps += 1
    if failed:
        return GateMark("failed", tuple(failed))
    reasons = [f"逐条核查 {passed_steps}/{len(rationales)} 步通过"]
    missing = [str(r.step_index) for r in rationales if r.status != "written"]
    if missing:
        reasons.append(f"第 {', '.join(missing)} 步没写成（未参与核查）")
    return GateMark("passed", tuple(reasons))


#: 闸门状态。三值而非两值：**「没跑」不是「通过」**。
#: W1 的样本一律是 ``not_run``——把「没跑」渲染成「通过」会让 batch 上线时
#: 看起来像过了闸，而它根本没被检查过。
GateStatus = Literal["passed", "failed", "not_run"]


def rationale_era_of(
    unit: Mapping[str, Any],
    rationales: Sequence[Rationale],
) -> str:
    """一个决策单元的 rationale 属于哪一代口径。

    这是 **P2 新旧混批唯一的结构判据**。两代在落盘形状上**同构**
    （都是 :meth:`~trajectory_pipeline.rationale.writer.WriteResult.to_json`
    的 ``status`` / ``rationale`` / ``reason`` / ``degraded``），正文里也
    没有可以机器判定的时代印记——**不能靠读内容猜**。

    判据是 **unit 级 ``reflection`` 键在不在**：INC46 的两条写路径
    （执行期 :meth:`~trajectory_pipeline.executor.orchestrator.Orchestrator._write_reflection`
    与存量 :func:`~trajectory_pipeline.executor.cli.cmd_regenerate_rationales`）
    **都必写该键**（哪怕值是 ``null``），而 INC46 之前的存档从不写它。
    所以键缺席 = 这份单元的 rationale 生成时还没有动作前动机口径。

    Returns:
        ``"pre_action"`` / ``"post_hoc"`` / ``""``。空串的含义是
        **没有可判的 rationale**（一条 ``written`` 都没有）——与「旧口径」
        不是一回事：前者没什么可训，后者是**有文本但口径不对**。
    """
    if not any(r.status == "written" for r in rationales):
        return ""
    return (RATIONALE_ERA_PRE_ACTION if "reflection" in unit
            else RATIONALE_ERA_POST_HOC)


@dataclass(frozen=True, slots=True)
class GateMark:
    """闸门标记。**状态与理由分列**，别合成一个字符串。"""

    status: GateStatus
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class OutcomeRef:
    """指回 P1 的那一条 outcome。

    样本里**不复制** evidence 原文，只留指针。理由与 P1 的 ``body_text``
    同源：P1 是真值源，复制就产生第二份需要同步的副本，而两份一旦错位
    （复核后 P1 更新、样本没重建）就是**静默失配**——读样本的人看到的是
    旧结论。

    ``branch is None`` 表示**成功**：P1 的 ``ledger`` 只记失败分支，
    成功的访问不记账。所以「无分支」不是「没结论」——真实存档里
    ``T001__65208dfb`` 的 5 个访问点有 3 个 ``branch=None``，它们全是成功的。

    :attr:`succeeded` 取自 ``visits[i].success``，是另一个独立来源。
    两者对不上时（本模块实测不到，但契约上允许）写 ``degraded_from``，
    因为「成功」直接决定这条样本进不进训练集，而它必须只有一个来源说了算。

    2026-10-10 补四个字段（``question`` / ``reached_play_page`` /
    ``site_url`` / ``play_page_url``）：D-5「记录/排除决策正确性」是
    **代码断言层**的一票否决维度，它的判据链要核「这条结论是**哪个判断点**
    在**有没有到播放页**的前提下得出的」——而这两个信息此前只活在 P1，
    P2 侧拿不到，于是评分器要么读 P1（违背「交接面是格式」，
    且让 P2 的可重建性打折），要么只能判最终结论、判不了过程。
    同 P1 的 ``run_config`` / ``elements_total``：**缺了不是精度下降，
    是那个维度根本没法在样本级判**。
    """

    url: str
    branch: str | None
    source: str = ""
    fallback_used: bool = False
    succeeded: bool = False
    #: 产出这条结论的判断题 id（``perception.base.Q`` 的四个之一）。
    question: str = ""
    #: 是否走到了播放页。D-5 的锚点 1「跳步结论（看 URL 直接记录）」就是
    #: 「``branch is None``（记成功）但 ``reached_play_page`` 为假」。
    reached_play_page: bool = False
    #: 候选链接。D-6「站点 URL」必填字段。
    site_url: str = ""
    #: 播放页地址。**未走到播放页时为空**，此时 D-6 的该项不适用、不扣分
    #: （rubric v1.1 D-6 边界规则）。
    play_page_url: str = ""
    #: P1 的 outcome 里**压根没有** ``site_url`` / ``play_page_url`` 这两个键
    #: （2026-10-10 之前切的存档）。逐条记，不靠「值为空」推断。
    #:
    #: **这条不能省**：缺字段与字段为空在 :meth:`to_json` 之后同形
    #: （都渲染成 ``""``），于是 D-6 分不出「老存档没这个字段」与
    #: 「采集时确实没落」——前者该判不可判定（先重跑），后者才是缺失。
    #: 实测那样判出 20 条假 3 分，而它们的结论本身是对的。
    #: 通用形态见避坑指南 §2.3「缺字段 ≠ 空值」。
    p1_missing_fields: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        out = {
            "url": self.url,
            "branch": self.branch,
            "source": self.source,
            "fallback_used": self.fallback_used,
            "succeeded": self.succeeded,
            "question": self.question,
            "reached_play_page": self.reached_play_page,
        }
        # 这两个键**按存在与否**输出，不按值。老存档缺它们时，
        # ``p1_missing_fields`` 会记下「P1 里没有」，而下游据此判不可判定。
        if "site_url" not in self.p1_missing_fields:
            out["site_url"] = self.site_url
        if "play_page_url" not in self.p1_missing_fields:
            out["play_page_url"] = self.play_page_url
        out["p1_missing_fields"] = list(self.p1_missing_fields)
        return out


@dataclass(frozen=True, slots=True)
class SiteOutcomeView:
    """轨迹内**一个访问**的结论（2026-10-30 · 方案 23）。

    为什么它回到轨迹里：裁定 ③「负样本池保留站点级行作**轨迹内细节**，
    训练侧只消费任务级标签」——站点级结论不再被切成独立的样本，
    但**不能丢**：它回答「这条轨迹里到底在哪一步走不通」。

    ``outcome is None`` = **没配到** outcome（缺件），**不是**成功——
    成功是 ``outcome.branch is None``。两件事在报表上差得很远。
    """

    #: 访问序号，从 1 起（与 P1 的 ``visits[i]`` 对齐）。
    index: int
    #: 候选链接（搜索结果给的地址）。
    url: str
    #: 落地后的地址（可能与候选不同）。
    landed_url: str
    #: 配到的结论（``None`` = 没配到）。
    outcome: OutcomeRef | None
    #: 这个访问点占**全局步号**的起点（含）。
    step_start: int = 0
    #: 这个访问点占全局步号的终点（**不含**）。区间为空 = 该站点没有动作步。
    #:
    #: 为什么必须带区间：站点级结论回到轨迹里之后，「这个站点是轨迹里
    #: 哪一段」就成了必须能回答的问题。靠 url 反推是**猜**——两个站点
    #: 跳到同一个地址时会撞，而撞了报告上看不出异常。
    step_end: int = 0
    #: 配到的 outcome 在 P1 ``outcomes[]`` 里的下标（``-1`` = 没配到）。
    #:
    #: **这是候选集站点行与本访问点之间的唯一对齐身份**：候选行按
    #: **url** 建键、访问点按**访问顺序**枚举，两边光靠 url 会撞
    #: （两个站点跳到同一个地址）。下标是两边**共有**的事实，不是推出来的。
    outcome_index: int = -1

    @property
    def matched(self) -> bool:
        return self.outcome is not None

    @property
    def is_success(self) -> bool:
        return self.outcome is not None and self.outcome.branch is None

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "url": self.url,
            "landed_url": self.landed_url,
            "outcome": self.outcome.to_json() if self.outcome else None,
            "step_start": self.step_start,
            "step_end": self.step_end,
            "outcome_index": self.outcome_index,
        }


@dataclass(frozen=True, slots=True)
class SiteView:
    """一个访问点在轨迹内的**站点级视图**——评分用（2026-10-30 · 方案 23）。

    为什么需要它
    ------------
    v1–v16 的**站点级** ``dimension_scores``（D-2/D-4/D-5/D-6）是**逐站点**
    打出来的：打分的输入是那个站点**自己的**动作步、**自己的**观察、
    **自己的**结论（那时一条 P2 样本 = 一个决策单元）。

    per-Run 之后一条存档只切出**一条**轨迹样本 ⇒ 直接拿它打分，得到的是
    **轨迹级**分数 ⇒ 站点行的分数从「这个站点的分」变成「整条轨迹的分」，
    而 **D-10 的分母与历史读数是绑定口径**（门禁只读调参池；分母薄不是
    改口径的理由）。

    ⇒ 站点行要打分，就得先把轨迹**切回站点那一段**：动作步与观察按
    :attr:`SiteOutcomeView.step_start` / ``step_end`` 切片，结论换成
    **该站点自己的**那条。

    ⚠️ **它不是一条 P2 样本**
    ----------------------
    它没有 ``system_prompt`` / ``tools`` / ``user_prompt`` / ``gate`` /
    ``rationales``——那些是**轨迹级**的东西，站点视图里给一半就是给假。
    所以它**不是** :class:`Sample`，也不落 ``samples/`` 目录：
    per-unit 样本的形态**不回来**（那正是方案 23 要拆掉的）。

    打分器读的是 ``actions`` / ``observations`` / ``outcome`` / ``title``
    四个面（见 ``evaluation/scorers/``）——这四个都在。
    """

    #: 视图 id = ``{task}__{run}__site-{访问序号}``。
    #:
    #: ⚠️ 与**旧 per-unit 样本 id 同形**（``{task}__{run}__site-1``）——
    #: 这是有意的：旧冻结档里那些 id 还能被认出来（"这是第几个访问点"），
    #: 而新口径下它明确**只是视图**，不落盘、不进 ``samples/``。
    sample_id: str
    task_id: str
    run_id: str
    #: 轨迹片名（评分器 D-6 要用）。
    title: str
    #: 访问序号（从 1 起）。
    index: int
    #: 候选链接。
    url: str
    #: 落地后的地址。
    landed_url: str
    #: 这个访问点占的全局步号区间（``[step_start, step_end)``）。
    step_start: int
    step_end: int
    #: 配到的 outcome 在 P1 ``outcomes[]`` 里的下标（``-1`` = 没配到）。
    outcome_index: int
    #: **这个站点自己的**动作步（切片自轨迹）。
    actions: tuple[ActionView, ...]
    #: **这个站点自己的**观察（切片自轨迹）。
    observations: tuple[Mapping[str, Any], ...]
    #: **这个站点自己的**结论（``None`` = 没配到，属缺件）。
    outcome: OutcomeRef | None

    def to_json(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "title": self.title,
            "index": self.index,
            "url": self.url,
            "landed_url": self.landed_url,
            "step_start": self.step_start,
            "step_end": self.step_end,
            "outcome_index": self.outcome_index,
            "actions": [a.to_json() for a in self.actions],
            "observations": [dict(o) for o in self.observations],
            "outcome": self.outcome.to_json() if self.outcome else None,
        }


@dataclass(frozen=True, slots=True)
class Sample:
    """P2 单条样本 = **一条轨迹**（一个任务的一次运行）。

    ⚠️ **粒度变了**（2026-10-30 · 方案 23）：2026-10-30 之前一条样本是
    一个**决策单元**（搜索阶段一条、每个站点一条），同一份 P1 存档切出
    N 条样本。现在**一份 P1 存档切出 0 或 1 条**——一个任务一条轨迹。
    """

    sample_id: str
    task_id: str
    title: str

    system_prompt: str
    tools: tuple[ToolSpec, ...]
    user_prompt: str
    provenance: Mapping[str, Any]
    actions: tuple[ActionView, ...]
    observations: tuple[Mapping[str, Any], ...]
    #: ⑥ 与 :attr:`actions` **按 ``step_index`` 对齐**（2026-10-10 改，
    #: 原为「整条一条」）。逐条对得上才有训练意义，也才够闸门逐条核。
    #:
    #: **三个字段带默认值**：评分器与打分测试构造 ``Sample`` 时只关心
    #: outcome 与观察，并不关心思考怎么写。默认值让「没接模块 4」
    #: 成为一个可以显式表达的状态（``rationales=()`` + ``rationale_missing=True``），
    #: 而不是让每个调用方都必须传两个与它无关的参数。
    rationales: tuple[Rationale, ...] = ()
    #: 默认 ``not_run`` 而非 ``passed``——**「没跑」不是「通过」**。
    #: 这个模块级的实例是 frozen 的，可安全作默认值。
    gate: GateMark = GateMark("not_run", ("闸门未运行",))

    #: **轨迹级结论**（2026-10-30 · 方案 23 裁定 ②：看最后一个结论）。
    #:
    #: ⚠️ 它是**整条轨迹**的结论，不是某一个站点的：``branch is None``
    #: = 成功（末条结论是成功），``branch`` 非空 = 末条结论的分支。
    #: 逐站点的结论在 :attr:`site_outcomes` 里。
    outcome: OutcomeRef | None = None

    #: **本轨迹的终局反思**（INC46）。
    #:
    #: ⚠️ **P1 里没有轨迹级反思**——``reflection`` 是 **unit 级**的
    #: （搜索阶段在存档顶层、站点单元在 ``visits[i]`` 里），形状同
    #: ``WriteResult.to_json``。
    #:
    #: 这里取**末个单元**的那一份（裁定 ②「看最后的结论」同款）：它说的
    #: 是模型对**最后核验的那个站点**的判断，而那正是这条轨迹的落点。
    #: 搜索阶段的反思（存档顶层）说的是「找到了哪些候选」，不是结论，
    #: 所以**排在末位之后**时不会被取到。
    #:
    #: 取不到（有反思的单元一个都没有）→ ``None``。「没有反思」与
    #: 「有反思但内容是空的」在下游是两回事，合成一个空 dict 就把
    #: 前者记成了后者。
    reflection: Mapping[str, Any] | None = None

    #: **一条 rationale 都没写成**（模块 4 未落地 / 后端不可用 / 全被拒写）。
    #: 与「闸门跑过」分开记：闸门过了但这里为 True，说明样本只有动作没有思考——
    #: 训练 CoT SFT 时会缺一整块，而报告上「gate=passed」看不出来。
    rationale_missing: bool = True

    #: 这份样本的 rationale 属于哪一代**口径**（INC46，见
    #: :func:`rationale_era_of`）：``"pre_action"``（动作前动机）/
    #: ``"post_hoc"``（事后反思）/ ``""``（没有可判的 rationale）。
    #:
    #: **为什么必须逐条落盘**：两代在正文与字段形状上同构，混在一批里训练
    #: 的后果是「有时可以未卜先知、有时不行」——那在报表上表现为「分数略降」
    #: 而不是「坏了一批」。批次筛选、闸门分代、评估分档都要读它，
    #: 而从 ``reflection`` 键在不在**现推**只在 P1 侧可能——P2 必须自己带。
    rationale_era: str = ""

    #: 本条样本据以生成的 P1（文件名，不含目录）。批次可回溯到源存档。
    source_archive: str = ""

    #: P1 里**没有**的键（老存档缺 ``steps`` / ``user_prompt`` / 全文正文…）。
    #: 非空即表示这条样本是**降级重建**的，别当成与新存档同等的样本混着用。
    degraded_from: tuple[str, ...] = ()

    #: 源 P1 的 :data:`~trajectory_pipeline.executor.orchestrator.SCHEMA_VERSION`。
    #:
    #: **空串 = 那份存档压根没有这个键**，不是「v1」也不是「未知版本 1」——
    #: 38 份历史存档都落在这。两种读法在报表上差很远：前者说「这份数据
    #: 没有版本身份」，后者说「这份数据是旧的」，而后者是**猜的**。
    #: 评估侧据它分档：认得版本就按版本分，认不得就退回缺件代理，
    #: 并把那份代理**标注为代理**（见 ``evaluation.cli``）。
    schema_version: str = ""

    #: **工具面**（2026-10-20 方案 15 §7 R1）——这份样本的训练动作空间
    #: 属于哪个方言（``obscura-1`` / ``camoufox-1``〔只读存量〕 / ``camofox-1``）。
    #:
    #: 它来自源 P1 的 ``driver`` 字段，经
    #: :func:`~trajectory_pipeline.toolcatalog.surface_of_driver` 折算。
    #:
    #: **空串 = 这条样本没有工具面身份**（2026-10-20 之前建的 P2 没有
    #: 这个键）。读档时按 :data:`~trajectory_pipeline.toolcatalog.DEFAULT_SURFACE`
    #: 渲染——那是那批样本**真实跑出来**的面，不是默认值兜底。
    #:
    #: 为什么必须显式落：``click`` 在两个面里参数不同，渲染时拿错面就会
    #: 训出「有时按标签点、有时按角色点」的模型，而那在报告上看不出异常。
    tool_surface: str = ""

    #: **轨迹内的站点级明细**（2026-10-30 · 方案 23 裁定 ③）。
    #:
    #: 站点级结论**不再被切成独立的样本**，但**不能丢**——它回答
    #: 「这条轨迹里到底在哪一步走不通」。训练侧只消费
    #: :attr:`outcome`（任务级标签），这一层是**给人看的**。
    site_outcomes: tuple[SiteOutcomeView, ...] = ()

    def site_views(self) -> tuple[SiteView, ...]:
        """这条轨迹的**站点级视图**（每个访问点一条）。

        动作步与观察按 :attr:`SiteOutcomeView.step_start` / ``step_end``
        **切片**，结论换成该访问点自己的那条 ⇒ 与 per-unit 时代的
        「站点样本」在**评分器读的那四个面**上逐字等价。

        ⚠️ **区间可能为空**：某次访问一步都没走（搜索页直接给了结论、
        或访问被拦在导航之前）⇒ 该站点**没有动作步**。这时视图照样给出来
        （观察为空、结论在），但**打分器会因「没有观察」而弃权**——
        这与「压根没访问过这个站点」不是一回事，不许混。

        ⚠️ **步号越界不静默**：``step_end`` 超出轨迹长度说明存档与切条
        对不上（降级重建、存档被改过）⇒ 那时**不切**，返回空区间，
        由调用方看到「这个站点切不出来」。**不猜**。
        """
        views: list[SiteView] = []
        n = len(self.actions)
        for s in self.site_outcomes:
            start = max(0, s.step_start)
            end = min(s.step_end, n)
            if s.step_start < 0 or s.step_end < s.step_start or start > end:
                # 区间与轨迹对不上 ⇒ 不切（空区间），不静默截断成「看着像
                # 对得上」的一段——那会让评分打在别人的步上。
                start = end = 0
            views.append(
                SiteView(
                    sample_id=f"{self.sample_id}__site-{s.index}",
                    task_id=self.task_id,
                    run_id=run_id_of(self.sample_id),
                    title=self.title,
                    index=s.index,
                    url=s.url,
                    landed_url=s.landed_url,
                    step_start=start,
                    step_end=end,
                    outcome_index=s.outcome_index,
                    actions=tuple(self.actions[start:end]),
                    observations=tuple(self.observations[start:end]),
                    outcome=s.outcome,
                )
            )
        return tuple(views)

    def site_view(self, index: int) -> SiteView | None:
        """按**访问序号**取站点视图（从 1 起）。取不到返回 ``None``。"""
        for v in self.site_views():
            if v.index == index:
                return v
        return None

    #: 每步动作的**失败原因**（P1 ``steps[i].error``），与 :attr:`actions`
    #: 按下标对齐。空串 = 这一步没报错。
    #:
    #: 它**不进** :meth:`to_json` 的六件套，而是渲染期作为
    #: ``action_error`` 挂到观察上（方案 23 R1-2）：训练侧要能区分
    #: 「这块信息没采到」（``degraded``）与「这个动作白做」（本条）。
    #: 两者混成一个标记，模型会把「点了没反应」学成「页面没内容」。
    step_errors: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "task_id": self.task_id,
            "title": self.title,
            "source_archive": self.source_archive,
            "schema_version": self.schema_version,
            "tool_surface": self.tool_surface,
            "six": {
                "system_prompt": self.system_prompt,
                "tools": [t.to_json() for t in self.tools],
                "user_prompt": self.user_prompt,
                "provenance": dict(self.provenance),
                "actions": [a.to_json() for a in self.actions],
                "observations": [dict(o) for o in self.observations],
                "rationales": [r.to_json() for r in self.rationales],
            },
            "gate": self.gate.to_json(),
            "rationale_missing": self.rationale_missing,
            "rationale_era": self.rationale_era,
            "reflection": dict(self.reflection) if self.reflection else None,
            "outcome": self.outcome.to_json() if self.outcome else None,
            "site_outcomes": [s.to_json() for s in self.site_outcomes],
            "degraded_from": list(self.degraded_from),
        }


# ── sample_id ──────────────────────────────────────────────────────────


def sample_id(task_id: str, run_id: str) -> str:
    """``<task_id>__<run_id>``。

    ⚠️ **2026-10-30 · 方案 23**：原来是 ``<task_id>__<run_id>__<unit>``
    （一个决策单元一条样本）。现在**一个任务一条轨迹**，粒度键就是
    ``(task_id, run_id)`` ⇒ id 与 P1 存档名**逐字相同**。

    为什么这样最省事：id 与存档名同构 ⇒ 从 id 就能反查源存档、不必开文件；
    批次筛选、产物目录、``purge-run`` 的 glob 全都沿用同一把尺子；
    而 T 视图的 ``trajectory_id`` 本来就是这个形式——两边**终于**对齐了。
    """
    return f"{task_id}__{run_id}"


def run_id_of(archive_name: str) -> str:
    """从存档文件名取 run_id。``T001__004b6be9.json`` → ``004b6be9``。

    探针取证文件（``obscura_tools.json`` 等）没有 ``__``，那是正常形态——
    它们不是存档，走不到这里。

    ⚠️ ``.reviewed`` **不算 run_id 的一部分**：``T001__x1.reviewed.json``
    与 ``T001__x1.json`` 是**同一次 run 的两份档**（复核档是原档 + 人工裁定），
    映射到同一个 ``sample_id`` 才对——P2 从哪一份切出来由
    ``source_archive`` 记录下来，而不是靠 id 分叉。不剥这个后缀会切出
    ``T001__x1.reviewed__site-1``：同一批数据在样本目录里**并存两份**，
    而报表上看不出哪份是复核后的（2026-10-25 让 ``build`` 改读复核档时踩到）。
    """
    stem = Path(archive_name).name
    if stem.endswith(".json"):
        stem = stem[:-5]
    if stem.endswith(".reviewed"):
        stem = stem[: -len(".reviewed")]
    return stem.split("__", 1)[1] if "__" in stem else stem


# ── P1 读取器（按文件格式，不 import executor）─────────────────────────


class P1ShapeError(ValueError):
    """存档缺到无法切成样本（没有 task_id / 没有 observations）。

    注意这与「存档里某些键缺失」是**两回事**：后者走
    :attr:`Sample.degraded_from`，前者没有样本可产出，只能抛。
    """


#: 老存档用 ``body_preview``（前 400 字符），新存档用 ``body_text`` 全文。
#: 两者**不能当成同一份正文**——那是不同长度的证据。
BODY_KEY = "body_text"
LEGACY_BODY_KEY = "body_preview"

#: 步观察缺失占位（P1 ``steps[i].observation is None`` 时顶位）。
#:
#: 为什么必须有它（2026-10-15 实测 ``E005__e35b7455__site-4``）：
#: 执行器的会话隔离动作 ``new_tab`` 那一步 observation=null，而站点样本
#: 的观察此前取 ``(site_obs, player_obs)`` 加 ``if o`` 真值过滤——null 被
#: **静默丢掉**，于是 3 个动作配 2 份观察：``new_tab`` 步的 rationale
#: （「URL、标题和正文均为空」）配到了 player 页观察（满屏正文），渲染出的
#: 训练消息自相矛盾；末步 ``click`` 则成了没有回执的悬空调用。
#:
#: **占位必须真值为真**：``None`` / ``{}`` 会在下游任何 ``if o`` 真值
#: 过滤里被再次丢掉，错位原样复发。``degraded`` 标记让评分器的
#: observation-shortfall 系判据把它当「没采到」而不是「页面真空」；
#: 消息视图渲染成「（本步没有取到页面内容）」（见 observation_view /
#: views 两侧的 missing 路径，本就存在）。
MISSING_OBSERVATION: dict[str, Any] = {"degraded": ["observation_missing"]}


def read_archive(path: str | Path) -> dict[str, Any]:
    """读一份 P1 存档的 JSON。**只做文件格式层面的解析。**

    不做任何语义校验——那属于 executor 的领域，而 executor 一旦参与进来
    就会把「重建 P2 独立于执行端」这条纪律破掉。
    """
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise P1ShapeError(f"{p.name}: 不是合法 JSON（{exc}）") from exc
    if not isinstance(data, dict):
        raise P1ShapeError(f"{p.name}: 顶层不是 JSON 对象")
    if not data.get("task_id"):
        raise P1ShapeError(f"{p.name}: 缺 task_id，无法切成样本")
    return data


def _search_collection_failed(archive: Mapping[str, Any]) -> bool:
    """搜索页采集是否因异常失败（网络错误等），而非正常搜到再被拦。

    与 :data:`search_blocked` 一样是「采集/决策**尚未发生**」的形态：
    executor 在搜索页 ``goto``/``observe`` 抛异常时提前 return，只留一条
    ``搜索页采集失败: <ExcType>: <exc>`` warning，没有任何搜索观察或访问
    记录。此时切不出样本**不是错误**——被 :func:`iter_samples` 遍历时
    抛出会让整批陪葬（同 :data:`search_blocked` 的豁免逻辑）。
    """
    return any(
        str(w).startswith("搜索页采集失败") for w in archive.get("warnings") or ()
    )


def _obs_missing(obs: Mapping[str, Any]) -> str | None:
    """这条观察缺了什么，返回可写进 ``degraded_from`` 的标记；不缺返回 ``None``。

    两种缺失要分开，因为它们对下游的影响不同：

    - 完全没有正文 → 观察不可用，rationale 无从核查；
    - 只有 ``body_preview``（400 字符）→ **正文被截短了**。这件事必须显式，
      否则 rationale 的实体核查会把落在预览之外的实体判成幻觉——那不是
      幻觉，是**没存**。悄悄拿 400 字符当全文喂进去，等于凭空造出一批
      假幻觉，而闸门会把它们当成真的模型错误记进负样本池。

    ⚠️ **本函数答的是「P1 里没有哪个键」，不是「这份观察哪里不可信」。**
    所以「有键但内容是烂的」（``body_mojibake`` / ``title_mojibake``）
    **不在这里判**——它在**采集期**就记进了 ``Observation.degraded``，
    随 P1 落盘，由闸门侧 ``gates.observation_shortfall`` 消费
    （见到 ``degraded`` 非空即拒绝判幻觉）。理由与
    ``executor.integrity.degraded_marker_map`` 刻意不收 ``mojibake``
    是同一条：``degraded_from`` 是平铺的标记列表，
    「正文被截短」与「正文是烂的」轻重不同，混进同一张表就分不出。
    """
    if not obs:
        return None
    body = obs.get(BODY_KEY)
    if isinstance(body, str) and body:
        return None
    legacy = obs.get(LEGACY_BODY_KEY)
    if isinstance(legacy, str) and legacy:
        return "body_preview_only"
    return "no_body"


def _actions_of(steps: Sequence[Mapping[str, Any]]) -> tuple[ActionView, ...]:
    out: list[ActionView] = []
    for st in steps or ():
        a = st.get("action") or {}
        out.append(
            ActionView(
                tool=str(a.get("tool", "")),
                params=dict(a.get("params") or {}),
                target=dict(a["target"]) if a.get("target") else None,
                origin=str(a.get("origin") or "model"),
            )
        )
    return tuple(out)


def _outcome_ref(raw: Mapping[str, Any] | None) -> OutcomeRef | None:
    """一条原始 outcome → :class:`OutcomeRef`。``None`` 进 ``None`` 出。

    「键不存在」逐条记，不靠值推断（避坑指南 §2.3）：``site_url`` /
    ``play_page_url`` 缺了就记进 :attr:`OutcomeRef.p1_missing_fields`，
    下游据此判**不可判定**。
    """
    if raw is None:
        return None
    missing = tuple(k for k in ("site_url", "play_page_url") if k not in raw)
    return OutcomeRef(
        url=str(raw.get("url")),
        branch=raw.get("branch"),
        source=str(raw.get("source") or ""),
        fallback_used=bool(raw.get("fallback_used")),
        question=str(raw.get("question") or ""),
        reached_play_page=bool(raw.get("reached_play_page")),
        site_url=str(raw.get("site_url") or ""),
        play_page_url=str(raw.get("play_page_url") or ""),
        p1_missing_fields=missing,
    )


def _outcome_for(
    outcomes: Sequence[Mapping[str, Any]], *urls: str
) -> OutcomeRef | None:
    """按 url 找那条 outcome。**口径在** :mod:`trajectory_pipeline.outcomerules`。

    ⚠️ **2026-10-30 · 方案 23**：配对规则**收敛到单一真源**
    （候选 → 落地 → 播放页，逐档匹 ``site_url`` **或** ``url``，
    **同档取末条**——末条才是这个站点的最终结论）。
    本函数**不再自己写匹配规则**，只做 dict → :class:`OutcomeRef` 的转换。

    旧口径（只匹 ``url``、取首个）在真实存档上把**同一个站点的中间结论**
    当成了最终结论：实测 29 个访问上与新口径分支不同。

    刻意**不看**存档里的 ``is_negative_sample`` 字段——那份判据由
    :data:`~trajectory_pipeline.executor.branches.NON_SAMPLE_BRANCHES` 定义，
    而 assembler 不 import executor，就不能保证两份口径同步。
    """
    return _outcome_ref(outcomerules.resolve(outcomes, *urls))


# ── 决策单元（P1 的结构，不是 P2 的粒度）────────────────────────


@dataclass(frozen=True, slots=True)
class _Unit:
    """P1 的一个**决策单元**：搜索阶段，或一个访问过的站点。

    单元是 **P1 的结构**——P2 现在整条轨迹一条样本，但读 P1 仍要按单元读
    （每个单元有自己的 ``steps`` 与自己的 ``reflection``）。
    """

    #: 单元名（``"search"`` / ``"site-N"``）。**不出现在 P2 里**——
    #: 单元不再是样本的粒度，只是 P1 的分段。
    name: str
    #: 该单元的动作步（每步带自己的 ``observation`` 与 ``rationale``）。
    steps: tuple[Mapping[str, Any], ...]
    #: 单元本体（判口径用：看它有没有 ``reflection`` 键）。
    body: Mapping[str, Any]


def _units_of(archive: Mapping[str, Any]) -> tuple[_Unit, ...]:
    """把一份 P1 拆成决策单元序列：搜索阶段 + 每个访问过的站点。"""
    units: list[_Unit] = []

    search_steps = tuple(archive.get("steps") or ())
    search_obs = archive.get("search_observation")
    if search_steps and isinstance(search_obs, Mapping) and len(search_steps) == 1:
        # 搜索阶段只有一步，而存档顶层另有一份「搜索页快照」。两份内容
        # 实测只差 ``links``（39/138 份），但那份快照是 P2 现行契约用的
        # 那份——沿用它，别在改粒度的同时换观察来源。
        #
        # ⚠️ 「只有一步」是**实测**结论（145 份真实存档：138 份顶层恰好
        # 1 步、7 份 0 步），不是结构保证。所以这段有长度判断：万一哪天
        # 出现多步的搜索阶段，就退回逐 step 的观察（宁可少一份快照，
        # 也不能让动作与观察对不上）。
        step = dict(search_steps[0])
        step["observation"] = dict(search_obs)
        search_steps = (step,)
    units.append(_Unit("search", search_steps, archive))

    for i, visit in enumerate(archive.get("visits") or (), 1):
        units.append(
            _Unit(f"site-{i}", tuple(visit.get("steps") or ()), visit)
        )
    return tuple(units)


def _obs_of(step: Mapping[str, Any]) -> Mapping[str, Any]:
    """一步的观察。缺（或为 null）→ :data:`MISSING_OBSERVATION` 占位。

    **占位必须真值为真**（见 :data:`MISSING_OBSERVATION` 的注）：
    ``None`` / ``{}`` 会在下游任何 ``if o`` 真值过滤里被再次丢掉，
    错位原样复发。
    """
    o = step.get("observation")
    return dict(o) if o else dict(MISSING_OBSERVATION)


def _last_reflection_of(units: Sequence[_Unit]) -> Mapping[str, Any] | None:
    """轨迹级反思：**末个单元**写成的那份（真源在 ``closing``）。

    P1 里没有轨迹级反思——``reflection`` 是单元级的。取法（末个 +
    ``status == "written"``）与 T 的收尾结论**共用同一份判断**，见
    :func:`~trajectory_pipeline.assembler.closing.last_written_reflection`。
    """
    return closing.last_written_reflection([u.body for u in units])


def _apply_output_contract(
    gate: GateMark,
    *,
    task_id: str,
    title: str,
    outcome: OutcomeRef | None,
    site_outcomes: Sequence[SiteOutcomeView],
    reflection: Mapping[str, Any] | None,
    has_visible_action: bool,
) -> GateMark:
    """终局验收读**最终回复**（方案 23 R2-3，owner 裁定 O-2）。

    判据只有一条：任务库 ``output_contract.min_urls ≥ 1``，而这条样本的
    收尾答复里**没有 URL** ⇒ **不许判 ``passed``**。

    - 只作用于**有任务级结论**的样本（``outcome is not None``）：搜索阶段
      就被拦 / 一个候选都没访问的 run 没有「答复」可读；
    - 只降 ``passed``：``failed`` 已经是失败，``not_run`` 是「没判」——
      两者都不许被这条判据改写（``not_run`` 不是通过，见 :func:`gate_of`）；
    - 读的文本是 :func:`~trajectory_pipeline.assembler.closing.closing_for_run`
      的产出，**与 P3 渲染的末条 assistant 是同一句话**——分开写的那天，
      验收读的就不是模型看到的那句了（事实的算法在
      :func:`~trajectory_pipeline.assembler.closing.run_closing_outcomes`，
      三侧共用）；
    - 任务库读不到 / 未知 ``task_id`` / 合同没写 ``min_urls`` ⇒
      ``None`` ⇒ **不改判定**（fail-closed：判据不适用 ≠ 判通过）。同理，
      没有可见模型动作就没有收尾文本，也就没有可验收的「最终回复」。
    """
    if outcome is None or not has_visible_action:
        return gate
    if gate.status != "passed":
        return gate
    min_urls = contracts.min_urls_of(task_id)
    if min_urls is None:
        return gate
    text = _run_closing_text(
        title=title,
        outcome=outcome,
        site_outcomes=site_outcomes,
        reflection=reflection,
    )
    found = closing.count_urls(text)
    if found >= min_urls:
        return gate
    return GateMark(
        "failed",
        gate.reasons
        + (
            f"终局答复未兑现输出契约：合同要求至少 {min_urls} 个网址，"
            f"答复里只有 {found} 个",
        ),
    )


def _run_closing_text(
    *,
    title: str,
    outcome: OutcomeRef | None,
    site_outcomes: Sequence[SiteOutcomeView],
    reflection: Mapping[str, Any] | None,
) -> str:
    """这条轨迹的收尾答复——**P2 与 P3 读的是同一句**。

    P3 侧的同一个函数在 :mod:`assembler.views`（``_closing_text``），
    那边读的是 P2 落盘后的 JSON，这边读的是内存里的对象。事实一样 ⇒
    文本一样 ⇒ P2 的终局验收判的**就是**模型最后看到的那句。

    ⚠️ 事实的算法在 :func:`assembler.closing.run_closing_outcomes`——
    **不在这里重写一遍**。
    """
    decisive = outcome.to_json() if outcome is not None else None
    sites = [
        s.outcome.to_json() if s.outcome is not None else None
        for s in site_outcomes
    ]
    return closing.closing_for_run(
        title=title,
        outcomes=closing.run_closing_outcomes(
            decisive=decisive,
            site_outcomes=sites,
        ),
        conclusion=closing.reflection_conclusion(reflection),
    )


def _run_sample(
    *,
    archive: Mapping[str, Any],
    source_name: str,
    units: Sequence[_Unit],
    degraded: list[str],
    outcome: OutcomeRef | None,
    site_outcomes: tuple[SiteOutcomeView, ...],
    reflection: Mapping[str, Any] | None,
) -> Sample:
    """把一条轨迹（一个 Run）组装成一条 P2 样本。"""
    task_id = str(archive.get("task_id"))
    # 工具面 = 源存档的方言（2026-10-20 方案 15 §7 R1）。认不出的驱动名
    # 在这里**抛**：渲染期才发现会晚一步，而那一步已经把错声明写进样本了。
    surface = toolcatalog.surface_of_driver(archive.get("driver"))

    # 全部单元的步**摊平成一条流**：搜索阶段 → 站点 1 → …（时间序）。
    # 摊平之后 ``step_index`` 就是**全局**序号——闸门、渲染、复核三处
    # 都按它对齐，而不再是「每个单元各自从 0 数起」（那会让第 5 步的
    # rationale 在字典查找里撞上第 1 个单元的第 5 步，**静默丢思考**）。
    steps: list[Mapping[str, Any]] = []
    observations: list[Mapping[str, Any]] = []
    for unit in units:
        steps.extend(unit.steps)
        observations.extend(_obs_of(s) for s in unit.steps)
    actions_ = _actions_of(steps)

    rationales = align_rationales(actions_, steps)
    written = [r for r in rationales if r.status == "written"]
    # 口径按**整条轨迹**判：看存档顶层有没有 ``reflection`` 键。
    # 实测（2026-10-30 · 145 份真实存档）顶层与各 visit 的 ``reflection``
    # 键**总是同时在或同时不在**，所以轨迹级口径不会混代。
    era = rationale_era_of(archive, rationales)
    gate = gate_of(
        rationales,
        action_tools=[a.tool for a in actions_],
        observations=observations,
        # 片名 + 用户原话是**任务给的**，不是模型从页面上看的。
        # 拿它们当「模型引入的实体」去核，会把每一条提到片名的
        # rationale 都判成幻觉（rationale 的豁免域，见 gates 口径二）。
        exempt=f"{archive.get('title') or ''} {archive.get('user_prompt') or ''}".strip(),
        # 旧口径不按新窗口核（见 :func:`gate_of` 的跨口径注）。
        rationale_era=era,
    )
    # 终局验收读最终回复（方案 23 R2-3）：闸门只判过 outcome，不读文本，
    # 于是「回复必须含可播放网址」这条合同从未被执行（审计 G4：44/49 违约
    # 而 gate 仍 passed）。这里补的是**读回复**，不是第二条 rationale 闸。
    gate = _apply_output_contract(
        gate,
        task_id=task_id,
        title=str(archive.get("title") or ""),
        outcome=outcome,
        site_outcomes=site_outcomes,
        reflection=reflection,
        has_visible_action=any(a.origin != "infrastructure" for a in actions_),
    )
    return Sample(
        sample_id=sample_id(task_id, run_id_of(source_name)),
        task_id=task_id,
        title=str(archive.get("title") or ""),
        system_prompt=SYSTEM_PROMPT,
        tools=TOOL_SPECS_BY_SURFACE[surface],
        user_prompt=str(archive.get("user_prompt") or ""),
        provenance=dict(archive.get("provenance") or {}),
        actions=actions_,
        observations=tuple(observations),
        rationales=rationales,
        gate=gate,
        outcome=outcome,
        reflection=reflection,
        rationale_missing=not written,
        rationale_era=era,
        source_archive=source_name,
        degraded_from=tuple(dict.fromkeys(degraded)),
        # 键不存在 → 空串（**不是** ``"1"``）：没有版本身份就是没有版本身份，
        # 补一个默认值等于凭空断言这份存档属于哪一批。
        schema_version=str(archive.get("schema_version") or ""),
        # 工具面：源存档记了驱动就按那个方言走，没记就按默认面
        # （历史批次都是 obscura 面）。**面认不出 → 上面已经抛了**。
        tool_surface=surface,
        site_outcomes=site_outcomes,
        # 逐Step的动作失败原因（R1-2）：与 actions 对齐，渲染期挂到观察上。
        # ⚠️ 对齐的是**摊平后的步流**（``steps``）——``_actions_of`` 一步一条
        # 动作、不跳步，所以这里的下标与 ``actions`` 逐条对得上。
        step_errors=tuple(str(s.get("error") or "") for s in steps),
    )


@dataclass(frozen=True, slots=True)
class SplitResult:
    """一份 P1 的切条结果 + 被访问级过滤器剔掉的 unit。

    ``excluded`` 是 ``(unit, platform)`` 对：**整条 unit 被丢弃**，因为
    outcome / reflection 指向被排除平台而训练步里没有它，会让因果链在
    样本中间断掉（见 :mod:`assembler.excluded`）。
    """

    samples: tuple[Sample, ...] = ()
    excluded: tuple[tuple[str, str], ...] = ()


def split_archive(path: str | Path) -> list[Sample]:
    """把一份 P1 存档切成**一条**样本（一个 Run = 一条轨迹）。

    产出条数 = **0 或 1**：

    - **0**：搜索阶段被反爬拦（``search_blocked`` 非空）或搜索页采集失败
      （网络错误）——那一步压根没进入决策：前者那一步的观察是验证码页，
      切成「模型该怎么做」的样本只会教它对着验证码页编答案；后者连搜索
      观察都没有。这与 executor 把反爬单开字段、不进分支体系是同一条
      纪律的延续。
    - **1**：其余情况。

    ⚠️ **2026-10-30 · 方案 23**：这条函数以前切出「1（搜索阶段）+ N（每个
    站点）」条——一个**决策单元**一条样本。现在**一个任务一条轨迹**：
    决策单元仍在 P1 里（见 :func:`_units_of`），但它不再是样本的粒度，
    只作为轨迹内的分段与 :attr:`Sample.site_outcomes` 的来源。

    ⚠️ **动作流是摊平的**（搜索阶段 → 站点 1 → …）：搜索阶段的每一步
    与每个站点的每一步接成**一条**流，``step_index`` 是全局序号。
    以前每个单元各自从 0 数起，于是第 5 步的 rationale 在字典查找里
    会撞上第 1 个单元的第 5 步——**静默丢思考**。

    ⚠️ **2026-11-03 · re-base 到 per-Run**：**访问级剔除**（方案 23 A10 /
    R4-2(b)）在这条路径上**整条 unit 地生效**——访问了任务声明的被排除
    平台的站点，它的**步与它的结论都不进这条轨迹**。于是「看最后一个
    结论」的任务级标签是在**剔后**的结论集合上取的（这是 R4-2 的必然
    结果，不是 bug）。要看得见剔了哪些，用 :func:`split_archive_report`。
    """
    return list(split_archive_report(path).samples)


def split_archive_report(path: str | Path) -> SplitResult:
    """切条 + 访问级剔除，并返回剔除明细。

    与 :func:`split_archive` 同一条切条路径；差别只在「剔掉的 unit 是否
    要报出来」。P2 落盘（builder）用本函数记账，其它读样本的调用方仍可
    用 :func:`split_archive`。

    ⚠️ **per-Run 口径**（2026-11-03 re-base）：剔的是**整条 unit**——
    被排除平台的站点，它的步与它的 outcome **都不进这条轨迹**。于是：

    - 步区间按**剔后**的步流重编（``site_outcomes[i].step_start`` 指向
      剔完之后的全局序号）；
    - 任务级结论取**剔后**的末条结论（:func:`outcomerules.task_conclusion`
      的 ``exclude`` 参数）；
    - T 侧读同一份剔除账（:func:`assembler.excluded.excluded_unit_map`），
      所以 P2 与 T 的结论**同源**。
    """
    p = Path(path)
    archive = read_archive(p)
    source_name = p.name
    task_id = str(archive.get("task_id"))

    # ── 采集侧退场：没进入决策的 run 产出零条 ──────────────────────
    if archive.get("search_blocked") or _search_collection_failed(archive):
        # 搜索阶段没进决策的 run 产出零条**是正常形态**，不是错误：
        # 要么被反爬拦（观察是验证码页），要么搜索页采集失败（网络错误，
        # T048 这类坏档连搜索观察都没有）。抛出去会让 :func:`iter_samples`
        # 在遍历目录时整批中断——而一批里混进几个这样的 run 是常态
        # （反爬见 executor 里「直接重跑同一引擎大概率仍是验证码页」那条
        # 警告，网络错误见真实存档 ``T048__5aa34cf7.json``）。
        return SplitResult()

    units = _units_of(archive)
    steps = archive.get("steps") or []
    visits = archive.get("visits") or ()

    if not archive.get("search_observation") and not visits:
        raise P1ShapeError(
            f"{source_name}: task_id={task_id} 的存档里既没有搜索观察也没有访问记录，"
            f"切不出轨迹（warnings={archive.get('warnings')!r}）"
        )

    # ── 降级标记 ──────────────────────────────────────────────────
    # 与 ``executor.integrity.check_archive`` 的缺件集合**逐条相等**
    # （``tests/unit/test_integrity.py::TestDegradedMarkersMatchSplit``
    # 双向盯着，谁先改谁就红）。
    #
    # 轨迹级只有一条样本，所以这份集合就是**整条轨迹**的缺件：以前是
    # 「那个站点降级」，现在是「这条轨迹降级」——降级会传染到整条，
    # 而报告上「一个站点正文截短」读成「整条轨迹降级」正是要避免的
    # 那种含糊，所以这里保留**站点级**的明细在 :attr:`Sample.site_outcomes`
    # 的 outcome 里，而 ``degraded_from`` 只回答「这条能不能当成完整样本用」。
    degraded: list[str] = []
    if not archive.get("user_prompt"):
        degraded.append("user_prompt")
    if not archive.get("run_config"):
        # 与 ``executor.integrity`` 的 ``run_config_missing`` 同码。
        # 它不进 P2 的训练视图（那六件套里没有运行参数），
        # 但样本仍要知道「这份数据是降级重建的」，
        # 否则老批次与新批次会被当成同等样本混着用。
        degraded.append("run_config")
    if not steps:
        degraded.append("steps")
    miss = _obs_missing(archive.get("search_observation"))
    if miss:
        degraded.append(miss)

    # ── 访问级剔除（R4-2）：整条 unit 剔，不是只剔命中那一步 ──────
    # 判定读模型动作的 goto 网址与观察 URL；new_tab 这类 infrastructure
    # 动作不算（见 :func:`assembler.excluded.unit_excluded_platform`）。
    #
    # 判定**只有一份**（:func:`assembler.excluded.excluded_unit_map`）：
    # P2 与 T 两侧看同一份证据，否则同一条访问点一个剔一个留，而报告上
    # 只会显示「少了一条」。
    blocked = excluded.excluded_unit_map(archive)
    dropped = tuple((name, blocked[name]) for name in sorted(blocked))
    kept_units = tuple(u for u in units if u.name not in blocked)

    # ── 站点级明细（裁定 ③：留作轨迹内细节，不切成独立样本）─────────
    outcomes = archive.get("outcomes") or []
    site_outcomes: list[SiteOutcomeView] = []
    # 被剔站点对应的 outcome 在 ``outcomes[]`` 里的下标——任务级结论要
    # 在**剔后**的集合上取（见本函数 docstring）。
    excluded_outcome_indices: set[int] = set()
    # 全局步号：搜索阶段的步在前，各站点的步按访问顺序接上。
    # 站点级明细带着**自己的步区间**——「这个站点是轨迹里哪一段」必须
    # 能直接回答，靠 url 反推是猜（两个站点跳到同一地址时会撞）。
    #
    # ⚠️ 步号在**剔后**重编：被剔的 unit 不进这条轨迹，它的步也不占号。
    cursor = len(kept_units[0].steps) if kept_units and kept_units[0].name == "search" else 0
    for i, visit in enumerate(visits, 1):
        unit = units[i] if i < len(units) else None
        n_steps = len(unit.steps) if unit is not None else 0
        player_obs = visit.get("player_obs")
        player_url = (str(player_obs.get("url") or "")
                      if isinstance(player_obs, Mapping) else "")
        hit = outcomerules.resolve_at(
            outcomes,
            str(visit.get("url") or ""),
            str(visit.get("landed_url") or ""),
            player_url,
        )
        if f"site-{i}" in blocked:
            # 整条 unit 剔：不进轨迹，也不进站点明细。它的 outcome 也不
            # 参与任务级结论——否则「被排除平台上的结论」会替这条轨迹
            # 定标签，而那个平台的步根本不在轨迹里。
            if hit is not None:
                excluded_outcome_indices.add(hit[0])
            continue
        step_start, step_end = cursor, cursor + n_steps
        cursor = step_end
        if not (visit.get("steps") or ()):
            degraded.append("steps")
        for key in ("site_obs", "player_obs"):
            m = _obs_missing(visit.get(key))
            if m:
                degraded.append(m)
        raw = hit[1] if hit is not None else None
        ref = _outcome_ref(raw)
        if ref is None:
            # 站点查不到 outcome = 这个访问点没有分支结论。它不能只是
            # ``None``：那与「这条轨迹压根没有结论」同形。
            degraded.append("outcome_missing")
        else:
            succeeded = bool(visit.get("success"))
            if succeeded != (ref.branch is None):
                # 「成功」只有一个来源说了算。两个来源对不上时标记出来，
                # 而不是挑一个——挑一个就是在猜，而这条样本进不进训练集
                # 全看它。
                degraded.append("success_branch_mismatch")
            ref = replace(ref, succeeded=succeeded)
        site_outcomes.append(
            SiteOutcomeView(
                index=i,
                url=str(visit.get("url") or ""),
                landed_url=str(visit.get("landed_url") or ""),
                outcome=ref,
                step_start=step_start,
                step_end=step_end,
                outcome_index=hit[0] if hit is not None else -1,
            )
        )

    # ── 任务级结论（裁定 ②：看最后一个结论），**在剔后取** ─────────
    conclusion = outcomerules.task_conclusion(
        archive, exclude=excluded_outcome_indices
    )
    task_outcome = _outcome_ref(conclusion.outcome)
    if task_outcome is not None:
        # 轨迹级的 ``succeeded`` 只有一个来源说了算——分支为空即成功。
        # 站点级的**第二个**来源（``visits[i].success``）在上面的循环里
        # 比对过了，对不上时记 ``success_branch_mismatch``，不在这里
        # 替它挑一个。
        task_outcome = replace(task_outcome, succeeded=task_outcome.branch is None)

    return SplitResult(
        samples=(
            _run_sample(
                archive=archive,
                source_name=source_name,
                units=kept_units,
                degraded=degraded,
                outcome=task_outcome,
                site_outcomes=tuple(site_outcomes),
                reflection=_last_reflection_of(kept_units),
            ),
        ),
        excluded=dropped,
    )


def iter_samples(root: str | Path, pattern: str = "*.json") -> Iterable[Sample]:
    """遍历一个目录下的全部存档并切条。

    - **探针取证文件自动跳过**。判据用 ``__`` 而不是文件名白名单：白名单
      漏一个就把取证当存档读进来，而 ``obscura_tools.json`` 顶层没有
      ``task_id``，会直接抛 :class:`P1ShapeError` 把整批打断。
    - **复核档跳过**：由 executor 侧 :func:`~trajectory_pipeline.executor.archive.select_archives`
      统一挑选。这里再选一次，同一条就会跑两遍，而两遍的复核结论可能不同。
    - **被反爬拦 / 搜索页采集失败的 run 产出零条**，不算错误（见
      :func:`split_archive`）。
    - **存档本身损坏则抛出，不吞**。坏档静默变成「零样本」的话，
      批次看起来只是少了几条，没人会去查。异常消息里带文件名。
    """
    for path in sorted(Path(root).glob(pattern)):
        if path.name.endswith(".reviewed.json"):
            continue  # 复核档由 executor 侧挑选，不在这里重选
        if "__" not in path.stem:
            continue  # 探针取证，不是存档
        yield from split_archive(path)


__all__ = [
    "SYSTEM_PROMPT",
    "TOOL_SPECS",
    "ToolSpec",
    "ActionView",
    "Rationale",
    "GateStatus",
    "GateMark",
    "OutcomeRef",
    "Sample",
    "SiteOutcomeView",
    "SiteView",
    "SplitResult",
    "P1ShapeError",
    "MISSING_OBSERVATION",
    "RATIONALE_ERA_PRE_ACTION", "RATIONALE_ERA_POST_HOC",
    "rationale_era_of",
    "sample_id",
    "run_id_of",
    "read_archive",
    "split_archive",
    "iter_samples",
]
