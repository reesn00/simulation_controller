"""etl.qwenformat.tool_output_summarizer 单元测试 + qf_worker 集成测试.

全部使用 mock llm_caller / mock summarizer, 不发真实 LLM 请求.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etl.qwenformat.tool_output_summarizer import (
    LLMAnchoredSummarizer,
    RuleOnlySummarizer,
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


def test_l0_removes_ui_noise_lines() -> None:
    raw = (
        "武林外传 热度 3275\n"
        "正在加载...\n"
        "加载中...\n"
        "节目还没有准备好，晚点回来再试试～\n"
        "登录后可专享\n"
        "帐号登录\n"
        "奇秀直播\n"
        "相关推荐 换一组\n"
        "回到顶部\n"
        "剧情讨论"
    )
    cleaned, removed = clean_l0(raw)
    assert removed > 0
    assert cleaned == "武林外传 热度 3275\n剧情讨论"


def test_l0_removes_symbol_run_lines() -> None:
    raw = "正文行\n^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n另一行"
    cleaned, _ = clean_l0(raw)
    assert "^" not in cleaned
    assert "正文行" in cleaned and "另一行" in cleaned


def test_l0_keeps_inline_noise_phrase_and_short_symbol_runs() -> None:
    # 只删整行: 正文中含同名片段不删; 短符号串 (如 SyntaxError 的 ^ 指示符) 保留
    raw = "提示: 节目还没有准备好属于占位文案\n    ^\n====="
    cleaned, _ = clean_l0(raw)
    assert "提示: 节目还没有准备好属于占位文案" in cleaned
    assert "^" in cleaned
    assert "=====" in cleaned


def test_l0_noise_removal_is_idempotent() -> None:
    raw = "A\n正在加载...\n^^^^^^^^^^^^^^\n\nB\n"
    cleaned, _ = clean_l0(raw)
    again, removed_again = clean_l0(cleaned)
    assert again == cleaned
    assert removed_again == 0


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
# from_config (统一根配置 config/config.yaml 的 qf.tool_output_summarizer 段)
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, body: str) -> Path:
    fp = tmp_path / "config.yaml"
    fp.write_text(body, encoding="utf-8")
    return fp


@pytest.fixture(autouse=True)
def _clear_summarizer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("QF_SUMMARIZER_ENABLED", "QF_SUMMARIZER_L0_ENABLED",
                "QF_SUMMARIZER_BASE_URL", "QF_SUMMARIZER_API_KEY",
                "QF_SUMMARIZER_MODEL", "QF_SUMMARIZER_THRESHOLD_CHARS"):
        monkeypatch.delenv(var, raising=False)


def test_from_config_default_file_l0_only() -> None:
    """仓库内置 config.yaml 默认 enabled: false → L0-only (RuleOnlySummarizer)."""
    s = LLMAnchoredSummarizer.from_config()
    assert isinstance(s, RuleOnlySummarizer)


def test_from_config_loads_yaml(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, """
tool_output_summarizer:
  enabled: true
  base_url: "http://example.invalid/v1"
  model: "mini-summarizer"
  api_key: "k"
  threshold_chars: 123
  faith_threshold: 0.5
  cache_dir: "{cache}"
""".format(cache=(tmp_path / "cache").as_posix()))
    s = LLMAnchoredSummarizer.from_config(cfg)
    assert s is not None
    assert s._threshold_chars == 123
    assert s._faith_threshold == 0.5
    assert s._cache_dir == tmp_path / "cache"


def test_from_config_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write_config(tmp_path, """
tool_output_summarizer:
  enabled: true
  base_url: "http://example.invalid/v1"
  model: "mini-summarizer"
  threshold_chars: 123
""")
    monkeypatch.setenv("QF_SUMMARIZER_THRESHOLD_CHARS", "456")
    s = LLMAnchoredSummarizer.from_config(cfg)
    assert s is not None
    assert s._threshold_chars == 456


def test_from_config_env_can_disable_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write_config(tmp_path, """
tool_output_summarizer:
  enabled: true
  base_url: "http://example.invalid/v1"
  model: "m"
