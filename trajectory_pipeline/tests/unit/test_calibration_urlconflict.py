"""M-30 URL 级金标读数的守门测试。

设计来源：``docs/计划进展/11-施工单-M29-M30.md`` §2（M-30）·
``docs/计划进展/10-TODO台账.md`` §M-30 · Q-11（金标按访问）/ Q-12（切档按行）。

守的是五件事：

1. **只摆事实**：多头标签是「同一 URL 上有两种金标」，模块**不判哪个对**；
2. **口径同源**：金标取自快照条目（``gold_value``），不另读一遍
   ``golden.label``——两处各算一遍必然漂，漂了没人说得清谁对；
3. **无金标不进冲突**：``gold_value`` 为空的行不计入多头统计（但计入档位分布
   ——泄漏是切档的事，与金标无关）；
4. **取不到 URL 时归 ``?``**：**不静默合并**、不丢弃、不猜；
5. **机器不抹掉人的结论**：清单重跑只刷新事实块，人填的两列原样保留。
"""

import json

from trajectory_pipeline.evaluation.calibration import urlconflict
from trajectory_pipeline.evaluation.calibration.urlconflict import (
    EVIDENCE_FIELD,
    URL_UNKNOWN,
    VERDICT_FIELD,
)


# ── 夹具 ──────────────────────────────────────────────────────


def _entry(golden_id: str, gold_value: str | None, split: str = "calibration") -> dict:
    return {
        "golden_id": golden_id,
        "dimension": "d10",
        "split": split,
        "gold_value": gold_value,
        "pred_value": "success",
    }


def _row(golden_id: str, url: str, *, label: str = "success") -> dict:
    return {
        "golden_id": golden_id,
        "source_kind": "archive_outcome",
        "url": url,
        "code": {"branch": "success", "source": "rule"},
        "golden": {
            "label": label,
            "note": "备注",
            "reviewed_by": "auto-validator",
            "reviewed_at": "2026-10-14T01:11:41+00:00",
        },
    }


def _project(entries, rows):
    rows_by_id = {r["golden_id"]: r for r in rows}
    return urlconflict.project_d10_rows(entries, rows_by_id)


# ── 1. 多头 / 单头 / 无金标 ────────────────────────────────────


def test_多头_URL_被挑出来_单头不算():
    entries = [
        _entry("a", "subscription_wall"),
        _entry("b", "component_unverified"),
        _entry("c", "success"),
        _entry("d", "success"),
    ]
    rows = [
        _row("a", "https://x.test/1"),
        _row("b", "https://x.test/1"),
        _row("c", "https://y.test/2"),
        _row("d", "https://y.test/2"),
    ]
    conflicts = urlconflict.url_gold_conflicts(_project(entries, rows), {
        "a": "https://x.test/1",
        "b": "https://x.test/1",
        "c": "https://y.test/2",
        "d": "https://y.test/2",
    })
    summary = urlconflict.summarize_conflicts(conflicts)
    assert summary["urls_with_gold"] == 2
    assert summary["urls_multi_label"] == 1
    assert conflicts["https://x.test/1"]["labels"] == {
        "subscription_wall": 1,
        "component_unverified": 1,
    }
    assert conflicts["https://y.test/2"]["labels"] == {"success": 2}


def test_无金标的行_不进冲突统计():
    entries = [_entry("a", None), _entry("b", None)]
    rows = [_row("a", "https://x.test/1"), _row("b", "https://x.test/1")]
    conflicts = urlconflict.url_gold_conflicts(_project(entries, rows), {
        "a": "https://x.test/1",
        "b": "https://x.test/1",
    })
    # 两个都没有金标 ⇒ 这个 URL 根本不该出现
    assert conflicts == {}
    assert urlconflict.summarize_conflicts(conflicts)["urls_with_gold"] == 0


def test_金标取自快照条目_不重读_golden_label():
    """口径同源：条目说 success、冻结行的 golden.label 写别的，取条目的。"""
    entries = [_entry("a", "success")]
    row = _row("a", "https://x.test/1", label="broken")
    projected = _project(entries, [row])
    assert projected[0]["gold_value"] == "success"


