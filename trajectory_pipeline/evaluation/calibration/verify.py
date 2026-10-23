"""内环验证器（方案 16 §5.1 / C-08）：两轮快照差分 → 四条判据 → 退出码。

它回答的那句话
--------------
**「这次改动修好了哪几条、弄坏了哪几条、够不够格合入？」**
在它之前，这个问题的答案只存在于人写的复盘文章里（H1-α 的「修 4 / 伤 6 /
净 −2 / 撤回」就是靠 [11 号附录 A](../docs/设计方案/11-外部方案对账.md) 复述的）。

输入只有一样：**两轮全量快照**（``agreement --emit-disagreements`` 落的
``snapshot.json``）。为什么不是分歧卡——卡只记「这一轮不一致」，而"上一轮对、
这一轮错"的那一行**上一轮根本不在卡里**（见 :mod:`.snapshot` 模块 docstring）。

四条判据（§5.1，逐条对应一个 ``criterion``）
------------------------------------------
============================ ==================================================
``net_improvement``          **净改善 > 0**：``修好 − 反转 > 0``（不是"改了多少条"）
``class_regression``         **无类回归**：等价类 F1 任一下降超容差即驳回
                             （总数上升但 ``PAYWALL`` recall 掉了不算改善）
``bucket_clear``             **同桶清零**：本轮改动针对的桶，残留分歧必须为 0
``regression_regressed_zero``**回归集**：``regressed == 0``（C-17 冻结的 60 条）
============================ ==================================================

**第四条是 v1.3 加的，也是最容易被绕过的**：只有前三条时，H1-α 的净改善 −2
能靠"同桶清零"的松动解释过去；第四条不看总数，只看**有没有把原本判对的改错**。

判据只有三种状态：``pass`` / ``fail`` / ``not_evaluable``
--------------------------------------------------------
``not_evaluable`` **不是 pass**——它表示"证据不够，判不了"。前两条判不了就驳回
（没证据不能主张改善）；``bucket_clear`` 在没有分诊数据时**记 n/a 且不参与判定**，
并**明确打印「本项不算通过」**：分诊表是 C-05 的产物，现在还不存在，拿它的缺席
去拦所有改动只会逼人加 ``--no-verify``。

为什么朴素比对会判错（三条守卫）
--------------------------------
1. **按 ``(golden_id, dimension)`` 配对**，``golden_id`` 里带 run id ⇒ **重采集
   的行配不上**（不是"判对了"，是"没对上"）。配不上的逐条计数并打印，绝不当成
   ``unchanged``。
2. **回归集的键集与金标必须两轮相同**——不同就 ``VerifyError``（退出码 2），
   不判。它要么是有人改了冻结的行内容，要么是两轮快照根本不可比；两种都不该
   被算成"回归 0 条"这个绿色结论。
3. **两轮之间换过档的行也是退出码 2**（``calibration`` → ``regression``）：
   C-17 切档之后逐档不可比，命令必须拒绝而不是给出一个跨档的净改善。

🔒 持有集只报条数、不判（D-C5）；软维度只出读数、不设线（D-C9）。

退出码
------
``0`` 通过 · ``1`` **驳回**（判据不过——CI 靠它变红）· ``2`` 输入/用法错误
（快照不可比、文件缺失、键重复）。**1 与 2 必须分开**：前者是"这次改动不行"，
后者是"这次没测成"，两者混成一个非零码，CI 的报告就会把工具故障读成回退。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Iterable, Mapping, Sequence

from trajectory_pipeline.evaluation.calibration.snapshot import (
    DIFF_VERDICTS,
    HARD_DIM,
    SNAPSHOT_SCHEMA_VERSION,
    VERDICT_AGREE,
    VERDICT_DETAIL_MISMATCH,
    VERDICT_DISAGREE,
    VERDICT_TIER_GAP,
    VERDICT_UNGRADED,
    VERDICTS,
    load_snapshot,
)
from trajectory_pipeline.evaluation.golden.splits import (
    HEADLINE_SPLIT,
    SPLIT_CALIBRATION,
    SPLIT_HOLDOUT,
    SPLIT_REGRESSION,
)
from trajectory_pipeline.evaluation.calibration.triage import (
    TriageError,
    load_triage,
    validate_rows as validate_triage_rows,
)

#: 派生快照的默认根。与 ``builder.PIPELINE_ROOT / "calibration"`` 是同一个
#: 位置，由 ``test_快照默认根与builder同源`` 钉住——只 import builder 拿一个
#: 常量会让验证器拖上整个报表模块，抄一份又必然漂，所以两条都做
#: （本地常量 + 断言）。
DEFAULT_SNAPSHOT_ROOT = (
    pathlib.Path(__file__).resolve().parents[2] / "output" / "pipeline" / "calibration"
)

#: 条目级 verdict 的「三面」归属：ok（D-10 算对）/ bad（算错）/ unverifiable（判不了）。
#:
#: **``detail_mismatch`` 属于 ok**：等价类相同、字面不同，D-10 不算错（§3.2），
#: 若把它算成"错"，一次标签细分就会让整门判据翻号。
#: **``tier_gap`` 属于 bad**：软维度的档差超容差就是判错，与硬分类同面不同分母。
VERDICT_SIDES: Mapping[str, str] = {
    VERDICT_AGREE: "ok",
    VERDICT_DETAIL_MISMATCH: "ok",
    VERDICT_DISAGREE: "bad",
    VERDICT_TIER_GAP: "bad",
    VERDICT_UNGRADED: "unverifiable",
}
# fail-closed：``snapshot.VERDICTS`` 长出新状态而这里没归类时，导入即炸，
# 而不是让新状态悄悄落进"不计"的那一桶。
_MISSING_SIDES = set(VERDICTS) - set(VERDICT_SIDES)
if _MISSING_SIDES:  # pragma: no cover - 扩展 VERDICTS 时立刻命中
    raise RuntimeError(
        f"VERDICT_SIDES 未归类 {sorted(_MISSING_SIDES)}；先决定它属于 "
        "ok / bad / unverifiable 哪一面——判据的口径从这里起"
    )

SIDE_OK = "ok"
SIDE_BAD = "bad"
SIDE_UNVERIFIABLE = "unverifiable"

CRITERION_PASS = "pass"
CRITERION_FAIL = "fail"
CRITERION_NOT_EVALUABLE = "not_evaluable"

GATE_PASS = "pass"
GATE_REJECT = "reject"

#: 差分五桶。前三个是计划要的「三类计数」；后两个**不在三类里，但绝不能消失**：
#: ``still_wrong`` 是"两轮都错"（不是变化，但它是分母），``unverifiable`` 是
#: "任一轮判不了"（§2.5：缺证据不许记成"改好了"）。
COUNT_KEYS: tuple[str, ...] = (
    "fixed",
    "regressed",
    "unchanged",
    "still_wrong",
    "unverifiable",
)


class VerifyError(Exception):
    """输入不可用（退出码 2）。**不是**"改动不合格"——那由 ``gate=reject`` 表达。"""


# --------------------------------------------------------------------------
# 快照结构与配对
# --------------------------------------------------------------------------


def _entry_key(entry: Mapping[str, Any]) -> tuple[str, str]:
    return (str(entry.get("golden_id") or ""), str(entry.get("dimension") or ""))


def index_entries(snapshot: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """``(golden_id, dimension)`` → 条目。**键重复直接报错**。

    键重复＝同一行同一维出了两条判定，配对就成了二选一——那时候挑哪条都是猜。
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in snapshot.get("entries", []):
        key = _entry_key(entry)
        if not key[0]:
            raise VerifyError(f"快照里有条目缺 golden_id：{entry!r}")
        if key in out:
            raise VerifyError(
                f"快照里 {key[0]} / {key[1]} 出现两条判定：配对要二选一，拒绝猜"
            )
        out[key] = dict(entry)
    return out


