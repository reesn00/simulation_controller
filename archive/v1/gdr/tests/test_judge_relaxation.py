"""Fix B: judge_min_score 三段阶梯阈值.

阶梯 (按 modified_blocks):
  modified_count <= passthrough_threshold (1)  → passthrough_min (2)
  modified_count <= low_edit_threshold    (3)  → low_edit_min    (5)
  modified_count <= relaxed_threshold     (5)  → relaxed_min     (3)
  否则                                      → judge_min_score (7)

实现: gdr.reassembly.reassembler._judge_min_score_for
关闭阶梯: 对应 (threshold, min) 设为 0
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reassembly.reassembler import _judge_min_score_for


def _cfg(**overrides) -> SimpleNamespace:
    """构造最小 cfg (字段名匹配 _judge_min_score_for 读取的 7 个字段)."""
    defaults = dict(
        judge_min_score=7,
        judge_min_modified_passthrough=1,
        judge_min_score_passthrough=2,
        judge_min_modified_low_edit=3,
        judge_min_score_low_edit=5,
        judge_min_modified_for_relaxation=5,
        judge_min_score_relaxed=3,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestJudgeMinScoreStepped:
    def test_passthrough_bucket_when_minimal_edits(self):
        """modified=0/1 → passthrough 档, 阈值 = 2."""
        cfg = _cfg()
        assert _judge_min_score_for(cfg, 0) == (2, "passthrough")
        assert _judge_min_score_for(cfg, 1) == (2, "passthrough")

    def test_low_edit_bucket(self):
        """modified=2/3 → low_edit 档, 阈值 = 5."""
        cfg = _cfg()
        assert _judge_min_score_for(cfg, 2) == (5, "low_edit")
        assert _judge_min_score_for(cfg, 3) == (5, "low_edit")

    def test_relaxed_bucket(self):
        """modified=4/5 → relaxed 档, 阈值 = 3."""
        cfg = _cfg()
        assert _judge_min_score_for(cfg, 4) == (3, "relaxed")
        assert _judge_min_score_for(cfg, 5) == (3, "relaxed")

    def test_strict_bucket_above_relaxed(self):
        """modified > 5 → 严格阈值, 不放宽."""
        cfg = _cfg()
        assert _judge_min_score_for(cfg, 6) == (7, None)
        assert _judge_min_score_for(cfg, 100) == (7, None)

    def test_disable_passthrough_zero_threshold(self):
        """passthrough_threshold=0 关闭 passthrough 档 → modified=1 落入 low_edit."""
        cfg = _cfg(judge_min_modified_passthrough=0)
        assert _judge_min_score_for(cfg, 0)[1] == "low_edit"
        assert _judge_min_score_for(cfg, 1)[1] == "low_edit"

    def test_disable_passthrough_zero_min(self):
        """passthrough_min=0 也关闭该档 (与 disabled threshold 同样行为)."""
        cfg = _cfg(judge_min_score_passthrough=0)
        assert _judge_min_score_for(cfg, 0)[1] == "low_edit"
        assert _judge_min_score_for(cfg, 1)[1] == "low_edit"

    def test_disable_all_buckets_returns_strict(self):
        """全部阶梯关闭 → 任何 modified 都走严格阈值."""
        cfg = _cfg(
            judge_min_modified_passthrough=0,
            judge_min_modified_low_edit=0,
            judge_min_modified_for_relaxation=0,
        )
        for m in range(0, 20):
            assert _judge_min_score_for(cfg, m) == (7, None)

    def test_custom_thresholds(self):
        """自定义阈值阶梯仍按 modified_count 落档."""
        cfg = _cfg(
            judge_min_score=8,
            judge_min_modified_passthrough=2,
            judge_min_score_passthrough=3,
            judge_min_modified_low_edit=5,
            judge_min_score_low_edit=6,
            judge_min_modified_for_relaxation=10,
            judge_min_score_relaxed=4,
        )
        assert _judge_min_score_for(cfg, 0) == (3, "passthrough")
        assert _judge_min_score_for(cfg, 2) == (3, "passthrough")
        assert _judge_min_score_for(cfg, 3) == (6, "low_edit")
        assert _judge_min_score_for(cfg, 5) == (6, "low_edit")
        assert _judge_min_score_for(cfg, 6) == (4, "relaxed")  # modified<=10 仍走 relaxed
        assert _judge_min_score_for(cfg, 11) == (8, None)

    def test_legacy_single_layer_still_works(self):
        """把 low_edit / passthrough 都关掉 → 退回原单层 (modified<=5 用 3, 否则用 7)."""
        cfg = _cfg(
            judge_min_modified_passthrough=0,
            judge_min_modified_low_edit=0,
        )
        # modified<=5 走 relaxed_min=3 (与既有 judge_min_score_relaxed 兼容)
        assert _judge_min_score_for(cfg, 0) == (3, "relaxed")
        assert _judge_min_score_for(cfg, 5) == (3, "relaxed")
        # modified>5 走严格阈值 7
        assert _judge_min_score_for(cfg, 6) == (7, None)


class TestSettingsNewFields:
    """验证 settings.py 中 Fix B 新增字段都被 Settings 识别."""

    def test_settings_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
        (tmp_path / "root.yaml").write_text(
            "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
        )
        from config import Settings
        cfg = Settings()
        assert cfg.judge_min_modified_passthrough == 1
        assert cfg.judge_min_score_passthrough == 2
        assert cfg.judge_min_modified_low_edit == 3
        assert cfg.judge_min_score_low_edit == 5
        assert cfg.judge_relaxed_audit_note is True
        # 既有字段保留默认值
        assert cfg.judge_min_modified_for_relaxation == 5
        assert cfg.judge_min_score_relaxed == 3

    def test_settings_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
        (tmp_path / "root.yaml").write_text(
            "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
        )
        monkeypatch.setenv("GDR_JUDGE_MIN_MODIFIED_PASSTHROUGH", "2")
        monkeypatch.setenv("GDR_JUDGE_MIN_SCORE_PASSTHROUGH", "4")
        monkeypatch.setenv("GDR_JUDGE_MIN_SCORE_LOW_EDIT", "6")
        monkeypatch.setenv("GDR_JUDGE_RELAXED_AUDIT_NOTE", "false")
        from config import Settings
        cfg = Settings()
        assert cfg.judge_min_modified_passthrough == 2
        assert cfg.judge_min_score_passthrough == 4
        assert cfg.judge_min_score_low_edit == 6
        assert cfg.judge_relaxed_audit_note is False