""")
    monkeypatch.setenv("QF_SUMMARIZER_ENABLED", "0")
    # L1 被 env 关闭 → 回退 L0-only
    assert isinstance(LLMAnchoredSummarizer.from_config(cfg), RuleOnlySummarizer)


def test_from_config_missing_file_l0_only(tmp_path: Path) -> None:
    """配置文件不存在: L0 默认开启, 返回 RuleOnlySummarizer."""
    assert isinstance(
        LLMAnchoredSummarizer.from_config(tmp_path / "nope.yaml"), RuleOnlySummarizer
    )


def test_from_config_l0_disabled_returns_none(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, """
tool_output_summarizer:
  l0_enabled: false
""")
    assert LLMAnchoredSummarizer.from_config(cfg) is None


def test_from_config_env_can_disable_l0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _write_config(tmp_path, """
tool_output_summarizer:
  enabled: true
  base_url: "http://example.invalid/v1"
  model: "m"
""")
    monkeypatch.setenv("QF_SUMMARIZER_L0_ENABLED", "0")
    assert LLMAnchoredSummarizer.from_config(cfg) is None


def test_from_config_l1_missing_llm_config_falls_back_to_l0(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, """
tool_output_summarizer:
  enabled: true
""")
    assert isinstance(LLMAnchoredSummarizer.from_config(cfg), RuleOnlySummarizer)


def test_rule_only_summarizer_applies_l0() -> None:
    s = RuleOnlySummarizer()
    out = s.summarize("web_search", "正文 😀\n正在加载...\n^^^^^^^^^^^^", ToolOutputContext())
    assert "😀" not in out
    assert "正在加载" not in out
    assert "^" not in out
    assert "正文" in out
    assert s._stats["tool_output_chars_before"] > s._stats["tool_output_chars_after"]


def test_from_config_root_format_with_llm_fallback(tmp_path: Path) -> None:
    """统一根配置格式: qf.tool_output_summarizer 段 + llm 共享段缺省."""
    cfg = _write_config(tmp_path, """
llm:
  base_url: "http://llm-gateway/v1"
  api_key: "kk"
  model: "mm"
qf:
  tool_output_summarizer:
    enabled: true
    threshold_chars: 99
""")
    s = LLMAnchoredSummarizer.from_config(cfg)
    # base_url/model 来自 llm 段; 没有继承时回退 L0-only
    assert s is not None
    assert s._threshold_chars == 99


def test_from_config_root_format_explicit_overrides_llm(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, """
llm:
  base_url: "http://llm-gateway/v1"
  model: "mm"
qf:
  tool_output_summarizer:
    enabled: true
    base_url: "http://qf-gateway/v1"
""")
    s = LLMAnchoredSummarizer.from_config(cfg)
    assert s is not None  # base_url 显式指定, model 继承 llm 段


def test_from_config_root_format_disabled(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, """
llm:
  base_url: "http://llm-gateway/v1"
  model: "mm"
qf:
  tool_output_summarizer:
    enabled: false
""")
    # L1 关闭 → L0-only
    assert isinstance(LLMAnchoredSummarizer.from_config(cfg), RuleOnlySummarizer)


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


def test_qf_worker_default_l0_only(tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """默认配置 (L1 关) 时 worker 走 RuleOnlySummarizer: L0 清洗生效, 无 LLM."""
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
    assert isinstance(w._tool_summarizer, RuleOnlySummarizer)
    [task] = w.pull()
    out_path = w.process(task)
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    tool_msg = next(m for m in payload["metadata"]["openai_messages"]
                    if m["role"] == "tool")
    assert tool_msg["content"] == RAW_LONG


def test_qf_worker_explicit_false_disables_summarizer(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """tool_summarizer=False 显式关闭: 不清洗, tool 内容原样输出."""
    monkeypatch.delenv("QF_SUMMARIZER_ENABLED", raising=False)
    from orchestration.queue import SQLiteQueue
    from orchestration.workers.qf_worker import QfWorker

    queue = SQLiteQueue(tmp_path / "q.db")
    qf_out = tmp_path / "qf_out"
    fp = tmp_path / "r1__sess.json"
    fp.write_text(_trajectory_jsonl_with_tool("sess"), encoding="utf-8")
    queue.insert(src_path=fp, run_id="r1", session_id="sess", batch_id=1)

    w = QfWorker(queue=queue, worker_id="w", qf_output_dir=qf_out,
                 system_templates_dir=tmp_path / "templates",
                 tool_summarizer=False)
    assert w._tool_summarizer is None
