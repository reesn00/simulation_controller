"""批次级断言的**生产落盘**（L-23）——把 :mod:`.asserts` 接到生产链上。

模块 :mod:`.asserts` 只给函数，此前**没有任何调用方**：读数算得再对，
报表上看不见、闸门读不到，等于没接线。本模块补两件事：

1. **读数落盘**：``<calibration-root>/v{N}/batch_asserts.json``（与
   ``snapshot.json`` **同目录、同版本号绑定**）。生产者是
   ``agreement --version N --emit-disagreements``——那是 ``snapshot.json``
   的唯一生产点，两者读的是同一次冻结，口径不会分叉；
2. **闸门读数**：``verify.analyze`` 的第五条判据 ``batch_asserts_clear``
   从这份文件读（判定线在 :mod:`...scorers.thresholds`，默认
   **「只出读数」**：第一批只报不判，等 owner 看一批读数再定线）。

## 数据源：**P1 存档**，不是黄金集行

B-1 / B-2 / B-4 量的是「这一次运行本身」——动作 schema、候选处置、
异常处置。这些事实只在 P1 存档里（``run_config`` / ``candidates`` /
``steps`` / ``visits`` / ``outcomes``）；黄金集行是**站点级**的，没有
run 级结构。所以按 ``(task_id, run_id)`` 回读 P1 存档。

⚠️ **读哪份存档**：优先 ``<task>__<run>.reviewed.json``，与
:func:`trajectory_pipeline.executor.archive.select_archives` 的「人工复核档
取代原档」**同一口径**。两份一起读会把同一批站点数两遍。

## 不猜

- 行里缺 ``task_id`` / ``run_id`` ⇒ :class:`BatchReportError`（没有这两个
  键就没法归到一条轨迹，更没法读它的存档）；
- 存档缺失 / 坏 JSON ⇒ 该 run 的 B-1 / B-2 / B-4 记 **测不成**（不是
  「不通过」，更不是「通过」）；B-5 只吃金标行，不依赖存档，照常出读数；
- B-5 **只读调参池**（``calibration`` 档）：持有集（D-C5）与回归集
  （D-C8）是封存面——读数里会写明读了哪一档。

## 结构

```json
{
  "schema_version": 1,
  "golden_version": 16,
  "generated_at": "2026-11-09T…Z",
  "batch": {"unit": "run", "run_count": 39},
  "summary": {
    "run_count": 39,
    "by_clause": {"B-1": {"通过": …, "不通过": …, "只出读数": …, "测不成": …}, …},
    "failed_runs": ["T001__abc123"],
    "missing_archive_runs": []
  },
  "runs": [{"run_id", "task_id", "readings", "failed", "untestable", "readout",
            "archive_found", "archive_note", "row_splits"}, …]
}
```

``summary.failed_runs`` / ``summary.missing_archive_runs`` 是**闸门的读口**
（``verify._criterion_batch``）——改结构时先改那边。
"""

from __future__ import annotations

import collections
import datetime as _dt
import json
import pathlib
from typing import Any, Callable, Mapping, Sequence

from trajectory_pipeline.assembler.schema import P1ShapeError, read_archive
from trajectory_pipeline.evaluation.batch import asserts
from trajectory_pipeline.evaluation.golden.splits import SPLIT_CALIBRATION

#: 产物文件名——与 ``snapshot.json`` 同目录、同版本号绑定。
BATCH_ASSERTS_FILE = "batch_asserts.json"

#: 文件结构版本。**改动 summary / runs 的读口时进位**，读侧 fail-closed。
SCHEMA_VERSION = 1

#: ``(task_id, run_id) -> (存档 dict | None, 说明)``。
#:
#: 说明为空串 = 读到了；非空 = 读不到的原因（进读数的 caveat）。
ArchiveReader = Callable[[str, str], "tuple[Mapping[str, Any] | None, str]"]


class BatchReportError(ValueError):
    """批次级断言读数出不来（输入不成立，**不是**「判不过」）。"""


# ── 读存档 ────────────────────────────────────────────────────────


