"""负样本池的成员资格：**一条判定，三处同源**。

⚠️ **池成员资格有两段**，缺一段就会出现两种「数字悄悄不对」：

1. **分支口径**（旧有）：``branch ∉ NON_SAMPLE_BRANCHES ∪ COLLECTION_SIDE_BRANCHES``
   ——「没判出来」（无题兜底）与「这次没看到」（采集侧账）都不进池。
   真源：:attr:`trajectory_pipeline.executor.branches.SiteOutcome.is_negative_sample`。
2. **任务口径**（2026-11-03 补，方案 23 R4-2 / Q-33 裁「甲」）：任务声明了
   ``excluded_platforms`` 时，**落在那些平台上的访问点整条剔掉** ⇒ 它的结论
   不是「这个任务里这个站看不了」，而是「这个站本来就不在任务范围里」。
   真源：:func:`trajectory_pipeline.assembler.excluded.excluded_outcome_indices`
   （**按 URL 配对**给下标，与 R4-2 同一份口径）。

**为什么必须写在同一个函数里**：写入路径（:mod:`trajectory_pipeline.executor.archive`）、
门禁（:mod:`trajectory_pipeline.executor.integrity`）、回填
（:mod:`trajectory_pipeline.executor.pool_add`）**各自算一遍**的话，
任何一处漏掉第二段就会：
- 门禁把「本来就该没并池」的行报成 ``negative_not_pooled``（**报表全红，而池子是对的**）；
- 回填把剔掉的行**又并回池里**（池里混进不该训的站）。

这两种都**不报错**——只是数字悄悄不对 ⇒ 所以规则只能有一份。

⚠️ **池的身份是「键」不是「行」**：池的去重键是 ``(task_id, url, branch)``
⇒ 判定落在**键**上，不是 outcome 下标上。原因：同一条存档里两个 outcome
可能带**同一个键**（实测 33 组同 URL），而池里只存一行 ⇒ 按下标判会出现
「A 说该并、B 说不该并」的矛盾。⇒ 口径：**键里出现过被剔的 outcome ⇒ 整个键不进池**
（保守；fail-closed——不拿一条说不清归属的行去训模型）。

⚠️ **剔除只影响「这个任务」**：同一个站在另一个任务里没被排除 ⇒ 那边的池行照旧。
所以判据必须带 ``task_id``，不能只看域名。
"""

from __future__ import annotations

from typing import Any, Mapping

from trajectory_pipeline.executor.branches import (
    COLLECTION_SIDE_BRANCHES,
    NON_SAMPLE_BRANCHES,
)

__all__ = [
    "excluded_outcome_indices",
    "excluded_pool_keys",
    "is_pool_member_branch",
    "pool_member_indices",
    "pool_member_keys",
]


def excluded_outcome_indices(archive: Mapping[str, Any]) -> set[int]:
    """这份存档里被**任务级剔除**的 ``outcomes[]`` 下标。

    真源在 :mod:`trajectory_pipeline.assembler.excluded`（R4-2 的那一份），
    这里只是转发——**判定不许有第二份**。

    ``assembler`` 在依赖图的**下游**（``executor → assembler``）⇒ 这里 import
    它不构成回指。⚠️ 本模块**不得**被 ``assembler`` 侧 import：那样会成环
    （``assembler`` 的纪律是「不向执行端要对象」）。
    """
    from trajectory_pipeline.assembler.excluded import (
        excluded_outcome_indices as _impl,
    )

    return _impl(archive)


def is_pool_member_branch(branch: str | None) -> bool:
    """``branch`` 是不是「真负样本分支」（**第一段**口径）。

    与 :attr:`trajectory_pipeline.executor.branches.SiteOutcome.is_negative_sample`
    **逐字同源**：两段排除的集合就是那两个常量。
    """
    if branch is None:
        return False
    return (
        branch not in NON_SAMPLE_BRANCHES
        and branch not in COLLECTION_SIDE_BRANCHES
    )


def excluded_pool_keys(archive: Mapping[str, Any]) -> set[tuple[str, str]]:
    """被剔掉的 outcome 的 ``(url, branch)`` 键（**池的身份**）。

    键里不带 ``task_id``：调用方已经按存档的任务在算。
    """
    outcomes = list(archive.get("outcomes") or ())
    out: set[tuple[str, str]] = set()
    for j in excluded_outcome_indices(archive):
        if j >= len(outcomes):
            continue
        outcome = outcomes[j]
        if not isinstance(outcome, Mapping):
            continue
        out.add((str(outcome.get("url") or ""), str(outcome.get("branch") or "")))
    return out


def pool_member_keys(archive: Mapping[str, Any]) -> set[tuple[str, str]]:
    """这个存档里哪些 ``(url, branch)`` 键**算池成员**。

    = 第一段（分支口径）∧ 不在被剔键集里。

    Args:
        archive: **存档映射**（P1 的 JSON 形状）。写入路径上传的是
            ``RunRecord.to_json()`` 的结果——**同一形状**（``outcomes[]`` 与
            ``ledger.outcomes`` **同序**，见 ``orchestrator.RunRecord.to_json``），
            所以判定在写盘前就能算，不必回读文件。
    """
    dropped = excluded_pool_keys(archive)
    out: set[tuple[str, str]] = set()
    for outcome in archive.get("outcomes") or ():
        if not isinstance(outcome, Mapping):
            continue
        branch = outcome.get("branch")
        if not is_pool_member_branch(branch):
            continue
        key = (str(outcome.get("url") or ""), str(branch or ""))
        if key in dropped:
            continue
        out.add(key)
    return out


def pool_member_indices(archive: Mapping[str, Any]) -> set[int]:
    """这个存档里哪些 ``outcomes[]`` 下标是**池成员**（供报表计数用）。"""
    members = pool_member_keys(archive)
    out: set[int] = set()
    for j, outcome in enumerate(archive.get("outcomes") or ()):
        if not isinstance(outcome, Mapping):
            continue
        branch = outcome.get("branch")
        if not is_pool_member_branch(branch):
            continue
        key = (str(outcome.get("url") or ""), str(branch or ""))
        if key not in members:
            continue
        out.add(j)
    return out
