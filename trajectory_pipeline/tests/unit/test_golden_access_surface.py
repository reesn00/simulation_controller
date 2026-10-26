"""金标的采集面轴（方案 20 §R2）：`annotate` 必填两栏 → `freeze` 抄进 `golden`。

为什么这条轴要守着
------------------
方案 20 §2 实测：同一台机器、同一天、同一批站点，**人工浏览器 12 个进得去、
camoufox 8 个被拦**。于是「人判这个站没片」与「agent 判这个站没片」可能根本
不是在说同一件事——读数差可能来自**面**，不来自模型。要分开这两件事，每条
金标都得记「在哪张面、什么时候」看到的；否则跨面比较只能不可判定。

守的是七件事：

1. **词表与驱动实现名同步**：`DRIVER_SURFACE` 的键必须等于
   `driver_factory.SUPPORTED_KINDS`——多一个驱动忘登记，那条链的采集面就
   只能靠猜（与 `surface_of_driver` 同纪律：认不出就抛）；
2. **`unrecorded` 只有一份**：与 `rubric_version.VERSION_UNRECORDED` 同口径，
   报表里不许出现第二种「没记」的写法；
3. **没记 ≠ 认不出**：空 / 缺键 ⇒ `unrecorded`；写错的面代号 ⇒ **抛**
   （混成一个，「填错了」永远看不见）；
4. **比不了就说比不了**：`same_surface` 在任一侧未声明时返回 `None`，
   **不是 `False`**——`False` 是「两面不同」这个结论，会把「没记」读成
   「跨面分歧」，而报表上这两个数是同一个格子；
5. **写入口必填**（O-d）：缺面 / 缺时刻 / 时刻不是 ISO 一律退 2，
   **不按默认面补**；
6. **冻结侧如实落档**：`golden.access_surface` 抄进去，manifest 记
   `by_surface` / `declared` / `unrecorded` —— **旧行不回填**，
   `unrecorded` 是合法读数、不是错误；
7. **`--refresh-label` 把面一起带过来**：面与时刻是同一次人判的另一半。
"""

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation import access_surface as A
from trajectory_pipeline.evaluation.golden import builder
from trajectory_pipeline.executor.browser.driver_factory import SUPPORTED_KINDS