def default_archive_reader(p2_root: pathlib.Path | str) -> ArchiveReader:
    """按 ``(task, run)`` 读 P1 存档；**优先人工复核档**。

    复核档（``.reviewed.json``）是人工确认过的版本，与
    ``select_archives`` 的选择口径一致；只读原档会把复核改过的
    分支 / 理由读成旧的。
    """
    root = pathlib.Path(p2_root)

    def read(task_id: str, run_id: str) -> tuple[Mapping[str, Any] | None, str]:
        base = f"{task_id}__{run_id}"
        for name in (f"{base}.reviewed.json", f"{base}.json"):
            path = root / name
            if not path.exists():
                continue
            try:
                return read_archive(path), ""
            except (P1ShapeError, OSError) as exc:
                return None, f"{name} 读不了：{exc}"
        return None, (
            f"{root} 下没有 {base}.json（也没有 {base}.reviewed.json）"
        )

    return read


# ── 装配 ──────────────────────────────────────────────────────────


def _run_keys(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """按出现顺序收 ``(task_id, run_id)``；缺键 **fail-closed**。"""
    keys: dict[tuple[str, str], None] = {}
    for i, row in enumerate(rows or ()):
        task_id = str((row or {}).get("task_id") or "").strip()
        run_id = str((row or {}).get("run_id") or "").strip()
        if not task_id or not run_id:
            raise BatchReportError(
                f"第 {i} 行缺 task_id / run_id（{task_id!r}, {run_id!r}）："
                "没有这两个键就没法归到一条轨迹，也读不到它的 P1 存档——"
                "**不猜、不丢**"
            )
        keys[(task_id, run_id)] = None
    return sorted(keys)


def _b5_rows(run_rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """B-5 的分母来源：**调参池的站点级行**。

    两条过滤与 ``cmd_agreement`` 的头条口径一致：

    - ``__split__`` 只认 ``calibration``（持有集 / 回归集是封存面）；
    - ``p2_sample`` 是 per-unit 轨迹行，没有站点结论，不进 D-10 口径。

    未冻结的行没有 ``__split__``（退化为「还没切档」），按调参池算。
    """
    out: list[Mapping[str, Any]] = []
    for row in run_rows or ():
        if str(row.get("__split__") or SPLIT_CALIBRATION) != SPLIT_CALIBRATION:
            continue
        if str(row.get("source_kind") or "") == "p2_sample":
            continue
        out.append(row)
    return out


def _summarize(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """批次汇总——**闸门只读 ``failed_runs`` / ``missing_archive_runs``**。"""
    statuses = (
        asserts.STATUS_PASS,
        asserts.STATUS_FAIL,
        asserts.STATUS_READOUT,
        asserts.STATUS_UNTESTABLE,
    )
    by_clause: dict[str, dict[str, int]] = {
        clause: {s: 0 for s in statuses} for clause in asserts.CLAUSES
    }
    failed_runs: list[str] = []
    missing_runs: list[str] = []
    for run in runs:
        run_id = f"{run.get('task_id')}__{run.get('run_id')}"
        for reading in run.get("readings") or ():
            clause = str(reading.get("clause") or "")
            status = str(reading.get("status") or "")
            if clause in by_clause and status in by_clause[clause]:
                by_clause[clause][status] += 1
        if run.get("failed"):
            failed_runs.append(run_id)
        if not run.get("archive_found"):
            missing_runs.append(run_id)
    return {
        "run_count": len(runs),
        "by_clause": by_clause,
        "failed_runs": failed_runs,
        "missing_archive_runs": missing_runs,
    }


def build_batch_asserts(
    rows: Sequence[Mapping[str, Any]],
    *,
    version: int,
    p2_root: pathlib.Path | str,
    archive_reader: ArchiveReader | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """一个冻结版本的全部 run → 批次级断言读数（纯计算，不写盘）。"""
    keys = _run_keys(rows)
    reader = archive_reader or default_archive_reader(p2_root)
    by_run: dict[tuple[str, str], list[Mapping[str, Any]]] = collections.defaultdict(list)
    for row in rows or ():
        key = (
            str(row.get("task_id") or "").strip(),
            str(row.get("run_id") or "").strip(),
        )
        by_run[key].append(row)
    run_entries: list[dict[str, Any]] = []
    for task_id, run_id in keys:
        run_rows = by_run[(task_id, run_id)]
        gold_rows = _b5_rows(run_rows)
        archive, note = reader(task_id, run_id)
        archive_name = f"{task_id}__{run_id}"
        if archive is None:
            result = asserts.assert_missing_archive(
                run_id=run_id,
                task_id=task_id,
                archive_name=archive_name,
                gold_rows=gold_rows,
                split=SPLIT_CALIBRATION,
                reason=note,
            )
        else:
            result = asserts.assert_run(
                archive,
                run_id=run_id,
                task_id=task_id,
                archive_name=archive_name,
                gold_rows=gold_rows,
                split=SPLIT_CALIBRATION,
            )
        entry = result.to_json()
        entry["archive_found"] = archive is not None
        entry["archive_note"] = note
        entry["row_splits"] = dict(sorted(
            collections.Counter(
                str(row.get("__split__") or "unfrozen") for row in run_rows
            ).items()
        ))
        run_entries.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "golden_version": int(version),
        "generated_at": generated_at or _dt.datetime.now(
            _dt.timezone.utc
        ).replace(microsecond=0).isoformat(),
        "batch": {"unit": "run", "run_count": len(run_entries)},
        "summary": _summarize(run_entries),
        "runs": run_entries,
    }


# ── 落盘 / 读盘 ───────────────────────────────────────────────────


def write_batch_asserts(
    path: pathlib.Path | str, report: Mapping[str, Any]
) -> pathlib.Path:
    """写 ``batch_asserts.json``（父目录自动建）。"""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    return p


def load_batch_asserts(
    path: pathlib.Path | str,
) -> dict[str, Any] | None:
    """读 ``batch_asserts.json``；**不存在返回 ``None``**（由调用方决定怎么报）。

    存在但读不懂 ⇒ :class:`BatchReportError`（fail-closed）：
    **「文件在但读不懂」不许当成「没有」**——那会把结构漂移读成「本版
    没产出」，闸门与报表都会安静地少一条判据。
    """
    p = pathlib.Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise BatchReportError(f"{p} 读不了：{exc}") from exc
    if not isinstance(data, dict):
        raise BatchReportError(f"{p} 顶层不是 JSON 对象")
    got = data.get("schema_version")
    if got != SCHEMA_VERSION:
        raise BatchReportError(
            f"{p} 的 schema_version={got!r}，本代码只认 {SCHEMA_VERSION}——"
            "先确认这份产物是哪版写的，不要按猜的结构读"
        )
    if not isinstance(data.get("runs"), list):
        raise BatchReportError(f"{p} 缺 runs 列表")
    return data


def summary_line(report: Mapping[str, Any]) -> str:
    """人读的一句话（CLI 打印用）——**口径 + 数**都在里面。"""
    summary = report.get("summary") or {}
    by_clause = summary.get("by_clause") or {}
    b1 = by_clause.get("B-1") or {}
    return (
        f"{summary.get('run_count', 0)} 个 run"
        f"（B-1 通过 {b1.get(asserts.STATUS_PASS, 0)} / "
        f"不通过 {b1.get(asserts.STATUS_FAIL, 0)} / "
        f"测不成 {b1.get(asserts.STATUS_UNTESTABLE, 0)}；"
        f"整批不通过 {len(summary.get('failed_runs') or ())} 个 run；"
        f"缺存档 {len(summary.get('missing_archive_runs') or ())} 个）"
    )


__all__ = [
    "BATCH_ASSERTS_FILE",
    "SCHEMA_VERSION",
    "ArchiveReader",
    "BatchReportError",
    "build_batch_asserts",
    "default_archive_reader",
    "load_batch_asserts",
    "summary_line",
    "write_batch_asserts",
]