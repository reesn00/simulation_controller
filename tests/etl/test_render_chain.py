"""etl.writers.render_chain 单元测试 (F1, 2026-09-28).

覆盖 F1 接线的核心不变量：

* ``openai_messages`` / ``qf_text`` / ``tools`` 被写进 ``session.metadata``
* **merge 而非 replace** —— gdr 评分信号不被抹掉（评分卡 L3/L4/L5 的数据源）
* thinking 块映射为 ``reasoning_content``（CoT SFT 必需，见 MEMORY
  ``etl-drops-structured-thinking``）
* 空 session 跳过渲染而非抛错（数据问题 ≠ 环境问题）
* 模板缺失抛 ``RenderChainError``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from etl.writers.render_chain import (
    DEFAULT_TEMPLATE_PATH,
    RenderChainError,
    apply_render_chain,
)
from gdr.domain.schema import Session

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "search the web",
            "parameters": {"type": "object"},
        },
    }
]


def _session(**overrides) -> Session:
    payload = {
        "session_id": "s1",
        "messages": [
            {"role": "system", "id": "m0",
             "blocks": [{"type": "text", "id": "b0", "text": "You are helpful."}]},
            {"role": "user", "id": "m1",
             "blocks": [{"type": "text", "id": "b1", "text": "search Qwen3"}]},
            {"role": "assistant", "id": "m2", "blocks": [
                {"type": "thinking", "id": "b2", "thinking": "call tool first"},
                {"type": "toolcall", "id": "tc1", "name": "web_search",
                 "state": "finished", "input": '{"q":"Qwen3"}'},
            ]},
            {"role": "assistant", "id": "m3", "blocks": [
                {"type": "toolresult", "id": "tr1", "tool_call_id": "tc1",
                 "name": "web_search", "state": "success",
                 "output_text": "Qwen3 is an LLM series."},
                {"type": "text", "id": "b3", "text": "Qwen3 is a LLM series."},
            ]},
        ],
        "metadata": {
            "tools": TOOLS,
            "validation_summary": {"total_blocks": 4, "passed_L1": 4, "failed_L1": 0},
            "training_value_score": 0.72,
            "quality_scorer_components": {"health": 0.8, "judge": 0.6},
            "refine_history": [{"module": "x", "attempts": 1}],
        },
    }
    payload.update(overrides)
    return Session.model_validate(payload)


# ---------------------------------------------------------------------------
# 核心产出
# ---------------------------------------------------------------------------


def test_renders_openai_messages_and_qf_text():
    s = apply_render_chain(_session())
    meta = s.metadata
    assert meta["openai_messages"], "openai_messages 不应为空"
    assert meta["qf_text"], "qf_text 不应为空"
    assert meta["qf_rendered_at"].endswith("Z")
    # qf_stats 只统计 user/system/role=tool 分支；assistant 由 _flush_assistant
    # 产出但不计 bump，故此处按实际语义断言 2（system + user）。
    assert meta["qf_stats"]["openai_messages_emitted"] == 2
    assert meta["qf_stats"]["tool_calls_emitted"] == 1
    assert meta["qf_stats"]["tool_results_emitted"] == 1
    assert len(meta["openai_messages"]) == 5


def test_openai_message_roles_follow_block_structure():
    msgs = apply_render_chain(_session()).metadata["openai_messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]


def test_thinking_becomes_reasoning_content():
    """thinking 必须落到 reasoning_content —— CoT SFT 的必要输入。"""
    msgs = apply_render_chain(_session()).metadata["openai_messages"]
    assistant = [m for m in msgs if m["role"] == "assistant"][0]
    assert assistant["reasoning_content"] == "call tool first"


def test_tool_call_id_normalized_to_call_prefix():
    msgs = apply_render_chain(_session()).metadata["openai_messages"]
    tc = [m for m in msgs if m["role"] == "assistant"][0]["tool_calls"][0]
    assert tc["id"] == "call_tc1"


# ---------------------------------------------------------------------------
# merge 不变量（最关键：防止抹掉 gdr 评分信号）
# ---------------------------------------------------------------------------


def test_preserves_existing_metadata_keys():
    meta = apply_render_chain(_session()).metadata
    assert meta["validation_summary"]["total_blocks"] == 4
    assert meta["training_value_score"] == 0.72
    assert meta["quality_scorer_components"] == {"health": 0.8, "judge": 0.6}
    assert meta["refine_history"] == [{"module": "x", "attempts": 1}]


def test_preserves_tools_with_full_schema():
    """transform 必须拿到 metadata.tools 的完整 schema，而非退化成空壳。"""
    tools = apply_render_chain(_session()).metadata["tools"]
    assert tools[0]["function"]["description"] == "search the web"


def test_works_without_existing_tools():
    """metadata 无 tools 时退化为从 toolcall 名推导，渲染仍成功。"""
    s = _session(metadata={})
    out = apply_render_chain(s)
    assert out.metadata["openai_messages"]
    assert out.metadata["tools"][0]["function"]["name"] == "web_search"


def test_mutates_in_place_and_returns_same_object():
    s = _session()
    assert apply_render_chain(s) is s


# ---------------------------------------------------------------------------
# 空 session：跳过而非抛错
# ---------------------------------------------------------------------------


def test_empty_messages_skips_render():
    """空 session 是数据问题，不能让整个 etl 阶段失败。"""
    s = apply_render_chain(_session(messages=[]))
    assert "qf_text" not in s.metadata
    assert "openai_messages" not in s.metadata


def test_assistant_only_session_skips_render():
    """无 user 轮次 → Qwen3 模板会抛 "No user query found"; 跳过而非失败。"""
    s = apply_render_chain(_session(messages=[
        {"role": "assistant", "id": "m0", "blocks": [
            {"type": "text", "id": "b0", "text": "hello"}]},
    ]))
    assert "qf_text" not in s.metadata


def test_session_with_metadata_only_preserves_metadata():
    s = apply_render_chain(_session(messages=[], metadata={"training_value_score": 0.3}))
    assert s.metadata["training_value_score"] == 0.3


# ---------------------------------------------------------------------------
# 错误路径
# ---------------------------------------------------------------------------


def test_missing_template_raises_render_chain_error():
    with pytest.raises(RenderChainError, match="chat_template"):
        apply_render_chain(_session(), template_path=Path("does/not/exist.jinja"))


def test_custom_template_path_is_used(tmp_path, monkeypatch):
    tmpl = tmp_path / "t.jinja"
    tmpl.write_text("CUSTOM{{ messages | length }}", encoding="utf-8")
    s = apply_render_chain(_session(), template_path=tmpl)
    assert s.metadata["qf_text"].startswith("CUSTOM")
