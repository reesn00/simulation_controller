"""按 URL 逐条撤掉负样本池里的行——**判据在命令行，动作在这里**。

为什么需要它
------------
:func:`trajectory_pipeline.executor.cli.cmd_purge_run` 的 docstring 已经把
处置口径写死了：

    什么时候**不该**用：个别几条标签被证明错了（``check-archives`` 的
    ``negative_contradicted_by_success`` 会指出来）。那种**按 URL 逐条定**，
    不要整批回滚——同 run 里可能有完全正确的结论。

⇒ 「按 URL 逐条定」这条口径**没有工具**：

* :meth:`~trajectory_pipeline.executor.archive.P1Archive.purge_run` 按
  ``run_id`` **整批**删（连存档一起删）；
* :mod:`trajectory_pipeline.executor.downgrade` 改**存档**里的结论，
  **不动池**；
* :mod:`trajectory_pipeline.executor.relabel` 把一条真负样本换成**另一条**
  真负样本（**仍在池里**）；
* ``cli review --apply`` 只受理反思档（``unresolved`` / ``trailer_suspect``）。

三条路都删不掉**单独一行池记录**。

实测（2026-10-22）：camoufox 第二批之后 ``check-archives`` 报了 2 条
``negative_contradicted_by_success``（Severity.FATAL）。两条的裁定都是
「**这一行的结论不成立**」⇒ 存档侧用 ``cli downgrade`` 降为
``unresolved``，池侧那一行就成了**没有存档支撑的孤儿行**——而它照样会被
:func:`evaluation.golden.builder._scan_negative_pool` 读成负样本进训练集。

⇒ 本模块补的就是这一段：**把池里那些「存档侧已经不认」的逐条撤掉**。

三条硬纪律
------------
1. **默认 dry-run**——池是跨 run 累积的 append-only 资产，删错了不可逆
   （与 ``downgrade`` / ``relabel`` 同款口径）。
2. **理由必填**——撤掉的行连同理由进 ``pool_dropped.jsonl`` 审计日志。
   删掉的东西要留痕：半年后只剩「这行没了」，没人知道为什么。
3. **只删选中的行**——按**行的内容**配对删除（与
   :meth:`~trajectory_pipeline.executor.archive.P1Archive.apply_purge`
   同一口径：同一 ``url + branch`` 被两个 run 并过时池里只有一行，
   用计数而不是集合才不会「删一行少一行」），池里**坏行原样保留**
   （坏行可能是被人手工截断、正等人查的，不该被一次撤行动手清掉）。

⚠️ **它不碰存档**——存档侧的结论由 ``cli downgrade`` / ``cli relabel`` 改。
本模块只负责「池这一侧」。顺序永远是**先改存档、后撤池行**：
反过来的话，撤池时存档还写着旧结论 ⇒ 撤掉的那行在存档里还有支撑，
看着像撤错了。
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from typing import Any

from trajectory_pipeline.executor.archive import P1Archive

__all__ = [
    "DROPPED_LOG_NAME",
    "select_rows",
    "drop_from_root",
]

#: **撤行审计日志**的文件名（落在 P1 存档根目录，与池同根）。
#:
#: 池是 append-only 资产，撤行是**唯一**的删除动作 ⇒ 必须留痕：
#: 每一行记 ``原行 + 理由 + 时间 + 判据``，半年后能回答
#: 「这条为什么从池里没了」。
DROPPED_LOG_NAME = "pool_dropped.jsonl"


def select_rows(rows: list[dict[str, Any]], *,
                url_fragments: tuple[str, ...] = (),
                task_id: str | None = None,
                branch: str | None = None,
                ) -> list[dict[str, Any]]:
    """挑出要撤的池行。**同组条件是 AND**（URL 片段之间是 OR）。

    判据口径与 :func:`trajectory_pipeline.executor.relabel.select_by_url`
    一致（URL 片段 OR）；这里多两个限定项（``task_id`` / ``branch``），
    因为撤行要**精确到一行**，而池里同一 URL 可能挂着不同 task 的行。

    ⚠️ **不给任何判据就是「全选」**——调用方必须先校验
    （CLI 那层已经挡了）。
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        if task_id is not None and str(row.get("task_id") or "") != task_id:
            continue
        if branch is not None and str(row.get("branch") or "") != branch:
            continue
        if url_fragments:
            url = str(row.get("url") or "")
            if not any(f in url for f in url_fragments):
                continue
        out.append(row)
    return out


def drop_from_root(root: pathlib.Path | str, *,
                   url_fragments: tuple[str, ...] = (),
                   task_id: str | None = None,
                   branch: str | None = None,
                   reason: str,
                   dry_run: bool = True,
                   ) -> dict[str, Any]:
    """撤掉池里选中的行。返回 ``{"selected", "dropped", "log", "rows"}``。

    :param reason: **必填**。它会进审计日志——撤行动作不可逆，
        「凭什么撤」必须留在这条动作自己的记录里（与 ``downgrade`` /
        ``relabel`` 同一纪律）。
    :param dry_run: **默认 True**：只报清单，不碰盘。
    """
    if not (reason or "").strip():
        raise ValueError("撤池行必须给 reason：它是这条日后唯一的依据")
    if not url_fragments and not task_id and not branch:
        raise ValueError(
            "撤池行必须给判据（--url / --task-id / --branch）——"
            "不给就是全选，而池是 append-only 资产，全选等于清空训练集")

    base = pathlib.Path(root)
    store = P1Archive(base)
    rows = store._load_pool_rows()          # noqa: SLF001 —— 池的读法只有一份
    selected = select_rows(rows, url_fragments=url_fragments,
                            task_id=task_id, branch=branch)

    out: dict[str, Any] = {
        "selected": selected, "dropped": 0, "rows": [],
        "log": base / DROPPED_LOG_NAME,
    }
    if dry_run or not selected:
        return out

    # 复用 purge 的删除口径：按**行的内容**配对删，坏行原样保留。
    _arc, deleted = store.apply_purge((), tuple(selected))
    out["dropped"] = deleted
    out["rows"] = list(selected)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_path = base / DROPPED_LOG_NAME
    with log_path.open("a", encoding="utf-8") as fh:
        for row in selected:
            fh.write(json.dumps({
                "dropped_at": stamp,
                "reason": reason,
                "criteria": {"url_fragments": list(url_fragments),
                             "task_id": task_id, "branch": branch},
                "row": row,
            }, ensure_ascii=False) + "\n")
    return out
