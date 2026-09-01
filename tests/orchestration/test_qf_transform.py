"""etl.qwenformat.transform.trajectory_to_session_with_openai_metadata 单元测试."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etl.qwenformat.transform import (
    build_chat_env,
    camel_agent_state_to_session,
    load_chat_template,
    trajectory_to_session_with_openai_metadata,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "etl" / "qwenformat" / "chat_template.jinja"


@pytest.fixture(scope="module")
def template_env() -> tuple[str, object]:
    return load_chat_template(str(TEMPLATE_PATH)), build_chat_env()


def _basic_trajectory() -> dict:
    """最小 trajectory：1 user + 1 assistant (text only)."""
    return {
        "session_id": "useramulation-test-001",
        "summary": "查询武林外传",
        "messages": [
            {
                "role": "user",
                "name": "user",
                "id": "turn_001",
                "blocks": [{"type": "text", "text": "武林外传在线观看网址？"}],
                "metadata": {},
            },
            {
                "role": "assistant",
                "name": "Default",
                "id": "turn_002",
                "blocks": [
                    {"type": "thinking", "thinking": "用户要查网址..."},
                    {"type": "text", "text": "以下是一些在线观看链接..."},
                ],
                "metadata": {},
            },
        ],
    }


def _trajectory_with_tool_call() -> dict:
    """1 user + 1 assistant (thinking + text + toolcall) + 1 toolresult."""
    return {
        "session_id": "useramulation-test-002",
        "summary": "搜索网页",
        "messages": [
            {
                "role": "user",
                "name": "user",
                "id": "turn_010",
                "blocks": [{"type": "text", "text": "搜索 python 教程"}],
                "metadata": {},
            },
            {
                "role": "assistant",
                "name": "Default",
                "id": "turn_011",
                "blocks": [
                    {"type": "thinking", "thinking": "我应该搜索..."},
                    {"type": "text", "text": "我先搜索一下。"},
                    {
                        "type": "toolcall",
                        "id": "tc_001",
                        "name": "web_search",
                        "input": json.dumps({"q": "python tutorial"}),
                        "state": "finished",
                    },
                ],
                "metadata": {},
            },
            {
                "role": "tool",
                "name": "web_search",
                "tool_call_id": "tc_001",
                "id": "turn_012",
                "blocks": [{"type": "text", "text": "搜索结果: ..."}],
                "metadata": {},
            },
        ],
    }


# ---------------------------------------------------------------------------
# basic 拆解
# ---------------------------------------------------------------------------

def test_basic_text_only_messages(template_env):
    template, env = template_env
    source = _basic_trajectory()
    out = trajectory_to_session_with_openai_metadata(source, template, env)

    # 顶层字段保留
    assert out["session_id"] == "useramulation-test-001"
    assert out["summary"] == "查询武林外传"
    # out["messages"] 与原 trajectory["messages"] 是同一 list 对象（保留不动）
    assert out["messages"] is source["messages"]

    # openai_messages 拆解
    oa = out["metadata"]["openai_messages"]
    assert len(oa) == 2
    assert oa[0] == {"role": "user", "content": "武林外传在线观看网址？"}
    assert oa[1]["role"] == "assistant"
    assert oa[1]["content"] == "以下是一些在线观看链接..."
    assert oa[1]["reasoning_content"] == "用户要查网址..."


def test_messages_blocks_not_modified(template_env):
    """原始 messages（含 blocks）必须原封不动."""
    template, env = template_env
    trajectory = _basic_trajectory()
    original_messages = json.loads(json.dumps(trajectory["messages"]))
    trajectory_to_session_with_openai_metadata(trajectory, template, env)
    assert trajectory["messages"] == original_messages


# ---------------------------------------------------------------------------
# tool_call / tool_result 拆解
# ---------------------------------------------------------------------------

def test_tool_call_becomes_tool_calls_in_assistant(template_env):
    template, env = template_env
    out = trajectory_to_session_with_openai_metadata(
        _trajectory_with_tool_call(), template, env
    )
    oa = out["metadata"]["openai_messages"]
    # 期望: user / assistant (with tool_calls) / tool
    assert [m["role"] for m in oa] == ["user", "assistant", "tool"]
    asst = oa[1]
    assert asst["content"] == "我先搜索一下。"
    assert asst["reasoning_content"] == "我应该搜索..."
    assert len(asst["tool_calls"]) == 1
    tc = asst["tool_calls"][0]
    assert tc["id"].startswith("call_")           # 自动加 call_ 前缀
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "web_search"
    # arguments 被 sanitize_agent_sample 反序列化为 dict
    assert tc["function"]["arguments"] == {"q": "python tutorial"}


def test_tool_result_becomes_role_tool_message(template_env):
    template, env = template_env
    out = trajectory_to_session_with_openai_metadata(
        _trajectory_with_tool_call(), template, env
    )
    oa = out["metadata"]["openai_messages"]
    tool_msg = oa[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"].startswith("call_")
    assert tool_msg["name"] == "web_search"
    assert tool_msg["content"] == "搜索结果: ..."


# ---------------------------------------------------------------------------
# tools 列表推导
# ---------------------------------------------------------------------------

def test_tools_unique_and_ordered(template_env):
    """同一 tool 多次出现，tools 列表只含一次；保持首次出现顺序."""
    template, env = template_env
    trajectory = {
        "session_id": "s",
        "summary": "",
        "messages": [
            {"role": "user", "name": "user", "id": "u1",
             "blocks": [{"type": "text", "text": "x"}], "metadata": {}},
            {"role": "assistant", "name": "Default", "id": "a1", "blocks": [
                {"type": "toolcall", "id": "tc_a", "name": "alpha", "input": "{}"},
                {"type": "toolcall", "id": "tc_b", "name": "beta",  "input": "{}"},
            ], "metadata": {}},
            {"role": "assistant", "name": "Default", "id": "a2", "blocks": [
                {"type": "toolcall", "id": "tc_c", "name": "alpha", "input": "{}"},  # 重复
                {"type": "toolcall", "id": "tc_d", "name": "gamma", "input": "{}"},
            ], "metadata": {}},
        ],
    }
    out = trajectory_to_session_with_openai_metadata(trajectory, template, env)
    tools = out["metadata"]["tools"]
    names = [t["function"]["name"] for t in tools]
    assert names == ["alpha", "beta", "gamma"]
    assert out["metadata"]["qf_stats"]["tools_unique"] == 3


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def test_qf_text_nonempty(template_env):
    template, env = template_env
    out = trajectory_to_session_with_openai_metadata(
        _trajectory_with_tool_call(), template, env
    )
    qf_text = out["metadata"]["qf_text"]
    assert isinstance(qf_text, str)
    assert len(qf_text) > 100
    # 模板会把 tool 列表渲染进 system 段
    assert "alpha" not in qf_text  # 这个 trajectory 没 alpha
    assert "web_search" in qf_text   # tools 列表里有 web_search


def test_qf_text_includes_tools_in_system(template_env):
    template, env = template_env
    out = trajectory_to_session_with_openai_metadata(
        _trajectory_with_tool_call(), template, env
    )
    qf_text = out["metadata"]["qf_text"]
    # Qwen3 模板会以 <tools> 段列出工具定义
    assert "<tools>" in qf_text
    assert "web_search" in qf_text


# ---------------------------------------------------------------------------
# stats / metadata 完整性
# ---------------------------------------------------------------------------

def test_stats_keys_present(template_env):
    template, env = template_env
    out = trajectory_to_session_with_openai_metadata(
        _trajectory_with_tool_call(), template, env
    )
    stats = out["metadata"]["qf_stats"]
    assert "tool_calls_emitted" in stats
    assert "tool_results_emitted" in stats
    assert "openai_messages_emitted" in stats
    assert "tools_unique" in stats
    # sanitize_agent_sample 的统计
    assert "arguments_deserialized" in stats
    assert stats["tool_calls_emitted"] == 1
    assert stats["tool_results_emitted"] == 1
    assert stats["arguments_deserialized"] == 1


def test_metadata_rendered_at_iso(template_env):
    template, env = template_env
    out = trajectory_to_session_with_openai_metadata(
        _basic_trajectory(), template, env
    )
    rendered_at = out["metadata"]["qf_rendered_at"]
    assert rendered_at.endswith("Z")
    assert "T" in rendered_at


def test_external_stats_dict_is_updated(template_env):
    template, env = template_env
    stats: dict[str, int] = {}
    trajectory_to_session_with_openai_metadata(
        _trajectory_with_tool_call(), template, env, stats=stats
    )
    assert "tool_calls_emitted" in stats


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------

def test_empty_messages_renders_empty(template_env):
    """空 messages 模板必 raise（"No messages provided" 或 "No user query"）."""
    template, env = template_env
    with pytest.raises(Exception) as excinfo:
        trajectory_to_session_with_openai_metadata(
            {"session_id": "empty", "summary": "", "messages": []}, template, env
        )
    assert "No messages" in str(excinfo.value) or "No user query" in str(excinfo.value)


def test_text_blocks_merged_in_one_message(template_env):
    """user message 多 text block 应合并为一个 content."""
    template, env = template_env
    trajectory = {
        "session_id": "merge",
        "summary": "",
        "messages": [
            {"role": "user", "name": "user", "id": "u",
             "blocks": [
                 {"type": "text", "text": "你好，"},
                 {"type": "text", "text": "请问武林外传？"},
             ], "metadata": {}},
        ],
    }
    out = trajectory_to_session_with_openai_metadata(trajectory, template, env)
    oa = out["metadata"]["openai_messages"]
    assert oa[0]["content"] == "你好，请问武林外传？"


def test_thinking_then_text_no_tool_call(template_env):
    """thinking + text，无 tool_call：应输出 content + reasoning_content，无 tool_calls 字段."""
    template, env = template_env
    trajectory = {
        "session_id": "t", "summary": "",
        "messages": [
            {"role": "user", "name": "user", "id": "u",
             "blocks": [{"type": "text", "text": "请回答"}], "metadata": {}},
            {"role": "assistant", "name": "Default", "id": "a", "blocks": [
                {"type": "thinking", "thinking": "思考"},
                {"type": "text", "text": "回答"},
            ], "metadata": {}},
        ],
    }
    out = trajectory_to_session_with_openai_metadata(trajectory, template, env)
    asst = out["metadata"]["openai_messages"][1]
    assert asst["role"] == "assistant"
    assert asst["content"] == "回答"
    assert asst["reasoning_content"] == "思考"
    assert "tool_calls" not in asst


# ---------------------------------------------------------------------------
# camel_agent_state_to_session：CAMEL agent trajectory → Session 形态
# ---------------------------------------------------------------------------

def _camel_trajectory() -> dict:
    """真实 QwenPaw 拷贝出来的 CAMEL agent 形态."""
    return {
        "agent": {
            "state": {
                "session_id": "camel-session-001",
                "summary": "查网址",
                "context": [
                    {
                        "role": "user",
                        "name": "user",
                        "id": "turn_1",
                        "content": [{"type": "text", "text": "帮我查个链接", "id": "t1"}],
                        "metadata": {"qwenpaw_tag": "external_user_query"},
                    },
                    {
                        "role": "assistant",
                        "name": "Default",
                        "id": "turn_2",
                        "content": [
                            {"type": "thinking", "thinking": "先搜索", "id": "th1"},
                            {"type": "tool_call", "id": "tc1", "name": "web_search",
                             "input": '{"q": "x"}', "state": "finished"},
                            {"type": "tool_result", "id": "tc1", "name": "web_search",
                             "output": [{"type": "text", "text": "结果A", "id": "r1"}],
                             "state": "success"},
                            {"type": "hint", "hint": "placeholder", "id": "h1"},
                        ],
                        "metadata": {},
                    },
                ],
            }
        }
    }


def test_camel_trajectory_flattens_to_session():
    session = camel_agent_state_to_session(_camel_trajectory())
    assert session["session_id"] == "camel-session-001"
    assert session["summary"] == "查网址"
    assert len(session["messages"]) == 2

    user = session["messages"][0]
    assert user["role"] == "user"
    assert "blocks" in user
    assert "content" not in user
    assert user["blocks"][0]["text"] == "帮我查个链接"

    asst = session["messages"][1]
    assert asst["role"] == "assistant"
    block_types = [b["type"] for b in asst["blocks"]]
    assert block_types == ["thinking", "toolcall", "toolresult"]


def test_camel_tool_result_output_merged_to_output_text():
    session = camel_agent_state_to_session(_camel_trajectory())
    asst = session["messages"][1]
    tool_result = [b for b in asst["blocks"] if b["type"] == "toolresult"][0]
    assert tool_result["output_text"] == "结果A"
    assert "output" not in tool_result


def test_camel_end_to_end_matches_session_path(template_env):
    """CAMEL 形态经两次转换后，与直接喂 Session 形态的结果一致."""
    template, env = template_env
    camel_session = camel_agent_state_to_session(_camel_trajectory())
    out = trajectory_to_session_with_openai_metadata(camel_session, template, env)
    oa = out["metadata"]["openai_messages"]
    assert oa[0]["role"] == "user"
    assert oa[0]["content"] == "帮我查个链接"
    assert [m["role"] for m in oa] == ["user", "assistant", "tool"]
    assert oa[2]["content"] == "结果A"
    assert "web_search" in out["metadata"]["qf_text"]


def test_camel_empty_agent_returns_empty_session():
    session = camel_agent_state_to_session({"agent": {"state": {}}})
    assert session == {"session_id": None, "summary": "", "messages": []}


def test_camel_hint_block_dropped():
    """hint 等 GDR 未定义类型应被丢弃."""
    session = camel_agent_state_to_session(_camel_trajectory())
    asst = session["messages"][1]
    assert all(b["type"] != "hint" for b in asst["blocks"])
    assert [b["type"] for b in asst["blocks"]] == ["thinking", "toolcall", "toolresult"]