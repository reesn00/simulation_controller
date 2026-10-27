"""采集面（access surface）词表——「这次浏览是在哪张面上发生的」。

设计来源：``docs/设计方案/20-反爬拦截与金标观测面.md`` §R2（金标加两列）与
§R3（跨面比较前先问「两面是不是同一张面」）。

要解决的事实（方案 20 §2 实测）
------------------------------
同一台机器、同一天、同一批站点：**人工浏览器 12 个进得去、camoufox 8 个被
拦**。于是「人判这个站没片」与「agent 判这个站没片」可能根本不是在说同一件
事——**读数差可能来自面，不来自模型**。要分开这两件事，每条读数都得记
「这是在哪张面上看到的」，否则跨面比较只能**不可判定**。

为什么住在 ``executor/browser/``
--------------------------------
面是**驱动实现的属性**（哪份 stealth 配置、有没有代理层），采集侧（
``RunRecord.driver`` → 事件账本）与评估侧（金标行 ↔ 冻结行）都要用它。
放在评估侧会让 ``executor`` 反向 import ``evaluation``——采集是评估的**上游**，
反过来指就把分层倒了。评估侧那两个入口（标注行读面、比两面）在
:mod:`trajectory_pipeline.evaluation.access_surface` 里**再导出**本模块的词，
不另写第二份。

面 ≠ 工具面
-----------
``toolcatalog`` 的 ``obscura-1`` / ``camoufox-1`` / ``camofox-1`` 是**工具声明
方言**（模型看到的 ``click`` 参数长什么样）；这里说的是**浏览身份**——指纹、
出口 IP、是否登录态、有没有额外隐身层。两者今天恰好一一对应，**将来不会**：
给 camofox 挂住宅代理 + 隐身层，工具方言一个字不变（还是 ``camofox-1``），
而采集面已经换了——那正是 owner 暂缓的 O-c。所以**不复用工具面的名字**，
否则「加了隐身层」这件事在数据里看不出来。

命名口径
--------
``<实现>-<配置>``，两个名字都是**本仓库自己定的**：

===================  ==================================================
``human-browser``    复核员自己的浏览器（登录态、地区、插件都是他自己的）
``camofox-plain``    本仓库 camofox-browser 路径的固定配置：**无代理、
                     无额外隐身层**（2026-10-26 起是生产面）
``camoufox-plain``   同上口径的 camoufox 路径——**2026-10-26 起只读存量**：
                     camoufox 执行层已撤下，但存量的 ``blocked_surface``
                     记的就是它（盘上 2 份存档），**读入口要认**
``obscura-plain``    同上口径的 obscura 路径（2026-10-22 起降级为回溯路径）
===================  ==================================================

``plain`` 而不是 doc 初稿写的 ``default``：``default`` 会被读成「camoufox 库
的出厂默认」，而本仓库的固定配置里 ``humanize`` 是**开**的（出厂默认是关）、
``locale`` 是钉死的——叫 ``default`` 等于把三处反例藏进名字里。``plain`` 指的
是「**没有额外隐身 / 代理层**」，恰好是 O-c 那个待加的东西。

fail-closed 三条
----------------
1. **旧数据就是没记**：``""`` / ``None`` / 缺键 ⇒ :data:`SURFACE_UNRECORDED`，
   **不按默认面补**——历史批次到底跑在哪张面上，数据里没有证据
   （⚠️ ``dual_run`` 在**它自己的筛档**里把「没记 driver」当成 obscura 处理，
   那是「存量盘上只有 obscura」这一条**事后审计**的事实，不是本模块的口径）；
2. **认不出的面代号直接抛**，不回落 ``unrecorded``：面代号写错与没写是两回事，
   混成一个会让「填错了」永远看不见（与 ``toolcatalog.surface_of_driver``
   同纪律：不猜、不静默回落）；
3. **比不了就说比不了**：:func:`same_surface` 在任一侧未声明时返回 ``None``，
   **不返回 ``False``**——``False`` 是「两面不同」这个结论，会把「没记」读成
   「跨面分歧」，而报表上这两个数是同一个格子。
"""

from __future__ import annotations

from typing import Any, Final, Mapping

#: 「没记」的哨兵。字面量与 ``rubric_version.VERSION_UNRECORDED`` **必须相同**
#: （报表里 ``unrecorded`` 只有一种含义），由
#: ``tests/unit/test_golden_access_surface.py`` 钉住两处相等——那两张表分居
#: 采集侧与评估侧，谁也 import 不了谁，同步测试就是那条边。
SURFACE_UNRECORDED: Final = "unrecorded"

#: 人工复核员的浏览器。**不是驱动**——没有 ``driver=human`` 这回事，它是
#: 标注/复核侧写下的面，只能由人声明。
SURFACE_HUMAN: Final = "human-browser"

#: 本仓库 camofox-browser 固定配置（见模块 docstring 的命名口径表）。
#: **2026-10-26 起是生产面**——camoufox 执行层撤下（owner 裁定），
#: 改用 camofox-browser 的 MCP 适配器。
SURFACE_CAMOFOX: Final = "camofox-plain"

