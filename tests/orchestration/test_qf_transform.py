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
         "event_type": "model_response", "timestamp": "2026-09-05T00:00:01+00:00",
         "session_id": "useramulation-test", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"content": [{"type": "text", "text": "a"}],
                     "usage": {}, "finished_reason": "completed"},
         "metadata": {}},
        {"trace_id": "t", "span_id": "3", "parent_span_id": None,
         "event_type": "final_reply", "timestamp": "2026-09-05T00:00:02+00:00",
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
        + json.dumps({"event_type": "model_response", "session_id": "useramulation-nl",
                      "payload": {"content": [
                          {"type": "thinking", "thinking": "need search"},
                          {"type": "tool_call", "id": "tc1", "name": "web_search",
                           "input": "{}", "state": "pending"},
                      ]}}) + "\n"
        + json.dumps({
            "event_type": "tool_execution",
            "session_id": "useramulation-nl",
            # output 含原始换行 + 引号 —— 不能按行切分后 json.loads
            "payload": {"tool_call_id": "tc1", "tool_name": "web_search",
                        "output": "line1\n\"quoted\" line2\nline3"},
        }) + "\n"
        + json.dumps({"event_type": "model_response", "session_id": "useramulation-nl",
                      "payload": {"content": [{"type": "text", "text": "done"}]}}) + "\n"
        + json.dumps({"event_type": "final_reply", "session_id": "useramulation-nl",
                      "payload": {"content": [{"type": "message",
                                               "content": [{"type": "text", "text": "done"}]}]}}) + "\n"
    )
    fp.write_text(raw, encoding="utf-8")

    record = load_trajectory(fp)
    assert len(record.messages) == 2  # user / assistant(整轮)
    # assistant message 含 thinking + toolcall + toolresult + text
    asst_msg = record.messages[1]
    assert asst_msg.role == "assistant"
    assert [type(b).__name__ for b in asst_msg.blocks] == [
        "ThinkingBlock", "ToolCallBlock", "ToolResultBlock", "TextBlock",
    ]
    # toolresult 块的 output_text 必须完整保留内嵌换行
    from etl.qwenformat.load import ToolResultBlock
    tool_result = next(b for b in asst_msg.blocks if isinstance(b, ToolResultBlock))
    assert "line1" in tool_result.output_text
    assert "quoted" in tool_result.output_text
    assert tool_result.output_text.count("\n") == 2


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# trajectory JSONL → SessionRecord 端到端 (新格式事件流: model_response.content
# 携带 thinking / tool_call / text 块, tool_execution 携带工具结果,
# tools 定义在 model_request.payload.tools)
# ---------------------------------------------------------------------------


