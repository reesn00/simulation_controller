"""触发读数 T-1～T-4（方案 16 §5.4 / §9.3 C-16）。

一句话：**先出读数，读数越线才值得开一张卡**——把「人翻一遍差异然后决定改哪」
换成一条命令能查的口径。

四条纪律，代码里逐条对上
----------------------
1. **读数先于阈值**（D-C9）。新线只出读数、不出「合格 / 不合格」。所以本模块
   :data:`SIGNALS` 里每条信号都带一个 ``line_set``：线**没定**的信号即使读数
   过线也只标 ``over_line=None``，报告里 :data:`judged` **恒为 ``None``**。
   命令任何时候都不打印「合格」——没有线就没有合格可言，打印出来就是编的。
2. **触发 ≠ 归因**。越线只说明「该看一眼」，改哪一侧仍由六桶（§4）判。所以本
   模块**不产出桶**，也不碰 ``triage``。
3. **测不成就说测不成**。每条信号、每个条款都带 ``measurable`` + ``reason``；
   一条都测不出来时命令退 **2**（不是 0）。``0 条样本 ≠ 一切正常``。
4. **不造指标**（§5.4 的三方对比表）。「代码评估与 LLM 语义评估的分歧率」
   **只能在 D-4 上定义**（唯一双评分者条款）；D-3 / D-9 的 judge 逐行 pred
   不落黄金集行 ⇒ T-2 / T-3 在拿到 :func:`load_judge_pairs` 的输入之前
   **恒为测不成**，不给代理数、不拿 code 侧顶替。

今天（v7）能测出什么、测不出什么
--------------------------------
快照 856 条里**软维度（d2 / d4 / d5 / d6）全部 ``ungraded``**——黄金集还没填
软维度金标。所以：

- **T-1 只有 D-10 可测**（头条档 n=128，eq_class_mismatch 33 ⇒ 25.8%）；
  其余条款 ``n=0``，逐条写明「软维度金标 0 条」；
- **T-2 / T-3 测不成**（两条独立理由，见 :func:`_judge_gap`）；
- **T-4 只有 D-10 可测**（pred 众数 ``unresolved`` 37.5% / 13 个取值 ⇒ 未退化）。

⚠️ 「只有 D-10 可测」这件事本身就是读数：它把「先填软维度金标」从 M-18/L-04
的散活变成了**回路的硬前置**——软维度不填金标，T-2 的上岗线永远无从核对。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from trajectory_pipeline.evaluation.calibration.snapshot import (
    HARD_DIM,
    VERDICT_DISAGREE,
    VERDICT_UNGRADED,
    load_snapshot,
)
from trajectory_pipeline.evaluation.golden.builder import (
    PIPELINE_ROOT,
    cohen_kappa,
    linear_weighted_kappa,
)
from trajectory_pipeline.evaluation.golden.splits import HEADLINE_SPLIT
from trajectory_pipeline.evaluation.scorers import thresholds

REPORT_FILE = "triggers.json"

#: 派生数据的缺省根。**锚在 `trajectory_pipeline/output/pipeline/`（不跟 cwd 走）**——
#: 与 ``agreement --emit-disagreements`` / ``worklist build`` 的缺省同一个，
#: 所以 ``v{N}/`` 下五种产物（snapshot / disagreements / worklist / worksheet /
#: triggers / sample）天然挨在一起。
#:
#: ⚠️ 这里**不复用** ``worklist.DEFAULT_CALIBRATION_ROOT``（值相同，两处都有）：
#: 读数与抽样是**纯派生**的，不该因为分诊工作表那侧在动就跟着断。真源是
#: :data:`PIPELINE_ROOT`，本常量与 ``worklist`` 的那一份都由它派生——
#: 单测 ``test_派生根锚在pipeline输出下`` 钉住这个不变量。
DEFAULT_CALIBRATION_ROOT = PIPELINE_ROOT / "calibration"


class TriggerError(Exception):
    """读数的输入不成立（→ 退 2）。**不是驳回**：判据没跑起来。"""


# ---------------------------------------------------------------------------
# 信号与阈值（D-C9：新线只出读数）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalSpec:
    """一条触发信号。``line_set=False`` ⇒ :attr:`line` 只是**候选线**，不判。"""

    id: str
    name: str
    question: str
    line: str
    line_set: bool


SIGNALS: tuple[SignalSpec, ...] = (
    SignalSpec(
        id="T-1",
        name="两评分者分歧率（条款级）",
        question="同一条款上两侧判成不同等价类的比例",
        line=f"单条款 eq_class_mismatch ≥ {thresholds.value('t1_eq_class_rate'):.0%} 且 n ≥ {thresholds.value('t1_min_n')}（暂定，未定线）",
        line_set=False,
    ),
    SignalSpec(
        id="T-2",
        name="judge ↔ 人工一致性",
        question="LLM judge 与人工金标的一致程度（硬 Cohen / 软线性加权）",
        # 这条**沿用已有上岗线**（D-C9：已有线沿用，新线只出读数）。
        line=(
            f"上岗线沿用：硬 kappa ≥{thresholds.value('t2_hard_kappa'):.2f} / 软加权 ≥{thresholds.value('t2_soft_kappa'):.2f}；"
            f"下降 = Δ ≤ −{abs(thresholds.value('t2_drop_delta')):.2f}"
        ),
        line_set=True,
    ),
    SignalSpec(
        id="T-3",
        name="理由含糊率（按条款）",
        question="判分理由里不含观察原文片段的比例",
        line=f"单条款 ≥ {thresholds.value('t3_vague_rate'):.0%}（未定线）",
        line_set=False,
    ),
    SignalSpec(
        id="T-4",
        name="评分分布退化（按条款）",
        question="取值分布是否退化成单一值（条款区分不开）",
        line=(
            f"众数占比 ≥ {thresholds.value('t4_mode_share'):.0%} 或不同取值 ≤ {thresholds.value('t4_distinct_values')}"
            "（未定线）"
        ),
        line_set=False,
    ),
)

SIGNAL_IDS: tuple[str, ...] = tuple(s.id for s in SIGNALS)


def spec_of(signal_id: str) -> SignalSpec:
    for spec in SIGNALS:
        if spec.id == signal_id:
            return spec
    raise KeyError(f"{signal_id!r} 不是触发信号（{', '.join(SIGNAL_IDS)}）")


#: 判成两个值就算「这条款区分得开」。两个来自候选线的口径集中在这里，
#: 换口径 = 改这两行 + 重跑（抽样/读数都不写时间戳，diff 能看出来）。
#:
#: ⚠️ **数值来自阈值表**（`evaluation/scorers/thresholds.py`，owner 2026-10-23
#: 裁定 D-C6：阈值调整**不算** rubric 级变更 ⇒ 不升 rubric 版本、不进 rubric
#: 变更记录、不设签名门；但阈值有自己的唯一真源，不许各写一份字面量）。
MODE_SHARE_LINE = thresholds.value("t4_mode_share")
DISTINCT_VALUES_LINE = thresholds.value("t4_distinct_values")

#: T-1 候选线。``line_set=False`` ⇒ 这两个数**只用于标注「按候选线会越线」**，
#: 不进任何判定——候选线不是线。
T1_RATE_LINE = thresholds.value("t1_eq_class_rate")
T1_MIN_N = thresholds.value("t1_min_n")

#: T-2 的**已有**上岗线（沿用，不是新定的）。``line_set=True`` 是四条里唯一
#: 一条：所以它是唯一一条允许报「低于线」的，但仍不报「合格」。
T2_HARD_LINE = thresholds.value("t2_hard_kappa")
T2_SOFT_LINE = thresholds.value("t2_soft_kappa")


# ---------------------------------------------------------------------------
# 快照聚合：条款级分母（``ungraded`` 不进分母——None ≠ 0）
# ---------------------------------------------------------------------------


def _split_of(entry: Mapping[str, Any]) -> str:
    return str(entry.get("split") or "")


def clause_stats(
    snapshot: Mapping[str, Any], *, split: str | None = HEADLINE_SPLIT
) -> dict[str, dict[str, Any]]:
    """条款 → 该条款上的条目计数（``ungraded`` 单独记，**不进分母**）。

    键是条目自己的 ``rubric_ref``。**条款号为空照样开一组**（键 ``""``）：判不
    出条款号的条目不是豁免项，它照样参与 T-1 / T-4——把它单列出去反而会让人
    以为「那批不算」。

    ``ungraded`` 计数**必须留着**：只报 n>0 的条款却不说明另外几条条款「一条都
    判不了」，报告就会读成「全部条款都测过了」——这正是 ``0 条样本 ≠ 一切正常``
    要防的那类误读。所以 :func:`t1_disagreement_rate` / :func:`t4_distribution`
    跳过 n==0 的条款，而 :func:`_coverage_note` 负责把漏掉的条款喊出来。
    """
    out: dict[str, dict[str, Any]] = {}
    for entry in snapshot.get("entries", []):
        if split is not None and _split_of(entry) != split:
            continue
        clause = str(entry.get("rubric_ref") or "")
        bucket = out.setdefault(
            clause,
            {
                "n": 0,
                "ungraded": 0,
                "eq_class_mismatch": 0,
                "agree": 0,
                "detail_mismatch": 0,
                "tier_gap": 0,
                "disagree": 0,
                "pred_values": {},
                "gold_values": {},
            },
        )
        verdict = str(entry.get("verdict") or "")
        if verdict == VERDICT_UNGRADED:
            bucket["ungraded"] += 1
            continue
        bucket["n"] += 1
        bucket[verdict] = bucket.get(verdict, 0) + 1
        if verdict == VERDICT_DISAGREE:
            bucket["eq_class_mismatch"] += 1
        for side, key in (("pred", "pred_values"), ("gold", "gold_values")):
            value = entry.get(f"{side}_value")
            if value is None:
                continue
            table: dict[str, int] = bucket[key]
            label = str(value)
            table[label] = table.get(label, 0) + 1
    return out


def _mode(table: Mapping[str, int]) -> tuple[str, float]:
    if not table:
        return "", 0.0
    total = sum(table.values())
    label, count = max(sorted(table.items()), key=lambda kv: kv[1])
    return label, count / total


# ---------------------------------------------------------------------------
# T-1
# ---------------------------------------------------------------------------


def t1_disagreement_rate(
    stats: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """条款 → ``eq_class_mismatch / n``（**只对 n>0 的条款出数**）。

    ``n == 0`` 的条款**不出读数**——出 ``rate = 0`` 等于把「没数据」写成
    「零分歧」，而那正是 T-1 最该抓的东西（它会显得一切正常）。
    """
    readings: list[dict[str, Any]] = []
    for clause in sorted(stats):
        bucket = stats[clause]
        n = int(bucket.get("n") or 0)
        if n <= 0:
            continue
        mismatch = int(bucket.get("eq_class_mismatch") or 0)
        rate = mismatch / n
        readings.append(
            {
                "clause": clause,
                "n": n,
                "eq_class_mismatch": mismatch,
                "rate": round(rate, 4),
                # 「按候选线会不会越线」与「是否越线」是两件事——候选线未定，
                # 报告只报前者，判定留给 :attr:`SignalSpec.line_set`。
                "over_candidate_line": bool(rate >= T1_RATE_LINE and n >= T1_MIN_N),
                "candidate_line_not_met_n": n < T1_MIN_N,
            }
        )
    return readings


# ---------------------------------------------------------------------------
# T-2 / T-3 的输入：judge 逐行读数
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgePair:
    """一条 judge 逐行读数（**配对键必须自带**，不留「按 url 猜」的余地）。"""

    golden_id: str
    dimension: str
    score: Any
    reasons: tuple[str, ...]


def load_judge_pairs(path: pathlib.Path | str) -> list[JudgePair]:
    """读 judge 逐行读数（JSON / JSONL），按 ``(golden_id, dimension)`` 成对。

    ⚠️ **这份文件今天不存在**（``evaluation.cli --json`` 的行键是
    ``sample_id``，不是 ``golden_id``），所以 T-2 / T-3 默认是**测不成**。
    缺的键**直接抛**，不做「按 sample_id / url 猜一个键出来」——那会让
    kappa 建在一个配不上的分母上，而 kappa 对配对样本量极敏感
    （见 :func:`_judge_gap` 的两条理由）。
    """
    p = pathlib.Path(path)
    if not p.exists():
        raise TriggerError(f"judge 读数文件不存在：{p}")
    raw = p.read_text(encoding="utf-8")
    rows: list[Any]
    if p.suffix == ".jsonl":
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        data = json.loads(raw)
        rows = data.get("rows") if isinstance(data, Mapping) else data
        if not isinstance(rows, list):
            raise TriggerError(f"{p} 既不是 JSONL 也不是含 rows 的 JSON")

    pairs: list[JudgePair] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        golden_id = str(row.get("golden_id") or "")
        dimension = str(row.get("dimension") or "")
        if not golden_id or not dimension:
            raise TriggerError(
                f"{p} 里有行缺 golden_id / dimension："
                f"{sorted(row)[:8]}。与快照配对的键只能显式给——"
                "按 sample_id 或 url 猜会把配不上的行算进 kappa 分母"
            )
        reasons = row.get("reasons") or ()
        if isinstance(reasons, str):
            reasons = [reasons]
        pairs.append(
            JudgePair(
                golden_id=golden_id,
                dimension=dimension,
                score=row.get("score"),
                reasons=tuple(str(r) for r in reasons),
            )
        )
    return pairs


def _judge_gap(pairs: Sequence[JudgePair] | None) -> str:
    """为什么 T-2 / T-3 测不成（**两条独立理由**，缺一都不该改成"能测"）。"""
    if pairs is None:
        return (
            "judge 逐行 pred 不落盘：`evaluation.cli --include-llm-judge` 的行键是 "
            "sample_id，与快照的 golden_id 配不上（`--judge-report` 也没有可给的文件）"
        )
    if not pairs:
        return "judge 读数文件里 0 行"
    return ""


def _gold_pairs(
    snapshot: Mapping[str, Any], pairs: Sequence[JudgePair], *, split: str | None
) -> dict[str, list[tuple[Any, Any]]]:
    gold: dict[tuple[str, str], Any] = {}
    for entry in snapshot.get("entries", []):
        if split is not None and _split_of(entry) != split:
            continue
        gold[(str(entry.get("golden_id") or ""), str(entry.get("dimension") or ""))] = (
            entry.get("gold_value")
        )
    out: dict[str, list[tuple[Any, Any]]] = {}
    for pair in pairs:
        value = gold.get((pair.golden_id, pair.dimension))
        if value is None:
            continue
        out.setdefault(pair.dimension, []).append((str(pair.score), str(value)))
    return out


def t2_judge_human(
    snapshot: Mapping[str, Any],
    pairs: Sequence[JudgePair],
    *,
    split: str | None = HEADLINE_SPLIT,
) -> list[dict[str, Any]]:
    """条款 → judge ↔ 人工 kappa（硬用 Cohen、软用线性加权）。"""
    readings: list[dict[str, Any]] = []
    for dimension, values in sorted(_gold_pairs(snapshot, pairs, split=split).items()):
        if len(values) < 2:
            continue
        judge_side = [v[0] for v in values]
        gold_side = [v[1] for v in values]
        if dimension == HARD_DIM:
            kappa = cohen_kappa(judge_side, gold_side)
            kind = "cohen_kappa"
        else:
            try:
                kappa = linear_weighted_kappa(
                    [float(v) for v in judge_side],
                    [float(v) for v in gold_side],
                    1.0,
                    5.0,
                )
                kind = "linear_weighted_kappa"
            except ValueError:
                continue
        if kappa is None:
            continue
        readings.append(
            {"dimension": dimension, "n": len(values), "kind": kind, "kappa": kappa}
        )
    return readings


def t3_vague_rate(
    pairs: Sequence[JudgePair],
    *,
    observation_quote_markers: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """条款 → 理由含糊率（判分理由里**不含观察原文片段**的比例）。

    ⚠️ **「观察原文片段」在今天的产物里取不到**：理由是从评分器到
    ``pred_reasons`` 的散文，而观察存档不在快照里。所以这里能机械判的只有
    **「理由里有没有引到观察里出现的字符串」**——而引什么字符串要由人给
    (:paramref:`observation_quote_markers`)。不给 ⇒ 报**测不成**，不许拿
    「理由非空」当「理由有据」。
    """
    if not observation_quote_markers:
        return []
    readings: list[dict[str, Any]] = []
    by_dim: dict[str, list[JudgePair]] = {}
    for pair in pairs:
        by_dim.setdefault(pair.dimension, []).append(pair)
    for dimension, group in sorted(by_dim.items()):
        with_reason = [p for p in group if p.reasons]
        quoted = [
            p
            for p in with_reason
            if any(m in reason for m in observation_quote_markers for reason in p.reasons)
        ]
        readings.append(
            {
                "dimension": dimension,
                "n": len(group),
                "n_with_reasons": len(with_reason),
                "n_quoting_observation": len(quoted),
                "vague_rate": (
                    round((len(with_reason) - len(quoted)) / len(with_reason), 4)
                    if with_reason
                    else None
                ),
            }
        )
    return readings


# ---------------------------------------------------------------------------
# T-4
# ---------------------------------------------------------------------------


def t4_distribution(
    stats: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """条款 → 两侧取值分布 + 是否退化（众数占比 / 不同取值数）。"""
    readings: list[dict[str, Any]] = []
    for clause in sorted(stats):
        bucket = stats[clause]
        if int(bucket.get("n") or 0) <= 0:
            # n==0 的条款不出读数（出「无取值」等于把没数据写成「两侧一致为空」）。
            # 漏掉的条款由 :func:`_coverage_note` 单独喊。
            continue
        entry: dict[str, Any] = {"clause": clause, "n": int(bucket.get("n") or 0)}
        for side in ("pred", "gold"):
            table = dict(bucket.get(f"{side}_values") or {})
            label, share = _mode(table)
            entry[side] = {
                "n": sum(table.values()),
                "distinct": len(table),
                "mode": label,
                "mode_share": round(share, 4),
                "degenerate": bool(table) and (
                    share >= MODE_SHARE_LINE or len(table) <= DISTINCT_VALUES_LINE
                ),
            }
        readings.append(entry)
    return readings


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def _coverage_note(stats: Mapping[str, Mapping[str, Any]]) -> str:
    """把「一条都判不了」的条款喊出来——**即使别的条款测到了也要喊**。

    这是 ``0 条样本 ≠ 一切正常`` 在条款层的形态：v7 里 D-2 / D-4 / D-5 / D-6
    全部 ``ungraded``，只报 D-10 的话，报告会读成「五条条款都测过了」。
    """
    blind = [
        f"{clause or '(无条款号)'}({int(bucket.get('ungraded') or 0)} 条 ungraded)"
        for clause, bucket in sorted(stats.items())
        if int(bucket.get("n") or 0) == 0
    ]
    if not blind:
        return ""
    return (
        f"还有 {len(blind)} 个条款**一条都判不了**（`ungraded` 不进分母，"
        f"不是 0）：{'、'.join(blind)} —— 黄金集没填这些条款的金标，"
        "读数不覆盖它们"
    )


def _t2_below_line(readings: Sequence[Mapping[str, Any]]) -> bool | None:
    """T-2：任一维度低于已有上岗线 ⇒ ``True``；没有可比的就 ``None``。"""
    flags: list[bool] = []
    for reading in readings:
        line = T2_HARD_LINE if reading["kind"] == "cohen_kappa" else T2_SOFT_LINE
        flags.append(float(reading["kappa"]) < line)
    return any(flags) if flags else None


def evaluate(
    snapshot: Mapping[str, Any],
    *,
    judge_pairs: Sequence[JudgePair] | None = None,
    quote_markers: Sequence[str] = (),
    split: str | None = HEADLINE_SPLIT,
) -> dict[str, Any]:
    """四类信号的读数汇总。

    报告里 :data:`judged` **恒为 ``None``**：本命令不判合格/不合格（D-C9
    读数先行）。``measurable`` 是逐条信号给的——一条都测不出来时
    :func:`main` 退 2。
    """
    stats = clause_stats(snapshot, split=split)
    t1 = t1_disagreement_rate(stats)
    t4 = t4_distribution(stats)
    gap = _judge_gap(judge_pairs)
    t2 = t2_judge_human(snapshot, judge_pairs or (), split=split) if not gap else []
    t3 = t3_vague_rate(judge_pairs or (), observation_quote_markers=quote_markers)
    coverage_note = _coverage_note(stats)

    signals: list[dict[str, Any]] = []
    for spec in SIGNALS:
        readings: list[dict[str, Any]] = []
        reason = ""
        if spec.id == "T-1":
            readings = t1
            reason = "" if readings else coverage_note
        elif spec.id == "T-2":
            readings = t2
            reason = gap or ("" if readings else coverage_note)
        elif spec.id == "T-3":
            readings = t3
            reason = (
                gap
                or "「观察原文片段」的判据取不到：理由是散文、观察存档不在快照里，"
                "引什么字符串必须由人给（`--quote-marker`）"
            )
        else:
            readings = t4
            reason = "" if readings else coverage_note
        signals.append(
            {
                "id": spec.id,
                "name": spec.name,
                "question": spec.question,
                "line": spec.line,
                "line_set": spec.line_set,
                "measurable": bool(readings),
                "reason": reason,
                "readings": readings,
                # 线没定 ⇒ 连「低于线」都不判（None ≠ False）；线已定的 T-2
                # 才给读数级的标注，且只标「低于」，不标「合格」。
                "below_line": (
                    _t2_below_line(readings) if spec.line_set and readings else None
                ),
                # D-C9：只出读数，**恒不判合格**（见模块 docstring 第 1 条）。
                "judged": None,
            }
        )
    return {
        "golden_version": (snapshot.get("meta") or {}).get("golden_version"),
        "rubric_version": (snapshot.get("meta") or {}).get("rubric_version"),
        "evaluator_version": (snapshot.get("meta") or {}).get("evaluator_version"),
        "split": split,
        "judge_pairs": 0 if judge_pairs is None else len(judge_pairs),
        "coverage": {
            "measured": sorted(c for c, b in stats.items() if int(b.get("n") or 0) > 0),
            "blind": sorted(
                c for c, b in stats.items() if int(b.get("n") or 0) == 0
            ),
            "note": coverage_note,
        },
        "signals": signals,
        "measurable": any(s["measurable"] for s in signals),
    }


def render(report: Mapping[str, Any]) -> str:
    lines = [
        f"触发读数 T-1～T-4 · 黄金集 v{report.get('golden_version')} "
        f"· rubric {report.get('rubric_version')} · 档 {report.get('split')}",
        "**本命令不判合格 / 不合格**（D-C9 读数先行）：线没定的信号连「越线」都不报",
        "",
    ]
    coverage = report.get("coverage") or {}
    if coverage.get("note"):
        lines.append(f"⚠ {coverage['note']}")
        lines.append("")
    for signal in report["signals"]:
        mark = "✓" if signal["measurable"] else "➖"
        lines.append(f"{mark} {signal['id']} {signal['name']}")
        lines.append(f"    问：{signal['question']}")
        lines.append(
            f"    线：{signal['line']}"
            f"{'' if signal['line_set'] else '  ← **未定线（只出读数）**'}"
        )
        if signal["below_line"] is True:
            lines.append("    ⚠ 有维度低于这条已有上岗线")
        if signal["reason"]:
            lines.append(f"    测不成：{signal['reason']}")
        for reading in signal["readings"]:
            lines.append("      " + _render_reading(signal["id"], reading))
    return "\n".join(lines)


def _render_reading(signal_id: str, reading: Mapping[str, Any]) -> str:
    if signal_id == "T-1":
        flag = " ⚠按候选线会越线" if reading["over_candidate_line"] else ""
        small = "（n < 20，不够候选线的门槛）" if reading["candidate_line_not_met_n"] else ""
        return (
            f"{reading['clause']}: 分歧 {reading['eq_class_mismatch']}/{reading['n']}"
            f" = {reading['rate']:.1%}{flag}{small}"
        )
    if signal_id == "T-2":
        line = T2_HARD_LINE if reading["kind"] == "cohen_kappa" else T2_SOFT_LINE
        flag = " ⚠低于已有上岗线" if float(reading["kappa"]) < line else ""
        return (
            f"{reading['dimension']}: {reading['kind']} = {reading['kappa']:.3f}"
            f"（n={reading['n']}，线 {line:.2f}）{flag}"
        )
    if signal_id == "T-3":
        rate = reading["vague_rate"]
        share = f" = {rate:.1%}" if rate is not None else ""
        return (
            f"{reading['dimension']}: 含糊 "
            f"{reading['n_with_reasons'] - reading['n_quoting_observation']}"
            f"/{reading['n_with_reasons']}{share}"
        )
    parts = []
    for side in ("pred", "gold"):
        block = reading[side]
        if not block["n"]:
            parts.append(f"{side}: 无取值")
            continue
        flag = " ⚠退化" if block["degenerate"] else ""
        parts.append(
            f"{side}: 众数 {block['mode']} {block['mode_share']:.1%} / "
            f"{block['distinct']} 个取值 / n={block['n']}{flag}"
        )
    return f"{reading['clause']} · " + " · ".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def report_path(root: pathlib.Path | str, version: int) -> pathlib.Path:
    return pathlib.Path(root) / f"v{version}" / REPORT_FILE


def load_snapshot_for(root: pathlib.Path | str, version: int) -> dict[str, Any]:
    path = pathlib.Path(root) / f"v{version}" / "snapshot.json"
    if not path.exists():
        raise TriggerError(
            f"快照不存在：{path}\n"
            f"先跑 `agreement --version {version} --emit-disagreements`"
        )
    return load_snapshot(path)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="triggers",
        description=(
            "触发读数 T-1～T-4（方案 16 C-16）：只出读数，不判合格；"
            "触发 ≠ 归因"
        ),
    )
    ap.add_argument("--version", type=int, required=True, help="冻结版本号（v7 → 7）")
    ap.add_argument(
        "--out", default=str(DEFAULT_CALIBRATION_ROOT), help="派生数据根（缺省同上）"
    )
    ap.add_argument(
        "--snapshot", default="", help="快照路径（缺省 v{N}/snapshot.json）"
    )
    ap.add_argument(
        "--split", default=HEADLINE_SPLIT,
        help=f"读哪一档（缺省头条档 {HEADLINE_SPLIT}；空串 = 三档合计）",
    )
    ap.add_argument(
        "--judge-report", default="",
        help="judge 逐行读数（JSON/JSONL，行须带 golden_id + dimension）——"
             "T-2 / T-3 今天缺它就测不成",
    )
    ap.add_argument(
        "--quote-marker", action="append", default=[],
        help="T-3 判「引用了观察原文」的字符串（可重复）；不给则 T-3 测不成",
    )
    ap.add_argument("--json-out", default="", help="报告另存 JSON（默认已随 v{N}/triggers.json 落盘）")
    args = ap.parse_args(argv)

    try:
        snapshot = (
            load_snapshot(args.snapshot) if args.snapshot else load_snapshot_for(args.out, args.version)
        )
        pairs = load_judge_pairs(args.judge_report) if args.judge_report else None
        report = evaluate(
            snapshot,
            judge_pairs=pairs,
            quote_markers=tuple(args.quote_marker),
            split=args.split or None,
        )
    except (TriggerError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print("退出码 2（读数没跑起来）：这不是「驳回」，是判据没跑", file=sys.stderr)
        return 2

    path = report_path(args.out, args.version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(render(report))
    print(f"\n→ {path}")
    if args.json_out:
        pathlib.Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    unmeasurable = [s["id"] for s in report["signals"] if not s["measurable"]]
    if unmeasurable:
        print(f"⚠ 测不成：{' / '.join(unmeasurable)}（报告里逐条写了缺什么）")
    if not report["measurable"]:
        print(
            "错误：四条信号一条都没测到东西 —— 退 2（`0 条样本 ≠ 一切正常`）",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DISTINCT_VALUES_LINE",
    "MODE_SHARE_LINE",
    "REPORT_FILE",
    "SIGNAL_IDS",
    "SIGNALS",
    "JudgePair",
    "SignalSpec",
    "TriggerError",
    "clause_stats",
    "evaluate",
    "load_judge_pairs",
    "load_snapshot_for",
    "main",
    "report_path",
    "render",
    "spec_of",
    "t1_disagreement_rate",
    "t2_judge_human",
    "t3_vague_rate",
    "t4_distribution",
]