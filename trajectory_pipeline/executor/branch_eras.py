"""分支口径代（branch rule era）——「这条分支是**哪版规则**判出来的」。

为什么需要这一轴
----------------
分支名不只是标签，**它背后是判定规则**，而规则一改，同一批 URL 落的
分支就会换。前后两代的分支分布因此**不可直接比**：

* 2026-10-12 阶段 1 把登录墙合并态拆成 3 支、把「试看」拆出
  ``partial_free`` ⇒ ``login_wall_or_blocked`` 的占比**归零**，
  拆出来的那几支**从 0 涨**——这是规则变，不是质量变好；
* 2026-10-19 同页守卫从 5 支收窄到 1 支 ⇒ 被守卫拦下的行改落
  ``component_unverified``；
* 2026-10-21 ② 的兜底不再落合并态、付费墙前置事实换成门禁语境；
* 2026-10-23 点击失败不再记业务分支。

不记这一轴，这些跳变会被读成「**质量变好了**」——
这正是 CLAUDE.md 点名的坑：「批次边界前后分布会跳变，不记批次元数据
就会读成『质量变好了』」。

三条硬纪律
----------
1. **不回填**：新规则只对**之后**的判定生效；历史存档的分支标签按
   当时的规则原样留着（CLAUDE.md：「新分支（如 ``player_unverified``）
   **不回填历史存档**」）。回填等于把两代口径揉成一代，谁也说不清
   每条是哪版规则判的。
2. **不猜**：代要**有据**才落。历史存档**不带时间戳**（实测：``steps`` /
   ``visits`` / ``outcomes`` / ``run_config`` 里都没有时间），所以代只能
   按**文件 mtime** 判——那是**代理不是事实**，本模块把这件事写在返回值
   的口径里，不装作它是权威的。
3. **可筛**：:func:`split_archives_by_era` 按代切开存档集合，
   :func:`branch_distribution_by_era` 给出按代切开的分支分布。

受影响行的**确切集合**从证据里读
--------------------------------
守卫触发时，判定层会把**打算贴的分支**写进 ``evidence``
（「[记账降级] 本该记 X，……该分支的前置事实不成立，不贴这条负标签」）。
⇒ :func:`guard_downgrades` 能把「被守卫降级过的行」**逐条捞出来**，
不必靠日期猜。这就是「口径断点」能被**机器筛**而不只是被记一笔的原因。

⚠️ 这批行落的是 ``unresolved``，而 ``unresolved`` 是
:data:`~trajectory_pipeline.executor.branches.NON_SAMPLE_BRANCHES` 的一员
⇒ **它们从来就不在负样本池里**（实测：260 行池子，``unresolved`` 0 行）。
守卫收窄因此**没有在训练池上留下任何痕迹**——分布跳变全部发生在
**存档**与**黄金集候选**这一侧。
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import pathlib
import re
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "BranchEra",
    "BRANCH_ERAS",
    "GuardDowngrade",
    "era_for_date",
    "split_archives_by_era",
    "branch_distribution_by_era",
    "guard_downgrades",
    "M_TIME_CAVEAT",
]


#: **代判读口径的免责声明**——本模块任何按时间判代的结果都带这句。
#:
#: P1 存档**没有时间戳**（不是「没找到」，是逐字段核过：``steps`` /
#: ``visits`` / ``outcomes`` / ``run_config`` / ``provenance`` 全无时间
#: 字段），所以历史存档的代只能按**文件 mtime** 判。mtime 会被复制、
#: 解压、迁移扰动——**它是代理，不是事实**。要权威答案，只能重采。
M_TIME_CAVEAT = (
    "按文件 mtime 判代——P1 存档不带时间戳，mtime 是代理不是事实"
)


@dataclasses.dataclass(frozen=True)
class BranchEra:
    """一个分支口径代。

    :param era_id: 代号（``e0`` 起，按时间递增）。
    :param since: 生效日（``YYYY-MM-DD``，含当日）。
    :param summary: 这代改了什么（一句话）。
    :param changed_branches: 这代**新增或改判**的分支名。
    :param anchor: 记录锚点（编年史条目 / 台账 ID / 方案号）。
    """

    era_id: str
    since: str
    summary: str
    changed_branches: tuple[str, ...]
    anchor: str = ""


#: 分支口径代登记表。
#:
#: ⚠️ **这份表是「历史事实」的抄本**——每一项都能在编年史 / 台账里找到
#: 锚点。它与代码现状之间的对账由 contract 测试
#: (``tests/contract/test_branch_eras.py``) 盯住，不靠人记得。
BRANCH_ERAS: tuple[BranchEra, ...] = (
    BranchEra(
        era_id="e0",
        since="2026-10-09",
        summary="失败分支体系定型：10 支 + 1 deprecated 合并态（login_wall_or_blocked）",
        changed_branches=(
            "not_play_site", "unreachable_hard", "login_wall_or_blocked",
            "no_play_control", "trailer_only", "trailer_suspect",
            "component_unverified", "player_unverified", "subscription_wall",
            "unresolved",
        ),
        anchor="编年史 §增量四十四（簇 A/B/C 的时代）",
    ),
    BranchEra(
        era_id="e1",
        since="2026-10-12",
        summary=(
            "阶段 1：登录墙合并态拆为 login_required / blocked / broken；"
            "④ 拆出 partial_free；② payload 细分优先于题绑定"
        ),
        changed_branches=(
            "login_required", "blocked", "broken", "partial_free",
        ),
        anchor="编年史 §增量三十三 / 总体方案 §3.3",
    ),
    BranchEra(
        era_id="e2",
        since="2026-10-16",
        summary="发现链 +7 支（聚合站两支 / 文章链两支 / 站内搜索三支）",
        changed_branches=(
            "not_aggregator", "aggregator_no_entries", "not_article",
            "article_no_links", "no_site_search", "site_search_no_results",
            "site_search_routed",
        ),
        anchor="编年史 §增量三十九 / 方案 15 §7",
    ),
    BranchEra(
        era_id="e3",
        since="2026-10-19",
        summary=(
            "同页守卫从 5 支收窄到 1 支（方案 X）："
            "subscription_wall / login_required / blocked / partial_free "
            "不再因「点击前后同页」被降级"
        ),
        changed_branches=(
            "subscription_wall", "login_required", "blocked", "partial_free",
        ),
        anchor="编年史 §增量六十（方案 X 实施）",
    ),
    BranchEra(
        era_id="e4",
        since="2026-10-21",
        summary=(
            "② 的兜底不再落合并态（落 unresolved，H-15）；"
            "付费墙前置事实换代为门禁语境（删 _has_strong_paywall_signal，H-16）"
        ),
        changed_branches=("subscription_wall", "unresolved"),
        anchor="编年史 §增量七十五 / §增量七十八",
    ),
    BranchEra(
        era_id="e5",
        since="2026-10-23",
        summary="点击失败不再记成业务分支，落 unresolved（采集缺件 ≠ 业务负样本）",
        changed_branches=("unresolved",),
        anchor="编年史 §增量八十三（M-27 闭）",
    ),
)


def era_for_date(day: str) -> str:
    """日期 → 代号。日期格式 ``YYYY-MM-DD``。

    早于第一代的日期落 ``e0``（第一代本身覆盖 2026-10-09 起，
    所以更早的存档归到 ``e0``——那时候就是那 10 支）。
    """
    best = BRANCH_ERAS[0]
    for era in BRANCH_ERAS:
        if day >= era.since:
            best = era
        else:
            break
    return best.era_id


def _archive_day(path: pathlib.Path) -> str:
    """存档的「日」——按 mtime（见 :data:`M_TIME_CAVEAT`）。"""
    ts = path.stat().st_mtime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def split_archives_by_era(
    paths: Iterable[pathlib.Path],
) -> dict[str, list[pathlib.Path]]:
    """按口径代把存档切开。

    ⚠️ 判代用**文件 mtime**（:data:`M_TIME_CAVEAT`）。想要权威答案，
    重采。

    :return: ``{era_id: [存档路径…]}``；**没有存档**的那一代不出现在
        字典里（不塞空列表——空列表会被读成「这代有存档但都是空」）。
    """
    out: dict[str, list[pathlib.Path]] = collections.defaultdict(list)
    for p in paths:
        out[era_for_date(_archive_day(p))].append(p)
    return dict(out)


def branch_distribution_by_era(
    paths: Iterable[pathlib.Path],
) -> dict[str, collections.Counter]:
    """按代给分支分布——「前后分布跳变」的可机器读形式。

    每个存档读一次 ``outcomes[*].branch`` 计数。
    """
    import json

    out: dict[str, collections.Counter] = {}
    for era, group in split_archives_by_era(paths).items():
        counter: collections.Counter = collections.Counter()
        for p in group:
            try:
                blob = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(blob, Mapping):
                continue
            for oc in blob.get("outcomes") or ():
                if isinstance(oc, Mapping) and oc.get("branch"):
                    counter[str(oc["branch"])] += 1
        out[era] = counter
    return out


#: 判定层写进 ``evidence`` 的守卫降级标记。
_DOWNGRADE_MARK = re.compile(r"\[记账降级\]")

#: 「本该记 X」/「本应记 X」——守卫降级时**打算贴**的那条分支。
_INTENT = re.compile(r"本[该应]记\s*([a-z0-9_]+)")


@dataclasses.dataclass(frozen=True)
class GuardDowngrade:
    """一条被**前置事实核验**降级掉的判定。

    :param branch: 实际落的分支（几乎总是 ``unresolved``）。
    :param intended: 判定层**打算贴**的分支（从 evidence 读）。
    :param url: 这条判定对应的 URL。
    :param question: 判断点 id。
    """

    branch: str
    intended: str
    url: str
    question: str


def guard_downgrades(
    archive: Mapping[str, Any],
) -> list[GuardDowngrade]:
    """从一份存档里捞出「被前置事实核验降级掉」的行。

    判据是 ``evidence`` 里的 ``[记账降级]`` 标记 + 「本该记 X」——
    那是判定层**自己写下的**打算贴的分支，不是猜的。

    :param archive: P1 存档的 dict（``json.loads`` 的结果）。
    :return: 每条降级一行；``intended`` 认不出来时给空串（**不猜**）。
    """
    out: list[GuardDowngrade] = []
    outcomes = archive.get("outcomes") if isinstance(archive, Mapping) else None
    for oc in outcomes or ():
        if not isinstance(oc, Mapping):
            continue
        evidence = str(oc.get("evidence") or "")
        if not _DOWNGRADE_MARK.search(evidence):
            continue
        m = _INTENT.search(evidence)
        out.append(
            GuardDowngrade(
                branch=str(oc.get("branch") or ""),
                intended=m.group(1) if m else "",
                url=str(oc.get("url") or ""),
                question=str(oc.get("question") or ""),
            )
        )
    return out
