"""scout trajectory 产物体检(``scout.cli verify`` 背后的逻辑)。

设计: ``docs/设计方案/14-完整任务视图与LS上传.md`` §8 风险 2 +
§9 Phase 6 推迟项 —— trajectory 主路径跑通后,旧 per-unit 路径
的过渡期里,``trajectory_candidates.jsonl`` / ``trajectory_stats.json``
/ T 视图三者**各是各的盘**,没人盯一致性,数据漂移看不见。

盯的不变量:

* **jsonl 存在 + 至少 1 条非空行** —— 没产物直接拒
* **stats 存在 + 可 JSON 解析** —— 没 stats 直接拒
* **stats 三键到齐** ``scored``/``undetermined``/``not_run``
  (``aggregate_from_buckets`` 写过 ``setdefault(k, 0)``,但落盘前的
  失败也可能漏键 —— verify 再盯一次)
* **stats 计数 vs jsonl 行数** —— ``sum(stats.values()) == len(records)``
* **每个 trajectory_id 都有 T 视图** —— 缺则 warn
  (不视为 error:可能任务刚聚合还没渲 T,但``cli task-view`` 跑过就不该缺)
* **T 视图是合法 JSON + 含三键** ``messages``/``openai``/``meta`` —— 同
  ``storage.task_views.write_task_view`` 的 ``PAYLOAD_KEYS``,坏档 warn

纪律
----

* **不 import executor / assembler** —— verify 只读 scout 三件套 +
  T 视图盘,落盘路径走 ``storage.task_views_dir`` / 直接 ``Path``,
  不依赖写侧模块
* **fail-closed**:能力不可用(读不出 / 坏 JSON)进 ``issues``,**不抛**;
  让 CLI 整批报告有可读结果
* **read-only**:只读不写,本模块所有函数均不落盘
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from trajectory_pipeline.evaluation.scout.aggregate import (
    TRAJECTORY_BUCKET_FILE,
    TRAJECTORY_STATS_FILE,
)
from trajectory_pipeline.evaluation.scout.types import TrajectoryRecord
from trajectory_pipeline.storage.task_views import PAYLOAD_KEYS

log = logging.getLogger(__name__)

#: 期望 stats 必含的三键。``scored`` / ``undetermined`` / ``not_run``
#: 与 ``TrajectoryRecord.status`` 字面量对齐。
EXPECTED_STATS_KEYS: tuple[str, ...] = ("scored", "undetermined", "not_run")

IssueSeverity = Literal["error", "warning"]
IssueCode = Literal[
    "missing_jsonl",
    "empty_jsonl",
    "missing_stats",
    "bad_stats_json",
    "stats_missing_key",
    "stats_count_mismatch",
    "missing_t_view",
    "bad_t_view_json",
    "t_view_missing_keys",
    "bad_jsonl_line",
]


@dataclass(frozen=True)
class VerifyIssue:
    """单条失败记录。``severity=error`` 算 fail(返 1),warning 不算。"""

    code: IssueCode
    severity: IssueSeverity
    detail: str
    trajectory_id: str = ""

    def __str__(self) -> str:  # pragma: no cover - 调试用
        prefix = "✗" if self.severity == "error" else "⚠"
        tid = f" {self.trajectory_id}" if self.trajectory_id else ""
        return f"{prefix} {self.code}{tid}: {self.detail}"


@dataclass
class VerifyReport:
    """一次 ``verify_from_dir`` 的结果。"""

    issues: list[VerifyIssue] = field(default_factory=list)
    record_count: int = 0
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """无 error 级 issue → pass。warning 不影响 ok。"""
        return not any(i.severity == "error" for i in self.issues)

    @property
    def by_code(self) -> Counter:
        return Counter(i.code for i in self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    def summary(self) -> str:
        return (
            f"trajectory 记录 {self.record_count} 条; "
            f"issues: {self.error_count} error / {self.warning_count} warning"
        )


# ── 纯函数:在已有数据上跑断言 ─────────────────────────────


def _read_jsonl_records(path: Path) -> tuple[list[TrajectoryRecord], list[VerifyIssue]]:
    """读 trajectory_candidates.jsonl,坏行进 issues 不抛。"""
    issues: list[VerifyIssue] = []
    if not path.is_file():
        issues.append(VerifyIssue(
            code="missing_jsonl", severity="error",
            detail=f"{path} 不存在",
        ))
        return [], issues
    out: list[TrajectoryRecord] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            issues.append(VerifyIssue(
                code="bad_jsonl_line", severity="warning",
                detail=f"{path}:{lineno} 解析失败",
            ))
            continue
        if not isinstance(raw, dict) or not raw.get("trajectory_id"):
            issues.append(VerifyIssue(
                code="bad_jsonl_line", severity="warning",
                detail=f"{path}:{lineno} 缺 trajectory_id",
            ))
            continue
        out.append(raw)  # type: ignore[arg-type]
    if not issues and not out:
        issues.append(VerifyIssue(
            code="empty_jsonl", severity="error",
            detail=f"{path} 无有效记录(全是空行/坏行)",
        ))
    return out, issues


def _read_stats(path: Path) -> tuple[dict[str, int], list[VerifyIssue]]:
    """读 trajectory_stats.json,坏档进 issues。"""
    issues: list[VerifyIssue] = []
    if not path.is_file():
        issues.append(VerifyIssue(
            code="missing_stats", severity="error",
            detail=f"{path} 不存在",
        ))
        return {}, issues
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        issues.append(VerifyIssue(
            code="bad_stats_json", severity="error",
            detail=f"{path} 解析失败: {type(exc).__name__}: {exc}",
        ))
        return {}, issues
    if not isinstance(raw, dict):
        issues.append(VerifyIssue(
            code="bad_stats_json", severity="error",
            detail=f"{path} 顶层不是 dict",
        ))
        return {}, issues
    out = {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}
    for key in EXPECTED_STATS_KEYS:
        if key not in out:
            issues.append(VerifyIssue(
                code="stats_missing_key", severity="warning",
                detail=f"stats 缺键 {key!r}",
            ))
    return out, issues


def _check_stats_count(
    record_count: int, stats: dict[str, int]
) -> list[VerifyIssue]:
    """stats 计数 vs jsonl 行数。"""
    issues: list[VerifyIssue] = []
    if not stats:
        return issues
    total = sum(stats.get(k, 0) for k in EXPECTED_STATS_KEYS)
    if total != record_count:
        issues.append(VerifyIssue(
            code="stats_count_mismatch", severity="error",
            detail=(
                f"stats 三态合计 {total} != jsonl 记录 {record_count}; "
                f"stats={stats}"
            ),
        ))
    return issues


def _check_task_views(
    records: Iterable[TrajectoryRecord],
    task_views_root: str | Path | None,
) -> list[VerifyIssue]:
    """每个 trajectory_id 都要有 T 视图;缺 / 坏档 / 缺三键 进 issues。"""
    issues: list[VerifyIssue] = []
    # 延迟 import 破循环(scout.aggregate 也顶层 import ls_bridge.exporter,
    # 这次反过来也会触发——但 storage.task_views 不依赖 scout,所以这里
    # 延迟 import 仅是保险,实际上 storage.task_views 顶层 import 安全)
    from trajectory_pipeline.storage import task_views_dir

    root = task_views_dir(task_views_root)
    for rec in records:
        tid = str(rec.get("trajectory_id", ""))
        if not tid:
            continue
        tview_path = root / f"{tid}.json"
        if not tview_path.is_file():
            issues.append(VerifyIssue(
                code="missing_t_view", severity="warning",
                trajectory_id=tid,
                detail=f"{tview_path} 不存在",
            ))
            continue
        try:
            payload = json.loads(tview_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            issues.append(VerifyIssue(
                code="bad_t_view_json", severity="warning",
                trajectory_id=tid,
                detail=f"{tview_path} 解析失败: {type(exc).__name__}",
            ))
            continue
        if not isinstance(payload, dict):
            issues.append(VerifyIssue(
                code="bad_t_view_json", severity="warning",
                trajectory_id=tid,
                detail=f"{tview_path} 顶层不是 dict",
            ))
            continue
        missing = [k for k in PAYLOAD_KEYS if k not in payload]
        if missing:
            issues.append(VerifyIssue(
                code="t_view_missing_keys", severity="warning",
                trajectory_id=tid,
                detail=f"缺键 {missing}",
            ))
    return issues


# ── I/O 门面 ──────────────────────────────────────────────


def verify_from_dir(
    scout_dir: str | Path,
    *,
    task_views_root: str | Path | None = None,
) -> VerifyReport:
    """scout_dir → 跑全部不变量 → :class:`VerifyReport`。

    Args:
        scout_dir: 含 ``trajectory_candidates.jsonl`` + ``trajectory_stats.json`` 的目录。
        task_views_root: T 视图根;``None`` 走 :func:`storage.task_views_dir` 默认根。
    """
    scout_dir = Path(scout_dir)
    report = VerifyReport()
    jsonl_path = scout_dir / TRAJECTORY_BUCKET_FILE
    stats_path = scout_dir / TRAJECTORY_STATS_FILE

    records, issues = _read_jsonl_records(jsonl_path)
    report.issues.extend(issues)
    report.record_count = len(records)

    # 缺 jsonl 时 records 一定是空,后面 stats / T 视图检查也没意义,
    # 但 stats 缺也仍要报(让人能区分「jsonl 在 stats 不在」与「都不在」)
    if not any(i.code == "missing_jsonl" for i in issues):
        stats, stats_issues = _read_stats(stats_path)
        report.issues.extend(stats_issues)
        report.stats = stats
        report.issues.extend(_check_stats_count(len(records), stats))
        report.issues.extend(_check_task_views(records, task_views_root))

    return report


def format_report(report: VerifyReport) -> str:
    """:class:`VerifyReport` → 多行可读文本(给 CLI 打印)。"""
    lines: list[str] = []
    lines.append(f"trajectory 记录: {report.record_count} 条")
    if report.stats:
        lines.append(
            f"stats 三态: {', '.join(f'{k}={v}' for k, v in sorted(report.stats.items()))}"
        )
    if not report.issues:
        lines.append("✓ 全部通过")
        return "\n".join(lines)
    # 按 severity 排序(error 先)
    sorted_issues = sorted(
        report.issues,
        key=lambda i: (0 if i.severity == "error" else 1, i.code, i.trajectory_id),
    )
    for issue in sorted_issues:
        prefix = "✗" if issue.severity == "error" else "⚠"
        tid = f" {issue.trajectory_id}" if issue.trajectory_id else ""
        lines.append(f"  {prefix} {issue.code}{tid}: {issue.detail}")
    lines.append(
        f"\n{report.error_count} error / {report.warning_count} warning"
    )
    return "\n".join(lines)


__all__ = [
    "EXPECTED_STATS_KEYS",
    "VerifyIssue",
    "VerifyReport",
    "verify_from_dir",
    "format_report",
]
