"""URL 播放黄金集标签的**语义等价类**——D10 口径 C 的唯一出处。

设计来源：原《URL 播放黄金集与轨迹黄金集标签对齐方案》§4.3（D10 口径 C 升级为等价类一致；原稿已于 2026-10-25 随废弃文本清理删除）。

为什么需要这张表：
    现有 ``builder.cmd_agreement`` 的口径 C 是**字符串相等**
    （``y_true == y_pred`` 逐条 ``t == p``）。阶段 1 新增
    ``login_required`` / ``blocked`` / ``broken`` / ``partial_free``
    后，新代码输出与历史 golden 标签必然不相等（如代码判 ``blocked``、
    历史标注是 ``login_wall_or_blocked``），D10 会**假性暴跌**——不是
    模型变差而是口径变了。等价类把「语义相同」的标签归入一组，口径 C
    改为 ``equiv_class(t) == equiv_class(p)``。

本模块与分支注册表一样坚持「一个出处」：全部等价关系只在
:data:`EQUIV_CLASS` 里定义一次，下游（`builder.cmd_agreement`、
报表）只许调用函数，不许散落第二份映射。

关键裁定（保持现状，刻意分离）：
    ``broken`` → ``BROKEN_CONTENT``，``unreachable_hard`` → ``REACH_FAIL``
    ——一个是「内容损坏」（页面/媒体在**但坏**），一个是「环境导航失败」
    （**连页面都没到**）。新体系 L0 的 HTTP 404/403 初筛只会落
    ``broken``，不落 ``unreachable_hard``（后者现有语义是 DriverError，
    刻意不用 HTTP 状态码当判据，对齐方案 §4.3 明写保留这条现状）。

失败行为：
    :func:`equiv_class` 对**未登记标签**抛 ``KeyError``（fail-closed）。
    返回 ``None`` / 原样透传会静默放过拼错的分支名，等价类的价值就没了；
    谁把新标签接进来却忘了登记，D10 当场炸而不是悄悄算错。
"""

from __future__ import annotations

from typing import Mapping

#: 语义等价类（key = 现有体系分支名或新体系标注标签；value = 等价类名）。
#:
#: 覆盖两组来源：
#:   ① 现有体系分支：``executor.branches.BRANCH_LABELS`` 的全部 14 个 key
#:     （13 活跃 + 历史合并态 ``login_wall_or_blocked``）+ 成功标签
#:     ``success``（builder 的 ``SUCCESS_LABEL``）；
#:   ② 新体系 VLM 标注标签：``playable`` / ``ads_then_playable`` /
#:     ``paywall`` / ``empty`` / ``unknown``——历史 golden 与新代码比较时
#:     以现有体系锚点为准，VLM 标签只是挂入，不新增等价类。
#:
#: 清单由 :mod:`tests.unit.test_alignment` 与分支注册表做同步校验，
#: 新增分支未登记映射会在契约测试里当场红。
EQUIV_CLASS: Mapping[str, str] = {
    # 能播
    "success": "SUCCESS",
    "playable": "SUCCESS",
    "ads_then_playable": "SUCCESS",
    # 付费
    "subscription_wall": "PAYWALL",
    "paywall": "PAYWALL",
    "partial_free": "PAYWALL",
    # 访问阻断（历史合并态挂入，不回填细分）
    "login_wall_or_blocked": "ACCESS_BLOCKED",
    "login_required": "ACCESS_BLOCKED",
    "blocked": "ACCESS_BLOCKED",
    # 无播放资源
    "no_play_control": "NO_PLAY_RESOURCE",
    "component_unverified": "NO_PLAY_RESOURCE",
    "empty": "NO_PLAY_RESOURCE",
    # 内容/网络损坏——broken 与 unreachable_hard **刻意分离**
    "unreachable_hard": "REACH_FAIL",
    "broken": "BROKEN_CONTENT",
    # 不是播放站 / 预告
    "not_play_site": "NOT_PLAY_SITE",
    "trailer_only": "TRAILER",
    "trailer_suspect": "TRAILER",
    # 发现链（2026-10-16 路径 B/C）。①′/⑤′ 的「候选类型不对」与 ①
    # 的 not_play_site 同口径；四支「页面无产出」（聚合站无入口 /
    # 文章页无外链 / 无站内搜索 / 搜了没有）与 no_play_control
    # 同口径——页面在手，但没有可看的东西。金标侧的 empty 归
    # NO_PLAY_RESOURCE，正好接上。
    "not_aggregator": "NOT_PLAY_SITE",
    "not_article": "NOT_PLAY_SITE",
    "aggregator_no_entries": "NO_PLAY_RESOURCE",
    "article_no_links": "NO_PLAY_RESOURCE",
    "no_site_search": "NO_PLAY_RESOURCE",
    "site_search_no_results": "NO_PLAY_RESOURCE",
    # 路由记账不是结论：派发了子候选、结论由子候选自己的 visit 记
    # （NON_SAMPLE，不入池）。独立等价类——金标侧没有对应标签，
    # 挂进任何既有类都会让它与某个金标标签「意外等价」。
    "site_search_routed": "ROUTED",
    # 未判明
    "unresolved": "UNKNOWN",
    "player_unverified": "UNKNOWN",
    "unknown": "UNKNOWN",
}

