"""``overlay-scout`` 的回填口径（M-29 S1）。

S1 修的是一件事：**「评分器弃权」与「压根没测」在快照里长得一模一样**。

删键（只写成功打分的维度）时，两者在快照里都表现为「该维没有条目」——
弃权行无声消失，那 104 行（弃权 26 + 无样本 78）消失得干干净净，
读数对不上也看不出异常。改成：

  - **有样本 ⇒ 四维键齐全**，值可以是 ``null``（该维弃权）；
  - **没有样本 ⇒ 留 ``{}``**（``status`` 标明是哪种没样本）。

还有一条同源的病：没样本的行若用 ``setdefault``，上一轮留下的旧分数会
继续躺在行里，「这行现在没样本」就看不见了。改成显式写空。

下游吃 ``null`` 是既有的（``dim_agreement(None, p)`` 返回 ``None`` 不计入
分母；``_compute_soft_dim_agreement`` 里 ``p is None → skipped_no_pred``），
所以这里不需要改下游，只需要锁住「键齐全」这个约定。
"""

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation.golden import builder


# ── 工具 ────────────────────────────────────────────────────────


def _cand(
    gid: str,
    *,
    source_kind: str = "archive_outcome",
    dims: dict | None = None,
    **extra,
) -> dict:
    row = {
        "golden_id": gid,
        "source_kind": source_kind,
        "task_id": "T001",
        "run_id": "abc123",
        "url": "https://example.com/x",
        "code": {"branch": "unresolved", "evidence": "原话"},
        "golden": {"label": "success", "note": ""},
        "retest": {"label": "success"},
        "dimension_scores": dict(dims or {}),
    }
    row.update(extra)
    return row


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


def _scores(**by_dim) -> list[dict]:
    """造 ``_score_one`` 的返回形状（四维各一条，弃权为 ``None``）。"""
    return [
        {
            "dimension": dim,
            "score": by_dim.get(dim),
            "is_veto": False,
            "reasons": [],
        }
        for dim in ("d2", "d4", "d5", "d6")
    ]


@pytest.fixture()
def stub_scorer(monkeypatch):
    """把样本还原与评分都换成桩——本文件测的是**回填口径**，不是评分器。"""

    def _install(scores, *, have_sample=True):
        monkeypatch.setattr(
            builder,
            "_resolve_sample_for_candidate",
            lambda row, p2_root: object() if have_sample else None,
        )
        import trajectory_pipeline.evaluation.scout.io as scout_io

        monkeypatch.setattr(scout_io, "_score_one", lambda sample: list(scores))

    return _install


# ── 1. 弃权写 null，不删键 ──────────────────────────────────────


def test_弃权维度写null不删键(tmp_path, stub_scorer):
    """弃权 = 该维判不可判定。它必须**看得见**，而不是表现为键消失。"""
    stub_scorer(_scores(d2=5, d4=None, d5=2, d6=5))
    root = tmp_path / "golden"
    _write_jsonl(root / builder.CANDIDATES_FILE, [_cand("g1")])

    rc, out, err = _run([
        "--golden-root", str(root), "overlay-scout",
    ])
    assert rc == 0, (out, err)

    row = _read_jsonl(root / builder.CANDIDATES_FILE)[0]
    # 四维键齐全
    assert set(row["dimension_scores"]) == {"d2", "d4", "d5", "d6"}
    # 弃权的那一维是 null，不是缺键、也不是 0 分
    assert row["dimension_scores"]["d4"] is None
    assert row["dimension_scores"]["d5"] == 2
    assert row["dimension_scores"]["d2"] == 5
    assert row["status"] == "scored"


def test_四维全弃权也要留四个键(tmp_path, stub_scorer):
    """全弃权是最容易被「优化掉」的情形——空 dict 与「没样本」就撞上了。"""
    stub_scorer(_scores())
    root = tmp_path / "golden"
    _write_jsonl(root / builder.CANDIDATES_FILE, [_cand("g1")])

    assert _run(["--golden-root", str(root), "overlay-scout"])[0] == 0

    row = _read_jsonl(root / builder.CANDIDATES_FILE)[0]
    assert set(row["dimension_scores"]) == {"d2", "d4", "d5", "d6"}
    assert all(v is None for v in row["dimension_scores"].values())
    assert row["status"] == "scored"


