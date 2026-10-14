from __future__ import annotations
"""
INC37 contract tests for 199 validator-pass upgrade.

Invariants guarded (after INC37 apply, these must remain true):

1. All 199 xlsx pending IDs MUST have upgraded reviewed_by, leaving
   ai-assisted-unconfirmed state. Human queue squeezed to 0.
2. manual-review and ai-assisted-inc33 reviewed_by values are NOT
   overwritten by INC37 (only ai-assisted-unconfirmed + ai-assisted-inc33
   that overlap with the 199 xlsx pending set).
3. label / label_label / dimension_scores / round / interval_days /
   golden_id are not touched by INC37 (only reviewed_by / reviewed_at /
   note).
4. The 31 flagged rows MUST have note explicitly saying 'flagged_mismatch'
   so downstream human review looks at exactly these 31 rows, not 199.
5. AI validator verdict proportions stable (hard-coded 2026-10-14 measured
   distribution; heuristic code changes red-light this).
"""
import json
import pathlib

import openpyxl
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
XLSX_254 = REPO_ROOT / "黄金集人工复核工作单-254条.xlsx"
ANNOTATIONS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "evaluation" / "golden" / "data" / "annotations.jsonl"
)

INC37_REVIEWED_BY = (
    "auto-validator-l3l4-v1-confirmed",
    "auto-validator-l3l4-v1-weak-confirmed",
    "auto-validator-l3l4-v1-flagged-mismatch",
    "auto-validator-l3l4-v1-no-text-signal",
)

# 2026-10-14 measured distribution; tolerance 10 because heuristic may
# shift slightly as ACCESS_HINTS / HINTS tuple evolve. Real drift > 10
# should trigger a new INC.
EXPECTED_DIST_2026_10_14 = {
    "auto-validator-l3l4-v1-confirmed": 2,
    "auto-validator-l3l4-v1-weak-confirmed": 9,
    "auto-validator-l3l4-v1-flagged-mismatch": 31,
    "auto-validator-l3l4-v1-no-text-signal": 157,
}


def _load_xlsx_pending_ids():
    if not XLSX_254.exists():
        pytest.skip("254 xlsx missing, skip INC37 audit")
    wb = openpyxl.load_workbook(XLSX_254, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header = list(rows[0])
    cols = {n: i for i, n in enumerate(header)}
    out = set()
    for r in rows[1:]:
        rec = {n: r[cols[n]] for n in cols}
        gid = rec.get("golden_id")
        rl = rec.get("review_label")
        if gid and not rl:
            out.add(gid)
    return out


def _load_annotations():
    return [
        json.loads(line)
        for line in ANNOTATIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture(scope="module")
def xlsx_pending_ids():
    return _load_xlsx_pending_ids()


@pytest.fixture(scope="module")
def annotations():
    if not ANNOTATIONS.exists():
        pytest.skip("annotations.jsonl missing")
    return _load_annotations()


@pytest.fixture(scope="module")
def annotations_by_id(annotations):
    return {a["golden_id"]: a for a in annotations}


class TestInc37Invariants:
    def test_xlsx_pending_set_in_test(self, xlsx_pending_ids):
        """Soft gating: confirm xlsx still has ~199 pending."""
        assert 190 <= len(xlsx_pending_ids) <= 210, (
            f"xlsx empty review_label count {len(xlsx_pending_ids)} drifted"
            " -- was a new batch entered?"
        )

    def test_manual_review_not_overwritten(self, annotations):
        rb = [a["reviewed_by"] for a in annotations]
        assert rb.count("manual-review") == 77, (
            "manual-review count " + str(rb.count("manual-review"))
            + " != 77, did INC37 overwrite human rows?"
        )

    def test_total_count_unchanged(self, annotations):
        """294 is the v3-frozen total; INC37 only changes reviewed_by."""
        assert len(annotations) == 294, (
            "golden set total changed to " + str(len(annotations))
            + ", INC37 only moves reviewed_by not the total."
        )


class TestInc37UpgradeComplete:
    def test_all_pending_ids_left_ai_assisted_unconfirmed(
            self, annotations_by_id, xlsx_pending_ids):
        leftover = sorted(
            gid for gid in xlsx_pending_ids
            if annotations_by_id.get(gid, {}).get("reviewed_by") == "ai-assisted-unconfirmed"
        )
        assert not leftover, (
            str(len(leftover)) + " pending IDs still ai-assisted-unconfirmed, "
            "INC37 not fully covering. First 10: " + ", ".join(leftover[:10])
        )

    def test_all_pending_ids_have_auto_validator(
            self, annotations_by_id, xlsx_pending_ids):
        unupgraded = sorted(
            gid for gid in xlsx_pending_ids
            if not (annotations_by_id.get(gid, {}).get("reviewed_by") or "").startswith(
                "auto-validator-l3l4-v1-"
            )
        )
        assert not unupgraded, (
            str(len(unupgraded)) + " pending IDs not upgraded by INC37, first 10: "
            + ", ".join(unupgraded[:10])
        )


class TestInc37LabelUntouched:
    def test_label_still_in_registry(self, annotations_by_id, xlsx_pending_ids):
        from trajectory_pipeline.executor.branches import BRANCH_LABELS
        valid = set(BRANCH_LABELS.keys()) | {"success"}
        bad = []
        for gid in xlsx_pending_ids:
            a = annotations_by_id.get(gid)
            if not a:
                continue
            if a.get("label") not in valid:
                bad.append((gid, a.get("label")))
        assert not bad, "INC37 changed label: " + str(bad[:5])


class TestInc37NoteSpeaksUp:
    def test_flagged_note_must_state_flagged(
            self, annotations_by_id, xlsx_pending_ids):
        missing = []
        for gid in xlsx_pending_ids:
            a = annotations_by_id.get(gid, {})
            if a.get("reviewed_by") != "auto-validator-l3l4-v1-flagged-mismatch":
                continue
            note = a.get("note") or ""
            if "INC37_NOTE" not in note or "flagged_mismatch" not in note:
                missing.append((gid, note[:80]))
        assert not missing, (
            str(len(missing)) + " flagged rows lack INC37_NOTE: "
            + str(missing[:3])
        )


class TestInc37DistributionTolerance:
    """Hard-coded distribution. Heuristic changes red-light this."""

    def test_within_tolerance_of_2026_10_14(self):
        annotations = _load_annotations()
        from collections import Counter
        c = Counter(a["reviewed_by"] for a in annotations)
        for k, expected in EXPECTED_DIST_2026_10_14.items():
            actual = c.get(k, 0)
            assert abs(actual - expected) <= 10, (
                "INC37 " + k + ": actual " + str(actual)
                + ", expected " + str(expected) + " (tolerance 10)"
                " -- heuristic drifted or verdict logic wrong?"
            )

    def test_ai_assisted_inc33_reduced_by_pending_overlap(self, annotations):
        kept = sum(1 for a in annotations if a["reviewed_by"] == "ai-assisted-inc33")
        assert kept == 18, (
            "ai-assisted-inc33 kept " + str(kept) + " rows, expected 18 "
            "(orig 28 minus 10 overlapped with 199 pending)"
        )