def _new_format_events() -> list[dict]:
    """新格式 trajectory 事件流 (结构对齐 2026-09 真实文件).

    事件序列:
      turn_start → model_request (system + user + 2 tools)
                 → model_response [thinking, tool_call w1, tool_call w2]
                 → tool_call_request (冗余, 重放跳过)
                 → tool_execution × 2
                 → model_request (对话快照, 重放跳过)
                 → model_response [thinking, text] (最终回复)
                 → final_reply (整轮快照, 重放跳过; usage 取自 metadata)
    """
    return [
        {"trace_id": "t", "span_id": "1", "parent_span_id": None,
         "event_type": "turn_start", "timestamp": "2026-09-18T01:00:00+00:00",
         "session_id": "useramulation-nf", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "", "model_name": "m",
         "payload": {"input_text": "搜索武林外传", "request_agent_id": None,
                     "agent_backend": "qwenpaw"},
         "metadata": {}},
        {"trace_id": "t", "span_id": "2", "parent_span_id": None,
         "event_type": "model_request", "timestamp": "2026-09-18T01:00:00+00:00",
         "session_id": "useramulation-nf", "agent_id": "default", "user_id": "u",
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
        {"trace_id": "t", "span_id": "3", "parent_span_id": "2",
         "event_type": "model_response", "timestamp": "2026-09-18T01:00:01+00:00",
         "session_id": "useramulation-nf", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {
             "content": [
                 {"type": "thinking", "thinking": "用户想搜武林外传",
                  "id": "th1", "created_at": "2026-09-18T01:00:01+00:00",
                  "finished_at": None},
                 {"type": "tool_call", "id": "toolu_w1", "name": "web_search",
                  "input": '{"search_term": "武林外传"}', "state": "pending",
                  "created_at": "2026-09-18T01:00:01+00:00", "finished_at": None},
                 {"type": "tool_call", "id": "toolu_w2", "name": "web_search",
                  "input": '{"search_term": "武林外传 播放"}', "state": "pending",
                  "created_at": "2026-09-18T01:00:01+00:00", "finished_at": None},
             ],
             "usage": {"input_tokens": 100, "output_tokens": 10, "type": "chat"},
             "finished_reason": "tool_calls",
         }, "metadata": {}},
        {"trace_id": "t", "span_id": "4", "parent_span_id": "3",
         "event_type": "tool_call_request", "timestamp": "2026-09-18T01:00:01+00:00",
         "session_id": "useramulation-nf", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"tool_calls": [
             {"id": "toolu_w1", "type": "function",
              "function": {"name": "web_search", "arguments": '{"search_term": "武林外传"}'}},
             {"id": "toolu_w2", "type": "function",
              "function": {"name": "web_search", "arguments": '{"search_term": "武林外传 播放"}'}},
         ]}, "metadata": {}},
        {"trace_id": "t", "span_id": "5", "parent_span_id": None,
         "event_type": "tool_execution", "timestamp": "2026-09-18T01:00:02+00:00",
         "session_id": "useramulation-nf", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"tool_call_id": "toolu_w1", "tool_name": "web_search",
                     "input": {"search_term": "武林外传"},
                     "output": "[1] ...搜索结果..."},
         "metadata": {"duration_ms": 500, "end_state": "success"}},
        {"trace_id": "t", "span_id": "6", "parent_span_id": None,
         "event_type": "tool_execution", "timestamp": "2026-09-18T01:00:03+00:00",
         "session_id": "useramulation-nf", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"tool_call_id": "toolu_w2", "tool_name": "web_search",
                     "input": {"search_term": "武林外传 播放"},
                     "output": "[1] ...播放页..."},
         "metadata": {"duration_ms": 500, "end_state": "success"}},
        {"trace_id": "t", "span_id": "7", "parent_span_id": None,
         "event_type": "model_request", "timestamp": "2026-09-18T01:00:04+00:00",
         "session_id": "useramulation-nf", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {
             "messages": [
                 {"role": "system", "content": [{"type": "text", "text": "你是搜索助手。"}]},
                 {"role": "user", "content": [{"type": "text", "text": "搜索武林外传"}]},
                 {"role": "assistant", "content": [
                     {"type": "thinking", "thinking": "用户想搜武林外传"},
                     {"type": "tool_call", "id": "toolu_w1", "name": "web_search",
                      "input": '{"search_term": "武林外传"}', "state": "finished"},
                     {"type": "tool_call", "id": "toolu_w2", "name": "web_search",
                      "input": '{"search_term": "武林外传 播放"}', "state": "finished"},
                     {"type": "tool_result", "id": "toolu_w2", "name": "web_search",
                      "output": [{"type": "text", "text": "[1] ...播放页..."}],
                      "state": "success"},
                     {"type": "tool_result", "id": "toolu_w1", "name": "web_search",
                      "output": [{"type": "text", "text": "[1] ...搜索结果..."}],
                      "state": "success"},
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
        {"trace_id": "t", "span_id": "8", "parent_span_id": "7",
         "event_type": "model_response", "timestamp": "2026-09-18T01:00:05+00:00",
         "session_id": "useramulation-nf", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {
             "content": [
                 {"type": "thinking", "thinking": "整合搜索结果",
                  "id": "th2", "created_at": "2026-09-18T01:00:05+00:00",
                  "finished_at": None},
                 {"type": "text", "text": "为你找到：[武林外传播放页](https://example.com)",
                  "id": "tx1", "created_at": "2026-09-18T01:00:05+00:00",
                  "finished_at": None},
             ],
             "usage": {"input_tokens": 200, "output_tokens": 25, "type": "chat"},
             "finished_reason": "completed",
         }, "metadata": {}},
        {"trace_id": "t", "span_id": "9", "parent_span_id": None,
         "event_type": "final_reply", "timestamp": "2026-09-18T01:00:06+00:00",
         "session_id": "useramulation-nf", "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"content": [
             {"type": "reasoning", "role": "assistant",
              "content": [{"type": "text", "text": "用户想搜武林外传"}]},
             {"type": "plugin_call", "role": "assistant",
              "content": [{"type": "data", "data": {
                  "call_id": "toolu_w1", "name": "web_search",
                  "arguments": '{"search_term": "武林外传"}'}}]},
             {"type": "plugin_call_output", "role": "tool",
              "content": [{"type": "data", "data": {
                  "call_id": "toolu_w1", "name": "web_search",
                  "output": "[1] ...搜索结果..."}}]},
             {"type": "reasoning", "role": "assistant",
              "content": [{"type": "text", "text": "整合搜索结果"}]},
             {"type": "message", "role": "assistant",
              "content": [{"type": "text",
                           "text": "为你找到：[武林外传播放页](https://example.com)"}]},
         ]}, "metadata": {"status": "completed",
                          "usage": {"input_tokens": 200, "output_tokens": 25}}},
    ]


def test_new_format_full_pipeline(tmp_path):
    """新格式 trajectory → qf_out 端到端: system / user / assistant(整轮).

    同时验证:
      - tools 列表来自 model_request.payload.tools (而非从 toolcall 推导)
      - thinking 是独立结构化块 (model_response.content type=thinking)
      - tool_call 来自 model_response.content (state pending 归一为 finished)
      - tool_result 来自 tool_execution 事件 (state 取 metadata.end_state)
      - tool_call_request / final_reply.content / model_request.messages 快照
        均为冗余, 不参与重放
      - usage 取自 final_reply.metadata.usage
    """
    from etl.qwenformat.load import (
        load_trajectory, ThinkingBlock, ToolCallBlock, ToolResultBlock, TextBlock,
    )

    fp = tmp_path / "run_x__useramulation-nf.json"
    fp.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in _new_format_events()) + "\n",
        encoding="utf-8",
    )

    record = load_trajectory(fp)
    # tools 列表从 model_request.payload.tools 提取
    assert len(record.tools) == 2
    tool_names = {t["function"]["name"] for t in record.tools}
    assert tool_names == {"web_search", "Skill"}
    # summary = system prompt
    assert "搜索助手" in record.summary

    # messages: system / user / assistant(整轮: thinking+tool_call+tool_result+最终 thinking+text)
    assert len(record.messages) == 3
    assert [m.role for m in record.messages] == ["system", "user", "assistant"]
    assert len(record.messages[0].blocks) == 1
    assert isinstance(record.messages[0].blocks[0], TextBlock)
    assert "搜索助手" in record.messages[0].blocks[0].text

    asst = record.messages[2]
    block_types = [type(b).__name__ for b in asst.blocks]
    assert block_types == [
        "ThinkingBlock", "ToolCallBlock", "ToolCallBlock",
        "ToolResultBlock", "ToolResultBlock",
        "ThinkingBlock", "TextBlock",
    ]
    # 结构化 thinking 块按轮次保留
    assert "用户想搜武林外传" in asst.blocks[0].thinking
    assert "整合搜索结果" in asst.blocks[5].thinking
    # tool_call 块: input 是 JSON 字符串, state 归一为 finished
    tcs = [b for b in asst.blocks if isinstance(b, ToolCallBlock)]
    assert [t.id for t in tcs] == ["toolu_w1", "toolu_w2"]
    assert [t.name for t in tcs] == ["web_search", "web_search"]
    assert all(t.state == "finished" for t in tcs)
    # tool_result 块: id 与对应 tool_call 一致, state 来自 metadata.end_state
    trs = [b for b in asst.blocks if isinstance(b, ToolResultBlock)]
    assert [t.id for t in trs] == ["toolu_w1", "toolu_w2"]
    assert all(t.state == "success" for t in trs)
    # 最终 text 来自最后一个 model_response 的 text 块
    assert "为你找到" in asst.blocks[6].text
    # usage 取自 final_reply.metadata.usage
    assert asst.usage == {"input_tokens": 200, "output_tokens": 25}

    # 端到端 qf transform
    session_dict = record.to_session_dict()
    template_path = Path(__file__).resolve().parents[2] / "etl" / "qwenformat" / "chat_template.jinja"
    template, env = load_chat_template(str(template_path)), build_chat_env()
    out = trajectory_to_session_with_openai_metadata(session_dict, template, env)

    # messages 块结构同 record.messages
    assert len(out["messages"]) == 3
    assert [m["role"] for m in out["messages"]] == ["system", "user", "assistant"]

    # openai_messages 角色序列: system / user / assistant(with tool_calls) / tool / tool / assistant
    oa = out["metadata"]["openai_messages"]
    assert [m["role"] for m in oa] == ["system", "user", "assistant", "tool", "tool", "assistant"]
    asst_oa = oa[2]
    assert asst_oa["role"] == "assistant"
    assert "用户想搜武林外传" in (asst_oa.get("reasoning_content") or "")
    assert len(asst_oa["tool_calls"]) == 2
    assert asst_oa["tool_calls"][0]["id"].startswith("call_")
    final_oa = oa[5]
    assert "整合搜索结果" in final_oa["reasoning_content"]
    assert "为你找到" in final_oa["content"]
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


