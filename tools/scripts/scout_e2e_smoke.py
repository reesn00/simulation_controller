"""Scout E2E Smoke Test Driver.

Wires up the entire pipeline ``scout -> from-scout -> annotate -> freeze -> agreement``
end-to-end on real data, so the rubric gate chain actually executes (not just
be architecturally correct).

Two tracks:

- ``--mode real`` (default): run ``freeze / agreement`` against the existing 357
  candidates and 294 annotations in ``evaluation/golden/data/`` (preserves v3,
  produces v4 with the same data — confirms the commands run).
- ``--mode scout``: stand up a clean ``p2_root`` from ``output/scout_fixture/``,
  produce P2 samples, run ``scout``, ``from-scout``, generate synthetic
  annotations, ``freeze`` and ``agreement`` in an isolated ``golden_root``.
- ``--mode overlay``: run ``cmd_overlay_scout --dry-run`` against the existing
  357 candidates and verify ``dimension_scores`` coverage (Task ② path).
  Non-destructive by default; pass ``--overlay-apply`` to actually write back.
- ``--mode both``: do both.

Run with ``$env:PYTHONPATH='.'`` from the repo root, or invoke as
``python tools/scripts/scout_e2e_smoke.py``.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLDEN_ROOT = REPO_ROOT / "trajectory_pipeline" / "evaluation" / "golden" / "data"
FIXTURE_ARCHIVES = REPO_ROOT / "output" / "scout_fixture"
PIPELINE_ROOT = REPO_ROOT / "trajectory_pipeline" / "output" / "pipeline"


# --------------------------------------------------------------------------
# subprocess helpers
# --------------------------------------------------------------------------


def _run(cmd: list[str], *, cwd: Path | None = None) -> int:
    """Run a subprocess, streaming stdout/stderr.

    Raises ``SystemExit`` on non-zero return code so the driver stops fast.
    """
    label = " ".join(cmd)
    print(f"\n>>> {label}", flush=True)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd or REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        check=False,
    )
    if proc.returncode != 0:
        print(f"!! command failed with exit code {proc.returncode}", file=sys.stderr)
        raise SystemExit(proc.returncode)
    return proc.returncode


def _py_module(mod: str, *more: str) -> list[str]:
    return [sys.executable, "-m", mod, *more]


# --------------------------------------------------------------------------
# Track A: real existing data (preserve --existing, freeze v4, agreement v4)
# --------------------------------------------------------------------------


def _track_real(args: argparse.Namespace) -> int:
    """Freeze v4 + agreement v4 on existing real candidates/annotations.

    Identical inputs as v3 — this is a **chain-execution** test, not a new
    data pull. The whole point is to prove ``freeze`` and ``agreement`` run
    cleanly when invoked from outside code, with no surprises.
    """
    golden_root = Path(args.golden_root)
    version = args.version
    out_dir = Path(args.out_dir) if args.out_dir else golden_root
    if out_dir == golden_root:
        json_out = out_dir / f"v{version}" / "agreement.json"
    elif args.json_out:
        json_out = Path(args.json_out)
    else:
        json_out = out_dir / f"agreement_v{version}.json"

    print(f"[track-real] golden_root = {golden_root}")
    print(f"[track-real] freezing v{version} ...")
    _run(_py_module(
        "trajectory_pipeline.evaluation.golden.builder",
        "--golden-root", str(golden_root),
        "freeze",
        "--version", str(version),
    ))

    print(f"[track-real] agreement v{version} ...")
    _run(_py_module(
        "trajectory_pipeline.evaluation.golden.builder",
        "--golden-root", str(golden_root),
        "agreement",
        "--version", str(version),
        "--json-out", str(json_out),
    ))

    report = json.loads(Path(json_out).read_text(encoding="utf-8"))
    print(
        f"\n[track-real] D10 v{version} agreement = "
        f"{report['agreed']}/{report['n']} = {report['agreement_ratio']:.1%} "
        f"(threshold {report['threshold']:.0%})"
    )
    return 0


# --------------------------------------------------------------------------
# Track B: scout-driven fixture (full chain in isolated golden_root)
# --------------------------------------------------------------------------


def _make_p2_root(fixture_dir: Path, dest: Path) -> tuple[int, list[Path]]:
    """Build a clean p2_root from fixture archives.

    Copies fixture P1 archives to ``dest/<archive>.json`` and writes
    corresponding P2 samples to ``dest/samples/<sample_id>.json`` by running
    :func:`assembler.schema.split_archive` on each archive.
    """
    from trajectory_pipeline.assembler.schema import split_archive  # local import

    if dest.exists():
        shutil.rmtree(dest)
    samples_dir = dest / "samples"
    samples_dir.mkdir(parents=True)
    n_samples = 0
    archive_paths: list[Path] = []
    for arch_path in sorted(fixture_dir.glob("*.json")):
        dst_arch = dest / arch_path.name
        shutil.copy2(arch_path, dst_arch)
        archive_paths.append(dst_arch)
        try:
            samples = split_archive(dst_arch)
        except Exception as exc:
            print(f"  ! skip {arch_path.name}: split failed: {exc}", file=sys.stderr)
            continue
        for s in samples:
            out_p = samples_dir / f"{s.sample_id}.json"
            out_p.write_text(
                json.dumps(s.to_json(), ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8",
            )
            n_samples += 1
    return n_samples, archive_paths


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _synthetic_labels_from_scout(scout_dir: Path) -> list[dict]:
    """Build annotation rows from positive_candidates + negative_candidates.

    Mirrors the structure of human annotations: golden_id + label + round=1 +
    dimension_scores. Label is derived from scout's tag/branch (a coarse but
    deterministic mapping), not from any human judgment — this is a **chain
    execution** test, not a label-quality test.

    Mapping (rubric tag → coarse label):
      - d2_failed     -> "no_play_control"
      - d4_failed     -> "broken"
      - d5_failed     -> "no_play_resource"
      - d6_failed     -> "broken_content"
      - veto          -> "blocked"
      - (positive)    -> "success"
    """
    NEG_MAP = {
        "d2_failed": "no_play_control",
        "d4_failed": "broken",
        "d5_failed": "no_play_resource",
        "d6_failed": "broken_content",
    }
    out: list[dict] = []
    for src, default in (("positive_candidates.jsonl", "success"),
                          ("negative_candidates.jsonl", None)):
        p = scout_dir / src
        if not p.exists():
            continue
        for r in _read_jsonl(p):
            tags = list(r.get("tags") or [])
            dim = {s["dimension"]: s["score"] for s in r.get("scores") or []}
            if default == "success":
                label = "success"
            else:
                label = None
                for t in tags:
                    if t in NEG_MAP:
                        label = NEG_MAP[t]
                        break
                if label is None and "veto" in tags:
                    label = "blocked"
                if label is None:
                    label = "unknown"
            out.append({
                "golden_id": r["sample_id"],
                "label": label,
                "label_label": label,
                "round": 1,
                "reviewed_by": "synthetic-from-scout",
                "reviewed_at": "2026-10-14T00:00:00+00:00",
                "interval_days": None,
                "note": "",
                "dimension_scores": dim or None,
            })
    return out


def _track_scout(args: argparse.Namespace) -> int:
    """Full scout chain on fixture data in an isolated golden_root.

    Steps:
      1. Build a clean p2_root (P1 + samples) from fixture archives.
      2. Run scout -> positive/negative_candidates.jsonl.
      3. Run ``from-scout`` -> isolated golden_root/candidates.jsonl.
      4. Generate synthetic annotations -> annotations input.
      6. Run ``annotate`` to merge.
      7. Run ``freeze --version <N> --force`` (fixture is < TARGET_SIZE).
      8. Run ``agreement --version <N>``.
    """
    golden_root = Path(args.isolated_golden_root)
    scout_dir = Path(args.scout_out_dir)
    fixture_dir = Path(args.fixture_dir)
    version = args.scout_version

    print(f"[track-scout] building p2_root from {fixture_dir} ...")
    with tempfile.TemporaryDirectory(prefix="scout_e2e_p2_") as tmp:
        p2_root = Path(tmp)
        n_samples, _ = _make_p2_root(fixture_dir, p2_root)
        print(f"[track-scout]   generated {n_samples} P2 samples in {p2_root}")

        print(f"[track-scout] running scout (out -> {scout_dir}) ...")
        if scout_dir.exists():
            shutil.rmtree(scout_dir)
        _run(_py_module(
            "trajectory_pipeline.evaluation.scout.cli",
            "--p2-root", str(p2_root),
            "--out", str(scout_dir),
            "--stats",
        ))

        print(f"[track-scout] running from-scout -> {golden_root} ...")
        if golden_root.exists():
            shutil.rmtree(golden_root)
        golden_root.mkdir(parents=True)
        _run(_py_module(
            "trajectory_pipeline.evaluation.golden.builder",
            "--golden-root", str(golden_root),
            "from-scout",
            "--scout-dir", str(scout_dir),
            "--force",
        ))

        print(f"[track-scout] generating synthetic annotations from scout ...")
        ann_rows = _synthetic_labels_from_scout(scout_dir)
        ann_path = scout_dir / "synthetic_annotations.jsonl"
        ann_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in ann_rows) + "\n",
            encoding="utf-8",
        )
        print(f"[track-scout]   {len(ann_rows)} annotations -> {ann_path}")

        print(f"[track-scout] running annotate ...")
        _run(_py_module(
            "trajectory_pipeline.evaluation.golden.builder",
            "--golden-root", str(golden_root),
            "annotate",
            "--input", str(ann_path),
            "--force",
        ))

        print(f"[track-scout] running freeze v{version} --force ...")
        _run(_py_module(
            "trajectory_pipeline.evaluation.golden.builder",
            "--golden-root", str(golden_root),
            "freeze",
            "--version", str(version),
            "--force",
        ))

        json_out = golden_root / f"v{version}" / "agreement.json"
        print(f"[track-scout] running agreement v{version} ...")
        _run(_py_module(
            "trajectory_pipeline.evaluation.golden.builder",
            "--golden-root", str(golden_root),
            "agreement",
            "--version", str(version),
            "--json-out", str(json_out),
        ))

        report = json.loads(json_out.read_text(encoding="utf-8"))
        print(
            f"\n[track-scout] D10 v{version} agreement = "
            f"{report['agreed']}/{report['n']} = {report['agreement_ratio']:.1%} "
            f"(threshold {report['threshold']:.0%})"
        )
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------




# --------------------------------------------------------------------------
# Track C: overlay-scout — dimension_scores backfill on existing 357 candidates
# --------------------------------------------------------------------------


def _track_overlay(args: argparse.Namespace) -> int:
    """Run ``cmd_overlay_scout`` against the existing candidates.jsonl.

    By default runs in ``--dry-run`` mode (non-destructive) so this driver
    can be invoked in CI without mutating the on-disk golden dataset.
    Pass ``--overlay-apply`` to actually write the dimension_scores back.

    Verifies the **coverage** invariant after the run: rows with
    ``source_kind != 'negative_pool'`` should land in either
    ``status='scored'`` (with ``dimension_scores``) or
    ``status='missing_sample'`` (no P2 sample — expected for outcomes that
    never executed). Rows with ``source_kind == 'negative_pool'`` should
    land in ``status='negative_pool_no_sample'``.
    """
    golden_root = Path(args.golden_root)
    candidates_path = golden_root / "candidates.jsonl"
    if not candidates_path.exists():
        print(f"[track-overlay] FAIL: {candidates_path} 不存在；先跑 candidates / from-scout",
              file=sys.stderr)
        return 1

    n_rows_before = sum(1 for _ in _read_jsonl(candidates_path))
    p2_root = Path(args.overlay_p2_root)
    if not p2_root.exists():
        print(f"[track-overlay] FAIL: {p2_root} 不存在；无法解析 archive_outcome",
              file=sys.stderr)
        return 1

    dry_run = not args.overlay_apply
    print(f"[track-overlay] golden_root = {golden_root}")
    print(f"[track-overlay] p2_root    = {p2_root}")
    print(f"[track-overlay] mode       = {'APPLY (写回)' if not dry_run else 'DRY-RUN (不写回)'}")
    print(f"[track-overlay] candidates.jsonl 现有 {n_rows_before} 行")

    cmd = _py_module(
        "trajectory_pipeline.evaluation.golden.builder",
        "--golden-root", str(golden_root),
        "overlay-scout",
        "--p2-root", str(p2_root),
    )
    if dry_run:
        cmd.append("--dry-run")
    _run(cmd)

    rows = _read_jsonl(candidates_path)
    by_status: collections.Counter = collections.Counter()
    by_kind: collections.Counter = collections.Counter()
    scored_with_dims = 0
    for r in rows:
        by_status[r.get("status") or "<unset>"] += 1
        by_kind[r.get("source_kind") or "<unset>"] += 1
        if r.get("status") == "scored" and r.get("dimension_scores"):
            scored_with_dims += 1

    expected_scored_or_missing = by_kind.get("archive_outcome", 0) + by_kind.get("p2_sample", 0)
    actual_scored_or_missing = by_status.get("scored", 0) + by_status.get("missing_sample", 0)
    expected_negative_pool = by_kind.get("negative_pool", 0)
    actual_negative_pool = by_status.get("negative_pool_no_sample", 0)

    print(
        f"[track-overlay] by_status: {dict(by_status)} | "
        f"by_kind: {dict(by_kind)} | "
        f"scored_with_dims={scored_with_dims}"
    )

    ok = True
    if actual_scored_or_missing != expected_scored_or_missing:
        print(
            f"[track-overlay] FAIL: archive_outcome+p2_sample "
            f"({expected_scored_or_missing}) != scored+missing_sample "
            f"({actual_scored_or_missing})",
            file=sys.stderr,
        )
        ok = False
    if actual_negative_pool != expected_negative_pool:
        print(
            f"[track-overlay] FAIL: negative_pool ({expected_negative_pool}) "
            f"!= negative_pool_no_sample ({actual_negative_pool})",
            file=sys.stderr,
        )
        ok = False

    if ok:
        print(
            f"[track-overlay] OK: {scored_with_dims}/{n_rows_before} 行带 dimension_scores；"
            f"missing_sample={by_status.get('missing_sample', 0)}（archive_outcome 无轨迹，预期内）"
        )
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="scout-e2e-smoke",
        description=(
            "E2E driver for the scout -> from-scout -> annotate -> freeze "
            "-> agreement chain on real data."
        ),
    )
    ap.add_argument(
        "--mode",
        choices=["real", "scout", "overlay", "both"],
        default="real",
        help="real = run freeze/agreement on existing 357 candidates; "
             "scout = full chain on fixture data in isolated golden_root; "
             "overlay = run cmd_overlay_scout --dry-run on existing candidates "
             "and verify dimension_scores coverage; "
             "both = do both",
    )
    ap.add_argument("--golden-root", default=str(DEFAULT_GOLDEN_ROOT),
                    help="Real-data golden root (default: %(default)s)")
    ap.add_argument("--version", type=int, default=4,
                    help="Version to freeze for track-real (default: 4)")
    ap.add_argument("--out-dir", default="",
                    help="Override output dir for agreement.json")
    ap.add_argument("--json-out", default="",
                    help="Explicit path for agreement.json (overrides --out-dir)")
    ap.add_argument("--isolated-golden-root",
                    default=str(REPO_ROOT / "trajectory_pipeline" / "evaluation" / "golden" / "data_scout"),
                    help="Isolated golden_root for track-scout (default: %(default)s)")
    ap.add_argument("--scout-out-dir",
                    default=str(REPO_ROOT / "output" / "scout_e2e"),
                    help="Where to drop scout three-piece set for track-scout")
    ap.add_argument("--fixture-dir", default=str(FIXTURE_ARCHIVES),
                    help="Source archives for track-scout")
    ap.add_argument("--scout-version", type=int, default=1,
                    help="Version to freeze inside isolated golden_root")
    ap.add_argument("--overlay-apply", action="store_true",
                    help="track-overlay 才用：实际写回 candidates.jsonl（默认 dry-run 不写）")
    ap.add_argument("--overlay-p2-root", default=str(PIPELINE_ROOT),
                    help=f"track-overlay 才用：P2 样本根目录（默认 {PIPELINE_ROOT}）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc = 0
    if args.mode in ("real", "both"):
        try:
            rc = _track_real(args) or rc
        except SystemExit as exc:
            rc = int(exc.code or 1)
    if args.mode in ("scout", "both"):
        try:
            rc = _track_scout(args) or rc
        except SystemExit as exc:
            rc = int(exc.code or 1)
    if args.mode in ("overlay", "both"):
        try:
            rc = _track_overlay(args) or rc
        except SystemExit as exc:
            rc = int(exc.code or 1)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
