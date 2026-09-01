"""orchestration.health: 写 ``orchestration/data/health.json`` 供 CLI status 查询.

字段：
    * queue_counts: ``SQLiteQueue.count_by_state()``
    * batches: ``{batch_id: {status, simulate_started_at, simulate_done_at, qf_count, gdr_count, dead_count}}``
    * last_updated: ISO8601 UTC
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from orchestration.queue import SQLiteQueue

_log = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def collect_batches(queue: SQLiteQueue) -> dict[int, dict[str, object]]:
    """读 SQLite batches 表全部行；返回 ``{batch_id: row_dict}``."""
    out: dict[int, dict[str, object]] = {}
    with queue._conn() as conn:
        rows = conn.execute(
            """
            SELECT id, task_ids, simulate_started_at, simulate_done_at,
                   qf_count, gdr_count, dead_count, status
            FROM batches
            ORDER BY id
            """
        ).fetchall()
    for r in rows:
        out[int(r["id"])] = {
            "task_ids": (r["task_ids"] or "").split(",") if r["task_ids"] else [],
            "simulate_started_at": r["simulate_started_at"],
            "simulate_done_at": r["simulate_done_at"],
            "qf_count": int(r["qf_count"] or 0),
            "gdr_count": int(r["gdr_count"] or 0),
            "dead_count": int(r["dead_count"] or 0),
            "status": r["status"],
        }
    return out


def write_health(
    queue: SQLiteQueue,
    *,
    output_path: Path,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """收集状态写到 ``output_path``；返回写入的 dict."""
    payload: dict[str, object] = {
        "last_updated": _utc_now_iso(),
        "queue_counts": queue.count_by_state(),
        "batches": collect_batches(queue),
    }
    if extra:
        payload.update(extra)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    _log.debug("health: wrote %s", output_path)
    return payload
