"""**两种标注粒度**（方案 23 · owner 裁定 ④）：站点级 vs 轨迹级。

守的是八件事：

1. **粒度字面量只有一个真源**（``golden_ls.scope``）——项目台账的键、
   label_config 的文案、CLI 的 ``--scope`` 三处都从它取；
2. **两种粒度两个 LS 项目**（标题不同）——混在一个项目里，标注员分不清
   自己在判一条轨迹还是判一个站点；
3. **站点级的 label_config 一个字节都不动**（已推上 LS 的 v14/v15 认得出
   自己的配置）；
4. **轨迹行没有单一档位** ⇒ 跨档时按**头部站点**定档（owner 2026-11-01
   裁的 ② · 台账 Q-26）——**取严**口径保留作对照（``rows.strict_split``）；
5. **整条推**：一条轨迹的所有档位都得在本次范围内（半条轨迹推不上去）；
6. **档闸按头部站点**：头部站点在 holdout / regression ⇒ **不进金标**
   （裁定③ · C-17）；**认不出头部站点 ⇒ fail-closed 不放行**；
7. **可以「新建」**：轨迹级标注大部分还不存在 ⇒ 新建行**必须由人署名**；
8. **逐站金标进内容键**：页面上的东西变了，变更标记就得跟着变。
"""

from __future__ import annotations

import argparse

import pytest

from trajectory_pipeline.evaluation.golden import rows as rows_mod
from trajectory_pipeline.evaluation.golden_ls import (
    diff as diff_mod,
    exporter as exporter_mod,
    label_config as lc_mod,
    projects as projects_mod,
    reflow as reflow_mod,
    report as report_mod,
    scope as scope_mod,
)


# ── 工具 ────────────────────────────────────────────────────


def _site_row(gid: str = "T001__abcd1234__9f2c1e77", split: str = "calibration") -> dict:
    """站点级行（旧形状）。"""
    return {
        "golden_id": gid,
        "source_kind": "archive_outcome",
        "__split__": split,
        "golden": {"label": "success", "label_label": "成功"},
        "code": {"branch": "success"},
    }


def _traj_row(
    gid: str = "T001__abcd1234",
    *,
    splits: dict[str, int] | None = None,
    label: str = "success",
    branch: str = "success",
    site_labels: list[str] | None = None,
    head_split: str | None = None,
    head_missing: bool = False,
) -> dict:
    """轨迹级行（方案 23 的新形状）：站点明细收在 ``site_outcomes[]`` 里。

    ``head_split``：**头部站点（裁定② 的末条结论所在的那个站）**是哪一档。
    头部站点由 ``code_index`` 指出（它 = 末条 outcome 的下标，与站点明细的
    ``outcome_index`` **同一坐标系**）⇒ 想知道「头部站点在不在调参池」就
    点名它那一档。

    ``head_missing``：模拟**认不出头部站点**（``code_index`` 指不到任何站点）
    ⇒ 档闸必须 **fail-closed**（定不出档 ⇒ 不放行）。
    """
    splits = splits if splits is not None else {"calibration": 2}
    sites = []
    for i, (split, n) in enumerate(sorted(splits.items())):
        for j in range(n):
            sites.append(
                {
                    "golden_id": f"{gid}__{split[:2]}{i}{j}",
                    "url": f"https://site-{i}-{j}.example.com",
                    "split": split,
                    "outcome_index": i * 10 + j,
                    "golden": {
                        "label": (site_labels or [label])[min(j, len(site_labels or [label]) - 1)],
                        "access_surface": "human-browser",
                        "observed_at": "2026-10-12",
                    },
                }
            )
    code_index = 0
    if head_missing:
        code_index = 9999  # 指不到任何站点 ⇒ 认不出头部站点
    elif head_split is not None:
        for s in sites:
            if s["split"] == head_split:
                code_index = s["outcome_index"]
                break
    return {
        "golden_id": gid,
        "source_kind": rows_mod.SOURCE_TRAJECTORY,
        "task_id": "T001",
        "run_id": "abcd1234",
        "splits": dict(splits),
        "code_index": code_index,
        "site_outcomes": sites,
        "golden": {"label": label, "label_label": "成功"},
        "code": {"branch": branch},
    }


# ── 1. 粒度字面量的真源 ──────────────────────────────────────


