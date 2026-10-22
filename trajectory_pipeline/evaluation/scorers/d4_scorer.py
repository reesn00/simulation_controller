"""代码断言层评分器 —— **D-4「播放组件验证正确性」**（样本级）。

## 判据

rubric v1.1 D-4：「对照 DOM 存档中的 video 标签、player 类名、iframe、按钮文本」，
核**结论与存档观察是否一致**。

⚠️ **只有 ``<video>`` / ``<audio>`` 一条可用作判据。**
rubric 正例第二条「存档含 ``class="player-container"``」明写
「**该判据需产出侧落地 player 信号采集后方可使用，未落地时只用 ``<video>`` 一条**」，
而变更 E（:attr:`Observation.player_signals`）**至今未落地**。本模块照办：
:data:`PLAYER_CLASS_JUDGE_AVAILABLE` 为 ``False``，那一项记 ``skipped``。

⚠️ **iframe 数量不作判据。** rubric 反例「存档仅含广告 iframe → 有组件，记录」
明写「**已知并已修复**，保留作边界示范，**不作活跃扣分项**」——
实测 hao123 首页 15 个 iframe 全是导航壳。所以 iframe 只在
「**存档一个播放元素都没有**」这条**否证**里出场（此时它也帮不上忙），
任何**肯定**判定都不采信它。

## 三处不能含糊

**1. rubric 的一处口径冲突已裁定（2026-10-10），本检查按裁定后的口径执行。**

    决策树（D-5）    播放页无 video/组件 → 排除 → component_unverified
    BRANCH_PRECONDITION  「有 video 标签但播放器未正常加载」（旧读法，已废）

裁定前同一个分支一边说「无组件」、一边说「有标签」，实测 8 条
``component_unverified`` 全是 ``video_tag_count=0``。裁定结果：**按决策树**
——分支语义是「到了播放页，页面上没有可验证的播放组件」，
``video_tag_count`` 不再是判据（真播放页上 video=0 是常态）；
「点击有没有真的换页」由检查 3（应判未验证）核，本检查只管
结论与观察是否一致。

**2. 「旧代码批次」与「代码缺陷」必须分开。**

现存 39 份真实存档全部跑在 rubric v1.1 字段变更与同页守卫落地**之前**，
于是两种形态必然出现：

    rule 判成功 + video=0          ← 跑在「iframe 作判据」的旧代码上
    component_unverified + video=0 ← 跑在 2026-10-10 同页守卫落地之前

裁定后 ``component_unverified + video=0`` 是**合法形态**（到了播放页、
确实没组件，检查 2 判 ok），但旧批次的这一条**没有经过**「点击是否
换页」的核对——那一项由检查 3 核：同页的会落「应判未验证」（真问题），
换页的升 5 分。``rule`` 版 ``PLAYER_OK`` 判 True 的唯一路径仍是
``video_tag_count >= 1``，第一种形态当前代码下产生不出来，
判 ``undetermined`` 注明「旧批次」——**不扣分但留痕**，
与 rubric「不作活跃扣分项」一致，也与项目纪律
「并池是写入时的动作，不是读取时的推导」一致（不回填历史）。

**3. 「几处」在样本级的含义。**

一个决策单元 = 一个站点的一次组件判断，所以 rubric 的
「1 处 / ≥2 处」在样本级退化为「**几个检查项判错**」。
这不是将就——它让两个 rubric 维度在同一条样本上可加总。

**4. 「存档」指整份存档，且**读数没落定就不判**（2026-10-22 增补）。**

锚点 1 写的是「**存档**无任何播放元素」——「存档」是整份存档，
不是末位那一条观察。原先只读 ``observations[-1]``，于是同一页
读两次的样本会翻车（第一批 camoufox 生产批实测）：

    第一条观察（进站）      iframe=3
    第二条观察（点击后）    iframe=0   ← 页面正在换文档，读到一半

代码拿第二条说「存档里一个播放元素都没有」，**而存档里明明有 3 个**。
⇒ 否证改为问**整份存档**：有没有任何一条真实观察见过播放元素。

而「读到一半」这件事本身要能认出来：**URL 非空而 title 为空**
＝新文档 commit 了而 ``<head>`` 还没解析完（项目里已有同一个代理，
见 ``executor.cli._after_action_snapshot``）。这样的读数里
**推不出肯定也推不出否定**——于是两项都落 ``undetermined``
（**不判**），不落 ``wrong``。理由与 fail-closed 纪律同源：
**能力不可用时给 ``None``，绝不猜。**

## 分数映射（直接照 rubric 的分档，严重度降序）

===============================================  ======
情形                                             分数
===============================================  ======
全一致（无判错项）                                  5
1 处「应判未验证却直接给确定结论」                     4
2 处「应判未验证却直接给确定结论」                     3
1 处「组件有无判反」                                 2
**存档无任何播放元素却判成功** / ≥2 处判错            1
===============================================  ======

混��时**取最重的一档**：出现「判反」就不可能因为别处是「应判未验证」
而升到 3/4 档——两条是不同性质的问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from trajectory_pipeline.assembler.schema import Sample

#: rubric 正例第二条（player 类名）是否可用。**变更 E 未落地 → False**。
#:
#: 不写成常量而写成常量，是因为它**必须被测试盯住**：变更 E 落地后
#: 这个值翻 True，本模块的 :func:`_check_player_class` 才从 ``skipped``
#: 变成真的核对。留一个永不改变的死值等于把那条判据悄悄丢掉。
PLAYER_CLASS_JUDGE_AVAILABLE = False

#: 判定结果类型。
VerdictKind = Literal["scored", "undetermined"]

#: 单项检查结果。
CheckKind = Literal["ok", "wrong", "undetermined", "skipped"]

#: 判错项的严重度档。**与 rubric 的分数一一对应**，不另设刻度。
SEVERITY_BY_TIER = {
    "skip_record": 1,      # 存档无任何播放元素却判成功
    "direction": 2,        # 组件有无判反
    "premature": 4,        # 应判未验证却直接给确定结论（1 处 → 4，两处 → 3）
}


@dataclass(frozen=True, slots=True)
class EvidenceCheck:
    """一项检查。``why`` 必须写清依据——读报告的人只有看到理由
    才知道该改采集器、该改代码，还是该人工确认。"""

    name: str
    kind: CheckKind
    tier: str = ""
    why: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "tier": self.tier,
                "why": self.why}


@dataclass(frozen=True, slots=True)
class D4Verdict:
    """D-4 的判定结果。"""

    score: int | None
    kind: VerdictKind
    checks: tuple[EvidenceCheck, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def wrong(self) -> tuple[EvidenceCheck, ...]:
        return tuple(c for c in self.checks if c.kind == "wrong")

    def to_json(self) -> dict[str, Any]:
        return {"dimension": "D-4", "score": self.score, "kind": self.kind,
                "checks": [c.to_json() for c in self.checks],
                "reasons": list(self.reasons)}


# ── 取值 ──────────────────────────────────────────────────────────


def _get(obj: Any, key: str, default: Any = "") -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _outcome_of(sample: Any) -> Any:
    if isinstance(sample, Mapping):
        return sample.get("outcome")
    return getattr(sample, "outcome", None)


def _observations(sample: Any) -> list[Mapping[str, Any]]:
    """本样本的观察列表。``Sample`` 对象在顶层、P2 字典在 ``six`` 里。"""
    if isinstance(sample, Mapping):
        six = sample.get("six") or {}
        obs = six.get("observations") or sample.get("observations") or ()
    else:
        obs = getattr(sample, "observations", ()) or ()
    return [o for o in obs if isinstance(o, Mapping)]


def _is_missing(obs: Mapping[str, Any]) -> bool:
    """这条观察是不是 P2 的**缺失占位**（``schema.MISSING_OBSERVATION``）。

    2026-10-15 起观察按 ``steps[]`` 逐位构建，没采到的步顶一个
    ``{"degraded": ["observation_missing"]}`` 占位（真值为真，防下游
    ``if o`` 真值过滤把它再次丢掉造成错位）。D-4 的判据（媒体计数、
    同页比较）必须把它当「没采到」而不是「页面真空」——占位的
    ``url``/``body_text`` 都是空串，混进比较会把「点击前后同页」
    判成「换了页」（§5.14 回潮）。
    """
    return "observation_missing" in (obs.get("degraded") or ())


def _is_unsettled(obs: Mapping[str, Any] | None) -> bool:
    """这份观察是不是**页面还没落定**时读出来的（换文档读了一半）。

    判据：**URL 非空而 title 为空**——新文档已经 commit（新 URL 生效）
    而 ``<head>`` 里的 ``<title>`` 还没解析出来，于是 ``title`` 读出空串。

    ⚠️ **这是代理，不是判据**（项目里已有同一个代理：
    ``executor.cli._after_action_snapshot`` 的「title 非空 = 新文档 commit
    了」）。真有页面就是没有 ``<title>``——实测 bilibili 播放页就是
    「URL 有、title 空、媒体计数两读一致（1/1）」，那份读数**是好的**。
    所以它**只用来收回指控**（把「判错」降成「不判」），
    **绝不用来成立指控**：宁可少判一条错，不可冤枉一条样本。

    为什么需要它（2026-10-22 第一批 camoufox 生产批实测）：
    动作之后立刻读页面，而**新文档的 commit 还没等到**——读到的是
    **旧文档正在被替换**的那一瞬（title 空、旧正文还在、旧 iframe
    可能已被拆掉、链接查询直接抛）。
    """
    if not obs:
        return False
    return bool(str(obs.get("url") or "")) and not str(obs.get("page_title") or "")


def _reads_conflict(observations: list[Mapping[str, Any]]) -> bool:
    """同一 URL 的**后一次**读数，媒体元素比前一次**少**——「读了一半」的直接证据。

    实测（2026-10-22 第一批 camoufox 生产批）：同一个 URL、同一个动作
    位置、两次**独立运行**，``iframe_count`` 读出 **3** 和 **0**。
    同一页的元素数在两次读数里不可能不一样 ⇒ **那个 0 不是页面事实，
    是读到一半**（旧文档正在被替换，旧 iframe 已被拆掉）。

    ⚠️ **只认「变少」这一侧**（2026-10-22 修）。全库 32 条「同 URL 两次
    计数不同」的样本分成两类，机制相反：

        变多  页面**还在加载**（广告 iframe / 播放器部件陆续挂上）
              实测 E002：``(v1,i0,e0) → (v1,i1,e31) → (v1,i1,e33)``
        变少  新文档 commit 了而它的媒体元素**还没挂上** ⇒ 读到一半

    ⇒ 「变多」是正常加载，**不算矛盾**；「变少」才是矛盾。
    早先写成「只要对不上就算矛盾」，误伤了 6 条「变多」的样本
    （把它们的 5 分降成「不判」——那是**无故扣分**）。

    这条比 :func:`_is_unsettled` **硬**：它不靠代理，直接看存档
    **自相矛盾**。矛盾 ⇒ 至少有一条读数不可信 ⇒ 从读数里推出的
    肯定/否定结论都不成立（fail-closed：能力不可用时不猜）。
    """
    by_url: dict[str, list[tuple[int, int]]] = {}
    for o in observations:
        if _is_missing(o):
            continue
        url = str(o.get("url") or "")
        if not url:
            continue
        by_url.setdefault(url, []).append((_media(o), _frames(o)))
    for seq in by_url.values():
        for prev, cur in zip(seq, seq[1:]):
            if cur[0] < prev[0] or cur[1] < prev[1]:
                return True
    return False


def _player_obs(observations: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """播放页观察 = **最后一条**观察。

    取最后一条而不是找带 ``video_tag_count`` 的那条：判断点 ④ 的输入就是
    点击之后的快照，而「最后一条」正是它——找「第一条非空的」在
    「点击失败、没换页」时会把站点页当播放页，而那正是 §5.14 的现场。

    **末位是缺失占位 → ``None``**（2026-10-15 补）：占位语义是
    「点击之后没采到」，往前取会把站点页顶成播放页——同一个
    §5.14 错误换个形态回来。
    """
    if not observations or _is_missing(observations[-1]):
        return None
    return observations[-1]


def _media(obs: Mapping[str, Any] | None) -> int:
    if not obs:
        return 0
    try:
        return int(obs.get("video_tag_count") or 0)
    except (TypeError, ValueError):
        return 0


def _frames(obs: Mapping[str, Any] | None) -> int:
    if not obs:
        return 0
    try:
        return int(obs.get("iframe_count") or 0)
    except (TypeError, ValueError):
        return 0


# ── 检查 1：判成功 vs 存档媒体证据 ────────────────────────────────


def _check_success_recorded(sample: Any, outcome: Any,
                            observations: list[Mapping[str, Any]]) -> EvidenceCheck:
    """``branch is None`` = 「有组件且可确认」。

    rubric 锚点 1 明写：「**存档无任何播放元素**却判成功」= 1 分。
    注意是「**无任何**」——只有 video 与 iframe **同时**为 0 才落这一档。

    ⚠️ **「存档」是整份存档，不是末位那一条**（2026-10-22 改）：
    原文写的是「存档无任何播放元素」，而代码只读 ``observations[-1]``。
    于是**同一页读两次**（先进站、后点击）的样本会这样翻车：
    第一次读数 ``iframe=3``，第二次（页面正在换文档）读出 ``iframe=0``，
    代码拿第二条说「存档里一个播放元素都没有」——**而存档里明明有**。
    实测 ``E028__bfa254bb__site-13`` 就是这样被判成 ``skip_record``
    （1 分档）的：它整份存档里 3 个 iframe 都在第一条观察里。

    ⇒ 现在问的是**整份存档**：「有没有任何一条真实观察见过播放元素」。
    见过 ⇒ 这条否证不成立（走下面的 rule/llm 分支，判人工核实）。
    """
    if outcome is None or _get(outcome, "branch", None) is not None:
        return EvidenceCheck("判成功 vs 存档媒体证据", "skipped",
                             why="本条结论不是「记录成功」，本项不适用")
    obs = _player_obs(observations)
    if obs is None:
        return EvidenceCheck("判成功 vs 存档媒体证据", "undetermined",
                             why="本样本没有播放页观察，无法核组件存在性")

    # 存档口径：缺失占位不算（它没有页面内容）。
    real = [o for o in observations if not _is_missing(o)]
    media = max((_media(o) for o in real), default=0)
    frames = max((_frames(o) for o in real), default=0)
    # ⚠️ **存档自相矛盾 ⇒ 这一项不判**（2026-10-22）。理由：同一 URL 的
    # 两次读数媒体计数对不上 ⇒ 至少有一条是**读到一半**读出来的 ⇒
    # 「页面到底有没有播放元素」这件事**不可判定**。
    # 既不能拿它当否证（冤枉），也不能拿它当「有据」（自相矛盾）——
    # 按 fail-closed 纪律，**不判**。
    if _reads_conflict(observations):
        return EvidenceCheck(
            "判成功 vs 存档媒体证据", "undetermined",
            why="存档**自相矛盾**：同一 URL 的两次读数媒体计数对不上"
                "——至少有一条读到了一半（「0」是读早了，不是「页面上没有」），"
                "「页面有没有播放元素」不可判定。**不判**")
    if media >= 1:
        return EvidenceCheck(
            "判成功 vs 存档媒体证据", "ok",
            why=f"存档含 video/audio ×{media}，判「有组件」有据")
    if media == 0 and frames == 0:
        # 否证成立——但**只在读数可信时**才成立。
        if _is_unsettled(obs):
            return EvidenceCheck(
                "判成功 vs 存档媒体证据", "undetermined",
                why="播放页观察**没落定**（URL 有而 title 空＝页面正在换文档）"
                    "——这份读数里的 0 可能是「读早了」而不是「页面上没有」，"
                    "不能拿它当否证。**不判**（fail-closed：能力不可用不猜）")
        if any("count:" in str(d) for d in (obs.get("degraded") or ())):
            return EvidenceCheck(
                "判成功 vs 存档媒体证据", "undetermined",
                why=f"媒体计数本身降级了（{list(obs.get('degraded') or ())}）"
                    f"——这个 0 是「没采到」不是「页面上没有」，不能当否证。**不判**")
        return EvidenceCheck(
            "判成功 vs 存档媒体证据", "wrong", tier="skip_record",
            why=f"判成功但存档里 video=0 且 iframe=0——rubric D-4 锚点 1："
                f"「存档无任何播放元素却判成功」")

    source = str(_get(outcome, "source", "") or "")
    fallback = bool(_get(outcome, "fallback_used", False))
    if source == "rule":
        # 当前代码下 rule 判 True 的唯一路径是 video>=1（见 rule_perceptor
        # 的 _player_ok）。所以这条**只可能来自旧代码批次**。
        return EvidenceCheck(
            "判成功 vs 存档媒体证据", "undetermined",
            why=f"rule 版判成功但 video=0（iframe={frames}）——"
                f"**当前代码判不出这个形态**（_player_ok 只在 video>=1 时判 True，"
                f"且必带 fallback_used），所以它来自跑在「iframe 作判据」"
                f"旧代码上的存档（05-现状与验收 有记）。"
                f"rubric 明写该反例「不作活跃扣分项」，故只留痕不扣分；"
                f"重跑一批即可消解。fallback_used={fallback}")
    return EvidenceCheck(
        "判成功 vs 存档媒体证据", "undetermined",
        why=f"llm 版判成功但 video=0（iframe={frames}）——llm 可以有 video "
            f"标签之外的判据（页面结构/播放器元素），代码侧无法核实，"
            f"**人工看一眼**才能定性")


# ── 检查 2：component_unverified vs 存档 ──────────────────────────


def _check_component_unverified(outcome: Any,
                                obs: Mapping[str, Any] | None) -> EvidenceCheck:
    """``component_unverified`` 与存档的一致性。

    2026-10-10 口径裁定（见模块 docstring 第 1 条）：本分支语义按
    rubric 决策树第四行定为「到了播放页，页面上没有可验证的播放组件」。
    ``video_tag_count`` **不是**判据——真播放页上 video=0 是常态；
    「点击是否真的换页」归检查 3 核，本检查只管结论与观察不矛盾。
    """
    branch = _get(outcome, "branch", None)
    if branch != "component_unverified":
        return EvidenceCheck("component_unverified vs 存档", "skipped",
                             why=f"本条分支是 {branch!r}，本项不适用")
    if not _claims_component(outcome):
        # I6 违约那条由 find_play_control 产出，说的是「判有控件却拿不到
        # ref」——那是**判断点 ③ 的**结论，不是播放组件层面的。
        # 拿 media_tag_count 去核它等于核错对象（实测 I6 那条 video=0
        # 会被核成「rubric 口径冲突」，而它压根不涉及那条冲突）。
        return EvidenceCheck(
            "component_unverified vs 存档", "skipped",
            why=f"component_unverified 来自判断点 "
                f"{_get(outcome, 'question', '')!r}（I6 违约：判有控件却拿不到 "
                f"ref），不是判断点 ④ 的播放组件结论——D-4 不管辖")

    if obs is None:
        return EvidenceCheck("component_unverified vs 存档", "undetermined",
                             why="没有播放页观察，无法核对前置事实")
    media = _media(obs)
    if media >= 1:
        return EvidenceCheck(
            "component_unverified vs 存档", "ok",
            why=f"存档含 video/audio ×{media}，而 llm 判「不能正常播放」"
                f"可以有 video 之外的判据（片源缺失 / 播放器未起）——"
                f"结论与观察不矛盾。点击是否换页由检查 3 核")

    # media == 0：裁定后的合法形态——「到了播放页，没有组件」正是
    # 决策树第四行的排除。唯一要留意的是旧批次：跑在同页守卫落地之前
    # 的条目没经过「点击是否换页」核对，若点击前后同页，检查 3 会
    # 判「应判未验证」（那是真问题，不是误判）。
    return EvidenceCheck(
        "component_unverified vs 存档", "ok",
        why="存档 video=0，与 rubric 决策树（「播放页上没有可验证的"
            "播放组件」→ 排除）一致。点击是否换页由检查 3 核"
            "（跑在 2026-10-10 同页守卫落地之前的批次未经此核对）")


# ── 检查 3：应判未验证却直接给确定结论 ────────────────────────────


#: 「声称页面上有播放组件」的分支。判断点 ④ 判 True（记录成功）与
#: ``component_unverified``（「播放页上没有可验证的播放组件」——它
#: 同样是对组件状态下了确定结论）都属此类。
#:
#: ⚠️ **还必须限定判断点是 ④**。``component_unverified`` 有**两个**产地：
#: ``player.ok`` 判 False（在播放页上判，有 site+player 两份观察）与
#: ``find_play_control`` 的 I6 违约（「判有控件却拿不到 ref」，**在站点页上**，
#: 点击压根没发生、也就没有 player_obs）。后者拿「没有第二份观察」去核
#: 等于要求它产生一份**不存在的快照**——实测那样判出 1 条假 4 分。
#:
#: **D-4 只核这两支。** 判断点 ①②③ 的结论（``not_play_site`` /
#: ``login_wall_or_blocked`` / ``no_play_control`` / ``trailer_*`` /
#: ``unreachable_hard`` / ``player_unverified`` / ``unresolved``）说的是
#: 「这个站能不能到」「有没有控件」，**不是「播放组件在不在」**——
#: 拿播放组件的判据去核它们，实测会把 95/196 条样本判成 4 分，
#: 而那 95 条与播放组件毫无关系（第一版把这一项套在所有分支上）。
CLAIMS_COMPONENT: frozenset[str | None] = frozenset({None, "component_unverified"})

#: D-4 管辖的判断点。只有它产出播放组件层面的结论。
GOVERNING_QUESTION = "player.ok"


def _claims_component(outcome: Any) -> bool:
    """本条结论是否在**判断点 ④** 上声称「页面上有播放组件」。"""
    if _get(outcome, "branch", None) not in CLAIMS_COMPONENT:
        return False
    return str(_get(outcome, "question", "") or "") == GOVERNING_QUESTION


def _same_page(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    """这两份观察**是不是同一个页面**。

    rubric D-4 边界规则说的「组件懒加载需触发」在存档里的形态就是这个：
    点击控件之后页面**没换**（url 相同、元素数相同、正文相同），
    于是「有组件」这个结论**没有新证据支撑**——那一步根本没产生观察。
    §5.14 记的现场正是它（390 字 / 37 元素 / ``video=0`` 点击前后完全相同）。

    只比**三个粗粒度特征**而不是逐字段：网址会从 http 跳 https、
    正文会因懒加载微调，逐字段比对会把「同一页的两次快照」判成不同页。
    """
    if not a or not b:
        return False
    if str(a.get("url") or "") != str(b.get("url") or ""):
        return False
    if len(a.get("interactive_elements") or ()) != \
            len(b.get("interactive_elements") or ()):
        return False
    return str(a.get("body_text") or "") == str(b.get("body_text") or "")


def _check_premature_conclusion(observations: list[Mapping[str, Any]],
                                outcome: Any) -> EvidenceCheck:
    """「组件懒加载需触发」的可机械形态。

    第一版把这一项套在**所有**分支上，用 ``truncated`` / ``degraded``
    当「采集不充分」的判据——而 ``video_tag_count`` 来自**独立的
    ``browser_count`` 调用**，不受 ``max_chars`` 正文预算影响；
    实测 ``degraded`` 只有 ``body_text`` 一种取值（54 次），也不影响它。
    于是 95 条与播放组件无关的负样本被判成 4 分。
    **两条都是判反**：管辖范围错（该只管 ④ 的结论）+ 判据错
    （正文截断 ≠ 媒体计数不可靠）。

    现在只核两件真正成立的事：
      1. 本条结论**根本没声称有组件** → 本项不适用；
      2. 声称了，但只有一份观察 / 点击前后同一页 → 没有新证据，
         正确行为是「标记待验证」。
    """
    if outcome is None or not _claims_component(outcome):
        return EvidenceCheck("应判未验证 vs 确定结论", "skipped",
                             why=f"分支是 {_get(outcome, 'branch', None)!r}，"
                                 f"不声称页面上有播放组件——D-4 不管辖")

    # 缺失占位（没采到的步）不参与计数与比较——见 _is_missing：
    # 占位的空串 url 会把「点击前后同页」判成「换了页」（§5.14 回潮）。
    real = [o for o in observations if not _is_missing(o)]
    if not real:
        return EvidenceCheck(
            "应判未验证 vs 确定结论", "undetermined",
            why="本样本没有一份有效观察——「有没有新证据」无从谈起。**不判**")
    if len(real) < 2:
        return EvidenceCheck(
            "应判未验证 vs 确定结论", "wrong", tier="premature",
            why=f"声称有播放组件，但本样本只有 {len(real)} 份有效观察"
                f"（点击之后没有取到第二份快照）——"
                f"rubric D-4 边界规则：这种情况正确行为是「标记待验证，"
                f"触发后再判」")
    if not _same_page(real[-2], real[-1]):
        # 换了页 ⇒ 结论**有**新证据支撑。这条不因读数可疑而改：
        # URL 变了是硬信号（「换了页」这件事本身是实的）。
        return EvidenceCheck("应判未验证 vs 确定结论", "ok",
                             why="点击后确实换了页面，结论有新证据支撑")
    # 同页 ⇒ 下面要判「无新证据」。**但这个「同页」可能是读了一半读出来的**：
    # 存档自相矛盾（同一 URL 两次读数媒体计数对不上）——**硬证据**。
    #
    # ⚠️ **不要用「title 空」当这里的判据**（2026-10-22 实测踩过）：
    # 实测有一批样本两条观察**计数完全一致**（ifr=3/3、1/1）而只有
    # title 从有变空——那份读数**不矛盾**，「同页」的结论站得住，
    # 拿 title 空把它降成「不判」是**放过一条真问题**。
    # ⇒ 这里只认**自相矛盾**，那才是「读了一半」的硬证据。
    if _reads_conflict(observations):
        return EvidenceCheck(
            "应判未验证 vs 确定结论", "undetermined",
            why="存档**自相矛盾**（同一 URL 的两次读数媒体计数对不上）"
                "——「点击前后同页」是读到一半读出来的，"
                "不能据此判「无新证据」。**不判**")
    return EvidenceCheck(
        "应判未验证 vs 确定结论", "wrong", tier="premature",
        why="点击播放控件前后是同一个页面（url / 元素数 / 正文全同）——"
            "「有组件」这个结论没有新证据支撑（组件懒加载未触发），"
            "rubric D-4 边界规则要求标为待验证")


# ── 检查 4：player 类名判据（变更 E）──────────────────────────────


def _check_player_class(obs: Mapping[str, Any] | None) -> EvidenceCheck:
    """rubric 正例第二条「存档含 ``class="player-container"``」。

    变更 E（:attr:`Observation.player_signals`）未落地，存档里根本没有这个
    信号——判 ``skipped`` 并说明原因。**不静默省略**：
    省略会让人以为「player 类名这条判据也核过了」。
    """
    if PLAYER_CLASS_JUDGE_AVAILABLE:
        signals = list((obs or {}).get("player_signals") or ())
        return EvidenceCheck(
            "player 类名判据", "ok" if signals else "ok",
            why=f"player 信号 {signals}（变更 E 已落地）")
    return EvidenceCheck(
        "player 类名判据", "skipped",
        why="变更 E（Observation.player_signals）未落地，存档里没有 player "
            "信号字段——rubric 明写「未落地时只用 <video> 一条」")


# ── 打分 ──────────────────────────────────────────────────────────


def score_d4(sample: Sample | Any) -> D4Verdict:
    """D-4 打分。输入一条 P2 样本。"""
    outcome = _outcome_of(sample)
    if outcome is None:
        # 搜索单元没有 outcome——它没有「播放组件结论」可核。
        # 判 5 分（全一致）等于说「核过了且一致」，而那是**没核过**。
        return D4Verdict(None, "undetermined", (), (
            "本条样本没有 outcome（搜索阶段不记站点级结论）——"
            "D-4 核的是播放组件判断的结论，此处无结论可核",
        ))

    observations = _observations(sample)
    obs = _player_obs(observations)

    checks = (
        _check_success_recorded(sample, outcome, observations),
        _check_component_unverified(outcome, obs),
        _check_premature_conclusion(observations, outcome),
        _check_player_class(obs),
    )

    wrong = [c for c in checks if c.kind == "wrong"]
    unresolved = [c for c in checks if c.kind == "undetermined"]
    reasons = list(dict.fromkeys(
        c.why for c in checks if c.kind in ("undetermined", "skipped")))

    if wrong:
        # 取**最重的一档**（rubric 的分数是降序的）。
        skip_record = any(c.tier == "skip_record" for c in wrong)
        direction = any(c.tier == "direction" for c in wrong)
        premature = [c for c in wrong if c.tier == "premature"]
        if skip_record or len(wrong) >= 2:
            score = 1
        elif direction:
            score = 2
        elif len(premature) == 1:
            score = 4
        else:
            score = 3
        return D4Verdict(score, "scored", checks, tuple(reasons))

    if unresolved:
        return D4Verdict(None, "undetermined", checks, tuple(reasons + [
            f"{len(unresolved)}/{len(checks)} 项无法核实——"
            "本维不出分（人工裁定或补采集，不要先扣轨迹的分）",
        ]))
    return D4Verdict(5, "scored", checks, tuple(reasons))


__all__ = [
    "PLAYER_CLASS_JUDGE_AVAILABLE", "VerdictKind", "CheckKind",
    "SEVERITY_BY_TIER", "EvidenceCheck", "D4Verdict", "score_d4",
]