def _check_comparable(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    """两轮快照能不能比。结构上不可比的一律 ``VerifyError``。"""
    bmeta = baseline.get("meta") or {}
    cmeta = current.get("meta") or {}
    # schema 版本落在 entries 之外（顶层），老快照没有这个键时当空串比。
    bsch = str(baseline.get("schema_version") or "")
    csch = str(current.get("schema_version") or "")
    if bsch != csch:
        raise VerifyError(
            f"两轮快照 schema 不同（基线 {bsch or '未记录'} / 本轮 {csch or '未记录'}）："
            f"字段增删过，逐条差分没有意义。当前 schema 是 {SNAPSHOT_SCHEMA_VERSION}，"
            "重出一份基线再比"
        )
    if bmeta.get("hard_dim") != cmeta.get("hard_dim"):
        raise VerifyError(
            f"两轮快照的硬维度不同（{bmeta.get('hard_dim')!r} / "
            f"{cmeta.get('hard_dim')!r}）：头条判的不是同一道题"
        )
    return {
        "baseline": {
            "golden_version": bmeta.get("golden_version"),
            "rubric_version": bmeta.get("rubric_version") or "",
            "evaluator_version": bmeta.get("evaluator_version") or "",
            "tolerance": bmeta.get("tolerance"),
        },
        "current": {
            "golden_version": cmeta.get("golden_version"),
            "rubric_version": cmeta.get("rubric_version") or "",
            "evaluator_version": cmeta.get("evaluator_version") or "",
            "tolerance": cmeta.get("tolerance"),
        },
        # 软维度容差不同 ⇒ 软维度判据不同义（不进判据，但要喊出来）。
        "soft_tolerance_mismatch": bmeta.get("tolerance") != cmeta.get("tolerance"),
    }


def _split_of(entry: Mapping[str, Any], *, default_headline: bool) -> str:
    split = str(entry.get("split") or "")
    if split:
        return split
    # 老快照（未切档）整份都没有 split；``summarize_snapshot`` 对这种情况的
    # 处置同样是"全部算头条"。只在**整份都空**时用它，混着空串就交给调用方
    # 当"没对上的档"处理。
    return HEADLINE_SPLIT if default_headline else ""


def _has_any_split(entries: Iterable[Mapping[str, Any]]) -> bool:
    return any(str(e.get("split") or "") for e in entries)


# --------------------------------------------------------------------------
# 差分
# --------------------------------------------------------------------------


def diff_entries(
    baseline: Mapping[tuple[str, str], Mapping[str, Any]],
    current: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    dimension: str,
    split: str,
    default_headline: bool = False,
) -> dict[str, Any]:
    """某一档某一维的逐条差分（五桶 + 未配对 + 归因计数）。

    ``split`` 过滤两轮各自的档位标注；任一档位为空串（未切档）时按
    ``default_headline`` 折算——与 :func:`~.snapshot.summarize_snapshot` 同一口径。
    """
    bkeys = {k for k, e in baseline.items() if e.get("dimension") == dimension
             and _split_of(e, default_headline=default_headline) == split}
    ckeys = {k for k, e in current.items() if e.get("dimension") == dimension
             and _split_of(e, default_headline=default_headline) == split}
    counts = {k: 0 for k in COUNT_KEYS}
    gold_changed = pred_changed = both_changed = 0
    for key in sorted(bkeys & ckeys):
        b, c = baseline[key], current[key]
        bside = VERDICT_SIDES.get(str(b.get("verdict")), SIDE_UNVERIFIABLE)
        cside = VERDICT_SIDES.get(str(c.get("verdict")), SIDE_UNVERIFIABLE)
        if bside == SIDE_UNVERIFIABLE or cside == SIDE_UNVERIFIABLE:
            # 任一轮判不了 ⇒ 无从说"修好/弄坏"（§2.5：缺证据不进分子也不进分母）
            counts["unverifiable"] += 1
        elif bside == SIDE_BAD and cside == SIDE_OK:
            counts["fixed"] += 1
        elif bside == SIDE_OK and cside == SIDE_BAD:
            counts["regressed"] += 1
        elif bside == SIDE_OK:
            counts["unchanged"] += 1
        else:
            counts["still_wrong"] += 1
        b_gold, c_gold = b.get("gold_value"), c.get("gold_value")
        b_pred, c_pred = b.get("pred_value"), c.get("pred_value")
        g_ch, p_ch = b_gold != c_gold, b_pred != c_pred
        if g_ch and p_ch:
            both_changed += 1
        elif g_ch:
            gold_changed += 1
        elif p_ch:
            pred_changed += 1
    return {
        "split": split,
        "dimension": dimension,
        # 两轮各自的档内条目数：用来分清"这一版根本没有这一档"与"有但没配上"
        "entries_baseline": len(bkeys),
        "entries_current": len(ckeys),
        "paired": len(bkeys & ckeys),
        "only_baseline": len(bkeys - ckeys),
        "only_current": len(ckeys - bkeys),
        "only_baseline_ids": sorted(k[0] for k in bkeys - ckeys),
        "only_current_ids": sorted(k[0] for k in ckeys - bkeys),
        **counts,
        "net": counts["fixed"] - counts["regressed"],
        "attribution": {
            "gold_changed": gold_changed,
            "pred_changed": pred_changed,
            "both_changed": both_changed,
        },
    }


def _empty_diff(split: str) -> dict[str, Any]:
    """该档在该维上两轮都没有条目时的零值（让下游不必到处判 ``None``）。"""
    return {
        "split": split,
        "dimension": HARD_DIM,
        "entries_baseline": 0,
        "entries_current": 0,
        "paired": 0,
        "only_baseline": 0,
        "only_current": 0,
        "only_baseline_ids": [],
        "only_current_ids": [],
        **{k: 0 for k in COUNT_KEYS},
        "net": 0,
        "attribution": {"gold_changed": 0, "pred_changed": 0, "both_changed": 0},
    }


def _agreed(diff_stats: Mapping[str, Any]) -> int:
    return int(diff_stats["unchanged"]) + int(diff_stats["fixed"])


def _ratio(agreed: int, n: int) -> float | None:
    return (agreed / n) if n else None


def _per_class(
    baseline: Mapping[tuple[str, str], Mapping[str, Any]],
    current: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    split: str,
    default_headline: bool,
    class_tolerance: float,
) -> dict[str, Any]:
    """按**语义等价类**逐类 P/R/F1 两轮对比（§5.1 第 2 条）。

    只在**配对上的** ``d10`` 条目里算——用两轮各自的全部行算，会变成"分母变了"
    冒充"F1 变了"。F1 实现与 ``agreement`` 的下钻口径 D 同一份（``builder``
    里的 ``classification_report``），不在这里抄第二份。
    """
    from trajectory_pipeline.evaluation.golden.alignment import equiv_class
    from trajectory_pipeline.evaluation.golden.builder import classification_report

    keys = sorted(
        k for k, e in baseline.items()
        if e.get("dimension") == HARD_DIM
        and _split_of(e, default_headline=default_headline) == split
        and k in current
        and _split_of(current[k], default_headline=default_headline) == split
        # 判不了的条目不带标签，混进去会让某类的 recall 莫名掉
        and VERDICT_SIDES.get(str(e.get("verdict"))) != SIDE_UNVERIFIABLE
        and VERDICT_SIDES.get(str(current[k].get("verdict"))) != SIDE_UNVERIFIABLE
    )
    if not keys:
        return {}
    b_gold = [equiv_class(str(baseline[k].get("gold_value"))) for k in keys]
    b_pred = [equiv_class(str(baseline[k].get("pred_value"))) for k in keys]
    c_gold = [equiv_class(str(current[k].get("gold_value"))) for k in keys]
    c_pred = [equiv_class(str(current[k].get("pred_value"))) for k in keys]
    rep_b = classification_report(b_gold, b_pred)
    rep_c = classification_report(c_gold, c_pred)
    out: dict[str, Any] = {}
    for cls in sorted(set(rep_b["per_class"]) | set(rep_c["per_class"])):
        b = rep_b["per_class"].get(cls) or {}
        c = rep_c["per_class"].get(cls) or {}
        b_support = int(b.get("tp", 0)) + int(b.get("fn", 0))
        c_support = int(c.get("tp", 0)) + int(c.get("fn", 0))
        f1_b, f1_c = b.get("f1"), c.get("f1")
        delta = (
            float(f1_c) - float(f1_b)
            if f1_b is not None and f1_c is not None
            else None
        )
        out[cls] = {
            "support_baseline": b_support,
            "support_current": c_support,
            "f1_baseline": f1_b,
            "f1_current": f1_c,
            "delta": delta,
            # 基线里没有这一类（support 0）⇒ 无可比对象，不下回归结论
            "comparable": b_support > 0 and delta is not None,
            "regressed": (
                bool(delta is not None and delta < -float(class_tolerance) - 1e-12)
            ),
        }
    return out


# --------------------------------------------------------------------------
# 四条判据
# --------------------------------------------------------------------------


def _criterion_net(net: int, stats: Mapping[str, Any]) -> dict[str, Any]:
    if not int(stats["paired"]):
        return {
            "status": CRITERION_NOT_EVALUABLE,
            "detail": (
                "两轮没有一条配对上的头条样本（golden_id 里带 run id，重采集的行"
                "配不上）——没有证据不能主张改善"
            ),
        }
    detail = (
        f"净改善 {net}（修好 {stats['fixed']} − 反转 {stats['regressed']}；"
        f"配对 {stats['paired']} 条）"
    )
    if int(stats["unverifiable"]):
        detail += f"；{stats['unverifiable']} 条两轮之一判不了，未计入"
    return {
        "status": CRITERION_PASS if net > 0 else CRITERION_FAIL,
        "detail": detail,
    }


def _criterion_class(
    per_class: Mapping[str, Mapping[str, Any]], tolerance: float
) -> dict[str, Any]:
    comparable = {k: v for k, v in per_class.items() if v.get("comparable")}
    if not comparable:
        return {
            "status": CRITERION_NOT_EVALUABLE,
            "detail": "没有一类有可比基线（F1 两轮都算不出来）——类回归判不了",
        }
    dropped = {
        k: v for k, v in comparable.items() if v.get("regressed")
    }
    if dropped:
        detail = "；".join(
            f"{k} F1 {v['f1_baseline']:.3f} → {v['f1_current']:.3f}"
            f"（Δ {v['delta']:+.3f}）"
            for k, v in sorted(dropped.items())
        )
        return {"status": CRITERION_FAIL, "detail": f"类回归：{detail}"}
    return {
        "status": CRITERION_PASS,
        "detail": (
            f"{len(comparable)} 类无 F1 下降（容差 {tolerance:g}；"
            f"基线无支撑的类不参与比较）"
        ),
    }


def _criterion_regression(stats: Mapping[str, Any]) -> dict[str, Any]:
    if not int(stats["paired"]):
        if not int(stats.get("entries_current") or 0):
            detail = (
                "本轮快照没有 regression 档条目（这一版还没切回归集）——"
                "回归门判不了，不能按'零回归'放行"
            )
        else:
            detail = "回归集一条都没配上——冻结的行对不上，判不了"
        return {"status": CRITERION_NOT_EVALUABLE, "detail": detail}
    detail = (
        f"回归集 regressed {stats['regressed']}/{stats['paired']}"
        f"（修好 {stats['fixed']} · 仍对 {stats['unchanged']}）"
    )
    return {
        "status": CRITERION_PASS if not int(stats["regressed"]) else CRITERION_FAIL,
        "detail": detail,
    }


def _criterion_bucket(
    *,
    triage: Sequence[Mapping[str, Any]] | None,
    targeted: Sequence[str],
    residual: Mapping[str, int],
    n_conflicts: int,
    n_assigned: int,
) -> dict[str, Any]:
    """同桶清零：**本轮**目标桶的残留分歧必须为 0。

    三件事按顺序判：

    1. 目标桶有残留 → **不过**（这就是"没清零"）；
    2. 残留为 0，但**分诊表没覆盖全部分歧** → **判不了**——那 0 可能是
       "剩下的分歧恰好没人分桶"，不是"清干净了"；
    3. 覆盖全、目标桶 0 残留 → 过。

    分诊数据是 C-05 的产物，现在还没有。没有就记 ``not_evaluable`` 且
    **不参与判定**——但不能标成 pass：那会把"没判"读成"清零了"。
    """
    if triage is None:
        return {
            "status": CRITERION_NOT_EVALUABLE,
            "enforced": False,
            "detail": (
                "未给 --triage（分诊表是 C-05 的产物）：本项 **n/a，不算通过**，"
                "也不参与判定"
            ),
        }
    if not targeted:
        raise VerifyError(
            "给了 --triage 就必须给 --bucket：不知道这次改动针对哪个桶，"
            "就无法判「该桶清零」——不猜"
        )
    left = {b: int(residual.get(b, 0)) for b in targeted}
    detail = " · ".join(f"{b} 残留 {n}" for b, n in left.items())
    if any(left.values()):
        return {"status": CRITERION_FAIL, "enforced": True, "detail": detail}
    unassigned = int(n_conflicts) - int(n_assigned)
    if unassigned > 0:
        return {
            "status": CRITERION_NOT_EVALUABLE,
            "enforced": True,
            "detail": (
                f"{detail}，但分诊表只覆盖了 {n_assigned}/{n_conflicts} 条本轮分歧"
                f"（缺 {unassigned} 条）——剩下的可能就在目标桶里，判不了"
            ),
        }
    if not int(n_conflicts):
        detail += "（本轮调参池没有残留分歧）"
    return {"status": CRITERION_PASS, "enforced": True, "detail": detail}


def _gate(criteria: Mapping[str, Mapping[str, Any]]) -> tuple[str, list[str]]:
    """门 = 所有**参与判定**的判据都过。``not_evaluable`` 对必判项即驳回。"""
    reasons: list[str] = []
    for name, crit in criteria.items():
        enforced = crit.get("enforced", True)
        status = crit.get("status")
        if not enforced:
            continue
        if status == CRITERION_PASS:
            continue
        prefix = "判不了" if status == CRITERION_NOT_EVALUABLE else "不过"
        reasons.append(f"{name} {prefix}：{crit.get('detail')}")
    return (GATE_REJECT if reasons else GATE_PASS), reasons


# --------------------------------------------------------------------------
# 总装
# --------------------------------------------------------------------------


def analyze(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    triage: Sequence[Mapping[str, Any]] | None = None,
    buckets: Sequence[str] = (),
    class_tolerance: float = 0.0,
) -> dict[str, Any]:
    """两份快照 → 报告（含四条判据与门结论）。纯函数，不读盘、不打印。"""
    meta = _check_comparable(baseline, current)
    b_idx = index_entries(baseline)
    c_idx = index_entries(current)
    default_headline = not (
        _has_any_split(b_idx.values()) or _has_any_split(c_idx.values())
    )

    # 换档守卫：C-17 切档之后逐档不可比，跨档差分必须拒绝而不是给一个数。
    moved_keys = [
        k for k in b_idx.keys() & c_idx.keys()
        if _split_of(b_idx[k], default_headline=default_headline)
        != _split_of(c_idx[k], default_headline=default_headline)
    ]
    if moved_keys:
        # 报**行数**：一条行在快照里有多条判定（d10 + 各软维度），
        # 只报条数会让人以为换档的行比实际多几倍。
        moved_rows = sorted({k[0] for k in moved_keys})
        raise VerifyError(
            f"{len(moved_rows)} 行（{len(moved_keys)} 条判定）在两轮之间换了档"
            f"（如 {moved_rows[0]}）：切档后逐档不可比。"
            "C-17 切回归集本身就会让这些行从 calibration 换到 regression——"
            "拿**同一份切分**的两轮快照比（切档后的版本 vs 它的后继），"
            "不要拿切档前的版本当基线"
        )

    dims = sorted(
        {str(e.get("dimension")) for e in b_idx.values()}
        | {str(e.get("dimension")) for e in c_idx.values()}
    )
    splits = [SPLIT_CALIBRATION, SPLIT_REGRESSION, SPLIT_HOLDOUT]
    per_split: dict[str, dict[str, Any]] = {}
    for split in splits:
        dim_stats: dict[str, Any] = {}
        for dim in dims:
            stats = diff_entries(
                b_idx, c_idx,
                dimension=dim, split=split,
                default_headline=default_headline,
            )
            if stats["paired"] or stats["only_baseline"] or stats["only_current"]:
                dim_stats[dim] = stats
        per_split[split] = {"dimensions": dim_stats}

    calib = per_split[SPLIT_CALIBRATION]["dimensions"].get(HARD_DIM) or _empty_diff(
        SPLIT_CALIBRATION
    )
    reg = per_split[SPLIT_REGRESSION]["dimensions"].get(HARD_DIM) or _empty_diff(
        SPLIT_REGRESSION
    )

    # 回归集是冻结资产：键集或金标变了就不判（退出码 2），不报"回归 0 条"。
    if reg["only_baseline"] or reg["only_current"]:
        raise VerifyError(
            "两轮回归集不是同一批行（基线独有 "
            f"{reg['only_baseline']} 条 / 本轮独有 {reg['only_current']} 条）："
            "回归集不得改动（C-17）。这份快照是不是在两轮之间重切过？"
        )
    if reg["paired"]:
        reg_gold_changed = int(reg["attribution"]["gold_changed"]) + int(
            reg["attribution"]["both_changed"]
        )
        if reg_gold_changed:
            raise VerifyError(
                f"回归集金标在两轮之间变了 {reg_gold_changed} 条：金标变动 = 新基线，"
                "不能拿来算本次改动效果（§10：改金标要新版本 + 人裁）"
            )

    per_class = _per_class(
        b_idx, c_idx,
        split=SPLIT_CALIBRATION,
        default_headline=default_headline,
        class_tolerance=class_tolerance,
    )

    # 同桶清零的残留：本轮调参池里仍有分歧的条目，按分诊表归桶。
    residual: dict[str, int] = {}
    n_conflicts = n_assigned = 0
    if triage is not None:
        # 分诊行在这里**再校验一遍**：`load_triage` 是 CLI 的入口，
        # 而 `analyze` 是公开 API——两条路都得过同一套规矩（六桶 / 理由必填 /
        # bucket 只能人填），否则从库内直接传一组 dict 就绕开了 C-05。
        try:
            triage_rows = validate_triage_rows(triage)
        except TriageError as exc:
            raise VerifyError(str(exc)) from exc
        bucket_of: dict[tuple[str, str], str] = {
            (str(r["golden_id"]), str(r["dimension"])): str(r["bucket"])
            for r in triage_rows
        }
        for key, entry in c_idx.items():
            if entry.get("dimension") != HARD_DIM:
                continue
            if _split_of(entry, default_headline=default_headline) != SPLIT_CALIBRATION:
                continue
            if entry.get("verdict") not in DIFF_VERDICTS:
                continue
            n_conflicts += 1
            bucket = bucket_of.get(key)
            if bucket:
                n_assigned += 1
                residual[bucket] = residual.get(bucket, 0) + 1

    criteria = {
        "net_improvement": _criterion_net(int(calib["net"]), calib),
        "class_regression": _criterion_class(per_class, class_tolerance),
        "bucket_clear": _criterion_bucket(
            triage=triage,
            targeted=list(buckets),
            residual=residual,
            n_conflicts=n_conflicts,
            n_assigned=n_assigned,
        ),
        "regression_regressed_zero": _criterion_regression(reg),
    }
    gate, reasons = _gate(criteria)

    holdout = per_split[SPLIT_HOLDOUT]["dimensions"].get(HARD_DIM) or {}
    return {
        "gate": gate,
        "reasons": reasons,
        "ruler": meta,
        "class_tolerance": float(class_tolerance),
        "criteria": criteria,
        "calibration": {
            "d10": calib,
            "d10_ratio": {
                "baseline": _ratio(_agreed(calib), int(calib["paired"])),
                "current": _ratio(
                    _agreed(calib) - int(calib["fixed"]) + int(calib["regressed"]),
                    int(calib["paired"]),
                ),
            },
            "per_class": per_class,
            "other_dimensions": {
                d: s for d, s in per_split[SPLIT_CALIBRATION]["dimensions"].items()
                if d != HARD_DIM
            },
        },
        "regression": {
            "d10": reg,
            "other_dimensions": {
                d: s for d, s in per_split[SPLIT_REGRESSION]["dimensions"].items()
                if d != HARD_DIM
            },
        },
        # 🔒 持有集只报条数：一旦出比率就有人拿去和头条比（D-C5）
        "holdout": {
            "paired": int(holdout.get("paired", 0)),
            "note": "只报条数，不判——封存纪律（D-C5）",
            # 切档按行（Q-12）⇒ 这 N 条不是 N 条独立证据。**数字不在这里
            # 重算**：快照里没有 URL（entry 不带 url），跨档泄漏的条数由
            # ``agreement --version N`` 的「URL 级金标读数」出——那里是
            # 唯一真源，重复一份必然漂。
            "independence_note": (
                "切档按行（Q-12）⇒ 这 N 条不是 N 条独立证据：同一 URL 可同时"
                "落在调参池/回归集与持有集。跨档泄漏条数见 "
                "`agreement --version N` 的「URL 级金标读数」"
            ),
        },
        "default_headline": default_headline,
    }


# --------------------------------------------------------------------------
# 打印
# --------------------------------------------------------------------------


def _bar(side: str) -> str:
    return {"ok": "✅", "bad": "❌", "unverifiable": "➖"}.get(side, "?")


def _crit_mark(status: str) -> str:
    return {
        CRITERION_PASS: "✅",
        CRITERION_FAIL: "❌",
        CRITERION_NOT_EVALUABLE: "➖",
    }.get(status, "?")


def _diff_line(stats: Mapping[str, Any]) -> str:
    return (
        f"配对 {stats['paired']} 条 · 修好 {stats['fixed']} · 反转 {stats['regressed']}"
        f" · 净 {int(stats['net']):+d} · 仍对 {stats['unchanged']}"
        f" · 仍错 {stats['still_wrong']} · 判不了 {stats['unverifiable']}"
        f" · 未配对 {stats['only_baseline']}/{stats['only_current']}"
    )


def render(report: Mapping[str, Any]) -> str:
    """人读版。**先数字，后判据，最后门**——读的人先看改了什么，再看够不够格合。"""
    ruler = report["ruler"]
    b, c = ruler["baseline"], ruler["current"]
    lines: list[str] = []
    lines.append(
        f"内环验证（方案 16 C-08）：v{b['golden_version']} → v{c['golden_version']}"
    )
    lines.append(
        f"  尺子：基线 rubric v{b['rubric_version'] or '未记录'}"
        f" · judge {b['evaluator_version'] or '未记录'}"
        f"  →  本轮 rubric v{c['rubric_version'] or '未记录'}"
        f" · judge {c['evaluator_version'] or '未记录'}"
    )
    same_ruler = (
        b["rubric_version"] == c["rubric_version"]
        and b["evaluator_version"] == c["evaluator_version"]
    )
    if same_ruler:
        lines.append(
            "  ⚠ 两轮尺子完全相同：pred 侧取冻结行 code.branch（不由当前评分器重算），"
            "没换数据、没换金标时本门看不出变化"
        )
    if ruler["soft_tolerance_mismatch"]:
        lines.append(
            f"  ⚠ 两轮软维度容差不同（{b['tolerance']} / {c['tolerance']}）："
            "软维度读数不可直接比（不进判据）"
        )
    if report.get("default_headline"):
        lines.append(
            "  ⚠ 本轮快照未切档（整份无 split 标注）：全部条目按头条计"
        )

    calib = report["calibration"]["d10"]
    ratio = report["calibration"]["d10_ratio"]
    lines.append("")
    lines.append(f"  头条（{SPLIT_CALIBRATION} · d10）：{_diff_line(calib)}")
    if ratio["baseline"] is not None and ratio["current"] is not None:
        lines.append(
            f"    D-10（配对子集）：基线 {ratio['baseline']:.1%}"
            f" → 本轮 {ratio['current']:.1%}"
            f"（{(ratio['current'] - ratio['baseline']) * 100:+.1f} pp）"
        )
    attribution = calib["attribution"]
    if attribution["gold_changed"] or attribution["both_changed"]:
        lines.append(
            f"    ⚠ 金标变了 {attribution['gold_changed'] + attribution['both_changed']}"
            f" 条（预测变了 {attribution['pred_changed']}"
            f" · 两侧同变 {attribution['both_changed']}）：净改善不可归因到评估器"
            "（P2：一次只改一侧）"
        )
    per_class = report["calibration"]["per_class"]
    if per_class:
        lines.append(f"    per_class F1（等价类，容差 {report['class_tolerance']:g}）：")
        for cls, st in sorted(per_class.items()):
            if st["f1_baseline"] is None or st["f1_current"] is None:
                lines.append(
                    f"      {cls:16s} 基线无支撑（support "
                    f"{st['support_baseline']}）→ 不比较"
                )
                continue
            mark = "❌ 类回归" if st["regressed"] else ""
            lines.append(
                f"      {cls:16s} {st['f1_baseline']:.3f} → {st['f1_current']:.3f}"
                f"  {st['delta']:+.3f}  {mark}"
            )

    reg = report["regression"]["d10"]
    lines.append("")
    lines.append(f"  🛡 回归集（{SPLIT_REGRESSION} · d10）：{_diff_line(reg)}")
    lines.append(
        "    （三类计数 fixed / regressed / unchanged；仍错与判不了单列，"
        "不是「没变化」就是「判不了」）"
    )

    lines.append("")
    lines.append(
        f"  🔒 持有集：配对 {report['holdout']['paired']} 条"
        "（只报条数，不判——封存纪律 D-C5）"
    )
    lines.append(
        "    ⚠ 切档按行（Q-12）⇒ 这 N 条**不是 N 条独立证据**："
        "同一 URL 可同时落在调参池/回归集与持有集；跨档泄漏条数见 "
        "`agreement --version N` 的「URL 级金标读数」"
    )

    others = report["calibration"]["other_dimensions"]
    if others:
        lines.append("")
        lines.append("  软维度（只出读数，不设线——D-C9）：")
        for dim, st in sorted(others.items()):
            lines.append(f"    {dim}: {_diff_line(st)}")

    lines.append("")
    lines.append("  判据：")
    for name, crit in report["criteria"].items():
        enforced = crit.get("enforced", True)
        suffix = "" if enforced else "（未参与判定）"
        lines.append(
            f"    {_crit_mark(crit['status'])} {name:26s} {crit['detail']}{suffix}"
        )

    lines.append("")
    if report["gate"] == GATE_PASS:
        lines.append("  ✅ 通过：净改善 > 0、无类回归、回归集 regressed = 0")
    else:
        lines.append("  ❌ 驳回：")
        for reason in report["reasons"]:
            lines.append(f"     · {reason}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _resolve_snapshot(
    *, path: str, version: int, root: str, side: str
) -> pathlib.Path:
    if path and version:
        raise VerifyError(f"--{side} 与 --{side}-version 只能给一个")
    if path:
        p = pathlib.Path(path)
    elif version:
        p = pathlib.Path(root) / f"v{version}" / "snapshot.json"
    else:
        raise VerifyError(
            f"要指定 --{side}（快照路径）或 --{side}-version（版本号）"
        )
    if not p.exists():
        raise VerifyError(
            f"{side} 快照不存在：{p}\n先跑 agreement --version N --emit-disagreements"
        )
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="verify",
        description=(
            "内环验证：两轮快照差分 → 净改善 / 无类回归 / 同桶清零 / 回归集 regressed=0"
        ),
    )
    ap.add_argument("--baseline", default="", help="基线快照路径（snapshot.json）")
    ap.add_argument("--current", default="", help="本轮快照路径（snapshot.json）")
    ap.add_argument("--baseline-version", type=int, default=0, help="基线版本号")
    ap.add_argument("--current-version", type=int, default=0, help="本轮版本号")
    ap.add_argument(
        "--calibration-root",
        default=str(DEFAULT_SNAPSHOT_ROOT),
        help=f"派生快照根（默认 {DEFAULT_SNAPSHOT_ROOT}）",
    )
    ap.add_argument(
        "--class-tolerance",
        type=float,
        default=0.0,
        help=(
            "类回归容差：某类 F1 下降超过它即驳回。默认 0（任何下降都驳回）；"
            "放宽属阈值变更（D-C6，需 owner 签）"
        ),
    )
    ap.add_argument(
        "--triage",
        nargs="*",
        default=None,
        help=(
            "分诊表 JSONL（每行 golden_id / dimension / bucket）。给了它才判「同桶清零」；"
            "不给则该项记 n/a、不参与判定"
        ),
    )
    ap.add_argument(
        "--bucket",
        nargs="*",
        default=[],
        help="本次改动针对的桶（配 --triage）；每个都必须清零",
    )
    ap.add_argument("--json-out", default="", help="报告落成 JSON")
    args = ap.parse_args(argv)

    try:
        b_path = _resolve_snapshot(
            path=args.baseline, version=args.baseline_version,
            root=args.calibration_root, side="baseline",
        )
        c_path = _resolve_snapshot(
            path=args.current, version=args.current_version,
            root=args.calibration_root, side="current",
        )
        try:
            baseline = load_snapshot(b_path)
            current = load_snapshot(c_path)
        except (OSError, ValueError) as exc:
            raise VerifyError(f"读快照失败：{exc}") from exc
        report = analyze(
            baseline,
            current,
            triage=load_triage(args.triage),
            buckets=args.bucket or (),
            class_tolerance=args.class_tolerance,
        )
    except (VerifyError, TriageError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(render(report))
    if args.json_out:
        out = pathlib.Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"→ 报告已写 {out}")
    return 0 if report["gate"] == GATE_PASS else 1


__all__ = [
    "COUNT_KEYS",
    "CRITERION_FAIL",
    "CRITERION_NOT_EVALUABLE",
    "CRITERION_PASS",
    "DEFAULT_SNAPSHOT_ROOT",
    "GATE_PASS",
    "GATE_REJECT",
    "VERDICT_SIDES",
    "VerifyError",
    "analyze",
    "diff_entries",
    "index_entries",
    "main",
    "render",
]


if __name__ == "__main__":
    raise SystemExit(main())