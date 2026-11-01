"""per-Run 迁移（方案 23）与**冻结档主键**的守门测试。

这组测试守的是一个**实测踩过的坑**（2026-11-01）
--------------------------------------------------------
旧 P2 按「决策单元」切条 ⇒ 一个 Run 拆成 ``__search`` / ``__site-1`` / …
好几条 sample，每条一个 ``golden_id``。per-Run 之后它们合成**一条**轨迹
⇒ 挂在旧 id 上的人核标注必须迁（``migrate-trajectory``）。

迁移的映射是**多对一**（几个旧 id 合成一条新 id）⇒ 它**不能**拿去给
**冻结行**换键：换键要 **1:1**（:data:`builder.ID_ALIAS_FILE` 的硬要求）。
两张表合成一张的后果（实测）：

- 同一个 ``golden_id`` 在**同一档里出现两次** ⇒ 按 id 建索引时后出现的赢
  ⇒ 前一行**静默消失**（报表上只是少了几条，**看不出异常**）；
- 同一个 ``golden_id`` **跨两档出现** ⇒ 一行同时进两个分母 ⇒ 读数偏高。

⇒ 这组测试钉住三件事：

1. **两张表分开**：迁移写 ``id_alias_trajectory.json``，**不碰**
   ``id_alias.json``（后者是 1:1 的行键迁移表）；
2. **冻结不按合并表换行键**：迁移跑完再冻结，冻结行**保留自己的旧 id**；
3. **主键门禁**：万一还是撞了（换别的路径撞上），**拦下不冻结**，
   而不是冻出一份「按 id 查会少行」的档。
"""

from __future__ import annotations

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation.golden import builder


# ── 工具 ────────────────────────────────────────────────────────


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = builder.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _legacy_anno(gid: str, *, label: str = "unresolved", rnd: int = 1) -> dict:
    """一条挂在**旧 per-unit id** 上的人核标注。"""
    return {
        "golden_id": gid,
        "round": rnd,
        "label": label,
        "label_label": "感知层未给出结论（fail-closed）",
        "note": "占位样本：无URL无evidence，不可人工验证",
        "reviewed_by": "manual-review",
        "reviewed_at": "2026-10-12T02:12:57+00:00",
        "dimension_scores": None,
        "interval_days": None,
    }


def _site_row(gid: str, *, label: str = "unresolved") -> dict:
    """一条**站点级**冻结行（形状与 v1–v16 的冻结档一致）。"""
    return {
        "golden_id": gid,
        "source_kind": "p2_sample",
        "task_id": gid.split("__")[0],
        "run_id": gid.split("__")[1],
        "url": "",
        "code": None,
        "golden": {"label": label, "note": "", "dimension_scores": None},
        "retest": None,
    }


def _golden_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "golden"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ── 1. 迁移：两张表分开 ────────────────────────────────────────


