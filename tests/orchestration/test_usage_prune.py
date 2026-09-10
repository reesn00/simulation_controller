"""etl.qwenformat.usage_prune 单元测试.

覆盖: 使用采集 / system 段级按调用删留 / skills 条目裁剪 / tools 裁剪 /
本机路径泛化 (转义形态归一、确定性、跨 session 多样性) / 主入口一致性断言.
"""

from __future__ import annotations

import json

import pytest

from etl.qwenformat.transform import build_chat_env, load_chat_template
from etl.qwenformat.usage_prune import (
    build_path_mapping,
    collect_usage,
    generalize_local_paths,
    load_refined_session,
    prune_session_in_place,
    prune_system_text,
    prune_tools,
    write_refined_session,
)

SYSTEM_TEMPLATE = """# Agent Identity

Your agent id is `default`.

### Directories

Working directory: C:\\Users\\tester\\.qwenpaw\\workspaces\\default

你的对话会被持久记录，即使较早的轮次滚出当前上下文也不会丢。

检索标题（RETRIEVAL HEADLINE）。最终回复必须追加 headline。

地图（THE MAP）。上下文压缩后你会看到索引。

纪律（DISCIPLINE）：recall 是真相来源。

<agent-skills>
Skills are a collection of instructions.

<skill>
<name>browser</name>
<description>Drive a live browser.</description>
<dir>C:\\Users\\tester\\.qwenpaw\\workspaces\\default\\skills\\browser</dir>
</skill>
<skill>
<name>pdf</name>
<description>PDF files.</description>
<dir>C:\\Users\\tester\\.qwenpaw\\workspaces\\default\\skills\\pdf</dir>
</skill>
</agent-skills>

# 长期记忆

- `MEMORY.md` 是你的核心长期记忆。
"""


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}


def _session(*, skill_call: bool = True, headline: bool = True) -> dict:
    blocks = [{"type": "text", "id": "t1", "text": "做点什么"}]
    if skill_call:
        blocks.append({"type": "toolcall", "id": "tc1", "name": "Skill", "input": json.dumps({"skill": "browser"})})
        blocks.append({"type": "toolresult", "id": "tr1", "name": "Skill", "output_text": "ok", "state": "success"})
    blocks.append({"type": "toolcall", "id": "tc2", "name": "web_search", "input": "{}"})
    blocks.append({"type": "toolresult", "id": "tr2", "name": "web_search", "output_text": "ok", "state": "success"})
    final = "结论。\n\n⟦ 任务｜已完成 ⟧" if headline else "结论。"
    return {
        "session_id": "sess-x",
        "summary": SYSTEM_TEMPLATE,
        "messages": [
            {"role": "system", "name": "system", "id": "s0",
             "blocks": [{"type": "text", "id": "s0b", "text": SYSTEM_TEMPLATE}]},
            {"role": "user", "name": "user", "id": "u1", "blocks": blocks[:1]},
            {"role": "assistant", "name": "assistant", "id": "a1", "blocks": blocks[1:]},
            {"role": "assistant", "name": "assistant", "id": "a2",
             "blocks": [{"type": "text", "id": "a2b", "text": final}]},
        ],
        "metadata": {"tools": [_tool("Skill"), _tool("browser"), _tool("web_search"),
                               _tool("recall_history"), _tool("memory_search")]},
    }


def test_collect_usage():
    usage = collect_usage(_session())
    assert usage["called_tools"] == {"Skill", "web_search"}
    assert usage["called_skills"] == {"browser"}
    assert usage["has_headline"] is True


def test_prune_system_drops_unused_sections():
    usage = {"called_tools": {"web_search"}, "called_skills": set(), "has_headline": False}
    new, stats = prune_system_text(SYSTEM_TEMPLATE, usage)
    assert "RETRIEVAL HEADLINE" not in new
    assert "THE MAP" not in new
    assert "长期记忆" not in new
    assert "agent-skills" not in new
    assert "Agent Identity" in new  # 始终保留
    assert set(stats["dropped_skills"]) == {"browser", "pdf"}


def test_prune_system_keeps_used_verbatim():
    usage = {"called_tools": {"Skill", "recall_history"}, "called_skills": {"browser"},
             "has_headline": True}
    new, stats = prune_system_text(SYSTEM_TEMPLATE, usage)
    assert "RETRIEVAL HEADLINE" in new  # 用到 → 原样保留
    assert "THE MAP" in new            # recall_history 用到 → 保留
    assert "长期记忆" not in new        # memory_search 未用 → 删
    assert "<name>browser</name>" in new
    assert "<name>pdf</name>" not in new
    assert "<dir>" not in new          # 保留条目也去掉本机路径行
    assert stats["kept_skills"] == ["browser"]


def test_prune_tools():
    tools = [_tool("web_search"), _tool("recall_history")]
    pruned, dropped = prune_tools(tools, {"web_search", "mystery"})
    names = [t["function"]["name"] for t in pruned]
    assert names == ["web_search", "mystery"]  # 缺失的被调工具补最小 schema
    assert dropped == ["recall_history"]


