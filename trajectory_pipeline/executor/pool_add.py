"""把「人工补判的真负样本」并进负样本池——**写入时的那一半**（2026-10-26）。

为什么需要它
------------
「每条分支都必须有对应样本入库」是硬要求，而**并池是写入时的动作**。
但写入点有两个，只有一个并池：

* :meth:`P1Archive.write`（跑完一条 run）⇒ 并池 ✅
* ``cli review --apply``（人工复核回填）⇒ **不并池** ❌

实测（2026-10-26）：人工把 ``unresolved`` 改判成 ``partial_free`` /
``subscription_wall`` / ``component_unverified`` 等**真负样本**后，复核档里
有了这条分支，**池里没有对应的行** ⇒ 门禁 ``check-archives`` 报
``negative_not_pooled``（Severity.DEGRADED）——实测 **30 条**，其中 29 条
是人工补判的。

三条既有处置路都补不上这个洞：

* :mod:`trajectory_pipeline.executor.downgrade`——目标是 ``unresolved``，
  方向是「移出池」；
* ``cli review --apply`` 只受理 :data:`~executor.review_queue.REVIEW_BRANCHES`
  （``unresolved`` / ``trailer_suspect`` / ``player_unverified``），
  **真负样本不在其列**；
* :mod:`trajectory_pipeline.executor.relabel`——把一条真负样本换成**另一条**
  真负样本，**前提是它已经在池里**（``in_pool``）。

⇒ 本模块补的就是「人工补判 ⇒ 并池」这一段。

三条硬纪律
----------
1. **默认 dry-run**——池是跨 run 累积的 append-only 资产，训练数据直接吃它
   （与 ``downgrade`` / ``relabel`` / ``pool_drop`` 同款口径）。
2. **有分歧不并，暴露给人看**——三条拒并条件（见 :data:`REFUSALS`）命中就
   **跳过并打印**冲突的那份存档与它的 evidence。CLAUDE.md 写着「禁止投票/
   集成，分歧必须暴露给人看」：把一条与成功记录矛盾的标签并进池子，等于
   替人把分歧投票掉了。
3. **口径与门禁一致**——存档集、负样本判定、去重键三样都照
   :func:`trajectory_pipeline.executor.integrity.check_archive` 的写法
   （复核档优先、``NON_SAMPLE_BRANCHES`` 与 ``COLLECTION_SIDE_BRANCHES``
   排除、键 ``(task_id, url, branch)``）。**口径不同 ⇒ 门禁说「已并池」而
   这里说「没并」**，那种分歧比没并池更难查。
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

from trajectory_pipeline.executor.archive import P1Archive, select_archives
from trajectory_pipeline.executor.branches import (
    BRANCH_LABELS,
    COLLECTION_SIDE_BRANCHES,
    NON_SAMPLE_BRANCHES,
)

__all__ = [
    "Candidate",
    "REFUSALS",
    "collect_candidates",
    "add_from_root",
]


#: 拒并理由的**封闭集**。三条都是「并进去就是造一条站不住的标签」。
#:
#: - ``quarantined``——整条 URL 已被人工判为拒收（``quarantine.jsonl``）。
#:   门禁也会跳过它，所以并它只会让池与人工裁定打架。
#: - ``contradicted``——**同一 URL 在别的存档里被判成功**。并进去就造出一条
#:   「这里看不了」正对着一条「这里能看」——训练数据里最毒的一种错。
#: - ``stale_branch``——池里**同 ``(task_id, url)`` 已有别的分支**：那是
#:   人工改判前的旧行（人工改分支时池侧没跟着改）。此时并新行会让池里
#:   同一个 ``(task_id, url)`` 挂两个分支，训练时读到哪条取决于顺序。
#:   正解是 ``cli relabel``（它两边一起改）。
REFUSALS = ("quarantined", "contradicted", "stale_branch")

#: 拒并理由的中文说明（人读）。
_REFUSAL_TEXT = {
    "quarantined": "整条 URL 已人工判为拒收（quarantine.jsonl）",
    "contradicted": "同一 URL 在别的存档里被判成功——并进去就自相矛盾",
    "stale_branch": "池里同址（task_id+url）已有别的分支——人工改判的旧行，"
                    "先走 cli relabel",
}


@dataclass
class Candidate:
    """一条「该进池但还没进池」的真负样本。"""

    task_id: str
    url: str
    branch: str
    evidence: str
    #: 命中的存档文件名（人读）。
    archive: str
    #: 池行的**成品**（字段与 :meth:`P1Archive._negative_row` 同形）。
    row: dict[str, Any] = field(default_factory=dict)
    #: 拒并理由（:data:`REFUSALS` 之一）；空串 = 可并。
    refuse: str = ""
    #: 拒并的细节：冲突存档名 + 它的 evidence（原话）。
    detail: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.task_id, self.url, self.branch)

    @property
    def ok(self) -> bool:
        return not self.refuse

    def describe(self) -> str:
        return (f"[{self.task_id}] {self.branch}  {self.url[:72]}")


# ── 读 ────────────────────────────────────────────────────────────


def _quarantine_urls(root: pathlib.Path) -> set[str]:
    """读 ``quarantine.jsonl`` 的 URL 集合（人工判定的拒收名单）。"""
    path = root / "quarantine.jsonl"
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            u = str(row.get("url") or "").strip()
            if u:
                out.add(u)
    return out


def _success_records(
    root: pathlib.Path,
) -> dict[str, list[tuple[str, str]]]:
    """门禁口径的存档集里，**成功**（``branch is None``）的 URL 索引。

    与 :func:`executor.integrity._flag_contradicted_negatives` **同口径**：
    同为按 URL 比对（不按 ``(task_id, url, branch)``）——同一个站在不同
    task / 不同 persona 下出现矛盾，正是要抓的东西。
    """
    winners: dict[str, list[tuple[str, str]]] = {}
    for path, _reviewed in select_archives(root):
        try:
            doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        for o in doc.get("outcomes") or []:
            if not isinstance(o, dict) or o.get("branch") is not None:
                continue
            u = str(o.get("url") or "").strip()
            if u:
                winners.setdefault(u, []).append(
                    (path.name, str(o.get("evidence") or ""))
                )
    return winners


def _pool_row(
    doc: dict[str, Any], outcome: dict[str, Any], task_id: str
) -> dict[str, Any]:
    """从一份**存档**（而不是 ``record``）造一条池行。

    字段与 :meth:`P1Archive._negative_row` **同形**——训练侧
    （``evaluation.golden.builder._scan_negative_pool``）按这些字段读，
    少一个就少一条解释。

    ⚠️ ``branch_label`` 取**注册表**的（:data:`branches.BRANCH_LABELS`），
    不取 outcome 里那个：人工改判分支时 outcome 里的 ``branch_label``
    可能还是改判前的旧值（实测 ``partial_free`` 那一行的 label 还写着
    「成功」）。

    ⚠️ **人工裁定的理由必须进 ``evidence``**（2026-10-26 实测）：
    复核档里 outcome 的 ``evidence`` 保留的是**代码原判**的正文
    （「只改分支，不改原始证据」——那样才测得出人工与代码的分歧率），
    而人的理由在另一个字段 ``review_note`` 里。训练侧
    （``evaluation.golden.builder._scan_negative_pool``）只读 ``evidence``
    ⇒ 不把它折进去，训练样本里就会出现「evidence 说『无证据不记负样本』」
    配一个负样本标签——**理由与标签互相打脸**。
    折法与 :mod:`executor.relabel` 同款：**追加，不覆盖**。
    """
    provenance = doc.get("provenance")
    provenance = dict(provenance) if isinstance(provenance, dict) else {}
    branch = str(outcome.get("branch") or "")
    evidence = str(outcome.get("evidence") or "")
    review_verdict = str(outcome.get("review_verdict") or "")
    review_note = str(outcome.get("review_note") or "")
    if review_verdict:
        who = str(outcome.get("reviewed_by") or "")
        tail = f"[人工裁定 · {review_verdict}"
        if who:
            tail += f" · {who}"
        tail += "]" + (f" {review_note}" if review_note else "")
        evidence = f"{evidence}\n\n{tail}" if evidence else tail
    row = {
        "task_id": task_id,
        "run_id": str(doc.get("run_id") or ""),
        "persona_id": str(provenance.get("persona_id") or ""),
        "driver": str(doc.get("driver") or ""),
        "url": str(outcome.get("url") or ""),
        "branch": branch,
        "branch_label": BRANCH_LABELS.get(branch, ""),
        "evidence": evidence,
        "question": str(outcome.get("question") or ""),
        "source": str(outcome.get("source") or ""),
        "fallback_used": outcome.get("fallback_used", False),
        "reached_play_page": outcome.get("reached_play_page", False),
        "content_tier": str(provenance.get("content_tier") or ""),
        "popularity": str(provenance.get("popularity") or ""),
        "genre": str(provenance.get("genre") or ""),
    }
    if review_verdict:
        # 人工来源的三件事实**带上**：训练侧只吃池行，这三件是「这条标签
        # 是人判的」的唯一凭据（历史池行**不回填**——并池是写入时的动作）。
        row["review_verdict"] = review_verdict
        row["reviewed_by"] = str(outcome.get("reviewed_by") or "")
        row["branch_before_review"] = str(
            outcome.get("branch_before_review") or "")
    return row


def collect_candidates(
    root: pathlib.Path | str,
) -> list[Candidate]:
    """扫出「该进池但还没进池」的真负样本，**逐条给判定**。

    口径与门禁一致：存档集取 :func:`executor.archive.select_archives`
    （顶层、复核档优先），负样本判定照 ``branch ∉ NON_SAMPLE_BRANCHES ∪
    COLLECTION_SIDE_BRANCHES``，去重键 ``(task_id, url, branch)``。

    已在池里的键**不出现**在结果里（幂等：跑第二次是空清单）。
    """
    base = pathlib.Path(root)
    store = P1Archive(base)
    pool_keys = store._load_negative_keys()          # noqa: SLF001
    pool_rows = store._load_pool_rows()              # noqa: SLF001
    quarantined = _quarantine_urls(base)
    winners = _success_records(base)

    # 同 (task_id, url) 在池里已有的分支——用来认「人工改判的旧行」。
    pool_by_site: dict[tuple[str, str], set[str]] = {}
    for row in pool_rows:
        key = (str(row.get("task_id") or ""), str(row.get("url") or ""))
        pool_by_site.setdefault(key, set()).add(str(row.get("branch") or ""))

    out: list[Candidate] = []
    seen: set[tuple[str, str, str]] = set()
    for path, _reviewed in select_archives(base):
        try:
            doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        task_id = str(doc.get("task_id") or "")
        for outcome in doc.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            branch = outcome.get("branch")
            if branch is None:
                continue
            if branch in NON_SAMPLE_BRANCHES or branch in COLLECTION_SIDE_BRANCHES:
                continue
            url = str(outcome.get("url") or "").strip()
            if not url:
                continue
            if (task_id, url, str(branch)) in pool_keys:
                continue                      # 已在池里——幂等，不重复报
            if (task_id, url, str(branch)) in seen:
                continue                      # 同一份存档里同键有多行——只并一行
            seen.add((task_id, url, str(branch)))
            cand = Candidate(
                task_id=task_id,
                url=url,
                branch=str(branch),
                evidence=str(outcome.get("evidence") or ""),
                archive=path.name,
                row=_pool_row(doc, outcome, task_id),
            )
            # ── 三条拒并条件（顺序即优先级）────────────────────
            if url in quarantined:
                cand.refuse = "quarantined"
            else:
                clash = winners.get(url) or []
                if clash:
                    cand.refuse = "contradicted"
                    who, ev = clash[0]
                    cand.detail = (f"{who}：{ev[:120]}"
                                    + (f"（另有 {len(clash) - 1} 份）"
                                       if len(clash) > 1 else ""))
                else:
                    stale = pool_by_site.get((task_id, url)) or set()
                    stale.discard(str(branch))
                    if stale:
                        cand.refuse = "stale_branch"
                        cand.detail = "池内已有 " + "、".join(sorted(stale))
            out.append(cand)
    return out


# ── 写 ────────────────────────────────────────────────────────────


def add_from_root(
    root: pathlib.Path | str, *, dry_run: bool = True
) -> dict[str, Any]:
    """把「可并的」候选并进池。返回
    ``{"candidates", "added", "skipped", "rows"}``。

    :param dry_run: **默认 True**：只报清单，不碰盘。
    """
    base = pathlib.Path(root)
    cands = collect_candidates(base)
    ok = [c for c in cands if c.ok]
    skipped = [c for c in cands if not c.ok]
    out: dict[str, Any] = {
        "candidates": cands,
        "added": 0,
        "skipped": skipped,
        "rows": [],
    }
    if dry_run or not ok:
        return out
    store = P1Archive(base)
    out["added"] = store.append_pool_rows([c.row for c in ok])
    out["rows"] = [c.row for c in ok]
    return out


def refusal_text(reason: str) -> str:
    """拒并理由的人读说明。"""
    return _REFUSAL_TEXT.get(reason, reason)
