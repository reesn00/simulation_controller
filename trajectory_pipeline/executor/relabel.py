"""把「标签错了但仍是负样本」的记录**改分支**——处置动作（2026-10-21 H-16 修复②）。

为什么要有这条命令
------------------
「这条确实是负样本，但分支贴错了」这一类，改动前**没有处置路径**：

* :mod:`trajectory_pipeline.executor.downgrade` 的目标分支是硬编码的
  ``unresolved``——那说的是「判定不成立」，而这里判定**成立**，只是**贴错了支**；
* ``cli review --apply`` 只对 :data:`~executor.review_queue.REVIEW_BRANCHES`
  （``unresolved`` / ``trailer_suspect`` / ``player_unverified``）生效，
  而**真负样本不在其列**——复核队列压根不导出它们
  （见 :func:`~executor.review_queue.extract_items` 的分支过滤）。

⇒ 唯一的手工手段是直接改 JSON，而那会留下**两处不一致**：
``ledger.by_branch`` 不动、``negative.jsonl`` 里的 ``branch`` 还是旧的。
而**训练侧吃的是池子**（:func:`evaluation.golden.builder._scan_negative_pool`
读的就是池行的 ``branch`` / ``branch_label``），不是存档——只改存档等于没改。

三条处置路径的分工（互不重叠）
------------------------------

======================  ====================  ====================  =============
路径                    源                   目标                  还在池里吗
======================  ====================  ====================  =============
``cli downgrade``       任何判定             ``unresolved``        **不在**
``cli review --apply``  ``REVIEW_BRANCHES``   人裁的分支            看目标
**``cli relabel``**     **真负样本**         **另一个真负样本**    **在**
======================  ====================  ====================  =============

五条硬纪律
----------
1. **目标必须仍是真负样本**（∉ :data:`~executor.branches.NON_SAMPLE_BRANCHES`）
   ——「改标签」不是「改结论」；要把一条真负样本踢出池子，走
   ``cli downgrade``。
2. **核归属**（:func:`check_attribution`）——目标分支必须属于这条记录
   **当前的判断点**，否则会造出决策树（rubric D-5）里不存在的
   ``(question, branch)`` 组合（实测 15 条因此被 D-5 判成一票否决，
   门禁 ``test_d5_scorer::Test同源`` 当场红）。
3. **按池的去重键聚合**（:class:`Candidate`）——同一个
   ``(task_id, url, branch)`` 在几份存档里各有一条时必须**一起改**，
   否则另一份当场报 ``negative_not_pooled``（门禁 DEGRADED）。
4. **理由必填、默认 dry-run**——与 :mod:`downgrade` 同款。改标签是
   **不可回退**的动作（池是跨 run 累积的 append-only 资产），
   先看清单再落盘。
5. **不碰反思档**——``unresolved`` / ``trailer_suspect`` /
   ``player_unverified`` 走 ``review --apply``。它们**不是负样本**，
   而本命令的整个前提就是「**仍是负样本**」。
"""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from trajectory_pipeline.common.atomic import write_json_atomic, write_text_atomic
from trajectory_pipeline.executor.branches import (
    BRANCH_LABELS,
    COLLECTION_SIDE_BRANCHES,
    NON_SAMPLE_BRANCHES,
)
from trajectory_pipeline.perception import questions

__all__ = [
    "RelabelError",
    "RELABEL_MARK",
    "Candidate",
    "is_negative_sample_branch",
    "check_target",
    "check_attribution",
    "select_by_url",
    "select_by_branch",
    "select_by_evidence",
    "select_all",
    "collect_candidates",
    "plan_relabels",
    "apply_plan",
    "relabel_to_root",
]


class RelabelError(ValueError):
    """改判出错。显式抛——处置动作出错必须**中止**并让人看见。"""


#: 改判留痕的戳。**与降级的 ``[原判 X] `` 前缀刻意不同**。
#:
#: 降级那个前缀是 :func:`executor.downgrade.base_evidence` 的幂等判据
#:（按**戳**识别「已处置过的记录」），而改判**允许链式**（A→B 之后再
#: B→C，理由可能来自第二批证据）。复用同一个前缀会让第二次改判被当成
#: 「已处置」跳过，于是「先改判、再降级」这条路就断了。
#:
#: 改判自己的幂等判据是**目标分支值**（已经是目标分支就跳过），
#: 留痕是 ``outcomes[].branch_before_relabel`` 与 evidence 的追加段。
RELABEL_MARK = "[改判 "


