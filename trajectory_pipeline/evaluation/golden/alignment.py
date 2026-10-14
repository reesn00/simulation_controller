"""URL 播放黄金集标签的**语义等价类**——D10 口径 C 的唯一出处。

设计来源：``docs/设计方案/06-URL播放黄金集与轨迹黄金集标签对齐方案.md``
§4.3（D10 口径 C 升级为等价类一致）。

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
    # 未判明
    "unresolved": "UNKNOWN",
    "player_unverified": "UNKNOWN",
    "unknown": "UNKNOWN",
}

#: 全部等价类名（供报表/测试枚举；与 :data:`EQUIV_CLASS` 同一出处派生）。
ALL_EQUIV_CLASSES: frozenset[str] = frozenset(EQUIV_CLASS.values())

#: 刻意分离的两组：断言它们不在同一等价类（防止以后有人误合并）。
SPLIT_CONTENT_VS_REACH = ("broken", "unreachable_hard")


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
