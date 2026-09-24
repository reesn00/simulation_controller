"""etl 训练集准入门控 (gate_then_load) 单元测试 (方案 etl-prune-frontload.md §5.2).

覆盖:
    - reject 决策 → 返回 None + audit 兜底落盘
    - accept + trajectory_compare.overall=fail → 返回 Session + meta 标 compare_warn
    - accept (默认) → 返回 Session 原样
    - schema_version 不匹配 → ValueError
    - 比较器对比 accept + fail 同存的归类
    - audit 落盘的 record 字段 (origin="etl_fallback")
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# 使用 gdr.domain.schema 直接路径避免 conftest.py 的 sys.path hack 造成
# 双重加载 (sys.modules 中 ``domain.schema`` 与 ``gdr.domain.schema``
# 键名不同, pydantic 类不可互换)。
from etl.parsers import (
    append_scoring_reject_fallback,
    gate_then_load,
    load_refined_session,
)
from gdr.domain.schema import Message, Session, TextBlock


# ============================================================
# Helpers
# ============================================================

def _make_session_payload(
    *,
    session_id: str = "test",
    schema_version: str = "refined_session.v1",
    trajectory_free: dict | None = None,
    trajectory_compare: dict | None = None,
    messages: list | None = None,
) -> dict:
    return {
        "schema_version": schema_version,
        "session_id": session_id,
        "messages": messages or [
            {"role": "user", "id": "m1", "blocks": [{"type": "text", "id": "t1", "text": "q"}]},
        ],
        "metadata": {
            "trajectory_free": trajectory_free or {},
            "trajectory_compare": trajectory_compare or {},
        },
    }


@pytest.fixture
def c2_path_with_session(tmp_path: Path):  # noqa: F821
    """fixture: 写一个 C2 refined Session 到 tmp_path, 返回 path + payload."""
    pass  # 由具体测试通过 _write_session 写入


def _write_session(tmp_path, payload: dict):
    p = tmp_path / "c2_sample.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


# ============================================================
# load_refined_session
# ============================================================

class TestLoadRefinedSession:
    def test_accepts_v1(self, tmp_path):
        payload = _make_session_payload()
        p = _write_session(tmp_path, payload)
        session = load_refined_session(p)
        assert isinstance(session, Session)
        assert session.session_id == "test"

    def test_rejects_wrong_version(self, tmp_path):
        payload = _make_session_payload(schema_version="refined_session.v0")
        p = _write_session(tmp_path, payload)
        with pytest.raises(ValueError, match="unsupported"):
            load_refined_session(p)


# ============================================================
# gate_then_load: 三类决策
# ============================================================

class TestGateThenLoad:
    def test_accept_returns_session(self, tmp_path):
        payload = _make_session_payload(
            trajectory_free={"decision": "accept", "absolute_quality": {"score": 5}},
        )
        p = _write_session(tmp_path, payload)
        session = gate_then_load(p)
        assert session is not None
        assert session.session_id == "test"
        # accept 时不应标记 compare_warn
        assert session.metadata.get("compare_warn") is None

    def test_reject_returns_none_and_writes_audit(self, tmp_path, monkeypatch):
        audit_path = tmp_path / "audit" / "scoring_reject.jsonl"
        monkeypatch.setattr(
            "etl.parsers._current_settings_for_etl",
            lambda: SimpleNamespace(
                scoring_reject_output_path=str(audit_path),
                scoring_reject_audit_enabled=True,
            ),
        )
        payload = _make_session_payload(
            session_id="reject-1",
            trajectory_free={
                "decision": "reject",
                "redline": {"violation": True, "labels": ["pii_email"]},
                "absolute_quality": {
                    "score": 2,
                    "subscores": {"executability": 3},
                    "fail_reasons": ["executability_low"],
                },
            },
        )
        p = _write_session(tmp_path, payload)
        result = gate_then_load(p)
        assert result is None

        # 兜底 audit 落盘
        assert audit_path.exists()
        records = [
            json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(records) == 1
        rec = records[0]
        assert rec["session_id"] == "reject-1"
        assert rec["scoring_reject"]["decision"] == "reject"
        assert rec["scoring_reject"]["redline_violation"] is True
        assert rec["scoring_reject"]["origin"] == "etl_fallback"
        assert rec["scoring_reject"]["redline_labels"] == ["pii_email"]

    def test_reject_disabled_audit_skips(self, tmp_path, monkeypatch):
        audit_path = tmp_path / "audit" / "should_not_exist.jsonl"
        monkeypatch.setattr(
            "etl.parsers._current_settings_for_etl",
            lambda: SimpleNamespace(
                scoring_reject_output_path=str(audit_path),
                scoring_reject_audit_enabled=False,
            ),
        )
        payload = _make_session_payload(
            trajectory_free={"decision": "reject"},
        )
        p = _write_session(tmp_path, payload)
        result = gate_then_load(p)
        # disabled 时仍 return None (审计是兜底功能, 主决策不变)
        assert result is None
        assert not audit_path.exists()

    def test_accept_with_compare_fail_marks_warn(self, tmp_path):
        payload = _make_session_payload(
            trajectory_free={"decision": "accept", "absolute_quality": {"score": 5}},
            trajectory_compare={
                "overall": "fail",
                "instruction_adherence": {
                    "diff_summary": ["order_changed: step 3 before step 2"],
                },
            },
        )
        p = _write_session(tmp_path, payload)
        session = gate_then_load(p)
        assert session is not None
        meta = session.metadata
        assert meta.get("compare_warn") is True
        assert "order_changed" in meta.get("compare_diff_summary", [""])[0]

    def test_accept_with_compare_pass_no_warn(self, tmp_path):
        payload = _make_session_payload(
            trajectory_free={"decision": "accept"},
            trajectory_compare={"overall": "pass"},
        )
        p = _write_session(tmp_path, payload)
        session = gate_then_load(p)
        assert session is not None
        assert session.metadata.get("compare_warn") is None

    def test_reject_takes_precedence_over_compare(self, tmp_path, monkeypatch):
        """reject 决策不能因为 compare=pass 而被遮蔽."""
        audit_path = tmp_path / "audit" / "scoring_reject.jsonl"
        monkeypatch.setattr(
            "etl.parsers._current_settings_for_etl",
            lambda: SimpleNamespace(
                scoring_reject_output_path=str(audit_path),
                scoring_reject_audit_enabled=True,
            ),
        )
        payload = _make_session_payload(
            trajectory_free={"decision": "reject"},
            trajectory_compare={"overall": "pass"},
        )
        p = _write_session(tmp_path, payload)
        result = gate_then_load(p)
        assert result is None
        assert audit_path.exists()


# ============================================================
# append_scoring_reject_fallback 直接接口
# ============================================================

class TestAppendScoringRejectFallback:
    def test_disabled_noop(self, tmp_path, monkeypatch):
        audit_path = tmp_path / "audit" / "skip.jsonl"
        monkeypatch.setattr(
            "etl.parsers._current_settings_for_etl",
            lambda: SimpleNamespace(
                scoring_reject_output_path=str(audit_path),
                scoring_reject_audit_enabled=False,
            ),
        )
        # 构造一个最小 session
        session = Session(
            session_id="noop-1",
            messages=[Message(role="user", id="u1", blocks=[TextBlock(type="text", id="t1", text="q")])],
            metadata={"trajectory_free": {"decision": "reject"}},
        )
        append_scoring_reject_fallback(session, tmp_path / "dummy.json")
        assert not audit_path.exists()

    def test_writes_full_record(self, tmp_path, monkeypatch):
        audit_path = tmp_path / "audit" / "scoring_reject.jsonl"
        monkeypatch.setattr(
            "etl.parsers._current_settings_for_etl",
            lambda: SimpleNamespace(
                scoring_reject_output_path=str(audit_path),
                scoring_reject_audit_enabled=True,
            ),
        )
        session = Session(
            session_id="full-record",
            messages=[Message(role="user", id="u1", blocks=[TextBlock(type="text", id="t1", text="q")])],
            metadata={
                "trajectory_free": {
                    "decision": "reject",
                    "redline": {"violation": True, "labels": ["pii_phone"]},
                    "absolute_quality": {
                        "score": 3,
                        "subscores": {"executability": 3, "action_obs": 4},
                        "fail_reasons": ["absolute_quality_low"],
                    },
                },
            },
        )
        c2_path = tmp_path / "dummy_c2.json"
        c2_path.write_text("{}", encoding="utf-8")
        append_scoring_reject_fallback(session, c2_path)
        assert audit_path.exists()
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["session_id"] == "full-record"
        assert rec["source_file"] == str(c2_path)
        assert rec["scoring_reject"]["origin"] == "etl_fallback"
        assert rec["scoring_reject"]["absolute_quality_score"] == 3
        assert "absolute_quality_low" in rec["scoring_reject"]["absolute_quality_fail_reasons"]
        assert "session" in rec  # session model_dump 已写入


# ============================================================
# 跨场景: decision == resample 时归类 (后续规则扩展点)
# ============================================================

class TestGateEdgeCases:
    def test_missing_trajectory_free_treated_as_accept(self, tmp_path):
        """无 trajectory_free 时按 accept 处理 (向后兼容旧 C2)."""
        payload = _make_session_payload()  # trajectory_free 为空 dict
        p = _write_session(tmp_path, payload)
        session = gate_then_load(p)
        assert session is not None

    def test_decision_resample_not_treated_as_reject(self, tmp_path):
        """resample 不是 reject — 应放行 (后续 PR 决定如何归类)."""
        payload = _make_session_payload(
            trajectory_free={"decision": "resample"},
        )
        p = _write_session(tmp_path, payload)
        session = gate_then_load(p)
        assert session is not None
        # resample 走 gdr 主流程 metadata 标, 不在 etl 拦截