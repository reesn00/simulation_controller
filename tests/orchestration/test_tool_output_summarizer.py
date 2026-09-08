"""etl.qwenformat.tool_output_summarizer 单元测试 + qf_worker 集成测试.

全部使用 mock llm_caller / mock summarizer, 不发真实 LLM 请求.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etl.qwenformat.tool_output_summarizer import (
    LLMAnchoredSummarizer,
    ToolOutputContext,
    clean_l0,
    summarize_record,
)
from etl.qwenformat.load import (
    Message,
    SessionRecord,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


# ---------------------------------------------------------------------------
# L0 规则预清洗
# ---------------------------------------------------------------------------

def test_l0_removes_emoji_and_pua() -> None:
    raw = "结果 😀 完成 \ue60b 图标 ▶️ 播放"
    cleaned, removed = clean_l0(raw)
    assert removed > 0
    assert "😀" not in cleaned
    assert "\ue60b" not in cleaned
    assert "▶" not in cleaned
    assert "️" not in cleaned  # variation selector
    assert "结果" in cleaned and "完成" in cleaned and "播放" in cleaned


def test_l0_normalizes_tabs_crlf_and_ansi() -> None:
    raw = "标题\t内容\r\n\x1b[32m绿色\x1b[0m 行尾   \n"
    cleaned, _ = clean_l0(raw)
    assert "\t" not in cleaned
    assert "\r" not in cleaned
    assert "\x1b" not in cleaned
    assert "标题  内容" in cleaned
    assert "绿色" in cleaned
    assert not cleaned.endswith(" ")


def test_l0_collapses_duplicate_lines_and_blanks() -> None:
    raw = "闫妮\n\n闫妮\n\n\n\n沙溢\n"
    cleaned, _ = clean_l0(raw)
    assert cleaned.count("闫妮") == 1
    assert "\n\n\n" not in cleaned
    assert "沙溢" in cleaned


def test_l0_keeps_cjk_punctuation_and_is_idempotent() -> None:
    raw = "《武林外传》。「同福客栈」，共80集。"
    cleaned, removed = clean_l0(raw)
    assert cleaned == raw
    assert removed == 0
    again, _ = clean_l0(cleaned)
    assert again == cleaned


# ---------------------------------------------------------------------------
# LLMAnchoredSummarizer
# ---------------------------------------------------------------------------

RAW_LONG = "搜索结果正文: 武林外传共80集, 2006年央视播出。" + "噪声" * 500


def _ctx(**kwargs) -> ToolOutputContext:
    return ToolOutputContext(user_query="武林外传在线观看网址？", **kwargs)


def _ok_json(summary: str, facts: list[str], relevant: bool = True) -> str:
    return json.dumps({"summary": summary, "kept_facts": facts, "relevant": relevant},
                      ensure_ascii=False)


def test_structured_tool_skips_llm(tmp_path: Path) -> None:
    def forbidden(prompt: str) -> str:
        raise AssertionError("structured tool must not call LLM")

    s = LLMAnchoredSummarizer(llm_caller=forbidden, cache_dir=tmp_path / "c")
    out = s.summarize("execute_shell_command", RAW_LONG, _ctx())
    assert "武林外传共80集" in out
    assert len(out) == len(clean_l0(RAW_LONG)[0])
    assert s._stats["tool_summaries_structured"] == 1


def test_short_output_skips_llm(tmp_path: Path) -> None:
    def forbidden(prompt: str) -> str:
        raise AssertionError("short output must not call LLM")

    s = LLMAnchoredSummarizer(llm_caller=forbidden, threshold_chars=800)
    short = "共80集"
    assert s.summarize("web_search", short, _ctx()) == short
    assert s._stats["tool_summaries_skipped_short"] == 1


def test_llm_summary_success_and_cached(tmp_path: Path) -> None:
    calls: list[str] = []

    def caller(prompt: str) -> str:
        calls.append(prompt)
        return _ok_json("武林外传共80集，2006年央视播出。", ["武林外传共80集"])

    s = LLMAnchoredSummarizer(llm_caller=caller, cache_dir=tmp_path / "cache")
    out = s.summarize("web_search", RAW_LONG, _ctx())
    assert out == "武林外传共80集，2006年央视播出。"
    assert s._stats["tool_summaries_llm"] == 1
    assert len(calls) == 1

    # 缓存命中: 第二次不再调 LLM
    out2 = s.summarize("web_search", RAW_LONG, _ctx())
    assert out2 == out
    assert len(calls) == 1
    assert s._stats["tool_summaries_cache_hit"] == 1


def test_user_query_in_prompt(tmp_path: Path) -> None:
    seen: list[str] = []

    def caller(prompt: str) -> str:
        seen.append(prompt)
        return _ok_json("武林外传共80集。", ["武林外传共80集"])

    s = LLMAnchoredSummarizer(llm_caller=caller)
    s.summarize("web_search", RAW_LONG,
                _ctx(tool_input={"search_term": "武林外传"}))
    assert "武林外传在线观看网址？" in seen[0]      # 主锚点
    assert "search_term" in seen[0]                  # 辅助锚点


def test_faithfulness_violation_keeps_full_content(tmp_path: Path) -> None:
    def caller(prompt: str) -> str:
        # kept_facts 编造了 raw 中不存在的事实
        return _ok_json("摘要", ["原文根本不存在的发布会"])

    s = LLMAnchoredSummarizer(llm_caller=caller)
    out = s.summarize("web_search", RAW_LONG, _ctx())
    assert out == clean_l0(RAW_LONG)[0]
    assert s._stats["faithfulness_violations"] == 1
    assert s._stats["tool_summaries_gate_failed"] == 1


def test_irrelevant_output_keeps_full_content(tmp_path: Path) -> None:
    def caller(prompt: str) -> str:
        return _ok_json("", [], relevant=False)

    s = LLMAnchoredSummarizer(llm_caller=caller)
    out = s.summarize("web_search", RAW_LONG, _ctx())
    assert out == clean_l0(RAW_LONG)[0]
    assert s._stats["tool_summaries_irrelevant"] == 1


def test_llm_error_keeps_full_content(tmp_path: Path) -> None:
    def caller(prompt: str) -> str:
        raise RuntimeError("network down")

    s = LLMAnchoredSummarizer(llm_caller=caller)
    out = s.summarize("web_search", RAW_LONG, _ctx())
    assert out == clean_l0(RAW_LONG)[0]
    assert s._stats["tool_summaries_fallback"] == 1


def test_coverage_gate_missing_assistant_anchor(tmp_path: Path) -> None:
    def caller(prompt: str) -> str:
        return _ok_json("武林外传共80集。", ["武林外传共80集"])

    s = LLMAnchoredSummarizer(llm_caller=caller)
    ctx = _ctx(assistant_response="为你找到播放页: https://example.com/wlwz")
    raw = RAW_LONG + "\nURL: https://example.com/wlwz"
    out = s.summarize("web_search", raw, ctx)
    # assistant 引用了 raw 里的 URL, 但 summary 没保留 → 退回完整内容
    assert out == clean_l0(raw)[0]
    assert s._stats["tool_summaries_gate_failed"] == 1


def test_empty_kept_facts_when_relevant_fails_gate(tmp_path: Path) -> None:
    def caller(prompt: str) -> str:
        return _ok_json("摘要", [])

    s = LLMAnchoredSummarizer(llm_caller=caller)
    out = s.summarize("web_search", RAW_LONG, _ctx())
    assert out == clean_l0(RAW_LONG)[0]
    assert s._stats["tool_summaries_gate_failed"] == 1


def test_from_env_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QF_SUMMARIZER_ENABLED", raising=False)
    assert LLMAnchoredSummarizer.from_env() is None


def test_from_env_enabled_but_missing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QF_SUMMARIZER_ENABLED", "1")
    monkeypatch.delenv("QF_SUMMARIZER_BASE_URL", raising=False)
    monkeypatch.delenv("QF_SUMMARIZER_MODEL", raising=False)
    assert LLMAnchoredSummarizer.from_env() is None


# ---------------------------------------------------------------------------
# summarize_record
# ---------------------------------------------------------------------------

def _record_with_tool_result() -> SessionRecord:
    return SessionRecord(
        session_id="sess", run_id="r1", summary="sys",
        messages=[
            Message(role="system", name="system", id="",
                    blocks=[TextBlock(text="sys")]),
            Message(role="user", name="user", id="u1",
                    blocks=[TextBlock(text="武林外传在线观看网址？")]),
            Message(role="assistant", name="assistant", id="a1", blocks=[
                ToolCallBlock(id="tc1", name="web_search",
                              input='{"search_term": "武林外传"}'),
                ToolResultBlock(id="tc1", name="web_search",
                                output_text=RAW_LONG),
            ]),
            Message(role="assistant", name="assistant", id="a2",
                    blocks=[TextBlock(text="为你找到 https://example.com/wlwz")]),
        ],
        source_file="r1__sess.json",
    )


def test_summarize_record_pairs_context_and_preserves_raw(tmp_path: Path) -> None:
    seen: list[ToolOutputContext] = []

    def caller(prompt: str) -> str:
        return _ok_json("共80集, 含 https://example.com/wlwz", ["武林外传共80集"])

    class Spy(LLMAnchoredSummarizer):
        def summarize(self, tool_name: str, raw_output: str, ctx: ToolOutputContext) -> str:
            seen.append(ctx)
            return super().summarize(tool_name, raw_output, ctx)

    record = _record_with_tool_result()
    summarizer = Spy(llm_caller=caller)
    stats = summarize_record(record, summarizer)

    assert len(seen) == 1
    ctx = seen[0]
    assert ctx.user_query == "武林外传在线观看网址？"
    assert ctx.tool_input == {"search_term": "武林外传"}
    assert "example.com" in ctx.assistant_response

    tr = next(b for b in record.messages[2].blocks if isinstance(b, ToolResultBlock))
    assert tr.metadata["raw_output"] == RAW_LONG
    assert "共80集" in tr.output_text
    assert stats["tool_results_processed"] == 1
    assert stats["tool_summaries_llm"] == 1


# ---------------------------------------------------------------------------
# qf_worker 集成
# ---------------------------------------------------------------------------

def _trajectory_jsonl_with_tool(session_id: str = "sess-1") -> str:
    events = [
        {"trace_id": "t1", "span_id": "s1", "parent_span_id": None,
         "event_type": "turn_start", "timestamp": "2026-09-05T00:00:00+00:00",
         "session_id": session_id, "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "", "model_name": "m",
         "payload": {"input_text": "武林外传在线观看网址？"}, "metadata": {}},
        {"trace_id": "t1", "span_id": "s2", "parent_span_id": None,
         "event_type": "model_request", "timestamp": "2026-09-05T00:00:00+00:00",
         "session_id": session_id, "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {
             "messages": [{"role": "system", "content": [{"type": "text", "text": "sys"}]}],
             "tools": [],
         }, "metadata": {}},
        {"trace_id": "t1", "span_id": "s3", "parent_span_id": None,
         "event_type": "tool_call_request", "timestamp": "2026-09-05T00:00:01+00:00",
         "session_id": session_id, "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"tool_calls": [{"id": "tc1", "type": "function",
                                     "function": {"name": "web_search",
                                                  "arguments": '{"search_term": "武林外传"}'}}]},
         "metadata": {}},
        {"trace_id": "t1", "span_id": "s4", "parent_span_id": None,
         "event_type": "tool_execution", "timestamp": "2026-09-05T00:00:02+00:00",
         "session_id": session_id, "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"tool_call_id": "tc1", "tool_name": "web_search",
                     "input": {"search_term": "武林外传"},
                     "output": RAW_LONG},
         "metadata": {"end_state": "success"}},
        {"trace_id": "t1", "span_id": "s5", "parent_span_id": None,
         "event_type": "final_reply", "timestamp": "2026-09-05T00:00:03+00:00",
         "session_id": session_id, "agent_id": "default", "user_id": "u",
         "channel": "console", "provider_id": "p", "model_name": "m",
         "payload": {"content": [
             {"type": "message", "content": [{"type": "text", "text": "找到了，共80集。"}]},
         ]}, "metadata": {}},
    ]
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"


class MockSummarizer:
    """直接返回固定摘要, 记录调用."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def summarize(self, tool_name: str, raw_output: str, ctx: ToolOutputContext) -> str:
        self.calls.append((tool_name, raw_output))
        return "MOCK_CLEANED"


