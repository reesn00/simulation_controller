"""C-05 分诊档与六桶的守门测试。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §3.1 / §4 / C-05。

守的是五件事：

1. **六桶是封闭集**：未登记桶一律拒（没有 `other`，不认识就留空）；
2. **理由必填**：`triage_evidence` 写的是「为什么不是别的桶」——只写"判错了"不算；
3. **`bucket` 只能人填**（D-C3）：`rubric_*` / `gold_defect` 是尺子与真值的裁定，
   agent 写进来一律拒；机器身份按**显式名单 + 前缀**判，不靠"看起来像"猜；
4. **责任方由桶推导**，手填冲突即拒（否则 C-07 的分组会错位）；
5. **写入口只有一条**：`add_rows` 先校验再合并；默认 dry-run，改判要 `--replace`，
   同一条重复跑是幂等而不是两行。

`verify.analyze` / `gate` 也吃同一套校验（不是只在校验 CLI 那一侧）——见最后一节。
"""

import io
import json

import pytest

from trajectory_pipeline.evaluation.calibration import buckets, triage, verify
from trajectory_pipeline.evaluation.calibration.snapshot import HARD_DIM


def _row(**over) -> dict:
    row = {
        "golden_id": "P000__r1__h",
        "dimension": HARD_DIM,
        "bucket": buckets.BUCKET_EVALUATOR_LOGIC,
        "triage_by": "owner",
        "triage_evidence": "按 D-3.5 明写条款应给 PAYWALL，金标与准则均无歧义",
    }
    row.update(over)
    return row


