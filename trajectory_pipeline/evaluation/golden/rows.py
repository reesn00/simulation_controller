"""黄金集**行形状**的唯一真源（方案 23 · 2026-10-30 · owner 裁定 ①④）。

两种行形状
----------

**站点级（legacy）**
    一行 = 一次**访问**（一个站点）的结论。
    ``golden_id`` = ``{task}__{run}__{url_hash}``。

**轨迹级（方案 23 起）**
    一行 = 一条**轨迹**（一个 Run）。
    ``golden_id`` = ``{task}__{run}``，站点级明细收在 ``site_outcomes[]`` 里。

为什么两套形状要并存
--------------------
「数据集的交付粒度」与「测量的口径」是**两件事**：

- owner 裁定 ①/④：**黄金集数据**改成一条轨迹一行（人核的单元也是一条轨迹）；
- D-10（口径 C）的测量单元是**站点**——人核的是站点，code 判的也是站点。

⇒ 轨迹级化**只改交付粒度，不改测量口径**。所以每个读者都要先过
:func:`site_rows` 展开成站点视图，读到的分母与从前逐字一致。

⚠️ **分档仍然是站点级的**
------------------------
实测（:mod:`trajectory_pipeline.research.probes.per_run_golden_shape`）：
**33/50 条轨迹的站点跨着 ≥2 档**，其中 **28** 条**三档全跨**
（calibration / regression / holdout）。
⚠️ **「33」是「跨着 ≥2 档」、「28」才是「三档全跨」，两个数不是一件事**
（复算见该探针末段「两个『28』的关系（防混）」）——混起来读就是
「三档全跨 33 条」⇒ 台账 **Q-26** 裁的就是「跨档轨迹按哪一档算」。
⭐ **Q-26 已裁（owner 2026-11-01）：按「头部站点」定档** ⇒ 档闸见
:func:`headline_split`（⚠️ 与取严的**交集只有 2 条**，且封存面泄漏面
11/13 ⇒ **已加「整条沾着封存面/回归面就不放行」的兜底**
（:func:`non_reflow_splits`）⇒ 实测放行数 **13 ⇒ 2** ⇒ 详见该函数的
docstring）。

按轨迹重切档 ⇒ D-10 的分母整个换掉（当前头条 106/128 = 82.8%），
而那个分母是**绑定口径**（门禁只读调参池；分母薄不是改口径的理由）。
⇒ **分档留在站点级**：每个站点自带 ``split``，测量时按它过滤。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = [
    "NON_REFLOW_SPLITS",
    "SITE_SCOPE",
    "STRICT_ORDER",
    "TRAJECTORY_SCOPE",
    "SOURCE_TRAJECTORY",
    "expand_site_rows",
    "headline_site",
    "headline_split",
    "index_site_entries",
    "is_trajectory_row",
    "non_reflow_splits",
    "row_splits",
    "site_entries_of",
    "site_ids_of",
    "site_rows",
    "splits_within",
    "strict_split",
    "trajectory_id",
]


#: 人核标注的两种 scope。``site`` = 一次访问；``trajectory`` = 一条轨迹。
SITE_SCOPE = "site"
TRAJECTORY_SCOPE = "trajectory"

#: 轨迹级候选行的 ``source_kind``。
#:
#: ⚠️ **不叫 ``p2_sample``**：旧的 ``p2_sample`` 行是「per-unit 轨迹」——
#: 一个 Run 被拆成 ``__search`` / ``__site-1`` / … 好几条，每条一个 sample_id。
#: per-Run 之后这些**合并成一条**，而那条带站点级明细 ⇒ 它不是「没有站点结论的
#: 轨迹」，它是**有站点明细的轨迹**。继续叫 ``p2_sample`` 会让
#: 「D-10 只算 ``source_kind != "p2_sample"``」这条判据把带金标的轨迹级行
#: 一起排除掉。
SOURCE_TRAJECTORY = "trajectory"


def trajectory_id(task_id: str, run_id: str) -> str:
    """轨迹级 ``golden_id`` = ``{task_id}__{run_id}``。

    与 P2 的 ``sample_id``（:func:`trajectory_pipeline.assembler.schema.sample_id`）
    **逐字相同**——同一个 Run 在两条管线里是同一条轨迹，键必须一样，
    否则跨层对不上又得靠人工猜。
    """
    return f"{task_id}__{run_id}"


def is_trajectory_row(row: Mapping[str, Any]) -> bool:
    """这一行是不是轨迹级（方案 23 的新形状）。

    判据是**结构**（有没有 ``site_outcomes``）而不是 ``source_kind``——
    结构判据对**手写/迁移过来**的行同样成立，而 ``source_kind`` 可能被改过。
    """
    return "site_outcomes" in row


def site_rows(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把一行展开成**站点视图**列表（测量层读的就是这个）。

    - 站点级行（旧形状）⇒ 原样返回一行（**不复制**，避免把调用方的写操作
      变成「改了展开出来的副本」这种静默失效）；
    - 轨迹级行 ⇒ 每个 ``site_outcomes[]`` 条目展成一行。

    站点视图的键与旧的站点级行**逐字一致**（``golden_id`` / ``source_kind`` /
    ``url`` / ``site_url`` / ``play_page_url`` / ``access_surface`` / ``code`` /
    ``persona`` / ``golden`` / ``retest``），另加两个**新键**：

    - ``trajectory_id``：所属轨迹（轨迹级化之后，「这一行属于哪条轨迹」要能查出来）；
    - ``split``：该站点所属档（冻结时写；读不到就是空串 = 未冻结）。
    """
    if not is_trajectory_row(row):
        return [dict(row)]

    out: list[dict[str, Any]] = []
    for site in row.get("site_outcomes") or ():
        view = dict(site)
        view.setdefault("task_id", row.get("task_id"))
        view.setdefault("run_id", row.get("run_id"))
        view.setdefault("persona", row.get("persona"))
        view.setdefault("retest", None)
        view["trajectory_id"] = str(row.get("golden_id") or "")
        view.setdefault("split", "")
        out.append(view)
    return out


