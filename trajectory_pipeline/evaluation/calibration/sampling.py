"""分层抽样器 S0–S3（方案 16 §5.7 / D-C10）。

一句话：**人工判读的 40 条不是随手抽的**——只抽分歧会把问题估大，所以额外留
一层「两侧一致但落在两端」的对照层，用来估「一致但都错」的那部分假阴性。

四条纪律，代码里逐条对上
----------------------
1. **入层判据全部机械可算**（:data:`STRATA` 的 ``why`` 字段就是判据本身）。
   人不参与分层——人只判读抽出来的样本。一条判据要靠人读一遍才知道算不算，
   那它就不是判据，是意见。
2. **配额缺口如实报，不拿别的层补**（:func:`_draw` 只在**本层**候选里抽）。
   补齐会让 40 条看着齐了，实际是同一类问题占了 20 条——「分层样本」这个词
   就废了。缺口写进 ``short_by``，宁可 26 条诚实也不凑 40。
3. **抽样可复现靠「固定种子 + 稳定排序 + 哈希定序」，不用 RNG**
   （:func:`_order_key`）。``random.sample`` 的实现细节换 Python 版本可能变，
   冻结档是研究产物，换台机器就跑不出同一份样本——那不叫可复现。
4. **层内读数不外推总体**（D-C10）：每行带 ``sample_weight`` = N_h/n_h 供人
   知道**这条有多重**，但 ``sample_weight`` **不是**总体估计量；分层样本上的
   kappa 只描述这 40 条，要外推得单独设计抽样权重推导。

今天（v7）能抽到什么、抽不到什么
--------------------------------
头条档（calibration）D-10 上：S1 = 33 条可抽（配额 10，取满）、S0 = 95 条可抽
（取满）、**S2 = 0**（要 judge 逐行理由，而它不落盘）、**S3 = 0**（要软维度
3 分金标或 ``ambiguous`` 硬标签，两者今天都不存在）⇒ **实抽 20 条，缺 20**。

⚠ 抽不满**不是** bug、也不是「先随便补 20 条」的理由：它说明判读盘里少两类
样本（S2 的理由质量、S3 的中间段），而人工判读只看得见 S0/S1——**回路这半边
看不见 S2/S3 那一半的质量**。这是「软维度金标未填」的又一个后果。

产物落在 ``calibration/v{N}/sample.jsonl``，**不覆盖** C-15 的
``worksheet.jsonl``（两份是不同东西：worksheet 是分歧卡的判读表，sample 是
判读**谁**的抽样清单）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from trajectory_pipeline.evaluation.calibration.snapshot import (
    DIFF_VERDICTS,
    HARD_DIM,
    VERDICT_AGREE,
    VERDICT_UNGRADED,
    load_snapshot,
)
from trajectory_pipeline.evaluation.calibration.triggers import (
    DEFAULT_CALIBRATION_ROOT,
    JudgePair,
    TriggerError,
    load_judge_pairs,
    load_snapshot_for,
)
from trajectory_pipeline.evaluation.golden.alignment import equiv_class
from trajectory_pipeline.evaluation.golden.splits import HEADLINE_SPLIT

SAMPLE_FILE = "sample.jsonl"

#: D-C10 拍板的配额：10 × 4 = 40。改它 = 改设计，走 rubric/方案级变更。
QUOTA_PER_STRATUM = 10

#: 抽样种子。**写死在这里**（不读时钟、不读环境变量）——同一份快照 + 同一个
#: 种子 = 同一份样本，这是冻结档能被复算的前提。
SAMPLING_SEED = "20261023"

#: S0「两端」用的等价类：一端是**能播**，另一端是**压根没到 / 内容损坏**。
#: 为什么是这两个：它们是这张表里最不含混的两端——两侧一致时最不容易「一致
#: 但都错」，所以拿它们当**对照层**才估得出假阴性。中间那些
#: （PAYWALL / ACCESS_BLOCKED / NO_PLAY_RESOURCE）恰恰是最容易含混、最需要
#: 抽查的，**不属于对照层**。
S0_END_CLASSES: tuple[str, ...] = ("SUCCESS", "REACH_FAIL", "BROKEN_CONTENT")

#: S3 中间段的两条臂：软维度落在正中 3 分，或硬分类判成 ``ambiguous``。
#: ⚠️ ``ambiguous`` **不在** :data:`EQUIV_CLASS` 里 ⇒ 硬臂今天恒为空，
#: 这是设计如此（fail-closed：等价类表没有它，就不能假装能判它）。
S3_SOFT_MID_SCORE = 3.0
S3_HARD_AMBIGUOUS = "ambiguous"


class SamplingError(Exception):
    """抽样输入不成立（→ 退 2）。**不是驳回**：是「一条都抽不出来」。"""


@dataclass(frozen=True)
class Stratum:
    """一层的判据。``test`` 返回 ``True`` 即入层；``why`` 是给人看的判据原文。"""

    id: str
    name: str
    question: str
    why: str
    test: Callable[[Mapping[str, Any], Mapping[str, Any]], bool] = field(
        compare=False, repr=False
    )


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------


def _hard_class(entry: Mapping[str, Any]) -> str | None:
    """条目的 D-10 侧等价类。未登记标签 → ``None``（不猜）。"""
    label = entry.get("gold_value" if entry.get("verdict") == VERDICT_AGREE else "pred_value")
    if not label:
        return None
    try:
        return equiv_class(str(label))
    except KeyError:
        return None


def _s0(entry: Mapping[str, Any], _judge: Mapping[str, Any]) -> bool:
    """S0 对照层：两侧一致（同一等价类）**且**落在两端。"""
    if str(entry.get("verdict") or "") != VERDICT_AGREE:
        return False
    if str(entry.get("dimension") or "") == HARD_DIM:
        return _hard_class(entry) in S0_END_CLASSES
    # 软维度臂：一致且落在 1 / 5 分两端。
    try:
        return float(entry.get("gold_value")) in (1.0, 5.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _s1(entry: Mapping[str, Any], _judge: Mapping[str, Any]) -> bool:
    """S1：两评分者打架（分歧卡三态）。"""
    return str(entry.get("verdict") or "") in DIFF_VERDICTS


def _s2(entry: Mapping[str, Any], judge: Mapping[str, Any]) -> bool:
    """S2：理由含糊——有理由但引不到观察原文片段。

    ``judge`` 是按 ``(golden_id, dimension)`` 索引的逐行读数；**没有这一行就
    不入层**（不给代理数）。引什么片段由人给（``--quote-marker``）。
    """
    reasons = judge.get("reasons") or ()
    if not reasons:
        return False
    markers = judge.get("quote_markers") or ()
    if not markers:
        return True
    return not any(marker in reason for marker in markers for reason in reasons)


def _s3(entry: Mapping[str, Any], _judge: Mapping[str, Any]) -> bool:
    """S3 中间段：软维度正中 3 分，或硬分类 ``ambiguous``。"""
    if str(entry.get("dimension") or "") == HARD_DIM:
        return str(entry.get("gold_value") or "") == S3_HARD_AMBIGUOUS
    try:
        return float(entry.get("gold_value")) == S3_SOFT_MID_SCORE  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


#: 入层优先级。**一条只入一层**：S1/S3/S2 先挑（信息量最大、最稀缺），
#: S0 兜底。重叠写进报告（``overlap``）——层间重叠是事实，藏起来就成了「层是
#: 互斥的」这个假前提。
STRATA: tuple[Stratum, ...] = (
    Stratum(
        id="S1",
        name="两评分者打架",
        question="这一条两侧判成不同等价类 / 档位 / 标签？",
        why=f"verdict ∈ {sorted(DIFF_VERDICTS)}（分歧卡三态）",
        test=_s1,
    ),
    Stratum(
        id="S3",
        name="中间段",
        question="这一条落在「不好判」的正中间？",
        why=f"软维度金标 = {S3_SOFT_MID_SCORE:g} 分，或硬分类 = {S3_HARD_AMBIGUOUS!r}"
        f"（⚠ {S3_HARD_AMBIGUOUS!r} 不在 EQUIV_CLASS 里 ⇒ 硬臂今天恒空）",
        test=_s3,
    ),
    Stratum(
        id="S2",
        name="理由含糊",
        question="判分理由引得到观察原文吗？",
        why="有 judge 理由、且理由里引不到 `--quote-marker` 给的观察片段",
        test=_s2,
    ),
    Stratum(
        id="S0",
        name="一致且两端（对照层）",
        question="两侧一致，但这一条是不是最不容易含混的那两端？",
        why=f"verdict = agree 且等价类 ∈ {list(S0_END_CLASSES)}"
        "（软维度臂：一致且 1 / 5 分）",
        test=_s0,
    ),
)

STRATUM_IDS: tuple[str, ...] = tuple(s.id for s in STRATA)

#: 某层「今天抽不出来」的机械原因。没判据可算 = 层总体 0。
STRATUM_GAPS: Mapping[str, str] = {
    "S2": (
        "judge 逐行理由不落盘：`evaluation.cli` 的行键是 sample_id，"
        "与快照的 golden_id 配不上（要 `--judge-report`）；"
        "且「观察原文片段」要人给（`--quote-marker`）"
    ),
    "S3": (
        f"要软维度正中 {S3_SOFT_MID_SCORE:g} 分金标或硬分类 {S3_HARD_AMBIGUOUS!r}——"
        "黄金集软维度全是 ungraded，EQUIV_CLASS 里也没有 ambiguous，两条臂今天都是空的"
    ),
}


def stratum_of(sid: str) -> Stratum:
    for stratum in STRATA:
        if stratum.id == sid:
            return stratum
    raise KeyError(f"{sid!r} 不是分层（{', '.join(STRATUM_IDS)}）")


# ---------------------------------------------------------------------------
# 抽签
# ---------------------------------------------------------------------------


def _order_key(seed: str, entry: Mapping[str, Any]) -> tuple[str, str, str]:
    """确定性定序键：种子的 SHA-256，**不碰 RNG**。

    为什么不用 ``random.sample``：它的实现细节跨 Python 版本可能变，而冻结档
    要的是「换台机器跑出同一份样本」。哈希定序 + 稳定兜底键（golden_id /
    dimension）⇒ 同输入必然同输出。
    """
    key = f"{seed}\x00{entry.get('golden_id') or ''}\x00{entry.get('dimension') or ''}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return digest, str(entry.get("golden_id") or ""), str(entry.get("dimension") or "")


def _key_of(entry: Mapping[str, Any]) -> tuple[str, str]:
    return str(entry.get("golden_id") or ""), str(entry.get("dimension") or "")


def assign_strata(
    snapshot: Mapping[str, Any],
    *,
    split: str | None = HEADLINE_SPLIT,
    judge_pairs: Sequence[JudgePair] = (),
    quote_markers: Sequence[str] = (),
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, int]]:
    """条目 → 归属层（**一条一层**）+ 层间重叠计数。

    ``ungraded`` 不入任何层：它既没判过也谈不上分歧，塞进抽样池等于拿「没数据」
    去填人工判读的坑。
    """
    judge_index: dict[tuple[str, str], dict[str, Any]] = {}
    for pair in judge_pairs:
        judge_index[(pair.golden_id, pair.dimension)] = {
            "reasons": pair.reasons,
            "quote_markers": tuple(quote_markers),
        }
    assigned: dict[tuple[str, str], dict[str, Any]] = {}
    overlap: dict[str, int] = {}
    for entry in snapshot.get("entries", []):
        if split is not None and str(entry.get("split") or "") != split:
            continue
        if str(entry.get("verdict") or "") == VERDICT_UNGRADED:
            continue
        key = _key_of(entry)
        hits = [s.id for s in STRATA if s.test(entry, judge_index.get(key, {}))]
        if not hits:
            continue
        overlap[len(hits).__str__()] = overlap.get(len(hits).__str__(), 0) + 1
        if len(hits) > 1:
            overlap["multi:" + ",".join(hits)] = overlap.get("multi:" + ",".join(hits), 0) + 1
        assigned[key] = {"stratum": hits[0], "also": hits[1:], "entry": entry}
    return assigned, overlap


def _draw(
    candidates: Sequence[Mapping[str, Any]], quota: int, seed: str
) -> list[Mapping[str, Any]]:
    """本层内按定序键取前 ``quota`` 条。**候选不足就少给，不补。**"""
    ordered = sorted(candidates, key=lambda e: _order_key(seed, e))
    return list(ordered[:quota])


def build_sample(
    snapshot: Mapping[str, Any],
    *,
    split: str | None = HEADLINE_SPLIT,
    quota: int = QUOTA_PER_STRATUM,
    seed: str = SAMPLING_SEED,
    judge_pairs: Sequence[JudgePair] = (),
    quote_markers: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """抽 ``quota × len(STRATA)`` 条，返回 ``(样本行, 汇总)``。

    ⚠ 汇总里 ``taken`` 可能小于 ``quota``——**这是读数不是失败**。缺口
    ``short_by`` 原样写出，``sample_weight`` 按**实取**条数算（N_h/n_h）。
    """
    assigned, overlap = assign_strata(
        snapshot, split=split, judge_pairs=judge_pairs, quote_markers=quote_markers
    )
    pools: dict[str, list[Mapping[str, Any]]] = {s.id: [] for s in STRATA}
    for record in assigned.values():
        pools[record["stratum"]].append(record)

    rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "split": split,
        "quota_per_stratum": quota,
        "seed": seed,
        "overlap": overlap,
        "strata": [],
    }
    for stratum in STRATA:
        members = pools[stratum.id]
        pool_n = len(members)
        drawn = _draw(members, quota, seed)
        weight = (pool_n / len(drawn)) if drawn else None
        for rank, record in enumerate(drawn, start=1):
            entry = record["entry"]
            rows.append(
                {
                    "golden_id": entry.get("golden_id") or "",
                    "dimension": entry.get("dimension") or "",
                    "clause": entry.get("rubric_ref") or "",
                    "split": entry.get("split") or "",
                    "stratum": stratum.id,
                    "stratum_name": stratum.name,
                    "stratum_why": stratum.why,
                    "stratum_rank": rank,
                    "also_in": record["also"],
                    "population": pool_n,
                    "sample_weight": round(weight, 4) if weight else None,
                    "verdict": entry.get("verdict") or "",
                    "delta_kind": entry.get("delta_kind") or "",
                    "tier_gap": entry.get("tier_gap"),
                    "gold_value": entry.get("gold_value"),
                    "pred_value": entry.get("pred_value"),
                    "pred_provider": entry.get("pred_provider") or "",
                    "seed": seed,
                }
            )
        summary["strata"].append(
            {
                "id": stratum.id,
                "name": stratum.name,
                "why": stratum.why,
                "population": pool_n,
                "quota": quota,
                "taken": len(drawn),
                "short_by": max(0, quota - len(drawn)),
                "sample_weight": round(weight, 4) if weight else None,
                "gap": STRATUM_GAPS.get(stratum.id, "") if pool_n == 0 else "",
            }
        )
    rows.sort(key=lambda r: (STRATUM_IDS.index(r["stratum"]), r["stratum_rank"]))
    summary["taken"] = len(rows)
    summary["short_by"] = sum(s["short_by"] for s in summary["strata"])
    summary["distinct_samples"] = len({r["golden_id"] for r in rows})
    summary["golden_version"] = (snapshot.get("meta") or {}).get("golden_version")
    summary["rubric_version"] = (snapshot.get("meta") or {}).get("rubric_version")
    return rows, summary


def render(summary: Mapping[str, Any]) -> str:
    lines = [
        f"分层抽样 S0–S3 · 黄金集 v{summary.get('golden_version')} "
        f"· rubric {summary.get('rubric_version')} · 档 {summary.get('split')}",
        f"配额 {summary['quota_per_stratum']}×{len(STRATA)} = "
        f"{summary['quota_per_stratum'] * len(STRATA)} · 种子 {summary['seed']}"
        "（哈希定序，无 RNG ⇒ 同输入必同输出）",
        "⚠ **层内读数不外推总体**（D-C10）：`sample_weight` 只是「这条有多重」，"
        "不是总体估计量",
        "",
    ]
    for item in summary["strata"]:
        flag = " ✓" if item["short_by"] == 0 else f" ⚠缺 {item['short_by']}"
        lines.append(
            f"{item['id']} {item['name']}：总体 {item['population']} / 配额 "
            f"{item['quota']} / 实抽 {item['taken']}{flag}"
            + (f"（权重 {item['sample_weight']}）" if item["sample_weight"] else "")
        )
        lines.append(f"    判据：{item['why']}")
        if item["gap"]:
            lines.append(f"    抽不出的原因：{item['gap']}")
    multi = {k: v for k, v in summary["overlap"].items() if k.startswith("multi:")}
    if multi:
        lines.append(f"\n⚠ 层间重叠（按优先级归到 {STRATUM_IDS[0]}…{STRATUM_IDS[-1]}）：")
        for key, count in sorted(multi.items()):
            lines.append(f"    同时命中 {key.removeprefix('multi:')}：{count} 条")
    lines.append(
        f"\n实抽 {summary['taken']} 条（缺口 {summary['short_by']}）· "
        f"覆盖 {summary['distinct_samples']} 个不同样本"
        "（行是 `golden_id × dimension`，同一样本可能占多行）"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def sample_path(root: pathlib.Path | str, version: int) -> pathlib.Path:
    return pathlib.Path(root) / f"v{version}" / SAMPLE_FILE


def write_sample(root: pathlib.Path | str, version: int, rows: Sequence[Mapping[str, Any]]) -> pathlib.Path:
    """落盘。**不覆盖 C-15 的 worksheet.jsonl**——两份是不同东西（见 docstring）。"""
    path = sample_path(root, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="sampling",
        description=(
            "分层抽样 S0–S3（方案 16 C-19）：固定配额 + 固定种子，"
            "产出人工判读清单；缺口如实报，不补"
        ),
    )
    ap.add_argument("--version", type=int, required=True, help="冻结版本号（v7 → 7）")
    ap.add_argument("--out", default=str(DEFAULT_CALIBRATION_ROOT), help="派生数据根（缺省同上）")
    ap.add_argument("--snapshot", default="", help="快照路径（缺省 v{N}/snapshot.json）")
    ap.add_argument(
        "--split", default=HEADLINE_SPLIT,
        help=f"抽哪一档（缺省头条档 {HEADLINE_SPLIT}；空串 = 三档合计）",
    )
    ap.add_argument("--quota", type=int, default=QUOTA_PER_STRATUM, help="每层配额（缺省 10）")
    ap.add_argument("--seed", default=SAMPLING_SEED, help=f"抽样种子（缺省 {SAMPLING_SEED}）")
    ap.add_argument("--judge-report", default="", help="judge 逐行读数（S-2 臂要用）")
    ap.add_argument("--quote-marker", action="append", default=[], help="观察原文片段标记（可重复）")
    ap.add_argument("--dry-run", action="store_true", help="只印读数不落盘")
    args = ap.parse_args(argv)

    if args.quota <= 0:
        print("错误：--quota 必须 ≥ 1（配额 0 = 不抽，那不如不跑）", file=sys.stderr)
        return 2

    try:
        snapshot = (
            load_snapshot(args.snapshot)
            if args.snapshot
            else load_snapshot_for(args.out, args.version)
        )
        pairs = load_judge_pairs(args.judge_report) if args.judge_report else ()
    except (TriggerError, SamplingError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print("退出码 2（一条都抽不出来）：这不是「驳回」，是判据没跑", file=sys.stderr)
        return 2

    rows, summary = build_sample(
        snapshot,
        split=args.split or None,
        quota=args.quota,
        seed=args.seed,
        judge_pairs=pairs,
        quote_markers=tuple(args.quote_marker),
    )
    print(render(summary))
    if not rows:
        print(
            "错误：一条都没抽到 —— 退 2（`0 条样本 ≠ 一切正常`："
            "档位选错，或该档一条可判条目都没有）",
            file=sys.stderr,
        )
        return 2
    if args.dry_run:
        print("\n(--dry-run：未落盘)")
        return 0
    path = write_sample(args.out, args.version, rows)
    print(f"\n→ {path}")
    if summary["short_by"]:
        print(
            f"⚠ 缺 {summary['short_by']} 条（{args.quota * len(STRATA)} 的配额没抽满）"
            "—— 缺口原样报，不从别的层补：补齐会把 40 条看着齐了而实际同质"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "QUOTA_PER_STRATUM",
    "S0_END_CLASSES",
    "SAMPLING_SEED",
    "STRATA",
    "STRATUM_GAPS",
    "STRATUM_IDS",
    "SamplingError",
    "Stratum",
    "assign_strata",
    "build_sample",
    "main",
    "render",
    "sample_path",
    "stratum_of",
    "write_sample",
]