def _write(path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


def _main(argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = triage.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ── 1. 桶表：封闭集与推导 ───────────────────────────────────


def test_未登记桶一律keyerror_并列出六桶():
    with pytest.raises(KeyError) as ei:
        buckets.require_bucket("other")
    msg = str(ei.value.args[0])
    for name in buckets.BUCKETS:
        assert name in msg
    # 状态不是桶：混进来就会变成万能垃圾桶（§3.3）
    with pytest.raises(KeyError):
        buckets.require_bucket("accepted_exception")


def test_责任方与回路覆盖六桶全集():
    assert set(buckets.BUCKET_OWNER) == set(buckets.BUCKETS)
    assert set(buckets.BUCKET_LOOP) == set(buckets.BUCKETS)
    assert set(buckets.BUCKET_PRECONDITION) == set(buckets.BUCKETS)
    assert buckets.buckets_in_loop("evaluator") == (
        buckets.BUCKET_EVALUATOR_LOGIC,
        buckets.BUCKET_EVALUATOR_PROMPT,
    )
    assert buckets.buckets_in_loop("rubric") == (
        buckets.BUCKET_RUBRIC_AMBIGUOUS,
        buckets.BUCKET_RUBRIC_MISSING,
    )
    with pytest.raises(KeyError):
        buckets.buckets_in_loop("other")


def test_只能人填的三个桶由前缀派生():
    assert buckets.HUMAN_ONLY == frozenset(
        {buckets.BUCKET_RUBRIC_AMBIGUOUS, buckets.BUCKET_RUBRIC_MISSING,
         buckets.BUCKET_GOLD_DEFECT}
    )
    assert not buckets.is_human_only(buckets.BUCKET_EVALUATOR_LOGIC)
    assert not buckets.is_human_only(buckets.BUCKET_EVIDENCE_MISSING)
    with pytest.raises(KeyError):
        buckets.is_human_only("nope")


def test_前置事实写清了什么不算():
    """每条前置事实都要能挡住「看起来像」的那一侧（§4）。"""
    for bucket, text in buckets.BUCKET_PRECONDITION.items():
        assert len(text) >= 30, bucket
    assert "不算" in buckets.BUCKET_PRECONDITION[buckets.BUCKET_EVALUATOR_LOGIC]
    assert "不算" in buckets.BUCKET_PRECONDITION[buckets.BUCKET_GOLD_DEFECT]
    assert "根本没写" in buckets.BUCKET_PRECONDITION[buckets.BUCKET_RUBRIC_MISSING]


# ── 2. 身份：谁能写 bucket ──────────────────────────────────


def test_合法分诊_责任方自动推导_状态默认assigned():
    out = triage.validate_row(_row())
    assert out["responsibility"] == "code"
    assert out["status"] == triage.STATUS_ASSIGNED
    assert out["bucket"] == buckets.BUCKET_EVALUATOR_LOGIC


def test_机器身份写bucket一律拒():
    for actor in ("agent", "AGENT", "llm:qwen", "agent:coder", "bot"):
        assert triage.actor_kind(actor) == "agent"
        with pytest.raises(triage.TriageError, match="只能写建议值"):
            triage.validate_row(_row(triage_by=actor))


def test_agent写rubric桶_拒且点名D_C3():
    with pytest.raises(triage.TriageError) as ei:
        triage.validate_row(
            _row(bucket=buckets.BUCKET_RUBRIC_MISSING, triage_by="llm:judge")
        )
    assert "D-C3" in str(ei.value)


def test_人的名字里带关键字也算人():
    # 名单 + 前缀之外一律当人：把人的名字判成机器会让分诊写不进来
    assert triage.actor_kind("agent-smith") == "human"
    assert triage.actor_kind("Qwen") == "human"


def test_空签名拒():
    with pytest.raises(triage.TriageError, match="签名"):
        triage.validate_row(_row(triage_by="  "))


# ── 3. 字段校验 ─────────────────────────────────────────────


def test_缺理由拒_且说清理由要写什么():
    with pytest.raises(triage.TriageError) as ei:
        triage.validate_row(_row(triage_evidence=""))
    assert "为什么不是别的桶" in str(ei.value)


def test_缺任一必填字段都说得出是哪几个():
    with pytest.raises(triage.TriageError, match="triage_by"):
        triage.validate_row(_row(triage_by=None))


def test_未登记桶拒_错误里带六桶():
    with pytest.raises(triage.TriageError) as ei:
        triage.validate_row(_row(bucket="gold_label"))
    assert "evaluator_logic" in str(ei.value)


def test_维度大小写写错拒():
    with pytest.raises(triage.TriageError, match="D10"):
        triage.validate_row(_row(dimension="D10"))
    # 软维度白名单内可以
    assert triage.validate_row(_row(dimension="d4"))["dimension"] == "d4"


def test_手填责任方与桶冲突拒():
    with pytest.raises(triage.TriageError, match="由桶推导"):
        triage.validate_row(_row(responsibility="rubric"))
    assert triage.validate_row(_row(responsibility="code"))["responsibility"] == "code"


def test_未知状态拒():
    with pytest.raises(triage.TriageError, match="状态机"):
        triage.validate_row(_row(status="other"))


def test_同键两个不同桶拒():
    rows = [_row(), _row(bucket=buckets.BUCKET_GOLD_DEFECT)]
    with pytest.raises(triage.TriageError, match="两个桶"):
        triage.validate_rows(rows)


def test_同键同桶重复_收下第一份():
    rows = [_row(), _row()]
    assert len(triage.validate_rows(rows)) == 1


# ── 4. 读 ───────────────────────────────────────────────────


def test_load_triage_没给就是None_给了空路径是错误():
    assert triage.load_triage(None) is None
    with pytest.raises(triage.TriageError, match="没给路径"):
        triage.load_triage([])


def test_load_triage_文件不存在拒(tmp_path):
    with pytest.raises(triage.TriageError, match="不存在"):
        triage.load_triage([str(tmp_path / "nope.jsonl")])


def test_load_triage_坏JSON报行号(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('{"golden_id": "a"}\nnot json\n', encoding="utf-8")
    with pytest.raises(triage.TriageError, match=":2"):
        triage.load_triage([str(p)])


def test_load_triage_读多份并逐条校验(tmp_path):
    good = tmp_path / "a.jsonl"
    _write(good, [_row()])
    bad = tmp_path / "b.jsonl"
    _write(bad, [_row(golden_id="P001__r1__h", triage_evidence="")])
    assert len(triage.load_triage([str(good)])) == 1
    with pytest.raises(triage.TriageError, match="为什么不是别的桶"):
        triage.load_triage([str(good), str(bad)])


# ── 5. 写 ───────────────────────────────────────────────────


def test_默认dry_run不写盘(tmp_path):
    p = tmp_path / "triage.jsonl"
    result = triage.add_rows([_row()], path=p)
    assert result["applied"] is False
    assert result["added"] == 1
    assert not p.exists()


def test_apply后可再load_且带triage_at(tmp_path):
    p = tmp_path / "triage.jsonl"
    triage.add_rows([_row()], path=p, apply=True)
    rows = triage.load_triage([str(p)])
    assert len(rows) == 1
    assert rows[0]["triage_at"]
    assert rows[0]["bucket"] == buckets.BUCKET_EVALUATOR_LOGIC


def test_同一条重复add是幂等(tmp_path):
    p = tmp_path / "triage.jsonl"
    triage.add_rows([_row()], path=p, apply=True)
    again = triage.add_rows([_row()], path=p, apply=True)
    assert (again["added"], again["skipped"], again["replaced"]) == (0, 1, 0)
    assert len(triage.load_triage([str(p)])) == 1


def test_改判默认拒_加replace才覆盖(tmp_path):
    p = tmp_path / "triage.jsonl"
    triage.add_rows([_row()], path=p, apply=True)
    changed = _row(bucket=buckets.BUCKET_GOLD_DEFECT, triage_evidence="标注员读错尺子")
    with pytest.raises(triage.TriageError, match="--replace"):
        triage.add_rows([changed], path=p, apply=True)
    result = triage.add_rows([changed], path=p, apply=True, replace=True)
    assert (result["added"], result["skipped"], result["replaced"]) == (0, 0, 1)
    rows = triage.load_triage([str(p)])
    assert rows[0]["bucket"] == buckets.BUCKET_GOLD_DEFECT


def test_追加保留已有行的顺序(tmp_path):
    p = tmp_path / "triage.jsonl"
    first = _row(golden_id="A__r1__h")
    second = _row(golden_id="B__r1__h")
    triage.add_rows([first], path=p, apply=True)
    triage.add_rows([second], path=p, apply=True)
    rows = triage.load_triage([str(p)])
    assert [r["golden_id"] for r in rows] == ["A__r1__h", "B__r1__h"]


def test_写盘前就拒_不留下半截文件(tmp_path):
    p = tmp_path / "triage.jsonl"
    with pytest.raises(triage.TriageError):
        triage.add_rows(
            [_row(), _row(golden_id="B__r1__h", bucket="gold_label")],
            path=p, apply=True,
        )
    assert not p.exists()


# ── 6. CLI ─────────────────────────────────────────────────


def test_check_缺省档不存在退2(tmp_path, monkeypatch):
    monkeypatch.setattr(triage, "DEFAULT_TRIAGE_FILE", tmp_path / "none.jsonl")
    rc, _, err = _main(["check"])
    assert rc == 2
    assert "不存在" in err


def test_check_合法退0并打印桶分布(tmp_path):
    p = tmp_path / "triage.jsonl"
    _write(p, [_row(), _row(golden_id="B__r1__h", bucket=buckets.BUCKET_GOLD_DEFECT,
                            triage_evidence="标注员读错尺子，能指到 B-3")])
    rc, out, _ = _main(["check", str(p)])
    assert rc == 0
    assert "共 2 行" in out
    assert "（只能人填）" in out


def test_add_默认dryrun打印不落盘(tmp_path):
    p = tmp_path / "triage.jsonl"
    rc, out, _ = _main([
        "add", "--golden-id", "P000__r1__h", "--dimension", "d10",
        "--bucket", buckets.BUCKET_EVALUATOR_LOGIC, "--by", "owner",
        "--evidence", "按明写条款应得另一个值", "--path", str(p),
    ])
    assert rc == 0
    assert "dry-run" in out
    assert not p.exists()


def test_add_机器身份退2(tmp_path):
    p = tmp_path / "triage.jsonl"
    rc, _, err = _main([
        "add", "--golden-id", "P000__r1__h", "--dimension", "d10",
        "--bucket", buckets.BUCKET_RUBRIC_MISSING, "--by", "agent",
        "--evidence", "准则没写这种形态", "--path", str(p),
    ])
    assert rc == 2
    assert "只能写建议值" in err


# ── 7. 同一套校验也守在 verify / gate 的 API 上 ───────────────


def test_analyze_拒绝未经校验的分诊行():
    """从库内直接传一组 dict 也不许绕开 C-05。"""
    snap = {"schema_version": "1", "meta": {"hard_dim": HARD_DIM}, "entries": []}
    with pytest.raises(verify.VerifyError, match="签名"):
        verify.analyze(
            snap, dict(snap),
            triage=[{"golden_id": "A", "dimension": "d10", "bucket": "evaluator_logic"}],
            buckets=["evaluator_logic"],
        )


def test_analyze_拒未登记桶():
    snap = {"schema_version": "1", "meta": {"hard_dim": HARD_DIM}, "entries": []}
    with pytest.raises(verify.VerifyError, match="六桶封闭集"):
        verify.analyze(
            snap, dict(snap), triage=[_row(bucket="gold_label")],
            buckets=["evaluator_logic"],
        )