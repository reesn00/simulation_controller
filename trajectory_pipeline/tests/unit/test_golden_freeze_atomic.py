"""M-22：freeze 要么整版落成，要么什么都不留。

旧写盘顺序是 ``mkdir(vN)`` → ``manifest.json`` → 三档 / trajectories →
``versions.jsonl``。中途失败会留下一个只有 manifest 的半截 ``vN/``，而
``cmd_freeze`` 顶部的 ``vdir.exists()`` 会让**同版本号永久冻不了**——只能
人工删目录。修法：先写 ``.vN.staging-*``，全部写完再原子改名；索引写失败
则把刚改名的目录撤掉。

两条测试分别打在两个断点上：

    1. 三档写到一半失败（staging 里已有 manifest）→ 不留 vN，可重冻；
    2. ``versions.jsonl`` 写入失败（vN 已改名）→ 撤掉 vN，可重冻。
"""
from __future__ import annotations

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout
from typing import Callable

import pytest

from trajectory_pipeline.evaluation.golden import builder


def _row(golden_id: str, label: str) -> dict:
    return {
        "golden_id": golden_id,
        "source_kind": "archive_outcome",
        "task_id": "T001",
        "run_id": "abc12345",
        "url": f"https://{golden_id}.test/",
        "site_url": f"https://{golden_id}.test/",
        "play_page_url": "",
        "code": {"branch": None, "branch_label": "success"},
        "persona": None,
        "golden": {"label": label},
        "retest": None,
    }


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _prepare_golden_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """造 6 条已标注候选（两类标签）；切分比例需 ``--force``。"""
    labels = ["success", "success", "success", "broken", "broken", "broken"]
    candidates = [_row(f"g{i}", lab) for i, lab in enumerate(labels)]
    annotations = [
        {
            "golden_id": f"g{i}",
            "round": 1,
            "label": lab,
            "label_label": lab,
            "reviewed_by": "manual-review",
            "reviewed_at": "2026-10-22T00:00:00+00:00",
            "note": "",
        }
        for i, lab in enumerate(labels)
    ]
    root = tmp_path / "golden"
    _write_jsonl(root / builder.CANDIDATES_FILE, candidates)
    _write_jsonl(root / builder.ANNOTATIONS_FILE, annotations)
    return root


def _freeze_argv(root: pathlib.Path) -> list[str]:
    return ["--golden-root", str(root), "freeze", "--version", "7", "--force"]


def _run(argv: list[str]) -> int:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        return builder.main(argv)


def _freeze_must_fail(root: pathlib.Path) -> None:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        with pytest.raises(OSError, match="磁盘满"):
            builder.main(_freeze_argv(root))


def _assert_no_half_frozen(root: pathlib.Path) -> None:
    assert not (root / "v7").exists(), "中途失败后仍留下 v7/——同版本会永久冻不了"
    assert not list(root.glob(".v7.staging-*")), "暂存目录没清理"


def _retry_must_succeed(root: pathlib.Path) -> None:
    assert _run(_freeze_argv(root)) == 0
    assert (root / "v7" / "manifest.json").exists()
    index = [
        json.loads(line)
        for line in (root / builder.VERSIONS_INDEX)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert [int(r["version"]) for r in index] == [7], "重冻后索引应恰好一条 v7"


def _boom_on(name: str, real: Callable) -> Callable:
    """包住 ``_dump_jsonl``：写 ``name`` 这个文件时抛错，其余照常。"""

    def wrapper(path: pathlib.Path, rows, force: bool = False):
        if path.name == name:
            raise OSError("磁盘满了")
        return real(path, rows, force=force)

    return wrapper


def test_三档写到一半失败_不留半截vdir且可重冻(tmp_path, monkeypatch):
    """失败点在 manifest 已落进 staging、三档还没写完时。"""
    root = _prepare_golden_root(tmp_path)
    real = builder._dump_jsonl
    monkeypatch.setattr(
        builder,
        "_dump_jsonl",
        _boom_on(builder.SPLIT_FILES[builder.SPLIT_CALIBRATION], real),
    )

    _freeze_must_fail(root)
    _assert_no_half_frozen(root)

    monkeypatch.setattr(builder, "_dump_jsonl", real)
    _retry_must_succeed(root)


def test_索引写入失败_撤掉已改名目录且可重冻(tmp_path, monkeypatch):
    """失败点在 v7 已原子改名、``versions.jsonl`` 还没写进去时。"""
    root = _prepare_golden_root(tmp_path)
    real = builder._dump_jsonl
    monkeypatch.setattr(
        builder, "_dump_jsonl", _boom_on(builder.VERSIONS_INDEX, real)
    )

    _freeze_must_fail(root)
    _assert_no_half_frozen(root)

    monkeypatch.setattr(builder, "_dump_jsonl", real)
    _retry_must_succeed(root)