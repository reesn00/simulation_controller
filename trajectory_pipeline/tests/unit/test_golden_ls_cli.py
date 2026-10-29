"""``golden_ls.cli``：**不联网**的那部分命令行行为。

守四件事：

1. **档名拼错 ⇒ 退 2**，不静默变成「那一档悄悄是空的」——档名只从
   ``golden/splits.py`` 取（``cli._select_splits``）；
2. **上一版按版本号找**，不按目录顺序（``v9`` 排在 ``v10`` 前面）；
3. **开关不许挂而不生效**——``--batch-size`` 声明了就得真的改
   ``settings.upload.batch_size``；
4. **``projects`` / ``pending`` 不联网**（没有凭据也要能跑——台账是本地文件）。

⚠️ 全部**不联网**：``init-project`` / ``upload``（非 dry-run）/
``export-annotations`` 要 LS，本文件不碰。
"""

from __future__ import annotations

import argparse

import pytest

from trajectory_pipeline.evaluation.golden_ls import cli


def _ns(**kwargs) -> argparse.Namespace:
    base = {
        "url": None,
        "api_key": None,
        "golden_root": None,
        "ledger": None,
        "splits": None,
        "exclude_holdout": False,
        "only_changed": False,
        "batch_size": None,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


# ── 档选择 ──────────────────────────────────────────────────────


class TestSelectSplits:
    def test_缺省是三档全传(self) -> None:
        assert cli._select_splits(_ns()) == ["calibration", "regression", "holdout"]

    def test_排除holdout(self) -> None:
        assert cli._select_splits(_ns(exclude_holdout=True)) == [
            "calibration",
            "regression",
        ]

    def test_指定档按SPLITS的固定顺序(self) -> None:
        """给的是 `holdout,calibration`，返回也得是固定顺序——
        否则「推送计划里档的次序」会随命令行摆动。"""
        assert cli._select_splits(_ns(splits="holdout,calibration")) == [
            "calibration",
            "holdout",
        ]

    def test_档名拼错退2(self) -> None:
        """拼错的档名**不报错**是最坏的：那一档悄悄空了，人看不出来。"""
        with pytest.raises(cli.GoldenCliError) as exc:
            cli._select_splits(_ns(splits="calib"))
        assert "calib" in str(exc.value)

    def test_一档都不剩退2(self) -> None:
        """`--splits holdout --exclude-holdout` ⇒ 空 ⇒ 退 2（不推个零条）。"""
        with pytest.raises(cli.GoldenCliError):
            cli._select_splits(_ns(splits="holdout", exclude_holdout=True))


# ── 上一版 ──────────────────────────────────────────────────────


class TestPreviousVersion:
    def test_按版本号不按目录顺序(self, tmp_path) -> None:
        """``v9`` 在目录里排在 ``v10`` 前面——按目录序找会拿错版本。"""
        for name in ("v9", "v10", "v14"):
            (tmp_path / name).mkdir(parents=True)
        assert cli._previous_version(14, golden_root=tmp_path) == 10
        assert cli._previous_version(10, golden_root=tmp_path) == 9

    def test_没有更早的版本就是None(self, tmp_path) -> None:
        (tmp_path / "v14").mkdir(parents=True)
        assert cli._previous_version(14, golden_root=tmp_path) is None


# ── 开关不许挂而不生效 ──────────────────────────────────────────


class TestBatchSize:
    def test_batch_size真的改到settings上(self) -> None:
        from trajectory_pipeline.evaluation.ls_bridge.settings import (
            LSSettings,
        )

        settings = LSSettings(api_key="x")
        assert settings.upload.batch_size == 50
        patched = cli._with_batch_size(settings, 10)
        assert patched.upload.batch_size == 10
        # 原对象不动（frozen dataclass）
        assert settings.upload.batch_size == 50

    def test_小于1退2(self) -> None:
        from trajectory_pipeline.evaluation.ls_bridge.settings import LSSettings

        with pytest.raises(cli.GoldenCliError):
            cli._with_batch_size(LSSettings(api_key="x"), 0)


# ── 不联网的那两个子命令 ────────────────────────────────────────


class TestOfflineCommands:
    def test_projects没凭据也能跑(self, tmp_path, capsys) -> None:
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text(
            '{"version": 14, "project_id": 78, "title": "golden-v14"}\n',
            encoding="utf-8",
        )
        assert cli.main(["--ledger", str(ledger), "projects"]) == 0
        assert "v14" in capsys.readouterr().out

    def test_projects台账为空也退0(self, tmp_path, capsys) -> None:
        assert cli.main(["--ledger", str(tmp_path / "none.jsonl"), "projects"]) == 0
        assert "台账为空" in capsys.readouterr().out

    def test_pending列得出欠账(self, tmp_path, capsys) -> None:
        (tmp_path / "v14").mkdir(parents=True)
        (tmp_path / "v15").mkdir(parents=True)
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text(
            '{"version": 14, "project_id": 78, "title": "golden-v14"}\n',
            encoding="utf-8",
        )
        assert cli.main(
            ["--ledger", str(ledger), "--golden-root", str(tmp_path), "pending"]
        ) == 0
        out = capsys.readouterr().out
        # 输出是「欠账(≥v14): [15]」这种形态（版本号，不带 v 前缀）
        owed = out.split("欠账")[1].split("[")[1].split("]")[0]
        assert "15" in owed
        assert "14" not in owed


class TestPendingRespectsBaseline:
    """**存量不上 LS**（裁定 O-1）——v1..v13 **不算欠账**。

    把它们列进欠账清单，等于给出一条**会违反 owner 裁定**的指令：有人
    照着清单就能把历史全推上去，而历史版本的判定与 v14 不同。
    """

    def test_低于基线的版本不算欠账(self, tmp_path) -> None:
        from trajectory_pipeline.evaluation.golden_ls import projects

        for name in ("v1", "v13", "v14", "v15"):
            (tmp_path / name).mkdir(parents=True)
        index = projects.GoldenProjectIndex.load(tmp_path / "ledger.jsonl")
        owed = projects.pending_versions(index, golden_root=tmp_path)
        assert owed == [14, 15]

    def test_基线是14且写在常量里(self) -> None:
        from trajectory_pipeline.evaluation.golden_ls import projects

        assert projects.UPLOAD_BASELINE_VERSION == 14

    def test_命令输出把不传的版本单列(self, tmp_path, capsys) -> None:
        for name in ("v13", "v14"):
            (tmp_path / name).mkdir(parents=True)
        assert cli.main(
            [
                "--ledger",
                str(tmp_path / "ledger.jsonl"),
                "--golden-root",
                str(tmp_path),
                "pending",
            ]
        ) == 0
        out = capsys.readouterr().out
        assert "不上 LS" in out
        assert "13" in out.split("不上 LS")[1]


# ── 点名挑行（试点用） ──────────────────────────────────────────


def _row(gid: str, split: str) -> dict:
    return {"golden_id": gid, "__split__": split}


class TestSelectByIds:
    """``--golden-ids``：**点错的 id 必须是错，不能是「少推几条」**。

    试点要覆盖到哪条路径，「覆盖到了」本身就是结论的一部分。拼错一个
    字符若被静默忽略 ⇒ 人以为试点覆盖了那条路径、其实没有 ⇒ 试点白做，
    而且**没有任何地方会报错**。
    """

    def test_按点名顺序挑出(self) -> None:
        rows = [_row("a", "calibration"), _row("b", "holdout"), _row("c", "regression")]
        got = cli._select_by_ids(
            rows, ["c", "a"], selected=["calibration", "regression", "holdout"], version=14
        )
        assert [r["golden_id"] for r in got] == ["c", "a"]

    def test_不存在的id退2(self) -> None:
        rows = [_row("a", "calibration")]
        with pytest.raises(cli.GoldenCliError) as exc:
            cli._select_by_ids(rows, ["a", "typo"], selected=["calibration"], version=14)
        assert "typo" in str(exc.value)

    def test_不在本次档里的id退2(self) -> None:
        rows = [_row("a", "calibration"), _row("h", "holdout")]
        with pytest.raises(cli.GoldenCliError) as exc:
            cli._select_by_ids(rows, ["h"], selected=["calibration"], version=14)
        assert "h" in str(exc.value)

    def test_重复id只推一次且保持首次顺序(self) -> None:
        rows = [_row("a", "calibration"), _row("b", "calibration")]
        got = cli._select_by_ids(
            rows, ["b", "a", "b"], selected=["calibration"], version=14
        )
        assert [r["golden_id"] for r in got] == ["b", "a"]


# ── 产物按字节写（不许整文件变 CRLF） ────────────────────────────


class TestArtifactsAreByteStable:
    """**留底字节必须等于台账里记的 sha**。

    ``Path.write_text`` 在 Windows 上开文本模式 ⇒ ``\\n`` 翻成 ``\\r\\n``
    ⇒ 留底与台账对不上，而「对不上」**不报错**。
    """

    def test_label_config留底是LF且sha对得上(self, tmp_path) -> None:
        from trajectory_pipeline.evaluation.golden_ls.label_config import (
            label_config_sha,
            render_label_config,
        )

        xml = render_label_config()
        path = tmp_path / "label_config.xml"
        cli._write_text_bytes(path, xml)
        raw = path.read_bytes()
        assert b"\r\n" not in raw
        assert raw == xml.encode("utf-8")
        # 台账里记的 sha 就是对「内存里那份 XML 的 UTF-8 字节」算的 ⇒
        # 留底字节与它同源，**对得上**。
        assert label_config_sha(xml) == __import__("hashlib").sha256(raw).hexdigest()[:12]


# ── 用法错误退 2 ────────────────────────────────────────────────


class TestUsageErrors:
    def test_缺version退2(self) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["upload"])
        assert exc.value.code == 2

    def test_未知子命令退2(self) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["没有这个"])
        assert exc.value.code == 2
