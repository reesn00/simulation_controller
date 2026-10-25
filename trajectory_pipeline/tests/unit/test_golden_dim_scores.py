"""软维度金标的**写入规则**（方案 16 · d2 / d4 / d5 / d6）的守门测试。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §9.5 / C-11。

守的是七件事（每一条都对应一个踩过的坑，不是风格偏好）：

1. **只认白名单维度**：拼错的 dim 混进 `dimension_scores` 等于把那一维的价值
   丢掉，而报告上看不出来——所以 `dimension_scores: {d7: 3}` 直接炸；
2. **`True` / `4.5` / `"4"` 一律拒**：`True` 是 `int` 的子类，放过它会静默变成
   1 分；
3. **`null` 是结论、空串是没填、整栏省掉是错误**——三者必须分得开：
   把「没填」读成「判不了」，半张表会被静默写成一堆 `null`；
4. **只能补已有标注**：分数不能凭空造出一条样本，缺失 id 要**全部**列出来；
5. **必须有人签**（`reviewed_by`）：没人签的分数与机器猜的分不开；
6. **默认不覆盖已填的分**，`--force` 才覆盖且计入 `overwritten`；同键同值是
   **幂等跳过**（重跑同一张表不能算「改动」）；
7. **只动分数那几栏**：`label` / `label_label` / `note`（标签的）一个字节不变。
"""

import pytest

from trajectory_pipeline.evaluation.golden import dim_scores as ds


# ── 1–3. normalize_scores ──────────────────────────────────────


def test_合法分数与判不了都收():
    assert ds.normalize_scores({"d5": 4}) == {"d5": 4}
    assert ds.normalize_scores({"d5": None}) == {"d5": None}
    assert ds.normalize_scores({"d2": 1, "d4": 5, "d6": None}) == {
        "d2": 1,
        "d4": 5,
        "d6": None,
    }


@pytest.mark.parametrize("bad", [True, False, 4.5, "4", [4], {"v": 4}])
def test_非整数的分数一律拒(bad):
    with pytest.raises(ds.DimScoreError):
        ds.normalize_scores({"d5": bad})


@pytest.mark.parametrize("bad", [0, 6, -1, 99])
def test_越界的分数拒(bad):
    with pytest.raises(ds.DimScoreError):
        ds.normalize_scores({"d5": bad})


def test_白名单外的维度拒():
    # 白名单是 d2..d9；`d11` 拼错/不存在，必须炸（静默放过 = 那一维白填）
    with pytest.raises(ds.DimScoreError, match="白名单"):
        ds.normalize_scores({"d11": 3})


@pytest.mark.parametrize("bad", [None, {}, []])
def test_空的分数不是判不了(bad):
    with pytest.raises(ds.DimScoreError):
        ds.normalize_scores(bad)


# ── 3. 格子语义 ───────────────────────────────────────────────


def test_空串跳过_na是判不了_整栏省掉是错误():
    rows = [
        {"golden_id": "g1", "dimension": "d5", "gold_score": ""},
        {"golden_id": "g1", "dimension": "d4", "gold_score": "na"},
        {"golden_id": "g1", "dimension": "d2", "gold_score": "3"},
    ]
    fills = ds.group_fills(rows)
    assert len(fills) == 1
    # 空串那一格**不进**补丁；na 那一格进，且是 None
    assert fills[0]["dimension_scores"] == {"d4": None, "d2": 3}


def test_整栏省掉gold_score会被读成判不了_所以拒():
    with pytest.raises(ds.DimScoreError, match="没有 gold_score 栏"):
        ds.group_fills([{"golden_id": "g1", "dimension": "d5"}])


def test_一条都没填就不产出空补丁():
    rows = [{"golden_id": "g1", "dimension": "d5", "gold_score": ""}]
    assert ds.group_fills(rows) == []


def test_判不了写法多种_都是None():
    rows = [
        {"golden_id": "g1", "dimension": d, "gold_score": raw}
        for d, raw in (("d2", "n/a"), ("d4", "-"), ("d5", "判不了"), ("d6", None))
    ]
    fills = ds.group_fills(rows)
    assert fills[0]["dimension_scores"] == {"d2": None, "d4": None, "d5": None, "d6": None}


# ── group_fills 的其余规矩 ─────────────────────────────────────


def test_同一格出现两次就拒():
    rows = [
        {"golden_id": "g1", "dimension": "d5", "gold_score": 3},
        {"golden_id": "g1", "dimension": "d5", "gold_score": 4},
    ]
    with pytest.raises(ds.DimScoreError, match="两次"):
        ds.group_fills(rows)


def test_补丁形态与工作表形态都收():
    rows = [
        {"golden_id": "g1", "dimension": "d5", "gold_score": 4},
        {"golden_id": "g2", "dimension_scores": {"d4": 2, "d6": None}},
    ]
    fills = ds.group_fills(rows)
    assert {f["golden_id"]: f["dimension_scores"] for f in fills} == {
        "g1": {"d5": 4},
        "g2": {"d4": 2, "d6": None},
    }


def test_缺golden_id就拒():
    with pytest.raises(ds.DimScoreError, match="golden_id"):
        ds.group_fills([{"dimension": "d5", "gold_score": 4}])


def test_归并不打乱首次出现顺序():
    rows = [
        {"golden_id": "b", "dimension": "d5", "gold_score": 3},
        {"golden_id": "a", "dimension": "d5", "gold_score": 3},
        {"golden_id": "b", "dimension": "d4", "gold_score": 2},
    ]
    assert [f["golden_id"] for f in ds.group_fills(rows)] == ["b", "a"]


