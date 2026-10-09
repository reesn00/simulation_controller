"""P2 单条样本的形状——**六件套**的值对象与 P1 读取器。

六件套（设计方案 §3.5）::

    ① 系统提示词      SYSTEM_PROMPT
    ② 工具参数列表    tools: tuple[ToolSpec, ...]
    ③ 任务提示词      user_prompt + provenance
    ④ 动作+参数       actions: tuple[Action, ...]
    ⑤ 观察            observations: 训练态视图（见 observation_view）
    ⑥ 思考+结论       rationale: Rationale | None

四条决定

--------
1. **读取 P1 按文件格式，不 import ``executor``。**
   与 :mod:`trajectory_pipeline.executor.plan` 消费模块 1 的计划同一条纪律：
   **交接面是格式，不是 import**。理由是 P1 贵、P2 便宜——重建一批 P2 必须
   独立于 executor 的**当前版本**。一旦 ``import executor``，executor 改一个
   dataclass 字段就可能让半年前那批 P2 重建不出来，而重建不出来的那批就是
   永久损失。契约测试 ``tests/contract/test_assembler.py`` 盯着这条。

2. **一条样本 = 一个决策单元**（``search`` 阶段一条 / 每个站点一条）。
   设计方案写「单站点验证、单搜索任务各自独立成条」，早期措辞里还有
   「前几步走摘要」，两者互相矛盾；按决策单元切则唯一确定：
   搜索与遍历本来就是两种不同的选择，合成一条长轨迹会把「选哪个站」和
   「这个站行不行」压进同一次预测里。

3. **缺件必须看得见，不能是 ``None``。**
   W1 没有 rationale 模块（模块 4 待建），六件套第 ⑥ 件整个拿不到。
   若只写 ``rationale=None``，样本与「模型这一轮没说话」长得一样——
   而前者是**流水线没长出来**，后者是**数据事实**。所以另有
   :attr:`Sample.rationale_missing` 与 :class:`GateMark`：闸门与理由分列，
   读的人一眼能分出「没跑」与「没过」。

4. **工具清单在这里冻结，不从 P1 反推。**
   P1 的 ``steps[]`` 只记**这一条样本实际用过**的动作，而「训练动作空间」
   必须是整个任务空间——从存档反推会让每条样本各带各的动作空间，
   模型看到的世界随样本变。反过来，从 ``executor.actions.TOOLS`` import
   又把 assembler 焊死在执行端（违背纪律 1）。
   取折中：**本模块自己声明**，由契约测试拿真实存档交叉验证没有漏项
   （漂移是测试的事，不是 import 的事）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

#: ① 系统提示词。**随样本一起冻结**（进 P2 文件），不是渲染时读全局常量——
#: 常量改一次，历史样本的系统提示词就全变了，而样本之间原本是可比的。
#:
#: 内容口径：只描述**这一条任务里模型的角色与硬约束**，不掺任何执行端实现
#: （不提 obscura、不提 MCP、不提 ref 句柄）。模型学到的是「遇到这样的用户
#: 与页面该怎么办」，不是「怎么驱动某个浏览器」。
SYSTEM_PROMPT = (
    "你是一个会帮用户找在线观看资源的助手。\n"
    "你会拿到用户的诉求、可用工具，以及每一步操作后页面返回的观察。\n"
    "按下面的规则行动：\n"
    "1. 只根据观察里出现的东西判断，不要根据网址或片名猜。\n"
    "2. 找不到能看正片的资源时如实说找不到，不要编造链接。\n"
    "3. 每一步只做一个动作。\n"
)


#: ② 训练动作空间。刻意**只列控制流真会发出的动作**——多列一个就会训出
#: 一批永远不会被真实运行验证过的动作，而那类样本在离线评测里是绿的。
#: ``new_tab`` 是**执行器自己的**会话隔离动作，模型永远不该选它；
#: 它仍列在这里是因为它是真实发生过的动作，删掉 P1 就无法回放。
#: 由 :meth:`ToolSpec.visible_to_model` 而非 ``origin`` 决定模型看不看得见。
@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个动作原语的训练侧声明。"""

    name: str
    params: tuple[str, ...]
    instruction: str

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "params": list(self.params),
                "instruction": self.instruction}

    def visible_to_model(self) -> bool:
        """这条动作该不该出现在喂给模型的工具列表里。"""
        return self.name != "new_tab"


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(name="goto", params=("url",),
             instruction="打开一个网址。"),
    ToolSpec(name="click", params=(),
             instruction="点击页面上一个可交互元素（按元素类型与文字指定）。"),
    ToolSpec(name="new_tab", params=("url",),
             instruction="在新标签页打开网址。"),   # 不可见，见 visible_to_model
)


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
        return {"tool": self.tool, "params": dict(self.params),
                "target": dict(self.target) if self.target else None,
                "origin": self.origin}