class Test迁移写自己的表:
    def test_迁移不碰行键迁移表(self, tmp_path):
        """⚠️ **核心回归测试**：两张表合成一张 ⇒ 冻结行被按合并语义换键
        ⇒ 同一个 id 出现两次（实测：holdout 里 ``E001__57bbf995`` 出现 2 次，
        且 ``E001__57bbf995__6a28d60d`` 同时落在 calibration 与 holdout 两档）。
        """
        g = _golden_root(tmp_path)
        pipeline = tmp_path / "pipeline"
        pipeline.mkdir()
        alias = g / builder.ID_ALIAS_FILE
        alias.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "map": {"P001__2e0cec8b__10990df1__neg153": "P001__neg_blocked"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        before = alias.read_bytes()
        _write_jsonl(
            g / builder.ANNOTATIONS_FILE,
            [_legacy_anno("E001__57bbf995__search"), _legacy_anno("E001__57bbf995__site-1")],
        )
        rc, out, err = _run([
            "--golden-root", str(g), "--root", str(pipeline),
            "migrate-trajectory", "--apply",
        ])
        assert rc == 0, out + err
        # 行键迁移表**一个字都没动**
        assert alias.read_bytes() == before
        # 迁移表另存
        assert (g / builder.ID_ALIAS_TRAJECTORY_FILE).exists()

    def test_没有行键表时迁移也不创建它(self, tmp_path):
        g = _golden_root(tmp_path)
        pipeline = tmp_path / "pipeline"
        pipeline.mkdir()
        _write_jsonl(
            g / builder.ANNOTATIONS_FILE,
            [_legacy_anno("E001__57bbf995__search")],
        )
        rc, out, err = _run([
            "--golden-root", str(g), "--root", str(pipeline),
            "migrate-trajectory", "--apply",
        ])
        assert rc == 0, out + err
        assert not (g / builder.ID_ALIAS_FILE).exists()

    def test_迁移表是多对一的且逐轮去处分开记(self, tmp_path):
        """⚠️ **同一个旧 id 可能有两个去处**（round=1 收成轨迹级、
        round=2 落回站点级）⇒ 扁平表存不下 ⇒ ``map`` 记最小轮次，
        其余轮次记在 ``rounds`` 里。
        """
        g = _golden_root(tmp_path)
        pipeline = tmp_path / "pipeline"
        pipeline.mkdir()
        _write_jsonl(
            g / builder.ANNOTATIONS_FILE,
            [_legacy_anno("E001__57bbf995__search"), _legacy_anno("E001__57bbf995__site-1")],
        )
        rc, out, err = _run([
            "--golden-root", str(g), "--root", str(pipeline),
            "migrate-trajectory", "--apply",
        ])
        assert rc == 0, out + err
        doc = json.loads(
            (g / builder.ID_ALIAS_TRAJECTORY_FILE).read_text(encoding="utf-8")
        )
        # 多对一：两个旧 id → 同一条新 id
        assert doc["map"]["E001__57bbf995__search"] == "E001__57bbf995"
        assert doc["map"]["E001__57bbf995__site-1"] == "E001__57bbf995"
        # 逐轮去处单列
        assert doc["rounds"]["E001__57bbf995__search"] == {"1": "E001__57bbf995"}

    def test_迁移后标注合成一条且人话不丢(self, tmp_path):
        g = _golden_root(tmp_path)
        pipeline = tmp_path / "pipeline"
        pipeline.mkdir()
        _write_jsonl(
            g / builder.ANNOTATIONS_FILE,
            [_legacy_anno("E001__57bbf995__search"), _legacy_anno("E001__57bbf995__site-1")],
        )
        rc, out, err = _run([
            "--golden-root", str(g), "--root", str(pipeline),
            "migrate-trajectory", "--apply",
        ])
        assert rc == 0, out + err
        annos = _read_jsonl(g / builder.ANNOTATIONS_FILE)
        assert len(annos) == 1
        assert annos[0]["golden_id"] == "E001__57bbf995"
        assert annos[0]["scope"] == "trajectory"
        # 人话不许丢：两行的来龙去脉都记着
        notes = annos[0]["merged_notes"]
        assert {n["source_golden_id"] for n in notes} == {
            "E001__57bbf995__search",
            "E001__57bbf995__site-1",
        }


# ── 2. 冻结不按合并表换行键 ────────────────────────────────────


def _make_v1_with_legacy_rows(root: pathlib.Path) -> pathlib.Path:
    """造一个含**三条旧 per-unit 行**的冻结档（就是会撞 id 的那三条）。

    ⚠️ 档的比例要**合法**（调参池占比落在 [60%, 70%]）⇒ 否则冻结会先被
    比例门禁拦下，测不到主键门禁。
    """
    vdir = root / "v1"
    calib = [_site_row("E001__57bbf995__search")]
    calib += [_site_row(f"E001__57bbf995__c{i:04x}") for i in range(5)]
    hold = [_site_row("E001__57bbf995__site-1"), _site_row("E001__57bbf995__site-3")]
    hold += [_site_row(f"E001__57bbf995__h{i:04x}") for i in range(1)]
    _write_jsonl(vdir / "calibration.jsonl", calib)
    _write_jsonl(vdir / "holdout.jsonl", hold)
    (vdir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "label_schema_version": builder.LABEL_SCHEMA_VERSION,
                "version": 1,
                "holdout_runs": 0,
                "seed": 42,
                "counts": {"calibration": len(calib), "holdout": len(hold)},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_jsonl(root / builder.VERSIONS_INDEX, [{"version": 1}])
    return root


class Test冻结不按合并表换行键:
    def test_旧per_unit行保留自己的id(self, tmp_path):
        g = _golden_root(tmp_path)
        _make_v1_with_legacy_rows(g)
        # 迁移跑完的样子：合并表另存，行键表里没有这些 id
        (g / builder.ID_ALIAS_TRAJECTORY_FILE).write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "map": {
                        "E001__57bbf995__search": "E001__57bbf995",
                        "E001__57bbf995__site-1": "E001__57bbf995",
                        "E001__57bbf995__site-3": "E001__57bbf995",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        rc, out, err = _run([
            "--golden-root", str(g), "freeze", "--version", "2", "--from-version", "1",
        ])
        assert rc == 0, out + err
        calib = _read_jsonl(g / "v2" / "calibration.jsonl")
        hold = _read_jsonl(g / "v2" / "holdout.jsonl")
        ids = [r["golden_id"] for r in calib + hold]
        # ⚠️ **三条旧 id 一个都没被合并** ⇒ 主键不撞
        assert "E001__57bbf995__search" in ids
        assert "E001__57bbf995__site-1" in ids
        assert "E001__57bbf995__site-3" in ids
        assert len(set(ids)) == len(ids)

    def test_冻结档与上一版逐行一致(self, tmp_path):
        """**测量层一个字都不改**（含 id）⇒ D-10 的分母与历史读数保持可比。"""
        g = _golden_root(tmp_path)
        _make_v1_with_legacy_rows(g)
        rc, out, err = _run([
            "--golden-root", str(g), "freeze", "--version", "2", "--from-version", "1",
        ])
        assert rc == 0, out + err
        for name in ("calibration.jsonl", "holdout.jsonl"):
            assert (g / "v1" / name).read_bytes() == (g / "v2" / name).read_bytes()


# ── 3. 主键门禁 ────────────────────────────────────────────────


class Test主键门禁:
    def test_同档内重复拦下(self):
        parts = {
            "calibration": [_site_row("E001__57bbf995"), _site_row("E001__57bbf995")],
        }
        with pytest.raises(builder.GoldenError, match="同一个 golden_id 出现了多次"):
            builder._assert_unique_golden_ids(parts)

    def test_跨档重复拦下(self):
        """跨档重复 ⇒ 一行同时进两个分母 ⇒ 读数偏高。"""
        parts = {
            "calibration": [_site_row("E001__57bbf995")],
            "holdout": [_site_row("E001__57bbf995")],
        }
        with pytest.raises(builder.GoldenError, match="同一个 golden_id 出现了多次"):
            builder._assert_unique_golden_ids(parts)

    def test_正常档放行(self):
        parts = {
            "calibration": [_site_row("E001__57bbf995__search")],
            "holdout": [_site_row("E001__57bbf995__site-1")],
        }
        builder._assert_unique_golden_ids(parts)

    def test_撞了就不冻结(self, tmp_path):
        """门禁在**写盘之前** ⇒ 撞了就退 2，不留一份坏档。"""
        g = _golden_root(tmp_path)
        _make_v1_with_legacy_rows(g)
        # 造一个「行键迁移表里有合并映射」的坏状态
        (g / builder.ID_ALIAS_FILE).write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "map": {
                        "E001__57bbf995__site-1": "E001__57bbf995",
                        "E001__57bbf995__site-3": "E001__57bbf995",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        rc, out, err = _run([
            "--golden-root", str(g), "freeze", "--version", "2", "--from-version", "1",
        ])
        assert rc != 0
        assert "同一个 golden_id 出现了多次" in out + err
        assert not (g / "v2").exists()
