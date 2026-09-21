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
    """旧行为兼容: 默认参数 (strategy='none', min/max=0, ratio=0)
    应等价历史 (仅保留 called + 缺失补 schema)."""
    tools = [_tool("web_search"), _tool("recall_history")]
    pruned, dropped, audit = prune_tools(tools, {"web_search", "mystery"})
    names = [t["function"]["name"] for t in pruned]
    assert names == ["web_search", "mystery"]  # 缺失的被调工具补最小 schema
    assert dropped == ["recall_history"]
    assert audit["strategy"] == "none"
    assert audit["kept_unused"] == []


# ---------------------------------------------------------------------------
# P0-R fix: 随机保留 unused 工具作为 SFT 噪声
# ---------------------------------------------------------------------------


class TestPruneToolsUnusedNoise:
    """P0-R: 真实部署中 agent 面对完整工具菜单, SFT 训练样本若只展示被调工具
    会让模型学到"工具列表短 = 该调用" 的错误相关. 该测试组覆盖:

    - called 永远 100% 在结果里 (toolcall 必有 schema);
    - 保留 unused 数量受 min/max/ratio 联合控制;
    - 同 session_id 同 kept_unused (确定性);
    - 不同 session_id 跨样本多样 (随机种子多样性);
    - strategy="none" 回滚逃生口;
    - 顺序与 audit 字段稳定.
    """

    @staticmethod
    def _pool(n: int = 27) -> list[dict]:
        # 模拟真实 27 工具场景: web_search 是必调用, 其它 26 个 unused
        names = ["web_search"] + [f"tool_{i}" for i in range(n - 1)]
        return [_tool(name) for name in names]

    def test_called_tools_strict_subset_of_result(self):
        """called 永远在结果里, 不管 strategy 与采样."""
        tools = self._pool(27)
        called = {"web_search"}
        for strategy in ("none", "deterministic"):
            pruned, dropped, audit = prune_tools(
                tools, called,
                session_id="sess-1",
                keep_unused_min=4, keep_unused_max=12,
                keep_unused_ratio=0.3, strategy=strategy,
            )
            names = {t["function"]["name"] for t in pruned}
            assert "web_search" in names, f"strategy={strategy} lost called tool"
            # called 必须在 dropped 之外
            assert "web_search" not in dropped

    def test_keeps_unused_count_within_min_max_bounds(self):
        """保留数量 ∈ [min, min(len(unused), max)].  ratio 上限与 max 取 min."""
        tools = self._pool(27)  # unused = 26
        called = {"web_search"}
        pruned, dropped, audit = prune_tools(
            tools, called,
            session_id="sess-bounds",
            keep_unused_min=4, keep_unused_max=12,
            keep_unused_ratio=0.3, strategy="deterministic",
        )
        kept_unused = audit["kept_unused"]
        # ratio 0.3 * 26 = 7.8 → 取 8, 受 max=12 限制 → 8
        # 受 min=4 限制 → max(4, 8) = 8
        assert 4 <= len(kept_unused) <= 12
        assert len(kept_unused) == int(26 * 0.3)  # 7 → 7 (floor)

    def test_min_zero_drops_all_unused(self):
        """min=0 / max=0 / ratio=0 → 仅保留 called (旧行为子集)."""
        tools = self._pool(27)
        called = {"web_search"}
        pruned, dropped, audit = prune_tools(
            tools, called,
            session_id="sess-zero",
            keep_unused_min=0, keep_unused_max=0,
            keep_unused_ratio=0.0, strategy="deterministic",
        )
        names = {t["function"]["name"] for t in pruned}
        assert names == {"web_search"}
        assert audit["kept_unused"] == []

    def test_max_caps_pool_size(self):
        """unused 总数 < max 时全保留; called 不重复计入."""
        # 4 个工具: 1 called + 3 unused. max=10 → 全保
        tools = [_tool("a"), _tool("b"), _tool("c"), _tool("d")]
        pruned, dropped, audit = prune_tools(
            tools, {"a"},
            session_id="sess-cap",
            keep_unused_min=0, keep_unused_max=10,
            keep_unused_ratio=1.0, strategy="deterministic",
        )
        names = {t["function"]["name"] for t in pruned}
        assert names == {"a", "b", "c", "d"}
        assert set(audit["kept_unused"]) == {"b", "c", "d"}

    def test_strategy_none_legacy_behavior(self):
        """strategy='none' 完全等价旧行为: 只保留 called + 缺失补 schema."""
        tools = self._pool(27)
        called = {"web_search", "mystery"}
        pruned, dropped, audit = prune_tools(
            tools, called,
            session_id="sess-legacy",
            keep_unused_min=4, keep_unused_max=12,
            keep_unused_ratio=0.3, strategy="none",
        )
        names = {t["function"]["name"] for t in pruned}
        # called 全保 + 不保留任何 unused
        assert names == {"web_search", "mystery"}
        assert audit["kept_unused"] == []
        # 未用工具全部进入 dropped
        assert len(dropped) == 26

    def test_deterministic_same_session_id(self):
        """同 session_id 跑两次, kept_unused 集合完全一致."""
        tools = self._pool(27)
        called = {"web_search"}
        kwargs = dict(
            session_id="sess-determin",
            keep_unused_min=4, keep_unused_max=12,
            keep_unused_ratio=0.3, strategy="deterministic",
        )
        _, _, audit1 = prune_tools(tools, called, **kwargs)
        _, _, audit2 = prune_tools(tools, called, **kwargs)
        assert audit1["kept_unused"] == audit2["kept_unused"]

    def test_diverse_across_sessions(self):
        """50 个不同 session_id, kept_unused 集合基数 > 10 (多样性指标)."""
        tools = self._pool(27)
        called = {"web_search"}
        kept_sets = set()
        for i in range(50):
            _, _, audit = prune_tools(
                tools, called,
                session_id=f"sess-{i:03d}",
                keep_unused_min=4, keep_unused_max=12,
                keep_unused_ratio=0.3, strategy="deterministic",
            )
            kept_sets.add(tuple(sorted(audit["kept_unused"])))
        assert len(kept_sets) > 10, f"多样性不足: 只得到 {len(kept_sets)} 种组合"

    def test_preserves_called_order_with_appended_unused(self):
        """结果顺序: called 在前段按原 tools 顺序, kept_unused 追加在后段."""
        tools = [_tool("a"), _tool("b"), _tool("c"), _tool("d"), _tool("e")]
        # called 是 a 和 c (按 tools 中顺序); unused 是 b/d/e
        pruned, _, audit = prune_tools(
            tools, {"a", "c"},
            session_id="sess-order",
            keep_unused_min=1, keep_unused_max=2,
            keep_unused_ratio=0.5, strategy="deterministic",
        )
        names = [t["function"]["name"] for t in pruned]
        # called 部分顺序: a, c (按原 tools 顺序)
        called_in_result = [n for n in names if n in {"a", "c"}]
        assert called_in_result == ["a", "c"]
        # unused 部分按原 tools 顺序保留 (b/d/e 中按采样)
        for n in audit["kept_unused"]:
            assert n in {"b", "d", "e"}

    def test_audit_fields_complete(self):
        """audit 字段齐全: strategy / kept_unused / kept_unused_count / sampled_from_pool_size."""
        tools = self._pool(27)
        called = {"web_search"}
        _, _, audit = prune_tools(
            tools, called,
            session_id="sess-audit",
            keep_unused_min=4, keep_unused_max=12,
            keep_unused_ratio=0.3, strategy="deterministic",
        )
        assert audit["strategy"] == "deterministic"
        assert isinstance(audit["kept_unused"], list)
        assert audit["kept_unused_count"] == len(audit["kept_unused"])
        assert audit["sampled_from_pool_size"] == 26
        # 保留的名字都在 unused 池里
        for n in audit["kept_unused"]:
            assert n not in called
            assert n.startswith("tool_")

    def test_missing_called_tool_gets_minimal_schema(self):
        """called 中存在但 tools 未列出的工具, 仍补最小 schema (旧行为兼容)."""
        tools = [_tool("web_search"), _tool("recall_history")]
        pruned, dropped, audit = prune_tools(
            tools, {"web_search", "mystery"},
            session_id="sess-missing",
            keep_unused_min=0, keep_unused_max=0,
            keep_unused_ratio=0.0, strategy="deterministic",
        )
        names = {t["function"]["name"] for t in pruned}
        assert names == {"web_search", "mystery"}
        # mystery 是补的最小 schema
        m = next(t for t in pruned if t["function"]["name"] == "mystery")
        assert m["function"]["description"] == ""
        assert m["function"]["parameters"] == {"type": "object"}


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
    # P0-R fix: called 是结果子集, 还会保留若干 unused 工具作为 SFT 噪声;
    # called 全保 + 至少 4 个 unused (默认 min=4); fixture 共 5 个工具,
    # unused 仅 3 个, 全保 → result 仍是 5 个.
    names = {t["function"]["name"] for t in md["tools"]}
    assert {"Skill", "web_search"} <= names  # called 永远在
    assert names == {"Skill", "web_search", "browser", "recall_history", "memory_search"}
    assert "RETRIEVAL HEADLINE" in sys_text
    assert stats["tools_before"] == 5 and stats["tools_after"] == 5
    assert stats["tools_prune"]["strategy"] == "deterministic"


