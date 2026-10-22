"""``evaluation.golden.builder.cmd_agreement`` 软维度一致率分支的 contract 守门。

设计来源：``docs/设计方案/10-judge接入方案.md`` §三·第一步。

7 条 contract（plan §三·第一步）：

    1. ``test_dim_coverage`` —— 软维度名在白名单 ``d2..d9`` 子集；
    2. ``test_dim_tolerance_default_1`` —— ``--dim-tolerance`` 默认 1；
    3. ``test_软维度空样本不参与`` —— N=0 的 dim 不进 dict，输出报表不报
       ``0/0 = 0%``（避免误导）；
    4. ``test_软维度与硬分类口径分开`` —— 软维度不一致不传染硬分类的
       ``agreement_ratio``；
    5. ``test_软维度读_calibration_优先于_holdout`` —— holdout 行不进软
       维度分母（contract 5）；
    6. ``test_label_schema_version_不破`` —— 软维度接入不动
       ``label_schema_version`` 字段（contract 6）；
    7. ``test_输出_JSON_含_soft_dims_键`` —— 报表可机器读（contract 7）。

测试套路：tmp_path 造 ``calibration.jsonl`` + ``holdout.jsonl`` +
``manifest.json``（构造版 falsify 数据，避免碰真黄金集），调
``cmd_agreement`` 经由 ``builder.main([...])`` 走完整 CLI 分发。
"""
import argparse
import io
import io
import json
import pathlib
from contextlib import redirect_stdout, redirect_stderr

import pytest

from trajectory_pipeline.evaluation.golden import alignment
from trajectory_pipeline.evaluation.golden import builder


