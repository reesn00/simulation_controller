"""orchestration.criterion_source 单元测试 (F2, 2026-09-28).

验证把 simulate 端 ``ValidationReport`` 读成可注入 ``session.metadata`` 的结构，
以及**fail-soft** 语义：读不到 Criterion 不能让 etl 阶段失败。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gdr.domain.schema import Session
from orchestration.criterion_source import (
    CRITERION_METADATA_KEY,
    inject_criterion_evaluation,
    load_criterion_evaluation,
)

CRITERIA = [
    {
        "criterion_id": "C1",
        "verdict": "pass",
        "reason_code": "found_in_reply",
        "message": "已提供推荐列表",
        "evidence_ids": ["ev_1"],
        "retryable": False,
    },
    {
        "criterion_id": "C4",
        "verdict": "fail",
        "reason_code": "missing_item",
        "message": "未给出具体库存数字",
        "evidence_ids": [],
        "retryable": True,
    },
]


def _write_run(runs_dir: Path, run_id: str, reports: list[dict], *, session_id: str = "s1") -> Path:
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "validations.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in reports) + "\n",
        encoding="utf-8",
    )
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": run_id, "remote_session_id": session_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    return run_dir


def _report(verdict: str, criteria: list, **extra) -> dict:
    missing = [
        c["message"] for c in criteria
        if isinstance(c, dict) and c.get("verdict") not in (None, "pass")
    ]
    return {
        "report_id": f"vr_{verdict}",
        "verdict": verdict,
        "criteria": criteria,
        "missing_items": missing,
        "retryable": False,
        **extra,
    }


# ---------------------------------------------------------------------------
# 正常读取
# ---------------------------------------------------------------------------


def test_reads_last_validation_round(tmp_path: Path):
    _write_run(tmp_path, "run-1", [
        _report("fail", [CRITERIA[1]]),          # 追问前
        _report("pass", CRITERIA),                # 最终轮
    ])
    out = load_criterion_evaluation(runs_dir=tmp_path, run_id="run-1", session_id="s1")
    assert out is not None
    assert out["final_verdict"] == "pass"
    assert out["rounds"] == 2
    assert out["run_id"] == "run-1"
    assert len(out["criteria"]) == 2


def test_normalizes_criterion_fields(tmp_path: Path):
    _write_run(tmp_path, "run-1", [_report("fail", CRITERIA)])
    out = load_criterion_evaluation(runs_dir=tmp_path, run_id="run-1", session_id="s1")
    c0, c3 = out["criteria"]
    assert set(c0) == {"criterion_id", "verdict", "reason_code", "message",
                       "evidence_ids", "retryable"}
    assert c0["evidence_ids"] == ["ev_1"]
    assert c3["evidence_ids"] == []
    assert c3["retryable"] is True


def test_missing_items_captured(tmp_path: Path):
    _write_run(tmp_path, "run-1", [_report("fail", CRITERIA)])
    out = load_criterion_evaluation(runs_dir=tmp_path, run_id="run-1", session_id="s1")
    assert out["missing_items"] == ["未给出具体库存数字"]


def test_falls_back_to_run_json_when_no_jsonl(tmp_path: Path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {"run_id": "run-1", "remote_session_id": "s1",
             "validation_rounds": [_report("pass", CRITERIA)]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out = load_criterion_evaluation(runs_dir=tmp_path, run_id="run-1", session_id="s1")
    assert out["final_verdict"] == "pass"
    assert out["rounds"] == 1


def test_finds_run_by_session_when_run_id_unknown(tmp_path: Path):
    _write_run(tmp_path, "run-7", [_report("pass", CRITERIA)], session_id="sess-7")
    out = load_criterion_evaluation(runs_dir=tmp_path, run_id=None, session_id="sess-7")
    assert out is not None
    assert out["run_id"] == "run-7"


def test_derives_verdict_when_report_field_missing(tmp_path: Path):
    report = _report("pass", CRITERIA)
    del report["verdict"]
    _write_run(tmp_path, "run-1", [report])
    out = load_criterion_evaluation(runs_dir=tmp_path, run_id="run-1", session_id="s1")
    # 有 fail 项 → fail-closed 聚合出 fail，而不是默认 pass
    assert out["final_verdict"] == "fail"


# ---------------------------------------------------------------------------
# fail-soft：任何异常形态都返回 None，不抛
# ---------------------------------------------------------------------------


def test_missing_runs_dir_returns_none():
    assert load_criterion_evaluation(runs_dir=None, run_id="r", session_id="s") is None


def test_nonexistent_runs_dir_returns_none(tmp_path: Path):
    assert load_criterion_evaluation(
        runs_dir=tmp_path / "nope", run_id="r", session_id="s"
    ) is None


def test_unknown_run_id_returns_none(tmp_path: Path):
    _write_run(tmp_path, "run-1", [_report("pass", CRITERIA)])
    assert load_criterion_evaluation(
        runs_dir=tmp_path, run_id="run-missing", session_id="s1"
    ) is None


def test_run_without_validations_returns_none(tmp_path: Path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{}", encoding="utf-8")
    assert load_criterion_evaluation(
        runs_dir=tmp_path, run_id="run-1", session_id="s1"
    ) is None


def test_corrupt_jsonl_is_tolerated(tmp_path: Path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "validations.jsonl").write_text("{not json\n", encoding="utf-8")
    assert load_criterion_evaluation(
        runs_dir=tmp_path, run_id="run-1", session_id="s1"
    ) is None


def test_corrupt_run_json_during_scan_is_skipped(tmp_path: Path):
    bad = tmp_path / "run-bad"
    bad.mkdir(parents=True)
    (bad / "run.json").write_text("{broken", encoding="utf-8")
    _write_run(tmp_path, "run-good", [_report("pass", CRITERIA)], session_id="s1")
    out = load_criterion_evaluation(runs_dir=tmp_path, run_id=None, session_id="s1")
    assert out["run_id"] == "run-good"


def test_malformed_criteria_entries_dropped(tmp_path: Path):
    _write_run(tmp_path, "run-1", [_report("pass", [
        *CRITERIA, {"verdict": "pass"}, "not-a-dict", {"criterion_id": "C9"},
    ])])
    out = load_criterion_evaluation(runs_dir=tmp_path, run_id="run-1", session_id="s1")
    # 缺 criterion_id / 非 dict 的被丢弃；CRITERIA 两条保留
    assert [c["criterion_id"] for c in out["criteria"]] == ["C1", "C4"]


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------


def test_inject_writes_metadata_key(tmp_path: Path):
    _write_run(tmp_path, "run-1", [_report("fail", CRITERIA)])
    evaluation = load_criterion_evaluation(runs_dir=tmp_path, run_id="run-1", session_id="s1")
    session = Session.model_validate({"session_id": "s1", "messages": []})

    assert inject_criterion_evaluation(session, evaluation) is True
    assert CRITERION_METADATA_KEY in session.metadata
    assert session.metadata[CRITERION_METADATA_KEY]["final_verdict"] == "fail"


def test_inject_none_is_noop_and_does_not_clear():
    session = Session.model_validate({
        "session_id": "s1", "messages": [],
        "metadata": {CRITERION_METADATA_KEY: {"final_verdict": "pass"}},
    })
    assert inject_criterion_evaluation(session, None) is False
    # 已有值不得被一次读取失败抹掉
    assert session.metadata[CRITERION_METADATA_KEY]["final_verdict"] == "pass"


def test_inject_handles_none_metadata():
    session = Session.model_validate({"session_id": "s1", "messages": []})
    session.metadata = None
    assert inject_criterion_evaluation(session, {"final_verdict": "pass", "criteria": []})
    assert session.metadata[CRITERION_METADATA_KEY]["final_verdict"] == "pass"


def test_injection_does_not_touch_other_metadata(tmp_path: Path):
    _write_run(tmp_path, "run-1", [_report("pass", CRITERIA)])
    evaluation = load_criterion_evaluation(runs_dir=tmp_path, run_id="run-1", session_id="s1")
    session = Session.model_validate({
        "session_id": "s1", "messages": [],
        "metadata": {"training_value_score": 0.8, "validation_summary": {"total_blocks": 9}},
    })
    inject_criterion_evaluation(session, evaluation)
    assert session.metadata["training_value_score"] == 0.8
    assert session.metadata["validation_summary"] == {"total_blocks": 9}


# ---------------------------------------------------------------------------
# audit_reason 注入 (低分标记)
# ---------------------------------------------------------------------------


def test_inject_audit_reason_writes_key():
    from orchestration.criterion_source import AUDIT_METADATA_KEY, inject_audit_reason

    class _S:
        metadata = {}
        session_id = "s1"

    session = _S()
    assert inject_audit_reason(session, "judge_discard") is True
    assert session.metadata[AUDIT_METADATA_KEY] == "judge_discard"


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_inject_audit_reason_skips_blank(raw):
    """None/空串 → 不写键。

    与 inject_criterion_evaluation 语义**相反**: 那边 None 是"读取失败, 别
    抹掉已有值", 这边 None 是"这条没被拒收" —— 正常样本的 metadata 里不该
    留一个空 audit_reason, 评分卡和 label_config 都靠"键存在"判断要不要
    打低分标记, 空串会让两边都误判。
    """
    from orchestration.criterion_source import AUDIT_METADATA_KEY, inject_audit_reason

    class _S:
        metadata = {"keep": "me"}
        session_id = "s1"

    session = _S()
    assert inject_audit_reason(session, raw) is False
    assert AUDIT_METADATA_KEY not in session.metadata
    assert session.metadata["keep"] == "me"