class TestScopeConstants:
    def test_两个字面量(self) -> None:
        assert scope_mod.SCOPE_SITE == "site"
        assert scope_mod.SCOPE_TRAJECTORY == "trajectory"
        assert scope_mod.SCOPES == ("site", "trajectory")

    def test_projects用的是同一个对象不是抄一份(self) -> None:
        """抄一份必然漂（改了真源，投影还停在旧值）。"""
        assert projects_mod.SCOPE_SITE is scope_mod.SCOPE_SITE
        assert projects_mod.SCOPE_TRAJECTORY is scope_mod.SCOPE_TRAJECTORY


# ── 2. 两种粒度两个项目 ──────────────────────────────────────


class TestProjectTitle:
    def test_站点级标题与历史档逐字一致(self) -> None:
        """已建好的老项目靠**精确匹配**标题找回来 ⇒ 不能改。"""
        assert projects_mod.project_title(14) == "golden-v14"
        assert projects_mod.project_title(14, scope_mod.SCOPE_SITE) == "golden-v14"

    def test_轨迹级标题带后缀(self) -> None:
        assert (
            projects_mod.project_title(14, scope_mod.SCOPE_TRAJECTORY)
            == "golden-v14-traj"
        )

    def test_认不出的粒度报错(self) -> None:
        with pytest.raises(ValueError):
            projects_mod.project_title(14, "traj")


class TestPendingIsScopeAware:
    """**站点级建过 ≠ 轨迹级建过**——两个粒度两个项目。"""

    def _index(self, tmp_path, scope: str):
        index = projects_mod.GoldenProjectIndex.load(tmp_path / "ledger.jsonl")
        index.record(
            index.new_record(
                14, 78, title="golden-v14", scope=scope
            )
        )
        return index

    def test_只算这一粒度的欠账(self, tmp_path) -> None:
        (tmp_path / "v14").mkdir(parents=True)
        index = self._index(tmp_path, scope_mod.SCOPE_SITE)
        assert projects_mod.pending_versions(
            index, golden_root=tmp_path, scope=scope_mod.SCOPE_SITE
        ) == []
        assert projects_mod.pending_versions(
            index, golden_root=tmp_path, scope=scope_mod.SCOPE_TRAJECTORY
        ) == [14]

    def test_缺省是两种粒度都算(self, tmp_path) -> None:
        """缺省 = 历史上界（不是「两个都上了没」）——窄化要显式点名。"""
        (tmp_path / "v14").mkdir(parents=True)
        index = self._index(tmp_path, scope_mod.SCOPE_SITE)
        assert projects_mod.pending_versions(index, golden_root=tmp_path) == []


# ── 3. 站点级的 label_config 一个字节都不动 ────────────────────


class TestLabelConfig:
    def test_站点级的指纹与历史档一致(self) -> None:
        """v14/v15 的 sha 记在台账里 ⇒ 改一个字就「配置漂了」。"""
        xml = lc_mod.render_label_config(scope=scope_mod.SCOPE_SITE)
        assert lc_mod.label_config_sha(xml) == "376f822addb1"

    def test_轨迹级另出一份且页眉写明是轨迹(self) -> None:
        site = lc_mod.render_label_config(scope=scope_mod.SCOPE_SITE)
        traj = lc_mod.render_label_config(scope=scope_mod.SCOPE_TRAJECTORY)
        assert traj != site
        assert "轨迹" in traj
        # 九个块的键一个不少、一个不改（契约是 BLOCK_CONTROLS）
        for control in lc_mod.BLOCK_CONTROLS:
            assert f'name="{control}"' in traj
            assert f'name="{control}"' in site

    def test_认不出的粒度报错(self) -> None:
        with pytest.raises(ValueError):
            lc_mod.render_label_config(scope="traj")


# ── 4. 轨迹行没有单一档位 ────────────────────────────────────


class TestSplitPolicy:
    def test_站点行就是它自己的档(self) -> None:
        assert rows_mod.row_splits(_site_row(split="holdout")) == {"holdout": 1}

    def test_轨迹行是各站点档的计数(self) -> None:
        row = _traj_row(splits={"calibration": 2, "holdout": 1})
        assert rows_mod.row_splits(row) == {"calibration": 2, "holdout": 1}

    def test_取严_沾上封存面就按封存面算(self) -> None:
        row = _traj_row(splits={"calibration": 2, "holdout": 1})
        assert rows_mod.strict_split(row) == "holdout"

    def test_取严_回归集比调参池严(self) -> None:
        row = _traj_row(splits={"calibration": 2, "regression": 1})
        assert rows_mod.strict_split(row) == "regression"

    def test_只沾调参池就是调参池(self) -> None:
        row = _traj_row(splits={"calibration": 2})
        assert rows_mod.strict_split(row) == "calibration"

    def test_整条推_有交集不算(self) -> None:
        """页面上摆的是整条轨迹 ⇒ 半条推不上去。"""
        row = _traj_row(splits={"calibration": 2, "holdout": 1})
        assert rows_mod.splits_within(row, ["calibration"]) is False
        assert rows_mod.splits_within(row, ["calibration", "holdout"]) is True

    def test_整条推_没冻结的行不推(self) -> None:
        row = _traj_row(splits={})
        assert rows_mod.splits_within(row, ["calibration"]) is False


