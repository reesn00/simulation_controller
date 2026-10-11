"""P3 视图读取——三份视图 JSON 的**唯一读实现**（训练侧入口）。

与 :mod:`trajectory_pipeline.assembler.views` 的分工：
    views.py     渲染语义（P2 JSON → 三份 dict）
    read_p3.py   读取语义（磁盘三份 JSON → 三份 dict / 原始 P2）

读回的是**渲染后的训练视图**：``messages`` / ``openai`` / ``meta`` 三份
各自解析为 dict。元数据侧没有取舍的余地——视图文件里已经带了
``source_archive``，从 id 就能反查 P1，读侧不再发明第二套寻址。

一句话纪律：**不 import assembler/executor，不读仓库根 output/**。
P3 是格式交接面的最末端，读它只该依赖「文件名 + json」，不该依赖生产端
当前版本——半年前重建的一批 P3，今天必须还能原样读回来。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trajectory_pipeline.storage import VIEW_NAMES, views_dir

__all__ = ["read_views", "read_view"]


def read_views(
    sample_id: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any] | None:
    """读回一份样本的三份视图，返回 ``{messages, openai, meta}``。

    Args:
        sample_id: 视图的文件主键（与 P2 的 ``sample_id`` 一致）。
        root: 读取根；``None`` 取 :func:`views_dir` 的默认根。

    Returns:
        三份视图 dict；**任一视图文件缺失或不是合法 JSON 都返回 None**——
        fail-closed：能力不可用（缺文件 / 坏档）时返回 ``None``，不返回
        半份数据、不猜。调用方拿 ``None`` 当「这条读不到」处理，而不是
        当成一份假视图。
    """
    out = {}
    for name in VIEW_NAMES:
        payload = read_view(sample_id, name, root=root)
        if payload is None:
            return None
        out[name] = payload
    return out


def read_view(
    sample_id: str,
    view: str,
    *,
    root: str | Path | None = None,
) -> Any | None:
    """读回单份视图。

    Args:
        sample_id: 视图的文件主键。
        view: 视图名，须在 ``VIEW_NAMES`` 内（``messages`` / ``openai`` /
            ``meta``）；不在表内视为调用方写错，直接返回 None（不猜）。
        root: 同 :func:`read_views`。

    Returns:
        该视图的解析结果；文件缺失、JSON 坏或视图名不合法返回 None。
    """
    if view not in VIEW_NAMES:
        return None
    path = views_dir(root) / f"{sample_id}_{view}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
