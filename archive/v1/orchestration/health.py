"""orchestration.health: 写 ``<log_dir>/health.json`` 供 CLI status 查询.

字段 (契约 §6.5):
    * phases:        ``SQLiteQueue.count_by_phase()``
    * total:         phases 所有计数之和
    * last_updated:  ISO8601 UTC

老的 ``collect_batches`` / batches 表 / dead_count / gdr_count 等字段已删除
(契约 §6.5 明确不导出)。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestration.queue import ALL_PHASES, SQLiteQueue

_log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def collect_tasks(queue: SQLiteQueue) -> dict[str, Any]:
    """统计 tasks 表状态 (契约 §6.5).

    返回:
        {
            "phases": {"pending": int, "simulate": int, "gdr": int,
                       "etl": int, "done": int, "dead": int},
            "total": int,
            "last_updated": str,  # ISO8601
        }
    """
    counts = queue.count_by_phase()
    # 契约 §6.5 给的示例要求 6 个 phase 全字段 (含 0 计数的);
    # count_by_phase 已返回全分布, 这里再覆盖一次保证 keys 完整。
    phases: dict[str, int] = {p: int(counts.get(p, 0)) for p in ALL_PHASES}
    total = sum(phases.values())
    return {
        "phases": phases,
        "total": total,
        "last_updated": _utc_now_iso(),
    }


def write_health(
    queue: SQLiteQueue,
    *,
    log_dir: Path,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """收集状态写到 ``<log_dir>/health.json``; 返回写入的 dict.

    Args:
        queue: SQLite 队列 (读 tasks 表)
        log_dir: 日志目录 (不存在 → 自动 mkdir)
        extra: 额外写入 health.json 的字段 (例如 status / summary / submitted)
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    output_path = log_dir / "health.json"

    payload: dict[str, object] = collect_tasks(queue)
    if extra:
        payload.update(extra)
    with output_path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    _log.debug("health: wrote %s", output_path)
    return payload