def test_path_mapping_deterministic_and_diverse():
    roots = ["C:\\Users\\klpc", "C:\\\\Users\\\\klpc"]  # 正常 + JSON 内嵌形态
    m1 = build_path_mapping("sess-a", roots)
    m2 = build_path_mapping("sess-a", roots)
    assert m1 == m2  # 确定性
    # 同一逻辑路径的两种转义形态映射到同一用户名, 各自保持分隔符
    assert m1[roots[0]].split("\\")[-1] == m1[roots[1]].split("\\")[-1]
    assert "\\\\" in m1[roots[1]] and "\\\\" not in m1[roots[0]]
    others = {build_path_mapping(f"sess-{i}", roots)[roots[0]] for i in range(20)}
    assert len(others) > 1  # 跨 session 多样


def test_generalize_local_paths_nested_json_forms():
    session = {
        "session_id": "sess-path",
        "messages": [{"role": "assistant", "blocks": [
            {"type": "toolresult", "name": "browser",
             "output_text": json.dumps([{"text": "shot: C:\\Users\\klpc\\.qwenpaw\\workspaces\\default\\a.png"}])},
        ]}],
        "metadata": {},
    }
    mapping = generalize_local_paths(session)
    assert mapping
    raw = json.dumps(session, ensure_ascii=False)
    assert "klpc" not in raw  # 嵌套 JSON 形态也被替换


def test_prune_session_in_place_consistency():
    env = build_chat_env()
    template = load_chat_template("etl/qwenformat/chat_template.jinja")
    session = _session(skill_call=True, headline=True)
    stats = prune_session_in_place(session, template, env)
    md = session["metadata"]
    sys_text = session["messages"][0]["blocks"][0]["text"]
    assert session["summary"] == sys_text
    assert md["openai_messages"][0]["content"] == sys_text
    assert sys_text[:100] in md["qf_text"]
    assert {t["function"]["name"] for t in md["tools"]} == {"Skill", "web_search"}
    assert "RETRIEVAL HEADLINE" in sys_text
    assert stats["tools_before"] == 5 and stats["tools_after"] == 2


def test_prune_session_headline_marker_without_section_fails():
    env = build_chat_env()
    template = load_chat_template("etl/qwenformat/chat_template.jinja")
    session = _session(skill_call=False, headline=True)
    # 人为制造不一致: 文本有 ⟦⟧ 但 system 里没有 headline 段
    broken = SYSTEM_TEMPLATE.replace(
        "检索标题（RETRIEVAL HEADLINE）。最终回复必须追加 headline。", "")
    session["messages"][0]["blocks"][0]["text"] = broken
    session["summary"] = broken
    with pytest.raises(AssertionError):
        prune_session_in_place(session, template, env)


def test_split_format_round_trip(tmp_path):
    """新拆分四视图形态: 载入 → 裁剪 → 写回, 4 份文件同步更新."""
    env = build_chat_env()
    template = load_chat_template("etl/qwenformat/chat_template.jinja")
    session = _session(skill_call=True, headline=True)
    prune_session_in_place(session, template, env)

    base = tmp_path / "s1_refined"
    # 模拟 gdr save_session 的拆分落盘
    (tmp_path / "s1_refined.messages.json").write_text(
        json.dumps({"messages": session["messages"]}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "s1_refined.openai.json").write_text(
        json.dumps({"openai_messages": session["metadata"]["openai_messages"]},
                   ensure_ascii=False), encoding="utf-8")
    (tmp_path / "s1_refined.qwenjina.txt").write_text(
        session["metadata"]["qf_text"], encoding="utf-8")
    meta = dict(session["metadata"])
    meta["session_id"] = session["session_id"]
    (tmp_path / "s1_refined.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # 载入 → 再裁剪(幂等) → 写回
    loaded = load_refined_session(tmp_path / "s1_refined.messages.json")
    assert loaded["session_id"] == "sess-x"
    stats = prune_session_in_place(loaded, template, env)
    assert stats["system_chars_before"] == stats["system_chars_after"]  # 幂等
    write_refined_session(loaded, tmp_path / "s1_refined.messages.json")

    # 四份文件视图一致
    msgs = json.loads((tmp_path / "s1_refined.messages.json").read_text(encoding="utf-8"))
    om = json.loads((tmp_path / "s1_refined.openai.json").read_text(encoding="utf-8"))
    meta2 = json.loads((tmp_path / "s1_refined.meta.json").read_text(encoding="utf-8"))
    qf = (tmp_path / "s1_refined.qwenjina.txt").read_text(encoding="utf-8")
    sys_text = msgs["messages"][0]["blocks"][0]["text"]
    assert om["openai_messages"][0]["content"] == sys_text
    assert meta2["openai_messages"][0]["content"] == sys_text
    assert meta2["qf_text"] == qf
    assert meta2["session_id"] == "sess-x"
    assert "usage_prune" in meta2


def test_load_rejects_single_file_form(tmp_path):
    """旧单文件形态不再兼容 (MVP 不考虑兼容)."""
    p = tmp_path / "s1_refined.json"
    p.write_text(json.dumps(_session(), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="messages.json"):
        load_refined_session(p)
    with pytest.raises(ValueError, match="messages.json"):
        write_refined_session(_session(), p)
