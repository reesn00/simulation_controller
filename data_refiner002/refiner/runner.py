"""流程编排: 加载 -> 有效性判定 -> 剪裁 -> thinking 标注 -> 输出与日志。"""

from __future__ import annotations

import logging
from pathlib import Path

from refiner.loader import load_session_files
from refiner.report import write_summary, write_trajectory
from refiner.thinking_check import flag_thinking
from refiner.trimmer import trim
from refiner.validity import check_validity

logger = logging.getLogger("data_refiner.runner")


def _setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "refiner.log"
    root = logging.getLogger("data_refiner")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(console)


def run(
    input_dir: Path,
    output_dir: Path,
    log_dir: Path,
    thinking_min_chars: int = 20,
    thinking_max_chars: int = 4000,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    _setup_logging(log_dir)
    logger.info("=== data_refiner 启动: input=%s output=%s limit=%s dry_run=%s ===",
                input_dir, output_dir, limit, dry_run)

    cleaned_dir = output_dir / "cleaned"
    trajectory_dir = output_dir / "trajectory"
    if not dry_run:
        cleaned_dir.mkdir(parents=True, exist_ok=True)
        trajectory_dir.mkdir(parents=True, exist_ok=True)

    files = load_session_files(input_dir, limit=limit)
    if limit is not None and len(files) < limit:
        logger.warning("请求处理 %d 个文件, 输入目录只有 %d 个可用", limit, len(files))
    file_reports: list[dict] = []

    for loaded in files:
        logger.info("--- 处理 %s ---", loaded.file_id)
        valid, invalid_reason = check_validity(loaded.data)

        if not valid:
            events = [{
                "message_index": None,
                "block_index": None,
                "block_type": None,
                "tool": None,
                "action": "INVALID_FILE",
                "segment_id": None,
                "segment_size": None,
                "reason": invalid_reason,
                "original_block": None,
            }]
            if not dry_run:
                write_trajectory(trajectory_dir, loaded.file_id, events)
            file_reports.append({
                "file_id": loaded.file_id,
                "status": "invalid",
                "reason": invalid_reason,
                "removed_blocks": 0,
                "flagged_thinking": 0,
            })
            logger.warning("%s 无效不处理: %s", loaded.file_id, invalid_reason)
            continue

        trimmed_data, trim_events = trim(loaded.data)
        thinking_events = flag_thinking(
            trimmed_data,
            min_chars=thinking_min_chars,
            max_chars=thinking_max_chars,
        )
        events = trim_events + thinking_events

        if not dry_run:
            cleaned_path = cleaned_dir / f"{loaded.file_id}.json"
            cleaned_path.write_text(
                json_dump(trimmed_data), encoding="utf-8",
            )
            logger.info("剪裁结果写入 %s", cleaned_path.name)
            if events:
                write_trajectory(trajectory_dir, loaded.file_id, events)

        removed = sum(1 for e in trim_events if e["action"] == "REMOVED_EARLY_FAILURE")
        file_reports.append({
            "file_id": loaded.file_id,
            "status": "trimmed" if removed else "unchanged",
            "reason": "",
            "removed_blocks": removed,
            "flagged_thinking": len(thinking_events),
        })

    summary = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "dry_run": dry_run,
        "thinking_min_chars": thinking_min_chars,
        "thinking_max_chars": thinking_max_chars,
        "limit": limit,
        "total_files": len(files),
        "invalid_files": sum(1 for r in file_reports if r["status"] == "invalid"),
        "trimmed_files": sum(1 for r in file_reports if r["status"] == "trimmed"),
        "unchanged_files": sum(1 for r in file_reports if r["status"] == "unchanged"),
        "total_removed_blocks": sum(r["removed_blocks"] for r in file_reports),
        "total_flagged_thinking": sum(r["flagged_thinking"] for r in file_reports),
        "files": file_reports,
    }
    if not dry_run:
        write_summary(output_dir, summary)
    logger.info("=== 完成: %s ===", {
        k: v for k, v in summary.items() if k != "files"
    })
    return summary


def json_dump(data: dict) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)