def test_prune_session_headline_marker_without_section_succeeds():
    """F3-D: RETRIEVAL HEADLINE 段已下线, 系统提示不再强制含 headline 指令.
    即便 assistant 文本含 ⟦⟧ 而 system 里没有该段, ``prune_session_in_place``
    也不应失败. 历史 qf_out 中的 ⟦⟧ 由 gdr.refiners.meta_tag_strip 在
    save_session 落盘前剥离, 与 prune_session 解耦.
    """
    env = build_chat_env()
    template = load_chat_template("etl/qwenformat/chat_template.jinja")
    session = _session(skill_call=False, headline=True)
    # 人为制造"无 headline 段 + 有 ⟦⟧"组合, 验证不再抛 AssertionError
    broken = SYSTEM_TEMPLATE.replace(
        "检索标题（RETRIEVAL HEADLINE）。最终回复必须追加 headline。", "")
    session["messages"][0]["blocks"][0]["text"] = broken
    session["summary"] = broken
    stats = prune_session_in_place(session, template, env)
    assert stats["tools_before"] >= 1


def test_prune_session_tools_prune_audit_recorded():
    """P0-R: prune_session_in_place 把 tools_prune audit 写入 stats."""
    env = build_chat_env()
    template = load_chat_template("etl/qwenformat/chat_template.jinja")
    session = _session(skill_call=True, headline=True)
    # 默认 strategy="deterministic" + min=4/max=12/ratio=0.3
    stats = prune_session_in_place(session, template, env)
    assert "tools_prune" in stats
    audit = stats["tools_prune"]
    assert audit["strategy"] == "deterministic"
    assert isinstance(audit["kept_unused"], list)
    # called 全保
    rendered_names = {t["function"]["name"] for t in session["metadata"]["tools"]}
    assert {"Skill", "web_search"} <= rendered_names
    # kept_unused 来自 unused 池 (本 fixture 有 3 个 unused: browser, recall_history, memory_search)
    for n in audit["kept_unused"]:
        assert n in {"browser", "recall_history", "memory_search"}


def test_prune_session_tools_legacy_strategy_none_audit():
    """strategy='none' 路径: 不引入 unused 噪声, audit kept_unused=[]."""
    env = build_chat_env()
    template = load_chat_template("etl/qwenformat/chat_template.jinja")
    session = _session(skill_call=True, headline=True)
    stats = prune_session_in_place(
        session, template, env,
        tools_prune_strategy="none",
    )
    assert stats["tools_prune"]["strategy"] == "none"
    assert stats["tools_prune"]["kept_unused"] == []


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