@dataclass(frozen=True, slots=True)
class Rationale:
    """⑥ 思考+结论（模块 4，W1 尚未落地）。

    ``text`` 是 **refined CoT 的位点**，不是 raw CoT：raw CoT 受红线约束不得落盘，
    而训练需要 CoT SFT。这里留的是**结构**——模块 4 落地时照此填。
    """

    text: str
    conclusion: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text, "conclusion": self.conclusion}


#: 闸门状态。三值而非两值：**「没跑」不是「通过」**。
#: W1 的样本一律是 ``not_run``——把「没跑」渲染成「通过」会让 batch 上线时
#: 看起来像过了闸，而它根本没被检查过。
GateStatus = Literal["passed", "failed", "not_run"]


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
    """

    url: str
    branch: str | None
    source: str = ""
    fallback_used: bool = False
    succeeded: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"url": self.url, "branch": self.branch, "source": self.source,
                "fallback_used": self.fallback_used, "succeeded": self.succeeded}


@dataclass(frozen=True, slots=True)
class Sample:
    """P2 单条样本。字段顺序即六件套顺序。"""

    sample_id: str
    task_id: str
    unit: str                      # "search" | 站点序（"site-3"）
    title: str

    system_prompt: str
    tools: tuple[ToolSpec, ...]
    user_prompt: str
    provenance: Mapping[str, Any]
    actions: tuple[ActionView, ...]
    observations: tuple[Mapping[str, Any], ...]
    rationale: Rationale | None
    gate: GateMark
    outcome: OutcomeRef | None

    #: 模块 4 还没建 → 第 ⑥ 件整个拿不到。**与「模型这轮没说话」是两回事**，
    #: 前者是流水线没长出来，后者是数据事实。
    rationale_missing: bool = True

    #: 本条样本据以生成的 P1（文件名，不含目录）。批次可回溯到源存档。
    source_archive: str = ""

    #: P1 里**没有**的键（老存档缺 ``steps`` / ``user_prompt`` / 全文正文…）。
    #: 非空即表示这条样本是**降级重建**的，别当成与新存档同等的样本混着用。
    degraded_from: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "task_id": self.task_id,
            "unit": self.unit,
            "title": self.title,
            "source_archive": self.source_archive,
            "six": {
                "system_prompt": self.system_prompt,
                "tools": [t.to_json() for t in self.tools],
                "user_prompt": self.user_prompt,
                "provenance": dict(self.provenance),
                "actions": [a.to_json() for a in self.actions],
                "observations": [dict(o) for o in self.observations],
                "rationale": self.rationale.to_json() if self.rationale else None,
            },
            "gate": self.gate.to_json(),
            "rationale_missing": self.rationale_missing,
            "outcome": self.outcome.to_json() if self.outcome else None,
            "degraded_from": list(self.degraded_from),
        }


# ── sample_id ──────────────────────────────────────────────────────────

def sample_id(task_id: str, run_id: str, unit: str) -> str:
    """``<task_id>__<run_id>__<unit>``。

    与 P1 的 ``<task_id>__<run_id>`` 同构，加一段 unit：**排序即时间序**
    （同一批里 unit 单调），且从 id 就能反查源存档，不必开文件。
    """
    return f"{task_id}__{run_id}__{unit}"


def run_id_of(archive_name: str) -> str:
    """从存档文件名取 run_id。``T001__004b6be9.json`` → ``004b6be9``。

    探针取证文件（``obscura_tools.json`` 等）没有 ``__``，那是正常形态——
    它们不是存档，走不到这里。
    """
    stem = Path(archive_name).name
    if stem.endswith(".json"):
        stem = stem[:-5]
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


def _obs_missing(obs: Mapping[str, Any]) -> str | None:
    """这条观察缺了什么，返回可写进 ``degraded_from`` 的标记；不缺返回 ``None``。

    两种缺失要分开，因为它们对下游的影响不同：

    - 完全没有正文 → 观察不可用，rationale 无从核查；
    - 只有 ``body_preview``（400 字符）→ **正文被截短了**。这件事必须显式，
      否则 rationale 的实体核查会把落在预览之外的实体判成幻觉——那不是
      幻觉，是**没存**。悄悄拿 400 字符当全文喂进去，等于凭空造出一批
      假幻觉，而闸门会把它们当成真的模型错误记进负样本池。
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
        out.append(ActionView(
            tool=str(a.get("tool", "")),
            params=dict(a.get("params") or {}),
            target=dict(a["target"]) if a.get("target") else None,
            origin=str(a.get("origin") or "model"),
        ))
    return tuple(out)