def test_署名人以具体格子为准():
    rows = [
        {"golden_id": "g1", "dimension": "d5", "gold_score": 3, "reviewed_by": "甲"},
        {"golden_id": "g1", "dimension": "d4", "gold_score": 2, "reviewed_by": "乙"},
    ]
    fills = ds.group_fills(rows)
    assert fills[0]["reviewed_by"] == "乙"


# ── merge_dimension_scores ────────────────────────────────────


def _ann(**over) -> dict:
    row = {
        "golden_id": "g1",
        "round": 1,
        "label": "success",
        "label_label": "成功",
        "reviewed_by": "标标签的人",
        "reviewed_at": "2026-10-01",
        "note": "标签理由",
        "dimension_scores": None,
    }
    row.update(over)
    return row


def test_只补已有标注_缺失的一次全列出来():
    with pytest.raises(ds.DimScoreError) as exc:
        ds.merge_dimension_scores(
            [_ann()],
            [
                {"golden_id": "g2", "dimension_scores": {"d5": 3}, "reviewed_by": "甲"},
                {"golden_id": "g3", "dimension_scores": {"d5": 3}, "reviewed_by": "甲"},
            ],
        )
    assert "g2" in str(exc.value) and "g3" in str(exc.value)


def test_没人签就不写():
    with pytest.raises(ds.DimScoreError, match="reviewed_by"):
        ds.merge_dimension_scores(
            [_ann()], [{"golden_id": "g1", "dimension_scores": {"d5": 3}}]
        )


def test_只动分数那几栏_标签一字不碰():
    merged, stats = ds.merge_dimension_scores(
        [_ann()],
        [{"golden_id": "g1", "dimension_scores": {"d5": 4}, "reviewed_by": "甲"}],
    )
    row = merged[0]
    assert row["dimension_scores"] == {"d5": 4}
    assert row["dimension_reviewed_by"] == "甲"
    # 标签侧原样
    assert (row["label"], row["label_label"], row["note"], row["reviewed_by"]) == (
        "success",
        "成功",
        "标签理由",
        "标标签的人",
    )
    assert stats["changed"] == 1 and stats["per_dim"] == {"d5": 1}


def test_同键同值幂等跳过_不算改动():
    merged, stats = ds.merge_dimension_scores(
        [_ann(dimension_scores={"d5": 4})],
        [{"golden_id": "g1", "dimension_scores": {"d5": 4}, "reviewed_by": "甲"}],
    )
    assert stats["changed"] == 0 and stats["unchanged"] == 1
    assert merged[0]["dimension_scores"] == {"d5": 4}


def test_改已有分数要force_否则拒():
    ann = [_ann(dimension_scores={"d5": 4})]
    fill = [{"golden_id": "g1", "dimension_scores": {"d5": 2}, "reviewed_by": "甲"}]
    with pytest.raises(ds.DimScoreError, match="--force"):
        ds.merge_dimension_scores(ann, fill)
    merged, stats = ds.merge_dimension_scores(ann, fill, allow_overwrite=True)
    assert merged[0]["dimension_scores"] == {"d5": 2}
    assert stats["overwritten"] == 1


def test_旧分是null改它同样要force():
    """`null`（人判判不了）被后来的真分数覆盖**不需要** --force 吗？"""
    # 口径：null 也算「已判过」，改它同样要 --force。这里把口径钉死。
    ann = [_ann(dimension_scores={"d5": None})]
    fill = [{"golden_id": "g1", "dimension_scores": {"d5": 4}, "reviewed_by": "甲"}]
    merged, stats = ds.merge_dimension_scores(ann, fill, allow_overwrite=True)
    assert merged[0]["dimension_scores"]["d5"] == 4
    assert stats["overwritten"] == 1


def test_判不了计入judged_na_不并进未填():
    merged, stats = ds.merge_dimension_scores(
        [_ann()],
        [{"golden_id": "g1", "dimension_scores": {"d5": None}, "reviewed_by": "甲"}],
    )
    assert stats["judged_na"] == 1
    assert merged[0]["dimension_scores"] == {"d5": None}


def test_多轮标注只认round1():
    rows = [
        _ann(golden_id="g1", round=1),
        _ann(golden_id="g1", round=2, dimension_scores={"d5": 1}),
    ]
    merged, _ = ds.merge_dimension_scores(
        rows, [{"golden_id": "g1", "dimension_scores": {"d5": 5}, "reviewed_by": "甲"}]
    )
    assert merged[0]["dimension_scores"] == {"d5": 5}
    assert merged[1]["dimension_scores"] == {"d5": 1}  # round=2 原样


def test_输入行不被就地改动():
    ann = [_ann(dimension_scores={"d4": 2})]
    ds.merge_dimension_scores(
        ann, [{"golden_id": "g1", "dimension_scores": {"d5": 3}, "reviewed_by": "甲"}]
    )
    assert ann[0]["dimension_scores"] == {"d4": 2}


# ── coverage ─────────────────────────────────────────────────


def test_覆盖率把判不了单列():
    rows = [
        {"dimension_scores": {"d5": 4}},
        {"dimension_scores": {"d5": None}},
        {"dimension_scores": {}},
        {"dimension_scores": None},
    ]
    cov = ds.coverage(rows, ["d5"])
    assert cov == {"d5": {"filled": 1, "judged_na": 1, "empty": 2}}