#: 全部等价类名（供报表/测试枚举；与 :data:`EQUIV_CLASS` 同一出处派生）。
ALL_EQUIV_CLASSES: frozenset[str] = frozenset(EQUIV_CLASS.values())

#: 刻意分离的两组：断言它们不在同一等价类（防止以后有人误合并）。
SPLIT_CONTENT_VS_REACH = ("broken", "unreachable_hard")


# ---------------------------------------------------------------------------
# 合并记账（方案 16 C-13；owner 2026-10-23 裁定 D-C4）
# ---------------------------------------------------------------------------
#
#: **D-C4 裁定：合并等价类不需 owner 签。**
#:
#: 原建议是「等价类变更 owner 签」（理由：合并等价类是"放水"最省力的路径，
#: 且当时无人记账）。owner 裁掉签字那一半，**记账那一半留着**——因为
#: 「无人记账」是事实，换个签名人并不能让它变成有人记账。
#:
#: 于是本模块给每个**不止一个标签**的等价类配一张注记：
#:
#: - ``canonical``：该类的规范标签（代表）；
#: - ``merged``：被并进来的标签——并进来之后它们**互相不可分辨**；
#: - ``detail_lost``：合并后看不见的那层细分（一句话，说的是**丢了什么**，
#:   不是「为什么等价」）；
#: - ``reason``：为什么并（指到真实的出处：某份方案 / 某次分支拆分）。
#:
#: **机械保证**（契约测试盯）：一个类只要标签数 > 1 就**必须**有注记，
#: 且注记里的 ``canonical + merged`` 必须**恰好等于**该类在
#: :data:`EQUIV_CLASS` 里的全部标签。于是：
#:
#: - 偷偷合并 ⇒ 标签数变了而注记没跟上 ⇒ 测试红；
#: - 注记写错（标签对不上 / 理由空着 / 丢了什么没写）⇒ 测试红。
#:
#: **单一标签的类不需要注记**——它没发生过合并，「合并后不可见细分」对它
#: 是空集。
#:
#: ⚠️ 注记只记**当前**这一版分组，不是流水账：改动本身由 git diff 留着，
#: 注记保证的是「改动落地时分组与记账同步」——这正是「无人记账」要防的。
EQUIV_CLASS_NOTES: Mapping[str, Mapping[str, str]] = {
    "SUCCESS": {
        "canonical": "success",
        "merged": ("playable", "ads_then_playable"),
        "detail_lost": "先有广告才播 vs 直接能播（ads_then_playable 的广告前置）",
        "reason": (
            "原对齐方案 §4.3：VLM 标签挂入现有体系锚点，"
            "广告前置不改变「能不能播」的结论"
        ),
    },
    "PAYWALL": {
        "canonical": "subscription_wall",
        "merged": ("paywall", "partial_free"),
        "detail_lost": "有无免费部分（partial_free 的试看段长度）",
        "reason": (
            "原对齐方案 §4.3：partial_free 归付费类——"
            "「试看一段」与「完全付费」在「本轮能不能拿到完整内容」上同解"
        ),
    },
    "ACCESS_BLOCKED": {
        "canonical": "login_required",
        "merged": ("blocked", "login_wall_or_blocked"),
        "detail_lost": (
            "阻断的具体成因（要登录 / 地区限制或反爬 / 验证码），"
            "以及历史合并态 login_wall_or_blocked 原本混装的那层"
        ),
        "reason": (
            "分支表 2026-10-12 把 login_wall_or_blocked 拆为 login_required / blocked"
            "（拆分不改变「这一轮被挡住」这个结论）"
        ),
    },
    "NO_PLAY_RESOURCE": {
        "canonical": "no_play_control",
        "merged": (
            "component_unverified",
            "empty",
            "aggregator_no_entries",
            "article_no_links",
            "no_site_search",
            "site_search_no_results",
        ),
        "detail_lost": (
            "「页面在手但没有可看的东西」的具体形态"
            "（控件缺失 / 组件不可验证 / 聚合站无入口 / 文章页无外链 / "
            "无站内搜索 / 搜索无结果）"
        ),
        "reason": (
            "路径 B/C 发现链（2026-10-16）七支与 no_play_control 同口径："
            "页面在手，但没有可看的东西"
        ),
    },
    "NOT_PLAY_SITE": {
        "canonical": "not_play_site",
        "merged": ("not_aggregator", "not_article"),
        "detail_lost": "非播放站的具体形态（聚合站导航站 / 文章社区页）",
        "reason": "路径 B/C 发现链：候选类型不对与 not_play_site 同口径",
    },
    "TRAILER": {
        "canonical": "trailer_only",
        "merged": ("trailer_suspect",),
        "detail_lost": (
            "「疑似」状态（trailer_suspect 词表判不准、待人工；"
            "它**不是**负样本，混进负样本池会污染池子）"
        ),
        "reason": (
            "两者都不是可播资源：疑似预告同样不产出播放，"
            "但细分必须留着（负样本池靠它把疑似样本挡在外面）"
        ),
    },
    "UNKNOWN": {
        "canonical": "unresolved",
        "merged": ("player_unverified", "unknown"),
        "detail_lost": (
            "未判明的三种形态（感知层没给出结论 / 有播放组件但确认不了能不能用 / "
            "VLM 未识别）"
        ),
        "reason": (
            "三种都不是「判为非播放站」——未判明必须留在未判明里，"
            "并进别的类会把它洗成负样本"
        ),
    },
}