# ── 判据 ──────────────────────────────────────────────────────────


def is_negative_sample_branch(branch: str | None) -> bool:
    """这个分支是不是**真负样本**（能留在负样本池里）。

    判据两段：非空 + ∉ :data:`~executor.branches.NON_SAMPLE_BRANCHES`。

    ⚠️ **不查** ``questions.all_branches()``：源分支可以是**已弃用**的
    合并态（``login_wall_or_blocked``，实测池里 35 条）——那正是最该
    被改判的对象。目标分支才受那条约束（见 :func:`check_target`）。

    ⚠️ 采集侧分支（``blocked``）**故意算源**：R1（2026-10-26）之后它
    不再进池，但**历史池行不回填**——那批行仍然需要能被改判成真正的
    负分支（或走 ``pool_drop`` 出池）。把源也一起排掉，等于让旧行
    只剩「删」这一条路。
    """
    return bool(branch) and branch not in NON_SAMPLE_BRANCHES


def check_target(branch: str) -> tuple[bool, str]:
    """目标分支合不合法。返回 ``(通过, 理由)``。

    三条：

    1. **非空**；
    2. **在活跃分支表里**（:func:`questions.all_branches`，20 条）——
       已弃用的合并态与拼错的名字都在这里被挡下；
    3. **是真负样本**（∉ :data:`NON_SAMPLE_BRANCHES`）——「改标签」不是
       「改结论」，而 ``unresolved`` 这类反思档**根本不在池里**，
       把一条真负样本改成它等于把它踢出池（那是 ``cli downgrade`` 的活）。

    ⚠️ 2026-10-26（R1）第 3 条扩成**两张表**：采集侧分支
    （:data:`COLLECTION_SIDE_BRANCHES`，现为 ``blocked``）同样不能当目标。
    它 ``∉ NON_SAMPLE``（有题），但 ``∉`` 负样本池——改判到它等于
    **把一条采集失败写进负样本池**，而池子的全部价值就是「这里真的看不了」。
    历史上（2026-10-21）确实这么改过（E021 那三条改成 blocked），
    **已有池行不回填**；本命令从此不再产出新的。
    """
    if not branch:
        return False, "目标分支为空"
    if branch in questions.DEPRECATED_BRANCHES:
        return False, (f"目标分支 {branch!r} 已弃用（不再产出）——"
                       f"它只在历史存档里出现")
    if branch not in questions.all_branches():
        return False, (f"目标分支 {branch!r} 不在活跃分支表里"
                       f"（{len(questions.all_branches())} 条）")
    if branch in NON_SAMPLE_BRANCHES:
        return False, (f"目标分支 {branch!r} 不是负样本"
                       f"（{'、'.join(sorted(NON_SAMPLE_BRANCHES))}）——"
                       f"改判的前提是「仍是负样本」；要把一条踢出池走 "
                       f"cli downgrade")
    if branch in COLLECTION_SIDE_BRANCHES:
        return False, (f"目标分支 {branch!r} 是**采集侧账**，不是负样本"
                       f"（{'、'.join(sorted(COLLECTION_SIDE_BRANCHES))}）"
                       f"——「这次没进去」不构成站点性质，改判到它等于把"
                       f"采集失败写进负样本池；要记这条事实走 review 的 "
                       f"confirm_collection_blocked")
    return True, ""


