"""D9 retest 队列（``d9_retest_queue.jsonl``）contract 守门。

设计来源：INC35「补 D9 retest 子集」。队列是**计划**不是**已评分**
——它不进 annotations.jsonl，只在 14 天后真做 retest 时拿来对照。

守住的几条不变量（任一被破就让门禁当场红）：

    1. 队列存在且每行是合法 JSON；
    2. 数量在 ``RETEST_FRACTION = (0.20, 0.30)`` 区间内：
       manual-review calibration 50 条的 20–30% = 10–15；
    3. 所有 ``golden_id`` 都在 v3 calibration 里、且 ``reviewed_by ==
       "manual-review"``——AI 占大头的隐患由此被锁在外面；
    4. ``round_2_status == "pending"`` 且 ``round_2_label is None``——
       队列不能预填分数，那是 D9 不允许的伪造；
    5. ``scheduled_retest_at >= 2026-10-27``（v3 freeze + 14 天间隔）；
    6. **分层覆盖**：抽到的 label 数 ≥ 6（manual-review 共 8 个 label），
       防止有人为「省事」全抽 subscription_wall。

不在本测试范围：

    - ``annotations.jsonl`` round=2 是否存在——那是 ``builder.cmd_consistency``
      关心的事，不重复造轮子；
    - retest 实际打分（14 天后的事）——脚本是一次性产物，留它反成债。
"""
from __future__ import annotations

import json
import pathlib
from collections import Counter
from datetime import datetime, timezone

import pytest

QUEUE = pathlib.Path(__file__).resolve().parents[2] / "evaluation" / "golden" / "data" / "d9_retest_queue.jsonl"
V3_CALIB = pathlib.Path(__file__).resolve().parents[2] / "evaluation" / "golden" / "data" / "v3" / "calibration.jsonl"

MIN_SCHEDULED = datetime(2026, 10, 27, tzinfo=timezone.utc)
RETEST_FRACTION = (0.20, 0.30)


def _load_queue() -> list[dict]:
    assert QUEUE.exists(), f"队列文件缺失：{QUEUE}（INC35 一次性脚本未跑）"
    rows = []
    for line in QUEUE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _v3_manual_review() -> list[dict]:
    rows = [json.loads(l) for l in V3_CALIB.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in rows if (r.get("golden") or {}).get("reviewed_by") == "manual-review"]


class Test队列形状:
    def test_存在且每行合法JSON(self):
        rows = _load_queue()
        assert rows, "队列为空"

    def test_数量落在区间(self):
        """10–15 条（manual-review 50 条的 20–30%）。"""
        rows = _load_queue()
        mr = _v3_manual_review()
        n = len(rows)
        frac = n / len(mr)
        assert RETEST_FRACTION[0] <= frac <= RETEST_FRACTION[1], (
            f"队列 {n} 条 / manual-review {len(mr)} 条 = {frac:.1%}，"
            f"不在 RETEST_FRACTION {RETEST_FRACTION}"
        )


class Test入口来源:
    def test_所有golden_id都在v3_calibration(self):
        rows = _load_queue()
        mr_ids = {r["golden_id"] for r in _v3_manual_review()}
        unknown = [r["golden_id"] for r in rows if r["golden_id"] not in mr_ids]
        assert not unknown, f"队列里的 golden_id 不在 v3 manual-review：{unknown}"

    def test_入口reviewed_by全部manual_review(self):
        """AI 辅助占大头（189/294）的隐患不污染 retest。"""
        rows = _load_queue()
        bad = [r for r in rows if r.get("round_1_reviewed_by") != "manual-review"]
        assert not bad, f"队列里有非 manual-review 入口：{[b['golden_id'] for b in bad]}"

    def test_round_1_label与v3一致(self):
        rows = _load_queue()
        mr_by_id = {r["golden_id"]: r for r in _v3_manual_review()}
        drifted = [
            (r["golden_id"], r["round_1_label"], mr_by_id[r["golden_id"]]["golden"]["label"])
            for r in rows
            if mr_by_id[r["golden_id"]]["golden"]["label"] != r["round_1_label"]
        ]
        assert not drifted, f"round_1_label 与 v3 calibration 漂移：{drifted}"


class Testretest状态:
    @pytest.mark.parametrize("row", _load_queue(), ids=lambda r: r["golden_id"])
    def test_round_2_status_pending(self, row):
        assert row["round_2_status"] == "pending"

    @pytest.mark.parametrize("row", _load_queue(), ids=lambda r: r["golden_id"])
    def test_round_2_label为空(self, row):
        """D9 不允许在 retest 前预填分数——fail-closed。"""
        assert row["round_2_label"] is None

    @pytest.mark.parametrize("row", _load_queue(), ids=lambda r: r["golden_id"])
    def test_scheduled_retest_at_不早于2026_10_27(self, row):
        scheduled = datetime.fromisoformat(row["scheduled_retest_at"])
        assert scheduled >= MIN_SCHEDULED, (
            f"{row['golden_id']} scheduled={scheduled} 早于 v3 freeze+14 天"
        )

    @pytest.mark.parametrize("row", _load_queue(), ids=lambda r: r["golden_id"])
    def test_interval_days_为14(self, row):
        assert row["interval_days_planned"] == 14


class Test分层覆盖:
    def test_覆盖label数不低于6(self):
        """manual-review 共 8 个 label；至少抽 6 个（按比例允许漏 2 个零碎），
        否则分层失效——全抽 subscription_wall 的话 D9 kappa 没意义。"""
        rows = _load_queue()
        labels = {r["round_1_label"] for r in rows}
        assert len(labels) >= 6, f"只覆盖 {len(labels)} 个 label：{sorted(labels)}"


class Test与freeze逻辑对得上:
    def test_不含新分支_blocked_broken_partial_free_login_required(self):
        """v3 manual-review 0 条含新分支（INC33 把新分支补到了 ai-assisted-inc33），
        队列必须如实反映这一事实。哪天 manual-review 也补了新分支样本，再调。"""
        rows = _load_queue()
        new_branches = {"blocked", "broken", "partial_free", "login_required"}
        leaked = [r for r in rows if r["round_1_label"] in new_branches]
        assert not leaked, (
            f"队列里出现新分支：{[l['golden_id'] for l in leaked]}——"
            "manual-review 还没补新分支样本；如需补，先扩 v3 的人工子集"
        )