# ── 4b. 头部站点定档（owner 2026-11-01 裁的 ②）────────────────


class TestHeadlineSplit:
    """**头部站点定档**（台账 Q-26 · owner 裁的 ② 口径）。

    ⚠️ 口径换过一次：原来档闸用**取严** ⇒ v16 的 50 条轨迹里只有 **7** 条
    够格进金标（**全是「纯 calibration」的**）⇒ owner 2026-11-01 裁 **②**。
    ⚠️⚠️ **但两个数不是「宽严关系」**（同日逐条核对实测）：**交集只有 2 条**
    ⇒ ② 是**换了一批数据**；且 ② 放行的 **13** 条里 **11 条含 holdout
    站点** ⇒ **封存面泄漏面 11/13**。
    """

    def test_头部站点是末条结论所在的那个站(self) -> None:
        row = _traj_row(splits={"calibration": 1, "holdout": 1}, head_split="holdout")
        head = rows_mod.headline_site(row)
        assert head is not None
        assert head["split"] == "holdout"

    def test_头部站点的档就是这一行的档(self) -> None:
        row = _traj_row(splits={"calibration": 1, "holdout": 1}, head_split="holdout")
        assert rows_mod.headline_split(row) == "holdout"
        row = _traj_row(splits={"calibration": 1, "holdout": 1}, head_split="calibration")
        assert rows_mod.headline_split(row) == "calibration"

    def test_认不出头部站点就返回空串(self) -> None:
        """**fail-closed**：解不出来 ⇒ 返回 ``""`` ⇒ 调用方**必须**不放行。"""
        row = _traj_row(splits={"calibration": 1}, head_missing=True)
        assert rows_mod.headline_site(row) is None
        assert rows_mod.headline_split(row) == ""

    def test_站点级行没有头部站点(self) -> None:
        """站点级行（旧形状）压根不是「一条轨迹」⇒ 头部站点不存在。"""
        assert rows_mod.headline_site(_site_row()) is None
        assert rows_mod.headline_split(_site_row()) == ""

    def test_与取严口径是两个不同的数(self) -> None:
        """② ≠ 取严：同一条轨迹，两个口径给两个答案（这是 ② 的性质，不是 bug）。"""
        row = _traj_row(splits={"calibration": 1, "holdout": 1}, head_split="calibration")
        assert rows_mod.strict_split(row) == "holdout"
        assert rows_mod.headline_split(row) == "calibration"


# ── 5. 逐站金标进内容键 ──────────────────────────────────────


class TestDiffContentKey:
    def test_逐站金标变了就是内容变了(self) -> None:
        a = _traj_row(site_labels=["success"])
        b = _traj_row(site_labels=["blocked"])
        assert diff_mod.content_key(a) != diff_mod.content_key(b)

    def test_站点级行的判据不受影响(self) -> None:
        """站点级行没有 ``site_outcomes`` ⇒ 这一项恒为空元组。"""
        row = _site_row()
        assert diff_mod.content_key(row) == diff_mod.content_key(dict(row))


# ── 6. 轨迹级推送的读数 ──────────────────────────────────────


class TestTrajectoryExporter:
    def test_split是各档计数不是单一档(self) -> None:
        row = _traj_row(splits={"calibration": 2, "holdout": 1})
        data = exporter_mod.build_trajectory_task_data(
            row, version_meta={"version": 17}, clauses={}
        )
        assert data["splits"] == {"calibration": 2, "holdout": 1}
        assert "calibration" in data["split"]
        assert "holdout" in data["split"]

    def test_九个块一个不少(self) -> None:
        row = _traj_row()
        data = exporter_mod.build_trajectory_task_data(
            row, version_meta={"version": 17}, clauses={}
        )
        for control in exporter_mod.BLOCK_CONTROLS:
            assert control in data


