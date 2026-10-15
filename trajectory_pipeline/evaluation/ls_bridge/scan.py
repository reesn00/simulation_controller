"""ls_bridge.scan: R11 凭据扫描 —— fail-closed 拒推。

照读自 ``archive/v1/label_studio/task_exporter.py`` 的扫描段（v1 方案 §16
R11）, 拆成独立模块。设计理由不变: 静默脱敏会使标注样本与训练样本不一致,
所以命中即拒推, 让人来决定怎么处理。

ls_bridge 设计约束（见 ``__init__.py``）: 凭据扫描**双向不豁免** —— 即便
将来开了回流, 从 LS 拉回的数据同样要过这道闸。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from trajectory_pipeline.evaluation.ls_bridge.settings import CredentialScanSettings

log = logging.getLogger(__name__)


@dataclass
class ScanHit:
    """一次扫描命中记录。**只记录位置, 不记录命中的原文**（§16 R9）。"""

    view: str
    pattern_index: int
    char_offset: int

    def describe(self) -> str:
        return f"view={self.view} pattern#{self.pattern_index} offset={self.char_offset}"


def scan_for_credentials(
    payload: Any, *, view: str, settings: CredentialScanSettings
) -> list[ScanHit]:
    """在 payload 中扫描凭据形态。

    扫描**序列化后的文本**而非 Python 对象 —— tool_call 的 ``input`` 可能是
    任意嵌套 dict, 序列化后统一成一条可 grep 的文本流, 也不会漏掉被
    ``str()`` 掩盖的值。
    """
    if not settings.enabled:
        return []
    try:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        # 不可序列化本身就是异常形态, 交给上游解析报错, 这里不重复拦。
        return []

    hits: list[ScanHit] = []
    for index, source in enumerate(settings.patterns):
        try:
            matched = re.search(source, text)
        except re.error:
            log.warning("scan_for_credentials: pattern#%d 非法正则, 跳过", index)
            continue
        if matched:
            hits.append(ScanHit(view=view, pattern_index=index, char_offset=matched.start()))
    return hits


def scan_views(
    data: dict[str, Any], views: tuple[str, ...], settings: CredentialScanSettings
) -> list[ScanHit]:
    """对 task.data 的若干字段逐一扫描（exporter 的批量入口）。"""
    hits: list[ScanHit] = []
    for view in views:
        if view in data and data[view]:
            hits.extend(scan_for_credentials(data[view], view=view, settings=settings))
    return hits


__all__ = ["ScanHit", "scan_for_credentials", "scan_views"]
