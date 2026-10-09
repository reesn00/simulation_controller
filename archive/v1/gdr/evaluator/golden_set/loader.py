"""golden_set: 金标集加载 (方案 trajectory-scoring-two-layer.md §6 第一步).

金标集结构: 每条含 original_session + refined_session + human_compare_result
+ human_free_result, 用于校准评审 prompt + 设定独立式阈值.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def load_golden_set(path: Path) -> list[dict]:
    """加载金标集 (JSONL 格式, 每行一条)."""
    path = Path(path)
    if not path.is_file():
        log.warning("golden set not found: %s", path)
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            log.warning("golden set skip bad line: %s", e)
    log.info("golden set loaded %d entries from %s", len(out), path)
    return out


def load_anchors(path: Path, count: int = 50) -> list[dict]:
    """加载锚点子集 (前 count 条, 供漂移监控)."""
    full = load_golden_set(path)
    return full[:count]


def save_baseline(scores: dict[str, int], path: Path) -> None:
    """保存锚点基线分 (anchor_id → score)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8")


def load_baseline(path: Path) -> dict[str, int]:
    """加载锚点基线分."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        return {k: int(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
    except Exception as e:
        log.warning("baseline load failed: %s", e)
        return {}
