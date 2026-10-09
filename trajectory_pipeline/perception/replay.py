"""感知层回放——在**存档里的真实观察**上跑感知实现，不开浏览器。

为什么需要它
------------
W3 落地后有两件事一直没法验证：真实端点跑得通吗、rule 与 llm 在真实页面上
差多少。而这两件事**都可以先不开浏览器**就做一半：

P1 存档里存的是 ``_obs_json`` 落下来的**结构化观察原样**——``video_tag_count`` /
``iframe_count`` / ``interactive_elements`` / 正文 / ``degraded`` 全在，
那正是 :class:`~trajectory_pipeline.perception.base.Observation` 的字段。
把两个实现喂同一份观察，就得到它们在同一批真实页面上的分歧。

它验什么、不验什么
------------------
**验**：感知层在真实数据上的判得出来率、两版答案差在哪、LLM 版是不是真的
比规则版多答出几道。

**不验**：ref 点击、播放器探测、跳转、反爬、加载时序——回放里浏览器全程没启动。
**所以它不能替代真实批次**，只把「感知层选得对不对」这一层提前摆到人眼前。

⚠️ **只报分歧，不报准确率**
-------------------------
拿 rule 的答案当真值算分歧率会得到一个漂亮的假数字：实测 12 份真实存档里
rule 判成功的 3 条**有 2 条是假阳性**（纯导航站 hao123 首页 iframe=15、
youku iframe=1 —— 早于「iframe 不作判据」的修正），拿一个 1/3 正确的基线
去算另一个实现有多准，结论没有意义。

所以本模块把「谁对」留给人：**只输出分歧发生在哪里、两边各自说了什么**，
判断交人。真要算准确率，先做人工标注集——那也是 ``CONFIDENCE_JUDGED``
校准的前置条件，两件事可以一起做。

不 import executor
------------------
本模块读 P1 是**把存档当数据文件读**（``json.load``），不 import 任何
executor 符号。理由与 ``assembler`` 那侧同源：档选哪几个由调用方决定，
``cli replay`` 用 ``archive.select_archives`` 挑好再传路径进来。
这样 ``perception`` 仍是「只吃 Observation 值对象」的插件包。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from trajectory_pipeline.perception.base import (
    Decision, InteractiveElement, LinkItem, Observation, Perceptor, Q,
)

#: P1 里的正文键。全文落盘后是 ``body_text``；早期存档只有 400 字符的
#: ``body_preview``——**两种都要认**，否则回放会把老存档整批判成空页面，
#: 而「空页面」在 LLM 版那里会被讲成一条通顺的错结论（见 §5.5）。
_BODY_KEY = "body_text"
_LEGACY_BODY_KEY = "body_preview"


class Verdict(str, Enum):
    """两版之间的分歧形态。**不表示谁对**——那要人看 evidence。"""

    AGREE = "agree"
    DIVERGE = "diverge"          # 都答了，答案不同
    ONE_SILENT = "one_silent"    # 一个答了、一个 fail-closed（None）
    BOTH_SILENT = "both_silent"  # 两个都判不出来 —— W1 缺口的直接量化


@dataclass(frozen=True, slots=True)
class Case:
    """一道判断题在一个观察上的回放单元。

    ``recorded_*`` 是存档里**真正问过并记了档**的那条结论。存档每个访问点
    只记最终 outcome 一个问题，所以其余题这里 ``recorded_question`` 是空的——
    **没有记录不等于没问过**，只是无从对照。
    """

    archive: str
    task_id: str
    title: str
    unit: str                       # "search" | "site-1" …
    question: str
    obs: Observation
    body_degraded: bool = False     # 正文只有预览——LLM 版在这上面不可信
    recorded_question: str = ""
    recorded_branch: str | None = None
    recorded_source: str = ""


@dataclass(frozen=True, slots=True)
class Comparison:
    case: Case
    decisions: tuple[tuple[str, Decision], ...]

    @property
    def verdict(self) -> Verdict:
        answers = [d.answer for _name, d in self.decisions]
        known = [a for a in answers if a is not None]
        if not known:
            return Verdict.BOTH_SILENT
        if len(known) < len(answers):
            return Verdict.ONE_SILENT
        return Verdict.AGREE if len(set(known)) == 1 else Verdict.DIVERGE

    def answer_of(self, name: str) -> Decision | None:
        for n, d in self.decisions:
            if n == name:
                return d
        return None

    def label(self) -> str:
        """一行人读的标签。``None`` 显式渲染成 ``None``，不省略——
        「没答」和「答了 False」混成空白正是 fail-closed 最常见的误读。"""
        return "  ".join(
            f"{n}={'None' if d.answer is None else d.answer}" for n, d in self.decisions
        )


# ── 存档 → Observation ────────────────────────────────────────────

def observation_of(obs_json: Mapping[str, Any]) -> Observation:
    """P1 里的观察字典 → :class:`Observation`。

    按**存档的字段名**读，不按 ``Observation`` 的构造顺序猜；缺的字段一律
    取默认值而不是抛错——回放的职责是「用存档里有的东西跑一遍」，
    存档残缺是 :mod:`trajectory_pipeline.executor.integrity` 的活，
    两者叠在一起会让一份老存档直接跑不动。
    """
    body = str(obs_json.get(_BODY_KEY) or obs_json.get(_LEGACY_BODY_KEY) or "")
    return Observation(
        url=str(obs_json.get("url") or ""),
        page_title=str(obs_json.get("page_title") or ""),
        body_text=body,
        interactive_elements=tuple(
            InteractiveElement(str(e.get("ref") or ""), str(e.get("tag") or ""),
                               str(e.get("label") or ""))
            for e in obs_json.get("interactive_elements") or ()
            if isinstance(e, Mapping)
        ),
        links=tuple(
            LinkItem(str(l.get("text") or ""), str(l.get("href") or ""))
            for l in obs_json.get("links") or ()
            if isinstance(l, Mapping)
        ),
        video_tag_count=int(obs_json.get("video_tag_count") or 0),
        iframe_count=int(obs_json.get("iframe_count") or 0),
        truncated=bool(obs_json.get("truncated")),
        raw_len=int(obs_json.get("raw_len") or 0),
        stripped_ratio=float(obs_json.get("stripped_ratio") or 1.0),
        body_source=str(obs_json.get("body_source") or "inner_text"),
        degraded=tuple(str(d) for d in obs_json.get("degraded") or ()),
    )


def _has_full_body(obs_json: Mapping[str, Any]) -> bool:
    return bool(obs_json.get(_BODY_KEY))


# ── 存档 → 回放单元 ───────────────────────────────────────────────

def cases_of(archive_path: Path | str) -> list[Case]:
    """把一份 P1 存档摊成一组回放单元。

    题目到观察的对应关系按控制流走（``orchestrator`` 环节 ①②③④）：
    搜索观察问 ①、站点观察问 ②③、播放页观察问 ④。

    **被反爬拦的 run 产出零条**——那一步的观察是验证码页，拿它问 LLM
    会得到一条关于验证码页的「模型该怎么做」的答案，看着通顺、毫无价值。
    与 :func:`assembler.schema.split_archive` 同一处置。
    """
    p = Path(archive_path)
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("search_blocked"):
        return []

    name = p.name
    task_id = str(doc.get("task_id") or "")
    title = str(doc.get("title") or "")
    outcomes = list(doc.get("outcomes") or ())
    out: list[Case] = []

    def _outcome_for(*urls: str) -> Mapping[str, Any] | None:
        want = {u for u in urls if u}
        for o in outcomes:
            if str(o.get("url") or "") in want:
                return o
        return None

    search_obs = doc.get("search_observation")
    if isinstance(search_obs, Mapping) and search_obs:
        out.append(Case(
            archive=name, task_id=task_id, title=title, unit="search",
            question=Q.SELECT_PLAY_SITES,
            obs=observation_of(search_obs),
            body_degraded=not _has_full_body(search_obs),
        ))

    for i, visit in enumerate(doc.get("visits") or (), 1):
        recorded = _outcome_for(str(visit.get("url") or ""),
                                str(visit.get("landed_url") or ""))
        rec_q = str((recorded or {}).get("question") or "")
        rec_branch = (recorded or {}).get("branch")
        site_obs = visit.get("site_obs")
        if isinstance(site_obs, Mapping) and site_obs:
            for q in (Q.IS_REACHABLE, Q.FIND_PLAY_CONTROL):
                out.append(Case(
                    archive=name, task_id=task_id, title=title, unit=f"site-{i}",
                    question=q, obs=observation_of(site_obs),
                    body_degraded=not _has_full_body(site_obs),
                    recorded_question=rec_q if rec_q == q else "",
                    recorded_branch=rec_branch if rec_q == q else None,
                    recorded_source=str((recorded or {}).get("source") or ""),
                ))
        player_obs = visit.get("player_obs")
        if isinstance(player_obs, Mapping) and player_obs:
            out.append(Case(
                archive=name, task_id=task_id, title=title, unit=f"site-{i}",
                question=Q.PLAYER_OK, obs=observation_of(player_obs),
                body_degraded=not _has_full_body(player_obs),
                recorded_question=rec_q if rec_q == Q.PLAYER_OK else "",
                recorded_branch=rec_branch if rec_q == Q.PLAYER_OK else None,
                recorded_source=str((recorded or {}).get("source") or ""),
            ))
    return out


# ── 回放 ──────────────────────────────────────────────────────────

def replay_archive(
    archive_path: Path | str,
    perceivers: Mapping[str, Perceptor],
) -> list[Comparison]:
    """在这一份存档上跑所有实现。

    ``perceivers`` 是 ``{实现名: Perceptor}``。**只放一个实现也合法**——
    规则版不需要任何 LLM 后端，于是「W1 在真实数据上答得出来几道」这件事
    完全离线可测，而这正是现在最该先知道的数。
    """
    if not perceivers:
        raise ValueError("perceivers 为空，回放无从谈起")
    out: list[Comparison] = []
    for case in cases_of(archive_path):
        # **不传 title**：片名在**构造期**绑进感知器（``build_perceptor(
        # target_title=…)``），与 orchestrator 同一条路。``decide`` 的签名
        # 只有 ``(question, obs)``——判断点 ① 要的片名属于**任务**，
        # 不是观察的属性。
        #
        # 因此**一份存档一个感知器**：跨任务复用同一个实例，① 就会拿上一批的
        # 片名去选站，不报错、只是选错。调用方（cli）逐档构造正是为此。
        decisions = tuple(
            (name, p.decide(case.question, case.obs))
            for name, p in perceivers.items()
        )
        out.append(Comparison(case=case, decisions=decisions))
    return out


@dataclass(frozen=True, slots=True)
class Summary:
    """跨存档汇总。**只统计形态，不统计对错**——见模块 docstring。"""

    total: int = 0
    by_verdict: Mapping[str, int] = None          # type: ignore[assignment]
    by_question: Mapping[str, Mapping[str, int]] = None   # type: ignore[assignment]
    answered: Mapping[str, int] = None            # type: ignore[assignment]
    degraded: int = 0

    def __post_init__(self) -> None:
        for name in ("by_verdict", "by_question", "answered"):
            if getattr(self, name) is None:
                object.__setattr__(self, name, {})


def summarize(comparisons: Sequence[Comparison]) -> Summary:
    by_verdict: dict[str, int] = {}
    by_question: dict[str, dict[str, int]] = {}
    answered: dict[str, int] = {}
    degraded = 0
    for c in comparisons:
        v = c.verdict.value
        by_verdict[v] = by_verdict.get(v, 0) + 1
        q = by_question.setdefault(c.case.question, {})
        q[v] = q.get(v, 0) + 1
        if c.case.body_degraded:
            degraded += 1
        for name, d in c.decisions:
            if d.answer is not None:
                answered[name] = answered.get(name, 0) + 1
    return Summary(len(comparisons), by_verdict, by_question, answered, degraded)


_VERDICT_LABEL = {
    Verdict.AGREE.value: "一致",
    Verdict.DIVERGE.value: "分歧（都答了，答案不同）",
    Verdict.ONE_SILENT.value: "一方 fail-closed",
    Verdict.BOTH_SILENT.value: "两方都判不出来",
}


def format_summary(summary: Summary, names: Sequence[str]) -> str:
    lines = [f"回放 {summary.total} 道判断题（正文降级 {summary.degraded} 道）"]
    if not summary.total:
        lines.append("  没有可回放的单元——存档里既无搜索观察也无访问记录，"
                     "或整批都被反爬拦了")
        return "\n".join(lines)

    lines.append("\n答得出来的比例（None = fail-closed，**不代表题出错了**）：")
    for n in names:
        got = summary.answered.get(n, 0)
        pct = 100.0 * got / summary.total
        lines.append(f"  {n:>6}  {got:>4} / {summary.total}  ({pct:5.1f}%)")

    lines.append("\n按题：")
    for q, counts in sorted(summary.by_question.items()):
        parts = "  ".join(f"{_VERDICT_LABEL[k]} {v}" for k, v in sorted(counts.items()))
        lines.append(f"  {q:<24} {parts}")

    lines.append("\n⚠️ 这里没有准确率。要算准，得先有人工标注集——"
                 "rule 判成功的 3 条里 2 条是假阳性，拿它当真值算出来的数没有意义。")
    return "\n".join(lines)


def format_divergences(comparisons: Sequence[Comparison], limit: int = 20) -> str:
    """逐条列分歧。**带上 evidence 摘要**——不看证据没法判断谁对。"""
    picked = [c for c in comparisons if c.verdict is not Verdict.AGREE]
    if not picked:
        return "\n没有分歧。"
    lines = [f"分歧 / 沉默 {len(picked)} 条（前 {min(limit, len(picked))} 条）："]
    for c in picked[:limit]:
        lines.append(f"\n  [{c.case.archive} {c.case.unit} {c.case.question}] {c.label()}")
        if c.case.body_degraded:
            lines.append("      ⚠ 正文只有 400 字符预览——LLM 版在这上面不可信")
        if c.case.recorded_branch is not None or c.case.recorded_question:
            lines.append(f"      存档记录的结论：{c.case.recorded_question} "
                         f"→ {c.case.recorded_branch!r}（{c.case.recorded_source}）")
        for name, d in c.decisions:
            ev = (d.evidence or "").replace("\n", " ")[:110]
            lines.append(f"      {name} evidence: {ev}")
    return "\n".join(lines)


__all__ = [
    "Verdict", "Case", "Comparison", "Summary",
    "observation_of", "cases_of", "replay_archive", "summarize",
    "format_summary", "format_divergences",
]