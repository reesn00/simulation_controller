"""golden_ls.reflow: LS 上的人工裁定 → **回流进金标**（方案 22 §9 R5）。

为什么回流是**单独一步**，不与上传混做
--------------------------------------
上传是「把金标搬上 LS」（只读金标，写 LS）；回流是「把人在 LS 上的裁定搬回
金标」（读 LS，写金标）。两者方向相反、后果不同 ⇒ **不合成一条命令**。

两类裁定，两条路
----------------
============================  ==========================================
``confirm``（标签不变）      人在 LS 上确认了金标（``agree``，或
                             ``disagree`` 但给的分支与金标**等价**）
                             ⇒ **只补观测面与观测时刻**（方案 20 §R2），
                             标签 / 署名 / 理由**一个字不动** ⇒ 走
                             ``freeze --refresh-surface``（**不换尺子**）
``relabel``（标签要改）      人在 LS 上说「代码判错了，该是 X」，而 X 与
                             金标**不等价** ⇒ 金标要改 ⇒ 走
                             ``freeze --refresh-label``（**换尺子 ⇒ 新基线**，
                             ``gate`` 退 2 是对的）
============================  ==========================================

⚠️ **两套署名分列（裁定④）**：LS 侧复核人（``annotator``）与金标侧署名
（``reviewed_by``）**不合并**。确认型**不改**金标侧署名——标签没变，
「谁当初定了这个标签」还是那个人；本次复核人的身份留在 LS 侧记录与本模块
生成的工作表里。改判型才需要**新署一个名**（那就是他重新定了标签）。

三栏必须由人填
--------------
LS 表单里**没有**观测面控件 ⇒ 面与时刻**推不出来** ⇒ 工作表把三栏留空：

- ``access_surface`` —— 在**哪张面**上看到的（方案 20 §R2 的词表）；
- ``observed_at`` —— **什么时候**看到的（面与时刻是一对）；
- ``reviewed_by``（**仅改判型**）—— 金标侧署名，必须是**人**的署名。

⚠️ 缺一栏 ⇒ **退 2**，**不按默认面补**（``unrecorded`` / ``human-browser``
都不许猜）——这是 ``builder annotate`` 的既有纪律（方案 20 §R2 O-d），
本模块只是把它前移到「人填工作表」这一步。

不回流什么
----------
* **holdout** —— 裁定③让 holdout 也上传、也被人看，但**裁定不许进金标**
  （它要保持「没被人看过」的封存状态）；
* **判不了的裁定**（``unverifiable``）—— 那是「这里读不出结论」，不是结论；
* **没复核的行** —— 「没复核」与「复核了但没结论」是两件事。

⚠️ **一律显式记原因**，不静默丢弃：被跳过的行要能回答「为什么它没进金标」。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from trajectory_pipeline.evaluation.access_surface import (
    ACCESS_SURFACES,
    check_surface,
    is_declared,
)
from trajectory_pipeline.evaluation.golden.signatures import HUMAN_SIGNATURES
from trajectory_pipeline.evaluation.golden.splits import SPLIT_HOLDOUT, SPLIT_REGRESSION
from trajectory_pipeline.evaluation.golden_ls.label_config import NOT_APPLICABLE
from trajectory_pipeline.evaluation.golden_ls.verdicts import UNKNOWN

#: 工作表里的两类可回流裁定。
KIND_CONFIRM = "confirm"
KIND_RELABEL = "relabel"

#: 跳过一类裁定的原因（**显式记，不静默丢弃**）。
SKIP_NOT_REVIEWED = "not_reviewed"
SKIP_UNVERIFIABLE = "unverifiable"
SKIP_NO_BRANCH = "no_branch"
SKIP_BRANCH_NA = "branch_not_applicable"
SKIP_HOLDOUT = "holdout_not_reflowed"
SKIP_LABEL_DRIFT = "label_drift"
SKIP_NO_GOLD = "no_gold_row"
SKIP_NO_ANNOTATION = "no_annotation_row"

#: LS 表单上三个裁决值（与 label_config 同源）。
VERDICT_AGREE = "agree"
VERDICT_DISAGREE = "disagree"
VERDICT_UNVERIFIABLE = "unverifiable"


class ReflowError(RuntimeError):
    """回流用法/状态错误 ⇒ **退 2**（不猜、不静默回落）。"""


def _equiv(left: str, right: str) -> bool:
    """两个标签是否语义等价（等价类真源在 ``golden/alignment.py``）。

    ⚠️ 用等价类而不是字面相等：``blocked`` 与 ``login_wall_or_blocked``
    判一致（对齐方案 §4.3）——字面比会把「其实一样」误判成「改判」，
    而误判成改判的后果是**白改一次金标 + 白耗一个版本号**。
    """
    from trajectory_pipeline.evaluation.golden.alignment import is_equivalent

    try:
        return bool(is_equivalent(left, right))
    except KeyError:
        # 标签不在等价类注册表里 ⇒ 不可判定 ⇒ 按「不等价」处理（改判），
        # 但**不猜**：apply 阶段会因标签不在宇宙里退 2。
        return False


def classify(
    record: Mapping[str, Any],
    *,
    gold_label: str,
    split: str,
    annotation_label: str,
) -> tuple[str, str]:
    """一条 LS 裁定 → ``(kind, reason)``。

    ``kind`` 取值：:data:`KIND_CONFIRM` / :data:`KIND_RELABEL` / ``"skip"``。
    ``reason`` 在 ``skip`` 时是跳过原因，在可回流时是一句话说明。
    """
    if split == SPLIT_HOLDOUT:
        return "skip", SKIP_HOLDOUT
    verdict = str(record.get("verdict") or "")
    if not verdict or verdict == UNKNOWN:
        return "skip", SKIP_NOT_REVIEWED
    if verdict == VERDICT_UNVERIFIABLE:
        return "skip", SKIP_UNVERIFIABLE
    if annotation_label != gold_label:
        # 标注里的 label 与冻结档不一致 ⇒ 「这条的金标现在是什么」本身
        # 有歧义 ⇒ 交回人，不猜。
        return "skip", SKIP_LABEL_DRIFT
    if verdict == VERDICT_AGREE:
        return KIND_CONFIRM, "人确认了金标（agree）"
    if verdict != VERDICT_DISAGREE:
        return "skip", SKIP_NOT_REVIEWED
    branch = str(record.get("human_branch") or "")
    if not branch or branch == UNKNOWN:
        return "skip", SKIP_NO_BRANCH
    if branch == NOT_APPLICABLE:
        return "skip", SKIP_BRANCH_NA
    if _equiv(branch, gold_label):
        return KIND_CONFIRM, f"人给的分支 {branch} 与金标 {gold_label} 等价"
    return KIND_RELABEL, f"人给的分支 {branch} 与金标 {gold_label} 不等价"


def _gold_label_of(row: Mapping[str, Any]) -> str:
    golden = row.get("golden")
    if isinstance(golden, Mapping):
        return str(golden.get("label") or "")
    if isinstance(golden, str):
        return golden
    return ""


def build_worksheet(
    records: Sequence[Mapping[str, Any]],
    *,
    gold_rows: Mapping[str, Mapping[str, Any]],
    ann_rows: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """LS 裁定 → **回流工作表**（每行一条待回流裁定）。

    工作表里**有三栏留空**（必须由人填）：``access_surface`` /
    ``observed_at``，改判型再加 ``reviewed_by``。预填的其余字段是
    **原标注原样带出**（标签 / 署名 / 时刻 / 理由 / 软维度分）——回流是
    「整行模式」的一行，原样带出才不会在写回时把别的字段清空。

    Returns:
        ``(工作表行, 读数)``。读数里 ``skipped`` 逐条记原因。
    """
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    counts = {KIND_CONFIRM: 0, KIND_RELABEL: 0}

    for record in records:
        gid = str(record.get("golden_id") or "")
        gold_row = gold_rows.get(gid)
        if gold_row is None:
            skipped.append({"golden_id": gid, "reason": SKIP_NO_GOLD})
            continue
        ann = ann_rows.get(gid)
        if ann is None:
            skipped.append({"golden_id": gid, "reason": SKIP_NO_ANNOTATION})
            continue
        gold_label = _gold_label_of(gold_row)
        split = str(gold_row.get("__split__") or record.get("split") or "")
        kind, reason = classify(
            record,
            gold_label=gold_label,
            split=split,
            annotation_label=str(ann.get("label") or ""),
        )
        if kind == "skip":
            skipped.append({"golden_id": gid, "reason": reason})
            continue
        counts[kind] = counts.get(kind, 0) + 1
        golden_map = gold_row.get("golden")
        golden_map = golden_map if isinstance(golden_map, Mapping) else {}
        row: dict[str, Any] = {
            "kind": kind,
            "golden_id": gid,
            "split": split,
            # ⚠️ 本轮要不要提交。默认要；人把某行标成 ``submit: false`` ⇒
            # 这一轮**不回流**（它的三栏还没齐——比如采集时刻要另外取）。
            # 跳过的行**留在工作表里**，不删 ⇒ 下轮接着办。
            "submit": True,
            "round": int(ann.get("round", 1)),
            # ⚠️ 标签一律取**冻结档**的（我们要改的是 v{N} 的金标）
            "label": gold_label,
            "label_label": str(golden_map.get("label_label") or ""),
            # 署名 / 时刻 / 理由：确认型**原样带出**（标签没变，署名就不该变）；
            # 改判型留空，由人填——「谁重新定了这个标签」是人的事实。
            "reviewed_by": (
                "" if kind == KIND_RELABEL else str(ann.get("reviewed_by") or "")
            ),
            "reviewed_at": str(ann.get("reviewed_at") or ""),
            "note": str(ann.get("note") or ""),
            # ⚠️ 两栏必须由人填（推不出来）
            "access_surface": "",
            "observed_at": "",
            # 软维度分原样带出（不覆盖人填的分数）
            "dimension_scores": ann.get("dimension_scores"),
            "interval_days": ann.get("interval_days"),
            # 留痕（**不进 annotations.jsonl**，cmd_annotate 不取这两栏）
            "ls_annotator": str(record.get("annotator") or ""),
            "ls_verdict": str(record.get("verdict") or ""),
            "ls_human_branch": str(record.get("human_branch") or ""),
            "ls_reason": str(record.get("reason") or ""),
            "ls_annotated_at": str(record.get("annotated_at") or ""),
            "reflow_reviewed_at": str(record.get("annotated_at") or ""),
            "why": reason,
        }
        if kind == KIND_RELABEL:
            # 改标签要**显式确认**（不是「有差异就改」）
            row["confirm_label_change"] = False
        rows.append(row)

    summary = {
        "total_records": len(records),
        "confirm": counts.get(KIND_CONFIRM, 0),
        "relabel": counts.get(KIND_RELABEL, 0),
        "skipped": len(skipped),
        "skipped_detail": skipped,
    }
    return rows, summary


def validate(
    rows: Sequence[Mapping[str, Any]],
    *,
    gold_rows: Mapping[str, Mapping[str, Any]],
    ann_rows: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """工作表 → **错误清单**（空清单 = 可写）。

    fail-closed 的五条：
    1. ``access_surface`` 必须是**声明过的合法面**（缺 ⇒ 退 2，不按默认面补）；
    2. ``observed_at`` 必须是 ISO-8601（面与时刻是一对）；
    3. 改判型的 ``reviewed_by`` 必须是**人**的署名（∈ ``HUMAN_SIGNATURES``）——
       否则这条「人核行」会静默退化成「机器写的」；
    4. 改判型必须 ``confirm_label_change`` 为真（改金标要显式点头）；
    5. **软维度分不许被清空**：工作表里这一栏是 ``None`` 而原标注有分数 ⇒
       退 2（整行模式的「整行」：不带它就等于把它清掉——清掉不报错，只是
       那一行的 d2/d4/d5/d6 悄悄没了）。

    另加两条硬约束：标签必须在标签宇宙内、**holdout 与回归集不回流**
    （C-17：回归集内容一经冻结不得改动）。
    """
    errors: list[str] = []
    from trajectory_pipeline.executor.branches import BRANCH_LABELS

    from trajectory_pipeline.evaluation.golden.builder import SUCCESS_LABEL

    universe = set(BRANCH_LABELS) | {SUCCESS_LABEL}
    for row in rows:
        if not row.get("submit", True):
            continue  # 本轮不提交的行**不校验**（它的三栏还没齐）
        gid = str(row.get("golden_id") or "")
        kind = str(row.get("kind") or "")
        gold_row = gold_rows.get(gid)
        split = str((gold_row or {}).get("__split__") or row.get("split") or "")
        if split == SPLIT_HOLDOUT:
            errors.append(f"{gid}：holdout 的裁定不进金标（裁定③只上传，不回流）")
            continue
        if split == SPLIT_REGRESSION:
            errors.append(
                f"{gid}：回归集里的行不得改动（C-17）——要改就先把这一行挪出回归集"
            )
            continue
        surface = str(row.get("access_surface") or "").strip()
        if not is_declared(surface):
            errors.append(
                f"{gid}：access_surface 空或认不出——在哪张面上看到的必须由人填"
                f"（合法值：{sorted(ACCESS_SURFACES)}）"
            )
        else:
            try:
                check_surface(surface)
            except ValueError as exc:
                errors.append(f"{gid}：{exc}")
        observed = str(row.get("observed_at") or "").strip()
        if not observed:
            errors.append(f"{gid}：observed_at 空——面与时刻是一对（站点会漂移）")
        else:
            from datetime import datetime

            try:
                datetime.fromisoformat(observed)
            except ValueError:
                errors.append(f"{gid}：observed_at={observed!r} 不是 ISO-8601 时刻")
        label = str(row.get("label") or "")
        if label not in universe:
            errors.append(f"{gid}：label={label!r} 不在标签宇宙里")
        if ann_rows is not None:
            original = ann_rows.get(gid) or {}
            old_dims = original.get("dimension_scores")
            new_dims = row.get("dimension_scores")
            if isinstance(old_dims, Mapping) and old_dims and not new_dims:
                errors.append(
                    f"{gid}：软维度分这一栏空着——原标注有 {dict(old_dims)}，"
                    "整行模式不带它就等于把它清掉（清掉不报错，只是分数悄悄没了）"
                )
        if kind == KIND_RELABEL:
            signer = str(row.get("reviewed_by") or "").strip()
            if not signer:
                errors.append(f"{gid}：改判要署金标侧的名字（reviewed_by 不能空）")
            elif signer not in HUMAN_SIGNATURES:
                errors.append(
                    f"{gid}：reviewed_by={signer!r} 不在人的署名集合里"
                    f"（{sorted(HUMAN_SIGNATURES)}）——否则这条会从「人核」"
                    "静默退化成「机器写的」"
                )
            if not row.get("confirm_label_change"):
                errors.append(
                    f"{gid}：改判需要显式确认（confirm_label_change=true）"
                    "——金标不可回改，只能开新版本 + 人裁"
                )
    return errors


def dump_jsonl(rows: Sequence[Mapping[str, Any]]) -> str:
    """工作表 → JSONL 文本（**一行一条**）。"""
    return "".join(
        json.dumps(dict(r), ensure_ascii=False) + "\n" for r in rows
    )


def write_annotations(
    rows: Sequence[Mapping[str, Any]],
    *,
    golden_root: str,
) -> dict[str, Any]:
    """工作表 → **写进 annotations.jsonl**（走 ``builder annotate`` 整行模式）。

    ⚠️ **不另写一套合并逻辑**：``builder annotate`` 是标注合并的唯一真源
    （它顺带校验面与时刻、标签宇宙、id 迁移映射表）⇒ 本模块只负责把
    「人填好的工作表」变成它的输入格式，再调它。

    Args:
        rows: 已通过 :func:`validate` 的工作表行。
        golden_root: 黄金集真源根。

    Returns:
        ``{"accepted": N, "annotation_file": "..."}``。
    """
    import argparse
    import pathlib
    import tempfile

    from trajectory_pipeline.evaluation.golden import builder

    rows = [r for r in rows if r.get("submit", True)]
    if not rows:
        raise ReflowError("工作表里没有一行是 submit=true（没有要写的）")
    payload = dump_jsonl(rows)
    with tempfile.NamedTemporaryFile(
        "wb", suffix=".jsonl", delete=False, dir=str(pathlib.Path(golden_root))
    ) as handle:
        handle.write(payload.encode("utf-8"))
        temp_path = pathlib.Path(handle.name)
    try:
        namespace = argparse.Namespace(
            golden_root=str(golden_root),
            input=str(temp_path),
            force=any(str(r.get("kind")) == KIND_RELABEL for r in rows),
            dimensions_only=False,
        )
        builder.cmd_annotate(namespace)
    finally:
        temp_path.unlink(missing_ok=True)
    ann_path = pathlib.Path(golden_root) / builder.ANNOTATIONS_FILE
    return {"accepted": len(rows), "annotation_file": str(ann_path)}


def submitted(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """本轮**要提交**的行（``submit`` 不为假的）。

    ⚠️ 被标成 ``submit: false`` 的行**留在工作表里**（不删）——它们的
    三栏还没齐（面与时刻推不出来），下轮接着办。删掉等于把「还没办」
    写成「不办了」。
    """
    return [dict(r) for r in rows if r.get("submit", True)]


__all__ = [
    "KIND_CONFIRM",
    "KIND_RELABEL",
    "ReflowError",
    "SKIP_BRANCH_NA",
    "SKIP_HOLDOUT",
    "SKIP_LABEL_DRIFT",
    "SKIP_NO_ANNOTATION",
    "SKIP_NO_BRANCH",
    "SKIP_NO_GOLD",
    "SKIP_NOT_REVIEWED",
    "SKIP_UNVERIFIABLE",
    "build_worksheet",
    "classify",
    "dump_jsonl",
    "validate",
    "write_annotations",
]
