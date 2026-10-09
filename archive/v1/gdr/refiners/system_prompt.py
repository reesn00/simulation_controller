"""gdr.refiners.system_prompt: system prompt 段级拆解.

从 etl.qwenformat.system_prompt 迁移 (方案 etl-prune-frontload.md §2.1).
gdr 阶段按真实调用裁剪 system prompt 的前置依赖; etl 阶段渲染 qf_text 时
复用同一份 partition 实现, 通过 ``etl.qwenformat.system_prompt`` 转发
import 即可.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


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
    # F3-D: RETRIEVAL HEADLINE 段已下线. 历史 qf_out 仍可能含此段, 通过
    # partition 后归为 unknown (不在 _SYSTEM_BOUNDARIES 里匹配), 由后续
    # usage_prune 与 retention filter 决定是否保留.
    (r"^地图（THE MAP）", "constraint", "THE MAP"),
    (r"^纪律（DISCIPLINE）", "constraint", "DISCIPLINE"),
    (r"^={10,}", "framework", "Framework Info"),
    (r"^<agent-skills>", "constraint", "agent-skills"),
    (r"^# 长期记忆\s*$", "constraint", "长期记忆"),
]

_AGENT_SKILLS_END = "</agent-skills>"


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
