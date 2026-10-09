from __future__ import annotations

import json
import re

from simulate_serve.domain.task import AcceptanceCriterion
from simulate_serve.domain.validation import CriterionResult

from .common import failed, passed


def list_items(text: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^\s*(?:[-*+]\s+|\d+[.)、]\s+)(.+)$", text)
        if match.group(1).strip()
    ]


def markdown_table(text: str) -> tuple[list[str], list[list[str]]] | None:
    lines = [line.strip() for line in text.splitlines() if "|" in line]
    for index in range(len(lines) - 1):
        separator = [cell.strip() for cell in lines[index + 1].strip("|").split("|")]
        if separator and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
            headers = [cell.strip() for cell in lines[index].strip("|").split("|")]
            rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines[index + 2 :]]
            if headers and rows and all(len(row) == len(headers) for row in rows):
                return headers, rows
    return None


class FormatValidator:
    def validate(self, criterion: AcceptanceCriterion, text: str) -> CriterionResult:
        required = str(criterion.parameters.get("format", "text")).lower()
        if required == "text":
            return passed(criterion) if text.strip() else failed(criterion, "RESPONSE_EMPTY", "回复为空", retryable=False)
        if required == "json":
            try:
                parsed = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                return failed(criterion, "FORMAT_JSON_REQUIRED", "回复不是完整的 JSON")
            shape = criterion.parameters.get("shape")
            if shape == "object" and not isinstance(parsed, dict):
                return failed(criterion, "FORMAT_JSON_OBJECT_REQUIRED", "JSON 顶层必须是对象")
            if shape == "array" and not isinstance(parsed, list):
                return failed(criterion, "FORMAT_JSON_ARRAY_REQUIRED", "JSON 顶层必须是数组")
            return passed(criterion, "JSON 格式合法")
        if required == "list":
            return passed(criterion, "列表格式合法") if list_items(text) else failed(criterion, "FORMAT_LIST_REQUIRED", "请使用项目符号或编号列表")
        if required == "table":
            return passed(criterion, "Markdown 表格格式合法") if markdown_table(text) else failed(criterion, "FORMAT_TABLE_REQUIRED", "请使用有表头和分隔行的 Markdown 表格")
        if required == "card":
            fields = re.findall(r"(?m)^\s*(?:[-*]\s*)?(?:\*\*)?[^:\uff1a\n]{1,30}(?:\*\*)?\s*[:：]\s*\S+", text)
            return passed(criterion, "卡片字段格式合法") if len(fields) >= 2 else failed(criterion, "FORMAT_CARD_REQUIRED", "请用明确的字段名和字段值组织卡片")
        return failed(criterion, "FORMAT_UNSUPPORTED", f"不支持的格式：{required}", retryable=False)
