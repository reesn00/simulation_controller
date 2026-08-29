"""轨迹块状态记录 (R5) 与全量摘要输出。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("data_refiner.report")


def write_trajectory(
    trajectory_dir: Path,
    file_id: str,
    events: list[dict],
) -> Path:
    """每个文件一份 JSONL, 每行一个块事件 (含原块内容)。"""
    path = trajectory_dir / f"{file_id}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for event in events:
            record = {"file_id": file_id, **event}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.info("轨迹记录写入 %s: %d 条事件", path.name, len(events))
    return path


def write_summary(
    output_dir: Path,
    summary: dict,
) -> Path:
    path = output_dir / "summary.json"
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("摘要写入 %s", path)
    return path
