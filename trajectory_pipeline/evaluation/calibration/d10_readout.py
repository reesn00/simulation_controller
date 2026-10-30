"""D-10 三态口径只读读数——「代码弃权」不等于「代码判错」。

它回答的问题
------------
现口径的 D-10 一致率把**三类不同性质**的行压进了一档：

============================  ==================================  ==========
情形                          语义                                现口径
============================  ==================================  ==========
两侧都判出了结论且等价        **真一致**                          agree
**两侧都没判出结论**          谁都没判出来——**不是「一致」**       agree ✗
**b 弃权而 c 有结论**         b 的**能力边界**——不是「判错」       disagree ✗
两侧都有结论但不等价          **真冲突**                          disagree
============================  ==================================  ==========

「b 弃权」指的是 ``code.branch`` 落在 ``unresolved`` / ``player_unverified``
这类**不表达结论**的分支上——采集侧按 fail-closed 纪律「判不出就不判」。
把它算成 ``disagree`` 等于**惩罚守纪律的那一方**。

v16 实测（289 条 D-10 行）：真一致 160 · 双方都没结论 70 · b 弃权而 c 有
结论 43 · 真冲突 15 ⇒ **真冲突只有 15 条（5.2%）**，其余 274 条是「能力
边界」或「都没结论」。

为什么是**旁挂**而不是改现口径
------------------------------
:func:`~trajectory_pipeline.evaluation.golden.alignment.is_equivalent` 是
快照 verdict 的判定依据，而快照是**回归判定的基线**
（``calibration/verify.py`` 吃它）。改它的语义 = 改基线口径 ⇒ 「这一轮
多出来的分歧」会被读成模型变差，且旧快照不能再当基线。

所以本模块**只读**：不改冻结档、不改 rubric 版本、不改快照 schema、
**不写任何产物**。三态判定落在**本模块**的
:func:`hard_agreement`（与软维度
:func:`~trajectory_pipeline.evaluation.golden.alignment.dim_agreement`
的 ``None`` ⇒ 不进分母同形），读数是**补充口径**，与现口径并排报。

⚠️ **判定层（``golden/alignment.py``）原样复用、不改**：等价类表
（:data:`~trajectory_pipeline.evaluation.golden.alignment.EQUIV_CLASS`）与
:func:`~trajectory_pipeline.evaluation.golden.alignment.is_equivalent` 都是
现成的**唯一出处**——本模块只**在它之外**多问一句「这一条到底有没有
结论」，不另抄一份映射（抄两份必然漂）。

分母口径
--------
三态口径下**只有两侧都判出结论的行进分母**（``agree`` + ``disagree``）。
弃权行进的是**另一本账**（采集侧覆盖率 / 判读侧未决），不混进一致率——
这与项目纪律「``unresolved`` 不是负样本」「score=None != score=0」一致。

⚠️ **弃权率必须与一致率并排报**。只报一致率会把「把 43 行能力边界从
分母里拿掉」读成「质量提升到 91.4%」——那是把排除项当成绩。

署名分层
--------
用 :func:`~trajectory_pipeline.evaluation.golden.signatures.classify_signature`
的现成三分（``human`` / ``automated`` / ``unknown``），**不另划一套**——
``HUMAN_SIGNATURES`` 与 LS 流程里「要人看一眼」的开关是同一份常量。
另有**明细展开**（按 ``reviewed_by`` 原值）只作可读性，**不作判定口径**：
``ai-assisted-inc33`` 被 ``classify_signature`` 归 ``automated``（模型起草、
人没逐条看），但它的一致率与 ``auto-validator-*`` 差得远，混在一起会
掩盖它。

跨面可比
--------
沿用快照的 ``surface_comparable`` 三态（任一面未声明 ⇒ 不可判定，不给
``False``）。本模块**不重算**面，只读快照那一栏。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

from trajectory_pipeline.evaluation.calibration.snapshot import (
    HARD_DIM,
    VERDICT_AGREE,
    VERDICT_DETAIL_MISMATCH,
)
from trajectory_pipeline.evaluation.golden.alignment import (
    equiv_class,
    is_equivalent,
)
from trajectory_pipeline.evaluation.golden.signatures import (
    KIND_AUTOMATED,
    KIND_HUMAN,
    KIND_UNKNOWN,
    classify_signature,
)
from trajectory_pipeline.evaluation.golden.splits import (
    HEADLINE_SPLIT,
    SPLIT_FILES,
    SPLITS,
)
from trajectory_pipeline.evaluation.scorers import thresholds

#: 黄金集根（冻结档在它下面）。与 ``golden/builder.py`` 的 ``GOLDEN_ROOT``
#: 同一处。
GOLDEN_ROOT = pathlib.Path("trajectory_pipeline/evaluation/golden/data")

#: 产物根（快照在 ``<root>/calibration/v{N}/snapshot.json``）。
PIPELINE_ROOT = pathlib.Path("trajectory_pipeline/output/pipeline")

#: 版本台账（``versions.jsonl``）——「最新版本」的依据。
VERSIONS_FILE = "versions.jsonl"

#: 现口径算「一致」的两个 verdict（与 ``calibration/snapshot.py`` 的
#: ``summarize_snapshot`` 同一对，不另写）。
LEGACY_AGREED_VERDICTS = (VERDICT_AGREE, VERDICT_DETAIL_MISMATCH)


class ReadoutError(RuntimeError):
    """读数失败——**退 2**，不是「读成 0%」。"""


# ---------------------------------------------------------------------------
# 三态原语
# ---------------------------------------------------------------------------
#
# ⚠️ **落在本模块，不落在 ``golden/alignment.py``**——两条理由：
#
# 1. ``alignment.py`` 是**判定层**（``is_equivalent`` 是快照 verdict 的
#    依据，快照又是回归判定的基线）。在那里加语义 = 动基线口径 ⇒「这轮
#    多出来的分歧」会被读成模型变差。本任务是**只读读数**，不该动基线。
# 2. ``alignment.py`` 正在被别的 session 改（软维度白名单口径）。
#    在那儿动手会把两笔 diff 混进同一个改动集，回滚时连带误伤。
#
# ✅ **等价类仍然只有一份**：下面用的是 ``alignment.equiv_class``，
# 不另抄映射表（抄两份必然漂）。判定层的 ``is_equivalent`` 原样复用
# ——本模块只是**在它之外**多问一句「这一条到底有没有结论」。

#: **不表达结论**的等价类。
#:
#: - ``UNKNOWN``（``unresolved`` / ``player_unverified`` / ``unknown``）：
#:   采集侧按 fail-closed「判不出就不判」留下的分支——**没有结论**。
#: - ``ROUTED``（``site_search_routed``）：**分支记账**——派发了子候选，
#:   结论由子候选自己的 visit 记（NON_SAMPLE，不入池）。父行上的它不是
#:   「这个 URL 能不能播」的结论。
#:
#: ⚠️ **``TRAILER`` 不在其中**：``trailer_suspect``（「疑似预告」）虽然
#: **不是负样本**（那是「不进训练负样本池」的意思，见 ``alignment``
#: 的合并注记），但它**是**一个结论（不是可播资源）。把「不是负样本」
#: 读成「没有结论」是两件事——本模块按「有没有结论」分，不按「进不进
#: 负样本池」分。
NON_CONCLUSIVE_CLASSES: frozenset[str] = frozenset({"UNKNOWN", "ROUTED"})

#: 三种状态。
STATE_AGREE = "agree"
STATE_DISAGREE = "disagree"
STATE_UNDETERMINED = "undetermined"

#: ``undetermined`` 的归因方向——**是谁没给出结论**。
SIDE_BOTH = "both"      # 两侧都没结论
SIDE_PRED = "pred"      # b（代码）没结论
SIDE_GOLD = "gold"      # c（金标）没结论
SIDE_MISSING = "missing"  # 任一侧压根没值

#: 一致但**标签字面不同**（同一等价类）——细分对账要它。
DETAIL_LABEL_MISMATCH = "detail_mismatch"


class HardAgreement(NamedTuple):
    """一条硬分类对比的三态判定结果。"""

    #: ``agree`` / ``disagree`` / ``undetermined``。
    state: str
    #: ``undetermined`` 时的归因方向（见 :data:`SIDE_*`）；其余状态为空串。
    side: str
    #: ``agree`` 且标签字面不同时为 ``detail_mismatch``，否则空串。
    detail: str

    @property
    def counts_in_denominator(self) -> bool:
        """这一条**进不进一致率分母**。

        只有 ``agree`` / ``disagree`` 进——「没结论」不是「一致」也不是
        「不一致」，它**不进分母**（同 ``dim_agreement`` 的 ``None``
        口径：``None ≠ 0``）。
        """
        return self.state in (STATE_AGREE, STATE_DISAGREE)


def is_conclusive(label: str | None) -> bool:
    """这个标签**表达结论**吗。

    ``False`` ⇔ 标签的等价类在 :data:`NON_CONCLUSIVE_CLASSES` 里，或标签为空。

    未登记的标签 → ``KeyError``（fail-closed，``equiv_class`` 的口径）：
    静默当成「有结论」会让新分支悄悄进分母。
    """
    text = (label or "").strip()
    if not text:
        return False
    return equiv_class(text) not in NON_CONCLUSIVE_CLASSES


def hard_agreement(gold_label: str | None, pred_label: str | None) -> HardAgreement:
    """金标标签 ↔ 代码分支的三态判定。

    - 两侧都有结论且同等价类 → ``agree``（标签字面不同再记
      ``detail_mismatch``）；
    - 两侧都有结论但不同等价类 → ``disagree``；
    - 任一侧没结论 → ``undetermined`` + 归因方向。
    """
    gold = (gold_label or "").strip()
    pred = (pred_label or "").strip()
    if not gold or not pred:
        return HardAgreement(STATE_UNDETERMINED, SIDE_MISSING, "")
    gold_conclusive = is_conclusive(gold)
    pred_conclusive = is_conclusive(pred)
    if not gold_conclusive and not pred_conclusive:
        return HardAgreement(STATE_UNDETERMINED, SIDE_BOTH, "")
    if not pred_conclusive:
        return HardAgreement(STATE_UNDETERMINED, SIDE_PRED, "")
    if not gold_conclusive:
        return HardAgreement(STATE_UNDETERMINED, SIDE_GOLD, "")
    if equiv_class(gold) == equiv_class(pred):
        detail = "" if gold == pred else DETAIL_LABEL_MISMATCH
        return HardAgreement(STATE_AGREE, "", detail)
    return HardAgreement(STATE_DISAGREE, "", "")


# ---------------------------------------------------------------------------
# 载入
# ---------------------------------------------------------------------------


def _read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ReadoutError(f"{path} 不存在")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            text = line.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ReadoutError(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
    return rows


def latest_version(golden_root: pathlib.Path) -> int:
    """版本台账里**最新**的冻结版本号。

    台账读不出来 → :class:`ReadoutError`（**不猜**「最新就是最大的目录名」——
    目录名排序与台账顺序不是一回事）。
    """
    path = golden_root / VERSIONS_FILE
    rows = _read_jsonl(path)
    versions = [int(r["version"]) for r in rows if r.get("version") is not None]
    if not versions:
        raise ReadoutError(f"{path} 里没有可用的 version 记录")
    return max(versions)


def load_frozen_rows(
    golden_root: pathlib.Path, version: int
) -> list[dict[str, Any]]:
    """读某冻结版本的三档行（按 :data:`SPLIT_FILES` 读，标注 ``__split__``）。

    与 ``golden/builder.py`` 的 ``cmd_agreement`` 同源：同样的档位常量
    （:mod:`...golden.splits`）、同样的 ``__split__`` 标注、同样的「档文件
    不存在就跳过」（老版本没有 ``regression.jsonl``）。
    """
    vdir = golden_root / f"v{version}"
    if not vdir.exists():
        raise ReadoutError(f"冻结档目录 {vdir} 不存在")
    rows: list[dict[str, Any]] = []
    for split in SPLITS:
        path = vdir / SPLIT_FILES[split]
        if not path.exists():
            continue
        for row in _read_jsonl(path):
            row["__split__"] = split
            rows.append(row)
    if not rows:
        raise ReadoutError(f"{vdir} 下没有样本行")
    return rows


def load_snapshot(path: pathlib.Path) -> dict[str, Any]:
    """读快照（``calibration/v{N}/snapshot.json``）。

    文件不在 → :class:`ReadoutError`，**并说清去哪儿找**——worktree 里
    ``output/`` 是空的，读数会静默变成 0%，那比报错更坏。
    """
    if not path.exists():
        raise ReadoutError(
            f"快照 {path} 不存在。快照是产物（``output/`` 不入版本控制），"
            f"worktree 里通常没有——用 --snapshot 指到主树的产物路径，"
            f"或先在主树跑一次 agreement"
        )
    with path.open(encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ReadoutError(f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or "entries" not in data:
        raise ReadoutError(f"{path} 不像快照文件（缺 entries 栏）")
    return data


def snapshot_facts(
    snapshot: Mapping[str, Any], *, version: int
) -> dict[str, Any]:
    """从快照 ``meta`` 取读数要用的几项，并**核对它跟冻结档是同一版**。

    对不上（快照是 v15、冻结档是 v16）→ :class:`ReadoutError`：
    拿错版本的快照去读冻结档，读出来的一致率是**两版混起来的**数，
    而且看不出异常。
    """
    meta = snapshot.get("meta") or {}
    got_version = meta.get("golden_version")
    if got_version is not None and int(got_version) != int(version):
        raise ReadoutError(
            f"快照的 golden_version={got_version}，与指定的冻结档 v{version} "
            f"不是同一版——读出来的是混版读数"
        )
    hard_dim = str(meta.get("hard_dim") or HARD_DIM)
    if hard_dim != HARD_DIM:
        raise ReadoutError(f"快照的 hard_dim={hard_dim}，本读数只读 {HARD_DIM}")
    success_label = str(meta.get("success_label") or "")
    if not success_label:
        raise ReadoutError("快照 meta 里没有 success_label（缺了兜底标签的口径）")
    return {
        "golden_version": got_version,
        "rubric_version": meta.get("rubric_version"),
        "evaluator_version": meta.get("evaluator_version"),
        "code_version": meta.get("code_version"),
        "schema_version": snapshot.get("schema_version"),
        "success_label": success_label,
    }


# ---------------------------------------------------------------------------
# 逐行分类
# ---------------------------------------------------------------------------


def _snapshot_entries(
    snapshot: Mapping[str, Any], dimension: str = HARD_DIM
) -> dict[str, Mapping[str, Any]]:
    """快照里该维度的条目 → ``golden_id`` → 条目。

    重复的 ``golden_id`` → :class:`ReadoutError`（**不静默取最后一条**：
    同一个 id 出现两次说明快照有毛病，合并掉就看不出「哪一条变了」）。
    """
    out: dict[str, Mapping[str, Any]] = {}
    for entry in snapshot.get("entries") or []:
        if entry.get("dimension") != dimension:
            continue
        gid = str(entry.get("golden_id") or "")
        if not gid:
            raise ReadoutError("快照里有条目缺 golden_id")
        if gid in out:
            raise ReadoutError(f"快照里 {dimension} 的 golden_id 重复：{gid}")
        out[gid] = entry
    return out


def classify_rows(
    rows: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """把冻结档行与快照对齐，逐行给出三态判定 + 归因方向 + 署名分层。

    对齐靠 ``golden_id``。**对齐不上的行不静默丢掉**：快照里有而冻结档里
    没有（或反过来）都会被记下来——那是两个东西对不上，不是「这一行不算」。
    """
    entries = _snapshot_entries(snapshot)
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        gid = str(row.get("golden_id") or "")
        if gid:
            by_id[gid] = row

    out: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for gid, entry in entries.items():
        row = by_id.get(gid)
        if row is None:
            unmatched.append(gid)
            continue
        gold = row.get("golden") or {}
        code = row.get("code") or {}

        # pred/gold **取快照的值**（快照用的是归一化后的 pred_value，与
        # 现口径逐字对齐）；同时把**原始行**的结构化结论另记一栏——
        # ``code.branch`` 为空时快照会兜底成 success（``_hard_entry`` 的
        # ``or success_label``），那一栏必须能被看见，否则「b 根本没输出」
        # 会被读成「b 判成功」。
        pred_value = str(entry.get("pred_value") or "")
        gold_value = str(entry.get("gold_value") or "")
        raw_pred = str(code.get("branch") or "")
        raw_gold = str(gold.get("label") or "")

        state = hard_agreement(gold_value, pred_value)
        signature = str(gold.get("reviewed_by") or "")

        out.append(
            {
                "golden_id": gid,
                "split": str(entry.get("split") or row.get("__split__") or ""),
                "gold_value": gold_value,
                "pred_value": pred_value,
                "raw_gold_label": raw_gold,
                "raw_pred_branch": raw_pred,
                "raw_pred_missing": not raw_pred,
                "signature": signature,
                "signature_kind": classify_signature(signature),
                "state": state.state,
                "side": state.side,
                "detail": state.detail,
                "legacy_verdict": str(entry.get("verdict") or ""),
                "surface_comparable": entry.get("surface_comparable"),
            }
        )
    if unmatched:
        raise ReadoutError(
            f"快照里有 {len(unmatched)} 条 {HARD_DIM} 条目在冻结档里找不到对应行"
            f"（前 5 个：{unmatched[:5]}）——快照与冻结档不是同一版"
        )
    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def _blank_bucket() -> dict[str, Any]:
    return {
        "n": 0,
        "states": {STATE_AGREE: 0, STATE_DISAGREE: 0, STATE_UNDETERMINED: 0},
        "undetermined_side": {
            SIDE_BOTH: 0,
            SIDE_PRED: 0,
            SIDE_GOLD: 0,
            SIDE_MISSING: 0,
        },
        "legacy_agreed": 0,
        "raw_pred_missing": 0,
        "raw_pred_missing_legacy_agreed": 0,
        "surface_comparable_none": 0,
    }


def _tally(views: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """一组行的统计块。

    - ``denominator`` = ``agree`` + ``disagree``（**两侧都判出结论**的行）；
    - ``agreement_three_state`` = ``agree`` / ``denominator``；
    - ``abstention_rate`` = ``undetermined`` / ``n``（**必须与一致率并排读**）；
    - ``legacy_agreement`` = 现口径（分母全量），用于跟现读数逐字对照。
    """
    bucket = _blank_bucket()
    for view in views:
        bucket["n"] += 1
        state = str(view["state"])
        bucket["states"][state] = bucket["states"].get(state, 0) + 1
        if state == STATE_UNDETERMINED:
            side = str(view["side"])
            bucket["undetermined_side"][side] = (
                bucket["undetermined_side"].get(side, 0) + 1
            )
        if str(view["legacy_verdict"]) in LEGACY_AGREED_VERDICTS:
            bucket["legacy_agreed"] += 1
        if view["raw_pred_missing"]:
            bucket["raw_pred_missing"] += 1
            if str(view["legacy_verdict"]) in LEGACY_AGREED_VERDICTS:
                # 兜底**碰巧**与金标同侧——数对了，但数对的不是 b。
                bucket["raw_pred_missing_legacy_agreed"] += 1
        if view["surface_comparable"] is None:
            bucket["surface_comparable_none"] += 1

    n = bucket["n"]
    denominator = bucket["states"][STATE_AGREE] + bucket["states"][STATE_DISAGREE]
    bucket["denominator"] = denominator
    bucket["agreement_three_state"] = (
        bucket["states"][STATE_AGREE] / denominator if denominator else None
    )
    bucket["abstention_rate"] = (
        bucket["states"][STATE_UNDETERMINED] / n if n else None
    )
    bucket["legacy_agreement"] = bucket["legacy_agreed"] / n if n else None
    bucket["legacy_denominator"] = n
    return bucket


def _group(
    views: Sequence[Mapping[str, Any]], key
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for view in views:
        groups.setdefault(str(key(view)), []).append(view)
    return {name: _tally(rows) for name, rows in sorted(groups.items())}


def legacy_recompute(
    rows: Sequence[Mapping[str, Any]],
    *,
    success_label: str,
) -> dict[str, dict[str, Any]]:
    """**从原始行重算现口径**（照抄 ``golden/builder.py::cmd_agreement`` 的公式）。

    这一步是**自检**：本模块的「现口径」列是从快照派生的，而上岗线绑的
    是 ``cmd_agreement`` 报的数。两边必须逐字相等——不等就说明快照与
    builder 已经对不上了（那本身就是个要报的问题）。

    公式与 ``cmd_agreement`` 逐字一致：
    ``is_equivalent(gold_label or success_label, code.branch or success_label)``，
    分母 = 站点级已标注行（``source_kind != "p2_sample"`` 且有 ``golden``）。
    ``success_label`` 取自**快照自己的 meta**（不是另抄一个字面量）——
    快照记的是它当时用的兜底标签，读数跟它一致才谈得上「同口径」。
    """
    site = [
        r
        for r in rows
        if r.get("source_kind") != "p2_sample" and r.get("golden")
    ]

    def _agreed(subset: Sequence[Mapping[str, Any]]) -> int:
        return sum(
            1
            for r in subset
            if is_equivalent(
                (r.get("golden") or {}).get("label") or success_label,
                (r.get("code") or {}).get("branch") or success_label,
            )
        )

    out: dict[str, dict[str, Any]] = {}
    for split in sorted({str(r.get("__split__") or "") for r in site}):
        subset = [r for r in site if str(r.get("__split__") or "") == split]
        agreed = _agreed(subset)
        out[split] = {
            "n": len(subset),
            "agreed": agreed,
            "ratio": agreed / len(subset) if subset else None,
        }
    total = _agreed(site)
    out["__all__"] = {
        "n": len(site),
        "agreed": total,
        "ratio": total / len(site) if site else None,
    }
    return out


def summarize(
    views: Sequence[Mapping[str, Any]],
    *,
    version: int,
    snapshot_path: pathlib.Path,
    golden_root: pathlib.Path,
    rows: Sequence[Mapping[str, Any]] = (),
    success_label: str = "",
) -> dict[str, Any]:
    """汇总成一份可 JSON 化的读数。

    顶层分四层看：全体 / 按档 / 按署名分类 / 按署名原值（明细）。
    """
    threshold_name = "d10_min_agreement"
    report: dict[str, Any] = {
        "meta": {
            "dimension": HARD_DIM,
            "golden_version": version,
            "snapshot": str(snapshot_path),
            "golden_root": str(golden_root),
            "rows": len(views),
        },
        "threshold": {
            "name": threshold_name,
            "value": thresholds.value(threshold_name),
            "status": thresholds.status(threshold_name),
            "basis": thresholds.basis(threshold_name),
        },
        "overall": _tally(views),
        "by_split": _group(views, lambda v: v["split"]),
        "by_signature_kind": _group(views, lambda v: v["signature_kind"]),
        "by_signature_raw": _group(views, lambda v: v["signature"]),
        "by_split_and_kind": {
            f"{split}/{kind}": _tally(
                [
                    v
                    for v in views
                    if v["split"] == split and v["signature_kind"] == kind
                ]
            )
            for split in sorted({str(v["split"]) for v in views})
            for kind in (KIND_HUMAN, KIND_AUTOMATED, KIND_UNKNOWN)
        },
    }
    if rows and success_label:
        report["self_check"] = self_check(
            report, legacy_recompute(rows, success_label=success_label)
        )
    # 上岗线绑的是**头条档**（C-04：只有调参池进分母）。把头条单独拎
    # 出来，并把「人工子集」那一栏也摆出来——现线读的是全体（其中 73%
    # 是自动署名的行），绑哪一档是个**待裁的口径问题**，不是本模块能
    # 替他定的事。
    report["headline"] = {
        "split": HEADLINE_SPLIT,
        "all": report["by_split"].get(HEADLINE_SPLIT),
        "human": report["by_split_and_kind"].get(
            f"{HEADLINE_SPLIT}/{KIND_HUMAN}"
        ),
        "automated": report["by_split_and_kind"].get(
            f"{HEADLINE_SPLIT}/{KIND_AUTOMATED}"
        ),
    }
    return report


def self_check(
    report: Mapping[str, Any], recomputed: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """快照派生的「现口径」列 vs 从原始行重算的现口径——逐档比对。

    不一致 ⇒ 快照与 ``cmd_agreement`` 已经对不上（快照是回归基线，
    对不上就是**基线要重算**的信号），读数必须把这件事摆出来。
    """
    checks: list[dict[str, Any]] = []
    for split, bucket in report["by_split"].items():
        got = recomputed.get(split)
        if not got:
            continue
        checks.append(
            {
                "split": split,
                "snapshot_derived": {
                    "n": bucket["n"],
                    "agreed": bucket["legacy_agreed"],
                    "ratio": bucket["legacy_agreement"],
                },
                "recomputed_from_rows": {
                    "n": got["n"],
                    "agreed": got["agreed"],
                    "ratio": got["ratio"],
                },
                "matches": (
                    bucket["n"] == got["n"]
                    and bucket["legacy_agreed"] == got["agreed"]
                ),
            }
        )
    overall = report["overall"]
    all_got = recomputed.get("__all__")
    checks.append(
        {
            "split": "__all__",
            "snapshot_derived": {
                "n": overall["n"],
                "agreed": overall["legacy_agreed"],
                "ratio": overall["legacy_agreement"],
            },
            "recomputed_from_rows": (
                {
                    "n": all_got["n"],
                    "agreed": all_got["agreed"],
                    "ratio": all_got["ratio"],
                }
                if all_got
                else None
            ),
            "matches": bool(
                all_got
                and overall["n"] == all_got["n"]
                and overall["legacy_agreed"] == all_got["agreed"]
            ),
        }
    )
    return {"checks": checks, "all_match": all(c["matches"] for c in checks)}


# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _bucket_lines(name: str, bucket: Mapping[str, Any]) -> list[str]:
    """一个统计块的读数行。"""
    states = bucket["states"]
    out = [
        f"{name:<26} n={bucket['n']:<4} "
        f"三态一致 {states[STATE_AGREE]:>4}/{bucket['denominator']:<4} "
        f"= {_pct(bucket['agreement_three_state']):>6}  "
        f"弃权 {states[STATE_UNDETERMINED]:>3} "
        f"= {_pct(bucket['abstention_rate']):>6}  "
        f"| 现口径 {_pct(bucket['legacy_agreement']):>6}"
    ]
    sides = bucket["undetermined_side"]
    if any(sides.values()):
        detail = " · ".join(f"{k}={v}" for k, v in sides.items() if v)
        out.append(f"    └ 弃权归因：{detail}")
    return out


def render_text(report: Mapping[str, Any]) -> str:
    """人读版。"""
    meta = report["meta"]
    lines: list[str] = []
    lines.append(
        f"D-10 三态口径读数（黄金集 v{meta['golden_version']} · "
        f"快照 {meta['snapshot']}）"
    )
    lines.append(
        "口径：一致率分母 = 两侧都判出结论的行；弃权行进另一本账（不进分母）"
    )
    threshold = report["threshold"]
    lines.append(
        f"上岗线（现口径用，owner 2026-10-24 从 0.90 下调）："
        f"{threshold['value']} · {threshold['status']}"
    )
    lines.append("")
    lines.append("── 全体 ──")
    lines.extend(_bucket_lines("全体", report["overall"]))
    lines.append("")
    lines.append("── 按档（calibration = 调参池 = 头条档）──")
    for name, bucket in report["by_split"].items():
        lines.extend(_bucket_lines(name, bucket))
    lines.append("")
    lines.append("── 按金标署名分类（classify_signature 三分）──")
    for name, bucket in report["by_signature_kind"].items():
        lines.extend(_bucket_lines(name, bucket))
    lines.append("")
    lines.append("── 按署名原值（明细，只读不作判定口径）──")
    for name, bucket in report["by_signature_raw"].items():
        lines.extend(_bucket_lines(name, bucket))
    lines.append("")
    lines.append("── 现口径 vs 三态口径（对照）──")
    overall = report["overall"]
    states = overall["states"]
    both_abstain = overall["undetermined_side"][SIDE_BOTH]
    lines.append(
        f"现口径：{overall['legacy_agreed']}/{overall['legacy_denominator']} = "
        f"{_pct(overall['legacy_agreement'])}"
        f"（分母全量；双方都没结论的 {both_abstain} 行算成一致）"
    )
    lines.append(
        f"三态口径：{states[STATE_AGREE]}/{overall['denominator']} = "
        f"{_pct(overall['agreement_three_state'])}"
        f"（分母只含双方都有结论的行；弃权 {states[STATE_UNDETERMINED]} 行）"
    )
    lines.append(
        f"真冲突（双方都有结论且不等价）：{states[STATE_DISAGREE]} 行"
        f" = {_pct(states[STATE_DISAGREE] / overall['n'] if overall['n'] else None)}"
        f"（占全体）"
    )
    if overall["raw_pred_missing"]:
        lines.append(
            f"⚠ b 的结构化结论缺失（code.branch 为空，快照兜底成 success）："
            f"{overall['raw_pred_missing']} 行 —— 这几行在现口径里被算成「b 判成功」"
            f"（其中 {overall['raw_pred_missing_legacy_agreed']} 行兜底**碰巧**与金标同侧："
            f"数对了，但数对的不是 b）"
        )
    if overall["surface_comparable_none"]:
        lines.append(
            f"⚠ 跨面不可判定（surface_comparable = None）："
            f"{overall['surface_comparable_none']} 行 —— 两侧的面都没记"
        )
    check = report.get("self_check")
    if check:
        lines.append("")
        lines.append("── 自检：快照派生的现口径列 vs 从原始行重算 ──")
        for c in check["checks"]:
            got = c["recomputed_from_rows"]
            derived = c["snapshot_derived"]
            flag = "✓" if c["matches"] else "✗"
            lines.append(
                f"{flag} {c['split']:<12} "
                f"快照 {derived['agreed']}/{derived['n']}"
                f"  重算 {got['agreed']}/{got['n']}"
            )
        lines.append(
            "（✓ = 现口径列与 golden/builder.py 的 cmd_agreement 逐字一致；"
            "不等说明快照与 builder 已对不上）"
        )
    head = report.get("headline") or {}
    if head.get("all"):
        lines.append("")
        lines.append(f"── 上岗线对照（线 {threshold['value']} · 绑的档：{head['split']}）──")
        line = threshold["value"]
        kinds = report["by_signature_kind"]
        head_all = head["all"]
        rows = [
            ("现口径 · 头条（线现在读的就是它）", head_all["legacy_agreement"]),
            ("现口径 · 全体（三档合计）", report["overall"]["legacy_agreement"]),
            ("三态口径 · 头条", head_all["agreement_three_state"]),
            ("三态口径 · 头条·人工署名子集", (head.get("human") or {}).get("agreement_three_state")),
            ("三态口径 · 头条·自动署名子集", (head.get("automated") or {}).get("agreement_three_state")),
            ("三态口径 · 全体", report["overall"]["agreement_three_state"]),
            ("三态口径 · 全体·人工署名子集", (kinds.get(KIND_HUMAN) or {}).get("agreement_three_state")),
            ("三态口径 · 全体·自动署名子集", (kinds.get(KIND_AUTOMATED) or {}).get("agreement_three_state")),
        ]
        for label, value in rows:
            if value is None:
                continue
            mark = "过线" if value >= line else "**不过线**"
            lines.append(f"  {label:<32} {_pct(value):>7}   {mark}")
        lines.append(
            "（本模块**不改线**——「线该绑全体还是绑人工子集」是待裁的口径问题）"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m trajectory_pipeline.evaluation.calibration.d10_readout",
        description=(
            "D-10 三态口径只读读数：把「代码弃权」与「真冲突」分开报。"
            "不改冻结档、不改 rubric 版本、不写任何产物。"
        ),
    )
    ap.add_argument(
        "--golden-root",
        default=str(GOLDEN_ROOT),
        help="黄金集根目录（默认 %(default)s）",
    )
    ap.add_argument(
        "--version",
        type=int,
        default=0,
        help="冻结版本号；不给 = versions.jsonl 里的最新一版",
    )
    ap.add_argument(
        "--snapshot",
        default="",
        help=(
            "快照路径；不给 = <pipeline-root>/calibration/v{version}/snapshot.json。"
            "worktree 里 output/ 是空的，需要显式指到主树的产物路径"
        ),
    )
    ap.add_argument(
        "--pipeline-root",
        default=str(PIPELINE_ROOT),
        help="产物根目录（默认 %(default)s）",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON（默认人读表格）",
    )
    ap.add_argument(
        "--with-rows",
        action="store_true",
        help="JSON 里带上逐行明细（默认不带）",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)

    golden_root = pathlib.Path(args.golden_root)
    version = args.version or latest_version(golden_root)
    snapshot_path = (
        pathlib.Path(args.snapshot)
        if args.snapshot
        else pathlib.Path(args.pipeline_root) / "calibration" / f"v{version}" / "snapshot.json"
    )

    try:
        rows = load_frozen_rows(golden_root, version)
        snapshot = load_snapshot(snapshot_path)
        facts = snapshot_facts(snapshot, version=version)
        views = classify_rows(rows, snapshot)
        report = summarize(
            views,
            version=version,
            snapshot_path=snapshot_path,
            golden_root=golden_root,
            rows=rows,
            success_label=facts["success_label"],
        )
        report["meta"].update(
            {
                "rubric_version": facts["rubric_version"],
                "evaluator_version": facts["evaluator_version"],
                "code_version": facts["code_version"],
                "snapshot_schema_version": facts["schema_version"],
                "success_label": facts["success_label"],
            }
        )
    except ReadoutError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    if args.with_rows:
        report["rows"] = list(views)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