# ── 工具 ────────────────────────────────────────────────────


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = builder.main(argv)
        except SystemExit as exc:  # pragma: no cover
            rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def _dump(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def _read(path: pathlib.Path) -> list[dict]:
    return [
        json.loads(x)
        for x in path.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]


def _cand(gid: str) -> dict:
    return {
        "golden_id": gid,
        "source_kind": "archive_outcome",
        "url": f"https://{gid}.test/watch",
        "site_url": f"https://{gid}.test/",
        "code": {"branch": "success", "evidence": f"{gid} 的证据"},
    }


def _ann(gid: str, **over) -> dict:
    """一条**新口径**标注：带面与时刻（旧行没有这两栏，见 §旧行不回填）。"""
    row = {
        "golden_id": gid,
        "round": 1,
        "label": "success",
        "label_label": "成功",
        "reviewed_by": "复核员",
        "reviewed_at": "2026-10-26T10:00:00+08:00",
        "note": "看得到播放器",
        "access_surface": A.SURFACE_HUMAN,
        "observed_at": "2026-10-26T09:58:00+08:00",
        "dimension_scores": None,
    }
    row.update(over)
    return row


def _root(tmp_path: pathlib.Path, *, ids=("g1",)) -> pathlib.Path:
    root = tmp_path / "golden"
    _dump(root / builder.CANDIDATES_FILE, [_cand(i) for i in ids])
    return root


# ── 词表与判据 ──────────────────────────────────────────────


def test_词表与驱动实现名同步():
    """多一个驱动没登记面 ⇒ 那条链的采集面只能猜。"""
    assert set(A.DRIVER_SURFACE) == set(SUPPORTED_KINDS)


def test_面代号不是工具面也不是库默认():
    """``plain`` 指「无额外隐身/代理层」，别写成 camoufox 出厂默认。"""
    assert A.SURFACE_HUMAN in A.ACCESS_SURFACES
    assert A.SURFACE_UNRECORDED not in A.ACCESS_SURFACES  # 「没记」不是一张面
    assert not any("default" in s for s in A.ACCESS_SURFACES)


def test_没记只有一种写法():
    assert A.SURFACE_UNRECORDED == builder.VERSION_UNRECORDED
    for row in ({}, {"access_surface": None}, {"access_surface": ""},
                {"access_surface": "  "}, {"access_surface": A.SURFACE_UNRECORDED}):
        assert A.surface_of_annotation(row) == A.SURFACE_UNRECORDED, row
        assert not A.is_declared(A.surface_of_annotation(row))


def test_填错了就抛而不是当成没记():
    with pytest.raises(ValueError) as exc:
        A.check_surface("camoufox-default")
    assert "human-browser" in str(exc.value)  # 报错要把合法值摆出来
    with pytest.raises(ValueError):
        A.surface_of_annotation({"access_surface": "chrome"})


def test_空驱动不按默认面补():
    """历史存档没记配置，且 10-22 之前默认实现还是 obscura ⇒ 补就是猜。"""
    assert A.surface_of_driver("") == A.SURFACE_UNRECORDED
    assert A.surface_of_driver(None) == A.SURFACE_UNRECORDED
    assert A.surface_of_driver("camoufox") == A.SURFACE_CAMOUFOX
    assert A.surface_of_driver("obscura") == A.SURFACE_OBSCURA
    with pytest.raises(ValueError):
        A.surface_of_driver("playwright")


def test_比不了就说比不了():
    """未声明 ⇒ None（不可判定），**不是 False**（那是「两面不同」的结论）。"""
    assert A.same_surface(A.SURFACE_HUMAN, A.SURFACE_HUMAN) is True
    assert A.same_surface(A.SURFACE_HUMAN, A.SURFACE_CAMOUFOX) is False
    assert A.same_surface(A.SURFACE_HUMAN, A.SURFACE_UNRECORDED) is None
    assert A.same_surface(None, None) is None
    assert A.same_surface(A.SURFACE_UNRECORDED, A.SURFACE_CAMOUFOX) is None


# ── annotate：写入口必填 ─────────────────────────────────────


def test_缺面退2且说清两种合法来源(tmp_path):
    root = _root(tmp_path)
    labels = tmp_path / "labels.jsonl"
    row = _ann("g1")
    row.pop("access_surface")
    _dump(labels, [row])
    rc, _, err = _run(
        ["--golden-root", str(root), "annotate", "--input", str(labels)]
    )
    assert rc == 2
    assert "缺 access_surface" in err
    assert A.SURFACE_HUMAN in err and A.SURFACE_CAMOUFOX in err
    assert not (root / builder.ANNOTATIONS_FILE).exists()  # 一条都没写进去


def test_面写错退2并列合法值(tmp_path):
    root = _root(tmp_path)
    labels = tmp_path / "labels.jsonl"
    _dump(labels, [_ann("g1", access_surface="human_browser")])
    rc, _, err = _run(
        ["--golden-root", str(root), "annotate", "--input", str(labels)]
    )
    assert rc == 2
    assert "认不出的采集面" in err
    assert A.SURFACE_OBSCURA in err


def test_缺时刻退2(tmp_path):
    root = _root(tmp_path)
    labels = tmp_path / "labels.jsonl"
    row = _ann("g1")
    row.pop("observed_at")
    _dump(labels, [row])
    rc, _, err = _run(
        ["--golden-root", str(root), "annotate", "--input", str(labels)]
    )
    assert rc == 2
    assert "observed_at" in err


def test_时刻不是ISO退2(tmp_path):
    root = _root(tmp_path)
    labels = tmp_path / "labels.jsonl"
    _dump(labels, [_ann("g1", observed_at="昨天下午")])
    rc, _, err = _run(
        ["--golden-root", str(root), "annotate", "--input", str(labels)]
    )
    assert rc == 2
    assert "ISO-8601" in err


def test_填了就落进annotations(tmp_path):
    root = _root(tmp_path)
    labels = tmp_path / "labels.jsonl"
    _dump(labels, [_ann("g1")])
    rc, _, _ = _run(
        ["--golden-root", str(root), "annotate", "--input", str(labels)]
    )
    assert rc == 0
    rows = _read(root / builder.ANNOTATIONS_FILE)
    assert rows[0]["access_surface"] == A.SURFACE_HUMAN
    assert rows[0]["observed_at"] == "2026-10-26T09:58:00+08:00"


# ── freeze：抄进 golden + 如实计数 ──────────────────────────


def test_冻结把面抄进golden并计数(tmp_path):
    root = _root(tmp_path)
    _dump(root / builder.ANNOTATIONS_FILE, [_ann("g1")])
    rc, out, _ = _run(["--golden-root", str(root), "freeze", "--force"])
    assert rc == 0, out
    rows = _read(root / "v1" / "calibration.jsonl") + _read(root / "v1" / "holdout.jsonl")
    golden = {r["golden_id"]: r["golden"] for r in rows}
    assert golden["g1"]["access_surface"] == A.SURFACE_HUMAN
    assert golden["g1"]["observed_at"] == "2026-10-26T09:58:00+08:00"
    manifest = json.loads((root / "v1" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["access_surface"] == {
        "by_surface": {A.SURFACE_HUMAN: 1},
        "declared": 1,
        "unrecorded": 0,
    }
    assert A.SURFACE_HUMAN in out


def test_旧行没有面就写unrecorded且不报错(tmp_path):
    """**不回填**：历史行没这两栏，冻结照过，只是记成「面未声明」。"""
    root = _root(tmp_path)
    row = _ann("g1")
    row.pop("access_surface")
    row.pop("observed_at")
    _dump(root / builder.ANNOTATIONS_FILE, [row])
    rc, out, err = _run(["--golden-root", str(root), "freeze", "--force"])
    assert rc == 0, (out, err)
    rows = _read(root / "v1" / "calibration.jsonl") + _read(root / "v1" / "holdout.jsonl")
    golden = {r["golden_id"]: r["golden"] for r in rows}
    assert golden["g1"]["access_surface"] == A.SURFACE_UNRECORDED
    assert "observed_at" not in golden["g1"]  # 没有时刻，不编一个
    manifest = json.loads((root / "v1" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["access_surface"]["unrecorded"] == 1
    assert manifest["access_surface"]["declared"] == 0
    assert "面未声明" in err


def test_refresh_label把面一起带过来(tmp_path):
    """面与时刻是同一次人判的另一半：重挂 label 时一起换。"""
    root = _root(tmp_path)
    vdir = root / "v7"
    frozen = {
        "golden_id": "g1",
        "source_kind": "archive_outcome",
        "url": "https://g1.test/watch",
        "site_url": "https://g1.test/",
        "code": {"branch": "success", "evidence": "g1 的证据"},
        "golden": {
            "label": "success",
            "label_label": "成功",
            "reviewed_by": "复核员",
            "reviewed_at": "2026-10-26T10:00:00+08:00",
            "note": "看得到播放器",
            "dimension_scores": None,
            "access_surface": A.SURFACE_UNRECORDED,
        },
        "dimension_scores": {"d5": 2},
        "scores_detail": [],
    }
    _dump(vdir / builder.SPLIT_FILES[builder.SPLIT_CALIBRATION], [frozen])
    (vdir / "manifest.json").write_text(
        json.dumps(
            {
                "version": 7,
                "holdout_runs": 0,
                "seed": 1,
                "counts": {"calibration": 1},
                "split": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _dump(root / builder.VERSIONS_INDEX, [{"version": 7}])
    _dump(
        root / builder.ANNOTATIONS_FILE,
        [_ann("g1", label="subscription_wall", label_label="付费墙")],
    )
    rc, out, err = _run(
        [
            "--golden-root", str(root), "freeze",
            "--from-version", "7", "--refresh-label", "--force",
        ]
    )
    assert rc == 0, (out, err)
    row = _read(root / "v8" / builder.SPLIT_FILES[builder.SPLIT_CALIBRATION])[0]
    assert row["golden"]["label"] == "subscription_wall"
    assert row["golden"]["access_surface"] == A.SURFACE_HUMAN
    assert row["golden"]["observed_at"] == "2026-10-26T09:58:00+08:00"
    assert row["dimension_scores"] == {"d5": 2}  # pred 侧仍不动