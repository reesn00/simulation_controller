"""双跑对账（P5 的验收判据，设计方案 04 §6.1）——同一批任务在两个执行层上
各跑一遍，**逐条比**。

为什么要有这个东西
------------------
「camoufox 换上去对数据分布有没有影响」不能靠体感回答：两边产出的存档
**长得一模一样**（同一份 P1 结构），差异全在数值里——分支比例、正文长度、
元素条数。肉眼看两份 JSON 是看不出 54% 的落差的；而落差的后果是
**并池后的分布变了一个没人察觉的原因**。

所以判据写死在代码里：§6.1 的四条可机械判，两条必须人判（负样本成因、
动作可移植性）。**本模块只做前者，后者原样列给人看**——不投票、不合成
一个「总分」：分歧要暴露给人看，不是被一个加权数抹平。

口径
----
- ``--a`` 是**基线**（当前生产链 obscura），``--b`` 是**候选**（camoufox）。
  中位数差按 ``|a-b| / a`` 算——「相对基线变了多少」才是要回答的问题。
- 两侧必须是**同一批 task_id**。没配上的条目**逐条列出来**，不按并集悄悄比：
  少跑的那一边会把分母缩小，比例差看着反而更小。
- 两侧的 ``driver`` 字段不同是**预期的**（这正是双跑的意义），但它决定
  两批数据**不是同一口径**（P4：元素键名与词表都随面变）——所以本报告
  是「换层前后的分布差」，**不是**「两组同口径样本的质量对比」。
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from trajectory_pipeline.executor.archive import select_archives
from trajectory_pipeline.toolcatalog import surface_of_driver

#: 分支比例允许的**绝对**差（百分点）。§6.1
GATE_BRANCH_PP = 5.0
#: 正文长度 / 元素条数的中位数允许的**相对**差（相对 A 侧基线）。§6.1
GATE_MEDIAN_RATIO = 0.30

#: 与反爬/登录态有关的分支——拦截率由这几支 + ``search_blocked`` 组成。
#: ``login_wall_or_blocked`` 是 deprecated 的合并态，历史存档里还有，照算。
BLOCK_BRANCHES: frozenset[str] = frozenset(
    {"blocked", "login_required", "login_wall_or_blocked"}
)


# ── 读存档 ──────────────────────────────────────────────────────


def load_runs(path: Path) -> list[dict[str, Any]]:
    """读一个目录（或单个文件）里的 P1 存档。**只认存档形状**。

    目录走 :func:`~trajectory_pipeline.executor.archive.select_archives`——
    那里已经把「探针取证不是存档」「复核档取代原档」两条规则写死了，
    这里再抄一份就会漂（实测目录 9 个 json 只有 4 个是存档）。
    """
    root = Path(path)
    if root.is_file():
        return [json.loads(root.read_text(encoding="utf-8"))]
    out: list[dict[str, Any]] = []
    for archive_path, _reviewed in select_archives(root):
        out.append(json.loads(archive_path.read_text(encoding="utf-8")))
    return out


def _observations(record: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """一份存档里的**全部观察**（搜索页 + 顶层步骤 + 站点页 + 播放页）。

    顶层 ``steps`` 是搜索阶段、``visits[].steps`` 是站内阶段——两者**不重叠**
    （实测：顶层 1 条 + 每个 visit 各自 1 条，URL 不同）。
    """
    for key in ("search_observation",):
        obs = record.get(key)
        if isinstance(obs, Mapping):
            yield obs
    for step in record.get("steps") or ():
        obs = (step or {}).get("observation")
        if isinstance(obs, Mapping):
            yield obs
    for visit in record.get("visits") or ():
        visit = visit or {}
        for key in ("site_obs", "player_obs"):
            obs = visit.get(key)
            if isinstance(obs, Mapping):
                yield obs
        for step in visit.get("steps") or ():
            obs = (step or {}).get("observation")
            if isinstance(obs, Mapping):
                yield obs


def _body_len(obs: Mapping[str, Any]) -> int:
    """正文长度。``body_len`` 优先——它是落盘时算的，与 ``body_text`` 同源；
    老存档缺这个键才退回现数长度（**不假设两代一致**）。"""
    if isinstance(obs.get("body_len"), int):
        return int(obs["body_len"])
    return len(str(obs.get("body_text") or ""))


def _elements(obs: Mapping[str, Any]) -> int:
    """交互元素条数。``elements_total`` 优先：那是**采到的**条数，
    ``interactive_elements`` 可能是被裁过的落盘条数（P1 契约里写明的那条）。"""
    if isinstance(obs.get("elements_total"), int):
        return int(obs["elements_total"])
    return len(obs.get("interactive_elements") or ())


# ── 单侧汇总 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SideSummary:
    """一侧（一个执行层 / 一个目录）的分布摘要。"""

    label: str
    source: str
    runs: int
    task_ids: tuple[str, ...]
    drivers: Mapping[str, int]
    surfaces: Mapping[str, int]
    observations: int
    branch_counts: Mapping[str, int]
    body_len_median: float | None
    elements_median: float | None
    blocked_outcomes: int
    search_blocked_runs: int
    negatives: tuple[Mapping[str, Any], ...]

    @property
    def outcomes_total(self) -> int:
        return sum(self.branch_counts.values())

    @property
    def branch_shares(self) -> dict[str, float]:
        """分支占比（%）。分母是**出过分支的站点数**，不是 run 数。"""
        total = self.outcomes_total
        if not total:
            return {}
        return {b: c / total * 100.0 for b, c in self.branch_counts.items()}

    @property
    def block_rate(self) -> float | None:
        """反爬拦截率（%）：被拦站点 / 全部站点。

        ⚠️ 这只是**一个读数**，不是「采集体感」：真·拦截率要人看被拦的是
        哪一类站点。§6.1 明确它**不构成否决**（除非显著变高）。
        """
        if not self.outcomes_total:
            return None
        return self.blocked_outcomes / self.outcomes_total * 100.0

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "source": self.source,
            "runs": self.runs,
            "task_ids": list(self.task_ids),
            "drivers": dict(self.drivers),
            "surfaces": dict(self.surfaces),
            "observations": self.observations,
            "branch_counts": dict(self.branch_counts),
            "branch_shares": {k: round(v, 2) for k, v in self.branch_shares.items()},
            "outcomes_total": self.outcomes_total,
            "body_len_median": self.body_len_median,
            "elements_median": self.elements_median,
            "block_rate_pct": None if self.block_rate is None else round(self.block_rate, 2),
            "search_blocked_runs": self.search_blocked_runs,
            "negatives": [dict(n) for n in self.negatives],
        }


def _surface_of(driver: str) -> str:
    """驱动名 → 面。认不出的**不猜**：记 ``unknown`` 并在报告里点名。

    这里与 ``surface_of_driver`` 的「抛」口径不同，因为对账工具是**只读**
    的事后审计：一份认不出的 driver 不该让整份报告出不来，而该让它显形。
    """
    try:
        return surface_of_driver(driver)
    except ValueError:
        return "unknown"


def summarize(label: str, path: Path) -> SideSummary:
    """把一侧的存档汇总成 :class:`SideSummary`。"""
    records = load_runs(path)
    branch_counts: dict[str, int] = {}
    drivers: dict[str, int] = {}
    surfaces: dict[str, int] = {}
    body_lens: list[int] = []
    elements: list[int] = []
    observations = 0
    blocked = 0
    search_blocked_runs = 0
    negatives: list[Mapping[str, Any]] = []
    task_ids: list[str] = []

    for record in records:
        task_id = str(record.get("task_id") or "")
        if task_id:
            task_ids.append(task_id)
        driver = str(record.get("driver") or "")
        drivers[driver or "(未记)"] = drivers.get(driver or "(未记)", 0) + 1
        surface = _surface_of(driver)
        surfaces[surface] = surfaces.get(surface, 0) + 1
        if record.get("search_blocked"):
            search_blocked_runs += 1

        ledger = record.get("ledger") or {}
        for branch, count in (ledger.get("by_branch") or {}).items():
            branch_counts[branch] = branch_counts.get(branch, 0) + int(count)
            if branch in BLOCK_BRANCHES:
                blocked += int(count)

        for obs in _observations(record):
            observations += 1
            body_lens.append(_body_len(obs))
            elements.append(_elements(obs))

        for outcome in record.get("outcomes") or ():
            if not isinstance(outcome, Mapping):
                continue
            if not outcome.get("is_negative_sample"):
                continue
            negatives.append({
                "task_id": task_id,
                "url": outcome.get("url") or outcome.get("site_url") or "",
                "branch": outcome.get("branch") or "",
                "evidence": str(outcome.get("evidence") or "")[:300],
            })

    return SideSummary(
        label=label,
        source=str(path),
        runs=len(records),
        task_ids=tuple(sorted(task_ids)),
        drivers=drivers,
        surfaces=surfaces,
        observations=observations,
        branch_counts=branch_counts,
        body_len_median=statistics.median(body_lens) if body_lens else None,
        elements_median=statistics.median(elements) if elements else None,
        blocked_outcomes=blocked,
        search_blocked_runs=search_blocked_runs,
        negatives=tuple(negatives),
    )


# ── 判据 ────────────────────────────────────────────────────────


def _ratio_diff(base: float | None, cand: float | None) -> float | None:
    """``|cand-base| / base``。基线为 0/None 时**返回 None（不可判）**，
    不返回 0——那会把「没法判」写成「一模一样」。"""
    if not base:
        return None
    return abs((cand or 0.0) - base) / base


def compare(a: SideSummary, b: SideSummary) -> dict[str, Any]:
    """逐条判 §6.1 的四条机械判据，并把两条人判项原样列出来。

    返回 JSON 可序列化的报告。**不合成总分**——``verdict`` 只有
    ``pass`` / ``fail`` / ``incomplete``（有判不了的条目）三种，
    且每条判据的读数都留在报告里，人可以逐条推翻。
    """
    paired = sorted(set(a.task_ids) & set(b.task_ids))
    only_a = sorted(set(a.task_ids) - set(b.task_ids))
    only_b = sorted(set(b.task_ids) - set(a.task_ids))

    criteria: list[dict[str, Any]] = []

    # ① 分支分布：逐分支的占比差（百分点），超出 5pp 的逐条点名
    shares_a, shares_b = a.branch_shares, b.branch_shares
    branch_diff: dict[str, dict[str, float]] = {}
    offenders: list[str] = []
    for branch in sorted(set(shares_a) | set(shares_b)):
        sa = shares_a.get(branch, 0.0)
        sb = shares_b.get(branch, 0.0)
        branch_diff[branch] = {"a_pct": round(sa, 2), "b_pct": round(sb, 2),
                               "diff_pp": round(sb - sa, 2)}
        if abs(sb - sa) > GATE_BRANCH_PP:
            offenders.append(branch)
    criteria.append({
        "criterion": "分支分布",
        "gate": f"逐分支占比差 ≤ {GATE_BRANCH_PP}pp",
        "result": "pass" if not offenders else "fail",
        "detail": {"per_branch": branch_diff, "offenders": offenders},
    })

    # ②③ 中位数差
    for name, base, cand in (
        ("body_text 长度中位数", a.body_len_median, b.body_len_median),
        ("interactive_elements 条数中位数", a.elements_median, b.elements_median),
    ):
        ratio = _ratio_diff(base, cand)
        criteria.append({
            "criterion": name,
            "gate": f"相对基线差 ≤ {GATE_MEDIAN_RATIO:.0%}",
            "result": ("incomplete" if ratio is None
                       else "pass" if ratio <= GATE_MEDIAN_RATIO else "fail"),
            "detail": {"a": base, "b": cand,
                       "ratio": None if ratio is None else round(ratio, 4)},
        })

    # ④ 拦截率：**读数不是判据**（§6.1 改过口径）
    criteria.append({
        "criterion": "反爬拦截率（读数，不构成否决）",
        "gate": "仅当 b 显著高于 a 才是否决项（人判）",
        "result": "needs_manual",
        "detail": {"a_pct": a.to_json()["block_rate_pct"],
                   "b_pct": b.to_json()["block_rate_pct"]},
    })

    # ⑤ 负样本成因：必须逐条人读
    criteria.append({
        "criterion": "负样本成因",
        "gate": "逐条人工读（最贵的静默是「理由通顺的假负样本」）",
        "result": "needs_manual",
        "detail": {"a_negatives": len(a.negatives), "b_negatives": len(b.negatives)},
    })

    # ⑥ 动作可移植性：要脱离本项目在裸 camoufox 里重放，本工具不做
    criteria.append({
        "criterion": "动作可移植性",
        "gate": "P1 steps[] 的动作按 camoufox 原始调用重放，脱离本项目也能跑通",
        "result": "needs_manual",
        "detail": "本工具只读存档，不重放动作",
    })

    results = {c["result"] for c in criteria}
    if "fail" in results:
        verdict = "fail"
    elif "incomplete" in results:
        verdict = "incomplete"
    else:
        verdict = "pass"

    return {
        "verdict": verdict,
        "paired_task_ids": paired,
        "only_in_a": only_a,
        "only_in_b": only_b,
        "criteria": criteria,
        "a": a.to_json(),
        "b": b.to_json(),
        "note": ("本报告比的是「换层前后两次运行的分布差」，两批样本"
                 "**不是同一口径**（元素键名/词表随面变，见 P4）。"
                 "不要拿它当「两组同口径样本的质量对比」。"),
    }


# ── 报告 ────────────────────────────────────────────────────────


def format_report(report: Mapping[str, Any]) -> str:
    """人能读的版本。**表里每个数字都要能在 JSON 里找到同一个**。"""
    a, b = report["a"], report["b"]
    lines: list[str] = []
    lines.append(f"双跑对账：{a['label']}（A/基线） vs {b['label']}（B/候选）")
    lines.append("")
    lines.append(f"  A {a['label']}  {a['runs']} 份存档 · {a['observations']} 个观察"
                 f" · 驱动 {a['drivers']} · 面 {a['surfaces']}")
    lines.append(f"  B {b['label']}  {b['runs']} 份存档 · {b['observations']} 个观察"
                 f" · 驱动 {b['drivers']} · 面 {b['surfaces']}")
    lines.append("")
    if report["only_in_a"] or report["only_in_b"]:
        lines.append("⚠️ 两侧任务集合不同——比例差的分母因此不同义：")
        if report["only_in_a"]:
            lines.append(f"   只在 A：{', '.join(report['only_in_a'])}")
        if report["only_in_b"]:
            lines.append(f"   只在 B：{', '.join(report['only_in_b'])}")
        lines.append("")
    lines.append(f"判定：{report['verdict']}")
    lines.append("")
    lines.append("判据：")
    for c in report["criteria"]:
        lines.append(f"  [{c['result']}] {c['criterion']} —— {c['gate']}")
        detail = c.get("detail")
        if isinstance(detail, str):
            lines.append(f"        {detail}")
            continue
        for key, value in (detail or {}).items():
            if isinstance(value, dict):
                continue
            lines.append(f"        {key} = {value}")
    lines.append("")
    for side in (a, b):
        lines.append(f"{side['label']} 分支分布：")
        for branch, pct in sorted(side["branch_shares"].items(),
                                  key=lambda kv: -kv[1]):
            lines.append(f"    {branch:<28} {side['branch_counts'][branch]:>4}  {pct:>6.1f}%")
        lines.append("")
    lines.append("负样本（逐条读，不要只看条数）：")
    for side in (a, b):
        lines.append(f"  [{side['label']}] {len(side['negatives'])} 条")
        for n in side["negatives"][:20]:
            lines.append(f"    - {n['task_id']} {n['branch']} {n['url']}")
            lines.append(f"      {n['evidence'][:160]}")
        if len(side["negatives"]) > 20:
            lines.append(f"    …另 {len(side['negatives']) - 20} 条（见 --json）")
    lines.append("")
    lines.append(report["note"])
    return "\n".join(lines)