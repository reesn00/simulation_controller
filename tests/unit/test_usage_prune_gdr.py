"""usage_prune 前移到 gdr 的单元测试 (方案 etl-prune-frontload.md §6).

覆盖:
    - gdr.refiners.system_prompt.partition_system_prompt
    - gdr.refiners.usage_prune.collect_usage / prune_system_text / prune_tools /
      generalize_local_paths / prune_session_in_place
    - 路径泛化覆盖 1/2/4 反斜杠形态 (CLAUDE.md 红线级别隐私脱敏)
    - P0-R fix: deterministic strategy 保留 unused 工具
    - Session (pydantic) 输入自动转 dict 操作后写回
    - etl.qwenformat.usage_prune 与 system_prompt 通过 import 转发到 gdr 实现
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from domain import Message, Session, TextBlock, ToolcallBlock, ToolresultBlock

from gdr.refiners.system_prompt import partition_system_prompt
from gdr.refiners.usage_prune import (
    build_path_mapping,
    collect_usage,
    generalize_local_paths,
    prune_session_in_place,
    prune_system_text,
    prune_tools,
)


# ============================================================
# Helpers
# ============================================================

def _make_session_dict(
    *,
    session_id: str = "test-session",
    tools: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "messages": messages or [
            {"role": "user", "blocks": [{"type": "text", "text": "q"}]},
            {"role": "assistant", "blocks": [
                {"type": "toolcall", "id": "tc1", "name": "search",
                 "input": "{}", "state": "finished"},
                {"type": "toolresult", "id": "tc1", "name": "search",
                 "output_text": "ok", "state": "success"},
            ]},
        ],
        "metadata": {"tools": tools or []},
    }


def _make_cfg(**overrides: Any) -> SimpleNamespace:
    defaults = dict(
        usage_prune_enabled=True,
        tools_prune_strategy="none",
        tools_prune_keep_unused_min=4,
        tools_prune_keep_unused_max=12,
        tools_prune_keep_unused_ratio=0.3,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ============================================================
# partition_system_prompt
# ============================================================

class TestPartitionSystemPrompt:
    def test_empty(self):
        assert partition_system_prompt("") == []

    def test_no_boundary_returns_unknown(self):
        secs = partition_system_prompt("just plain text with no markers")
        assert len(secs) == 1
        assert secs[0].kind == "unknown"
        assert secs[0].content == "just plain text with no markers"

    def test_identity_section(self):
        text = "# Agent Identity\n\nI am a test agent."
        secs = partition_system_prompt(text)
        assert any(s.kind == "identity" and s.title == "Agent Identity" for s in secs)

    def test_framework_section_detected(self):
        text = "# Agent Identity\n\ntest\n\n# AGENTS.md\n\nframework body"
        secs = partition_system_prompt(text)
        titles = [(s.kind, s.title) for s in secs]
        assert ("identity", "Agent Identity") in titles
        assert ("framework", "AGENTS.md") in titles

    def test_agent_skills_block(self):
        text = "# Agent Identity\n\nx\n\n<agent-skills>\n<skill><name>a</name></skill>\n</agent-skills>"
        secs = partition_system_prompt(text)
        titles = [(s.kind, s.title) for s in secs]
        assert ("constraint", "agent-skills") in titles


# ============================================================
# collect_usage
# ============================================================

class TestCollectUsage:
    def test_called_tools(self):
        session = _make_session_dict()
        usage = collect_usage(session)
        assert "search" in usage["called_tools"]

    def test_called_skills(self):
        session = _make_session_dict(messages=[
            {"role": "assistant", "blocks": [
                {"type": "toolcall", "id": "tc1", "name": "Skill",
                 "input": '{"skill": "weather"}', "state": "finished"},
                {"type": "toolresult", "id": "tc1", "name": "Skill",
                 "output_text": "ok", "state": "success"},
            ]},
        ])
        usage = collect_usage(session)
        assert "weather" in usage["called_skills"]

    def test_headline_detection(self):
        session = _make_session_dict(messages=[
            {"role": "assistant", "blocks": [
                {"type": "text", "text": "result is ⟦done⟧"},
            ]},
        ])
        usage = collect_usage(session)
        assert usage["has_headline"] is True

    def test_empty_session(self):
        usage = collect_usage({"messages": []})
        assert usage["called_tools"] == set()
        assert usage["called_skills"] == set()
        assert usage["has_headline"] is False


# ============================================================
# prune_system_text
# ============================================================

class TestPruneSystemText:
    def test_drop_framework(self):
        text = "# Agent Identity\n\ntest\n\n# AGENTS.md\n\nframework body"
        usage = collect_usage({})
        new_text, stats = prune_system_text(text, usage)
        assert "framework body" not in new_text
        assert "AGENTS.md" in stats["dropped_sections"]

    def test_keep_identity(self):
        text = "# Agent Identity\n\ntest identity"
        new_text, stats = prune_system_text(text, collect_usage({}))
        assert "test identity" in new_text
        assert "Agent Identity" not in stats["dropped_sections"]

    def test_the_map_only_with_recall(self):
        text = "# Agent Identity\n\ntest\n\n地图（THE MAP）\n\nmap body"
        usage_no_recall = {"called_tools": set(), "called_skills": set(), "has_headline": False}
        new_text, _ = prune_system_text(text, usage_no_recall)
        assert "map body" not in new_text
        usage_recall = {"called_tools": {"recall_history"}, "called_skills": set(), "has_headline": False}
        new_text2, _ = prune_system_text(text, usage_recall)
        assert "map body" in new_text2

    def test_empty_returns_empty(self):
        new_text, stats = prune_system_text("", collect_usage({}))
        assert new_text == ""
        assert stats["dropped_sections"] == []


# ============================================================
# prune_tools
# ============================================================

class TestPruneTools:
    def test_preserve_called_order(self):
        tools = [
            {"type": "function", "function": {"name": "a"}},
            {"type": "function", "function": {"name": "b"}},
            {"type": "function", "function": {"name": "c"}},
        ]
        called = {"b"}
        pruned, dropped, audit = prune_tools(
            tools, called, strategy="none",
            keep_unused_min=0, keep_unused_max=0,
        )
        names = [t["function"]["name"] for t in pruned]
        assert names[0] == "b"  # called 在前段

    def test_missing_called_gets_minimal_schema(self):
        tools = []
        called = {"missing_tool"}
        pruned, _, _ = prune_tools(tools, called, strategy="none")
        assert any(t["function"]["name"] == "missing_tool" for t in pruned)

    def test_p0r_deterministic_keeps_unused(self):
        tools = [{"type": "function", "function": {"name": f"tool_{i}"}} for i in range(20)]
        called = {"tool_0"}
        pruned, dropped, audit = prune_tools(
            tools, called, session_id="sess-1",
            keep_unused_min=4, keep_unused_max=8,
            keep_unused_ratio=0.3, strategy="deterministic",
        )
        # called (1) + unused (min(19*0.3, 8) = 5, max(4, 5) = 5) = 6
        assert len(pruned) == 6
        assert audit["strategy"] == "deterministic"
        assert audit["kept_unused_count"] == 5

    def test_p0r_deterministic_same_session_same_kept(self):
        """同 session_id 的 P0-R 结果稳定 (按种子)."""
        tools = [{"type": "function", "function": {"name": f"tool_{i}"}} for i in range(20)]
        called = {"tool_0"}
        _, _, audit1 = prune_tools(
            tools, called, session_id="sess-stable",
            keep_unused_min=4, keep_unused_max=12,
            keep_unused_ratio=0.5, strategy="deterministic",
        )
        _, _, audit2 = prune_tools(
            tools, called, session_id="sess-stable",
            keep_unused_min=4, keep_unused_max=12,
            keep_unused_ratio=0.5, strategy="deterministic",
        )
        assert audit1["kept_unused"] == audit2["kept_unused"]

    def test_strategy_none_drops_all_unused(self):
        tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(5)]
        called = {"t0"}
        pruned, dropped, audit = prune_tools(
            tools, called, strategy="none",
            keep_unused_min=4, keep_unused_max=12,
        )
        assert len(pruned) == 1  # only called
        assert audit["kept_unused"] == []


# ============================================================
# generalize_local_paths (CLAUDE.md 红线)
# ============================================================

class TestGeneralizeLocalPaths:
    def test_no_windows_root_unchanged(self):
        session = {"messages": [{"role": "assistant", "blocks": [
            {"type": "text", "text": "no path here"},
        ]}]}
        mapping = generalize_local_paths(session)
        assert mapping == {}

    def test_single_backslash_root_generalized(self):
        session = {"session_id": "x", "messages": [{"role": "assistant", "blocks": [
            {"type": "toolresult", "output_text": "C:\\Users\\original\\file.txt",
             "id": "tc1", "name": "shell", "state": "success"},
        ]}]}
        mapping = generalize_local_paths(session)
        assert "C:\\Users\\original" in mapping
        assert "original" not in session["messages"][0]["blocks"][0]["output_text"]

    def test_double_backslash_json_embedded(self):
        """JSON 内嵌双反斜杠形态 (toolresult.output_text 本身是 JSON 字符串)."""
        session = {"session_id": "x", "messages": [{"role": "assistant", "blocks": [
            {"type": "toolresult", "output_text": "C:\\\\Users\\\\original\\\\file.txt",
             "id": "tc1", "name": "shell", "state": "success"},
        ]}]}
        mapping = generalize_local_paths(session)
        assert len(mapping) >= 1
        # 所有原始 root 形态都被替换
        out = session["messages"][0]["blocks"][0]["output_text"]
        assert "original" not in out

    def test_workspace_default_generalized(self):
        session = {"session_id": "x", "messages": [{"role": "assistant", "blocks": [
            {"type": "toolresult", "output_text": "C:\\Users\\foo\\.qwenpaw\\workspaces\\default\\x",
             "id": "tc1", "name": "shell", "state": "success"},
        ]}]}
        generalize_local_paths(session)
        out = session["messages"][0]["blocks"][0]["output_text"]
        assert "default" not in out
        assert ".qwenpaw\\workspaces\\" in out  # workspaces 保留

    def test_path_new_roots_in_stats(self):
        session = {"session_id": "x", "messages": [{"role": "assistant", "blocks": [
            {"type": "toolresult", "output_text": "C:\\Users\\foo\\a.txt",
             "id": "tc1", "name": "shell", "state": "success"},
        ]}]}
        # 通过 prune_session_in_place 间接验证 stats 写入
        stats = prune_session_in_place(session, _make_cfg())
        assert "path_new_roots" in stats
        assert len(stats["path_new_roots"]) >= 1


# ============================================================
# build_path_mapping (helper)
# ============================================================

class TestBuildPathMapping:
    def test_same_canon_maps_to_same_user(self):
        """同一逻辑路径的不同反斜杠形态映射到同一用户名."""
        mapping = build_path_mapping("sess", [
            "C:\\Users\\alice",
            "C:\\\\Users\\\\alice",  # 双反斜杠
            "C:\\\\\\\\Users\\\\\\\\alice",  # 四反斜杠
        ])
        users = {v.split("\\")[-1] for v in mapping.values()}
        assert len(users) == 1  # 同一逻辑路径只产生一个用户名

    def test_different_users_different_canon(self):
        mapping = build_path_mapping("sess", ["C:\\Users\\alice", "C:\\Users\\bob"])
        assert mapping["C:\\Users\\alice"] != mapping["C:\\Users\\bob"]


# ============================================================
# prune_session_in_place (主入口)
# ============================================================

class TestPruneSessionInPlace:
    def test_disabled_skips(self):
        session = _make_session_dict()
        stats = prune_session_in_place(session, _make_cfg(usage_prune_enabled=False))
        assert stats.get("skipped") is True
        assert "usage_prune" not in session["metadata"]

    def test_writes_metadata_usage_prune(self):
        session = _make_session_dict()
        stats = prune_session_in_place(session, _make_cfg())
        assert "usage_prune" in session["metadata"]
        # 默认 session metadata.tools=[] 但 called_tools={'search'},
        # prune_tools 会补 called tool 最小 schema → tools_after >= 1
        assert stats["tools_after"] >= 1
        assert stats["tools_before"] == 0
        assert "dropped_sections" in stats
        assert "path_new_roots" in stats

    def test_pydantic_session_writes_back(self):
        """Session (pydantic) 输入自动转 dict 操作后写回."""
        from domain import Message, TextBlock, ToolcallBlock, ToolresultBlock
        messages = [
            Message(role="user", id="u1", blocks=[TextBlock(type="text", id="t1", text="q")]),
            Message(role="assistant", id="a1", blocks=[
                ToolcallBlock(type="toolcall", id="tc1", name="search",
                              input="{}", state="finished"),
                ToolresultBlock(type="toolresult", id="tc1", name="search",
                                output_text="ok", state="success"),
            ]),
        ]
        session = Session(session_id="pyd-test", messages=messages)
        stats = prune_session_in_place(session, _make_cfg())
        assert isinstance(session.metadata, dict)
        assert "usage_prune" in session.metadata

    def test_called_tools_consistency_assertion(self):
        """被调工具必须出现在 pruned_tools 中 (否则 assert 失败)."""
        tools = [{"type": "function", "function": {"name": "real_tool"}}]
        session = _make_session_dict(tools=tools)
        # session 调用的 'search' 不在 tools 中, 但 prune_tools 会补最小 schema
        # 所以 assert 不应失败
        stats = prune_session_in_place(session, _make_cfg())
        new_tools = session["metadata"]["tools"]
        assert any(t.get("function", {}).get("name") == "search" for t in new_tools)


# ============================================================
# etl 兼容性 (etl.qwenformat.usage_prune 通过转发使用 gdr 实现)
# ============================================================

class TestEtlCompat:
    """验证 etl.qwenformat.usage_prune 与 system_prompt 通过 import 转发到 gdr."""

    def test_etl_partition_imports_from_gdr(self):
        from etl.qwenformat import system_prompt
        assert system_prompt.partition_system_prompt.__module__ == "gdr.refiners.system_prompt"
        assert system_prompt.SystemSection.__module__ == "gdr.refiners.system_prompt"

    def test_etl_submodules_import_from_gdr(self):
        from etl.qwenformat import usage_prune
        assert usage_prune.collect_usage.__module__ == "gdr.refiners.usage_prune"
        assert usage_prune.prune_system_text.__module__ == "gdr.refiners.usage_prune"
        assert usage_prune.prune_tools.__module__ == "gdr.refiners.usage_prune"
        assert usage_prune.generalize_local_paths.__module__ == "gdr.refiners.usage_prune"

    def test_etl_io_functions_preserved(self):
        """load_refined_session / write_refined_session 仍在 etl 包 (4 视图 I/O)."""
        from etl.qwenformat import usage_prune
        assert hasattr(usage_prune, "load_refined_session")
        assert hasattr(usage_prune, "write_refined_session")

    def test_etl_render_functions_preserved(self):
        """etl 的 qf_text 渲染能力 (render_cleaned_system) 保留."""
        from etl.qwenformat import system_prompt
        assert hasattr(system_prompt, "render_cleaned_system")
        assert hasattr(system_prompt, "save_section_templates")
