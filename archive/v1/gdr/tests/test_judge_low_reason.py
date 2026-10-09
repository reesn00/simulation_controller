"""Fix A: judge_low.jsonl 字段展开 + L3 judge reason 串入 metadata.

设计:
  - 旧 judge_low.jsonl: judge = {score, min_score}
  - 新 judge_low.jsonl: judge = {score, min_score, reason, relaxed_kind,
    modified_blocks, [exception, exception_type]}
  - judge_low_include_reason=False 退回旧行为

触发点:
  - gdr.reassembly.reassembler.reassemble: 把 result["reason"] 写入
    session.metadata["judge_discard"]["reason"]; 阶梯档位写入
    relaxed_kind; modified_blocks 一并写入
  - gdr.pipeline.runner._append_judge_low_queue: 把 judge_discard 展开为
    顶层 judge 字段
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config import Settings
from domain import Session, Message
from pipeline.runner import _append_judge_low_queue


def _session(session_id: str = "low-test") -> Session:
    msgs = [
        Message(role="user", id="m0", blocks=[{"type": "text", "id": "t0", "text": "Q"}]),
        Message(
            role="assistant", id="m1",
            blocks=[{"type": "text", "id": "t1", "text": "A."}],
        ),
    ]
    return Session(session_id=session_id, source_file="qf.json", messages=msgs)


def _cfg(tmp_path: Path, *, include_reason: bool = True) -> Settings:
    cfg = Settings()
    monkey_env = {"GDR_CONFIG_FILE": str(tmp_path / "root.yaml")}
    import os
    os.environ.update(monkey_env)
    (tmp_path / "root.yaml").write_text(
        "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
    )
    # 重新加载以应用 env
    cfg = Settings()
    cfg.judge_low_output_path = tmp_path / "judge_low.jsonl"
    cfg.judge_low_include_reason = include_reason
    return cfg


class TestJudgeLowReasonExpansion:
    """judge_low.jsonl 应包含 LLM 评语 + 阶梯档位 + 修改块数."""

    def test_includes_reason_and_relaxed_kind(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
        (tmp_path / "root.yaml").write_text(
            "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
        )
        cfg = Settings()
        cfg.judge_low_output_path = tmp_path / "judge_low.jsonl"
        cfg.judge_low_include_reason = True

        s = _session()
        s.metadata["judge_discard"] = {
            "score": 2,
            "min_score": 7,
            "reason": "trajectory 自洽度不足, 工具调用顺序与用户意图冲突",
            "relaxed_kind": None,
            "modified_blocks": 0,
        }
        _append_judge_low_queue(s, cfg)

        records = [json.loads(ln) for ln in cfg.judge_low_output_path.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 1
        judge = records[0]["judge"]
        assert judge["score"] == 2
        assert judge["min_score"] == 7
        assert "自洽度不足" in judge["reason"]
        assert judge["relaxed_kind"] is None
        assert judge["modified_blocks"] == 0

    def test_relaxed_kind_passed_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
        (tmp_path / "root.yaml").write_text(
            "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
        )
        cfg = Settings()
        cfg.judge_low_output_path = tmp_path / "judge_low.jsonl"
        cfg.judge_low_include_reason = True

        s = _session()
        s.metadata["judge_discard"] = {
            "score": 2,
            "min_score": 2,  # passthrough 档
            "reason": "搜索任务, judge 判分噪声大",
            "relaxed_kind": "passthrough",
            "modified_blocks": 1,
        }
        _append_judge_low_queue(s, cfg)
        records = [json.loads(ln) for ln in cfg.judge_low_output_path.read_text(encoding="utf-8").splitlines()]
        assert records[0]["judge"]["relaxed_kind"] == "passthrough"

    def test_exception_fields_passed_through(self, tmp_path, monkeypatch):
        """严格模式下 judge 抛异常时, exception/exception_type 也应进入 judge_low."""
        monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
        (tmp_path / "root.yaml").write_text(
            "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
        )
        cfg = Settings()
        cfg.judge_low_output_path = tmp_path / "judge_low.jsonl"
        cfg.judge_low_include_reason = True

        s = _session()
        s.metadata["judge_discard"] = {
            "reason": "consistency_check_failed",
            "exception": "ReadTimeout: judge LLM 30s 未响应",
            "exception_type": "ReadTimeout",
        }
        _append_judge_low_queue(s, cfg)
        records = [json.loads(ln) for ln in cfg.judge_low_output_path.read_text(encoding="utf-8").splitlines()]
        judge = records[0]["judge"]
        assert judge["reason"] == "consistency_check_failed"
        assert "ReadTimeout" in judge["exception"]
        assert judge["exception_type"] == "ReadTimeout"

    def test_disabled_falls_back_to_legacy_payload(self, tmp_path, monkeypatch):
        """judge_low_include_reason=False 时, 仅写 {score, min_score}, 不含 reason 等."""
        monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
        (tmp_path / "root.yaml").write_text(
            "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
        )
        cfg = Settings()
        cfg.judge_low_output_path = tmp_path / "judge_low.jsonl"
        cfg.judge_low_include_reason = False

        s = _session()
        s.metadata["judge_discard"] = {
            "score": 2,
            "min_score": 7,
            "reason": "should not appear",
            "relaxed_kind": "passthrough",
            "modified_blocks": 0,
        }
        _append_judge_low_queue(s, cfg)
        records = [json.loads(ln) for ln in cfg.judge_low_output_path.read_text(encoding="utf-8").splitlines()]
        judge = records[0]["judge"]
        assert judge == {"score": 2, "min_score": 7}  # 与旧行为一致

    def test_missing_reason_defaults_to_empty_string(self, tmp_path, monkeypatch):
        """旧 judge_discard 中无 reason 字段时 (迁移期), 落空串而非 KeyError."""
        monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
        (tmp_path / "root.yaml").write_text(
            "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
        )
        cfg = Settings()
        cfg.judge_low_output_path = tmp_path / "judge_low.jsonl"
        cfg.judge_low_include_reason = True

        s = _session()
        s.metadata["judge_discard"] = {"score": 3, "min_score": 7}  # 旧字段
        _append_judge_low_queue(s, cfg)
        records = [json.loads(ln) for ln in cfg.judge_low_output_path.read_text(encoding="utf-8").splitlines()]
        judge = records[0]["judge"]
        assert judge["reason"] == ""
        assert judge["relaxed_kind"] is None
        assert judge["modified_blocks"] is None

    def test_settings_field_present_and_default_true(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
        (tmp_path / "root.yaml").write_text(
            "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
        )
        cfg = Settings()
        assert cfg.judge_low_include_reason is True

    def test_settings_field_overridable_via_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "root.yaml"))
        (tmp_path / "root.yaml").write_text(
            "llm:\n  base_url: http://x/v1\n  model: m\n", encoding="utf-8",
        )
        monkeypatch.setenv("GDR_JUDGE_LOW_INCLUDE_REASON", "false")
        cfg = Settings()
        assert cfg.judge_low_include_reason is False
