"""outcome 解析口径的**单一真源**（方案 23 · 2026-10-30）。

这里住三件事，因为它们在「一个任务一个轨迹」改造前**散在三个文件里、
并且已经漂过一次**：

1. :func:`resolve` —— 「这个访问对应哪条 outcome」；
2. :func:`site_conclusions` —— 轨迹内的**站点级明细**（按**访问**枚举）；
3. :func:`site_entries` —— 轨迹内的**站点级明细**（按 **url 键**枚举，
   黄金集候选集的站点行就是它）；
4. :func:`task_conclusion` —— **任务级结论**（owner 裁定 ②：看最后一个结论）。

为什么单独成一个顶层包
----------------------

三方要读**同一份**口径，而三方的 import 边界互不相通：

- **P2**（``assembler/schema.py``）不 import executor——重建 P2 不能依赖
  执行端当前版本；
- **T 视图**（``assembler/task_view.py``）不 import executor、也不 import
  ``assembler.schema`` / ``assembler.views``（D14.3 / D14.6，AST 门禁钉着）；
- **评估侧**（``evaluation/golden/``）两边都能读。

⇒ 只有挂在 ``trajectory_pipeline/`` 顶层的**纯模块**能被三方同时读到——
与 ``toolcatalog`` 同一理由（工具声明的单一真源也是这么放的）。
本模块**不 import 任何功能模块**（含 executor / assembler）。

⚠️ 口径曾经漂过，且漂的方向很贵
--------------------------------

三处各写一套配对，结果是：

- P2 按「候选 url → 落地 url → 播放页 url」匹 ``outcome.url``，**取首个**；
- T 视图**按位置**配（``visits[i]`` ↔ ``outcomes[i]``）——而 ``outcomes[]``
  与 ``visits[]`` **不是一一对应**的（孤儿记账、候选阶段记账、多轮访问）；
- 执行器自己（``executor/orchestrator.py::_outcome_json_for``）先匹
  ``site_url``，再匹 ``url``。

实测（2026-10-30 · 131 份有 outcome 的真实存档）：

- T 的按位置配对与 P2 的 URL 配对在 **109/127 份存档（86%）** 上给出
  **不同**的结论——其中一份把**成功报成失败**；
- P2 的「取首个」与本模块的「取末条」在 **29 个访问**上分支不同
  （同一站点先后记了两条 ``player.ok`` 结论，末条才是最终结论）。

⇒ 统一到本模块：**候选 → 落地 → 播放页**逐档试，每档匹 ``site_url``
**或** ``url``，**同档取末条**（末条 = 访问阶段的最终结论）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "SiteConclusion",
    "SiteEntry",
    "TaskConclusion",
    "resolve",
    "resolve_at",
    "site_conclusions",
    "site_entries",
    "task_conclusion",
]


#: 「没有结论」用的分支名。
#:
#: 为什么不是 ``None``：``None`` 在这套体系里是**成功**的记号，
#: 而「这条轨迹压根没有站点结论」（搜索阶段被反爬拦、或一个候选都没访问）
#: 是**判不出来**——两件事在报表上差得很远（一个是成功样本，一个是废样本）。
#: ``unresolved`` 是封闭分支集里「感知层未给出结论（fail-closed）」那一支，
#: 用它表示「没有结论」是 fail-closed 的：宁可说判不出来，不许说成功。
UNRESOLVED = "unresolved"


# ── 数据形状 ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SiteConclusion:
    """一个**访问**（一个站点）的结论。

    ``branch is None`` = 成功（**仅在** ``matched`` 为真时成立）。
    """

    #: 访问序号，从 1 起（与 P1 的 ``visits[i]`` 对齐，``i`` 从 1 计）。
    index: int
    #: 候选链接（搜索结果给的地址）。
    url: str
    #: 落地后的地址（可能与候选不同：``http→https`` 跳转）。
    landed_url: str
    #: 分支名。``None`` = 成功。
    branch: str | None
    #: 配到的原始 outcome（``None`` = 没配到，属缺件）。
    outcome: Mapping[str, Any] | None
    #: 配到的 outcome 在 ``outcomes[]`` 里的下标（``-1`` = 没配到）。
    #:
    #: **为什么必须带**：候选集的站点行按 **url** 建键，P2 的访问点按
    #: **访问顺序**枚举——两张表要合成一张，光靠 url 会撞（两个站点跳到
    #: 同一个地址）。outcome 下标是两边共有的唯一身份。
    outcome_index: int = -1

    @property
    def matched(self) -> bool:
        """有没有配到 outcome。**没配到 ≠ 失败**，是缺件。"""
        return self.outcome is not None

    @property
    def is_success(self) -> bool:
        return self.matched and self.branch is None


@dataclass(frozen=True, slots=True)
class SiteEntry:
    """**一条 url 键**的站点级结论（黄金集候选集的站点行就是它）。

    为什么既有 :class:`SiteConclusion` 又有它
    -----------------------------------------
    两者的**主键不同**：

    - :class:`SiteConclusion` —— 主键是**访问**（``visits[]`` 里的第几个）；
    - :class:`SiteEntry` —— 主键是 **url**（黄金集 ``golden_id`` 就是
      ``{task}__{run}__{url 哈希}``，人核标签挂在它上面）。

    ⇒ 同一个访问点可能被两个 url 键命中（候选链接与落地链接不同），
    同一个 url 也可能被两个访问点命中（访问了两次同一个站）。
    **两张表都要** ⇒ 才排得出「哪条访问点配哪条 url 行」。

    ⚠️ ``outcome_index`` 是 ``outcomes[]`` 里的下标，**不是访问序号**
    （两者不是一一对应的：孤儿记账、候选阶段记账、多轮访问）。
    """

    #: 这个 url 键的地址（``outcome.url``）。
    url: str
    #: 配到的 outcome（``None`` = 这个 url 压根没有 outcome）。
    outcome: Mapping[str, Any] | None
    #: 配到的 outcome 在 ``outcomes[]`` 里的下标（``-1`` = 没有）。
    outcome_index: int = -1
    #: 这条 outcome **有没有被某个访问点配到过**。
    #:
    #: ``False`` = 孤儿记账（候选阶段记的那条，或访问阶段记了但没配上）。
    #: 孤儿**照样要进候选集**——它带着人核标签，丢了就是掉线。
    matched_by_visit: bool = False
    #: 配到它的访问点序号（``0`` = 没有）。
    visit_index: int = 0

    @property
    def branch(self) -> str | None:
        """分支名。``None`` = 成功；:data:`UNRESOLVED` = 判不出来。"""
        if self.outcome is None:
            return UNRESOLVED
        b = self.outcome.get("branch")
        return UNRESOLVED if b is None else str(b)


@dataclass(frozen=True, slots=True)
class TaskConclusion:
    """一条**轨迹**（一个 Run）的任务级结论。

    owner 裁定 ②（2026-10-30）：**任务级结论看最后一个结论**。
    这条规则是**暂定**的——「最后一个」的语义（末条记账 vs 末个访问）
    在实测里两者一致（见模块 docstring），但「多条冲突结论时谁说了算」
    的完整优先级表还没定。

    ⚠️ **``branch is None`` 只表示成功。** 判不出来时 ``branch`` 是
    :data:`UNRESOLVED`（封闭分支集里那一支），不是 ``None``——
    两件事在报表上差很远（一个进成功池，一个两个池都不进）。
    """

    #: 分支名。``None`` = 成功；:data:`UNRESOLVED` = 判不出来。
    branch: str | None
    #: 末条 outcome（``None`` = 这条轨迹压根没有任何站点结论）。
    outcome: Mapping[str, Any] | None
    #: 末条 outcome 在 ``outcomes[]`` 里的下标；``-1`` = 没有 outcome。
    index: int
    #: 这条轨迹共有多少条 outcome。
    total: int
    #: 取这条的理由（审计用：为什么是它）。
    reason: str

    @property
    def is_success(self) -> bool:
        """成功。**判不出来不算成功。**"""
        return self.outcome is not None and self.branch is None

    @property
    def has_conclusion(self) -> bool:
        """有没有站点结论。"""
        return self.outcome is not None


# ── 配对 ──────────────────────────────────────────────────────────


def resolve_at(
    outcomes: Sequence[Mapping[str, Any]],
    *wants: str,
) -> tuple[int, Mapping[str, Any]] | None:
    """同 :func:`resolve`，但**连下标一起给**（``(下标, outcome)``）。

    为什么要有下标：候选集���站点行是按 **url** 建键的，而 P2 的访问点是按
    **访问顺序**枚举的——两张表要合到一张上，光靠 url 会撞（两个站点跳到
    同一个地址）。**outcome 在 ``outcomes[]`` 里的下标是两边共有的唯一
    身份** ⇒ 配对时用它对齐，不用猜。

    ⚠️ 下标指的是 **``outcomes[]`` 里的位置**，不是「访问序号」——
    两者不是一回事（``outcomes[]`` 与 ``visits[]`` 不是一一对应的）。
    """
    for want in wants:
        if not want:
            continue
        hit: tuple[int, Mapping[str, Any]] | None = None
        for i, o in enumerate(outcomes or ()):
            if o.get("url") == want or o.get("site_url") == want:
                hit = (i, o)
        if hit is not None:
            return hit
    return None


def resolve(
    outcomes: Sequence[Mapping[str, Any]],
    *wants: str,
) -> Mapping[str, Any] | None:
    """按 URL 找那条 outcome：**候选 → 落地 → 播放页**逐档试，**同档取末条**。

    每档同时匹 ``site_url`` 与 ``url``：

    - ``site_url`` 记的是**候选链接**（``RunLedger`` 两个 URL 字段各管一层，
      见 ``executor/branches.py::SiteOutcome`` 的 docstring）；
    - ``url`` 记的是**结论形成处**的地址（导航失败时=候选、找控件失败时=落地、
      到播放页时=播放页）。

    **同档取末条**而不是首个：同一个候选地址可能在**候选阶段**先被记过
    （老 agent loop 路径给每个候选记一条 ``unresolved``），而**访问阶段的
    结论记在后面**——末条才是这个站点的最终结论。实测 29 个访问上
    「取首个」与「取末条」给出不同分支。

    Args:
        outcomes: P1 的 ``outcomes[]``（顺序 = 记账顺序 = 时间序）。
        *wants: 候选 URL、落地 URL、播放页 URL（按优先级）。

    Returns:
        配到的 outcome（原始 dict），配不到返回 ``None``。

    ⚠️ **返回 ``None`` 与「成功」是两回事**：``None`` = 缺件，
    成功是 ``outcome["branch"] is None``。调用方别把两者混起来。
    """
    hit = resolve_at(outcomes, *wants)
    return hit[1] if hit is not None else None


def site_conclusions(archive: Mapping[str, Any]) -> tuple[SiteConclusion, ...]:
    """一条轨迹的**站点级明细**：按 ``visits[]`` 顺序，每个访问配它的结论。

    配不上的访问照样进列表（``matched=False``）——**缺件要看得见**，
    悄悄少一条会让「访问了 8 个站点」读成「访问了 7 个」。
    """
    outcomes = archive.get("outcomes") or []
    out: list[SiteConclusion] = []
    for i, visit in enumerate(archive.get("visits") or (), 1):
        player_obs = visit.get("player_obs")
        player_url = (
            str(player_obs.get("url") or "")
            if isinstance(player_obs, Mapping)
            else ""
        )
        hit = resolve_at(
            outcomes,
            str(visit.get("url") or ""),
            str(visit.get("landed_url") or ""),
            player_url,
        )
        o = hit[1] if hit is not None else None
        out.append(
            SiteConclusion(
                index=i,
                url=str(visit.get("url") or ""),
                landed_url=str(visit.get("landed_url") or ""),
                branch=o.get("branch") if o is not None else None,
                outcome=o,
                outcome_index=hit[0] if hit is not None else -1,
            )
        )
    return tuple(out)


def site_entries(archive: Mapping[str, Any]) -> tuple[SiteEntry, ...]:
    """一条轨迹的**站点级明细**（按 **url 键**）——黄金集候选集的站点行。

    口径（**C 口径**，实测过，别自己改）
    ------------------------------------
    每个 url 出一行，行里的 outcome 这样选：

    1. **优先取「被某个访问点配到过」的那条**（多条时取末条访问的那条）；
    2. 该 url **一条都没被访问点配到过** ⇒ 取该 url 的**末条** outcome
       （这是**孤儿记账**——候选阶段记的那条 ``unresolved``）。

    为什么不是「同 url 一律取末条」
    ------------------------------
    因为 P2 的访问点用**逐档配对**（候选 → 落地 → 播放页，每档匹
    ``site_url`` 或 ``url``，同档取末条）取自己的 outcome。两套口径在
    **79/1386 个访问**上给出不同的 outcome ⇒ 候选行的 ``code`` 与 P2
    访问点的 outcome 会**对不上**，于是「这个站点是轨迹里哪一段」这个问题
    就没法答了（同一段步会被判成另一个站点的证据）。

    ⇒ 口径 C 让**两边取到同一条 outcome**（判据：outcome 下标）。

    为什么不能只留访问点配到过的（口径 B）
    --------------------------------------
    实测 **205 条 outcome 配不到任何访问点**（候选阶段的记账），
    而它们**带着人核标签**——删掉就是人核标签掉线。

    ⇒ 口径 C 的 **url 键集合与 v1–v16 逐字相同**（实测 A−C = 0、C−A = 0），
    306 条人核标注一条不丢；口径 B 会丢掉 83 条标注挂不上的键。
    """
    outcomes = list(archive.get("outcomes") or [])
    # 访问点 → outcome 下标（逐档配对，同档取末条）。
    visit_of: dict[int, int] = {}
    for concl in site_conclusions(archive):
        if concl.outcome_index >= 0:
            visit_of[concl.outcome_index] = concl.index

    # 按 url 分组，记住每条 outcome 的下标。
    by_url: dict[str, list[int]] = {}
    for i, o in enumerate(outcomes):
        by_url.setdefault(str(o.get("url") or ""), []).append(i)

    out: list[SiteEntry] = []
    for url, idxs in by_url.items():
        # 口径 C 第 1 步：优先取被访问点配到过的（取**末个**访问点的那条）。
        hits = [(visit_of[i], i) for i in idxs if i in visit_of]
        if hits:
            _, idx = max(hits)
            out.append(
                SiteEntry(
                    url=url,
                    outcome=outcomes[idx],
                    outcome_index=idx,
                    matched_by_visit=True,
                    visit_index=visit_of[idx],
                )
            )
            continue
        # 口径 C 第 2 步：孤儿记账 ⇒ 取该 url 的末条。
        idx = idxs[-1]
        out.append(
            SiteEntry(
                url=url,
                outcome=outcomes[idx],
                outcome_index=idx,
                matched_by_visit=False,
                visit_index=0,
            )
        )
    return tuple(out)


# ── 任务级结论 ────────────────────────────────────────────────────


def task_conclusion(archive: Mapping[str, Any]) -> TaskConclusion:
    """一条轨迹的**任务级结论**（owner 裁定 ②：看最后一个结论）。

    取 ``outcomes[]`` 的**末条**——那是记账顺序上的最后一条决策，
    也就是这条轨迹「最后的结论」。

    ⚠️ **末条不一定是「末个访问」的结论**：候选阶段的记账排在访问阶段
    之前，而访问阶段的结论也排在它之后，所以末条**通常**就是末个访问的
    结论；个别存档里末条是候选阶段的收尾记账（实测 131 份里 6 份），
    那时以末条为准（它仍是「最后的结论」）。

    **判不出来时** ``branch`` 是 :data:`UNRESOLVED` 而不是 ``None``——
    「没判出来」与「成功」在报表上差得很远，合成一个就看不出废样本。
    """
    outcomes = archive.get("outcomes") or []
    if not outcomes:
        return TaskConclusion(
            branch=UNRESOLVED,
            outcome=None,
            index=-1,
            total=0,
            reason="本轨迹没有任何站点结论——判不出来（fail-closed）",
        )
    last = outcomes[-1]
    return TaskConclusion(
        branch=last.get("branch"),
        outcome=last,
        index=len(outcomes) - 1,
        total=len(outcomes),
        reason=f"取末条结论（{len(outcomes)} 条里的第 {len(outcomes)} 条）",
    )
