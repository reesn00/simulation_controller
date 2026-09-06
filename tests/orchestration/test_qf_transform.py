"""etl.qwenformat.transform.trajectory_to_session_with_openai_metadata 单元测试.

注: 旧 ``camel_agent_state_to_session`` 与旧 ``etl.pawsession.extract``
模块已删除 (用户主旨: 不兼容旧格式). trajectory → Session dict 的
转换现在走 ``etl.qwenformat.load.load_trajectory`` (从 JSONL 事件流重放),
测试只覆盖 Session dict 输入路径.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etl.qwenformat.transform import (
    build_chat_env,
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
# trajectory JSONL → SessionRecord 端到端 (新模块: etl.qwenformat.load)
# ---------------------------------------------------------------------------

def test_session_record_to_session_dict_roundtrip(tmp_path):
    """``etl.qwenformat.load.SessionRecord.to_session_dict`` 必须保留全部块类型,
    且能被 ``trajectory_to_session_with_openai_metadata`` 直接消费."""
    from etl.qwenformat.load import (
        SessionRecord, TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock, Message,
    )
    record = SessionRecord(
        session_id="roundtrip-1",
        run_id="r1",
        summary="sys",
        messages=[
            Message(role="user", name="user", id="u1",
                    blocks=[TextBlock(text="hello")]),
            Message(role="assistant", name="Default", id="a1", blocks=[
                ThinkingBlock(thinking="think"),
                TextBlock(text="hi"),
                ToolCallBlock(id="tc1", name="web_search", input='{"q":"x"}'),
                ToolResultBlock(id="tc1", name="web_search", output_text="result"),
            ]),
        ],
        source_file=str(tmp_path / "r1__roundtrip-1.json"),
        tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
    )
    session_dict = record.to_session_dict()
    assert session_dict["session_id"] == "roundtrip-1"
    assert session_dict["run_id"] == "r1"
    assert len(session_dict["messages"]) == 2

    # 加载 jinja + env
    template_path = Path(__file__).resolve().parents[2] / "etl" / "qwenformat" / "chat_template.jinja"
    template, env = load_chat_template(str(template_path)), build_chat_env()
    out = trajectory_to_session_with_openai_metadata(session_dict, template, env)
    oa = out["metadata"]["openai_messages"]
    assert [m["role"] for m in oa] == ["user", "assistant", "tool"]
    assert "web_search" in out["metadata"]["qf_text"]


def test_load_trajectory_from_real_file(tmp_path):
    """读取真实新格式 trajectory JSONL → SessionRecord → qf_out 端到端."""
    from etl.qwenformat.load import load_trajectory

    fp = tmp_path / "run_5bf5__useramulation-test.json"
    events = [
        {"trace_id": "t", "span_id": "1", "parent_span_id": None,
         "event_type": "turn_start", "timestamp": "2026-09-05T00:00:00+00:00",
         "session_id": "useramulation-test", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "", "model_name": "m",
         "payload": {"input_text": "q"}, "metadata": {}},
        {"trace_id": "t", "span_id": "2", "parent_span_id": None,
         "event_type": "final_reply", "timestamp": "2026-09-05T00:00:01+00:00",
         "session_id": "useramulation-test", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"content": [
             {"type": "message", "content": [{"type": "text", "text": "a"}]}
         ]}, "metadata": {}},
    ]
    fp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    record = load_trajectory(fp)
    assert record.session_id == "useramulation-test"
    assert record.run_id == "run_5bf5"  # 文件名解析保留前缀
    assert len(record.messages) == 2
    assert record.messages[0].role == "user"
    assert record.messages[1].role == "assistant"

    # 必须能被 qf transform 直接消费
    session_dict = record.to_session_dict()
    template_path = Path(__file__).resolve().parents[2] / "etl" / "qwenformat" / "chat_template.jinja"
    template, env = load_chat_template(str(template_path)), build_chat_env()
    out = trajectory_to_session_with_openai_metadata(session_dict, template, env)
    assert out["metadata"]["openai_messages"][0]["role"] == "user"
    assert out["metadata"]["openai_messages"][1]["role"] == "assistant"


def test_load_trajectory_handles_embedded_newlines_in_tool_output(tmp_path):
    """tool_execution.payload.output 含原始换行符 / 引号时仍能正确切分 JSON 对象."""
    from etl.qwenformat.load import load_trajectory

    fp = tmp_path / "run_x__useramulation-nl.json"
    raw = (
        json.dumps({"event_type": "turn_start", "session_id": "useramulation-nl",
                    "payload": {"input_text": "q"}}) + "\n"
        + json.dumps({"event_type": "tool_call_request", "session_id": "useramulation-nl",
                      "payload": {"tool_calls": [{"id": "tc1", "type": "function",
                                                  "function": {"name": "web_search", "arguments": "{}"}}]}}) + "\n"
        + json.dumps({
            "event_type": "tool_execution",
            "session_id": "useramulation-nl",
            # output 含原始换行 + 引号 —— 不能按行切分后 json.loads
            "payload": {"tool_call_id": "tc1", "tool_name": "web_search",
                        "output": "line1\n\"quoted\" line2\nline3"},
        }) + "\n"
        + json.dumps({"event_type": "final_reply", "session_id": "useramulation-nl",
                      "payload": {"content": [{"type": "message",
                                               "content": [{"type": "text", "text": "done"}]}]}}) + "\n"
    )
    fp.write_text(raw, encoding="utf-8")

    record = load_trajectory(fp)
    assert len(record.messages) == 3  # user / assistant(toolcall+toolresult) / assistant(text)
    # 第二个 message 是 assistant, 含 1 个 toolcall + 1 个 toolresult
    asst_msg = record.messages[1]
    assert asst_msg.role == "assistant"
    assert len(asst_msg.blocks) == 2
    # toolresult 块的 output_text 必须完整保留内嵌换行
    from etl.qwenformat.load import ToolResultBlock
    tool_result = next(b for b in asst_msg.blocks if isinstance(b, ToolResultBlock))
    assert "line1" in tool_result.output_text
    assert "quoted" in tool_result.output_text
    assert tool_result.output_text.count("\n") == 2


# ---------------------------------------------------------------------------
# trajectory JSONL → SessionRecord 端到端 (AI SDK 形态: tool_call / tool_result
# 嵌在 model_request.payload.messages[].content 中, tools 定义在
# model_request.payload.tools)
# ---------------------------------------------------------------------------


def _ai_sdk_events() -> list[dict]:
    """AI SDK 形态 trajectory: tool_call/tool_result 内嵌在 model_request.messages.

    事件序列:
      turn_start → model_request[0] (system + user + 27 tools)
                 → model_response
                 → tool_execution × 2
                 → model_request[1] (system + user + assistant(thinking+text+2 tool_call+2 tool_result))
                 → model_response
                 → final_reply (last type=message 是 AI 最终回复, 含 inline <think>)
    """
    return [
        {"trace_id": "t", "span_id": "1", "parent_span_id": None,
         "event_type": "turn_start", "timestamp": "2026-09-05T10:00:00+00:00",
         "session_id": "useramulation-ai", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "", "model_name": "m",
         "payload": {"input_text": "搜索武林外传"}, "metadata": {}},
        {"trace_id": "t", "span_id": "2", "parent_span_id": None,
         "event_type": "model_request", "timestamp": "2026-09-05T10:00:00+00:00",
         "session_id": "useramulation-ai", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {
             "messages": [
                 {"role": "system", "content": [{"type": "text", "text": "你是搜索助手。"}]},
                 {"role": "user", "content": [{"type": "text", "text": "搜索武林外传"}]},
             ],
             "tools": [
                 {"type": "function", "function": {
                     "name": "web_search",
                     "description": "网页搜索",
                     "parameters": {"type": "object", "properties": {
                         "search_term": {"type": "string"}}}}},
                 {"type": "function", "function": {
                     "name": "Skill",
                     "description": "技能调用",
                     "parameters": {"type": "object"}}},
             ],
         }, "metadata": {}},
        {"trace_id": "t", "span_id": "3", "parent_span_id": None,
         "event_type": "model_response", "timestamp": "2026-09-05T10:00:01+00:00",
         "session_id": "useramulation-ai", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"usage": {"total_tokens": 10}}, "metadata": {"duration_ms": 1000}},
        {"trace_id": "t", "span_id": "4", "parent_span_id": None,
         "event_type": "tool_execution", "timestamp": "2026-09-05T10:00:02+00:00",
         "session_id": "useramulation-ai", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"tool_call_id": "call_w1", "tool_name": "web_search",
                     "input": {"search_term": "武林外传"},
                     "output": "[1] ...搜索结果..."},
         "metadata": {"duration_ms": 500, "end_state": "success"}},
        {"trace_id": "t", "span_id": "5", "parent_span_id": None,
         "event_type": "tool_execution", "timestamp": "2026-09-05T10:00:03+00:00",
         "session_id": "useramulation-ai", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"tool_call_id": "call_w2", "tool_name": "web_search",
                     "input": {"search_term": "武林外传 播放"},
                     "output": "[1] ...播放页..."},
         "metadata": {"duration_ms": 500, "end_state": "success"}},
        {"trace_id": "t", "span_id": "6", "parent_span_id": None,
         "event_type": "model_request", "timestamp": "2026-09-05T10:00:04+00:00",
         "session_id": "useramulation-ai", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {
             "messages": [
                 {"role": "system", "content": [{"type": "text", "text": "你是搜索助手。"}]},
                 {"role": "user", "content": [{"type": "text", "text": "搜索武林外传"}]},
                 {"role": "assistant", "content": [
                     {"type": "text",
                      "text": "<think>\n用户想搜武林外传\n</think>\n\n我来搜索。"},
                     {"type": "tool_call", "id": "call_w1", "name": "web_search",
                      "input": '{"search_term": "武林外传"}', "state": "finished"},
                     {"type": "tool_call", "id": "call_w2", "name": "web_search",
                      "input": '{"search_term": "武林外传 播放"}', "state": "finished"},
                     {"type": "tool_result", "id": "call_w2", "name": "web_search",
                      "output": "[1] ...播放页...", "state": "success"},
                     {"type": "tool_result", "id": "call_w1", "name": "web_search",
                      "output": "[1] ...搜索结果...", "state": "success"},
                 ]},
             ],
             "tools": [
                 {"type": "function", "function": {
                     "name": "web_search", "description": "网页搜索",
                     "parameters": {"type": "object"}}},
                 {"type": "function", "function": {
                     "name": "Skill", "description": "技能调用",
                     "parameters": {"type": "object"}}},
             ],
         }, "metadata": {}},
        {"trace_id": "t", "span_id": "7", "parent_span_id": None,
         "event_type": "model_response", "timestamp": "2026-09-05T10:00:05+00:00",
         "session_id": "useramulation-ai", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"usage": {"total_tokens": 25}}, "metadata": {"duration_ms": 1000}},
        {"trace_id": "t", "span_id": "8", "parent_span_id": None,
         "event_type": "final_reply", "timestamp": "2026-09-05T10:00:06+00:00",
         "session_id": "useramulation-ai", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"content": [
             # prior turn 的 <think> + 第一句 (model_request.assistant 已经覆盖, 跳过)
             {"type": "message", "content": [{"type": "text", "text": "<think>\n用户想搜\n</think>\n\n我来搜索。"}]},
             {"type": "plugin_call", "content": [{"type": "data", "text": ""}]},
             {"type": "plugin_call", "content": [{"type": "data", "text": ""}]},
             {"type": "plugin_call_output", "content": [{"type": "data", "text": ""}]},
             {"type": "plugin_call_output", "content": [{"type": "data", "text": ""}]},
             # 真正的 final answer: 末段 type=message
             {"type": "message", "content": [
                 {"type": "text",
                  "text": "<think>\n整合搜索结果\n</think>\n\n为你找到：[武林外传播放页](https://example.com)"},
             ]},
         ]}, "metadata": {"usage": {"total_tokens": 30}, "status": "ok"}},
    ]


def test_ai_sdk_format_full_pipeline(tmp_path):
    """AI SDK 形态 trajectory → qf_out 端到端: 必须含 system / user /
    thinking / assistant(含 tool_call) / tool / assistant 最终回复.

    同时验证:
      - tools 列表来自 model_request.payload.tools (而非从 toolcall 推导)
      - thinking 与 text 分离 (<think>...</think> 被抽出)
      - tool_call / tool_result 来自 LAST model_request.messages[].content
        (而非 tool_call_request / tool_execution 事件)
    """
    from etl.qwenformat.load import (
        load_trajectory, ThinkingBlock, ToolCallBlock, ToolResultBlock, TextBlock,
    )

    fp = tmp_path / "run_x__useramulation-ai.json"
    fp.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in _ai_sdk_events()) + "\n",
        encoding="utf-8",
    )

    record = load_trajectory(fp)
    # tools 列表从 model_request.payload.tools 提取
    assert len(record.tools) == 2
    tool_names = {t["function"]["name"] for t in record.tools}
    assert tool_names == {"web_search", "Skill"}
    # summary = system prompt
    assert "搜索助手" in record.summary

    # messages: system / user / assistant(含 thinking+text+2 tool_call+2 tool_result) /
    #           assistant(thinking+text)
    assert len(record.messages) == 4
    assert [m.role for m in record.messages] == ["system", "user", "assistant", "assistant"]
    assert len(record.messages[0].blocks) == 1
    assert isinstance(record.messages[0].blocks[0], TextBlock)
    assert "搜索助手" in record.messages[0].blocks[0].text

    asst_with_tools = record.messages[2]
    block_types = [type(b).__name__ for b in asst_with_tools.blocks]
    assert block_types == ["ThinkingBlock", "TextBlock", "ToolCallBlock",
                           "ToolCallBlock", "ToolResultBlock", "ToolResultBlock"]
    # thinking 与 text 分离 (来自同一 text 块内嵌 <think>)
    assert "用户想搜武林外传" in asst_with_tools.blocks[0].thinking
    assert "我来搜索" in asst_with_tools.blocks[1].text
    # tool_call 块的 input 是 JSON 字符串, 保留 id / name
    tcs = [b for b in asst_with_tools.blocks if isinstance(b, ToolCallBlock)]
    assert [t.id for t in tcs] == ["call_w1", "call_w2"]
    assert [t.name for t in tcs] == ["web_search", "web_search"]
    # tool_result 块的 id 与对应 tool_call 一致 (event 顺序: tool_result_2, tool_result_1)
    trs = [b for b in asst_with_tools.blocks if isinstance(b, ToolResultBlock)]
    assert [t.id for t in trs] == ["call_w2", "call_w1"]

    # 最终 assistant message 只取 final_reply 最后一个 message 块
    final = record.messages[3]
    assert len(final.blocks) == 2
    assert isinstance(final.blocks[0], ThinkingBlock)
    assert isinstance(final.blocks[1], TextBlock)
    assert "整合搜索结果" in final.blocks[0].thinking
    assert "为你找到" in final.blocks[1].text

    # 端到端 qf transform
    session_dict = record.to_session_dict()
    template_path = Path(__file__).resolve().parents[2] / "etl" / "qwenformat" / "chat_template.jinja"
    template, env = load_chat_template(str(template_path)), build_chat_env()
    out = trajectory_to_session_with_openai_metadata(session_dict, template, env)

    # messages 块结构同 record.messages
    assert len(out["messages"]) == 4
    assert [m["role"] for m in out["messages"]] == ["system", "user", "assistant", "assistant"]

    # openai_messages 角色序列: system / user / assistant(with tool_calls) / tool / tool / assistant
    oa = out["metadata"]["openai_messages"]
    assert [m["role"] for m in oa] == ["system", "user", "assistant", "tool", "tool", "assistant"]
    asst = oa[2]
    assert asst["role"] == "assistant"
    assert "整合" not in (asst.get("reasoning_content") or "")
    assert "用户想搜武林外传" in (asst.get("reasoning_content") or "")
    assert "我来搜索" in (asst.get("content") or "")
    assert len(asst["tool_calls"]) == 2
    assert asst["tool_calls"][0]["id"].startswith("call_")
    # tools 列表来自 trajectory.tools (含 description), 不是空推导
    tools_out = out["metadata"]["tools"]
    assert len(tools_out) == 2
    web_search_def = next(t for t in tools_out if t["function"]["name"] == "web_search")
    assert web_search_def["function"]["description"] == "网页搜索"
    # qf_text 模板正确渲染 system header (含 tools) + 全 6 段对话
    qf_text = out["metadata"]["qf_text"]
    assert "system\n" in qf_text
    assert "web_search" in qf_text
    assert "Skill" in qf_text


def test_thinking_split_in_text_block():
    """_split_thinking 必须正确处理单 text 块内嵌多个 <think> 段."""
    from etl.qwenformat.load import _split_thinking

    # 单个 <think> 段
    t, p = _split_thinking("<think>\n思考1\n</think>\n\n正文1")
    assert "思考1" in t
    assert "正文1" in p

    # 多个 <think> 段 (如 final 之前的两段思考)
    t, p = _split_thinking(
        "<think>\n思考1\n</think>\n\n第一段\n<think>\n思考2\n</think>\n\n第二段"
    )
    assert "思考1" in t and "思考2" in t
    assert "第一段" in p and "第二段" in p

    # 无 <think>
    t, p = _split_thinking("纯文本")
    assert t == ""
    assert p == "纯文本"

    # 只有 <think> 无正文
    t, p = _split_thinking("<think>\n纯思考\n</think>")
    assert "纯思考" in t
    assert p == ""


def test_trajectory_tools_propagate_through_session_dict():
    """SessionRecord.tools 必须出现在 to_session_dict() 输出里."""
    from etl.qwenformat.load import load_trajectory

    fp = Path("output/agent_trajectory/run_9672c8571ee04620bdb10b29a5c197b0"
              "__useramulation-9e04bd01ec294b03995247dcfeedb261.json")
    if not fp.exists():
        pytest.skip("真实 trajectory 不存在 (CI 环境)")
    record = load_trajectory(fp)
    session_dict = record.to_session_dict()
    assert "tools" in session_dict
    assert len(session_dict["tools"]) == len(record.tools)
    assert session_dict["tools"] == record.tools