def _row(
    golden_id: str,
    *,
    label: str = "success",
    branch: str | None = None,
    dim_gold: dict | None = None,
    dim_pred: dict | None = None,
) -> dict:
    return {
        "golden_id": golden_id,
        "source_kind": "archive_outcome",
        "task_id": "T001",
        "run_id": "abc12345",
        "url": f"https://{golden_id}.test/",
        "site_url": f"https://{golden_id}.test/",
        "play_page_url": "",
        "code": {
            "branch": branch,
            "branch_label": label,
            "evidence": "e",
            "question": "player.ok",
            "source": "rule",
            "fallback_used": False,
            "reached_play_page": True,
        },
        "dimension_scores": dim_pred or {},
        "persona": None,
        "golden": {
            "label": label,
            "label_label": label,
            "reviewed_by": "manual-review",
            "reviewed_at": "2026-10-12T00:00:00+00:00",
            "dimension_scores": dim_gold,
        },
        "retest": None,
    }


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def _setup_vdir(
    tmp_path: pathlib.Path,
    *,
    calib_rows: list[dict] | None = None,
    holdout_rows: list[dict] | None = None,
    version: int = 7,
) -> pathlib.Path:
    golden_root = tmp_path / "golden"
    vdir = golden_root / f"v{version}"
    _write_jsonl(vdir / "calibration.jsonl", calib_rows or [])
    _write_jsonl(vdir / "holdout.jsonl", holdout_rows or [])
    manifest = {
        "schema_version": "1",
        "label_schema_version": "2",
        "version": version,
        "frozen_at": "2026-10-12T00:00:00+00:00",
        "seed": 42,
        "counts": {
            "calibration": len(calib_rows or []),
            "holdout": len(holdout_rows or []),
        },
    }
    (vdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return golden_root


def _run_cmd(
    golden_root: pathlib.Path, *extra: str
) -> tuple[int, str, str]:
    # ``--golden-root`` 是顶层参数（不在 p_agr 子解析器下），
    # 必须在子命令之前；同样地 ``--root`` 也是顶层参数。
    # 实际跑 ``cmd_agreement`` 时这些顶层参数都用了子解析器的 argv。
    # 走 ``parse_known_args`` 兼容顺序，先把顶层参数剥给顶行。
    argv_top = ["--golden-root", str(golden_root)]
    argv_sub = ["agreement", *extra]
    ap = builder.build_parser()
    top_ns, sub_argv = ap.parse_known_args(argv_top + argv_sub)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = builder.main(argv_top + argv_sub)
    return rc, out_buf.getvalue(), err_buf.getvalue()


class TestDimCoverage:
    def test_白名单是d2到d9_共8个(self):
        assert alignment.SOFT_DIM_NAMES == frozenset(
            {f"d{i}" for i in range(2, 10)}
        )

    def test_白名单外名_fail_closed_抛ValueError(self):
        with pytest.raises(ValueError, match="不在白名单"):
            alignment.validate_soft_dim_name("d1")
        with pytest.raises(ValueError, match="不在白名单"):
            alignment.validate_soft_dim_name("d10")
        with pytest.raises(ValueError, match="不在白名单"):
            alignment.validate_soft_dim_name("rationale_consistency")
        with pytest.raises(ValueError, match="不在白名单"):
            alignment.validate_soft_dim_name("")

    def test_白名单内名_原样返回(self):
        for d in ("d2", "d3", "d5", "d9"):
            assert alignment.validate_soft_dim_name(d) == d

    def test_cmd_agreement_显式传非白名单_进程退出码2(self, tmp_path):
        golden_root = _setup_vdir(
            tmp_path,
            calib_rows=[_row("g1", dim_gold={"d3": 4}, dim_pred={"d3": 4})],
        )
        rc, _, err = _run_cmd(golden_root, "--version", "7", "--soft-dim", "d1")
        assert rc == 2
        assert "不在白名单" in err or "ValueError" in err


class TestDimToleranceDefault:
    def test_p_agr默认值为1(self):
        ap = builder.build_parser()
        ns = ap.parse_args(["agreement"])
        assert ns.dim_tolerance == 1

    def test_dim_agreement_默认_正负一节_容忍(self):
        assert alignment.dim_agreement(4, 3) is True
        assert alignment.dim_agreement(4, 5) is True
        assert alignment.dim_agreement(4, 2) is False
        assert alignment.dim_agreement(1, 5) is False

    def test_dim_agreement_tolerance_0_只允许相等(self):
        assert alignment.dim_agreement(4, 4, tolerance=0) is True
        assert alignment.dim_agreement(4, 5, tolerance=0) is False

    def test_dim_agreement_tolerance_2_放宽(self):
        assert alignment.dim_agreement(4, 2, tolerance=2) is True
        assert alignment.dim_agreement(4, 1, tolerance=2) is False

    def test_cmd_agreement_显式_tolerance_生效(self, tmp_path):
        golden_root = _setup_vdir(
            tmp_path,
            calib_rows=[_row("g1", dim_gold={"d3": 4}, dim_pred={"d3": 5})],
        )
        rc, out, _ = _run_cmd(
            golden_root, "--version", "7",
            "--soft-dim", "d3", "--dim-tolerance", "0",
        )
        assert rc == 0
        assert "d3: 0/1" in out
        rc2, out2, _ = _run_cmd(
            golden_root, "--version", "7",
            "--soft-dim", "d3", "--dim-tolerance", "1",
        )
        assert rc2 == 0
        assert "d3: 1/1" in out2


class Test空样本不参与:
    def test_黄金集dimension_scores全空(self, tmp_path):
        golden_root = _setup_vdir(
            tmp_path,
            calib_rows=[
                _row("g1", dim_gold=None, dim_pred=None),
                _row("g2", dim_gold=None, dim_pred=None),
            ],
        )
        rc, out, _ = _run_cmd(golden_root, "--version", "7")
        assert rc == 0
        assert "软维度一致率" in out
        assert "无任何软维度可算" in out
        assert "0/0" not in out

    def test_黄金集有dim但_pred全空(self, tmp_path):
        golden_root = _setup_vdir(
            tmp_path,
            calib_rows=[
                _row(
                    "g1",
                    dim_gold={"d3": 4, "d9": 5},
                    dim_pred={"d3": None, "d9": None},
                ),
            ],
        )
        rc, out, _ = _run_cmd(
            golden_root, "--version", "7", "--soft-dim", "d3", "d9",
        )
        assert rc == 0
        assert "无任何软维度可算" in out
        assert "d3:" not in out
        assert "d9:" not in out

    def test_仅gold侧None_走skipped_no_gold(self):
        assert alignment.dim_agreement(None, 4) is None

    def test_仅pred侧None_走skipped_no_pred(self):
        assert alignment.dim_agreement(4, None) is None

    def test_越界值返回None(self):
        assert alignment.dim_agreement(0, 4) is None
        assert alignment.dim_agreement(6, 4) is None
        assert alignment.dim_agreement(4, 0) is None
        assert alignment.dim_agreement(4, 6) is None

    def test_负tolerance_返None(self):
        assert alignment.dim_agreement(4, 4, tolerance=-1) is None

    def test_越界值_pred_dict_N0时不进(self, tmp_path):
        golden_root = _setup_vdir(
            tmp_path,
            calib_rows=[_row(
                "g1",
                dim_gold={"d3": 4},
                dim_pred={"d3": 99},
            )],
        )
        rc, out, _ = _run_cmd(golden_root, "--version", "7", "--soft-dim", "d3")
        assert rc == 0
        assert "无任何软维度可算" in out


class Test软维度与硬分类分开:
    def test_软维度全错_硬分类不受影响(self, tmp_path):
        calib = []
        for i in range(4):
            calib.append(_row(
                f"g{i}",
                label="success",
                branch="blocked",
                dim_gold={"d3": 4},
                dim_pred={"d3": 1},
            ))
        golden_root = _setup_vdir(tmp_path, calib_rows=calib)
        out_path = tmp_path / "agree.json"
        rc, _, _ = _run_cmd(
            golden_root, "--version", "7",
            "--json-out", str(out_path), "--soft-dim", "d3",
        )
        assert rc == 0
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["agreement_ratio"] == 0.0
        assert "d3" in data["soft_dims"]
        assert data["soft_dims"]["d3"]["n"] == 4
        assert data["soft_dims"]["d3"]["agreement_ratio"] == 0.0
        assert data["label_schema_version"] == "2"

    def test_软维度全对_硬分类部分错(self, tmp_path):
        calib = [
            _row("g0", label="success", branch="blocked", dim_gold={"d3": 4}, dim_pred={"d3": 4}),
            _row("g1", label="success", branch="blocked", dim_gold={"d3": 5}, dim_pred={"d3": 4}),
            _row("g2", label="success", branch=None,       dim_gold={"d3": 5}, dim_pred={"d3": 5}),
        ]
        golden_root = _setup_vdir(tmp_path, calib_rows=calib)
        out_path = tmp_path / "agree.json"
        rc, _, _ = _run_cmd(
            golden_root, "--version", "7",
            "--json-out", str(out_path), "--soft-dim", "d3",
        )
        assert rc == 0
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["agreed"] == 1
        assert data["n"] == 3
        assert data["soft_dims"]["d3"]["n"] == 3
        assert data["soft_dims"]["d3"]["agreed"] == 3
        assert data["soft_dims"]["d3"]["agreement_ratio"] == 1.0


class TestCalibration优先于Holdout:
    def test_holdout_行_不进软维度分母(self, tmp_path):
        calib = [
            _row("c0", dim_gold={"d3": 4}, dim_pred={"d3": 4}),
            _row("c1", dim_gold={"d3": 3}, dim_pred={"d3": 4}),
        ]
        holdout = [
            _row("h0", dim_gold={"d3": 4}, dim_pred={"d3": 4}),
            _row("h1", dim_gold={"d3": 4}, dim_pred={"d3": 4}),
            _row("h2", dim_gold={"d3": 4}, dim_pred={"d3": 4}),
        ]
        golden_root = _setup_vdir(
            tmp_path, calib_rows=calib, holdout_rows=holdout,
        )
        out_path = tmp_path / "agree.json"
        rc, _, _ = _run_cmd(
            golden_root, "--version", "7",
            "--json-out", str(out_path), "--soft-dim", "d3",
        )
        assert rc == 0
        data = json.loads(out_path.read_text(encoding="utf-8"))
        # 硬分类头条也只看调参池（C-04）：holders 旁置，只作旧口径读数
        assert data["n"] == 2
        assert data["legacy_denominator"]["n"] == 5
        assert data["soft_dims"]["d3"]["n"] == 2
        assert data["soft_dims"]["d3"]["agreed"] == 2

    def test_calibration全空_holdout填了_也不进(self, tmp_path):
        calib = [_row("c0", dim_gold=None, dim_pred=None)]
        holdout = [_row("h0", dim_gold={"d3": 4}, dim_pred={"d3": 4})]
        golden_root = _setup_vdir(
            tmp_path, calib_rows=calib, holdout_rows=holdout,
        )
        rc, out, _ = _run_cmd(golden_root, "--version", "7", "--soft-dim", "d3")
        assert rc == 0
        assert "无任何软维度可算" in out


class TestLabelSchemaVersion:
    def test_label_schema_version_字段保留(self, tmp_path):
        calib = [_row("g0", dim_gold={"d3": 4}, dim_pred={"d3": 4})]
        golden_root = _setup_vdir(tmp_path, calib_rows=calib)
        out_path = tmp_path / "agree.json"
        rc, _, _ = _run_cmd(
            golden_root, "--version", "7",
            "--json-out", str(out_path),
        )
        assert rc == 0
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["label_schema_version"] == builder.LABEL_SCHEMA_VERSION


class TestJsonOutput含SoftDims键:
    def test_输出JSON_含soft_dims键(self, tmp_path):
        calib = [
            _row("g0", dim_gold={"d3": 4}, dim_pred={"d3": 4}),
            _row("g1", dim_gold={"d3": 4}, dim_pred={"d3": 1}),
        ]
        golden_root = _setup_vdir(tmp_path, calib_rows=calib)
        out_path = tmp_path / "agree.json"
        rc, _, _ = _run_cmd(
            golden_root, "--version", "7",
            "--json-out", str(out_path), "--soft-dim", "d3",
        )
        assert rc == 0
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert "soft_dims" in data
        assert "soft_dim_tolerance" in data
        assert "soft_dim_criterion" in data
        assert data["soft_dim_tolerance"] == 1
        assert "1 (rubric" in data["soft_dim_criterion"]
        assert "d3" in data["soft_dims"]
        st = data["soft_dims"]["d3"]
        assert st["n"] == 2
        assert st["agreed"] == 1
        assert st["agreement_ratio"] == 0.5
        assert st["tolerance"] == 1

    def test_输出JSON_自动探测维度(self, tmp_path):
        calib = [
            _row("g0", dim_gold={"d3": 4, "d9": 5}, dim_pred={"d3": 4, "d9": 4}),
        ]
        golden_root = _setup_vdir(tmp_path, calib_rows=calib)
        out_path = tmp_path / "agree.json"
        rc, _, _ = _run_cmd(
            golden_root, "--version", "7", "--json-out", str(out_path),
        )
        assert rc == 0
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert set(data["soft_dims"].keys()) == {"d3", "d9"}

    def test_soft_dim_criterion_随tolerance变化(self, tmp_path):
        calib = [_row("g0", dim_gold={"d3": 4}, dim_pred={"d3": 4})]
        golden_root = _setup_vdir(tmp_path, calib_rows=calib)
        out_path = tmp_path / "agree.json"
        rc, _, _ = _run_cmd(
            golden_root, "--version", "7",
            "--json-out", str(out_path),
            "--soft-dim", "d3", "--dim-tolerance", "2",
        )
        assert rc == 0
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["soft_dim_tolerance"] == 2
        assert "<= 2" in data["soft_dim_criterion"]