def check_attribution(
    record_questions: Iterable[str],
    from_branch: str,
    to_branch: str,
) -> tuple[bool, str]:
    """换上去的分支能不能贴在这条记录上（**核归属**）。

    与 :func:`executor.review_queue._fits` 同一口径：目标分支必须
    **属于**这条记录的判断点，否则造出决策树（rubric D-5）里不存在的
    ``(question, branch)`` 组合——实测 15 条因此被 D-5 判成一票否决。

    归属表取 :func:`questions.questions_of_branch`（多产地分支任一命中
    即合规，与复核回填同一条口径）。

    ⚠️ **一条记录可能横跨多个判断点**：同一个 ``(task_id, url, branch)``
    键在几份存档里各有一条时（:class:`Candidate` 把它们聚在一起），
    每一条的判断点都要容得下目标分支——**任一条不容，整组就拒**。

    ⚠️ **老存档的记录可能没记判断点**（实测池里 57/181 行为空）。
    这时**不直接放行**（「无法核对」≠「可以贴」），而是**由源分支反推**：
    源分支自己就带着产地（``unreachable_hard`` 只可能出自②
    ``site.is_reachable``），源与目标的产地**有交集** ⇒ 合规；
    无交集 ⇒ 拒绝，并把两边的产地都写进理由里。
    """
    target_sites = questions.questions_of_branch(to_branch)
    if not target_sites:
        return False, f"目标分支 {to_branch!r} 没有登记任何判断点"
    seen = [q for q in record_questions if q]
    for q in seen:
        if q not in target_sites:
            return False, (f"目标分支 {to_branch!r} 归属判断点 {target_sites}，"
                           f"而本条记的是 {q!r}")
    if seen:
        return True, ""
    src_sites = questions.questions_of_branch(from_branch)
    if set(src_sites) & set(target_sites):
        return True, ""
    return False, (f"本条没记判断点，源分支 {from_branch!r} 的产地 {src_sites} "
                   f"与目标 {to_branch!r} 的产地 {target_sites} 无交集")


# ── 候选 ──────────────────────────────────────────────────────────


@dataclass
class Candidate:
    """一条待处置的记录——**按负样本池的去重键聚合**。

    ⚠️ **为什么按键聚合而不是按 outcome**：负样本池的去重键是
    ``(task_id, url, branch)``，而 :func:`executor.integrity.check_archive`
    查的正是「存档里每条真负样本的键**在不在池里**」。于是同一个键在
    两份存档里各有一条时，**改一份不改另一份**就会让另一份当场报
    ``negative_not_pooled``（门禁 DEGRADED），而报告上只多一行
    「没并池」——看不出是改判漏了一半。

    ⇒ 改判的最小单位是**键**，不是行。:attr:`members` 记着这个键在
    哪些存档的哪条 outcome 上出现过。
    """

    task_id: str
    url: str
    branch: str
    #: 这个键下所有记录的判断点（去重、去掉空串）。
    questions: list[str] = field(default_factory=list)
    #: 代表性的判定正文（存档的优先，没有则取池行的）——给证据选择器用。
    evidence: str = ""
    #: 这个键在池里有没有对应行。
    in_pool: bool = False
    #: 池文件里的行号（-1 = 不在池里）。
    pool_line: int = -1
    #: 命中的存档 outcome：``[(路径, outcome 字典), …]``。
    members: list[tuple[pathlib.Path, dict[str, Any]]] = field(default_factory=list)
    #: 命中过的存档文件名（给人看）。
    archives: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.task_id, self.url, self.branch)

    def describe(self) -> str:
        return f"{self.task_id} {self.branch} {self.url[:72]}"


# ── 采集 ──────────────────────────────────────────────────────────


