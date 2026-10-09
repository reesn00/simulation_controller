"""Regression tests: incomplete session safeguard (direction #integrity-detection).

Reproducer (2026-09-19 T001 useramulation-fc37...): the remote sim side submits
trajectories whose last block is a toolcall with no matching toolresult as
"completed"; GDR writes them into refine_data, but the session is not closed.
SFT using these half-open samples poisons training.

Coverage:
  - _detect_incomplete_session three trigger conditions
  - _is_text_incomplete heuristic (long text without terminal punctuation /
    ellipsis / continue-marker prefix)
  - _append_incomplete_queue jsonl write
  - _process_one_file returns status=incomplete on incomplete, skips refine_data
  - cfg.incomplete_detection_enabled=False preserves legacy behaviour
  - _aggregate counts the new incomplete bucket
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import Settings
from domain import Session, Message
from pipeline.runner import (
    _detect_incomplete_session,
    _is_text_incomplete,
    _append_incomplete_queue,
    _aggregate,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _session(*messages) -> Session:
    """Build a Session; messages is a sequence of (role, [blocks])."""
    msgs = []
    for i, (role, blocks) in enumerate(messages):
        msgs.append(Message(role=role, id=f"m{i}", blocks=blocks))
    return Session(session_id=f"incomplete-test-{id(messages)}", messages=msgs)


def _tc(name="browser", tid="toolu_x", input_='{"q":"x"}', state="finished"):
    return {"type": "toolcall", "id": tid, "name": name, "input": input_, "state": state}


def _tr(tid="toolu_x", name="browser", output="ok", state="success"):
    return {"type": "toolresult", "id": tid, "name": name, "output_text": output, "state": state}


def _thinking(text="Reasoning here"):
    return {"type": "thinking", "id": "th_x", "thinking": text}


def _text(text="Final answer."):
    return {"type": "text", "id": "tx_x", "text": text}


# ---------------------------------------------------------------------------
# _is_text_incomplete heuristic
# ---------------------------------------------------------------------------

class TestIsTextIncomplete:
    def test_empty_string_is_incomplete(self):
        assert _is_text_incomplete("") is True
        assert _is_text_incomplete("   ") is True

    def test_short_string_without_punct_is_complete(self):
        """Short text (<30 chars) tolerates missing terminal punctuation —
        could be 'OK', 'done', 'ok.', single word answers."""
        assert _is_text_incomplete("ok") is False
        assert _is_text_incomplete("OK") is False
        assert _is_text_incomplete("done") is False
        assert _is_text_incomplete("all good") is False

    def test_long_string_without_punct_is_incomplete(self):
        """Long text (>30 chars) with no terminal punctuation -> likely
        truncated by token budget."""
        text = (
            "let me first clarify one thing about the request: the user "
            "wants free full streaming links for unlicensed content"
        )
        assert len(text) > 30
        assert _is_text_incomplete(text) is True

    def test_long_string_with_period_is_complete(self):
        text = "let me first clarify one thing: this is half a sentence." * 5
        assert _is_text_incomplete(text) is False

    def test_ellipsis_is_incomplete(self):
        assert _is_text_incomplete("let me check...") is True
        assert _is_text_incomplete("waiting for results...") is True

    def test_continue_marker_prefix_is_incomplete(self):
        """Prefix containing continue markers -> mid-thought, even if text
        is short. (Note: heuristic also triggers on long-no-punct; this
        specifically exercises the marker-prefix path.)"""
        text = "let me check the actual download links for this first"
        assert _is_text_incomplete(text) is True

    def test_normal_complete_answer(self):
        text = (
            "there is no licensed platform offering the full series for free "
            "in this region. iqiyi removed it, bilibili hosts the movie cut "
            "only, and tencent only carries user clips. end of summary."
        )
        assert _is_text_incomplete(text) is False


# ---------------------------------------------------------------------------
# _detect_incomplete_session three trigger conditions
# ---------------------------------------------------------------------------

class TestDetectIncomplete:
    def test_complete_session_returns_none(self):
        """Complete session -- ends with text, toolcall pairing is balanced."""
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [_thinking(), _tc(), _tr(), _text("Final answer.")]),
        )
        assert _detect_incomplete_session(s) is None

    def test_trailing_toolcall_is_incomplete(self):
        """Ends with toolcall, no result yet -- reproduces fc37 core symptom."""
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _thinking(),
                _tc("browser", "tc1"),
                _tr("tc1"),
                _thinking(),
                _tc("execute_shell_command", "tc2"),  # trailing, no tc2 result
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is not None
        assert any("last_assistant_block_is_toolcall" in r for r in diag["reasons"])
        assert "execute_shell_command" in diag["reasons"][0]

    def test_toolcall_count_exceeds_toolresult_count(self):
        """toolcall count > toolresult count -- pairing deficit."""
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("tc1"),
                _tr("tc1"),
                _tc("tc2"),  # no tc2 result
                _text("Final answer."),
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is not None
        assert any("toolcall_result_mismatch" in r for r in diag["reasons"])
        # ends with text, so dimension 1 must NOT trigger
        assert not any("last_assistant_block_is_toolcall" in r for r in diag["reasons"])

    def test_multiple_reasons_combined(self):
        """Multiple triggers coexist: trailing toolcall + pairing deficit."""
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("tc1"),
                _tr("tc1"),
                _tc("tc2"),  # no tc2 result -> dimension 2 triggers
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is not None
        reasons = diag["reasons"]
        assert any("toolcall_result_mismatch" in r for r in reasons)
        assert any("last_assistant_block_is_toolcall" in r for r in reasons)

    def test_incomplete_text_heuristic(self):
        """Trailing text does not form a complete reply."""
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("tc1"),
                _tr("tc1"),
                _text(
                    "let me check the actual download links for this first "
                    "i need to verify a few things about the upstream sources"
                ),
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is not None
        assert any("last_text_incomplete" in r for r in diag["reasons"])

    def test_no_assistant_returns_none(self):
        """No assistant messages -- abnormal but not 'incomplete'."""
        s = _session(("user", [_text("query")]))
        assert _detect_incomplete_session(s) is None

    def test_empty_assistant_blocks_returns_none(self):
        s = _session(
            ("user", [_text("query")]),
            ("assistant", []),
        )
        assert _detect_incomplete_session(s) is None

    def test_complete_session_normalises_inputs(self):
        """Detector should not crash on blocks with extra fields and should
        classify balanced call/result pairs as complete even when input/output
        shapes vary."""
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc(name="browser", tid="tc1"),
                _tr(tid="tc1"),
                _tc(name="browser", tid="tc2"),
                _tr(tid="tc2"),
                _thinking(""),
                _text("All done."),
            ]),
        )
        assert _detect_incomplete_session(s) is None


# ---------------------------------------------------------------------------
# F2 fix: 维度 4 -- 末尾 assistant 仅 thinking, 无 final text, 无未配对 toolcall
# ---------------------------------------------------------------------------


class TestF2ThinkingOnlyTail:
    """复现链 (2026-09-19 T001): 最后一条 assistant 是 thinking-only 空 content,
    原 3 维全部跳过, 误判完整. 维度 4 在 thinking_chars >= 阈值时触发."""

    def _patch_runner_cfg(self, monkeypatch, threshold: int = 200):
        from types import SimpleNamespace
        from pipeline import runner as runner_mod
        monkeypatch.setattr(
            runner_mod,
            "_current_runner_cfg",
            lambda: SimpleNamespace(incomplete_thinking_only_min_chars=threshold),
        )

    def test_thinking_only_long_is_flagged_incomplete(self, monkeypatch):
        self._patch_runner_cfg(monkeypatch, threshold=200)
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("browser", "tc1"),
                _tr("tc1"),
                _thinking("x" * 500),  # 仅 thinking, 无 text
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is not None
        assert any(
            r.startswith("last_assistant_no_final_text") for r in diag["reasons"]
        )

    def test_short_thinking_tail_not_flagged(self, monkeypatch):
        """thinking_chars < threshold 视为快速结尾思考, 不算 incomplete."""
        self._patch_runner_cfg(monkeypatch, threshold=200)
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("browser", "tc1"),
                _tr("tc1"),
                _thinking("x" * 50),  # 短思考
                _text("Done."),
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is None or not any(
            r.startswith("last_assistant_no_final_text") for r in diag["reasons"]
        )

    def test_real_text_tail_not_flagged_by_dim4(self, monkeypatch):
        """末尾有完整 text, 维度 4 不命中 (即使被其他维度命中也单独验证)."""
        self._patch_runner_cfg(monkeypatch, threshold=200)
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("browser", "tc1"),
                _tr("tc1"),
                _thinking("x" * 500),
                _text(
                    "已核实: iQiyi 有 81 集链接, 但 payMark 有 0 和 1 两种, "
                    "需要逐集对应付费状态。"
                ),
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is None or not any(
            r.startswith("last_assistant_no_final_text") for r in diag["reasons"]
        )

    def test_pending_toolcall_skips_dim4(self, monkeypatch):
        """末尾有未配对 toolcall 时, 维度 4 不命中 (维度 1/2 已覆盖)."""
        self._patch_runner_cfg(monkeypatch, threshold=200)
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("browser", "tc1"),
                _tr("tc1"),
                _thinking("x" * 500),
                _tc("browser", "tc2"),  # 未配对 toolcall
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is not None
        # 维度 4 不应在 reasons 里
        assert not any(
            r.startswith("last_assistant_no_final_text") for r in diag["reasons"]
        )
        # 维度 1/2 命中
        assert any(
            r.startswith("last_assistant_block_is_toolcall") for r in diag["reasons"]
        ) or any(
            r.startswith("toolcall_result_mismatch") for r in diag["reasons"]
        )

    def test_threshold_zero_still_triggers_when_any_thinking(self, monkeypatch):
        """threshold=0 时任何 thinking 都触发 (供敏感模式使用)."""
        self._patch_runner_cfg(monkeypatch, threshold=0)
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("browser", "tc1"),
                _tr("tc1"),
                _thinking("a"),
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is not None
        assert any(
            r.startswith("last_assistant_no_final_text") for r in diag["reasons"]
        )


# ---------------------------------------------------------------------------
# F3-E fix: 维度 2 豁免 — 未配对 toolcall 之后有完整收尾 text → 不算 incomplete
# ---------------------------------------------------------------------------


class TestF3EMismatchWithCompleteClose:
    """复现链 (2026-09-19 T001 03f3f0fd): 31 个 toolcall / 30 个 toolresult,
    但末尾 text 块以结构闭合符号 (⟦...⟧) 收口, agent 主动放弃等结果.
    修复: 维度 2 加豁免, 末尾 text 含完整收尾信号时跳过维度 2.
    """

    def test_unpaired_toolcall_followed_by_complete_text_not_flagged(self):
        """未配对 toolcall + 末尾结构闭合 text → 不算 incomplete."""
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("browser", "tc1"),
                _tr("tc1"),
                _tc("execute_shell_command", "tc2"),  # 未配对 toolcall
                _thinking("系统拦下了 del, 决定放弃重试"),
                _thinking("现在给出最终回复"),
                _text(
                    "删除临时文件被系统拦下了（del 被判为高危），"
                    "我留在工作区里没管它——下面说结果。\n\n"
                    "## 结论\n\n"
                    "央视网 (CCTV) 有正版 80 集。\n\n"
                    "⟦ T001 免费全集｜已完成：给出央视网入口; 已排除盗版;"
                    "待办: 海外 IP 下央视网被拦需替代渠道｜锚点: VIDA1354531513618469 ⟧"
                ),
            ]),
        )
        diag = _detect_incomplete_session(s)
        # F3-E: 维度 2 被豁免, 维度 3 也由 F3-D 兜底 → 应返回 None
        assert diag is None, f"期望 None, 实际 {diag}"

    def test_unpaired_toolcall_followed_by_complete_text_via_keywords(self):
        """未配对 toolcall + 末尾含 '下一步' 等语义收尾词 → 不算 incomplete."""
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("browser", "tc1"),
                _tr("tc1"),
                _tc("execute_shell_command", "tc2"),  # 未配对
                _text("排查完成。下一步：等用户确认是否需要建定时任务监控。"),
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is None, f"期望 None, 实际 {diag}"

    def test_unpaired_toolcall_followed_by_markdown_report_not_flagged(self):
        """未配对 toolcall + 末尾 markdown 报告 (表格+分隔线) → 不算 incomplete."""
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("browser", "tc1"),
                _tr("tc1"),
                _tc("execute_shell_command", "tc2"),  # 未配对
                _text(
                    "## 结果\n\n"
                    "| 平台 | 状态 |\n|---|---|\n"
                    "| A | OK |\n| B | 404 |\n\n"
                    "---"
                ),
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is None, f"期望 None, 实际 {diag}"

    def test_unpaired_toolcall_with_incomplete_text_still_flagged(self):
        """未配对 toolcall + 末尾是截断 text (无收尾信号) → 仍判 incomplete."""
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("browser", "tc1"),
                _tr("tc1"),
                _tc("execute_shell_command", "tc2"),  # 未配对
                # 长文无标点 + 无结构闭合 + 无语义收尾词
                _text(
                    "let me first clarify one thing about the request the user "
                    "wants free full streaming links for unlicensed content"
                ),
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is not None
        # 维度 2 命中 (无豁免信号)
        assert any(
            r.startswith("toolcall_result_mismatch") for r in diag["reasons"]
        )

    def test_balanced_toolcall_text_incomplete_still_flagged(self):
        """配对平衡 (无维度 2 触发) 但末尾 text 不完整 → 维度 3 仍命中.

        防止 F3-E 误改: 豁免只针对维度 2, 维度 3 由 _is_text_incomplete
        独立判定. 这里验证修复未影响维度 3 的硬指标路径.
        """
        s = _session(
            ("user", [_text("query")]),
            ("assistant", [
                _tc("browser", "tc1"),
                _tr("tc1"),
                _text(
                    "let me first clarify one thing about the request the user "
                    "wants free full streaming links"
                ),
            ]),
        )
        diag = _detect_incomplete_session(s)
        assert diag is not None
        # 维度 3 触发 (末尾 text 启发式判不完整)
        assert any(
            r.startswith("last_text_incomplete") for r in diag["reasons"]
        )
        # 维度 2 不触发 (1 vs 1 配对)
        assert not any(
            r.startswith("toolcall_result_mismatch") for r in diag["reasons"]
        )

    def test_real_t001_artifact_reproduction(self):
        """复现 T001 03f3f0fd 实际结构: 末尾 toolcall (del) 未配对 + 完整 text 收尾."""
        s = _session(
            ("user", [_text("找到电视剧《武林外传》全集在线免费观看的可播放网址")]),
            ("assistant", [
                _thinking("整理已验证的事实清单"),
                _tc("execute_shell_command", "del-cleanup"),  # 未配对
                _thinking("del 被系统拦下, 决定放弃重试"),
                _thinking("现在写最终回复"),
                _text(
                    "删除临时文件被系统拦下了（`del` 被判为高危，审批超时），"
                    "我留在工作区里没管它——下面说结果。\n\n"
                    "## 能免费看全集的正版网址：央视网\n\n"
                    "https://tv.cctv.com/2012-12-03/VIDA1354531513618469.shtml\n\n"
                    "⟦ 武林外传免费全集｜已完成: 给出央视网入口; 已排除盗版; "
                    "待办: 海外 IP 需替代渠道｜锚点: VIDA1354531513618469 ⟧"
                ),
            ]),
        )
        diag = _detect_incomplete_session(s)
        # F3-E + F3-D 联合修复后: 维度 2 豁免, 维度 3 由结构闭合绕过
        assert diag is None, f"期望 None, 实际 {diag}"


# ---------------------------------------------------------------------------
# _append_incomplete_queue jsonl write
# ---------------------------------------------------------------------------

class TestAppendIncompleteQueue:
    def test_appends_to_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
        (tmp_path / "root.yaml").write_text(
            "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
        )
        cfg = Settings()
        out_path = tmp_path / "incomplete.jsonl"
        cfg.incomplete_output_path = out_path

        session = _session(
            ("user", [_text("query")]),
            ("assistant", [_tc(), _text("title")]),
        )
        diag = {"is_incomplete": True, "reasons": ["test"]}
        _append_incomplete_queue(session, diag, cfg)

        assert out_path.exists()
        records = [json.loads(ln) for ln in out_path.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 1
        rec = records[0]
        assert rec["session_id"] == session.session_id
        assert rec["diagnostic"] == diag
        assert "session" in rec
        assert rec["session"]["messages"][0]["role"] == "user"

    def test_disabled_skips_append(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
        (tmp_path / "root.yaml").write_text(
            "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
        )
        cfg = Settings()
        cfg.incomplete_detection_enabled = False
        out_path = tmp_path / "incomplete.jsonl"
        cfg.incomplete_output_path = out_path

        session = _session(("assistant", [_tc()]))
        _append_incomplete_queue(
            session, {"is_incomplete": True, "reasons": ["x"]}, cfg,
        )
        assert not out_path.exists()


# ---------------------------------------------------------------------------
# _aggregate rollup
# ---------------------------------------------------------------------------

class TestAggregateIncomplete:
    def test_aggregate_counts_incomplete(self):
        results = [
            {"status": "success"},
            {"status": "success"},
            {"status": "discard"},
            {"status": "incomplete"},
            {"status": "load_error", "error": "x"},
            None,
        ]
        agg = _aggregate(results)
        assert agg["total"] == 5  # None skipped
        assert agg["success"] == 2
        assert agg["discard"] == 1
        assert agg["incomplete"] == 1
        assert agg["error"] == 1
        assert agg["kept_ratio"] == 0.4


# ---------------------------------------------------------------------------
# Settings fields
# ---------------------------------------------------------------------------

def test_settings_incomplete_detection_defaults_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
    (tmp_path / "root.yaml").write_text(
        "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
    )
    cfg = Settings()
    assert cfg.incomplete_detection_enabled is True
    assert cfg.incomplete_output_path == Path("./refine_data/incomplete.jsonl")


def test_settings_incomplete_detection_overridable_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
    (tmp_path / "root.yaml").write_text(
        "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
    )
    monkeypatch.setenv("GDR_INCOMPLETE_DETECTION_ENABLED", "false")
    monkeypatch.setenv("GDR_INCOMPLETE_OUTPUT_PATH", "/tmp/custom.jsonl")
    cfg = Settings()
    assert cfg.incomplete_detection_enabled is False
    assert cfg.incomplete_output_path == Path("/tmp/custom.jsonl")
