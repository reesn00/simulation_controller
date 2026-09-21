"""gdr.pipeline.runner._process_one_file 加载契约回归.

qf 阶段产物 (qf_out) 是单 Session JSON (整体多行 indent), 由
``gdr.domain.load_session`` 加载. 不再支持 ``load_trajectory`` —— GDR
只消费 etl/qwenformat 导出的 qf_out 格式, 不再兼容原始 trajectory JSONL.
"""
from __future__ import annotations

import json
from pathlib import Path

from config import Settings


def _write_qf_out(tmp_path: Path) -> Path:
    """造 qf_worker 产物形态: 多行 indent Session JSON(含 metadata)."""
    fp = tmp_path / "qf_out" / "s1.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": "s1",
        "summary": "",
        "messages": [
            {
                "role": "user", "name": "user", "id": "u1",
                "blocks": [{"type": "text", "id": "b1", "text": "hi"}],
                "metadata": {},
            }
        ],
        "metadata": {
            "openai_messages": [{"role": "user", "content": "hi"}],
            "tools": [],
            "qf_text": "user\nhi",
        },
    }
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


def test_process_one_file_loads_session_json(tmp_path, monkeypatch):
    """qf_out 是多行 indent Session JSON; _process_one_file 应成功加载并保存."""
    from pipeline import runner

    qf_in = _write_qf_out(tmp_path)
    base_path = tmp_path / "gdr_out" / "s1_refined"

    monkeypatch.setattr(runner, "load_tools", lambda *a, **k: ([], [], {}, set()))
    monkeypatch.setattr(
        runner, "process_one",
        lambda session, cfg, tn, ha, *, tool_descriptions=None, off_topic_blacklist=None: session,
    )

    cfg = Settings(
        batch_output_dir=tmp_path / "gdr_out",
        workers=1,
        max_files=1,
        enable_llm_layer=False,
    )
    result = runner._process_one_file(qf_in, base_path, cfg)

    assert result["status"] == "success", result
    outputs = result["outputs"]
    assert Path(outputs["messages"]).exists()
    assert Path(outputs["openai"]).exists()
    assert Path(outputs["qwenjina"]).exists()  # fixture 含 qf_text
    assert Path(outputs["meta"]).exists()


def test_gdr_domain_does_not_expose_load_trajectory():
    """用户主旨: GDR 不再兼容原始 trajectory 直接加载; 不导出 load_trajectory."""
    from domain import __all__ as domain_all

    assert "load_trajectory" not in domain_all

    import domain.schema as schema_mod
    assert not hasattr(schema_mod, "load_trajectory"), (
        "gdr.domain.schema.load_trajectory 已删除 (GDR 只消费 qf_out)"
    )


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


def _write_qf_out_with_system(tmp_path: Path) -> Path:
    """造含完整 system prompt + 无工具调用 assistant 的 qf_out."""
    fp = tmp_path / "qf_out" / "s2.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": "s2",
        "summary": _VERBOSE_SYSTEM,
        "messages": [
            {
                "role": "system", "name": "system", "id": "s0",
                "blocks": [{"type": "text", "id": "s0b", "text": _VERBOSE_SYSTEM}],
                "metadata": {},
            },
            {
                "role": "user", "name": "user", "id": "u1",
                "blocks": [{"type": "text", "id": "b1", "text": "hi"}],
                "metadata": {},
            },
            {
                "role": "assistant", "name": "assistant", "id": "a1",
                "blocks": [{"type": "text", "id": "b2", "text": "你好"}],
                "metadata": {},
            },
        ],
        "metadata": {
            "openai_messages": [],
            "tools": [
                {"type": "function", "function": {"name": "web_search", "description": "", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": "recall_history", "description": "", "parameters": {"type": "object"}}},
            ],
            "qf_text": "placeholder",
        },
    }
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


def _run_prune_case(tmp_path, monkeypatch, **cfg_overrides):
    from pipeline import runner

    qf_in = _write_qf_out_with_system(tmp_path)
    base_path = tmp_path / "gdr_out" / "s2_refined"
    monkeypatch.setattr(runner, "load_tools", lambda *a, **k: ([], [], {}, set()))
    monkeypatch.setattr(
        runner, "process_one",
        lambda session, cfg, tn, ha, *, tool_descriptions=None, off_topic_blacklist=None: session,
    )
    cfg = Settings(
        batch_output_dir=tmp_path / "gdr_out",
        workers=1, max_files=1, enable_llm_layer=False,
        **cfg_overrides,
    )
    result = runner._process_one_file(qf_in, base_path, cfg)
    assert result["status"] == "success", result
    msgs = json.loads(Path(result["outputs"]["messages"]).read_text(encoding="utf-8"))
    meta = json.loads(Path(result["outputs"]["meta"]).read_text(encoding="utf-8"))
    return msgs, meta


def test_process_one_file_usage_prune_enabled(tmp_path, monkeypatch):
    """enable_usage_prune=True: 未调用的段/skill/tool 被裁, 四视图一致."""
    msgs, meta = _run_prune_case(tmp_path, monkeypatch, enable_usage_prune=True)

    sys_text = msgs["messages"][0]["blocks"][0]["text"]
    assert "Agent Identity" in sys_text          # identity 始终保留
    # F3-D: RETRIEVAL HEADLINE 段已下线; 在该 fixture 中其文本位于 Agent Identity
    # 与 THE MAP 之间 (无 Conversation Persistence boundary 匹配), 会被 partition
    # 归入 Agent Identity 段 (identity 始终保留), 因此 RETRIEVAL HEADLINE
    # 文本被原样保留. 测试期望不再断言"被裁".
    assert "RETRIEVAL HEADLINE" in sys_text      # 归入 identity 段, 保留
    assert "THE MAP" not in sys_text             # 未调 recall_history → 删
    assert "长期记忆" not in sys_text             # 未调 memory_search → 删
    assert "<agent-skills>" not in sys_text      # 未调 Skill → 整段删
    # P0-R fix: 无任何工具调用时, 仍按 SFT 噪声策略保留 unused 子集
    # (fixture 仅有 2 个 unused, ratio + min 上限 → 全部保留).
    names = {t["function"]["name"] for t in meta["tools"]}
    assert "web_search" in names and "recall_history" in names
    tp = meta["usage_prune"]["tools_prune"]
    assert tp["strategy"] == "deterministic"
    assert set(tp["kept_unused"]) == {"web_search", "recall_history"}
    assert meta["usage_prune"]["system_chars_after"] < meta["usage_prune"]["system_chars_before"]
    # 四视图同源
    assert meta["openai_messages"][0]["content"] == sys_text
    assert sys_text[:100] in meta["qf_text"]


def test_process_one_file_usage_prune_disabled(tmp_path, monkeypatch):
    """enable_usage_prune=False: system/tools/qf_text 原样落盘."""
    msgs, meta = _run_prune_case(tmp_path, monkeypatch, enable_usage_prune=False)

    sys_text = msgs["messages"][0]["blocks"][0]["text"]
    assert "RETRIEVAL HEADLINE" in sys_text
    assert "<agent-skills>" in sys_text
    assert len(meta["tools"]) == 2
    assert meta["qf_text"] == "placeholder"
    assert "usage_prune" not in meta