class TestTrajectoryReport:
    def test_档位组合分布一条轨迹一个桶(self) -> None:
        """按档计数会把一条轨迹数三次 ⇒ 分母虚高。"""
        rows = [
            _traj_row("T001__a", splits={"calibration": 1, "holdout": 1}),
            _traj_row("T002__b", splits={"calibration": 1, "holdout": 1}),
            _traj_row("T003__c", splits={"calibration": 1}),
        ]
        profile = report_mod.trajectory_split_profile(rows)
        assert profile["calibration+holdout"] == 2
        assert profile["calibration"] == 1
        assert sum(profile.values()) == 3

    def test_一致率的分母是有人核标签的轨迹(self) -> None:
        rows = [
            _traj_row("T001__a", label="success", branch="success"),
            _traj_row("T002__b", label="success", branch="blocked"),
            _traj_row("T003__c", label="", branch="success"),
        ]
        got = report_mod.trajectory_agreement(rows)
        assert got["total"] == 2
        assert got["agree"] == 1
        assert got["unlabeled"] == 1


# ── 7. 回流：档闸取严 + 允许新建 ──────────────────────────────


def _record(gid: str, **over) -> dict:
    row = {
        "golden_id": gid,
        "split": "calibration",
        "verdict": "agree",
        "human_branch": "（不可用）",
        "annotator": " reesn@qq.com",
        "annotated_at": "2026-10-29T06:03:22.123456Z",
    }
    row.update(over)
    return row


class TestReflowTrajectoryGate:
    """**档闸按「头部站点」定档**（owner 2026-11-01 裁的 ② · 台账 Q-26）。

    ⚠️ 口径换过一次：原来**取严**（沾上 holdout / regression 就整条不进金标）
    ⇒ 现在按**裁定② 的末条结论所在的那个站**（头部站点）的档算。
    ⚠️⚠️ **两个数不是宽严关系**（同日逐条核对实测）：取严 7 条 vs ② 13 条，
    **交集只有 2 条** ⇒ ② 是**换了一批数据**；且 ② 放行的 13 条里 **11 条
    含 holdout 站点** ⇒ **封存面泄漏面 11/13**。
    """

    def test_头部站点在封存面就不进金标(self) -> None:
        row = _traj_row(
            "T001__a", splits={"calibration": 1, "holdout": 1}, head_split="holdout"
        )
        rows, summary = reflow_mod.build_worksheet(
            [_record("T001__a")], gold_rows={"T001__a": row}, ann_rows={}
        )
        assert rows == []
        assert summary["skipped_detail"][0]["reason"] == reflow_mod.SKIP_HOLDOUT

    def test_头部站点在回归集就不进金标(self) -> None:
        row = _traj_row(
            "T001__a", splits={"calibration": 1, "regression": 1}, head_split="regression"
        )
        rows, summary = reflow_mod.build_worksheet(
            [_record("T001__a")], gold_rows={"T001__a": row}, ann_rows={}
        )
        assert rows == []
        assert summary["skipped_detail"][0]["reason"] == reflow_mod.SKIP_REGRESSION

    def test_头部站点在调参池就放行_哪怕整条还沾着封存面(self) -> None:
        """② 的口径**就是这样**：放行只看**头部站点**那一档。

        ⚠️ **这条是「封存面泄漏面 11/13」的机制**（实测：② 放行的 13 条里
        11 条含 holdout 站点）⇒ 不是「取严的放宽」，是**换了一批数据**。
        ⚠️ **要收回（回取严）或加兜底（「沾 holdout 就不回流」）就改
        `rows.headline_split` 与 `golden_ls/reflow.py` 的档闸**（两处）。
        """
        row = _traj_row(
            "T001__a", splits={"calibration": 1, "holdout": 1}, head_split="calibration"
        )
        rows, summary = reflow_mod.build_worksheet(
            [_record("T001__a")], gold_rows={"T001__a": row}, ann_rows={}
        )
        assert summary["skipped"] == 0
        assert len(rows) == 1

    def test_认不出头部站点就不放行(self) -> None:
        """**fail-closed**：定不出档 ⇒ 不放行（**不是**「没有档 ⇒ 放行」）。"""
        row = _traj_row("T001__a", splits={"calibration": 1}, head_missing=True)
        rows, summary = reflow_mod.build_worksheet(
            [_record("T001__a")], gold_rows={"T001__a": row}, ann_rows={}
        )
        assert rows == []
        assert summary["skipped_detail"][0]["reason"] == reflow_mod.SKIP_NO_SPLIT

    def test_认不出头部站点时校验也退2(self) -> None:
        """`validate` 是**第二道闸** ⇒ 认不出档 ⇒ 报错，不许回流。"""
        row = _traj_row("T001__a", splits={"calibration": 1}, head_missing=True)
        errors = reflow_mod.validate(
            [
                {
                    "kind": reflow_mod.KIND_CONFIRM,
                    "golden_id": "T001__a",
                    "split": "calibration",
                    "label": "success",
                    "reviewed_by": "",
                    "access_surface": "human-browser",
                    "observed_at": "2026-10-29T14:03:00+08:00",
                    "creates_gold": True,
                }
            ],
            gold_rows={"T001__a": row},
        )
        assert any("定不出档" in e for e in errors)

    def test_没有原标注的行是新建(self) -> None:
        """轨迹级标注大部分还不存在 ⇒ 人判的第一条是**新建**。"""
        row = _traj_row("T001__a", splits={"calibration": 1}, branch="success")
        rows, summary = reflow_mod.build_worksheet(
            [_record("T001__a")], gold_rows={"T001__a": row}, ann_rows={}
        )
        assert summary["create"] == 1
        assert rows[0]["creates_gold"] is True
        # 「谁定了这个标签」是人的事实 ⇒ 署名留空由人填
        assert rows[0]["reviewed_by"] == ""
        # 现在是什么 = 代码的轨迹级结论（裁定② 的末条）
        assert rows[0]["label"] == "success"

    def test_新建的行没署名时不合格(self) -> None:
        row = _traj_row("T001__a", splits={"calibration": 1})
        errors = reflow_mod.validate(
            [
                {
                    "kind": reflow_mod.KIND_CONFIRM,
                    "golden_id": "T001__a",
                    "split": "calibration",
                    "label": "success",
                    "reviewed_by": "",
                    "access_surface": "human-browser",
                    "observed_at": "2026-10-29T14:03:00+08:00",
                    "creates_gold": True,
                }
            ],
            gold_rows={"T001__a": row},
        )
        assert any("reviewed_by" in e for e in errors)


