"""etl.qwenformat.system_prompt: system prompt 段级拆解 + 模板渲染.

方案 etl-prune-frontload.md §6 P0 实施后:
    - ``partition_system_prompt`` 与 ``SystemSection`` 已迁到
      ``gdr.refiners.system_prompt`` (C1→C2 阶段统一实现).
    - 本包保留 etl 渲染专属能力:
        - ``render_cleaned_system`` — 重组清洗后的 system prompt (qf_text)
        - ``save_section_templates`` / ``load_section_template`` — 模板持久化
        - ``section_template_path`` — 路径计算
        - ``_REASONING_REQUIREMENT_SECTION`` — F3-C 推理要求段

partition 行为从 gdr.refiners.system_prompt 导入, 与 gdr 阶段保持单实现真值.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# partition_system_prompt 与 SystemSection 已迁到 gdr.refiners.system_prompt.
# etl 阶段渲染 (qf_text) 复用同一份 partition 实现, 避免双源真值.
from gdr.refiners.system_prompt import SystemSection, partition_system_prompt  # noqa: F401


# F3-C fix: 在清洗后的 system prompt 末尾追加 Reasoning requirement 段,
# 要求 agent 每轮 assistant message 都先输出 thinking 块再 content / tool_call.
# 本仓库内仅控制 qf_text 渲染产物 (SFT 训练数据形态), 真正的源头是远端
# QwenPaw agent 配置 (AGENTS.md / SOUL.md / PROFILE.md), 仓库外需同步修改.
_REASONING_REQUIREMENT_SECTION = """# Reasoning requirement (F3-C)

Each turn of the assistant message must include a structured ``thinking`` block
**before** any ``content`` (visible text) and any ``tool_call``. The thinking
block records the reasoning that led to the decision; it is not visible to the
user but is part of the training signal and is required for the conversation
to be archived as a complete sample.

Rules:

- Emit exactly one ``thinking`` block at the start of every assistant turn.
- Do not skip thinking even when the turn is a one-line acknowledgement.
- A turn without a thinking block will be flagged as an incomplete session
  and excluded from the training dataset.
- Thinking blocks must be substantive (>= 1 sentence) and reflect the actual
  reasoning, not boilerplate.
"""


def _slugify(title: str) -> str:
    """把标题变成合法文件名."""
    s = title.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[-\s]+", "_", s)
    return s or "section"


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
    append_reasoning_requirement: bool = True,
) -> tuple[str, dict[str, int]]:
    """把保留段与 tool 模板重组成新的 system prompt.

    Args:
        sections: partition 结果.
        tools_text: 已渲染的 tool schema 文本; 为空时不插入.
        templates_dir: 若提供, 优先读取本地模板替换对应段.
        insert_tools_at: ``agent-skills`` 时插入到原 ``<agent-skills>`` 段位置;
            未找到则追加到末尾. ``end`` 时直接追加到末尾.
        append_reasoning_requirement: F3-C. 当 True (默认) 时在清洗后的
            system 末尾追加 ``Reasoning requirement`` 段, 要求 agent 每轮先
            输出 thinking. 关闭后恢复原行为, 便于对比实验. 仅影响 qf_text
            渲染产物, 不改变 QwenPaw agent 实际行为 (需仓库外同步改
            AGENTS.md / SOUL.md).

    Returns:
        (new_system_text, stats). stats 含 ``reasoning_requirement_appended``。
    """
    stats: dict[str, int] = {
        "identity_sections": 0,
        "constraint_sections": 0,
        "framework_sections": 0,
        "unknown_sections": 0,
        "reasoning_requirement_appended": 0,
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

    # F3-C fix: 末尾追加 Reasoning requirement (仅当开关开启).
    # 强制在最后一段之后追加, 不参与 insert_tools_at 的定位逻辑.
    if append_reasoning_requirement:
        rendered_parts.append(_REASONING_REQUIREMENT_SECTION)
        stats["reasoning_requirement_appended"] = 1

    # 用双换行拼接各段, 保持可读性
    return "\n\n".join(p.strip() for p in rendered_parts if p.strip()), stats