def test_new_format_multi_turn(tmp_path):
    """多轮会话 (追问): 每轮一对 turn_start / final_reply, 各产出一个
    user message + 一个 assistant message."""
    from etl.qwenformat.load import load_trajectory

    def _turn(events: list[dict], n: int, input_text: str, reply: str) -> None:
        events.extend([
            {"event_type": "turn_start", "session_id": "useramulation-mt",
             "timestamp": f"2026-09-18T01:00:0{n}:00+00:00",
             "payload": {"input_text": input_text}},
            {"event_type": "model_response", "session_id": "useramulation-mt",
             "timestamp": f"2026-09-18T01:00:0{n}:01+00:00",
             "payload": {"content": [{"type": "text", "text": reply}],
                         "usage": {}, "finished_reason": "completed"}},
            {"event_type": "final_reply", "session_id": "useramulation-mt",
             "timestamp": f"2026-09-18T01:00:0{n}:02+00:00",
             "payload": {"content": [
                 {"type": "message", "content": [{"type": "text", "text": reply}]}]},
             "metadata": {}},
        ])

    events: list[dict] = []
    _turn(events, 0, "第一问", "第一答")
    _turn(events, 1, "追问", "追问答")
    fp = tmp_path / "run_x__useramulation-mt.json"
    fp.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )

    record = load_trajectory(fp)
    assert [m.role for m in record.messages] == ["user", "assistant", "user", "assistant"]
    assert record.messages[0].blocks[0].text == "第一问"
    assert record.messages[1].blocks[0].text == "第一答"
    assert record.messages[2].blocks[0].text == "追问"
    assert record.messages[3].blocks[0].text == "追问答"


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