# ── 8. CLI 的粒度开关 ────────────────────────────────────────


class TestCliScope:
    def _ns(self, **kwargs) -> argparse.Namespace:
        return argparse.Namespace(**kwargs)

    def test_缺省是轨迹级(self) -> None:
        """裁定④：一个标注单元 = 一条轨迹。"""
        from trajectory_pipeline.evaluation.golden_ls import cli

        assert cli._scope_of(self._ns()) == scope_mod.SCOPE_TRAJECTORY

    def test_点名站点级走历史口径(self) -> None:
        from trajectory_pipeline.evaluation.golden_ls import cli

        assert cli._scope_of(self._ns(scope="site")) == scope_mod.SCOPE_SITE

    def test_认不出的粒度报错(self) -> None:
        from trajectory_pipeline.evaluation.golden_ls import cli

        with pytest.raises(cli.GoldenCliError):
            cli._scope_of(self._ns(scope="traj"))

    def test_两个粒度的产物文件名不重样(self) -> None:
        """同名 ⇒ 一个粒度的产物把另一个盖掉 ⇒ 回流读到的是另一个粒度的内容。"""
        from trajectory_pipeline.evaluation.golden_ls import cli

        assert (
            cli._annotations_export_name(scope_mod.SCOPE_SITE)
            != cli._annotations_export_name(scope_mod.SCOPE_TRAJECTORY)
        )
        assert (
            cli._label_config_name(scope_mod.SCOPE_SITE)
            != cli._label_config_name(scope_mod.SCOPE_TRAJECTORY)
        )
        assert (
            cli._worksheet_name(scope_mod.SCOPE_SITE)
            != cli._worksheet_name(scope_mod.SCOPE_TRAJECTORY)
        )

    def test_命令行接受scope(self) -> None:
        from trajectory_pipeline.evaluation.golden_ls import cli

        ap = cli._build_parser()
        args = ap.parse_args(
            ["upload", "--version", "17", "--scope", "trajectory"]
        )
        assert args.scope == "trajectory"
        args = ap.parse_args(["upload", "--version", "17", "--scope", "site"])
        assert args.scope == "site"

    def test_命令行不接受乱写的粒度(self) -> None:
        from trajectory_pipeline.evaluation.golden_ls import cli

        with pytest.raises(SystemExit):
            cli._build_parser().parse_args(
                ["upload", "--version", "17", "--scope", "traj"]
            )
