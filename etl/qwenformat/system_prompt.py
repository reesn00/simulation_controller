"""etl.qwenformat.system_prompt: 把 trajectory system prompt 拆分为可复用模板并清洗.

核心能力:
    - ``partition_system_prompt``: 按 Markdown 标题 / 已知标记把 system prompt
      切分为有序段, 并标记为 identity / constraint / framework / unknown.
    - ``render_cleaned_system``: 去掉 framework 段, 把 identity、约束段与 tool
      模板按原顺序重组成新的 system prompt.
    - ``save_section_templates`` / ``load_section_templates``: 把保留段持久化为本地
      文本模板, 允许人工编辑后复用.

被归为 ``framework`` 的段(不保留为模板、不进入新 system):
    - AGENTS.md / SOUL.md / PROFILE.md
    - ``====================`` 开头的 About / OS / Channel 等框架元信息块

被归为 ``constraint`` 的段(保留为模板、进入新 system):
    - Agent Identity
    - ``<agent-skills>...</agent-skills>`` 技能列表
    - 长期记忆 / RETRIEVAL HEADLINE / THE MAP / DISCIPLINE 等
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class SystemSection:
    """system prompt 中的一个连续段."""

    kind: str           # "identity" | "constraint" | "framework" | "unknown"
    title: str          # 段标题(可为空)
    content: str        # 段文本(含标题)
    index: int          # 原顺序索引


# ---------------------------------------------------------------------------
# 分类规则
# ---------------------------------------------------------------------------

# 显式边界: (正则, 类别, 标题)
# 顺序很重要: 先出现的边界优先匹配; 因此应把更具体的模式放在前面.
_SYSTEM_BOUNDARIES: list[tuple[str, str, str]] = [
    (r"^# Agent Identity\s*$", "identity", "Agent Identity"),
    (r"^# AGENTS\.md\s*$", "framework", "AGENTS.md"),
    (r"^# SOUL\.md\s*$", "framework", "SOUL.md"),
    (r"^# PROFILE\.md\s*$", "framework", "PROFILE.md"),
    (r"^You can only understand text content", "constraint", "Image Understanding"),
    (r"^### Directories\s*$", "constraint", "Directories"),
    (r"^你的对话会被持久记录", "constraint", "Conversation Persistence"),
    (r"^检索标题（RETRIEVAL HEADLINE）", "constraint", "RETRIEVAL HEADLINE"),
    (r"^地图（THE MAP）", "constraint", "THE MAP"),
    (r"^纪律（DISCIPLINE）", "constraint", "DISCIPLINE"),
    (r"^={10,}", "framework", "Framework Info"),
    (r"^<agent-skills>", "constraint", "agent-skills"),
    (r"^# 长期记忆\s*$", "constraint", "长期记忆"),
]

_AGENT_SKILLS_END = "</agent-skills>"


def _slugify(title: str) -> str:
    """把标题变成合法文件名."""
    s = title.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[-\s]+", "_", s)
    return s or "section"


def _build_boundary_regex() -> re.Pattern[str]:
    """把所有边界正则合并成一个可定位每段起点的 pattern."""
    groups = [f"({pat})" for pat, _, _ in _SYSTEM_BOUNDARIES]
    return re.compile("|".join(groups), re.MULTILINE)


_BOUNDARY_RE = _build_boundary_regex()


def _boundary_kind_title(match: re.Match[str]) -> tuple[str, str]:
    """根据 combined regex 的捕获组确定边界类别与标题."""
    for i, (_, kind, title) in enumerate(_SYSTEM_BOUNDARIES):
        if match.group(i + 1) is not None:
            return kind, title
    return "unknown", ""


def partition_system_prompt(text: str) -> list[SystemSection]:
    """把 system prompt 切分为有序段并分类.

    使用显式边界列表而非纯 Markdown 标题层级, 避免把 AGENTS.md / SOUL.md /
    PROFILE.md 内部的 ## / ### 子标题误判为独立段.

    Args:
        text: 原始 system prompt 文本.

    Returns:
        ``SystemSection`` 列表, ``index`` 保持原顺序.
    """
    if not text:
        return []

    matches = list(_BOUNDARY_RE.finditer(text))
    if not matches:
        return [SystemSection(kind="unknown", title="", content=text, index=0)]

    sections: list[SystemSection] = []

    # 第一个边界前的序言
    first_start = matches[0].start()
    if first_start > 0:
        preamble = text[:first_start]
        if preamble.strip():
            sections.append(SystemSection(
                kind="unknown", title="", content=preamble, index=0,
            ))

    for i, m in enumerate(matches):
        kind, title = _boundary_kind_title(m)
        start = m.start()

        if title == "agent-skills":
            # <agent-skills> 是整块, 找到 </agent-skills>
            end_pos = text.find(_AGENT_SKILLS_END, start)
            end = end_pos + len(_AGENT_SKILLS_END) if end_pos != -1 else len(text)
        else:
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

        sections.append(SystemSection(
            kind=kind,
            title=title,
            content=text[start:end],
            index=len(sections),
        ))

    return sections


def section_template_path(templates_dir: Path, section: SystemSection) -> Path:
    """计算某段的本地模板文件路径."""
    if section.kind == "identity":
        subdir = templates_dir / "role"
        name = "identity.txt"
    elif section.kind == "constraint":
        subdir = templates_dir / "constraints"
        name = _slugify(section.title) + ".txt" if section.title else f"section_{section.index}.txt"
    elif section.kind == "unknown":
        subdir = templates_dir / "unknown"
        name = _slugify(section.title) + ".txt" if section.title else f"section_{section.index}.txt"
    else:
        # framework 不存模板
        raise ValueError(f"framework section has no template path: {section.title}")
    subdir.mkdir(parents=True, exist_ok=True)
    return subdir / name


def save_section_templates(
    sections: list[SystemSection],
    templates_dir: Path,
    *,
    update_existing: bool = True,
    stats: Optional[dict[str, int]] = None,
) -> list[Path]:
    """把保留段(identity / constraint / unknown)写入本地模板文件.

    Args:
        sections: partition 结果.
        templates_dir: 模板根目录.
        update_existing: 为 False 时, 已存在的模板不会被覆盖(保留人工编辑).
        stats: 可选统计 dict, 会写入 ``template_saved`` / ``template_unchanged``.

    Returns:
        写入(或已存在)的文件路径列表.
    """
    def bump(key: str) -> None:
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    saved: list[Path] = []
    for sec in sections:
        if sec.kind == "framework":
            continue
        path = section_template_path(templates_dir, sec)
        if path.exists() and not update_existing:
            bump("template_unchanged")
            saved.append(path)
            continue
        content = sec.content.strip()
        # 去掉标题? 不, 保留标题, 这样模板自包含.
        old = path.read_text(encoding="utf-8") if path.exists() else None
        path.write_text(content + "\n", encoding="utf-8")
        if old is None:
            bump("template_saved")
        elif old.strip() != content.strip():
            bump("template_updated")
        else:
            bump("template_unchanged")
        saved.append(path)
    return saved


def load_section_template(templates_dir: Path, section: SystemSection) -> str:
    """读取某段的本地模板; 不存在时返回原 content."""
    if section.kind == "framework":
        return section.content
    path = section_template_path(templates_dir, section)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return section.content


def render_cleaned_system(
    sections: list[SystemSection],
    tools_text: str = "",
    *,
    templates_dir: Optional[Path] = None,
    insert_tools_at: str = "agent-skills",
) -> tuple[str, dict[str, int]]:
    """把保留段与 tool 模板重组成新的 system prompt.

    Args:
        sections: partition 结果.
        tools_text: 已渲染的 tool schema 文本; 为空时不插入.
        templates_dir: 若提供, 优先读取本地模板替换对应段.
        insert_tools_at: ``agent-skills`` 时插入到原 ``<agent-skills>`` 段位置;
            未找到则追加到末尾. ``end`` 时直接追加到末尾.

    Returns:
        (new_system_text, stats)
    """
    stats: dict[str, int] = {
        "identity_sections": 0,
        "constraint_sections": 0,
        "framework_sections": 0,
        "unknown_sections": 0,
    }

    agent_skills_index: Optional[int] = None
    rendered_parts: list[str] = []
    for sec in sections:
        stats[f"{sec.kind}_sections"] = stats.get(f"{sec.kind}_sections", 0) + 1
        if sec.kind == "framework":
            if insert_tools_at == "agent-skills" and sec.title == "agent-skills" and agent_skills_index is None:
                agent_skills_index = len(rendered_parts)
            continue
        content = load_section_template(templates_dir, sec) if templates_dir else sec.content
        rendered_parts.append(content)

    tools_text_stripped = tools_text.strip()
    if tools_text_stripped:
        if insert_tools_at == "agent-skills" and agent_skills_index is not None:
            rendered_parts.insert(agent_skills_index, tools_text_stripped)
        else:
            rendered_parts.append(tools_text_stripped)

    # 用双换行拼接各段, 保持可读性
    return "\n\n".join(p.strip() for p in rendered_parts if p.strip()), stats
