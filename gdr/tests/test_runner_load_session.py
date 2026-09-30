"""gdr.pipeline.runner._process_one_file 的加载契约 + step 22 usage_prune 集成回归.

输入是 **C1 trajectory 事件流**（JSONL，一行一个事件对象），由
``gdr.parsers.from_trajectory`` 重放为 Session；输出是**单个 C2 refined
Session 文件**（4 视图拆分的职责在 etl 阶段，不在 gdr）。

⚠️ 本文件此前整体对齐的是 2026-09-22 之前的架构: fixture 写 etl 导出的
「嵌套 Session JSON」(qf_out 形态)、断言 ``result["outputs"]`` 四视图、
还把 ``process_one`` 整段打桩掉。格式与架构各演进一次之后, 三条用例全废
(``trajectory events yielded no messages`` / ``KeyError: 'outputs'``)。因为
``testpaths`` 不含 ``gdr/tests``, 一直没人发现。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from config import Settings


def _write_c1(c1_trajectory, tmp_path: Path) -> Path:
    """最小 C1 轨迹: 一问一答, 无 system / 无工具."""
    return c1_trajectory(
        tmp_path / "agent_trajectory" / "T001__s1.json",
        session_id="s1",
        user_text="hi",
        assistant_text="你好",
    )


def _offline_settings(tmp_path: Path, **over) -> Settings:
    """能跑完 ``process_one`` 的离线 Settings。

    端点指向 ``http://localhost:0/v1`` 且 ``enable_llm_layer=False`` —— 这一组
    用例的要点是 **runner 真的把 session 走完了**, 不是 LLM 判定质量。
    """
    base = dict(
        llm_base_url="http://localhost:0/v1",
        llm_api_key="x",
        main_model="m",
        tool_model="m",
        judge_model="m",
        enable_llm_layer=False,
        enable_context_understanding=False,
        enable_trajectory_compare=False,
        enable_free_quality=False,
        incomplete_detection_enabled=False,
        judge_low_output_path=str(tmp_path / "audit" / "judge_low.jsonl"),
    )
    base.update(over)
    return Settings(**base)


def test_process_one_file_loads_c1_and_writes_single_c2(c1_trajectory, tmp_path, monkeypatch):
    """C1 事件流应被重放成 Session, 并落**一个** C2 文件 (不是 4 视图)."""
    from pipeline import runner

    src = _write_c1(c1_trajectory, tmp_path)
    out_path = tmp_path / "refined" / "T001__s1.json"

    monkeypatch.setattr(runner, "load_tools", lambda *a, **k: ([], [], {}, set()))
    monkeypatch.setattr(
        runner, "process_one",
        lambda session, cfg, tn, ha, *, tool_descriptions=None, off_topic_blacklist=None: session,
    )

    result = runner._process_one_file(src, out_path, _offline_settings(tmp_path))

    assert result["status"] == "success", result
    # 4 视图拆分是 etl 阶段的职责; gdr 只交 C2。返回值也只带 output (无 outputs)
    assert result["output"] == str(out_path)
    assert "outputs" not in result, "gdr 不再直接写 4 视图 (2026-09-22 起归 etl)"

    c2 = json.loads(out_path.read_text(encoding="utf-8"))
    assert c2["session_id"] == "s1"
    roles = [m["role"] for m in c2["messages"]]
    assert roles == ["user", "assistant"], f"turn_start→user / model_response→assistant, 实得 {roles}"
    assert c2["messages"][0]["blocks"][0]["text"] == "hi"
    assert c2["messages"][1]["blocks"][0]["text"] == "你好"


def test_process_one_file_rejects_non_c1_input(c1_trajectory, tmp_path, monkeypatch):
    """非事件流输入 (旧 qf_out 嵌套 JSON) 必须报 load_error, 而不是静默产出空 session."""
    from pipeline import runner

    src = tmp_path / "agent_trajectory" / "T001__s9.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    # 老 qf_out 形态: 有 messages 但没有任何 event_type
    src.write_text(
        json.dumps(
            {"session_id": "s9", "summary": "", "messages": [
                {"role": "user", "id": "u1", "blocks": [{"type": "text", "id": "b1", "text": "hi"}]},
            ], "metadata": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = runner._process_one_file(
        src, tmp_path / "refined" / "T001__s9.json", _offline_settings(tmp_path)
    )

    assert result["status"] == "load_error", result
    assert not (tmp_path / "refined" / "T001__s9.json").exists()


def test_gdr_domain_does_not_expose_load_trajectory():
    """``domain`` 包不导出 ``load_trajectory`` —— 重放入口只有 C1 事件流.

    注意: 解析器本身仍在 ``gdr.parsers.from_trajectory`` /
    ``etl.qwenformat.load.load_trajectory``, 这里守的是**旧 qf_out 直读入口**
    不得复活 (它会让人绕过 C1 契约)。
    """
    from domain import __all__ as domain_all

    assert "load_trajectory" not in domain_all

    import domain.schema as schema_mod
    assert not hasattr(schema_mod, "load_trajectory"), (
        "gdr.domain.schema.load_trajectory 不得复活 (统一走 C1 事件流)"
    )


# ---------------------------------------------------------------------------
# step 22: usage_prune 集成
# ---------------------------------------------------------------------------
#
# 这一组**不桩掉 process_one** —— 2026-09-30 之前 usage_prune 在生产里其实
# 从未真正执行过 (详见 usage_prune.py 里 "1. 转 dict 统一操作" 的注释):
# ``isinstance(session, Session)`` 因 gdr 两套导入姿势产生两个类对象而恒 False,
# 落进 ``dict(session)`` 的浅转换后抛 AttributeError, 又被 step 22 的
# ``except Exception`` 吞成 warning。于是 C2 一直带着未裁的 system 段, 以及
# **未泛化的本机路径** (CLAUDE.md 隐私红线)。
# 下面两条是那道静默失效的守门: 断言裁剪结果真的落进了 C2。
#
# 工具裁剪 (``prune_tools`` 本身) 的单测在 tests/unit/test_usage_prune_gdr.py::TestPruneTools;
# 这里断言的是**端到端那条链**: C1 的 tools 有没有活着走到 C2, 以及裁剪有没有
# 真的发生。2026-09-30 之前这条链是断的 —— C1 的 tools 落在 Session 顶层 extra,
# 而三处消费者都读 metadata["tools"], 实测 ``tools_before=0`` (裁剪空转)、
# ``tools_declared`` 恒空。归一修复见 ``gdr/parsers/_normalize_tools``。

_VERBOSE_SYSTEM = """# Agent Identity