def _outcome_for(
    outcomes: Sequence[Mapping[str, Any]], *urls: str
) -> OutcomeRef | None:
    """按 url 找那条 outcome。**先候选 url、再落地 url。**

    两个都要试，顺序不能反：存档里 ``visits[i].url`` 是**候选链接**，
    而 ``outcomes[j].url`` 记的是**落地后的地址**——站点做 ``http→https``
    跳转时两者不等。实测 4 份真实存档里的每个 run 都有 3 个访问点对不上：

        visits[1].url  = 'http://www.iqiyi.com/...?vfm=...'
        outcomes[1].url = 'https://www.iqiyi.com/...?vfm=...'

    只按候选 url 匹配的后果是**静默**的：找不到就返回 ``None``，
    而 ``None`` 与「搜索阶段本来就没有 outcome」长得一模一样——
    于是每条负样本都显示成「无结论」，报表上一条负样本都不少，
    只是全都查不到分支。分母没错、分子为空，没人会发现。

    刻意**不看**存档里的 ``is_negative_sample`` 字段——那份判据由
    :data:`~trajectory_pipeline.executor.branches.NON_SAMPLE_BRANCHES` 定义，
    而 assembler 不 import executor，就不能保证两份口径同步。
    从 ``branch`` 现推：``unresolved`` / ``trailer_suspect`` 不是负样本。
    """
    for want in urls:
        if not want:
            continue
        for o in outcomes or ():
            if o.get("url") == want:
                return OutcomeRef(
                    url=str(o.get("url")),
                    branch=o.get("branch"),
                    source=str(o.get("source") or ""),
                    fallback_used=bool(o.get("fallback_used")),
                )
    return None


def _sample(
    *,
    archive: Mapping[str, Any],
    source_name: str,
    unit: str,
    actions_: tuple[ActionView, ...],
    observations: tuple[Mapping[str, Any], ...],
    outcome: OutcomeRef | None,
    degraded: list[str],
) -> Sample:
    task_id = str(archive.get("task_id"))
    return Sample(
        sample_id=sample_id(task_id, run_id_of(source_name), unit),
        task_id=task_id,
        unit=unit,
        title=str(archive.get("title") or ""),
        system_prompt=SYSTEM_PROMPT,
        tools=TOOL_SPECS,
        user_prompt=str(archive.get("user_prompt") or ""),
        provenance=dict(archive.get("provenance") or {}),
        actions=actions_,
        observations=observations,
        rationale=None,
        # 模块 4 未落地 → 闸门**没跑过**，不是「通过」。
        gate=GateMark(status="not_run",
                      reasons=("rationale 模块未落地，一致性闸门未运行",)),
        outcome=outcome,
        rationale_missing=True,
        source_archive=source_name,
        degraded_from=tuple(dict.fromkeys(degraded)),
    )


