"""评估侧的采集面入口——**读标注行**的面，比两面是否相同。

词表与判定住在 :mod:`trajectory_pipeline.executor.browser.access_surface`
（面是**驱动实现的属性**，采集侧也要用它给事件账本盖章；放在这里会让
``executor`` 反向 import ``evaluation``，把分层倒过来）。本模块只做两件事：

1. :func:`surface_of_annotation` —— **标注行 → 面**。历史行没有这一栏，
   读入口**不抛**（抛会把整条冻结链堵死），但写错的代号照样抛；
2. 把词表与判据**原样再导出**，评估侧（冻结 builder / 快照 / 分歧卡）
   继续 ``from ...evaluation import access_surface as A`` 那样用——**不另写
   第二份**。两处各留一份的后果是「面相同吗」有两个答案，而报表上看不出
   来；方案 20 §R3 那句「R3 的比较逻辑不许自己再写一遍」说的就是这条。

为什么「没记」与「跨面」必须分开
--------------------------------
方案 20 §2 实测：同一台机器、同一天、同一批站点，**人工浏览器 12 个进得去、
camoufox 8 个被拦**。「人判这个站没片」与「agent 判这个站没片」可能根本不
是在说同一件事。所以每条读数都记面；两侧有一侧没记 ⇒ :func:`same_surface`
给 ``None``（不可判定），**不给 ``False``**（那是「两面不同」这个结论）。
"""

from __future__ import annotations

from typing import Any, Mapping

from trajectory_pipeline.executor.browser.access_surface import (
    ACCESS_SURFACES,
    DRIVER_SURFACE,
    SURFACE_CAMOUFOX,
    SURFACE_HUMAN,
    SURFACE_OBSCURA,
    SURFACE_UNRECORDED,
    check_surface,
    is_declared,
    same_surface,
    surface_of_driver,
)


def surface_of_annotation(row: Mapping[str, Any] | None) -> str:
    """标注行 → 采集面。缺键 / 空 / ``unrecorded`` 一律 :data:`SURFACE_UNRECORDED`。

    读入口**不抛**（历史行本来就没有这一栏，抛会把整条冻结链堵死），
    但认不出的代号照样抛——那是填错了，不是没填。
    """
    if not isinstance(row, Mapping):
        return SURFACE_UNRECORDED
    return check_surface(row.get("access_surface"))


__all__ = [
    "ACCESS_SURFACES",
    "DRIVER_SURFACE",
    "SURFACE_CAMOUFOX",
    "SURFACE_HUMAN",
    "SURFACE_OBSCURA",
    "SURFACE_UNRECORDED",
    "check_surface",
    "is_declared",
    "same_surface",
    "surface_of_annotation",
    "surface_of_driver",
]