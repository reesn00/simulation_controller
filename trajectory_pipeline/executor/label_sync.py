"""把**存档里没跟上分支的标签**改对——历史存量的补救通道。

为什么需要它
------------
``branch`` 是**结论**、``branch_label`` 是结论的**中文名**——两者必须同源。
而 2026-11-04 之前有**三个**工具改 ``branch`` 却都忘了改标签：

* :mod:`executor.downgrade`（降级 → ``unresolved``）——2026-11-04 已修
  （见它的坑 9）；
* :mod:`executor.relabel`（改判 → 另一支负样本分支）——2026-11-04 修
  （:func:`executor.relabel._relabel_outcome`）；
* :mod:`executor.review_queue`（人工复核回填 → 另一支）——2026-11-04 修
  （:func:`executor.review_queue.apply_verdicts`）。

⇒ 存量存档里有**三批**记录是「分支说 A、标签写着 B」的。实测（2026-11-04，
生产根 ``output/pipeline/``）**137 条**（原档 **61** + 复核档 **76**，
72 份文件）：降级/改判留下的 **108** 条 · 人裁留下的 **29** 条。

⚠️ **下游取的是行内的 ``branch_label``**
（:func:`evaluation.golden.builder._code_block` →
:func:`evaluation.golden_ls.exporter` 的标签文本）⇒ 矛盾一路传到
Label Studio 的人核卡上：**人看到的是「付费墙」，机器读到的分支却是「未判定」**。

⚠️ **只修「工具改过分支」的那批，不修「口径文本漂移」的那批**
------------------------------------------------------------------
存档里另有一批标签与当前 :data:`executor.branches.BRANCH_LABELS` 对不上，
但**分支本身没被任何工具改过**——那是**口径演进**造成的：

* ``unreachable_hard``：2026-10-27 M-44 裁定把这一支收窄成「机器码证明
  站点不可达」，标签文本跟着改 ⇒ 历史存档与黄金集里那批行仍带旧文本
  「导航失败（网络/DNS/超时）」——**明确不回填**
  （新旧两代口径不可直接比，见 ``executor/branch_eras.py`` 的 e6）；
* ``login_wall_or_blocked``：2026-10-12 拆分成细分三支时给它加了
  「（deprecated：已拆分为细分）」后缀 ⇒ 历史行仍带旧文本。

⇒ **判据是「这条记录的分支被工具改过」**，不是「标签文本与当前表对不上」。
前者是**同一份结论的两个字段没同步**（该修）；后者是**两代口径**（不该修）。

⚠️ **本模块只改存档，不改池行**
--------------------------------
池行那一侧本来就对：:func:`executor.relabel._relabel_pool_row` 一直改标签，
而被降级的记录**已经出池**（``cli pool-drop``）——池里没有需要修的行。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from trajectory_pipeline.executor.branches import branch_label_of

__all__ = [
    "TOOL_STAMPS",
    "label_fixes",
    "sync_archive_labels",
    "sync_to_root",
]

#: 「分支被工具改过」的**留痕键**。这三个键由 :mod:`executor.downgrade` ·
#: :mod:`executor.relabel` · :mod:`executor.review_queue` 写入
#: （它们记录「这条最初被判成什么」）。
#:
#: ⚠️ **只认「键存在」，不认「值非空」**：降级一条**成功**的记录时
#: ``branch_before_downgrade`` 记的是 ``None``——键在、值为空，那**也是**
#: 「分支被工具改过」。
TOOL_STAMPS: tuple[str, ...] = ("branch_before_downgrade", "branch_before_relabel",
                                "branch_before_review")


def label_fixes(archive: dict[str, Any]) -> list[dict[str, Any]]:
    """这份存档里**该改标签**的记录。返回要改的条目（不改盘）。

    判据见模块 docstring：分支被工具改过、而标签没跟上。
    """
    out: list[dict[str, Any]] = []
    for rec in archive.get("outcomes") or ():
        if not isinstance(rec, dict):
            continue
        if not any(stamp in rec for stamp in TOOL_STAMPS):
            continue
        branch = rec.get("branch")
        want = branch_label_of(branch if isinstance(branch, str) else None)
        have = rec.get("branch_label")
        if have == want:
            continue
        out.append({
            "url": str(rec.get("url") or ""),
            "branch": branch,
            "branch_before": rec.get("branch_before_downgrade")
            if "branch_before_downgrade" in rec
            else rec.get("branch_before_relabel"),
            "label_before": have,
            "label_after": want,
        })
    return out


def sync_archive_labels(archive: dict[str, Any]) -> int:
    """把这份存档里没跟上分支的标签改对。返回改了**几条**。

    ⚠️ **只改标签，不碰结论**：``branch`` / ``evidence`` / ``ledger`` /
    ``visits`` 一个字都不动——本模块修的是「结论的两个字段没同步」，
    不是「结论错了」。结论错了走 :mod:`executor.downgrade` /
    :mod:`executor.relabel`，那是**另一层**的处置。
    """
    n = 0
    for rec in archive.get("outcomes") or ():
        if not isinstance(rec, dict):
            continue
        if not any(stamp in rec for stamp in TOOL_STAMPS):
            continue
        branch = rec.get("branch")
        want = branch_label_of(branch if isinstance(branch, str) else None)
        if rec.get("branch_label") == want:
            continue
        rec["branch_label"] = want
        n += 1
    return n


def _atomic_write(path: pathlib.Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)


def sync_to_root(root: pathlib.Path, *, dry_run: bool = True,
                 ) -> list[dict[str, Any]]:
    """遍历存档目录并把标签改对。返回每份文件的改动摘要。

    ⚠️ **两层都改**（原档 + ``.reviewed.json``）——复核档才是门禁与报表
    **实际读的那一份**（:func:`executor.archive.select_archives` 优先取它）
    ⇒ 只改原档 ⇒ 复核档还留着旧标签 ⇒ 门禁与报表看到的还是矛盾。
    """
    from trajectory_pipeline.executor.archive import REVIEWED_SUFFIX
    from trajectory_pipeline.executor.downgrade import (
        original_archive_paths,
        reviewed_archive_paths,
    )

    out: list[dict[str, Any]] = []
    paths = ([(p, "原档") for p in original_archive_paths(root)]
             + [(p, "复核档") for p in reviewed_archive_paths(root)])
    for path, layer in paths:
        try:
            archive = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(archive, dict):
            continue
        fixes = label_fixes(archive)
        if not fixes:
            continue
        summary = {"archive": path.name, "layer": layer, "fixed": len(fixes),
                   "fixes": fixes}
        if not dry_run:
            summary["fixed"] = sync_archive_labels(archive)
            _atomic_write(path, archive)
        out.append(summary)
    return out