def class_labels(cls: str) -> tuple[str, ...]:
    """某个等价类的全部标签（**派生**自 :data:`EQUIV_CLASS`，不手抄）。

    顺序按标签名字典序——清单要能逐字节复现，不靠字典哈希序。
    未登记的类名 → ``KeyError``（fail-closed）。
    """
    labels = tuple(sorted(lbl for lbl, c in EQUIV_CLASS.items() if c == cls))
    if not labels:
        raise KeyError(f"等价类 {cls!r} 在 EQUIV_CLASS 里没有标签")
    return labels


def invisible_details() -> tuple[str, ...]:
    """**「合并后不可见细分」清单**（C-13 的产出面）。

    一行一个等价类，形如::

        ACCESS_BLOCKED：阻断的具体成因（要登录 / 地区限制或反爬 / 验证码）…

    读法：「在 D-10 口径 C 下，这一类的哪些区分**已经分不出来了**」——
    合并等价类的代价就在这一行里。清单是**派生**的（:data:`EQUIV_CLASS_NOTES`
    + :data:`EQUIV_CLASS`），不是手抄的第二份。

    只列**发生过合并**的类（标签数 > 1）；单标签类不在清单里。
    """
    out: list[str] = []
    for cls in sorted(EQUIV_CLASS_NOTES):
        note = EQUIV_CLASS_NOTES[cls]
        out.append(f"{cls}：{note['detail_lost']}")
    return tuple(out)


def validate_merge_notes() -> list[str]:
    """合并记账与 :data:`EQUIV_CLASS` 的一致性检查（返回问题清单，空 = 通过）。

    契约测试调它。查四件事：

    1. **每个多标签类都有注记**——漏记 = 有人悄悄合了一个类；
    2. 注记的 ``canonical + merged`` **恰好等于**该类全部标签
       ——多一个（表里有、注记没有）或少一个（注记有、表里没有）都算错；
    3. ``canonical`` / ``merged`` 里的标签在 :data:`EQUIV_CLASS` 里
       确实指向这个类；
    4. ``detail_lost`` / ``reason`` 非空——「丢了什么」「为什么并」不许留白。

    返回**人读的问题清单**（不是抛异常）：这是检查工具，调用方决定怎么用；
    但它**不会**给「差不多通过」留口子——问题清单非空就是有问题。
    """
    problems: list[str] = []
    for cls in sorted(ALL_EQUIV_CLASSES):
        labels = class_labels(cls)
        note = EQUIV_CLASS_NOTES.get(cls)
        if note is None:
            if len(labels) > 1:
                problems.append(
                    f"{cls}：{len(labels)} 个标签却没有合并注记"
                    f"（{'、'.join(labels)}）"
                )
            continue
        canonical = note.get("canonical", "")
        merged = tuple(note.get("merged", ()))
        declared = tuple(sorted((canonical,) + merged))
        if declared != labels:
            problems.append(
                f"{cls}：注记声明的标签 {declared} 与 EQUIV_CLASS 实际 {labels} 不符"
            )
        for lbl in (canonical,) + merged:
            if EQUIV_CLASS.get(lbl) != cls:
                problems.append(
                    f"{cls}：标签 {lbl!r} 在 EQUIV_CLASS 里指向 "
                    f"{EQUIV_CLASS.get(lbl)!r}，不是 {cls}"
                )
        if not note.get("detail_lost", "").strip():
            problems.append(f"{cls}：detail_lost 是空的（没说合并后丢了什么）")
        if not note.get("reason", "").strip():
            problems.append(f"{cls}：reason 是空的（没说为什么并）")
    for cls in sorted(EQUIV_CLASS_NOTES):
        if cls not in ALL_EQUIV_CLASSES:
            problems.append(f"{cls}：注记里的类在 EQUIV_CLASS 里根本不存在")
    return problems


