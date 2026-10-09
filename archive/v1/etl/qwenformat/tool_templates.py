"""etl.qwenformat.tool_templates: 从 trajectory tools 提取本地 tool 模板.

把 ``model_request.payload.tools`` 中的 function 定义渲染成固定格式的文本,
保存为 ``templates_dir/tools/<tool_name>.txt``.
处理时读取本地模板再渲染成 tools section, 与 system prompt 其他段组装.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def _normalize_tool_name(name: str) -> str:
    """把 tool 名变成合法文件名."""
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name) or "unknown"


def render_tool_definition(tool: dict, *, indent: int = 2) -> str:
    """把 OpenAI function tool 渲染成可读的文本描述.

    输出包含:
        - Function: <name>
        - Description: <description>
        - Parameters: <JSON schema>
    """
    if not isinstance(tool, dict):
        return ""
    func = tool.get("function") or tool
    name = func.get("name") or tool.get("name") or "unknown"
    description = func.get("description") or ""
    parameters = func.get("parameters") or {}

    lines = [f"Function: {name}"]
    if description:
        lines.append(f"Description: {description}")
    else:
        lines.append("Description: (no description)")
    if parameters:
        try:
            params_json = json.dumps(parameters, ensure_ascii=False, indent=indent)
        except (TypeError, ValueError):
            params_json = str(parameters)
        lines.append("Parameters:")
        lines.extend("  " + line for line in params_json.splitlines())
    return "\n".join(lines)


def render_tools_section(tools: list[dict], *, title: str = "# Tools", indent: int = 2) -> str:
    """渲染 tools section.

    去重、保持首次出现顺序, 用 ``title`` 作为标题.
    """
    if not tools:
        return ""

    seen: set[str] = set()
    unique_tools: list[dict] = []
    for t in tools:
        func = t.get("function") or t
        name = func.get("name") or t.get("name") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        unique_tools.append(t)

    parts = [title]
    for t in unique_tools:
        rendered = render_tool_definition(t, indent=indent)
        if rendered:
            parts.append(rendered)
    return "\n\n".join(parts)


def save_tool_templates(
    tools: list[dict],
    templates_dir: Path,
    *,
    update_existing: bool = True,
    stats: Optional[dict[str, int]] = None,
) -> dict[str, Path]:
    """把每个 tool 的文本模板保存到 ``templates_dir/tools/<tool_name>.txt``.

    Args:
        tools: OpenAI tools 列表.
        templates_dir: 模板根目录.
        update_existing: 为 False 时, 已存在的模板不会被覆盖.
        stats: 可选统计 dict, 会写入 ``tool_template_saved`` /
            ``tool_template_updated`` / ``tool_template_unchanged``.

    Returns:
        tool_name -> path 的映射.
    """
    def bump(key: str) -> None:
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    tools_dir = templates_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)

    saved: dict[str, Path] = {}
    seen: set[str] = set()
    for t in tools:
        func = t.get("function") or t
        name = func.get("name") or t.get("name") or ""
        if not name or name in seen:
            continue
        seen.add(name)

        path = tools_dir / f"{_normalize_tool_name(name)}.txt"
        content = render_tool_definition(t)

        if path.exists() and not update_existing:
            bump("tool_template_unchanged")
            saved[name] = path
            continue

        old = path.read_text(encoding="utf-8") if path.exists() else None
        path.write_text(content + "\n", encoding="utf-8")
        if old is None:
            bump("tool_template_saved")
        elif old.strip() != content.strip():
            bump("tool_template_updated")
        else:
            bump("tool_template_unchanged")
        saved[name] = path
    return saved


def load_tool_templates(templates_dir: Path) -> dict[str, str]:
    """读取 ``templates_dir/tools/*.txt`` 返回 tool_name -> content 映射."""
    tools_dir = templates_dir / "tools"
    if not tools_dir.exists():
        return {}
    out: dict[str, str] = {}
    for path in sorted(tools_dir.glob("*.txt")):
        name = path.stem
        out[name] = path.read_text(encoding="utf-8")
    return out


def render_tools_section_from_templates(
    tools: list[dict],
    templates_dir: Path,
    *,
    title: str = "# Tools",
) -> str:
    """优先用本地 tool 模板渲染 tools section.

    本地不存在的 tool 用原始定义渲染.
    """
    local = load_tool_templates(templates_dir)
    seen: set[str] = set()
    parts = [title]
    for t in tools:
        func = t.get("function") or t
        name = func.get("name") or t.get("name") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        key = _normalize_tool_name(name)
        if key in local:
            parts.append(local[key].strip())
        else:
            parts.append(render_tool_definition(t))
    return "\n\n".join(parts)
