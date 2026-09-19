"""orchestration.failure_handler: 把 ``state=dead`` 的 task 产物移入 dead 目录.

职责：
    1. 读 SQLite ``tasks WHERE state='dead'``
    2. 把 ``src_path`` 与 ``qf_output_path``（如果存在）move 到 ``dead_dir/<batch>_<task_id>__<src_basename>``
       + 把 ``qf_output_path`` 同步保留到 ``dead_dir/qf_out/``（Fix C），便于审计/
       用更优 gdr 配置回灌
    3. 追加一行 ``dead.log`` 与一行 ``dead/INDEX.jsonl``
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
    preserve_qf_out_in_dead: bool = True,
    dead_index_path: Path | None = None,
) -> list[DeadArchive]:
    """把所有 ``state=dead`` 的 task 的产物移入 ``dead_dir``，并写 ``dead.log``.

    Args:
        preserve_qf_out_in_dead: Fix C 开关；True 时 qf_output_path 同步复制到
            ``dead_dir/qf_out/<basename>``，便于改进 gdr 配置后回灌。默认 True。
        dead_index_path: 追加一行 dead 归档索引 (jsonl)；None 时不写。

    Returns: 被归档的 ``DeadArchive`` 列表（一个 task 可能移 0~2 个文件）。
    """
    dead_dir = Path(dead_dir)
    dead_dir.mkdir(parents=True, exist_ok=True)
    qf_out_dead_dir = dead_dir / "qf_out"
    if preserve_qf_out_in_dead:
        qf_out_dead_dir.mkdir(parents=True, exist_ok=True)

    with queue._conn() as conn:
        rows = conn.execute(
            """
            SELECT id, src_path, batch_id, qf_output_path,
                   gdr_messages_path, gdr_openai_path, gdr_qwenjina_path,
                   gdr_meta_path,
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

        for col in (
            "src_path", "qf_output_path",
            "gdr_messages_path", "gdr_openai_path",
            "gdr_qwenjina_path", "gdr_meta_path",
        ):
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

        # Fix C: qf_output_path 同步复制到 dead/qf_out/, 便于回灌
        qf_out_dead_path: str | None = None
        if preserve_qf_out_in_dead and r["qf_output_path"]:
            qf_src = Path(r["qf_output_path"])
            # 优先用刚 move 的 target (qf_output_path 已不在原位); 找不到再用
            # 原始路径 (罕见: 上一步 move 失败的容错路径)
            moved_target = dead_dir / f"{prefix}__{qf_src.name}"
            qf_to_copy = moved_target if moved_target.is_file() else (
                qf_src if qf_src.is_file() else None
            )
            if qf_to_copy is not None:
                qf_target = qf_out_dead_dir / qf_src.name
                try:
                    shutil.copy2(str(qf_to_copy), str(qf_target))
                    qf_out_dead_path = str(qf_target)
                    _log.info(
                        "dead archive: preserved qf_out %s -> %s",
                        qf_to_copy, qf_target,
                    )
                except OSError as exc:
                    _log.warning(
                        "dead archive: failed to copy qf_out for task %d: %s",
                        task_id, exc,
                    )

        if moved:
            log_entry = {
                "task_id": task_id,
                "batch_id": batch_id,
                "attempts_qf": int(r["attempts_qf"] or 0),
                "attempts_gdr": int(r["attempts_gdr"] or 0),
                "error_msg": r["error_msg"],
                "moved_to": moved,
                "qf_out_dead_path": qf_out_dead_path,
                "archived_at": _utc_now_iso(),
            }
            if dead_log_path is not None:
                dead_log_path.parent.mkdir(parents=True, exist_ok=True)
                with dead_log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            # Fix C: 索引 jsonl, 每行一条 dead 归档; 含 gdr_status / score /
            # reason 等元信息 (从 error_msg 解析)
            if dead_index_path is not None:
                index_entry = dict(log_entry)
                # 从 error_msg 解析 gdr_status / score (格式:
                # "[non-retryable] NonRetryableError: gdr worker gdr_0:
                # gdr status='discard' (task=N) \nTraceback...")
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


def _extract_score(error_msg: str | None) -> int | None:
    """从 error_msg 解析 judge score (若 error_msg 中包含 score=N)."""
    if not error_msg:
        return None
    import re
    m = re.search(r"score[=:]?\s*(\d+)", error_msg)
    return int(m.group(1)) if m else None


def _extract_reason(error_msg: str | None) -> str | None:
    """从 error_msg 截取 reason 段 (NonRetryableError 第一行)。"""
    if not error_msg:
        return None
    first_line = error_msg.splitlines()[0] if error_msg else ""
    return first_line[:500]


def reprocess_dead(
    *,
    dead_dir: Path,
    qf_out_target: Path,
    dead_index_path: Path,
    filter_score_lt: int | None = None,
    filter_gdr_status: str | None = None,
) -> list[Path]:
    """Fix C: 从 dead/qf_out/ 把 qf_out 拷贝回 qf_out_target, 供重跑 gdr.

    Args:
        dead_dir: failure_handler 写入的 dead 根目录
        qf_out_target: 目标 qf_out 目录 (默认 output/qf_out)
        dead_index_path: failure_handler 写入的 INDEX.jsonl
        filter_score_lt: 仅回灌 score < 该值的 task; None = 全部
        filter_gdr_status: 仅回灌 gdr_status 等于该值的 task; None = 全部

    Returns: 实际拷贝的 qf_out 路径列表 (供调用方进一步处理).
    """
    qf_out_target = Path(qf_out_target)
    qf_out_target.mkdir(parents=True, exist_ok=True)

    if not dead_index_path.is_file():
        _log.warning("dead index %s missing, nothing to reprocess", dead_index_path)
        return []

    copied: list[Path] = []
    with dead_index_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if filter_score_lt is not None:
                score = entry.get("score")
                if score is None or score >= filter_score_lt:
                    continue
            if filter_gdr_status is not None:
                if entry.get("gdr_status") != filter_gdr_status:
                    continue
            qf_out_dead = entry.get("qf_out_dead_path")
            if not qf_out_dead:
                continue
            src = Path(qf_out_dead)
            if not src.is_file():
                _log.warning("reprocess_dead: %s missing, skip", src)
                continue
            dst = qf_out_target / src.name
            shutil.copy2(str(src), str(dst))
            copied.append(dst)
            _log.info("reprocess_dead: copied %s -> %s", src, dst)
    return copied
