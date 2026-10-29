"""M-53：**「冻结了但没人推」不能静默**。

要拦的失败形态
--------------
owner 裁定①：**每一个增量版本都要上 LS**。但冻结与上传是**两条命令**，
中间没有自动接上 ⇒ 冻结完忘了推，**不报任何错**：冻结档在手、LS 上少一版，
两边各自看着都对，直到某天发现「这一版没人看过」。

⇒ 两条看得见的口子：

1. ``golden_ls pending --strict`` —— 欠账（冻结了但没建 LS 项目）非空就
   **退 1**（状态问题，不是用法错误 ⇒ 不是 2）；
2. ``golden.builder freeze`` 收尾**提醒**一句「若本版要上 LS：跑 pending
   看欠账」——**只提醒，不判断**（builder 不 import golden_ls，依赖方向是
   golden_ls → golden；欠账由 ``golden_ls pending`` 算）。

⚠️ **第 2 条刻意不做成门禁**：它挂在 ``freeze`` 的 stdout 上，不改退出码。
把欠账判成冻结失败 ⇒ 人会为了「让 freeze 过」而去建项目，那就把「欠账」
变成了「必须立刻上 LS」——裁定③（holdout 也上）与「攒几版一起上」的自由
度就没了。**看得见 ≠ 卡得住**。
"""

from __future__ import annotations

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

from trajectory_pipeline.evaluation.golden import builder
from trajectory_pipeline.evaluation.golden_ls import cli


# ── golden_ls pending ─────────────────────────────────────────


class TestPendingStrict:
    """``pending --strict``：欠账非空 ⇒ **退 1**。"""

    def _root(self, tmp_path: pathlib.Path) -> pathlib.Path:
        for name in ("v14", "v15"):
            (tmp_path / name).mkdir(parents=True)
        return tmp_path

    def test_有欠账时strict退1(self, tmp_path, capsys) -> None:
        root = self._root(tmp_path)
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text(
            '{"version": 14, "project_id": 78, "title": "golden-v14"}\n',
            encoding="utf-8",
        )
        rc = cli.main(
            [
                "--ledger",
                str(ledger),
                "--golden-root",
                str(root),
                "pending",
                "--strict",
            ]
        )
        assert rc == 1  # **状态问题**，不是用法错误（用法错误是 2）
        captured = capsys.readouterr()
        assert "欠账" in captured.err  # 错误走 stderr
        assert "15" in captured.out  # 清单走 stdout

    def test_没欠账时strict退0(self, tmp_path, capsys) -> None:
        root = self._root(tmp_path)
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text(
            '{"version": 14, "project_id": 78, "title": "golden-v14"}\n'
            '{"version": 15, "project_id": 79, "title": "golden-v15"}\n',
            encoding="utf-8",
        )
        assert cli.main(
            [
                "--ledger",
                str(ledger),
                "--golden-root",
                str(root),
                "pending",
                "--strict",
            ]
        ) == 0

    def test_缺省strict时退0(self, tmp_path, capsys) -> None:
        """缺省**退 0**：它是给人看的清单，不是门禁。"""
        root = self._root(tmp_path)
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text(
            '{"version": 14, "project_id": 78, "title": "golden-v14"}\n',
            encoding="utf-8",
        )
        assert cli.main(
            ["--ledger", str(ledger), "--golden-root", str(root), "pending"]
        ) == 0
        assert "欠账" in capsys.readouterr().out


# ── builder freeze 的提醒 ─────────────────────────────────────


def _dump(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


def _frozen(gid: str) -> dict:
    return {
        "golden_id": gid,
        "source_kind": "archive_outcome",
        "url": f"https://{gid}.test/",
        "site_url": f"https://{gid}.test/",
        "code": {"branch": "success", "evidence": f"{gid} 的证据"},
        "golden": {
            "label": "success",
            "label_label": "成功",
            "reviewed_by": "manual-review",
            "reviewed_at": "2026-10-01",
            "note": "标签理由",
            "dimension_scores": {"d5": 4},
        },
        "dimension_scores": {"d5": 2},
        "scores_detail": [],
    }


def _fixture(tmp_path: pathlib.Path, *, version: int = 1) -> pathlib.Path:
    """造一个已冻结的小版本（3 调参 / 1 回归 / 2 持有）。"""
    root = tmp_path / "golden"
    vdir = root / f"v{version}"
    _dump(vdir / "calibration.jsonl", [_frozen("g1"), _frozen("g3"), _frozen("g5")])
    _dump(vdir / "holdout.jsonl", [_frozen("g2"), _frozen("g4")])
    _dump(vdir / "regression.jsonl", [_frozen("r1")])
    reg_fp = builder._rows_fingerprint(
        [
            json.loads(x)
            for x in (vdir / "regression.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()
        ]
    )
    (vdir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "label_schema_version": builder.LABEL_SCHEMA_VERSION,
                "version": version,
                "holdout_runs": 0,
                "seed": 42,
                "counts": {"calibration": 3, "holdout": 2, "regression": 1},
                "split": {
                    "regression_fingerprint": reg_fp,
                    "regression_seed": 20261022,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _dump(root / builder.VERSIONS_INDEX, [{"version": version}])
    return root


class TestFreezeReminder:
    """``freeze`` 收尾**提醒**上 LS——但**不改退出码**。"""

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = builder.main(argv)
            except SystemExit as exc:  # pragma: no cover
                rc = int(exc.code or 0)
        return rc, out.getvalue(), err.getvalue()

    def test_冻结完提醒去看欠账(self, tmp_path) -> None:
        root = _fixture(tmp_path)
        rc, out, _ = self._run(
            ["--golden-root", str(root), "freeze", "--from-version", "1"]
        )
        assert rc == 0
        assert "golden_ls" in out and "pending" in out
        # 提醒里要带上**本版版本号**——不然人不知道该 init 哪一版
        assert "init-project --version 2" in out

    def test_提醒不改退出码(self, tmp_path) -> None:
        """⚠️ **只提醒，不卡**：欠账不该让冻结失败。

        把「上不上 LS」变成冻结的前置条件 ⇒ 人为了过门禁就得建项目 ⇒
        裁定③（holdout 也上）与「攒几版一起上」的自由度就没了。
        """
        root = _fixture(tmp_path)
        rc, _, _ = self._run(["--golden-root", str(root), "freeze", "--from-version", "1"])
        assert rc == 0