def split_archive(path: str | Path) -> list[Sample]:
    """把一份 P1 存档切成若干条样本（一个决策单元一条）。

    产出条数 = 1（搜索阶段）+ N（每个访问过的站点）。搜索阶段在
    ``search_blocked`` 非空时**不产出**——被反爬拦住的 run 压根没进入
    决策，那一步的观察是验证码页，切成「模型该怎么做」的样本只会教它
    学会对着验证码页编答案。这与 executor 把反爬单开字段、不进分支体系
    是同一条纪律的延续。
    """
    p = Path(path)
    archive = read_archive(p)
    source_name = p.name
    task_id = str(archive.get("task_id"))
    base_degraded: list[str] = []
    if not archive.get("user_prompt"):
        base_degraded.append("user_prompt")
    if not archive.get("run_config"):
        # 与 ``executor.integrity`` 的 ``run_config_missing`` 同码——
        # 两张表由 ``tests/unit/test_integrity.py::TestDegradedMarkersMatchSplit``
        # 双向盯着，谁先改谁就红。
        #
        # 它不进 P2 的训练视图（那六件套里没有运行参数），
        # 但样本仍要知道「这份数据是降级重建的」，
        # 否则老批次与新批次会被当成同等样本混着用。
        base_degraded.append("run_config")

    steps = archive.get("steps") or []
    if not steps:
        base_degraded.append("steps")

    out: list[Sample] = []

    # ── 单元 1：搜索阶段 ────────────────────────────────────────────
    search_obs = archive.get("search_observation")
    if search_obs and not archive.get("search_blocked"):
        degraded = list(base_degraded)
        miss = _obs_missing(search_obs)
        if miss:
            degraded.append(miss)
        out.append(_sample(
            archive=archive, source_name=source_name, unit="search",
            actions_=_actions_of(steps),
            observations=(dict(search_obs),),
            outcome=None,             # 搜索阶段不记站点级 outcome
            degraded=degraded,
        ))

    # ── 单元 2..N：每个站点 ────────────────────────────────────────
    outcomes = archive.get("outcomes") or []
    for i, visit in enumerate(archive.get("visits") or (), 1):
        vsteps = visit.get("steps") or []
        degraded = list(base_degraded)
        if not vsteps:
            degraded.append("steps")
        for key in ("site_obs", "player_obs"):
            miss = _obs_missing(visit.get(key))
            if miss:
                degraded.append(miss)
        outcome = _outcome_for(outcomes, str(visit.get("url") or ""),
                               str(visit.get("landed_url") or ""))
        if outcome is None:
            # 站点级样本查不到 outcome = 这条没有分支结论。它不能只是
            # ``None``：那与「搜索阶段本来就没有 outcome」同形。
            degraded.append("outcome_missing")
        else:
            succeeded = bool(visit.get("success"))
            if succeeded != (outcome.branch is None):
                # 「成功」只有一个来源说了算。两个来源对不上时标记出来，
                # 而不是挑一个——挑一个就是在猜，而这条样本进不进训练集
                # 全看它。
                degraded.append("success_branch_mismatch")
            outcome = replace(outcome, succeeded=succeeded)
        out.append(_sample(
            archive=archive, source_name=source_name, unit=f"site-{i}",
            actions_=_actions_of(vsteps),
            observations=tuple(
                dict(o) for o in (visit.get("site_obs"), visit.get("player_obs"))
                if o
            ),
            outcome=outcome,
            degraded=degraded,
        ))

    if not out and archive.get("search_blocked"):
        # 被反爬拦的 run 产出零条**是正常形态**，不是错误。抛出去会让
        # :func:`iter_samples` 在遍历目录时整批中断——而一批里混进几个
        # 被拦的 run 是常态（见 executor 里「直接重跑同一引擎大概率仍是
        # 验证码页」那条警告）。
        return []
    if not out:
        raise P1ShapeError(
            f"{source_name}: task_id={task_id} 的存档里既没有搜索观察也没有访问记录，"
            f"切不出样本（warnings={archive.get('warnings')!r}）"
        )
    return out


def iter_samples(root: str | Path, pattern: str = "*.json") -> Iterable[Sample]:
    """遍历一个目录下的全部存档并切条。

    - **探针取证文件自动跳过**。判据用 ``__`` 而不是文件名白名单：白名单
      漏一个就把取证当存档读进来，而 ``obscura_tools.json`` 顶层没有
      ``task_id``，会直接抛 :class:`P1ShapeError` 把整批打断。
    - **复核档跳过**：由 executor 侧 :func:`~trajectory_pipeline.executor.archive.select_archives`
      统一挑选。这里再选一次，同一条就会跑两遍，而两遍的复核结论可能不同。
    - **被反爬拦的 run 产出零条**，不算错误（见 :func:`split_archive`）。
    - **存档本身损坏则抛出，不吞**。坏档静默变成「零样本」的话，
      批次看起来只是少了几条，没人会去查。异常消息里带文件名。
    """
    for path in sorted(Path(root).glob(pattern)):
        if path.name.endswith(".reviewed.json"):
            continue                    # 复核档由 executor 侧挑选，不在这里重选
        if "__" not in path.stem:
            continue                    # 探针取证，不是存档
        yield from split_archive(path)


__all__ = [
    "SYSTEM_PROMPT", "TOOL_SPECS", "ToolSpec", "ActionView", "Rationale",
    "GateStatus", "GateMark", "OutcomeRef", "Sample", "P1ShapeError",
    "sample_id", "run_id_of", "read_archive", "split_archive", "iter_samples",
]
