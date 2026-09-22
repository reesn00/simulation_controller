from __future__ import annotations

import json
from pathlib import Path

import pytest

from simulate_serve.checker.completion_checker import (
    check_completion,
    snapshot_partial_trajectory,
)


def _write_trajectory(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(ev, ensure_ascii=False) for ev in events) + "\n", encoding="utf-8")


def _event(event_type: str, **payload) -> dict:
    return {"event_type": event_type, "payload": payload, "metadata": {}}


def _model_response(content: list[dict]) -> dict:
    return _event("model_response", content=content)


def _tool_execution(call_id: str = "tc1") -> dict:
    return _event("tool_execution", tool_call_id=call_id, tool_name="search", output="ok")


def _text_block(text: str) -> dict:
    return {"type": "text", "id": "b1", "text": text}


def _thinking_block(thinking: str) -> dict:
    return {"type": "thinking", "id": "b1", "thinking": thinking}


def _tool_call_block(call_id: str = "tc1", name: str = "search") -> dict:
    return {"type": "tool_call", "id": call_id, "name": name, "input": "{}", "state": "pending"}


# ---------- 缺失/空场景 ----------


def test_missing_path_returns_incomplete(tmp_path: Path) -> None:
    result = check_completion(tmp_path / "nope.json")
    assert result.status == "incomplete"
    assert result.retryable is True
    assert "trajectory_file_missing" in result.reasons


def test_none_path_returns_incomplete() -> None:
    result = check_completion(None)
    assert result.status == "incomplete"
    assert result.retryable is True


def test_empty_trajectory_returns_incomplete(tmp_path: Path) -> None:
    p = tmp_path / "empty.json"
    p.write_text("", encoding="utf-8")
    result = check_completion(p)
    assert result.status == "incomplete"
    assert "trajectory_empty" in result.reasons


# ---------- 终态事件维度 ----------


def test_no_terminal_event_is_incomplete(tmp_path: Path) -> None:
    # 只有 turn_start / model_response，没有 final_reply/error/cancel
    events = [
        _event("turn_start", input_text="hi"),
        _model_response([_text_block("partial answer without end")]),
    ]
    p = tmp_path / "x.json"
    _write_trajectory(p, events)
    result = check_completion(p)
    assert result.status == "incomplete"
    assert result.terminal_event_seen is False
    assert result.has_final_reply is False
    assert any(r.startswith("no_terminal_event") for r in result.reasons)


def test_error_terminal_event_is_aborted(tmp_path: Path) -> None:
    events = [
        _event("turn_start", input_text="hi"),
        _model_response([_text_block("...")])
    ]
    events.append(_event("error", message="remote crashed"))
    p = tmp_path / "x.json"
    _write_trajectory(p, events)
    result = check_completion(p)
    assert result.status == "aborted"
    assert result.retryable is False
    assert result.terminal_event_seen is True
    assert result.last_event_type == "error"


def test_cancel_terminal_event_is_aborted(tmp_path: Path) -> None:
    events = [
        _event("turn_start", input_text="hi"),
        _event("cancel", reason="user stopped"),
    ]
    p = tmp_path / "x.json"
    _write_trajectory(p, events)
    result = check_completion(p)
    assert result.status == "aborted"
    assert result.retryable is False
    assert result.last_event_type == "cancel"


# ---------- final_reply + 末段判定 ----------


def test_final_reply_with_complete_text_is_complete(tmp_path: Path) -> None:
    events = [
        _event("turn_start", input_text="query"),
        _model_response([_text_block("调研完成。结论已确认。")]),
        _event("final_reply", content=[]),
    ]
    p = tmp_path / "x.json"
    _write_trajectory(p, events)
    result = check_completion(p)
    assert result.status == "complete"
    assert result.retryable is False
    assert result.has_final_reply is True
    assert result.terminal_event_seen is True


def test_final_reply_with_truncated_text_is_incomplete(tmp_path: Path) -> None:
    # 长文本没有句末标点也没有结构性闭合信号 → incomplete
    long_text = "现在我先把数据处理一下，过程中需要重新核验字段定义是否一致。先重新查一下"
    events = [
        _event("turn_start", input_text="query"),
        _model_response([_text_block(long_text)]),
        _event("final_reply", content=[]),
    ]
    p = tmp_path / "x.json"
    _write_trajectory(p, events)
    result = check_completion(p)
    assert result.status == "incomplete"
    assert "last_text_incomplete" in result.reasons
    assert result.retryable is True


