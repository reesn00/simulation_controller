"""批次级断言（rubric v1.3 的 B-1..B-5）——**一批 = 一个 run**。

## 「批」是什么

rubric v1.3 把「批」写死为 **一个 run**：一批 = **一次运行的存档**
（``run_id``），批次级断言表**每个 run 一份**。不是每个「评分批次」一份，
也不是每个 rubric 版本一份。

理由：批次级断言量的是**一次运行的结构属性**（动作空间、候选处置、
异常处置、结论错率）——这些属性只在 run 之间可比。把多个 run 合成
一批再算比率，分母就把不同运行的处置密度混在一起，读数**没有对象**
（「这条线读的是哪一档 × 哪个口径 × 哪一侧」——口径 C 的教训）。

## 为什么不出一张 1–5 分

批次级断言**不出分**。每条给的是「判定结果 + 读数 + 口径」：
通过 / 不通过 / 只出读数 / **测不成**。

「测不成」是独立的一档，不是「不通过」——**判不出 ≠ 判错**
（CLAUDE.md 的 fail-closed：``None`` 不等于 ``0``）。B-3 现在就落在
这一档：黄金集里**没有**「播放站 / 导航站 / 无关站」的类别标注，
D-3 的 judge 读数也还没落盘 ⇒ 分母不存在 ⇒ **测不成**，不是「0%」。

## 三处口径的由来（都是踩过之后定死的）

**B-2 分母**：取 ``run_config.max_candidates``（**每个 run 自己的**，
不能用全批统一值）。``run_config`` 缺失 ⇒ 分母不可知 ⇒ **不通过**
（rubric B-2 明写「候选上限被改过而 run_config 未记录，导致分母不可知」）。

**B-4 分母**：**含异常的访问数**。「异常」有两个来源——

- 结论分支落在 :data:`ABNORMAL_BRANCHES` 里（访问被挡 / 采集失败 /
  内容损坏 / 感知层没给出结论）；
- **有异常信号但连分支都没记**（步骤报错、或一份观察都没采到）——
  这类是「静默丢弃」，rubric B-4 ① 明写要抓的就是它。

只用第一个来源会让 ① 变成恒真的同义反复（分母按「记了异常分支」筛，
再检查「有没有记异常分支」）。加上第二个来源，① 才有判别力。

⚠️ **B-4 ③ 的字面判据有已知的不匹配**（读数会照实报出来，不擅自放宽）：
③ 用的是与 D-3 **同一套**锚定判据（引用观察里的原文短语 / 元素名 /
计数）。而代码侧的止损理由有两类压根不引用观察——**控制流事件**
（「步数预算耗尽」「后端未作答」）与**转述**（「本页有非预告播放控件」，
说的是观察的内容但不引原文）。这两类会被字面判据判成「无观察锚定」。

本模块的处理：**照实报数 + 归因**——观察本身不完整（降级 / 截断 /
元素被裁）的归**采集侧**，观察完整的归**理由侧**。判据要不要放宽
（让控制流事件类理由也算合规）是 **rubric 级问题**，须 owner 裁定，
本模块不擅自改口径。

**B-5 分母**：**只进三态口径里「两侧都有结论」的行**（一致 + 不一致）。
「双方都没结论」与「单侧弃权」不进分母——「没结论」既不是一致也不是
不一致（CLAUDE.md：``None`` ≠ ``0``）。

⚠️ **B-5 默认只读调参池（calibration 档）**：持有集（D-C5）与回归集
（D-C8）是**封存面**，读它们 = 拿封存面调参。要读别的档必须由调用方
**显式**传 ``splits=``，读数里会记下读了哪几档。

## 不做什么

- 不投票、不集成——五条断言各读各的事实，分歧原样报出来；
- 不替人裁「异常分支」口径——:data:`ABNORMAL_BRANCHES` 是**模块内
  明写的一张表**（不是从别处派生的），改了它就是改了口径，改动要在
  本模块的 git 历史里看得见；
- 不读 raw CoT / Cookie / Authorization Header。
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from trajectory_pipeline import outcomerules
from trajectory_pipeline.common.elements import element_name, element_role
from trajectory_pipeline.evaluation import observation_match
from trajectory_pipeline.evaluation.golden.alignment import hard_agreement
from trajectory_pipeline.evaluation.scorers import thresholds
from trajectory_pipeline.executor.actions import TOOLS
from trajectory_pipeline.executor.branches import BRANCH_LABELS

# ── 状态词汇 ──────────────────────────────────────────────────────

#: 断言通过。
STATUS_PASS = "通过"
#: 断言不通过。
STATUS_FAIL = "不通过"
#: **只出读数**——没有线可比，只报数（B-2 / B-4 / B-5）。
STATUS_READOUT = "只出读数"
#: **测不成**——判不出（缺数据、缺分母）。**不是「不通过」。**
STATUS_UNTESTABLE = "测不成"

#: 全部批次级断言的条款号（rubric v1.3：批次级 **5 维**）。
CLAUSES: tuple[str, ...] = ("B-1", "B-2", "B-3", "B-4", "B-5")

#: 单条读数**最多列几条**违规明细。
#:
#: ⚠️ **截断必须**留痕**：明细长到只看前 20 条时，读的人会以为「就这 20
#: 条」——而分母可能是几百（实测一个 run 就有 60+ 条不合规）⇒ **读数离开
#: 口径就没有对象**。截断时补一行「还有 N 条未列」，让「20」不被读成
#: 「全部」。
MAX_CAVEATS = 20

#: 断言名的真源——与 rubric 卡的标题逐字对齐。
CLAUSE_NAMES: Mapping[str, str] = {
    "B-1": "动作 schema 合规性",
    "B-2": "检索覆盖完整性",
    "B-3": "语义判断一致率",
    "B-4": "异常处置合规率",
    "B-5": "结论错率",
}


# ── B-1 的动作 schema ─────────────────────────────────────────────

#: 各动作的**必填参数**（键名 = 驱动原语的入参名，见
#: :mod:`trajectory_pipeline.executor.browser.page_driver`）。
#:
#: 形态：``tool`` → ``((参数名, 是否要求非空白), …)``。
#:
#: ⚠️ **「非空白」与「非空」是两件事**：``press`` 的 ``key`` 合法取值
#: 就有 ``" "``（空格键）——实测存档里有 32 处 ``key=" "``。把它按
#: ``strip()`` 判成「缺参数」会造出 32 条**假的**不合规（实测踩到过）。
#:
#: ⚠️ **只列真正必填的**：
#:
#: - ``new_tab`` 的 ``url`` **不列**——驱动签名是 ``url: str | None = None``，
#:   开空白页是合法形态；把它列成必填会造出一条永远不成立的断言；
#: - ``close_tab`` 的 ``tab_id`` 同理；
#: - ``click`` 不列参数——它的目标在 ``action["target"]`` 里（方言两套
#:   键名），不在 ``params`` 里。
_REQUIRED_PARAMS: Mapping[str, tuple[tuple[str, bool], ...]] = {
    "goto": (("url", True),),
    "fill": (("text", False),),
    "press": (("key", False),),
}

#: 必须是**字符串**的参数名（其余类型的值都算类型不符）。
_STRING_PARAMS: frozenset[str] = frozenset({"url", "text", "key"})

#: 必须带**目标**的动作。目标存在 ``action["target"]``——
#: aria 面是 ``{"role", "name"}``，obscura 面是 ``{"tag", "label"}``，
#: 两套都读得出来（:func:`~trajectory_pipeline.common.elements.element_role`
#: / :func:`~...element_name`）。
_TARGET_REQUIRED: frozenset[str] = frozenset({"click", "fill"})


# ── B-4 的「异常」口径 ────────────────────────────────────────────

#: **异常分支**——B-4 的分母口径之一（模块内明写的一张表）。
#:
#: 收的是「**这次没能得出结论**」的四类形态：
#:
#: - 传输层失败（没打开）：``unreachable_hard``；
#: - 访问被挡：``login_required`` / ``blocked``（地区 / DRM / 验证码 /
#:   反爬）/ ``login_wall_or_blocked``（历史合并态，仍在存量存档里）；
#: - 内容损坏：``broken``；
#: - 感知层没给出结论：``unresolved``（fail-closed 的「判不出就不判」）。
#:
#: **不收**：``not_play_site`` / ``no_play_control`` /
#: ``component_unverified`` / ``subscription_wall`` / ``trailer_only`` ——
#: 那些是**正常负结论**（页面在手，就是没有可看的东西），不是「异常」。
#: 把正常负结论算进异常，会让 B-4 的分母被「判得准」撑大，异常处置的
#: 合规率读数反而被稀释。
ABNORMAL_BRANCHES: frozenset[str] = frozenset({
    "unreachable_hard",
    "login_required",
    "blocked",
    "login_wall_or_blocked",
    "broken",
    "unresolved",
})


# ── 读数的呈现 ────────────────────────────────────────────────────


def _trim(lines: Sequence[str]) -> tuple[str, ...]:
    """明细截到 :data:`MAX_CAVEATS` 条，**截断了就说还剩几条**。

    不留痕的话，「前 20 条」会被读成「就这些」——而分母可能是几百。
    """
    if len(lines) <= MAX_CAVEATS:
        return tuple(lines)
    hidden = len(lines) - MAX_CAVEATS
    return tuple(lines[:MAX_CAVEATS]) + (f"（还有 {hidden} 条未列）",)


# ── 协议 ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AssertReading:
    """一条批次级断言的读数。"""

    #: 条款号（``B-1`` … ``B-5``）。
    clause: str
    #: 条款名（与 rubric 卡同名）。
    name: str
    #: :data:`STATUS_PASS` / :data:`STATUS_FAIL` / :data:`STATUS_READOUT`
    #: / :data:`STATUS_UNTESTABLE`。
    status: str
    #: 人读的一句话——**口径 + 数**都写在这里（报表上不能只有数）。
    reading: str
    #: 分子（``None`` = 这条断言没有分子）。
    numerator: int | None = None
    #: 分母（``None`` = 这条断言没有分母 / 分母不可知）。
    denominator: int | None = None
    #: 口径说明与告警——**每条必读**：读数离开口径就没有对象。
    caveats: tuple[str, ...] = ()

    @property
    def rate(self) -> float | None:
        """比率（分子 / 分母）。**分母为 0 或缺分母 → ``None``**。

        ⚠️ 0/0 **不是 0**：那是「没有可判的东西」，不是「一条都没合规」。
        """
        if self.numerator is None or not self.denominator:
            return None
        return self.numerator / self.denominator

    @property
    def is_pass(self) -> bool:
        """这条断言**通过**吗。

        ⚠️ :data:`STATUS_UNTESTABLE`（测不成）**不算通过**——它是
        「判不出」，把它算成通过等于把缺数据读成达标。
        """
        return self.status == STATUS_PASS

    def to_json(self) -> dict[str, Any]:
        return {
            "clause": self.clause,
            "name": self.name,
            "status": self.status,
            "reading": self.reading,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "rate": self.rate,
            "caveats": list(self.caveats),
        }


@dataclass(frozen=True, slots=True)
class RunAsserts:
    """一个 run 的批次级断言表（**每 run 一份**）。"""

    run_id: str
    task_id: str
    archive: str
    readings: tuple[AssertReading, ...]

    @property
    def failed(self) -> tuple[AssertReading, ...]:
        """不通过的断言（rubric G-7：任一不过 → 暂停该批发放）。"""
        return tuple(r for r in self.readings if r.status == STATUS_FAIL)

    @property
    def untestable(self) -> tuple[AssertReading, ...]:
        """测不成的断言——**单列**，不混进「不通过」。"""
        return tuple(r for r in self.readings if r.status == STATUS_UNTESTABLE)

    def by_clause(self, clause: str) -> AssertReading:
        """按条款号取读数。条款号不在表里 → ``KeyError``（fail-closed）。"""
        for reading in self.readings:
            if reading.clause == clause:
                return reading
        raise KeyError(f"批次级断言表里没有条款 {clause!r}")

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "archive": self.archive,
            "readings": [r.to_json() for r in self.readings],
            "failed": [r.clause for r in self.failed],
            "untestable": [r.clause for r in self.untestable],
        }

    def render_text(self) -> str:
        """人读版（仿 rubric 的「批次级断言记录单」）。"""
        lines = [
            f"批次ID(= run_id): {self.run_id}  任务: {self.task_id}  存档: {self.archive}",
            "-" * 60,
        ]
        for reading in self.readings:
            lines.append(f"{reading.clause} {reading.name}: {reading.status} — {reading.reading}")
            for caveat in reading.caveats:
                lines.append(f"    · {caveat}")
        lines.append("-" * 60)
        lines.append(
            "批次级结论: " + ("暂停发放" if self.failed else "通过（无断言不通过）")
        )
        return "\n".join(lines)


# ── B-1 ────────────────────────────────────────────────────────────


def _iter_actions(archive: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    """全批的动作——**存档级 steps + 每个 visit 的 steps**。

    产出 ``(位置, action)``；``位置`` 形如 ``steps[3]`` /
    ``visits[2].steps[0]``，报违规时能直接指到是哪一步。
    """
    for i, step in enumerate(archive.get("steps") or ()):
        action = (step or {}).get("action") or {}
        if isinstance(action, Mapping):
            yield f"steps[{i}]", action
    for vi, visit in enumerate(archive.get("visits") or ()):
        for si, step in enumerate((visit or {}).get("steps") or ()):
            action = (step or {}).get("action") or {}
            if isinstance(action, Mapping):
                yield f"visits[{vi}].steps[{si}]", action


def _target_ok(action: Mapping[str, Any]) -> bool:
    """目标齐不齐。

    目标在 ``action["target"]`` 里（方言两套键名都读）。**空目标 = 缺**。
    """
    target = action.get("target")
    if not isinstance(target, Mapping):
        return False
    return bool(element_role(target).strip() or element_name(target).strip())


def _assert_b1(archive: Mapping[str, Any]) -> AssertReading:
    """B-1 动作 schema 合规性。

    断言：全批所有动作的 ``tool`` ∈ 已声明动作空间，且 ``params`` 符合
    各动作 schema。
    """
    violations: list[str] = []
    total = 0
    for where, action in _iter_actions(archive):
        total += 1
        tool = str(action.get("tool") or "")
        if tool not in TOOLS:
            violations.append(f"{where}: tool={tool!r} 不在已声明动作空间 {list(TOOLS)}")
            continue
        params = action.get("params")
        if not isinstance(params, Mapping):
            violations.append(f"{where}: tool={tool!r} 的 params 不是字典")
            continue
        for key, must_be_nonblank in _REQUIRED_PARAMS.get(tool, ()):
            value = params.get(key)
            if not isinstance(value, str):
                violations.append(f"{where}: tool={tool!r} 缺必填参数 {key!r}")
            elif not value:
                violations.append(f"{where}: tool={tool!r} 的必填参数 {key!r} 是空串")
            elif must_be_nonblank and not value.strip():
                violations.append(
                    f"{where}: tool={tool!r} 的必填参数 {key!r} 只有空白"
                )
        for key, value in params.items():
            if key in _STRING_PARAMS and not isinstance(value, str):
                violations.append(f"{where}: tool={tool!r} 的参数 {key!r} 不是字符串")
        if tool in _TARGET_REQUIRED and not _target_ok(action):
            violations.append(f"{where}: tool={tool!r} 缺目标（target 为空）")
    caveats: list[str] = []
    if total == 0:
        caveats.append("本批没有任何动作——B-1 无对象")
    if violations:
        return AssertReading(
            "B-1", CLAUSE_NAMES["B-1"], STATUS_FAIL,
            f"{total} 个动作里 {len(violations)} 个不合规",
            numerator=len(violations), denominator=total,
            caveats=_trim(caveats + violations),
        )
    return AssertReading(
        "B-1", CLAUSE_NAMES["B-1"], STATUS_PASS,
        f"{total} 个动作全部合规（tool ∈ {list(TOOLS)}，必填参数齐备）",
        numerator=total, denominator=total, caveats=tuple(caveats),
    )


# ── B-2 ────────────────────────────────────────────────────────────


def _assert_b2(archive: Mapping[str, Any]) -> AssertReading:
    """B-2 检索覆盖完整性。

    断言：候选处置覆盖率 = 有明确处置的候选数 / 本批候选上限内的候选数。

    分母取 ``run_config.max_candidates``（**每个 run 自己的**）与本批
    候选数的较小者——候选数已受上限约束，取较小者是 fail-closed 的
    写法：候选数异常超过上限时不把分母撑大。
    """
    run_config = archive.get("run_config") or {}
    cap = run_config.get("max_candidates")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        return AssertReading(
            "B-2", CLAUSE_NAMES["B-2"], STATUS_FAIL,
            "分母不可知：run_config 没记 max_candidates",
            caveats=(
                "rubric B-2「不通过的情形」：候选上限被改过而 run_config 未记录，"
                "导致分母不可知",
            ),
        )
    candidates = archive.get("candidates") or []
    outcomes = archive.get("outcomes") or []
    denominator = min(len(candidates), cap)
    # 「有明确处置」= 配到了 outcome 且它带着分支（成功也算处置——成功
    # 是「分支为空」，不是「没处置」）。
    disposed: set[str] = set()
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            continue
        for key in ("url", "site_url"):
            value = str(outcome.get(key) or "").strip()
            if value:
                disposed.add(value)
    handled = sum(
        1 for c in candidates
        if isinstance(c, Mapping) and str(c.get("url") or "").strip() in disposed
    )
    caveats: list[str] = []
    filters = archive.get("candidate_filter") or {}
    if filters:
        detail = "、".join(f"{k}={v}" for k, v in sorted(filters.items()))
        caveats.append(
            f"结构性剔除只有计数、没有逐条 URL 痕迹（{detail}）⇒ "
            "剔除动作**不可复核**（rubric B-2 边界：剔除须逐条留 URL 痕迹）"
        )
    if denominator == 0:
        return AssertReading(
            "B-2", CLAUSE_NAMES["B-2"], STATUS_READOUT,
            "本批没有候选——分母为 0，比率无定义（不是 0%）",
            numerator=0, denominator=0, caveats=tuple(caveats),
        )
    rate = handled / denominator
    return AssertReading(
        "B-2", CLAUSE_NAMES["B-2"], STATUS_READOUT,
        f"候选处置覆盖率 {rate:.1%}（{handled} / {denominator}，"
        f"分母 = min(候选数 {len(candidates)}, run_config.max_candidates {cap})）",
        numerator=handled, denominator=denominator, caveats=tuple(caveats),
    )


# ── B-3 ────────────────────────────────────────────────────────────


def _assert_b3(d3_pairs: Sequence[tuple[str, str]]) -> AssertReading:
    """B-3 语义判断一致率。

    断言：与黄金集人工类别标注的一致率，阈值 ≥ 领证线。

    ⚠️ **默认测不成**：黄金集里**没有**「播放站 / 导航站 / 无关站」的
    类别标注（``golden.dimension_scores`` 只有 d2 / d4 / d5 / d6），
    D-3 的 judge 读数也还没落盘 ⇒ 分母不存在。

    调用方手上有 ``(人工类别, 本批判定类别)`` 对子时，这条才出读数。
    """
    if not d3_pairs:
        return AssertReading(
            "B-3", CLAUSE_NAMES["B-3"], STATUS_UNTESTABLE,
            "本批没有站点类型判定对照——测不成",
            caveats=(
                "黄金集里没有「播放站 / 导航站 / 无关站」的类别标注，"
                "D-3 的 judge 读数也还没落盘 ⇒ 分母不存在",
            ),
        )
    hits = sum(1 for gold, pred in d3_pairs if gold == pred)
    denominator = len(d3_pairs)
    rate = hits / denominator
    line = thresholds.value("d3_batch_agreement_min")
    passed = rate >= line
    return AssertReading(
        "B-3", CLAUSE_NAMES["B-3"],
        STATUS_PASS if passed else STATUS_FAIL,
        f"语义判断一致率 {rate:.1%}（{hits} / {denominator}）"
        f"——领证线 {line:.0%}，{'过线' if passed else '不过线'}",
        numerator=hits, denominator=denominator,
        caveats=(f"线绑在：{thresholds.scope_text('d3_batch_agreement_min')}",),
    )


# ── B-4 ────────────────────────────────────────────────────────────


def _visit_observations(visit: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """这个访问点采到的观察（``site_obs`` + ``player_obs``）。"""
    out: list[Mapping[str, Any]] = []
    for key in ("site_obs", "player_obs"):
        obs = visit.get(key)
        if isinstance(obs, Mapping) and obs:
            out.append(obs)
    return out


def _observation_empty(obs: Mapping[str, Any]) -> bool:
    """这份观察**是空的**（正文与交互元素都没有）——即采集降级。"""
    body = str(obs.get("body_text") or "").strip()
    elements = obs.get("interactive_elements") or ()
    return not body and not elements


def _observation_incomplete(obs: Mapping[str, Any]) -> str:
    """这份观察**不完整**——空串 = 完整。

    不完整有三种形态（都从观察自己的字段读，不猜）：

    - **正文与元素皆空** ⇒ 采集降级（``degraded`` 里通常记着原因）；
    - ``truncated`` 为真 ⇒ 正文被 ``max_chars`` 截断——**理由引用的内容
      可能压根不在存档里**；
    - ``elements_total`` > 保留的交互元素数 ⇒ 元素被裁——同款。

    ⚠️ 这三种都让「引用了观察里的具体内容」**无从谈起** ⇒ ③ 判不合规时
    责任在**采集侧**，不在止损理由上。归因错了，读的人会去改理由，
    而该改的是采集。
    """
    if _observation_empty(obs):
        degraded = obs.get("degraded") or []
        return f"观察是空的（降级：{list(degraded) if degraded else '未记原因'}）"
    if bool(obs.get("truncated")):
        return "观察正文被截断（truncated）"
    total = int(obs.get("elements_total") or 0)
    kept = len(obs.get("interactive_elements") or ())
    if total > kept:
        return f"交互元素被裁（保留 {kept} / 采到 {total}）"
    return ""


def _visit_errors(visit: Mapping[str, Any]) -> tuple[str, ...]:
    """这个访问点里**动作报错**的原文。"""
    return tuple(
        str((step or {}).get("error") or "").strip()
        for step in (visit.get("steps") or ())
        if str((step or {}).get("error") or "").strip()
    )


def _visit_outcome(
    visit: Mapping[str, Any], outcomes: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """这个访问点配到的 outcome。

    配对口径**用 :mod:`trajectory_pipeline.outcomerules`**（候选 → 落地
    → 播放页逐档试，每档匹 ``site_url`` 或 ``url``，同档取末条）——
    **不自己写一套**：P2 / T 视图 / 黄金集三方的口径都在那儿，写第四套
    只会多一处漂。
    """
    player_obs = visit.get("player_obs")
    player_url = (
        str(player_obs.get("url") or "")
        if isinstance(player_obs, Mapping) else ""
    )
    hit = outcomerules.resolve_at(
        outcomes,
        str(visit.get("url") or ""),
        str(visit.get("landed_url") or ""),
        player_url,
    )
    return hit[1] if hit is not None else None


def _assert_b4(archive: Mapping[str, Any]) -> AssertReading:
    """B-4 异常处置合规率。

    一条访问**合规**指三件事同时成立：

    ① 该异常被记成分支，未静默丢弃；
    ② 异常后**立即转向下一站**，未在同一 URL 上重复无效动作；
    ③ 止损理由锚定观察（引用了存档里的具体依据，而非主观判断）。
    """
    visits = list(archive.get("visits") or ())
    outcomes = list(archive.get("outcomes") or ())
    denominator = 0
    compliant = 0
    failures: list[str] = []
    no_observation = 0
    degraded_empty = 0
    for i, visit in enumerate(visits):
        outcome = _visit_outcome(visit, outcomes)
        branch = str((outcome or {}).get("branch") or "").strip()
        errors = _visit_errors(visit)
        observations = _visit_observations(visit)
        abnormal = branch in ABNORMAL_BRANCHES
        # 「异常信号」：动作报错，或一份观察都没采到。
        signal = bool(errors) or not observations
        # 静默丢弃 = 有异常信号、却**连分支都没记**（成功或漏记）。
        dropped = signal and not branch
        if not abnormal and not dropped:
            continue
        denominator += 1
        why: list[str] = []
        # ① 记成分支
        if not branch:
            why.append("① 异常未记账（既没有分支也没有结论）")
        elif branch not in BRANCH_LABELS:
            why.append(f"① 分支 {branch!r} 不在分支注册表里")
        # ② 转向下一站
        if i + 1 < len(visits):
            nxt = str((visits[i + 1] or {}).get("url") or "")
            cur = str(visit.get("url") or "")
            if nxt and nxt == cur:
                why.append("② 异常后在同一 URL 上重复动作（下一站 URL 相同）")
        # ③ 止损理由锚定观察
        evidence = str((outcome or {}).get("evidence") or "")
        anchors = observation_match.anchored_phrases(evidence, observations)
        if not anchors and not observations:
            # 没有观察可锚 ⇒ 退到**步骤报错原文**上（同一条判据：
            # ≥4 字连续子串）。理由必须锚在某个具体东西上——观察、
            # 或者报错原文；「感觉不行」两者都不是。
            no_observation += 1
            for err in errors:
                hit = observation_match.text_anchor(err, evidence)
                if hit:
                    anchors = (f"报错原文「{hit[:40]}」",)
                    break
            if not anchors:
                why.append(
                    "③ 止损理由没有锚点（既没引用观察、也没引用报错原文）"
                    "——**责任在采集侧**（这次没有观察存档）"
                )
        elif not anchors:
            # 有观察但没锚上 ⇒ 看**观察本身完不完整**：不完整时
            # 「引用观察里的具体内容」无从谈起（理由引用的内容可能压根
            # 不在存档里），责任在采集侧；完整时才是理由自己的问题。
            incomplete = next(
                (why_text for why_text in
                 (_observation_incomplete(o) for o in observations) if why_text),
                "",
            )
            if incomplete:
                degraded_empty += 1
                why.append(f"③ 无从锚定：{incomplete}——**责任在采集侧**")
            else:
                why.append("③ 止损理由无观察锚定")
        if why:
            failures.append(f"visits[{i}] {branch or '(无分支)'}：" + "；".join(why))
        else:
            compliant += 1
    caveats: list[str] = []
    if no_observation:
        caveats.append(
            f"{no_observation} 条异常访问没有观察存档 ⇒ ③ 退到「锚步骤报错原文」判"
        )
    if degraded_empty:
        caveats.append(
            f"{degraded_empty} 条异常访问的观察不完整（降级 / 截断 / 元素被裁）⇒ "
            "③ 无从锚定，**责任在采集侧**（不是止损理由写得不好）"
        )
    if denominator == 0:
        return AssertReading(
            "B-4", CLAUSE_NAMES["B-4"], STATUS_READOUT,
            "本批没有异常访问——分母为 0，比率无定义（不是 100%）",
            numerator=0, denominator=0, caveats=tuple(caveats),
        )
    rate = compliant / denominator
    return AssertReading(
        "B-4", CLAUSE_NAMES["B-4"], STATUS_READOUT,
        f"异常处置合规率 {rate:.1%}（{compliant} / {denominator}，"
        "判据 = ①记成分支 ②转向下一站 ③止损理由锚定观察）",
        numerator=compliant, denominator=denominator,
        caveats=_trim(caveats + failures),
    )


# ── B-5 ────────────────────────────────────────────────────────────


def rows_for_run(
    rows: Sequence[Mapping[str, Any]], task_id: str, run_id: str
) -> list[Mapping[str, Any]]:
    """从黄金集行里挑出**属于这个 run** 的那些。

    认键：``task_id`` + ``run_id``（run_id = 存档名里 ``__`` 后那段
    哈希，见 :func:`trajectory_pipeline.assembler.schema.run_id_of`）。
    两个都认——只看哈希的话，两个任务撞上同一哈希就会把两批行混进
    一个分母。
    """
    out: list[Mapping[str, Any]] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        if str(row.get("task_id") or "") != str(task_id or ""):
            continue
        if str(row.get("run_id") or "") != str(run_id or ""):
            continue
        out.append(row)
    return out


def _assert_b5(
    gold_rows: Sequence[Mapping[str, Any]], *, split: str = "calibration"
) -> AssertReading:
    """B-5 结论错率。

    错率 = 不一致条数 / 本批进入 D-10 判定的样本数。
    分母**只进三态口径里「两侧都有结论」的行**（一致 + 不一致）。

    ⚠️ **只出读数**（rubric v1.3 未定线）：报读数，不报合格 / 不合格。
    要定线时先补 :mod:`...scorers.thresholds`（D-C6：定线不进 rubric
    变更记录）。
    """
    if not gold_rows:
        return AssertReading(
            "B-5", CLAUSE_NAMES["B-5"], STATUS_UNTESTABLE,
            "本批没有进入 D-10 判定的黄金集行——测不成",
            caveats=(
                f"分母按「{split} 档」取；本 run 的行不在这一档里"
                "（持有集 / 回归集是封存面，按 D-C5 / D-C8 纪律不读）",
            ),
        )
    agree = disagree = undetermined = 0
    undetermined_reasons: list[str] = []
    for row in gold_rows:
        golden = (row.get("golden") or {}) if isinstance(row, Mapping) else {}
        code = (row.get("code") or {}) if isinstance(row, Mapping) else {}
        gold_label = str(golden.get("label") or "").strip()
        code_branch = str(code.get("branch") or "").strip()
        try:
            state = hard_agreement(gold_label, code_branch)
        except KeyError as exc:
            # 标签没登记在等价类表 ⇒ **判不出**（fail-closed），不猜。
            return AssertReading(
                "B-5", CLAUSE_NAMES["B-5"], STATUS_UNTESTABLE,
                f"标签未登记在语义等价类表，读数不成立：{exc}",
                caveats=("口径 C 的等价类表缺这条标签——先补表再读数",),
            )
        if state.state == "agree":
            agree += 1
        elif state.state == "disagree":
            disagree += 1
        else:
            undetermined += 1
            undetermined_reasons.append(
                f"{state.side}：{gold_label or '(空)'} vs {code_branch or '(空)'}"
            )
    denominator = agree + disagree
    caveats = [
        f"分母口径 = 三态里「两侧都有结论」的行（{split} 档）",
        f"未判定 {undetermined} 条不进分母（没结论既不是一致也不是不一致）",
    ]
    if denominator == 0:
        return AssertReading(
            "B-5", CLAUSE_NAMES["B-5"], STATUS_UNTESTABLE,
            f"本批 {len(gold_rows)} 行里没有「两侧都有结论」的行——分母为 0",
            numerator=0, denominator=0,
            caveats=tuple(caveats + undetermined_reasons[:10]),
        )
    rate = disagree / denominator
    return AssertReading(
        "B-5", CLAUSE_NAMES["B-5"], STATUS_READOUT,
        f"结论错率 {rate:.1%}（{disagree} / {denominator}，"
        f"一致 {agree} 条，按 D-10 口径 C 的语义等价类）",
        numerator=disagree, denominator=denominator,
        caveats=tuple(caveats),
    )


# ── 装配 ──────────────────────────────────────────────────────────


def assert_run(
    archive: Mapping[str, Any],
    *,
    run_id: str = "",
    task_id: str = "",
    archive_name: str = "",
    gold_rows: Sequence[Mapping[str, Any]] = (),
    d3_pairs: Sequence[tuple[str, str]] = (),
    split: str = "calibration",
) -> RunAsserts:
    """跑一个 run 的五条批次级断言。

    Args:
        archive: P1 存档（``read_archive`` 出来的 dict）。
        run_id / task_id / archive_name: 标识（缺了从存档里取）。
        gold_rows: 这个 run 的黄金集行（B-5 的分母来源）。
        d3_pairs: ``(人工类别, 本批判定类别)`` 对子（B-3 的分母来源）。
        split: ``gold_rows`` 来自哪一档（记进读数的口径里）。

    Returns:
        :class:`RunAsserts`——**每 run 一份**的批次级断言表。
    """
    archive = archive or {}
    task = str(task_id or archive.get("task_id") or "")
    run = str(run_id or "")
    if not run and archive_name:
        from trajectory_pipeline.assembler.schema import run_id_of

        run = run_id_of(archive_name)
    readings = (
        _assert_b1(archive),
        _assert_b2(archive),
        _assert_b3(d3_pairs),
        _assert_b4(archive),
        _assert_b5(gold_rows, split=split),
    )
    return RunAsserts(
        run_id=run, task_id=task, archive=str(archive_name or ""),
        readings=readings,
    )


def assert_archive(
    path: str | pathlib.Path,
    *,
    gold_rows: Sequence[Mapping[str, Any]] = (),
    d3_pairs: Sequence[tuple[str, str]] = (),
    split: str = "calibration",
) -> RunAsserts:
    """读一份存档并跑它的批次级断言。"""
    from trajectory_pipeline.assembler.schema import read_archive, run_id_of

    p = pathlib.Path(path)
    archive = read_archive(p)
    return assert_run(
        archive,
        run_id=run_id_of(p.name),
        task_id=str(archive.get("task_id") or ""),
        archive_name=p.name,
        gold_rows=gold_rows,
        d3_pairs=d3_pairs,
        split=split,
    )


__all__ = [
    "ABNORMAL_BRANCHES",
    "CLAUSES",
    "CLAUSE_NAMES",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_READOUT",
    "STATUS_UNTESTABLE",
    "AssertReading",
    "RunAsserts",
    "assert_archive",
    "assert_run",
    "rows_for_run",
]