def test_qf_worker_applies_tool_summarizer(tmp_path: Path) -> None:
    from orchestration.queue import SQLiteQueue
    from orchestration.workers.qf_worker import QfWorker

    queue = SQLiteQueue(tmp_path / "q.db")
    qf_out = tmp_path / "qf_out"
    fp = tmp_path / "r1__sess.json"
    fp.write_text(_trajectory_jsonl_with_tool("sess"), encoding="utf-8")
    tid, inserted = queue.insert(src_path=fp, run_id="r1", session_id="sess", batch_id=1)
    assert inserted

    mock = MockSummarizer()
    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out,
                 system_templates_dir=tmp_path / "templates",
                 tool_summarizer=mock)
    [task] = w.pull()
    out_path = w.process(task)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    # mock 被调用, 摘要进入 openai tool message
    assert mock.calls and mock.calls[0][0] == "web_search"
    tool_msg = next(m for m in payload["metadata"]["openai_messages"]
                    if m["role"] == "tool")
    assert tool_msg["content"] == "MOCK_CLEANED"

    # 原始内容保留在 blocks metadata
    raw_meta = None
    for msg in payload["messages"]:
        for b in msg.get("blocks", []):
            if b.get("type") == "toolresult":
                raw_meta = b.get("metadata", {}).get("raw_output")
    assert raw_meta == RAW_LONG

    # 统计
    assert payload["metadata"]["qf_stats"]["tool_results_processed"] == 1


def test_qf_worker_default_no_summarizer(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """未开 env 时 worker 不摘要, tool 内容原样输出."""
    monkeypatch.delenv("QF_SUMMARIZER_ENABLED", raising=False)
    from orchestration.queue import SQLiteQueue
    from orchestration.workers.qf_worker import QfWorker

    queue = SQLiteQueue(tmp_path / "q.db")
    qf_out = tmp_path / "qf_out"
    fp = tmp_path / "r1__sess.json"
    fp.write_text(_trajectory_jsonl_with_tool("sess"), encoding="utf-8")
    queue.insert(src_path=fp, run_id="r1", session_id="sess", batch_id=1)

    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out,
                 system_templates_dir=tmp_path / "templates")
    assert w._tool_summarizer is None
    [task] = w.pull()
    out_path = w.process(task)
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    tool_msg = next(m for m in payload["metadata"]["openai_messages"]
                    if m["role"] == "tool")
    assert tool_msg["content"] == RAW_LONG