Your agent id is `default`.

检索标题（RETRIEVAL HEADLINE）。最终回复必须追加 headline。

地图（THE MAP）。上下文压缩后你会看到索引。

<agent-skills>
Skills are a collection of instructions.

<skill>
<name>browser</name>
<description>Drive a live browser.</description>
<dir>C:\\Users\\klpc\\.qwenpaw\\workspaces\\default\\skills\\browser</dir>
</skill>
</agent-skills>

# 长期记忆

- `MEMORY.md` 是你的核心长期记忆。
"""

# 6 个工具且**一个都没被调用** —— 超过 P0-R 的 keep_unused_min=4,
# 于是裁剪真的会发生 (实测 6 → 4, 随机保留 2 个 unused), 而不是恒等变换。
# 用 2 个工具时 P0-R 会全留, 裁不裁看不出来。
_SIX_UNUSED_TOOLS = [
    {"type": "function", "function": {"name": name, "description": name,
                                      "parameters": {"type": "object"}}}
    for name in ("web_search", "recall_history", "browser", "file_write", "memory_search", "send_mail")
]


def _stub_defect_pipeline(monkeypatch) -> None:
    """让 ``process_one`` 越过「无缺陷早退」, 从而真的走到 step 22.

    ``process_one`` 在 ``not refine_records and not policy_decisions`` 时直接
    return (runner.py:478), **早于 step 22 的 usage_prune**。一条干净的
    一问一答轨迹没有缺陷, 于是永远走不到裁剪 —— 要测 step 22 就必须先造出
    一个缺陷。

    这里只桩"判定层"(Router / decide_policy / reassemble), 被测的 step 22
    及其前后的真实代码全部照跑。手法与
    ``tests/unit/test_gdr_scoring_reject_gate.py::stub_pipeline`` 一致 (同仓
    已验证的写法), 差别是那里用 DEFER_TO_HUMAN 去够 step 23 的 reject 门控,
    这里同样用 DEFER_TO_HUMAN —— repair_items 为空, 不会真调 refiner / LLM。
    """
    from domain import MessageHealth
    from pipeline import runner

    monkeypatch.setattr(runner, "load_tools", lambda *a, **k: ([], [], {}, set()))
    monkeypatch.setattr(runner, "_hard_filter_session", lambda s, c: True)
    monkeypatch.setattr(runner, "light_health_score_for_session", lambda s, c: {})
    monkeypatch.setattr(runner, "fold_failed_toolresults", lambda *a, **k: 0)
    monkeypatch.setattr(runner, "fold_repeated_thinking", lambda *a, **k: 0)

    def _fake_tag(session, tool_names, hallu_apis, cfg, **kw):
        # 必须非空: 空 defects_index → policy_decisions 也空 → 撞上上面的早退。
        # key 必须是 assistant 文本块的真实 id —— write_c1_trajectory 给的 text
        # 块 id 是 "tx1", 写别的 key 等于没打上缺陷 (踩过: 早退依旧命中)。
        defects = {"tx1": [runner.DefectTag.TEXT_FACT_HALLUCINATION]}
        health = [
            MessageHealth(msg_idx=1, msg_id="m1", health_score=1.0, is_healthy=True)
        ]
        return defects, health, []

    monkeypatch.setattr(
        runner, "Router",
        lambda *a, **k: SimpleNamespace(tag=_fake_tag),
    )
    monkeypatch.setattr(
        runner, "decide_policy",
        lambda block, defects, view, retry_exhausted=False, cfg=None:
            runner.RefinementPolicy.DEFER_TO_HUMAN,
    )
    monkeypatch.setattr(runner, "policy_reason", lambda policy, defects, view: "stub")

    def _fake_reassemble(session, *a, **k):
        session.metadata = session.metadata or {}
        return session

    monkeypatch.setattr(runner, "reassemble", _fake_reassemble)


def _run_prune_case(c1_trajectory, tmp_path, monkeypatch, **cfg_overrides):
    """跑完整的 runner 流程 (只桩判定层), 返回落盘的 C2。"""
    from pipeline import runner

    _stub_defect_pipeline(monkeypatch)

    src = c1_trajectory(
        tmp_path / "agent_trajectory" / "T002__s2.json",
        session_id="s2",
        system_prompt=_VERBOSE_SYSTEM,
        user_text="hi",
        assistant_text="你好",
        tools=_SIX_UNUSED_TOOLS,
        # 刻意零 tool_calls: usage_prune 要据此判「6 个工具都没被调用过」,
        # 走 P0-R 的 unused 保留分支 (超过 min=4, 所以会真的裁掉 2 个)。
    )
    out_path = tmp_path / "refined" / "T002__s2.json"

    result = runner._process_one_file(
        src, out_path, _offline_settings(tmp_path, **cfg_overrides)
    )
    assert result["status"] == "success", result
    return json.loads(out_path.read_text(encoding="utf-8"))


def test_process_one_file_usage_prune_enabled(c1_trajectory, tmp_path, monkeypatch):
    """usage_prune_enabled=True: 未调用的段/skill 被裁, 本机路径被泛化, 工具被裁."""
    c2 = _run_prune_case(c1_trajectory, tmp_path, monkeypatch, usage_prune_enabled=True)

    sys_text = c2["messages"][0]["blocks"][0]["text"]
    up = c2["metadata"]["usage_prune"]

    assert "skipped" not in up, "usage_prune 不该被跳过"

    assert "Agent Identity" in sys_text          # identity 始终保留
    # F3-D: RETRIEVAL HEADLINE 段已下线; 在该 fixture 中其文本位于 Agent Identity
    # 与 THE MAP 之间 (无 Conversation Persistence boundary 匹配), 会被 partition
    # 归入 Agent Identity 段 (identity 始终保留), 因此 RETRIEVAL HEADLINE
    # 文本被原样保留. 测试期望不再断言"被裁".
    assert "RETRIEVAL HEADLINE" in sys_text      # 归入 identity 段, 保留
    assert "长期记忆" not in sys_text             # 未调 memory_search → 删
    assert "<agent-skills>" not in sys_text      # 未调 Skill → 整段删

    assert up["system_chars_after"] < up["system_chars_before"]
    assert up["system_chars_after"] == len(sys_text), (
        "stats 里的裁后长度应与落盘 C2 的 system 文本一致 (四视图/统计同源)"
    )

    # CLAUDE.md 隐私红线: skill 目录里的本机路径不得原样进 C2。
    # 这条正是 usage_prune 静默失效期间唯独没人发现的损失 —— 泛化没跑,
    # C:\\Users\\klpc 会直接进训练数据。
    assert "klpc" not in sys_text, f"本机路径未泛化, system 仍含: {sys_text[-160:]!r}"
    assert up["path_new_roots"], "本机路径泛化应记录替换后的新根"

    # 工具裁剪: C1 的 6 个工具经 _normalize_tools 进了 metadata, 在此真的被裁。
    # 归一之前这里是 tools_before=0 —— 裁剪全程空转, 而 stats 看起来"正常"。
    assert "tools" not in c2, "tools 归一到 metadata, 不应再留顶层 extra"
    declared = [t["function"]["name"] for t in c2["metadata"]["tools"]]
    assert up["tools_before"] == len(_SIX_UNUSED_TOOLS) == 6, (
        "C1 声明的 6 个工具应完整到达 usage_prune; 实得 "
        f"{up['tools_before']} —— tools 归一化若回退, 裁剪会静默空转"
    )
    assert up["tools_after"] < up["tools_before"], "6 个全未调用的工具应被裁掉一部分"
    assert len(declared) == up["tools_after"], "落盘 tools 数量应与 stats 一致"
    # P0-R 保留的是按 session_id 播种的**随机子集**, 所以不能断言具体是哪几个,
    # 只能断言"保留与丢弃恰好互补, 且合起来是原始 6 个"。
    all_names = {t["function"]["name"] for t in _SIX_UNUSED_TOOLS}
    assert not (set(up["dropped_tools"]) & set(declared)), (
        f"丢弃与保留不应重叠: dropped={up['dropped_tools']} kept={declared}"
    )
    assert set(up["dropped_tools"]) | set(declared) == all_names, (
        "丢弃 + 保留应恰好还原 C1 声明的全部工具, 不多不少不重"
    )
    assert up["tools_prune"]["strategy"] == "deterministic"
    assert up["tools_prune"]["sampled_from_pool_size"] == 6
    assert up["tools_prune"]["kept_unused_count"] == up["tools_after"]


def test_process_one_file_usage_prune_disabled(c1_trajectory, tmp_path, monkeypatch):
    """usage_prune_enabled=False: 整个 step 22 不进, system/tools 原样, 无任何裁剪痕迹."""
    c2 = _run_prune_case(c1_trajectory, tmp_path, monkeypatch, usage_prune_enabled=False)

    sys_text = c2["messages"][0]["blocks"][0]["text"]

    # runner 的 step 22 是 `if getattr(cfg, "usage_prune_enabled", True):` 包着的
    # —— 关闭时整块跳过, 连 prune_session_in_place 都没被调用, 所以 C2 里既没有
    # usage_prune 键, 也没有 prune_session_in_place 自己会返回的 {"skipped": ...}
    # (那个分支只有直接调 prune_session_in_place 时才会走到)。
    assert "usage_prune" not in c2["metadata"]

    assert sys_text == _VERBOSE_SYSTEM
    assert "地图（THE MAP）" in sys_text
    assert "<agent-skills>" in sys_text
    assert "长期记忆" in sys_text
    assert "C:" + chr(92) + "Users" + chr(92) + "klpc" in sys_text, (
        "关闭裁剪时路径泛化也不该跑 —— 这条同时钉住「关掉 usage_prune 就等于"
        "关掉本机路径泛化」这个已知副作用 (隐私红线靠默认值保证, 不靠用户记得开)"
    )
    # 工具也不裁, 6 个全留
    assert len(c2["metadata"]["tools"]) == 6


def test_process_one_file_usage_prune_disabled(c1_trajectory, tmp_path, monkeypatch):
    """usage_prune_enabled=False: 整个 step 22 不进, system 原样, 无任何裁剪痕迹."""
    c2 = _run_prune_case(c1_trajectory, tmp_path, monkeypatch, usage_prune_enabled=False)

    sys_text = c2["messages"][0]["blocks"][0]["text"]

    # runner 的 step 22 是 `if getattr(cfg, "usage_prune_enabled", True):` 包着的
    # —— 关闭时整块跳过, 连 prune_session_in_place 都没被调用, 所以 C2 里既没有
    # usage_prune 键, 也没有 prune_session_in_place 自己会返回的 {"skipped": ...}
    # (那个分支只有直接调 prune_session_in_place 时才会走到)。
    assert "usage_prune" not in c2["metadata"]

    assert sys_text == _VERBOSE_SYSTEM
    assert "地图（THE MAP）" in sys_text
    assert "<agent-skills>" in sys_text
    assert "长期记忆" in sys_text
    assert "C:" + chr(92) + "Users" + chr(92) + "klpc" in sys_text, (
        "关闭裁剪时路径泛化也不该跑 —— 这条同时钉住「关掉 usage_prune 就等于"
        "关掉本机路径泛化」这个已知副作用 (隐私红线靠默认值保证, 不靠用户记得开)"
    )
