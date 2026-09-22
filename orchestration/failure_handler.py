"""orchestration.failure_handler: 把 ``phase=dead`` 的 task 产物移入 dead 目录.

新架构 (simulation server → gdr → etl) 下, task 可能死在任意阶段:
    * 死在 simulate 阶段: 只有 ``src_path`` 可能存在
    * 死在 gdr 阶段:  ``src_path`` + ``gdr_refined_path`` (若 gdr 写了部分)
    * 死在 etl 阶段:  ``src_path`` + ``gdr_refined_path`` + ``etl_*_path`` 部分

职责:
    1. 读 SQLite ``tasks WHERE phase='dead'``
    2. 把 ``src_path`` / ``gdr_refined_path`` / ``etl_*_path`` (存在的)
       move 到 ``dead_dir/<task_id>__<src_basename>``
    3. 写 ``dead.log`` 与 ``dead/INDEX.jsonl``

边界:
    * 源文件不存在 → 跳过该文件、记录 warning
    * dead_dir 不存在 → 自动创建
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from orchestration.queue import PHASE_DEAD, SQLiteQueue

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeadArchive:
    """一个 dead task 的归档信息 (契约 §6.4 — 删 batch_id 字段)."""

    task_id: int
    src_path: str
    moved_to: list[str]


# 新架构下需要归档的产物列 (按归档顺序写 dead.log, 顺序无关紧要但保持稳定)
_DEAD_ARCHIVE_COLUMNS = (
    "src_path",
    "gdr_refined_path",
    "etl_messages_path",
    "etl_openai_path",
    "etl_qwenjina_path",
    "etl_meta_path",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def reap_dead(
    queue: SQLiteQueue,
    *,
    dead_dir: Path,
    dead_log_path: Path | None = None,
    dead_index_path: Path | None = None,
) -> list[DeadArchive]:
    """把所有 ``phase=dead`` 的 task 的产物移入 ``dead_dir``, 并写 ``dead.log``.

    Args:
        queue: SQLite 队列
        dead_dir: 归档根目录 (不存在 → 自动创建)
        dead_log_path: 追加一行 dead 归档记录 (jsonl); None 时不写。
        dead_index_path: 追加一行 dead 索引 jsonl; None 时不写。

    Returns:
        被归档的 ``DeadArchive`` 列表 (一个 task 可能移 0~N 个文件)。
    """
    dead_dir = Path(dead_dir)
    dead_dir.mkdir(parents=True, exist_ok=True)

    with queue._conn() as conn:
        rows = conn.execute(
            """
            SELECT id, src_path,
                   gdr_refined_path,
                   etl_messages_path, etl_openai_path, etl_qwenjina_path,
                   etl_meta_path,
                   attempts_gdr, attempts_etl, error_msg
            FROM tasks
            WHERE phase = ?
            ORDER BY id
            """,
            (PHASE_DEAD,),
        ).fetchall()

    archives: list[DeadArchive] = []
    for r in rows:
        task_id = int(r["id"])
        prefix = f"{task_id}"
        moved: list[str] = []

        for col in _DEAD_ARCHIVE_COLUMNS:
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
                "attempts_gdr": int(r["attempts_gdr"] or 0),
                "attempts_etl": int(r["attempts_etl"] or 0),
                "error_msg": r["error_msg"],
                "moved_to": moved,
                "archived_at": _utc_now_iso(),
            }
            if dead_log_path is not None:
                dead_log_path.parent.mkdir(parents=True, exist_ok=True)
                with dead_log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            # dead 索引 jsonl, 每行一条 dead 归档; 含 gdr_status / score /
            # reason 等元信息 (从 error_msg 解析), 便于改进重跑用
            if dead_index_path is not None:
                index_entry = dict(log_entry)
                index_entry["gdr_status"] = _extract_gdr_status(r["error_msg"])
                index_entry["score"] = _extract_score(r["error_msg"])
                index_entry["reason"] = _extract_reason(r["error_msg"])
                dead_index_path.parent.mkdir(parents=True, exist_ok=True)
                with dead_index_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(index_entry, ensure_ascii=False) + "\n")
        archives.append(DeadArchive(task_id=task_id, src_path=r["src_path"], moved_to=moved))

    if archives:
        _log.info("failure_handler: archived %d dead task(s)", len(archives))
    return archives


def _extract_gdr_status(error_msg: str | None) -> str | None:
    """从 error_msg 解析 gdr_status (status='discard' / 'dead' 等)."""
    if not error_msg:
        return None
    import re
    m = re.search(r"status='(\w+)'", error_msg)
    return m.group(1) if m else None


def _extract_score(error_msg: str | None) -> str | None:
    """从 error_msg 解析 judge score (若 error_msg 中包含 score=N)."""
    if not error_msg:
        return None
    import re
    m = re.search(r"score[=:]?\s*(\d+)", error_msg)
    return m.group(1) if m else None


def _extract_reason(error_msg: str | None) -> str | None:
    """从 error_msg 截取 reason 段 (NonRetryableError 第一行)。"""
    if not error_msg:
        return None
    first_line = error_msg.splitlines()[0] if error_msg else ""
    return first_line[:500]