def equiv_class(label: str) -> str:
    """标签 → 等价类名。未登记标签抛 ``KeyError``（fail-closed）。"""
    return EQUIV_CLASS[label]


def is_equivalent(true_label: str, pred_label: str) -> bool:
    """D10 口径 C 的判定：两条标签是否语义等价。

    用法（与对齐方案 §4.3 一致）：:

        agree = sum(
            1 for t, p in zip(y_true, y_pred)
            if is_equivalent(t, p)
        )
    """
    return equiv_class(true_label) == equiv_class(pred_label)


def dim_agreement(
    true_score: int | float | None,
    pred_score: int | float | None,
    tolerance: int = 1,
    *,
    scale: tuple[int, int] = (1, 5),
) -> bool | None:
    """D10 软维度一致率（口径 C 衍生）的判定。

    rubric 软维度（如 d3 / d9）以 1-5 分档打分；与黄金集对应分档对比时，
    ``|t - p| <= tolerance`` 视为一致。

    返回值口径与硬分类 ``is_equivalent`` 不一致——后者总是 True/False，
    本函数多一个 ``None``：
      - 任一侧为 ``None``（不可判定 / 未填）→ 返回 ``None``，调用方**不计
        入分母**（项目纪律：「score=None != score=0」；plan §二.1）；
      - 两侧非 None 且差 <= tolerance → ``True``；
      - 否则 → ``False``。

    设计来源：``docs/设计方案/10-judge接入方案.md`` §三·第一步。
    ``tolerance`` 默认 1，由 ``cmd_agreement --dim-tolerance`` 透传，
    不在本函数里写死——首次跑出分布后再定阈值（plan §三·风险
    「dim_tolerance 不是 5 档而是 1-5 软一致」）。
    """
    if true_score is None or pred_score is None:
        return None
    try:
        t_int = int(true_score)
        p_int = int(pred_score)
    except (TypeError, ValueError):
        return None
    lo, hi = scale
    if not (lo <= t_int <= hi) or not (lo <= p_int <= hi):
        return None  # 越界值不当分处理（fail-closed）
    if tolerance < 0:
        return None  # 负容忍度无意义，按未对齐处理
    return abs(t_int - p_int) <= tolerance


#: 软维度名白名单——黄金集 ``dimension_scores`` 的合法 key。
#: 路由 plan §三·第一步 contract 1「软维度名在 ``d2..d9`` 子集」，
#: 写死全集而不是散落在调用方散落校验。
#: D-3 / D-9 judge 未建，但白名单里**预留**——本批接入后扩到 d3 / d9
#: 不需要改对齐层（fail-closed 同步：白名单外抛 ``ValueError``）。
SOFT_DIM_NAMES: frozenset[str] = frozenset(
    {f"d{i}" for i in range(2, 10)}
)


def validate_soft_dim_name(dim: str) -> str:
    """校验软维度名合法（白名单内）。

    ``cmd_agreement --soft-dim`` 接受多值；每一条都必须通过本函数。
    未登记名 **fail-closed**（抛 ``ValueError``）——白名单外静默放过会让
    拼错的 dim 算进一致率，等价类的价值就没了（plan §三·第一步 contract 1）。
    """
    if dim not in SOFT_DIM_NAMES:
        raise ValueError(
            f"软维度名 {dim!r} 不在白名单 {sorted(SOFT_DIM_NAMES)} 内"
        )
    return dim