# ── 2. 没样本 ⇒ 空 dict + 三态 status ───────────────────────────


def test_没样本留空dict(tmp_path, stub_scorer):
    stub_scorer(_scores(), have_sample=False)
    root = tmp_path / "golden"
    _write_jsonl(root / builder.CANDIDATES_FILE, [_cand("g1")])

    assert _run(["--golden-root", str(root), "overlay-scout"])[0] == 0

    row = _read_jsonl(root / builder.CANDIDATES_FILE)[0]
    assert row["dimension_scores"] == {}
    assert row["scores_detail"] == []
    assert row["status"] == "missing_sample"


def test_负样本池单独标状态(tmp_path, stub_scorer):
    """负样本池只有 outcome 行没有样本——它不是「缺件」，是另一种「没有」。"""
    stub_scorer(_scores(), have_sample=False)
    root = tmp_path / "golden"
    _write_jsonl(
        root / builder.CANDIDATES_FILE,
        [_cand("g1", source_kind="negative_pool")],
    )

    assert _run(["--golden-root", str(root), "overlay-scout"])[0] == 0

    row = _read_jsonl(root / builder.CANDIDATES_FILE)[0]
    assert row["status"] == "negative_pool_no_sample"
    assert row["dimension_scores"] == {}


def test_没样本不许留着上一轮的旧分数(tmp_path, stub_scorer):
    """这行现在没样本 ⇒ 行里就不该有分数。留着旧的等于谎报。"""
    stub_scorer(_scores(), have_sample=False)
    root = tmp_path / "golden"
    _write_jsonl(
        root / builder.CANDIDATES_FILE,
        [_cand("g1", dims={"d5": 5})],
    )

    assert _run(["--golden-root", str(root), "overlay-scout"])[0] == 0

    row = _read_jsonl(root / builder.CANDIDATES_FILE)[0]
    assert row["dimension_scores"] == {}
    assert row["status"] == "missing_sample"


# ── 3. 原有字段一格不动 ─────────────────────────────────────────


def test_原有字段一格不动(tmp_path, stub_scorer):
    """overlay 只挂评分：分支结论、金标、retest 一个字都不许动。"""
    stub_scorer(_scores(d2=5, d4=4, d5=5, d6=5))
    root = tmp_path / "golden"
    before = _cand("g1")
    _write_jsonl(root / builder.CANDIDATES_FILE, [before])

    assert _run(["--golden-root", str(root), "overlay-scout"])[0] == 0

    after = _read_jsonl(root / builder.CANDIDATES_FILE)[0]
    for key in ("code", "golden", "retest", "url", "task_id", "run_id", "source_kind"):
        assert after[key] == before[key], key
    assert after["golden_id"] == before["golden_id"]


# ── 4. 报表读数 ─────────────────────────────────────────────────


def test_报表把弃权算进分布(tmp_path, stub_scorer):
    """分布里必须看得见弃权——``{5: 2, None: 1}`` 而不是 ``{5: 2}``。"""
    stub_scorer(_scores(d2=5, d4=5, d5=None, d6=5))
    root = tmp_path / "golden"
    _write_jsonl(root / builder.CANDIDATES_FILE, [_cand("g1")])

    rc, out, err = _run([
        "--golden-root", str(root), "overlay-scout", "--dry-run",
    ])
    assert rc == 0, (out, err)
    assert "None" in out
    assert "d5" in out


def test_dry_run不落盘(tmp_path, stub_scorer):
    stub_scorer(_scores(d2=5, d4=5, d5=5, d6=5))
    root = tmp_path / "golden"
    _write_jsonl(root / builder.CANDIDATES_FILE, [_cand("g1")])
    before = (root / builder.CANDIDATES_FILE).read_text(encoding="utf-8")

    assert _run([
        "--golden-root", str(root), "overlay-scout", "--dry-run",
    ])[0] == 0
    assert (root / builder.CANDIDATES_FILE).read_text(encoding="utf-8") == before
