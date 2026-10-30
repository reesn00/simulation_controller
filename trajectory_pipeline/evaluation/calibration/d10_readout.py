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

v16 实测（**调参池 128 条** D-10 行）：真一致 75 · 双方都没结论 31 · b 弃权
而 c 有结论 19 · 真冲突 3 ⇒ **真冲突只有 3 条（2.3%）**，其余 125 条是
「能力边界」或「都没结论」。

读数范围：**默认只读调参池**（头条档，C-04）
----------------------------------------------
持有集（D-C5「每个 rubric 版本只跑一次，平时只报条数」）与回归集
（D-C8「冻结、永不作调参依据」）是**封存面**——它们的**三态**读数是
**新信息**，报出来就等于拿封存面调参（看一眼就回不去）。

⇒ 本模块**默认只读调参池**；要看封存面必须显式 ``--show-sealed``，
且**封存面的行不进任何判定**（上岗线判定对象永远是
「调参池 × 人工署名子集 × 三态口径」）。

为什么是**旁挂**而不是改现口径
------------------------------
:func:`~trajectory_pipeline.evaluation.golden.alignment.is_equivalent` 是
快照 verdict 的判定依据，而快照是**回归判定的基线**
（``calibration/verify.py`` 吃它）。改它的语义 = 改基线口径 ⇒ 「这一轮
多出来的分歧」会被读成模型变差，且旧快照不能再当基线。

所以本模块**只读**：不改冻结档、不改 rubric 版本、不改快照 schema、
**不写任何产物**。三态判定落在**判定层**
（:func:`~trajectory_pipeline.evaluation.golden.alignment.hard_agreement`，
与软维度
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

⚠️ **弃权率必须与一致率并排报**。只报一致率会把「把 19 行能力边界从
分母里拿掉」读成「质量提升」——那是把排除项当成绩。

⚠️ **三态口径此前没有正式读数**——它是 2026-10-30 才接上上岗线的**新
口径**。所以同一批行上「旧口径 43.2% / 三态 88.9%」的 45.6pp 摆幅
**全是记账口径的差**（弃权算不算「不一致」），**不是代码质量的差**。

署名分层
--------
用 :func:`~trajectory_pipeline.evaluation.golden.signatures.classify_signature`
的现成三分（``human`` / ``automated`` / ``unknown``），**不另划一套**——
``HUMAN_SIGNATURES`` 与 LS 流程里「要人看一眼」的开关是同一份常量。
另有**明细展开**（按 ``reviewed_by`` 原值）只作可读性，**不作判定口径**：
``ai-assisted-inc33`` 被 ``classify_signature`` 归 ``automated``（模型起草、
人没逐条看），但它的一致率与 ``auto-validator-*`` 差得远，混在一起会
掩盖它。