def expand_site_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """一批行 ⇒ 展平成站点视图列表（保持原顺序）。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        out.extend(site_rows(row))
    return out


def site_ids_of(row: Mapping[str, Any]) -> set[str]:
    """这一行涉及的**站点级 id 集合**。

    轨迹级行 ⇒ 它的所有 ``site_outcomes[].golden_id``；
    站点级行 ⇒ 它自己的 ``golden_id``。

    用途：重建候选集的**孤儿门禁**——人核标注挂在站点级 id 上，
    候选集改成轨迹级之后，那些 id 不在顶层了，得往站点明细里找。
    """
    if not is_trajectory_row(row):
        return {str(row.get("golden_id") or "")}
    return {
        str(s.get("golden_id") or "")
        for s in (row.get("site_outcomes") or ())
        if s.get("golden_id")
    }


def headline_site(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """轨迹的**头一个站点** = 给出轨迹级结论的那一个（裁定 ②：末条结论）。

    找法按**记账下标**（``outcome_index``）对齐轨迹的 ``code_index``——
    不是按分支值猜（同一个分支可能出现在多个站点上）。

    找不到就返回 ``None``：**不猜**。轨迹级结论的归属站点判不出来时，
    应当由调用方显式处理「这条轨迹的头部站点缺失」，而不是随便挑一个。
    """
    if not is_trajectory_row(row):
        return None
    want = row.get("code_index")
    if want is None:
        return None
    for site in row.get("site_outcomes") or ():
        if site.get("outcome_index") == want:
            return dict(site)
    return None


def headline_split(row: Mapping[str, Any]) -> str:
    """这一行（轨迹级）的**头部站点档** —— **owner 2026-11-01 裁的 ② 口径**。

    头部站点 = **裁定② 的末条结论所在的那个站**（:func:`headline_site`）；
    档 = 该站点明细条目自带的 ``split``（**冻结时写下的站点级档**）。

    口径换过一次
    ------------
    原来档闸用 :func:`strict_split`（跨档**取严**）⇒ v16 的 50 条轨迹里只有
    **7** 条够格进金标（**全是「纯 calibration」的**）。owner 2026-11-01 裁
    **② 头部站点口径**。

    ⚠️⚠️ **但两个数不是「宽严关系」**（2026-11-01 逐条核对实测）：

    - **交集只有 2 条** ⇒ ② **不是**「取严的放宽」，是**换了一批数据**；
    - ② 放行的 **13** 条里 **11 条含 holdout（封存面）站点** ⇒
      **封存面泄漏面 = 11/13**（与裁定③「holdout 的裁定不进金标」相抵）；
    - **9 条**定不出档（头部站点认不出 ⇒ fail-closed 挡掉）⇒ 其中 **5 条**的
      档集是「纯 calibration」（⇒ 挡掉不涉封存面，只是少收 5 条干净数据）
      ⇒ 「补数据」补的就是这 5 条。⚠️ **这 9 条不是同一类**：**6 条是负样本池行**
      （`source_kind: "negative_pool"` ⇒ **从来就不是轨迹** · 没有 P1 存档）·
      **3 条是头部站点配不到**（那个访问点没进冻结集）。

    ⚠️ **上面这三个数是 owner 裁 ② 之「后」核对出来的**——裁的时候台账里
    只有「13 vs 7」⇒ **不是「owner 知情下裁的」** ⇒ 数字已摆到台账 Q-26
    ⇒ **要收回（回取严）或加兜底（「沾 holdout 就不回流」）就改本函数**
    （与 `golden_ls/reflow.py` 的档闸）。

    ⭐ **兜底已加（owner 2026-11-01「泄露做兜底」）**
    ------------------------------------------------------
    「头部站点在调参池」**不够**：其余站点里的封存事实会跟着轨迹级标签一起
    进调参池 ⇒ **封存面作废** ⇒ 加一道**整条沾着封存面/回归面就不放行**
    的兜底（:func:`non_reflow_splits`）。

    ⚠️⚠️ **读数（v16 · 走产品代码路径实测）**：

    - ② 无兜底放行 **13** ⇒ **加兜底后只剩 2**（被挡 11 条：2 条只沾
      holdout · 9 条 holdout+regression 都沾）；
    - 取严放行 **7** ⇒ **② + 兜底（2）比取严（7）还少**——差的 5 条是
      **「头部站点认不出」的残缺轨迹**（被 ② 的 fail-closed 挡，**不是被兜底挡**）；
    - ⇒ **② + 兜底 ⊂ 取严**（交集 2）。

    ⚠️ **兜底量的是「冻结集里那些站点」的档** ⇒ 挡得住「已冻结的封存站点」，
    挡不住「没冻结过的访问点里藏着封存事实」——那是**冻结集覆盖不到**的问题
    （另一件事，见台账 Q-26）。

    ⇒ **取严口径保留**（:func:`strict_split`）作对照，不再是档闸用的那个。

    ⚠️ **fail-closed**：头部站点认不出 ⇒ 返回 **``""``** ⇒ 调用方**必须**把
    它当「**定不出档 ⇒ 不放行**」处理。
    ⚠️⚠️ **不是**「没有档 ⇒ 放行」——那是把「解不出来」当成「没有限制」
    ⇒ 恰恰是**最不该放**的那种情况（解不出来的那条往往**沾着封存面**）。
    """
    site = headline_site(row)
    if site is None:
        return ""
    return str(site.get("split") or "")


def site_entries_of(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """这一行的**站点明细条目**（**原样**，不展平成视图）。

    - 轨迹级行 ⇒ ``site_outcomes[]`` 里的条目；
    - 站点级行（旧形状）⇒ 行本身。

    与 :func:`site_rows` 的差别：那个**摊平**成测量视图（补
    ``trajectory_id`` / ``split`` 等键），这个**保持原样**——
    读 ``dimension_scores`` / ``status`` 这类**写在明细条目里**的字段时
    要用它（摊平视图会把它们带上，但那是顺带；这里语义更准）。
    """
    if not is_trajectory_row(row):
        return [dict(row)]
    return [dict(s) for s in (row.get("site_outcomes") or ())]


def index_site_entries(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """**站点级 id → 明细条目**的索引（轨迹级 + 旧形状一起收）。

    为什么需要它
    ------------
    候选集改成轨迹级之后，站点行的 ``golden_id`` 不在顶层了——它在
    ``site_outcomes[]`` 里。而 ``_refresh_scores`` / ``_refresh_code``
    要按 ``golden_id`` 把 pred 侧从候选集重挂到冻结行上：**按顶层查会
    一个都查不到** ⇒ 每条站点行都会落进「缺失」⇒ 冻结直接 fail-closed。

    ⇒ 这张索引把两层都收进来，调用方只管按 id 查。

    ⚠️ **重键会被覆盖**：同一站点级 id 在两行里出现时，**后出现的赢**。
    这与候选集「一条轨迹一行」的前提一致（一个站点级 id 只属于一条轨迹）。
    真撞上了说明候选集有病——但覆盖不报错，**由 orphan 门禁去报**。
    """
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        for entry in site_entries_of(row):
            gid = str(entry.get("golden_id") or "")
            if gid:
                out[gid] = entry
    return out


# --------------------------------------------------------------------------
# 档位：分档是站点级的，轨迹跨档
# --------------------------------------------------------------------------


def row_splits(row: Mapping[str, Any]) -> dict[str, int]:
    """这一行涉及的**档位分布**（档名 → 条数）。

    - 站点级行 ⇒ ``{该行的档: 1}``（未冻结 ⇒ ``{}``）；
    - 轨迹级行 ⇒ **各站点档的计数**（一条轨迹可以跨着几档）。

    ⚠️ **轨迹行没有单一档位**（实测 50 条里 28 条三档全跨，见
    :mod:`trajectory_pipeline.research.probes.per_run_split_membership`）
    ⇒ 谁问「这一行是哪一档」，都必须先过这个函数，**不许拿顶层
    ``__split__`` 硬读**（轨迹行压根没有这个键）。
    """
    if not is_trajectory_row(row):
        split = str(row.get("__split__") or "")
        return {split: 1} if split else {}
    counter = row.get("splits")
    if isinstance(counter, Mapping) and counter:
        return {str(k): int(v) for k, v in counter.items()}
    out: dict[str, int] = {}
    for site in row.get("site_outcomes") or ():
        if not isinstance(site, Mapping):
            continue
        split = str(site.get("split") or "")
        if split:
            out[split] = out.get(split, 0) + 1
    return out


#: **取严顺序**：holdout > regression > calibration。
#:
#: ⚠️ 为什么 holdout 比 regression 更严：两者都「不许回流」，但 holdout
#: 是**封存面**（D-C5：只在最终验收跑一次），regression 是**回归面**
#: （D-C8：改了 rubric 才跑）。封存面连「人看过」都不该发生。
STRICT_ORDER: tuple[str, ...] = ("holdout", "regression", "calibration")


#: **不许回流**的两档：**封存面**（holdout · D-C5「只在最终验收跑一次」·
#: 裁定③「只上传、只被人看，但不回流」）与**回归面**（regression · D-C8
#: 「改了 rubric 才跑」· C-17「回归集里的行不得改动」）。
#:
#: ⚠️ **这两档不是「更严一点」——是「不许进金标」**：进了金标 = 进了调参池
#: ⇒ 封存面作废、回归面不再独立。
NON_REFLOW_SPLITS: tuple[str, ...] = ("holdout", "regression")


def non_reflow_splits(row: Mapping[str, Any]) -> set[str]:
    """这一行**沾着**哪些「不许回流」的档（封存面 / 回归面）。

    用途：**兜底**（owner 2026-11-01 在裁了 ② 头部站点口径之后追加的一条）——
    「头部站点在调参池」**不够**：其余站点里的封存事实会跟着轨迹级标签
    一起进调参池 ⇒ 封存面作废 ⇒ **一条轨迹只要沾着封存面 / 回归面的站点，
    整条不进金标**。

    ⚠️ **量的是「冻结集里那些站点」的档**（轨迹行的顶层 ``splits`` /
    ``site_outcomes[].split``）⇒ **没被冻结的访问点不在其中**——这是
    「分档留在站点级」的**测量底座**决定的（全仓库口径一致），不是漏。
    ⇒ 这条兜底挡得住「已冻结的封存站点」，挡不住「没冻结过的访问点里藏着
    封存事实」——那属于**冻结集覆盖不到**的问题（另一件事）。
    """
    return set(row_splits(row)) & set(NON_REFLOW_SPLITS)


def strict_split(row: Mapping[str, Any]) -> str:
    """这一行的**取严档**（跨档时取最严的那一档）。

    站点级行 ⇒ 它自己的档。轨迹级行 ⇒ 各站点档里**最严**的那一档。

    为什么要有「取严」这个口径
    ------------------------
    轨迹跨档（28/50 条三档全跨）⇒ 「这条轨迹算哪一档」没有唯一定义。
    而档位决定的是**能不能进金标**（裁定③：holdout 的裁定不进金标 ·
    C-17：回归集不得改动）。

    ⇒ **取严**：只要沾上 holdout / regression，就按那档处理。
    「取宽」的口径（按多数档、按头部站点档）会让人拿一个沾着封存内容的
    轨迹级标签去调参 ⇒ 封存面失效。**宁可少回流，不许错回流。**

    ⚠️ 站点级行的档**不取严**（它本来就只在一档里）。

    ⚠️ ⚠️ **「取宽」不是「不可计算」——两条口径都在桌上**（2026-11-01 实测订正）：
    早先写「替代口径按头部站点**算不出来**（44/50 配不到）」是**探针的键形状
    bug**（`split_of_url` 用 `(task__run, url)` 两段键，探针按三段查 ⇒ 永远
    查不到）。订正后：取严 **7** 条够格进金标 vs 头部站点 **13** 条
    （v16 的 50 条轨迹）。

    ⭐ **owner 2026-11-01 裁了 ②（头部站点口径）** ⇒ **档闸不再用本函数**
    （改用 :func:`headline_split`）⇒ 本函数**保留作对照口径**。
    ⚠️⚠️ **两个数不是「宽严关系」**（同日逐条核对）：**交集只有 2 条** ⇒
    ② 是**换了一批数据**，不是「放宽」；且 ② 放行的 13 条里 **11 条含
    holdout 站点** ⇒ **封存面泄漏面 11/13** ⇒ **已加兜底**
    （:func:`non_reflow_splits`）⇒ 实测放行数 **13 ⇒ 2**（**比本口径的 7 还少**
    ——少的 5 条是「头部站点认不出」的残缺轨迹，被 ② 的 fail-closed 挡）。
    ⇒ **要回取严口径**就改档闸那**一处**（`golden_ls/reflow.py` 的档闸 +
    兜底），不是散着改。
    """
    splits = row_splits(row)
    for split in STRICT_ORDER:
        if split in splits:
            return split
    return ""


def splits_within(row: Mapping[str, Any], allowed: Sequence[str]) -> bool:
    """这一行的**全部档位**都在 ``allowed`` 里吗？

    轨迹上传的筛选口径：**整条都在本次范围内**才推。
    ⚠️ 不用「有交集就行」——那会把一条跨着封存内容的轨迹整个推上去
    （页面上摆的是整条轨迹，**没法只推一半**）。
    """
    splits = set(row_splits(row))
    if not splits:
        return False
    return splits <= set(allowed)