# ── 2. URL 取不到时归 "?"，不静默合并 ──────────────────────────


def test_URL_取不到时归_问号_而不是静默合并():
    entries = [_entry("a", "success"), _entry("b", "broken")]
    rows = [_row("a", ""), _row("b", "")]
    # url_of 里**没有**这两个 id ⇒ 取不到
    conflicts = urlconflict.url_gold_conflicts(_project(entries, rows), {})
    assert list(conflicts) == [URL_UNKNOWN]
    assert conflicts[URL_UNKNOWN]["labels"] == {"success": 1, "broken": 1}
    # 归堆是显式的「取不到」，不是「同一个 URL」
    assert URL_UNKNOWN == "?"


def test_取不到_URL_的行_不与其他_URL_合并():
    entries = [_entry("a", "success"), _entry("b", "broken")]
    rows = [_row("a", "https://x.test/1"), _row("b", "https://y.test/2")]
    conflicts = urlconflict.url_gold_conflicts(_project(entries, rows), {
        "b": "https://y.test/2",
    })
    # a 的 url 取不到 ⇒ 归 "?"；b 有自己的 URL ⇒ 两者**不合并**
    assert set(conflicts) == {URL_UNKNOWN, "https://y.test/2"}


# ── 3. 跨档与泄漏 ─────────────────────────────────────────────


def test_跨档_URL_被识别():
    entries = [
        _entry("a", "success", "calibration"),
        _entry("b", "success", "holdout"),
    ]
    rows = [_row("a", "https://x.test/1"), _row("b", "https://x.test/1")]
    url_of = {"a": "https://x.test/1", "b": "https://x.test/1"}
    projected = _project(entries, rows)
    conflicts = urlconflict.url_gold_conflicts(projected, url_of)
    assert conflicts["https://x.test/1"]["splits"] == ["calibration", "holdout"]
    leak = urlconflict.split_leakage(projected, url_of)
    assert leak["cross_split_urls"] == 1
    assert leak["holdout_rows"] == 1
    assert leak["holdout_rows_on_cross_split_urls"] == 1


def test_持有集泄漏_只数跨档_URL_上的行():
    entries = [
        _entry("a", "success", "calibration"),
        _entry("b", "success", "holdout"),
        _entry("c", "success", "holdout"),
    ]
    rows = [
        _row("a", "https://x.test/1"),
        _row("b", "https://x.test/1"),
        _row("c", "https://y.test/2"),
    ]
    url_of = {
        "a": "https://x.test/1",
        "b": "https://x.test/1",
        "c": "https://y.test/2",
    }
    leak = urlconflict.split_leakage(_project(entries, rows), url_of)
    assert leak["cross_split_urls"] == 1
    assert leak["holdout_rows"] == 2
    assert leak["holdout_rows_on_cross_split_urls"] == 1


def test_无金标的行_也计入档位分布_但不计入冲突():
    entries = [_entry("a", None, "holdout")]
    rows = [_row("a", "https://x.test/1")]
    projected = _project(entries, rows)
    leak = urlconflict.split_leakage(projected, {"a": "https://x.test/1"})
    assert leak["holdout_rows"] == 1
    assert urlconflict.url_gold_conflicts(projected, {"a": "https://x.test/1"}) == {}


# ── 4. 清单：只列多头 + 保留人填结论 ───────────────────────────


def _two_conflicts():
    entries = [
        _entry("a", "subscription_wall"),
        _entry("b", "component_unverified"),
        _entry("c", "success"),
    ]
    rows = [
        _row("a", "https://x.test/1"),
        _row("b", "https://x.test/1"),
        _row("c", "https://y.test/2"),
    ]
    url_of = {
        "a": "https://x.test/1",
        "b": "https://x.test/1",
        "c": "https://y.test/2",
    }
    projected = _project(entries, rows)
    return urlconflict.url_gold_conflicts(projected, url_of), urlconflict.split_leakage(
        projected, url_of
    )