⚠️ **上岗线只绑人工署名子集**：自动署名行（``auto-validator-*``）的标签
由验证器从**同一条** ``code.branch`` 派生，其一致率接近**自证**、不是独立
裁定；人署名的行才是独立裁定。

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
    iter_site_rows,
)
from trajectory_pipeline.evaluation.golden.alignment import (
    DETAIL_LABEL_MISMATCH,
    SIDE_BOTH,
    SIDE_GOLD,
    SIDE_MISSING,
    SIDE_PRED,
    STATE_AGREE,
    STATE_DISAGREE,
    STATE_UNDETERMINED,
    HardAgreement,
    equiv_class,
    hard_agreement,
    is_conclusive,
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
    SIDE_SPLITS,
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
# 三态原语：**在判定层，不在这里**
# ---------------------------------------------------------------------------
#
# ✅ 三态判定 (``hard_agreement`` / ``is_conclusive`` /
# ``NON_CONCLUSIVE_CLASSES``) **归 :mod:`trajectory_pipeline.evaluation.golden.alignment`**——
# 它是判定层的归属地，**单一真源**。owner 2026-10-30 裁定：
# 「不表达结论」的定义上移到判定层，各处只许 import，**不许留第二份**。
#
# 上移的三个理由：
#
# 1. **判定层才是它的家**：口径 C 在那里，三态是它「多问一句」的延伸，
#    放在别处等于把「有没有结论」的定义拆成两份。
# 2. **上岗线要用它**（``golden/builder.py`` 的 D-10 判定处 + 读数模块）——
#    两个消费方共用一份定义，才不会「读数说的和上线判的不是一回事」。
# 3. **本模块是只读读数**，判定语义不该由它定义。
#
# ⚠️ **三态是旁挂，不改口径 C**：``is_equivalent`` 原样复用，快照的
# verdict 判定不变（快照是 ``calibration/verify.py`` 的回归基线）。

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


def site_rows_of(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], int]:
    """**站点级已标注行** + 被排除的行数。

    判据走 :func:`...calibration.snapshot.iter_site_rows`——**同一处**
    （快照与 ``golden/builder.py::cmd_agreement`` 都用它），不在这儿再抄
    一份 ``source_kind != "p2_sample" and golden``。

    ⚠️ **口径必须与 builder 的 ``site`` 逐行一致**：D-10 的分母是「站点级
    已标注行」。轨迹级样本（``p2_sample``）没有可比的人工标签，未标注的行
    无从判对错——它们**不是「这一行算错」**，是**不在这把尺子上**。

    排除掉的行数**照样报出来**：读入 294 行、站点级 289 行——差的那 5 行
    去哪了要看得见，否则「分母怎么少了几行」只能靠猜。
    """
    site = list(iter_site_rows(rows))
    return site, len(rows) - len(site)