def test_final_reply_with_complete_close_signal_is_complete(tmp_path: Path) -> None:
    # 末段含 "报告完毕" 强信号
    long_text = "综合上述分析，调研结论如下：\n\n| 项目 | 结果 |\n| --- | --- |\n| 数据 | 已核实 |\n\n报告完毕。"
    events = [
        _event("turn_start", input_text="query"),
        _model_response([_text_block(long_text)]),
        _event("final_reply", content=[]),
    ]
    p = tmp_path / "x.json"
    _write_trajectory(p, events)
    result = check_completion(p)
    assert result.status == "complete"


# ---------- toolcall 配对维度 ----------


def test_last_block_toolcall_is_incomplete(tmp_path: Path) -> None:
    # 末段是 tool_call，没等 tool_execution 回来
    events = [
        _event("turn_start", input_text="hi"),
        _model_response([_text_block("让我搜一下"), _tool_call_block(call_id="tc1")]),
        _event("final_reply", content=[]),
    ]
    p = tmp_path / "x.json"
    _write_trajectory(p, events)
    result = check_completion(p)
    assert result.status == "incomplete"
    assert any(r.startswith("last_assistant_block_is_toolcall") for r in result.reasons)


def test_toolcall_result_mismatch_with_complete_close_is_complete(tmp_path: Path) -> None:
    # tool_call 没配 tool_execution, 但最后 text 含 "报告完毕" → 豁免 → complete
    long_text = "综合上述分析，调研结论如下。报告完毕。"
    events = [
        _event("turn_start", input_text="hi"),
        _model_response([_text_block("查一下"), _tool_call_block(call_id="tc1")]),
        _model_response([_text_block(long_text)]),
        _event("final_reply", content=[]),
    ]
    p = tmp_path / "x.json"
    _write_trajectory(p, events)
    result = check_completion(p)
    # 末尾是 text, toolcall/result 不匹配但有完整收尾信号 → 豁免
    assert result.status == "complete"


def test_toolcall_paired_then_final_text_is_complete(tmp_path: Path) -> None:
    # tool_call + tool_execution 都配对，最后有完整 text
    events = [
        _event("turn_start", input_text="hi"),
        _model_response([_text_block("查一下"), _tool_call_block(call_id="tc1")]),
        _tool_execution(call_id="tc1"),
        _model_response([_text_block("已查完。结论：报告完毕。")]),
        _event("final_reply", content=[]),
    ]
    p = tmp_path / "x.json"
    _write_trajectory(p, events)
    result = check_completion(p)
    assert result.status == "complete"


# ---------- thinking-only 长尾巴维度 ----------


def test_long_thinking_only_tail_is_incomplete(tmp_path: Path) -> None:
    # 末段只有 thinking (>200 chars) 且无 text → F2 修复命中
    long_thinking = "A" * 250
    events = [
        _event("turn_start", input_text="hi"),
        _model_response([_thinking_block(long_thinking)]),
        _event("final_reply", content=[]),
    ]
    p = tmp_path / "x.json"
    _write_trajectory(p, events)
    result = check_completion(p)
    assert result.status == "incomplete"
    assert any(r.startswith("last_assistant_no_final_text") for r in result.reasons)


# ---------- snapshot_partial_trajectory ----------


def test_snapshot_partial_trajectory_copies_file(tmp_path: Path) -> None:
    src = tmp_path / "partial.json"
    src.write_text("events here", encoding="utf-8")
    archive = snapshot_partial_trajectory(src, run_id="r1", attempt=2)
    assert archive is not None
    assert archive.exists()
    assert archive.name == "r1.trajectory_attempt_2.json"
    assert archive.read_text(encoding="utf-8") == "events here"


def test_snapshot_partial_trajectory_returns_none_when_missing(tmp_path: Path) -> None:
    src = tmp_path / "missing.json"
    assert snapshot_partial_trajectory(src, run_id="r1", attempt=1) is None


# ---------- 内嵌换行 / 引号 容错 ----------


def test_trajectory_with_embedded_newlines_in_payload(tmp_path: Path) -> None:
    # tool_execution.payload.output 含原始换行 — 与 etl._iter_json_objects 行为对齐
    raw = (
        '{"event_type": "turn_start", "payload": {"input_text": "hi"}}' + "\n"
        + '{"event_type": "tool_execution", "payload": {"tool_call_id": "tc1", "tool_name": "x", "output": "line1\\nline2\\nline3"}, "metadata": {"end_state": "success"}}' + "\n"
        + '{"event_type": "model_response", "payload": {"content": [{"type": "text", "id": "b1", "text": "完成。报告完毕。"}]}}' + "\n"
        + '{"event_type": "final_reply", "payload": {"content": []}, "metadata": {}}' + "\n"
    )
    p = tmp_path / "x.json"
    p.write_text(raw, encoding="utf-8")
    result = check_completion(p)
    assert result.status == "complete"