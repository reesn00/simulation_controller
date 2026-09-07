"""etl.qwenformat.system_prompt 与 tool_templates 单元测试."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etl.qwenformat.system_prompt import (
    partition_system_prompt,
    render_cleaned_system,
    save_section_templates,
    section_template_path,
)
from etl.qwenformat.tool_templates import (
    render_tool_definition,
    render_tools_section,
    save_tool_templates,
)


def _sample_system_prompt() -> str:
    return (
        "# Agent Identity\n\nYour agent id is `default`.\n\n"
        "# AGENTS.md\n\n## 安全\n- 不要泄露数据.\n\n"
        "# SOUL.md\n\n真心帮忙.\n\n"
        "# PROFILE.md\n\n## 身份\n- 名字\n\n"
        "You can only understand text content, so reading an image will not help.\n\n"
        "### Directories\n\nWorking directory: /tmp\n\n"
        "你的对话会被持久记录.\n\n"
        "检索标题（RETRIEVAL HEADLINE）。每个回复都必须追加 headline。\n\n"
        "地图（THE MAP）。一旦上下文被压缩...\n\n"
        "纪律（DISCIPLINE）：\n- recall 是真相来源.\n\n"
        "====================\n- About: QwenPaw\n- OS: Windows\n====================\n\n"
        "<agent-skills>\n<skill><name>browser</name></skill>\n</agent-skills>\n\n"
        "# 长期记忆\n\n- `MEMORY.md` 是核心记忆.\n"
    )


def test_partition_classifies_framework_sections():
    sections = partition_system_prompt(_sample_system_prompt())
    by_title = {s.title: s for s in sections}

    assert by_title["Agent Identity"].kind == "identity"
    assert by_title["AGENTS.md"].kind == "framework"
    assert by_title["SOUL.md"].kind == "framework"
    assert by_title["PROFILE.md"].kind == "framework"
    assert by_title["agent-skills"].kind == "constraint"
    assert by_title["Framework Info"].kind == "framework"


def test_partition_classifies_constraint_sections():
    sections = partition_system_prompt(_sample_system_prompt())
    by_title = {s.title: s for s in sections}

    assert by_title["Image Understanding"].kind == "constraint"
    assert by_title["Directories"].kind == "constraint"
    assert by_title["Conversation Persistence"].kind == "constraint"
    assert by_title["RETRIEVAL HEADLINE"].kind == "constraint"
    assert by_title["THE MAP"].kind == "constraint"
    assert by_title["DISCIPLINE"].kind == "constraint"
    assert by_title["长期记忆"].kind == "constraint"


def test_render_cleaned_system_removes_framework_sections():
    sections = partition_system_prompt(_sample_system_prompt())
    new_system, stats = render_cleaned_system(sections)

    assert "# AGENTS.md" not in new_system
    assert "# SOUL.md" not in new_system
    assert "# PROFILE.md" not in new_system
    assert "About: QwenPaw" not in new_system

    assert "Agent Identity" in new_system
    assert "Your agent id is `default`" in new_system
    assert "<agent-skills>" in new_system
    assert "RETRIEVAL HEADLINE" in new_system
    assert "长期记忆" in new_system

    assert stats["framework_sections"] == 5
    assert stats["identity_sections"] == 1
    assert stats["constraint_sections"] == 8


def test_render_cleaned_system_keeps_original_order():
    sections = partition_system_prompt(_sample_system_prompt())
    new_system, _ = render_cleaned_system(sections)

    identity_pos = new_system.index("Agent Identity")
    image_pos = new_system.index("You can only understand")
    dirs_pos = new_system.index("Directories")
    headline_pos = new_system.index("RETRIEVAL HEADLINE")
    memory_pos = new_system.index("长期记忆")

    assert identity_pos < image_pos < dirs_pos < headline_pos < memory_pos


def test_save_section_templates_creates_files(tmp_path: Path):
    sections = partition_system_prompt(_sample_system_prompt())
    saved = save_section_templates(sections, tmp_path, update_existing=True)

    assert any(p.name == "identity.txt" and p.parent.name == "role" for p in saved)
    assert any(p.name == "image_understanding.txt" and p.parent.name == "constraints" for p in saved)
    assert any(p.name == "retrieval_headline.txt" and p.parent.name == "constraints" for p in saved)
    assert any(p.name == "discipline.txt" and p.parent.name == "constraints" for p in saved)

    identity_path = section_template_path(tmp_path, sections[0])
    assert identity_path.exists()
    content = identity_path.read_text(encoding="utf-8")
    assert "Agent Identity" in content


def test_save_section_templates_does_not_overwrite_when_update_false(tmp_path: Path):
    sections = partition_system_prompt(_sample_system_prompt())
    save_section_templates(sections, tmp_path, update_existing=True)

    path = section_template_path(tmp_path, sections[0])
    path.write_text("human edited", encoding="utf-8")

    stats: dict[str, int] = {}
    save_section_templates(sections, tmp_path, update_existing=False, stats=stats)
    assert path.read_text(encoding="utf-8") == "human edited"
    assert stats.get("template_unchanged") == len([s for s in sections if s.kind != "framework"])


def test_render_tool_definition_includes_schema():
    tool = {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索网页",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
    }
    text = render_tool_definition(tool)
    assert "Function: web_search" in text
    assert "Description: 搜索网页" in text
    assert '"q"' in text


def test_render_tools_section_dedup_and_order():
    tools = [
        {"type": "function", "function": {"name": "alpha"}},
        {"type": "function", "function": {"name": "beta"}},
        {"type": "function", "function": {"name": "alpha"}},
    ]
    text = render_tools_section(tools)
    lines = [ln for ln in text.splitlines() if ln.startswith("Function:")]
    assert lines == ["Function: alpha", "Function: beta"]


def test_save_tool_templates_creates_files(tmp_path: Path):
    tools = [
        {"type": "function", "function": {"name": "web_search", "description": "搜索"}},
        {"type": "function", "function": {"name": "Skill", "description": "技能"}},
    ]
    paths = save_tool_templates(tools, tmp_path)
    assert (tmp_path / "tools" / "web_search.txt").exists()
    assert (tmp_path / "tools" / "Skill.txt").exists()
    assert "web_search" in (tmp_path / "tools" / "web_search.txt").read_text(encoding="utf-8")


def test_partition_from_real_trajectory():
    """用仓库中真实 trajectory 验证 partition 不抛错且能识别主要段."""
    fp = Path("output/agent_trajectory/run_1359b724f9c84661b7267dc7d443e017"
              "__useramulation-a3a43ff775d84e62b4a84b1ef7dfb928.json")
    if not fp.exists():
        pytest.skip("真实 trajectory 不存在")

    with fp.open(encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            if obj.get("event_type") == "model_request":
                msgs = obj["payload"]["messages"]
                sys = next((m for m in msgs if m.get("name") == "system"), None)
                text = sys["content"][0]["text"]
                break
        else:
            pytest.fail("未找到 model_request")

    sections = partition_system_prompt(text)
    titles = [s.title for s in sections]
    kinds = {s.title: s.kind for s in sections}

    assert "Agent Identity" in titles
    assert kinds["AGENTS.md"] == "framework"
    assert kinds["SOUL.md"] == "framework"
    assert kinds["PROFILE.md"] == "framework"
    assert kinds["agent-skills"] == "constraint"
    assert kinds["Framework Info"] == "framework"
    assert kinds["RETRIEVAL HEADLINE"] == "constraint"
    assert kinds["THE MAP"] == "constraint"
    assert kinds["DISCIPLINE"] == "constraint"
    assert kinds["长期记忆"] == "constraint"

    new_system, stats = render_cleaned_system(sections)
    assert "# AGENTS.md" not in new_system
    assert "# SOUL.md" not in new_system
    assert "# PROFILE.md" not in new_system
    assert "Agent Identity" in new_system
    assert "RETRIEVAL HEADLINE" in new_system
    assert stats["framework_sections"] > 0
