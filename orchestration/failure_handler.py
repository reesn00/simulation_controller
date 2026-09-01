"""orchestration.failure_handler: 把 ``state=dead`` 的 task 产物移入 dead 目录.

职责：
    1. 读 SQLite ``tasks WHERE state='dead'``
    2. 把 ``src_path`` 与 ``qf_output_path``（如果存在）move 到 ``dead_dir/<batch>_<task_id>__<src_basename>``
    3. 追加一行 ``dead.log``
    4. 标记 task 已经归档（避免重复处理）：目前用 dead_count 在 batches 表里 ++；
       但任务本身的 state 仍保持 ``dead`` 不变（worker 不会再去拉）。

边界：
    - 源文件不存在 → 跳过该文件、记录 warning
    - dead_dir 不存在 → 自动创建
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from orchestration.queue import STATE_DEAD, SQLiteQueue

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeadArchive:
    task_id: int
    src_path: str
    moved_to: list[str]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def reap_dead(
    queue: SQLiteQueue,
    *,
    dead_dir: Path,
    dead_log_path: Path | None = None,
) -> list[DeadArchive]:
    """把所有 ``state=dead`` 的 task 的产物移入 ``dead_dir``，并写 ``dead.log``.

    Returns: 被归档的 ``DeadArchive`` 列表（一个 task 可能移 0~2 个文件）。
    """
    dead_dir = Path(dead_dir)
    dead_dir.mkdir(parents=True, exist_ok=True)

    with queue._conn() as conn:
        rows = conn.execute(
            """
            SELECT id, src_path, batch_id, qf_output_path, gdr_output_path,
                   attempts_qf, attempts_gdr, error_msg
            FROM tasks
            WHERE state = ?
            ORDER BY id
            """,
            (STATE_DEAD,),
        ).fetchall()

    archives: list[DeadArchive] = []
    for r in rows:
        task_id = int(r["id"])
        batch_id = int(r["batch_id"])
        prefix = f"{batch_id}_{task_id}"
        moved: list[str] = []

        for col in ("src_path", "qf_output_path", "gdr_output_path"):
            src = r[col]
            if not src:
                continue
            src_path = Path(src)
            if not src_path.is_file():
                _log.warning("dead archive: %s missing for task %d", col, task_id)
                continue
            target = dead_dir / f"{prefix}__{src_path.name}"
            try:
                shutil.move(str(src_path), str(target))
                moved.append(str(target))
            except OSError as exc:
                _log.warning(
                    "dead archive: failed to move %s for task %d: %s",
                    src_path, task_id, exc,
                )

        if moved:
            log_entry = {
                "task_id": task_id,
                "batch_id": batch_id,
                "attempts_qf": int(r["attempts_qf"] or 0),
                "attempts_gdr": int(r["attempts_gdr"] or 0),
                "error_msg": r["error_msg"],
                "moved_to": moved,
                "archived_at": _utc_now_iso(),
            }
            if dead_log_path is not None:
                dead_log_path.parent.mkdir(parents=True, exist_ok=True)
                with dead_log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        archives.append(DeadArchive(task_id=task_id, src_path=r["src_path"], moved_to=moved))

    if archives:
        _log.info("failure_handler: archived %d dead task(s)", len(archives))
    return archives