def classify_rows(
    rows: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """把冻结档行与快照对齐，逐行给出三态判定 + 归因方向 + 署名分层。

    对齐靠 ``golden_id``。**对齐不上的行不静默丢掉**：快照里有而冻结档里
    没有（或反过来）都会**报错**——那是两个东西对不上，不是「这一行不算」。

    行集先过 :func:`site_rows_of`（站点级已标注行），与 builder 的
    ``site`` 同一口径。
    """
    entries = _snapshot_entries(snapshot)
    site_rows, _excluded = site_rows_of(rows)
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in site_rows:
        gid = str(row.get("golden_id") or "")
        if not gid:
            raise ReadoutError("站点级行里有缺 golden_id 的——对齐键没了")
        if gid in by_id:
            raise ReadoutError(f"站点级行的 golden_id 重复：{gid}")
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
    # 反向也要核：冻结档有、快照没有 ⇒ 快照漏了行。两边都得在，
    # 「读数少了几行」这件事不能只靠对上一个方向。
    missing = sorted(gid for gid in by_id if gid not in entries)
    if missing:
        raise ReadoutError(
            f"冻结档里有 {len(missing)} 条站点级行在快照里没有对应条目"
            f"（前 5 个：{missing[:5]}）——快照不是这一版冻结档的"
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


def reading_of(bucket: Mapping[str, Any]) -> dict[str, Any]:
    """统计块 → **读数形状**（与 ``golden/builder.py`` 的三态读数同形）。

    形状固定为：``n`` / ``agree`` / ``disagree`` / ``undetermined`` /
    ``denominator`` / ``ratio`` / ``abstention_rate``。

    ⚠️ **``ratio`` 分母为 0 时是 ``None``**（JSON 的 ``null``）——**不写
    0.0**。``None ≠ 0``：分母为 0 是「读不出数」，不是「读出 0%」。

    两处（三态读数模块 / builder 的 ``--json-out``）用**同一套键名**，
    机器侧才不用写两套解析。
    """
    states = bucket["states"]
    return {
        "n": bucket["n"],
        "agree": states.get(STATE_AGREE, 0),
        "disagree": states.get(STATE_DISAGREE, 0),
        "undetermined": states.get(STATE_UNDETERMINED, 0),
        "denominator": bucket["denominator"],
        "ratio": bucket["agreement_three_state"],
        "abstention_rate": bucket["abstention_rate"],
    }


def _distribution(values: Iterable[str]) -> dict[str, int]:
    """一组标签的分布（按数量降序，同数按标签名排）。

    空标签记成 ``(空)``——「没有标签」与「标签叫 (空)」不是一回事，
    但都不该悄悄消失。
    """
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "") or "(空)"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


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
    sealed: bool = False,
) -> dict[str, Any]:
    """汇总成一份可 JSON 化的读数。

    顶层分四层看：全体 / 按档 / 按署名分类 / 按署名原值（明细）。

    ⚠️ **默认只读调参池**（``sealed=False``）：持有集（D-C5）与回归集
    （D-C8）是**封存面**——它们的**三态**读数是**新信息**，报出来就等于
    拿封存面调参（看一眼就回不去）。要看封存面必须显式 ``--show-sealed``，
    且**封存面的行不进任何判定**。
    """
    threshold_name = "d10_min_agreement"
    site_rows, excluded = site_rows_of(rows) if rows else ([], 0)
    scoped_views = list(views) if sealed else [
        v for v in views if str(v["split"]) == HEADLINE_SPLIT
    ]
    scoped_rows = list(rows)
    if rows and not sealed:
        scoped_rows = [
            r for r in rows if str(r.get("__split__") or "") == HEADLINE_SPLIT
        ]
    report: dict[str, Any] = {
        "meta": {
            "dimension": HARD_DIM,
            "golden_version": version,
            "snapshot": str(snapshot_path),
            "golden_root": str(golden_root),
            # ⚠️ **行集口径**：分母只认「站点级已标注行」（与 builder 的
            # ``site`` 同一判据）。``rows_site`` 是**行集大小**（全部档），
            # ``rows_in_scope`` 是**本次读数覆盖的行数**（默认只调参池）。
            # 两个数分开报：不说清就会把「行集 289」读成「读了这 289 行」。
            "rows": len(scoped_views),
            "rows_read": len(rows),
            "rows_site": len(site_rows) if rows else len(scoped_views),
            "rows_in_scope": len(scoped_views),
            "rows_excluded": excluded,
            "row_set": "站点级已标注行（source_kind != p2_sample 且有 golden）",
            # 读数覆盖哪一档——**封存面默认不读**（D-C5 / D-C8）。
            "split_scope": "all" if sealed else HEADLINE_SPLIT,
            "sealed_shown": bool(sealed),
            "sealed_splits": list(SIDE_SPLITS),
        },
        "threshold": {
            "name": threshold_name,
            "value": thresholds.value(threshold_name),
            "status": thresholds.status(threshold_name),
            "scope": thresholds.scope(threshold_name),
            "basis": thresholds.basis(threshold_name),
        },
        "overall": _tally(scoped_views),
        "by_split": _group(scoped_views, lambda v: v["split"]),
        "by_signature_kind": _group(scoped_views, lambda v: v["signature_kind"]),
        "by_signature_raw": _group(scoped_views, lambda v: v["signature"]),
        "by_split_and_kind": {
            f"{split}/{kind}": _tally(
                [
                    v
                    for v in scoped_views
                    if v["split"] == split and v["signature_kind"] == kind
                ]
            )
            for split in sorted({str(v["split"]) for v in scoped_views})
            for kind in (KIND_HUMAN, KIND_AUTOMATED, KIND_UNKNOWN)
        },
    }
    if scoped_rows and success_label:
        report["self_check"] = self_check(
            report, legacy_recompute(scoped_rows, success_label=success_label)
        )
    # ── 上岗线绑定（owner 2026-10-30 裁定）────────────────────────
    # **数值仍是 0.80**（没裁新数值）；变的是**判定对象**：
    # 「调参池 × 人工署名子集 × 三态口径」。
    #
    # 为什么**只读调参池**：上岗线是**调参依据**（它决定这条管线能不能
    # 上岗）⇒ 只能读调参池。持有集（D-C5「每个 rubric 版本只跑一次，
    # 平时只报条数」）与回归集（D-C8「冻结、永不作调参依据」）是**封存面**
    # ——读它们 = 拿封存面调参 = 看一眼就回不去。
    #
    # ⚠️ **封存面的行不进任何判定**：``--show-sealed`` 打开时它们只作
    # **展示**，判定对象永远是调参池 × 人工 × 三态。
    #
    # ⚠️ 所以判定读数**不能**取 ``by_signature_kind[human]``（那个桶在
    # ``--show-sealed`` 下是**全档**人工行 ⇒ 会把封存面读进判定），
    # 必须**点名取调参池那一档**。
    bound_human = report["by_split_and_kind"].get(
        f"{HEADLINE_SPLIT}/{KIND_HUMAN}"
    )
    line_value = float(thresholds.value(threshold_name))
    bound_reading = reading_of(bound_human) if bound_human else None
    bound_ratio = (bound_reading or {}).get("ratio")
    report["line"] = {
        "value": line_value,
        "scope": thresholds.scope(threshold_name),
        "scope_text": thresholds.scope_text(threshold_name),
        "reading": bound_reading,
        # 分母为 0 ⇒ 不可判定（``None ≠ 0``）：**不判过线**，
        # ``passed`` 是 ``None`` 而不是 ``False``。
        "determinable": bound_ratio is not None,
        "passed": (
            None if bound_ratio is None else bool(bound_ratio >= line_value)
        ),
        # 判定对象的分档切片：**只有调参池**（封存面不进判定）。
        "by_split": {
            split: reading_of(bucket)
            for split, bucket in (
                (
                    split,
                    report["by_split_and_kind"].get(f"{split}/{KIND_HUMAN}"),
                )
                for split in [HEADLINE_SPLIT]
            )
            if bucket
        },
    }
    # 头条档（C-04：只有调参池进分母）**保留**——历史报表印的是它，
    # 删了就读不成「质量变了」。
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
    # ── 人工子集的**分支分布** ────────────────────────────────────
    # 「这条线为什么不过」必须**由产物自己说**，不能只写在文档里。
    #
    # 只看一致率会读成「金标与代码打架」；分支分布才看得出真相反：
    # 弃权行的**金标侧全都有结论**（人亲自定了性），是**代码侧留了
    # unresolved**——采集侧没判出来。于是瓶颈指向采集侧的 unresolved
    # 桶，而不是「金标错了」或「模型变差」。
    # ⚠️ 用 ``scoped_views``（不是 ``views``）：封存面默认不读，
    # 「人工子集的分支分布」若跨封存面，等于把封存面的分支构成也读出来。
    human_views = [v for v in scoped_views if v["signature_kind"] == KIND_HUMAN]
    abstention_views = [
        v for v in human_views if v["state"] == STATE_UNDETERMINED
    ]
    report["human_subset"] = {
        "n": len(human_views),
        "pred_branch_distribution": _distribution(
            v["raw_pred_branch"] for v in human_views
        ),
        "gold_label_distribution": _distribution(
            v["raw_gold_label"] for v in human_views
        ),
        "abstention": {
            "n": len(abstention_views),
            "pred_branch_distribution": _distribution(
                v["raw_pred_branch"] for v in abstention_views
            ),
            "gold_label_distribution": _distribution(
                v["raw_gold_label"] for v in abstention_views
            ),
            # 弃权行的金标**有没有结论**：全有 ⇒ 不是「人也没判出来」。
            "gold_conclusive": sum(
                1 for v in abstention_views if is_conclusive(v["raw_gold_label"])
            ),
        },
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


def _dist_text(dist: Mapping[str, Any]) -> str:
    """分布 → 一行「标签 数量 · 标签 数量」。"""
    if not dist:
        return "（无）"
    return " · ".join(f"{k} {v}" for k, v in dist.items())


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
    excluded = int(meta.get("rows_excluded") or 0)
    lines.append(
        f"行集：{meta.get('row_set', '')} · **站点级 {meta['rows_site']} 行**"
        + (
            f"（读入 {meta.get('rows_read', 0)} 行，排除 {excluded} 行："
            "轨迹级 p2_sample / 未标注——它们不在这把尺子上，不是「判错」）"
            if excluded
            else ""
        )
    )
    # ⚠️ **封存面默认不读**（D-C5 / D-C8）——要看得显式 ``--show-sealed``。
    if meta.get("sealed_shown"):
        lines.append(
            "⚠️⚠️ **封存面读数已展开**（--show-sealed）：持有集（D-C5）与回归集"
            "（D-C8）是**封存面**——看一眼就回不去（它们是冻结的对比基准，"
            "读过就等于「知道答案后再调参」）。以下封存面读数**仅供展示**，"
            "**不进任何判定**：上岗线判定对象永远是「调参池 × 人工署名子集 × "
            "三态口径」。"
        )
    else:
        lines.append(
            f"读数范围：**{meta.get('split_scope')}（调参池 = 头条档）· "
            f"{meta.get('rows_in_scope', meta.get('rows'))} 行**"
            "——持有集（D-C5）/ 回归集（D-C8）是封存面，**默认不读**"
            "（要看得显式 --show-sealed；开了也不进判定）"
        )
    threshold = report["threshold"]
    lines.append(
        f"上岗线：{threshold['value']} · {threshold['status']}"
        "（数值 2026-10-24 由 0.90 下调至 0.80，2026-10-30 **数值未动**、"
        "只改判定对象）"
    )
    lines.append(f"上岗线绑定对象（阈值表 scope）：{threshold['scope']}")
    lines.append("")
    lines.append(
        f"── 全体（本次读数范围 {meta.get('split_scope')} · "
        f"{meta.get('rows_in_scope', meta.get('rows'))} 行）──"
    )
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
    lines.append("── 分档 × 署名（三态口径）──")
    for name, bucket in report["by_split_and_kind"].items():
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
    line_block = report.get("line") or {}
    if head.get("all") or line_block:
        lines.append("")
        lines.append(
            "── 上岗线判定"
            f"（线 {threshold['value']} · 绑定对象 {line_block.get('scope', '—')}）──"
        )
        line = float(threshold["value"])
        bound = line_block.get("reading") or {}
        bound_ratio = bound.get("ratio")
        if bound_ratio is None:
            lines.append(
                "  ⚠ 不可判定（分母为 0：人工署名子集里没有双方都有结论的行）"
                "——**不判过线**（None ≠ 0）"
            )
        else:
            verdict = "✅ 过线" if bound_ratio >= line else "❌ **不过线**"
            lines.append(
                f"  判定对象（{line_block.get('scope', '—')}）："
                f"{bound.get('agree', 0)}/{bound.get('denominator', 0)}"
                f" = {_pct(bound_ratio)}   {verdict}"
            )
        # 分档切片**并排印**——只印一个数没人知道它为什么是这个数。
        for split, reading in (line_block.get("by_split") or {}).items():
            if not reading:
                continue
            lines.append(
                f"    · {split:<14} n={reading['n']:<4} "
                f"{reading['agree']}/{reading['disagree']}/{reading['undetermined']}"
                f"   一致率 {_pct(reading['ratio']):>7}"
                f"   弃权 {_pct(reading['abstention_rate']):>7}"
            )
        lines.append(
            "  ⓘ 为什么只读调参池（阈值表 basis）：上岗线是**调参依据**"
            "（它决定这条管线能不能上岗）⇒ 只能读调参池（C-04「头条 = 调参池」）。"
            "持有集（D-C5「每个 rubric 版本只跑一次，平时只报条数」）与回归集"
            "（D-C8「冻结、永不作调参依据」）是**封存面**——读它们 = 拿封存面"
            "调参 = 看一眼就回不去。"
        )
        # ⚠️ **分母薄要自己喊出来**——过线的数只有 18 行分母时，
        # 「✅ 过线」会被读成稳的。薄不薄是**事实**，由产物报，不靠人记得。
        if bound.get("denominator") is not None and bound["denominator"] < 20:
            lines.append(
                f"  ⚠ 分母薄：这条线的结论只有 {bound['denominator']} 行"
                f"（人工 {bound.get('n', 0)} 行里 {bound.get('undetermined', 0)} 行被代码弃权）"
                "⇒ 抽样波动大。　└ 下一步是**让分母变厚**，不是调线"
            )
        # 口径对照：**同一批行**，旧口径 vs 三态口径差多少——差在**记账**，
        # 不是代码质量的差。不把这个差摆出来，读数会被读成「质量变了」。
        legacy_human = head.get("human") or {}
        legacy_human_ratio = legacy_human.get("legacy_agreement")
        legacy_human_n = legacy_human.get("n")
        if legacy_human_ratio is not None and bound_ratio is not None:
            lines.append(
                f"  ⇢ 口径对照：同一批 {legacy_human_n} 行人工行，旧口径 C 读 "
                f"{legacy_human.get('legacy_agreed', 0)}/{legacy_human_n} = "
                f"{_pct(legacy_human_ratio)}（弃权被算成「不一致」）；三态口径读 "
                f"{bound.get('agree', 0)}/{bound.get('denominator', 0)} = "
                f"{_pct(bound_ratio)} ⇒ 差在**记账口径**（弃权算不算不一致），"
                "不是代码质量的差。"
            )
        lines.append(
            "  ── 旧读数（保留：跨版本对比用；**不再是上岗线的判定对象**）──"
        )
        kinds = report["by_signature_kind"]
        head_all = head.get("all") or {}
        all_is_headline = not meta.get("sealed_shown")
        overall_label = (
            "现口径 · 全体（= 头条档；封存面未读）"
            if all_is_headline
            else "现口径 · 全体（三档合计）"
        )
        kind_scope = "（= 头条档；封存面未读）" if all_is_headline else "（三档合计）"
        rows = [
            ("现口径 · 头条（历史报表印的就是它）", head_all.get("legacy_agreement")),
            (overall_label, report["overall"]["legacy_agreement"]),
            ("三态口径 · 头条", head_all.get("agreement_three_state")),
            ("三态口径 · 全体", report["overall"]["agreement_three_state"]),
            (
                f"三态口径 · 人工署名子集{kind_scope}",
                (kinds.get(KIND_HUMAN) or {}).get("agreement_three_state"),
            ),
            (
                f"三态口径 · 自动署名子集{kind_scope}",
                (kinds.get(KIND_AUTOMATED) or {}).get("agreement_three_state"),
            ),
        ]
        for label, value in rows:
            if value is None:
                continue
            mark = "过线" if value >= line else "**不过线**"
            lines.append(f"  {label:<32} {_pct(value):>7}   {mark}")
        lines.append(
            "  ⚠️ 同一个线数，换了绑定对象读数就不一样——"
            "「82.8% 过线」说的是**旧口径**（口径 C × 头条档），"
            "现在这条线读的是「调参池 × 人工署名子集 × 三态口径」。"
        )
    human = report.get("human_subset") or {}
    if human.get("n"):
        abstention = human.get("abstention") or {}
        lines.append("")
        lines.append(
            "── 人工子集的分支分布"
            f"（本次读数范围 {meta.get('split_scope')} · {human['n']} 行"
            "——弃权是谁给的）──"
        )
        lines.append(f"    代码侧：{_dist_text(human['pred_branch_distribution'])}")
        lines.append(f"    金标侧：{_dist_text(human['gold_label_distribution'])}")
        lines.append(
            f"    弃权 {abstention.get('n', 0)} 行 · 代码侧："
            f"{_dist_text(abstention.get('pred_branch_distribution'))}"
        )
        lines.append(
            f"    弃权 {abstention.get('n', 0)} 行 · 金标侧："
            f"{_dist_text(abstention.get('gold_label_distribution'))}"
        )
        gold_conclusive = int(abstention.get("gold_conclusive") or 0)
        if abstention.get("n"):
            if gold_conclusive == abstention["n"]:
                lines.append(
                    f"    ⇒ 弃权行的金标 {gold_conclusive}/{abstention['n']} 条"
                    "**都有结论**（人亲自定了性）⇒ 这条线不是被「判错」压下去的，"
                    "是被**代码没判出来**压下去的（采集侧的 unresolved 桶）"
                )
            else:
                lines.append(
                    f"    ⇒ 弃权行里只有 {gold_conclusive}/{abstention['n']} 条"
                    "金标有结论——两侧都没判出来的那部分另算"
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
    ap.add_argument(
        "--show-sealed",
        action="store_true",
        help=(
            "**破封**：把持有集（D-C5）/ 回归集（D-C8）的读数也展开。"
            "默认不展开——封存面是冻结的对比基准，看一眼就回不去"
            "（读过 = 拿封存面调参）。开了只作**展示**，**不进任何判定**"
        ),
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
            sealed=bool(args.show_sealed),
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
