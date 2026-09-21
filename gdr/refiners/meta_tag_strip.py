"""gdr/refiners/meta_tag_strip: 剥离 QwenPaw agent 在 reply 末尾追加的 ⟦...⟧ 审计摘要标签.

该标签由上游 QwenPaw agent system prompt 强制输出, 不属于任务答复本体. 本仓库
不修改上游配置, 而在 save_session 落盘前对所有 4 类导出文件 (messages.json /
openai.json / qwenjina.txt) 做剥离, 同时在 meta.json 写入
``meta_tag_contamination`` 字段供观测.

API:
    - ``strip_meta_tags(text)`` — 纯文本剥离
    - ``annotate_meta_tags(*payloads)`` — 扫描多 payload, 不修改
    - ``strip_session_payload(obj)`` — 递归剥离 dict/list 中的字符串
"""
from __future__ import annotations

import re
from typing import Any, Iterable


# 容忍多行 / 中文标点 / 竖线分隔符 / 不贪婪. 标签长度上限 500 字符以防止误命中
# 含 ⟦ 字符的长篇正文.
_META_TAG_RE = re.compile(r"⟦[^⟧]{0,500}⟧", re.MULTILINE)


def strip_meta_tags(text: str) -> str:
    """从纯文本剥离 ⟦⟧ 块, 并清理多余空行.

    空字符串 / 不含 ⟦ 的字符串原样返回.
    """
    if not text or "⟦" not in text:
        return text
    cleaned = _META_TAG_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if cleaned.endswith("\n\n"):
        cleaned = cleaned.rstrip() + "\n"
    return cleaned


def annotate_meta_tags(*payloads: Iterable[Any]) -> dict[str, Any]:
    """扫描多个 payload, 收集 ⟦⟧ 出现位置. 不修改任何内容.

    Args:
        *payloads: 每个 payload 可以是任意 Python 对象 (str/dict/list/tuple).
            str 直接扫描; dict/list/tuple 遍历元素.

    Returns:
        ``{
            "has_meta_tag": bool,
            "total_count": int,
            "occurrences": [{"path": str, "tag": str, "char_offset": int}, ...]
        }``
        ``path`` 是 JSON-pointer 风格的定位路径, e.g. ``"messages[0].content"``.
    """
    occurrences: list[dict[str, Any]] = []
    for i, payload in enumerate(payloads):
        _scan(payload, f"payload[{i}]", occurrences)
    return {
        "has_meta_tag": bool(occurrences),
        "total_count": len(occurrences),
        "occurrences": [
            {"path": occ["path"], "tag": occ["tag"], "char_offset": occ["off"]}
            for occ in occurrences
        ],
    }


def _scan(obj: Any, path: str, out: list[dict[str, Any]]) -> None:
    if isinstance(obj, str):
        for m in _META_TAG_RE.finditer(obj):
            out.append({"path": path, "tag": m.group(0), "off": m.start()})
    elif isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{path}.{k}" if path else str(k)
            _scan(v, child, out)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _scan(v, f"{path}[{i}]", out)


def strip_session_payload(obj: Any) -> Any:
    """递归剥离 dict / list 中所有 string 字段的 ⟦⟧.

    非字符串原样保留. 空 dict/list 原样返回.
    """
    if isinstance(obj, str):
        return strip_meta_tags(obj)
    if isinstance(obj, dict):
        return {k: strip_session_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_session_payload(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(strip_session_payload(v) for v in obj)
    return obj