def test_清单_只列多头的_URL():
    conflicts, leak = _two_conflicts()
    text = urlconflict.render_conflicts_markdown(conflicts, leakage=leak)
    assert "## 1 · `https://x.test/1`" in text
    assert "https://y.test/2`" not in text.split("## 1 ·")[1].split("## ")[0] \
        if "## 1 ·" in text else True
    assert text.count("## ") == 1  # 只有一个多头 URL ⇒ 一节


def test_清单_保留人填的结论_机器重跑不抹掉():
    conflicts, leak = _two_conflicts()
    verdicts = {
        "https://x.test/1": {
            VERDICT_FIELD: urlconflict.VERDICT_IMPOSSIBLE,
            EVIDENCE_FIELD: "正文里同时有付费门禁与控件缺失的原文",
        }
    }
    text = urlconflict.render_conflicts_markdown(
        conflicts, leakage=leak, verdicts=verdicts
    )
    assert urlconflict.VERDICT_IMPOSSIBLE in text
    assert "正文里同时有付费门禁与控件缺失的原文" in text

    # 回读 ⇒ 重跑一次，结论还在
    back = urlconflict.parse_verdicts_markdown(text)
    assert back["https://x.test/1"][VERDICT_FIELD] == urlconflict.VERDICT_IMPOSSIBLE
    assert back["https://x.test/1"][EVIDENCE_FIELD].startswith("正文里同时有")
    again = urlconflict.render_conflicts_markdown(
        conflicts, leakage=leak, verdicts=back
    )
    assert urlconflict.VERDICT_IMPOSSIBLE in again


def test_清单_未填的结论列是占位符_不是空白():
    conflicts, leak = _two_conflicts()
    text = urlconflict.render_conflicts_markdown(conflicts, leakage=leak)
    assert urlconflict.VERDICT_TODO in text
    back = urlconflict.parse_verdicts_markdown(text)
    assert back["https://x.test/1"][VERDICT_FIELD] == urlconflict.VERDICT_TODO


def test_回读_认不出形状的节_不按行号硬对齐():
    """行号会随 URL 增删而漂；认不出就跳过，不猜。"""
    text = (
        "## 1 · `https://x.test/1`\n\n"
        f"- **{VERDICT_FIELD}**：{urlconflict.VERDICT_DRIFT}\n"
        f"- **{EVIDENCE_FIELD}**：依据 A\n\n"
        "## 2 · `https://y.test/2`\n\n"
        f"- **{VERDICT_FIELD}**：{urlconflict.VERDICT_UNKNOWN}\n"
    )
    back = urlconflict.parse_verdicts_markdown(text)
    assert back["https://x.test/1"][VERDICT_FIELD] == urlconflict.VERDICT_DRIFT
    assert back["https://y.test/2"][EVIDENCE_FIELD] == ""


# ── 5. 定性取值是封闭集 ───────────────────────────────────────


def test_定性取值_三选一():
    assert urlconflict.VERDICT_CHOICES == (
        "漂移可解释",
        "不可能共存",
        "待查",
    )


def test_表格_里的竖线被转义_不破表():
    entries = [_entry("a", "success"), _entry("b", "broken")]
    rows = [_row("a", "https://x.test/1"), _row("b", "https://x.test/1")]
    rows[0]["golden"] = dict(rows[0]["golden"], note="含 | 竖线 | 的备注")
    projected = _project(entries, rows)
    conflicts = urlconflict.url_gold_conflicts(
        projected, {"a": "https://x.test/1", "b": "https://x.test/1"}
    )
    text = urlconflict.render_conflicts_markdown(conflicts)
    assert "\\|" in text
    data_lines = [
        line for line in text.splitlines() if line.startswith("| `a`")
    ]
    assert len(data_lines) == 1
    # 表头 11 列 ⇒ 数据行 12 个未转义分隔符（首尾各一个）
    assert data_lines[0].count("|") - data_lines[0].count("\\|") == 12


def test_JSON_可序列化_读数不炸():
    conflicts, leak = _two_conflicts()
    payload = json.dumps(
        {
            "summary": urlconflict.summarize_conflicts(conflicts),
            "leakage": leak,
            "conflicts": conflicts,
        },
        ensure_ascii=False,
    )
    assert "https://x.test/1" in payload