def _read_pool_lines(path: pathlib.Path) -> list[tuple[str, dict[str, Any] | None]]:
    """读池子，**坏行原样带出**（第二项为 ``None``）。

    与 :meth:`executor.archive.P1Archive._load_pool_lines` 同一处置：
    重写整份文件时坏行必须原样保留——一次改判顺手清掉全池的坏行是
    另一件事（而坏行可能是被人手工截断、正等人查的）。
    """
    if not path.exists():
        return []
    out: list[tuple[str, dict[str, Any] | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            out.append((line, None))
            continue
        out.append((line, row if isinstance(row, dict) else None))
    return out


def _load_archives(root: pathlib.Path) -> list[tuple[pathlib.Path, dict[str, Any]]]:
    """读该目录下**全部**存档（原档 + 复核档）。

    ⚠️ **复核档也要改**，这与 :mod:`downgrade` 的做法**不同**，理由是
    读侧口径：:func:`executor.archive.select_archives` 读的时候
    **优先取复核档**，所以复核档里的分支才是门禁与报表**实际看到**的
    那一份。只改原档 ⇒ 复核档还留着旧键 ⇒ 池里已是新键 ⇒ 门禁当场报
    ``negative_not_pooled``。

    而复核档**会被下一次 ``review --apply`` 从原档重新合并出来**——
    原档也改了，所以改判不会被覆盖回去（这正是 :mod:`downgrade`
    模块 docstring 坑 1 要防的那个坑，这里靠「两份都改」躲开）。

    ⚠️ **只认 ``<task_id>__<run_id>.json`` 形状**（与
    :func:`executor.archive.select_archives` 同一条口径）：同目录里还
    躺着探针取证（``obscura_tools.json`` 等），按 ``*.json`` 收会把它们
    当存档。
    """
    out: list[tuple[pathlib.Path, dict[str, Any]]] = []
    for path in sorted(root.glob("*.json")):
        if "__" not in path.stem:
            continue                      # 探针取证，不是存档
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue                      # 坏档由 check-archives 单独报
        if not isinstance(doc, dict) or "outcomes" not in doc:
            continue
        out.append((path, doc))
    return out


def collect_candidates(
    root: pathlib.Path | str,
) -> tuple[dict[tuple[str, str, str], Candidate],
           list[tuple[str, dict[str, Any] | None]],
           dict[pathlib.Path, dict[str, Any]]]:
    """扫存档与池子，按**池的去重键**聚成候选。

    返回 ``(候选表, 池文件原始行, 存档文档)``。

    * 池行原样带出，是为了**重写时保留坏行**（见 :func:`_read_pool_lines`）；
    * 存档文档一并返回，是为了**改的就是写的那个**——处置全程在
      同一个对象上（:mod:`downgrade` 模块 docstring 里那个「改的不是
      写的那个」的坑）。
    """
    base = pathlib.Path(root)
    candidates: dict[tuple[str, str, str], Candidate] = {}

    def _slot(key: tuple[str, str, str]) -> Candidate:
        cand = candidates.get(key)
        if cand is None:
            cand = Candidate(task_id=key[0], url=key[1], branch=key[2])
            candidates[key] = cand
        return cand

    docs: dict[pathlib.Path, dict[str, Any]] = {}
    for path, doc in _load_archives(base):
        docs[path] = doc
        task_id = str(doc.get("task_id") or "")
        for outcome in doc.get("outcomes") or ():
            if not isinstance(outcome, dict):
                continue
            branch = str(outcome.get("branch") or "")
            if not is_negative_sample_branch(branch):
                continue                  # 成功样本 / 反思档：不是负样本
            key = (task_id, str(outcome.get("url") or ""), branch)
            cand = _slot(key)
            q = str(outcome.get("question") or "")
            if q and q not in cand.questions:
                cand.questions.append(q)
            if not cand.evidence:
                cand.evidence = str(outcome.get("evidence") or "")
            cand.members.append((path, outcome))
            if path.name not in cand.archives:
                cand.archives.append(path.name)

    pool_path = base / "negative.jsonl"
    pool_lines = _read_pool_lines(pool_path)
    for idx, (_raw, row) in enumerate(pool_lines):
        if row is None:
            continue
        branch = str(row.get("branch") or "")
        if not is_negative_sample_branch(branch):
            continue
        key = (str(row.get("task_id") or ""), str(row.get("url") or ""), branch)
        cand = _slot(key)
        cand.in_pool = True
        cand.pool_line = idx
        if not cand.evidence:
            cand.evidence = str(row.get("evidence") or "")
        q = str(row.get("question") or "")
        if q and q not in cand.questions:
            cand.questions.append(q)
    return candidates, pool_lines, docs


# ── 选择器（判据在命令行，这里只提供可组合的谓词）──────────────────


def select_by_url(*fragments: str) -> Callable[[Candidate], bool]:
    """按 URL 片段挑。**多片段是 OR。**"""
    if not fragments:
        raise ValueError("至少要给一个 URL 片段")

    def _sel(cand: Candidate) -> bool:
        return any(f in cand.url for f in fragments)
    return _sel


def select_by_branch(*names: str) -> Callable[[Candidate], bool]:
    """按**当前**分支挑。**多名是 OR。**"""
    if not names:
        raise ValueError("至少要给一个分支名")
    want = set(names)

    def _sel(cand: Candidate) -> bool:
        return cand.branch in want
    return _sel


def select_by_evidence(
    patterns: Iterable[re.Pattern[str]],
) -> Callable[[Candidate], bool]:
    """按**判定正文**是否命中某条正则来挑。

    ⚠️ 判据只看**第一段**（:func:`executor.downgrade.base_evidence` 的
    同一口径）：追加的说明里也会出现判据关键词（人写「依据是
    『当前设备不支持播放』」这类理由时很自然），全文匹配会把自己
    刚改判的记录再改一次。
    """
    pats = list(patterns)
    if not pats:
        raise ValueError("至少要给一条判据正则")
    from trajectory_pipeline.executor.downgrade import base_evidence

    def _sel(cand: Candidate) -> bool:
        head = base_evidence(cand.evidence)
        return any(p.search(head) for p in pats)
    return _sel


def select_all() -> Callable[[Candidate], bool]:
    """全选（不按判据挑）。**危险**：会改判池里的每一条。"""
    return lambda cand: True


# ── 计划 ──────────────────────────────────────────────────────────


@dataclass
class Plan:
    """一次改判的**全部计划**：改哪些、并哪些、拒哪些、为什么。"""

    to_branch: str
    reason: str
    accepted: list[Candidate] = field(default_factory=list)
    merged: list[tuple[Candidate, str]] = field(default_factory=list)
    rejected: list[tuple[Candidate, str]] = field(default_factory=list)
    skipped: list[tuple[Candidate, str]] = field(default_factory=list)

    @property
    def touched(self) -> list[Candidate]:
        """**要改分支**的全部候选（改写池行的 + 并入已有行的）。"""
        return self.accepted + [c for c, _ in self.merged]

    @property
    def outcomes(self) -> int:
        return sum(len(c.members) for c in self.touched)

    @property
    def pool_rows(self) -> int:
        return sum(1 for c in self.accepted if c.in_pool)

    @property
    def pool_dropped(self) -> int:
        return sum(1 for c, _ in self.merged if c.in_pool)

    @property
    def archives(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for cand in self.touched:
            for name in cand.archives:
                out[name] = out.get(name, 0) + 1
        return out


def plan_relabels(
    candidates: dict[tuple[str, str, str], Candidate],
    *,
    to_branch: str,
    reason: str,
    select: Callable[[Candidate], bool] | None = None,
    pool_keys: Iterable[tuple[str, str, str]] = (),
) -> Plan:
    """算出改哪些、并哪些、拒哪些。**不碰盘。**

    四种去处，逐条都带原因（**不静默跳过**——静默跳过读起来
    就像「跑完了」）：

    * **已经是目标分支** ⇒ 幂等，跳过（``skipped``）；
    * **归属不符** ⇒ 换上去的分支不属于这条记录的判断点（``rejected``）；
    * **改写**（``accepted``）⇒ 池里那一行改成新分支；
    * **并入**（``merged``）⇒ **池里已经有同 ``(task_id, url)`` 的新标签**
      ——那一行**删掉**，信息已经在正确标签的那条里。**不是撞键报错**：
      撞键的正确处置是「合并」，不是「拒绝」（见下）。

    ## 为什么「并入」而不是「拒绝」
    ----------------------------
    实测（2026-10-21 人工复核）：同一个 ``(task_id, url)`` 在池里有**两行**、
    标签还**互相矛盾**——

    ```text
    E021  94a1392f  subscription_wall  v.qq.com/…8lvnb7aiio1crpd/i002515xdcm.html
    E021  129c3fae  blocked            v.qq.com/…8lvnb7aiio1crpd/i002515xdcm.html
    ```

    同一 task、同一站，两次跑出了两个结论（② 那次判对了地区限制，④ 那次被
    付费墙救济改判）。改判之后两条都是 ``blocked`` ⇒ 池里出现**两条同键的行**
    ⇒ 违反池自己的去重约定（``(task_id, url, branch)``，见
    :meth:`executor.archive.P1Archive._append_negatives`）。

    ⇒ 处置是「**并入**」：保留已有的那条（它是**判对了**的那次跑的），
    删掉贴错标签的那条。**不新建行**——同键两行是重复样本。
    """
    if not reason.strip():
        raise RelabelError("改判必须给 --reason：它是这条日后唯一的依据")
    ok, why = check_target(to_branch)
    if not ok:
        raise RelabelError(why)
    # 凭据闸门挂在**计划**这一步（不是落盘那一步）：理由是人手输的，
    # 而 dry-run 也会把它打印进终端——凭据进了终端就是进了日志。
    _guard_credentials(reason, "改判理由")

    plan = Plan(to_branch=to_branch, reason=reason)
    eligible: list[Candidate] = []
    for key in sorted(candidates):
        cand = candidates[key]
        if select is not None and not select(cand):
            continue
        if cand.branch == to_branch:
            plan.skipped.append((cand, "已经是目标分支"))
            continue
        ok, why = check_attribution(cand.questions, cand.branch, to_branch)
        if not ok:
            plan.rejected.append((cand, why))
            continue
        eligible.append(cand)

    # 池的**最终**键集：被改判的旧键全部让位（无论那行是改写还是删掉），
    # 剩下的旧键原样留着。判据是「**这次改判结束后**池里会不会出现两行
    # 同键」——所以基准是结束后的键集，不是改判前的。
    removed = {c.key for c in eligible}
    surviving = set(pool_keys) - removed
    claimed: set[tuple[str, str, str]] = set()
    for cand in eligible:
        new_key = (cand.task_id, cand.url, to_branch)
        if new_key in surviving:
            plan.merged.append((cand, f"池里已经有 {new_key} 这一行"
                                        f"（改判后的标签已存在）——"
                                        f"本行并入那一条，不新建"))
            continue
        if new_key in claimed:
            plan.merged.append((cand, f"本批里 {new_key} 已被另一条占用——"
                                        f"本行并入那一条，不新建"))
            continue
        claimed.add(new_key)
        plan.accepted.append(cand)
    return plan



# ── 处置 ──────────────────────────────────────────────────────────


def _note(old: str, new: str, reason: str) -> str:
    return f"{RELABEL_MARK}{old} → {new}] {reason}"


def _relabel_outcome(outcome: dict[str, Any], to_branch: str,
                     reason: str) -> str:
    """改一条 outcome。**只改结论，不改采集事实。** 返回旧分支。

    * ``branch_before_relabel`` 记**原判**（链式改判不覆盖——它记的是
      「这条最初被贴成什么」，那才是半年后要回查的东西）；
    * ``evidence`` **追加**一段说明，**不覆盖**原正文——覆盖掉之后就
      永远测不出「人工与代码的分歧率」了；
    * ``source`` 记 ``human``（与 :func:`executor.review_queue.apply_verdicts`
      同口径：人工改过的结论要认得出来）。
    """
    old = str(outcome.get("branch") or "")
    outcome.setdefault("branch_before_relabel", old)
    outcome["branch"] = to_branch
    outcome["is_negative_sample"] = True
    outcome["source"] = "human"
    outcome["evidence"] = (f"{outcome.get('evidence') or ''}\n\n"
                           f"{_note(old, to_branch, reason)}")
    return old


def _relabel_pool_row(row: dict[str, Any], to_branch: str,
                       reason: str) -> None:
    """改池里的一行。**训练侧吃的就是这一份**（见模块 docstring）。"""
    old = str(row.get("branch") or "")
    row["branch"] = to_branch
    row["branch_label"] = BRANCH_LABELS.get(to_branch, "")
    row["source"] = "human"
    row["evidence"] = (f"{row.get('evidence') or ''}\n\n"
                       f"{_note(old, to_branch, reason)}")


def _update_ledger(doc: dict[str, Any], changes: list[tuple[str, str]]) -> None:
    """改 ``ledger.by_branch`` 与 ``missing_branches``。

    :param changes: ``[(旧分支, 新分支), …]``——**一条 outcome 一项**
        （``by_branch`` 数的是 outcome 条数，不是「键」的条数）。

    ⚠️ **``negative_samples`` 计数不动**：改判前后**都是真负样本**，
    池的规模没变——把计数也改一遍就会说成「池里少了一条」。
    """
    led = doc.get("ledger")
    if not isinstance(led, dict):
        return
    by = led.get("by_branch")
    if not isinstance(by, dict):
        by = {}
    for old, new in changes:
        by[old] = max(0, int(by.get(old, 0)) - 1)
        by[new] = int(by.get(new, 0)) + 1
    led["by_branch"] = {k: v for k, v in by.items() if v}
    if "missing_branches" in led:
        miss = set(led.get("missing_branches") or [])
        for old, new in changes:
            if int(by.get(old, 0)) <= 0 and old in questions.all_branches():
                miss.add(old)
            if new in questions.all_branches():
                miss.discard(new)
        led["missing_branches"] = sorted(miss)
    doc["ledger"] = led


def _guard_credentials(text: str, what: str) -> None:
    """凭据闸门：理由是人手输的，**凭据不得进存档、不得进池**。

    与 :meth:`executor.archive.P1Archive.write` 同一道闸（红线：
    凭据不得提交、打包、复制到测试、文档或日志）。改判是**人工输入**
    落盘的唯一入口，所以闸门挂在这里。
    """
    from trajectory_pipeline.executor.archive import scan_credentials

    hits = scan_credentials(text)
    if hits:
        raise RelabelError(
            f"{what}命中 {len(hits)} 项凭据特征，已拒绝改判：{hits[:3]}")


def apply_plan(
    root: pathlib.Path | str,
    plan: Plan,
    pool_lines: list[tuple[str, dict[str, Any] | None]],
    docs: dict[pathlib.Path, dict[str, Any]],
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """执行计划。**默认 dry-run**（只报不动）。

    落盘顺序：**先存档，后池**。理由是池是**派生资产**——存档写成功
    而池写失败时，池里那行的旧分支会与存档的新分支对不上，而门禁
    ``negative_not_pooled`` 报的是「**没并池**」，读的人不会想到
    「这是改判写了一半」。反过来（先池后存档）失败时，池里是新键、
    存档里是旧键，同样对不上——**两种半截状态都坏**，所以两头都用
    原子写，把「写一半」压到最小（单次 ``os.replace`` 之内）。
    """
    base = pathlib.Path(root)
    summary: dict[str, Any] = {
        "to_branch": plan.to_branch,
        "relabelled": len(plan.accepted),
        "merged": len(plan.merged),
        "outcomes": plan.outcomes,
        "pool_rows": plan.pool_rows,
        "pool_dropped": plan.pool_dropped,
        "archives": plan.archives,
        "rejected": [(c.describe(), why) for c, why in plan.rejected],
        "skipped": len(plan.skipped),
        "dry_run": dry_run,
    }
    if dry_run or not plan.touched:
        return summary

    _guard_credentials(plan.reason, "改判理由")

    # ── 存档 ─────────────────────────────────────────────────────
    by_path: dict[pathlib.Path, list[Candidate]] = {}
    for cand in plan.touched:
        for path, _outcome in cand.members:
            by_path.setdefault(path, []).append(cand)
    for path, cands in by_path.items():
        doc = docs.get(path)
        if doc is None:
            continue
        pairs: list[tuple[str, str]] = []
        for cand in cands:
            for p, outcome in cand.members:
                if p is not path:
                    continue
                old = _relabel_outcome(outcome, plan.to_branch, plan.reason)
                pairs.append((old, plan.to_branch))
        _update_ledger(doc, pairs)
        doc.setdefault("warnings", []).append(
            f"人工改判 {len(pairs)} 条 outcome → {plan.to_branch}："
            f"{plan.reason[:80]}")
        write_json_atomic(path, doc, indent=2)

    # ── 池 ───────────────────────────────────────────────────────
    # 改写的行**换标签**，并入的行**删掉**（同键已有正确标签那条）。
    # ⚠️ **不新建行**：同 ``(task_id, url, branch)`` 只能有一行——
    # 那是池自己的去重约定（见 ``_append_negatives`` 的键说明）。
    pool_path = base / "negative.jsonl"
    if (plan.pool_rows or plan.pool_dropped) and pool_path.exists():
        rewrite = {c.pool_line: c for c in plan.accepted if c.in_pool}
        drop = {c.pool_line for c, _ in plan.merged if c.in_pool}
        kept: list[str] = []
        for idx, (raw, row) in enumerate(pool_lines):
            hit = rewrite.get(idx)
            if hit is not None and row is not None:
                _relabel_pool_row(row, plan.to_branch, plan.reason)
                kept.append(json.dumps(row, ensure_ascii=False))
                continue
            if idx in drop:
                continue                  # 并入已有行：这一行删掉
            kept.append(raw)              # 坏行原样带出
        write_text_atomic(pool_path, "".join(raw + "\n" for raw in kept))
    return summary


def relabel_to_root(
    root: pathlib.Path | str,
    *,
    select: Callable[[Candidate], bool] | None = None,
    to_branch: str,
    reason: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """遍历存档目录与负样本池并改分支。返回改动摘要。

    处置动作在**本模块**，判据由调用方（命令行）传进来——与
    :mod:`downgrade` 同一分工：各批数据的判据形态不同，而处置动作
    只有一份。
    """
    base = pathlib.Path(root)
    candidates, pool_lines, docs = collect_candidates(base)
    pool_keys = {(str(row.get("task_id") or ""), str(row.get("url") or ""),
                  str(row.get("branch") or ""))
                 for _raw, row in pool_lines if row is not None}
    plan = plan_relabels(candidates, to_branch=to_branch, reason=reason,
                          select=select, pool_keys=pool_keys)
    return apply_plan(base, plan, pool_lines, docs, dry_run=dry_run)
