"""golden_ls.diff: **相对上一版**的变更标记（方案 22 §2.4）。

为什么需要它
------------
owner 裁定「增量版本每个都上传」之后，每推一版就多 294 条 task。人在标注页
上要能一眼看出**哪几行是这一版新变的**——否则每版都要从头看一遍，人力成本
随版本数线性涨。

四种标记
--------
=============  ==========================================================
``new``        上一版没有这一行（新增标注）
``changed``    金标 / 代码判定 / 评分面相对上一版变了
``id_migrated``键变了（``golden_id_legacy`` 非空）但**内容没变**
``unchanged``  内容与上一版一致
=============  ==========================================================

优先级: ``new`` > ``changed`` > ``id_migrated`` > ``unchanged``。
``changed`` 压过 ``id_migrated``：内容也变了的时候，人要重看的是**内容**。

⚠️ **id 迁移要靠旧 id 配对**。v14 迁移了 6 行（新 ``golden_id`` + 旧的
``golden_id_legacy``）。只按新 id 查上一版 ⇒ 这 6 行会被误判成 ``new``。
所以配对时**新旧 id 都查**（:func:`mark_changes`）。

「变了」的判据
--------------
:func:`content_key` 决定什么算变了。判据宽一点（评分面重挂也算变）是有意的：
**评分面换了 = 逐维分数全变 = 人要重新看一眼**，那正是 v6 / v8 两版的实况。

⚠️ **观测面与观测时刻也算（2026-10-29 补）**：方案 20 §R2 把
``golden.access_surface`` / ``golden.observed_at`` 加进了**金标块** ⇒
「金标相对上一版变了」（方案 22 §2.4 的原话）**就包括这两栏** ⇒ 不算进去
⇒ 页面上一行显示着「human-browser · 2026-10-12」、标记却写着 ``unchanged``
⇒ **人在标注页上看到的与标记对不上**（这正是「标记的作用是让人一眼看出哪几行
新变了」的反面）。

⚠️ **只加这两栏，不加 ``reviewed_by`` / ``reviewed_at`` / ``note``**：那三栏
**每次重新标注都会变**（``reviewed_at`` 是写下的那一刻）⇒ 放进来 ⇒ 标记动辄
半版都是 ``changed`` ⇒ 「重点看」这个词就废了。**判据要宽，但宽在「人看到
的东西变了」上，不宽在「时间戳动了」上**。

⚠️ **不影响既有版本对**：v13 / v14 的 294 行**一个都没带**这两栏
⇒ v14 相对 v13 的标记**逐条不变** ⇒ 已经推上 LS 的 v14 项目**不受影响**。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

#: 上一版没有这一行。
MARK_NEW = "new"
#: 内容变了（金标 / 代码判定 / 评分面）。
MARK_CHANGED = "changed"
#: 键变了、内容没变。
MARK_ID_MIGRATED = "id_migrated"
#: 内容与上一版一致。
MARK_UNCHANGED = "unchanged"

#: **需要人看**的三个标记（``--only-changed`` 按它过滤）。
CHANGE_MARKERS: tuple[str, ...] = (MARK_NEW, MARK_CHANGED, MARK_ID_MIGRATED)

#: 全部四种标记（报表按这个顺序输出）。
ALL_MARKERS: tuple[str, ...] = (
    MARK_NEW, MARK_CHANGED, MARK_ID_MIGRATED, MARK_UNCHANGED,
)


def content_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """一行里**算「内容」**的那些字段。

    ⚠️ 刻意**不含** ``golden_id`` / ``golden_id_legacy`` / ``__split__``：
    键变了是 ``id_migrated`` 的事，内容变没变是这里的事——两件事分开判，
    才不会出现「id 迁移 + 内容也变」时说不清是哪一种。

    ⚠️ **含观测面与观测时刻**（方案 20 §R2 把它们加进了金标块 ⇒ 方案 22
    §2.4 的「金标变了」就包括它们）⇒ 不含 ⇒ 页面显示与标记对不上。
    ⚠️ **不含 ``reviewed_by`` / ``reviewed_at`` / ``note``**：那三栏每次重新
    标注都会动 ⇒ 含进去标记就废了（见模块 docstring）。
    """
    gold = row.get("golden") if isinstance(row.get("golden"), Mapping) else {}
    code = row.get("code") if isinstance(row.get("code"), Mapping) else {}
    return (
        gold.get("label"),
        _stable(gold.get("access_surface")),
        _stable(gold.get("observed_at")),
        _stable(gold.get("dimension_scores")),
        _stable(row.get("dimension_scores")),
        _stable(row.get("scores_detail")),
        code.get("branch"),
        code.get("branch_label"),
        code.get("evidence"),
    )


def _stable(value: Any) -> str:
    """dict/list 的稳定序列化（键排序）——比较时不能靠 dict 的插入序。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _index_by_ids(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """按 ``golden_id`` **与** ``golden_id_legacy`` 建索引。

    两个键都收：id 迁移的行，旧 id 只存在于 ``golden_id_legacy`` 里，
    不收它就配不上上一版 ⇒ 误判成 ``new``（见模块 docstring）。
    """
    out: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        gid = str(row.get("golden_id") or "")
        if gid:
            out[gid] = row
        legacy = str(row.get("golden_id_legacy") or "")
        if legacy and legacy not in out:
            out[legacy] = row
    return out


def mark_changes(
    current: Sequence[Mapping[str, Any]],
    previous: Sequence[Mapping[str, Any]] | None,
) -> dict[str, str]:
    """``{golden_id: 标记}``。

    Args:
        current: 本版全部行。
        previous: 上一版全部行。**``None`` 或空 ⇒ 全判 ``new``**
            （本版没有可比对象——不是「都没变」）。

    没有可比对象时判 ``new`` 而不是 ``unchanged``：前者说的是「要人看」，
    后者说的是「不用看」，两者的后果相反，不能混。
    """
    prev_index = _index_by_ids(previous or ())
    out: dict[str, str] = {}
    for row in current:
        gid = str(row.get("golden_id") or "")
        if not gid:
            continue
        legacy = str(row.get("golden_id_legacy") or "")
        prev = prev_index.get(gid)
        matched_by_legacy = False
        if prev is None and legacy:
            prev = prev_index.get(legacy)
            matched_by_legacy = prev is not None
        if prev is None:
            out[gid] = MARK_NEW
            continue
        if content_key(row) != content_key(prev):
            out[gid] = MARK_CHANGED
            continue
        if legacy and (matched_by_legacy or legacy != gid):
            out[gid] = MARK_ID_MIGRATED
            continue
        out[gid] = MARK_UNCHANGED
    return out


def filter_changed(
    rows: Iterable[Mapping[str, Any]],
    markers: Mapping[str, str],
    *,
    only_changed: bool = False,
) -> list[dict[str, Any]]:
    """按标记过滤行。

    ``only_changed=False`` ⇒ 全留（**默认**：owner 裁定「一致行也传」）。
    ``only_changed=True`` ⇒ 只留 ``new`` / ``changed`` / ``id_migrated``。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        gid = str(row.get("golden_id") or "")
        if only_changed and markers.get(gid) not in CHANGE_MARKERS:
            continue
        out.append(dict(row))
    return out


def summarize_markers(rows: Iterable[Mapping[str, Any]],
                      markers: Mapping[str, str]) -> dict[str, int]:
    """标记分布（按 :data:`ALL_MARKERS` 的固定顺序输出，缺的补 0）。"""
    counts = {m: 0 for m in ALL_MARKERS}
    unknown = 0
    for row in rows:
        gid = str(row.get("golden_id") or "")
        marker = markers.get(gid)
        if marker in counts:
            counts[marker] += 1
        else:
            unknown += 1
    if unknown:
        counts["__unknown"] = unknown
    return counts


__all__ = [
    "ALL_MARKERS",
    "CHANGE_MARKERS",
    "MARK_CHANGED",
    "MARK_ID_MIGRATED",
    "MARK_NEW",
    "MARK_UNCHANGED",
    "content_key",
    "filter_changed",
    "mark_changes",
    "summarize_markers",
]
