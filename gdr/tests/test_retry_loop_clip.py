"""gdr/refiners/retry_loop_clip 模块的单元 + 集成测试.

TDD 起点: 这些测试先于实现存在, 驱动 retry_loop_clip 模块的 API.

设计要点 (方案 ②):
- 触发严格: 仅 rule-based 预筛通过的连续同函数 429 段才进入 LLM 评估.
- LLM 失败时保留原状不剪枝 (保守 fallback).
- LLM 决策: 是否同一意图反复重试 + 保留哪几条 (≤3, ≥1 含失败).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gdr.domain.schema import (
    Message,
    Session,
    TextBlock,
    ThinkingBlock,
    ToolcallBlock,
    ToolresultBlock,
)
from gdr.refiners.retry_loop_clip import (
    apply_clip,
    find_retry_loop_segments,
    group_consecutive_same_function,
    is_rate_limited_text,
    llm_judge_retry_loop,
    clip_session,
    _bid,
    _bname,
)


# ---------------------------------------------------------------------------
# 纯文本 rate-limit 标记检测
# ---------------------------------------------------------------------------


class TestIsRateLimitedText:
    def test_429_marker(self):
        assert is_rate_limited_text("Error 429: too many requests") is True

    def test_rate_limit_phrase(self):
        assert is_rate_limited_text("Rate limit exceeded; retry later.") is True

    def test_too_many_requests(self):
        assert is_rate_limited_text("Too Many Requests") is True

    def test_rate_limited_snake(self):
        assert is_rate_limited_text("status: rate_limited") is True

    def test_timeout_marker(self):
        assert is_rate_limited_text("Request timeout after 30s") is True

    def test_unrelated_error(self):
        assert is_rate_limited_text("404 Not Found") is False

    def test_success_response(self):
        assert is_rate_limited_text('{"results": [...]}') is False

    def test_empty(self):
        assert is_rate_limited_text("") is False

    def test_mixed_with_unrelated_text(self):
        """包含 marker 但也包含其它内容 — 仍然判定为 rate limited (任一命中即 True)."""
        assert is_rate_limited_text("search returned 429; see logs") is True


# ---------------------------------------------------------------------------
# group_consecutive_same_function
# ---------------------------------------------------------------------------


def _tc(name: str, bid: str, input_: str = "{}") -> ToolcallBlock:
    """toolcall 与其配对 toolresult 使用相同 id (与 chatml 一致)."""
    return ToolcallBlock(type="toolcall", id=bid, name=name, input=input_, state="finished")


def _tr(bid: str, output: str = "ok", state: str = "success") -> ToolresultBlock:
    """``bid`` 必须与对应 toolcall 的 id 一致, 才能被 apply_clip 配对."""
    return ToolresultBlock(type="toolresult", id=bid, name="web_search", output_text=output, state=state)


class TestGroupConsecutiveSameFunction:
    def test_groups_two_web_search(self):
        blocks = [
            _tc("web_search", "tc1"),
            _tr("tc1"),
            _tc("web_search", "tc2"),
            _tr("tc2"),
        ]
        runs = group_consecutive_same_function(blocks)
        # 同名相邻: 视为同 run
        assert len(runs) == 1
        assert len(runs[0]) == 2

    def test_splits_on_different_function(self):
        blocks = [
            _tc("web_search", "tc1"),
            _tr("tc1"),
            _tc("browser", "tc2"),
            _tr("tc2"),
            _tc("web_search", "tc3"),
            _tr("tc3"),
        ]
        runs = group_consecutive_same_function(blocks)
        # 3 段: web_search[tc1], browser[tc2], web_search[tc3]
        assert len(runs) == 3
        assert all(_bname(c) == "web_search" for _, c, _ in runs[0])
        assert all(_bname(c) == "browser" for _, c, _ in runs[1])
        assert all(_bname(c) == "web_search" for _, c, _ in runs[2])

    def test_splits_on_text_between(self):
        """text/thinking 块打断连续同函数段."""
        blocks = [
            _tc("web_search", "tc1"),
            _tr("tc1"),
            TextBlock(type="text", id="t0", text="中间文字"),
            _tc("web_search", "tc2"),
            _tr("tc2"),
        ]
        runs = group_consecutive_same_function(blocks)
        assert len(runs) == 2

    def test_empty_blocks(self):
        assert group_consecutive_same_function([]) == []

    def test_returns_toolcall_index(self):
        """每个 run 元素应带 toolcall 在原 blocks 列表中的下标, 便于 apply_clip."""
        blocks = [
            ThinkingBlock(type="thinking", id="th0", thinking="思考"),
            _tc("web_search", "tc1"),
            _tr("tc1"),
            _tc("web_search", "tc2"),
            _tr("tc2"),
        ]
        runs = group_consecutive_same_function(blocks)
        assert len(runs) == 1
        indices = [idx for idx, _, _ in runs[0]]
        assert indices == [1, 3]  # thinking 不计入, tc 在 1 / 3


# ---------------------------------------------------------------------------
# find_retry_loop_segments: rule 预筛
# ---------------------------------------------------------------------------


class TestFindRetryLoopSegments:
    def _make_429_run(self, n: int):
        """构造 n 次同函数调用 + 配对 result, 全 429. 真实 chatml 是交错形态."""
        blocks = []
        for i in range(n):
            blocks.append(_tc("web_search", f"tc{i}", '{"q": "x"}'))
            blocks.append(_tr(f"tc{i}", "Error 429: rate limited", state="error"))
        return blocks

    def test_short_run_skipped(self):
        """段长 < min_consecutive → 不触发."""
        blocks = self._make_429_run(3)
        segs = find_retry_loop_segments(blocks, min_consecutive=5)
        assert segs == []

    def test_long_run_all_429_triggered(self):
        """段长 ≥ min_consecutive + 全部 429 → 触发."""
        blocks = self._make_429_run(5)
        segs = find_retry_loop_segments(blocks, min_consecutive=5)
        assert len(segs) == 1
        assert len(segs[0]) == 5

    def test_mixed_states_not_triggered(self):
        """段中有非 429 结果 → 不触发 (留待 LLM 之外的其它处理)."""
        blocks = [
            _tc("web_search", "tc1"),
            _tr("tc1", "Error 429", state="error"),
            _tc("web_search", "tc2"),
            _tr("tc2", "ok found it", state="success"),  # success 打断
        ]
        segs = find_retry_loop_segments(blocks, min_consecutive=5)
        assert segs == []

    def test_long_run_with_one_success_not_triggered(self):
        """段长够 + 含 success → 不触发."""
        blocks = []
        for i in range(5):
            blocks.append(_tc("web_search", f"tc{i}"))
            state = "success" if i == 2 else "error"
            output = "found" if i == 2 else "429"
            blocks.append(_tr(f"tc{i}", output, state=state))
        segs = find_retry_loop_segments(blocks, min_consecutive=5)
        assert segs == []


# ---------------------------------------------------------------------------
# llm_judge_retry_loop
# ---------------------------------------------------------------------------


def _make_mock_client(text: str) -> MagicMock:
    client = MagicMock()
    client.generate.return_value = (text, {"tokens_in": 1, "tokens_out": 1})
    return client


def _call_summary(call: ToolcallBlock, result: ToolresultBlock | None) -> dict:
    return {
        "function": call.name,
        "input": (call.input or "")[:200],
        "error": (result.output_text if result else "")[:200],
        "state": result.state if result else None,
    }


def _segment_payload(segment) -> str:
    """构造 LLM 输入的 calls 字符串."""
    return json.dumps(
        [_call_summary(call, result) for _, call, result in segment],
        ensure_ascii=False, indent=2,
    )


class TestLlmJudgeRetryLoop:
    def test_llm_says_no_returns_none(self):
        """LLM is_retry_loop=false → 返回 None (不剪枝)."""
        client = _make_mock_client(json.dumps({
            "is_retry_loop": False, "reason": "用户换了搜索方向",
        }))
        segment = [
            (i, _tc("web_search", f"tc{i}", json.dumps({"q": f"q{i}"})), _tr(f"tc{i}", "429", "error"))
            for i in range(5)
        ]
        result = llm_judge_retry_loop(segment, client)
        assert result is None
        client.generate.assert_called_once()

    def test_llm_returns_keep_indices(self):
        """LLM is_retry_loop=true + 合法 keep_indices → 返回该列表."""
        keep = [0, 4]
        client = _make_mock_client(json.dumps({
            "is_retry_loop": True,
            "reason": "高度一致, 区别仅在大小写",
            "keep_indices": keep,
        }))
        segment = [
            (i, _tc("web_search", f"tc{i}", json.dumps({"q": "test"})), _tr(f"tc{i}", "429", "error"))
            for i in range(5)
        ]
        result = llm_judge_retry_loop(segment, client, max_keep=3)
        assert result == keep

    def test_llm_keep_capped_at_max_keep(self):
        """LLM keep_indices 长度 > max_keep → 截断."""
        client = _make_mock_client(json.dumps({
            "is_retry_loop": True,
            "keep_indices": [0, 1, 2, 3, 4],
        }))
        segment = [
            (i, _tc("web_search", f"tc{i}", json.dumps({"q": "test"})), _tr(f"tc{i}", "429", "error"))
            for i in range(5)
        ]
        result = llm_judge_retry_loop(segment, client, max_keep=3)
        assert result is not None
        assert len(result) <= 3

    def test_llm_keep_without_failure_rejected(self):
        """keep_indices 全是 success → 违反"≥1 失败"约束 → None."""
        # 构造混合段 (含 success), 直接喂给 llm_judge_retry_loop 测试其拒判.
        # find_retry_loop_segments 不会让该段通过 rule 预筛, 但 llm_judge_retry_loop
        # 是 unit 函数, 应单独验证其兜底逻辑.
        client = _make_mock_client(json.dumps({
            "is_retry_loop": True,
            "keep_indices": [2],  # LLM 选了 success idx
        }))
        segment = [
            (0, _tc("web_search", "tc0"), _tr("tc0", "429", "error")),
            (1, _tc("web_search", "tc1"), _tr("tc1", "429", "error")),
            (2, _tc("web_search", "tc2"), _tr("tc2", "ok found it", "success")),
            (3, _tc("web_search", "tc3"), _tr("tc3", "429", "error")),
            (4, _tc("web_search", "tc4"), _tr("tc4", "429", "error")),
        ]
        result = llm_judge_retry_loop(segment, client, max_keep=3)
        # keep=[2] 只有 success, 违反 ≥1 失败 → None
        assert result is None

    def test_llm_invalid_json_returns_none(self):
        """LLM 返回非 JSON → 保守 None."""
        client = _make_mock_client("not valid json {")
        segment = [
            (i, _tc("web_search", f"tc{i}", "{}"), _tr(f"tc{i}", "429", "error"))
            for i in range(5)
        ]
        assert llm_judge_retry_loop(segment, client) is None

    def test_llm_exception_returns_none(self):
        """LLM 调用抛异常 → 保守 None, 不动数据."""
        client = MagicMock()
        client.generate.side_effect = RuntimeError("network down")
        segment = [
            (i, _tc("web_search", f"tc{i}", "{}"), _tr(f"tc{i}", "429", "error"))
            for i in range(5)
        ]
        assert llm_judge_retry_loop(segment, client) is None

    def test_llm_missing_keep_indices_returns_none(self):
        client = _make_mock_client(json.dumps({"is_retry_loop": True, "reason": "yes"}))
        segment = [
            (i, _tc("web_search", f"tc{i}", "{}"), _tr(f"tc{i}", "429", "error"))
            for i in range(5)
        ]
        assert llm_judge_retry_loop(segment, client) is None

    def test_llm_keep_out_of_range_filtered(self):
        """keep_indices 含超出段长的下标 → 过滤掉, 若过滤后空 → None."""
        client = _make_mock_client(json.dumps({
            "is_retry_loop": True,
            "keep_indices": [10, 20],  # 全超界
        }))
        segment = [
            (i, _tc("web_search", f"tc{i}", "{}"), _tr(f"tc{i}", "429", "error"))
            for i in range(5)
        ]
        assert llm_judge_retry_loop(segment, client) is None


# ---------------------------------------------------------------------------
# apply_clip: 按 keep_indices 剪枝
# ---------------------------------------------------------------------------


class TestApplyClip:
    def test_drops_non_kept_toolcall_and_result(self):
        """保留 keep_indices 处的 toolcall 与配对 toolresult, 其余删除."""
        # blocks 索引:   0   1   2   3   4   5
        # 内容:        tc0 tr0 tc1 tr1 tc2 tr2
        # keep=[0,4] → 保留 tc0 + tr0 (配对), tc2 + tr2 (配对)
        blocks = [
            _tc("web_search", "tc0", '{"q": "a"}'),
            _tr("tc0", "429", "error"),
            _tc("web_search", "tc1", '{"q": "b"}'),
            _tr("tc1", "429", "error"),
            _tc("web_search", "tc2", '{"q": "c"}'),
            _tr("tc2", "429", "error"),
        ]
        keep = [0, 4]  # block indices: tc0, tc2
        out = apply_clip(blocks, keep)
        assert len(out) == 4
        kept_ids = [_bid(b) for b in out]
        assert kept_ids == ["tc0", "tc0", "tc2", "tc2"]

    def test_keeps_surrounding_blocks(self):
        """非工具块 (text/thinking) 不受 keep_indices 影响, 保留原位."""
        # blocks 索引:   0   1    2    3    4    5
        # 内容:        text tc0  tr0  tc1  tr1  text
        # keep=[1] → 保留 tc0 + 配对 tr0
        blocks = [
            TextBlock(type="text", id="t0", text="开头"),
            _tc("web_search", "tc0"),
            _tr("tc0", "429", "error"),
            _tc("web_search", "tc1"),
            _tr("tc1", "429", "error"),
            TextBlock(type="text", id="t1", text="结尾"),
        ]
        keep = [1]  # block index of tc0
        out = apply_clip(blocks, keep)
        kept_ids = [_bid(b) for b in out]
        assert "t0" in kept_ids
        assert "t1" in kept_ids
        assert "tc0" in kept_ids
        assert "tc1" not in kept_ids
        assert "tc1" not in kept_ids  # tr1 同样不在

    def test_empty_keep_returns_unchanged(self):
        """keep_indices 为空 → 返回原 blocks (保守)."""
        blocks = [_tc("web_search", "tc0"), _tr("tc0", "429", "error")]
        out = apply_clip(blocks, [])
        assert out == blocks

    def test_toolcall_index_resets_after_text(self):
        """blocks 中间夹 text, 仍按 block index 定位."""
        # blocks 索引:   0   1    2    3   4    5   6
        # 内容:        tc0 tr0  text tc1 tr1  tc2 tr2
        # keep=[0,5] → 保留 tc0+tr0, tc2+tr2
        blocks = [
            _tc("web_search", "tc0"),
            _tr("tc0", "429", "error"),
            TextBlock(type="text", id="t0", text="中间"),
            _tc("web_search", "tc1"),
            _tr("tc1", "429", "error"),
            _tc("web_search", "tc2"),
            _tr("tc2", "429", "error"),
        ]
        keep = [0, 5]
        out = apply_clip(blocks, keep)
        kept_ids = [_bid(b) for b in out]
        assert "t0" in kept_ids  # 中间 text 保留
        assert "tc0" in kept_ids and "tc0" in kept_ids  # tc0 + 配对 tr0
        assert "tc2" in kept_ids and "tc2" in kept_ids  # tc2 + 配对 tr2
        assert "tc1" not in kept_ids


# ---------------------------------------------------------------------------
# clip_session: 整 session 入口
# ---------------------------------------------------------------------------


def _make_session_with_retry_loop() -> Session:
    """构造一段: 5 次连续 429 web_search, 不应被任何其它处理拦截."""
    tool_blocks = []
    for i in range(5):
        tool_blocks.append(_tc("web_search", f"tc{i}", json.dumps({"q": "test"})))
        tool_blocks.append(_tr(f"tc{i}", "Error 429: rate limited", state="error"))
    msg = Message(
        role="assistant",
        id="m0",
        blocks=[
            ThinkingBlock(type="thinking", id="th0", thinking="我得搜"),
            *tool_blocks,
        ],
    )
    return Session(session_id="sess-rl", messages=[msg])


class TestClipSession:
    def test_no_match_returns_zero(self):
        """session 无连续 429 段 → 不调 LLM, 返回 0."""
        client = _make_mock_client("")  # 若调用会抛
        msg = Message(role="assistant", id="m0", blocks=[
            _tc("web_search", "tc0"),
            _tr("tc0", "ok found", state="success"),
        ])
        sess = Session(session_id="s", messages=[msg])
        removed = clip_session(sess, client, min_consecutive=5)
        assert removed == 0
        client.generate.assert_not_called()

    def test_match_clip_reduces_blocks(self):
        """命中 + LLM 同意剪枝 → blocks 数量下降."""
        # segment 内部 keep=[0,4] 对应 block indices [1, 9] (thinking 占 0)
        client = _make_mock_client(json.dumps({
            "is_retry_loop": True,
            "keep_indices": [0, 4],
        }))
        sess = _make_session_with_retry_loop()
        before = len(sess.messages[0].blocks)
        removed = clip_session(sess, client, min_consecutive=5, max_keep=3)
        after = len(sess.messages[0].blocks)
        assert removed > 0
        assert after < before
        # 保留 th0 + tc0+tr0 + tc4+tr4 (共 5 个)
        kept_ids = [_bid(b) for b in sess.messages[0].blocks]
        assert "th0" in kept_ids
        assert kept_ids.count("tc0") == 2  # tc0 + 配对 tr0
        assert kept_ids.count("tc4") == 2  # tc4 + 配对 tr4
        assert "tc2" not in kept_ids

    def test_llm_rejects_keeps_unchanged(self):
        """LLM is_retry_loop=false → session 不变."""
        client = _make_mock_client(json.dumps({
            "is_retry_loop": False,
            "reason": "用户换了搜索方向",
        }))
        sess = _make_session_with_retry_loop()
        before_ids = [_bid(b) for b in sess.messages[0].blocks]
        removed = clip_session(sess, client, min_consecutive=5)
        assert removed == 0
        after_ids = [_bid(b) for b in sess.messages[0].blocks]
        assert before_ids == after_ids

    def test_llm_exception_keeps_unchanged(self):
        """LLM 抛异常 → session 不变 (保守 fallback)."""
        client = MagicMock()
        client.generate.side_effect = RuntimeError("LLM down")
        sess = _make_session_with_retry_loop()
        before_ids = [_bid(b) for b in sess.messages[0].blocks]
        removed = clip_session(sess, client, min_consecutive=5)
        assert removed == 0
        after_ids = [_bid(b) for b in sess.messages[0].blocks]
        assert before_ids == after_ids

    def test_disabled_skips(self):
        """enabled=False → 完全不调 LLM, 不动数据."""
        client = _make_mock_client("")  # 若调用会返回空字符串 → parse 失败
        sess = _make_session_with_retry_loop()
        before_ids = [_bid(b) for b in sess.messages[0].blocks]
        removed = clip_session(sess, client, min_consecutive=5, enabled=False)
        assert removed == 0
        after_ids = [_bid(b) for b in sess.messages[0].blocks]
        assert before_ids == after_ids
        client.generate.assert_not_called()


# ---------------------------------------------------------------------------
# 集成: 集成 clip_session 与 save_session, 验证 4 后缀文件无 ⟦⟧ 且 retry loop 被剪
# ---------------------------------------------------------------------------


def test_clip_then_save_session_roundtrip(tmp_path: Path):
    """clip_session 后调 save_session, 4 后缀文件均反映剪枝结果."""
    from gdr.domain.schema import save_session

    client = _make_mock_client(json.dumps({
        "is_retry_loop": True,
        "keep_indices": [0, 4],
    }))
    sess = _make_session_with_retry_loop()
    # 先初始化 metadata (qf_text / openai_messages 是 save_session 必需字段)
    sess.metadata = {
        "qf_text": "<dummy>\nuser\nhi\nassistant\nsearched\n<|im_end|>",
        "openai_messages": [{"role": "user", "content": "hi"}],
    }
    # 再跑 clip_session; 必须在 metadata 初始化之后, 以便写入 retry_loop_clip 审计
    removed = clip_session(sess, client, min_consecutive=5, max_keep=3)
    assert removed > 0

    base = tmp_path / "sess_rl_refined"
    save_session(sess, base)

    # 4 后缀文件都写出
    assert (tmp_path / "sess_rl_refined.messages.json").exists()
    assert (tmp_path / "sess_rl_refined.openai.json").exists()
    assert (tmp_path / "sess_rl_refined.qwenjina.txt").exists()
    meta = json.loads((tmp_path / "sess_rl_refined.meta.json").read_text(encoding="utf-8"))
    # meta.json 含 retry_loop_clip 标注 (供观测 LLM 决策)
    assert "retry_loop_clip" in meta
    assert meta["retry_loop_clip"]["total_removed"] > 0
