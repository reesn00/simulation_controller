"""输入加载与基础结构校验。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("data_refiner.loader")


@dataclass
class LoadedFile:
    file_id: str
    source_path: Path
    data: dict

    @property
    def messages(self) -> list[dict]:
        msgs = self.data.get("messages")
        return msgs if isinstance(msgs, list) else []


def load_session_files(input_dir: Path, limit: int | None = None) -> list[LoadedFile]:
    """按文件名顺序加载 input_dir 下所有 .json 会话文件，坏文件记日志并跳过。

    limit: 最多返回的文件数（坏文件不计入限额）。
    """
    files: list[LoadedFile] = []
    for path in sorted(input_dir.glob("*.json")):
        if limit is not None and len(files) >= limit:
            logger.info("达到 limit=%d, 停止加载后续文件", limit)
            break
        file_id = path.stem
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("文件 %s 无法解析, 跳过: %s", path.name, exc)
            continue
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            logger.error("文件 %s 结构不符合会话 schema (缺少 messages 列表), 跳过", path.name)
            continue
        logger.info("加载 %s: %d 条消息", path.name, len(data["messages"]))
        files.append(LoadedFile(file_id=file_id, source_path=path, data=data))
    if not files:
        logger.warning("输入目录 %s 没有可处理的 json 文件", input_dir)
    return files