#: 本仓库 camoufox 固定配置——**只读存量面**（2026-10-26 起不再产出）。
#:
#: 留在这里的理由与工具面 ``camoufox-1`` 同一条：**存量存档正是唯一的真实
#: 证据**——盘上 2 份 P1 存档的 ``gatekeeper_events[].blocked_surface`` 记的
#: 就是 ``camoufox-plain``，删了这个代号，那些账本行就成了一串读不出来的
#: 字符串（而「被拦在哪张面上」正是方案 20 §R3 要归因的那一栏）。
SURFACE_CAMOUFOX: Final = "camoufox-plain"

#: 同上口径的 obscura 路径（回溯路径，``--driver obscura`` 显式点名）。
SURFACE_OBSCURA: Final = "obscura-plain"

#: 合法面代号（封闭集，**不含** ``unrecorded``——那是「没记」，不是一张面）。
#:
#: ⚠️ **含只读存量面** ``camoufox-plain``：合法 ≠ 还会被写到。
#: 「合法」是「读得懂」的口径——存量账本里就是那个值。
ACCESS_SURFACES: Final = frozenset(
    {SURFACE_HUMAN, SURFACE_CAMOFOX, SURFACE_CAMOUFOX, SURFACE_OBSCURA}
)

#: 驱动实现名 → 面。键必须与 ``driver_factory.SUPPORTED_KINDS`` 一致
#: （同步测试盯着）：多一个驱动忘登记，那条链的采集面就只能靠猜。
DRIVER_SURFACE: Final[Mapping[str, str]] = {
    "camofox": SURFACE_CAMOFOX,
    "obscura": SURFACE_OBSCURA,
}

#: **已退役**的驱动实现名 → 面。**只读**：只为读得懂存量存档。
#:
#: 2026-10-26：camoufox 执行层撤下。存量的 ``driver="camoufox"`` 存档
#: 仍要折算回它那一张面（``camoufox-plain``）——**退役不等于失忆**。
#: 它**不是**回落表：表里没有的名字照样**抛**。
LEGACY_DRIVER_SURFACE: Final[Mapping[str, str]] = {
    "camoufox": SURFACE_CAMOUFOX,
}


def is_declared(value: Any) -> bool:
    """这一侧**声明了**面（非空且不是 ``unrecorded``）。"""
    return bool(str(value or "").strip()) and (
        str(value).strip() != SURFACE_UNRECORDED
    )


def check_surface(value: Any) -> str:
    """校验一个面代号，返回规范值。空 ⇒ ``unrecorded``；认不出 ⇒ **抛**。"""
    name = str(value or "").strip()
    if not name:
        return SURFACE_UNRECORDED
    if name == SURFACE_UNRECORDED:
        return SURFACE_UNRECORDED
    if name not in ACCESS_SURFACES:
        raise ValueError(
            f"认不出的采集面 {name!r}——合法值："
            f"{sorted(ACCESS_SURFACES)}（没记填 {SURFACE_UNRECORDED!r} 或不填）"
        )
    return name


def surface_of_driver(driver: Any) -> str:
    """P1 存档的 ``driver`` → 采集面。

    空 ⇒ ``unrecorded``；**退役但有存量存档的实现名** ⇒ 它的旧面
    （:data:`LEGACY_DRIVER_SURFACE`）；其余认不出的 ⇒ **抛**。
    """
    name = str(driver or "").strip()
    if not name:
        return SURFACE_UNRECORDED
    try:
        return DRIVER_SURFACE[name]
    except KeyError:
        pass
    try:
        return LEGACY_DRIVER_SURFACE[name]
    except KeyError:
        raise ValueError(
            f"认不出的驱动实现名 {name!r}——采集面无从确定。"
            f"在跑的：{sorted(DRIVER_SURFACE)}；"
            f"已退役但有存量存档的：{sorted(LEGACY_DRIVER_SURFACE)}"
        ) from None


def same_surface(left: Any, right: Any) -> bool | None:
    """两条读数的面是否相同：``True`` / ``False`` / **``None`` = 不可判定**。

    任一侧未声明 ⇒ ``None``。这是本模块存在的理由的一半：把「没记」和
    「跨面」分开，否则「人进得去 agent 进不去」永远会被算成模型的问题。
    """
    a = check_surface(left)
    b = check_surface(right)
    if a == SURFACE_UNRECORDED or b == SURFACE_UNRECORDED:
        return None
    return a == b


__all__ = [
    "ACCESS_SURFACES",
    "DRIVER_SURFACE",
    "LEGACY_DRIVER_SURFACE",
    "SURFACE_CAMOFOX",
    "SURFACE_CAMOUFOX",
    "SURFACE_HUMAN",
    "SURFACE_OBSCURA",
    "SURFACE_UNRECORDED",
    "check_surface",
    "is_declared",
    "same_surface",
    "surface_of_driver",
]