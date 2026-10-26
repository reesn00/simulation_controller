"""分支口径代（``executor/branch_eras.py``）的守门。

为什么要有这一轴
----------------
分支名背后是判定规则，规则一改，同一批 URL 落的分支就换。前后两代的
分布**不可直接比**——不记这一轴，跳变会被读成「质量变好了」
（CLAUDE.md 点名的坑）。

2026-10-26 owner 裁定：**不回溯**（历史存档的分支标签按当时规则原样
留着），但**把断点落成可筛的批次边界**。本文件盯住三件事：

    1. ``test_代与代码现状对账`` —— 登记项跟**代码**对得上（e3 说的
       「同页守卫收窄到 1 支」必须与 ``SAME_PAGE_GUARD_BRANCHES`` 一致）；
    2. ``test_守卫降级行不在负样本池`` —— 「不回溯」这个裁定在代码上
       站得住：降级行落 ``unresolved``，而它是 NON_SAMPLE 分支；
    3. ``test_降级行能从证据里逐条捞出来`` —— 断点**可筛**，不只可记。

另外两条是单元级行为（``era_for_date`` / ``split_archives_by_era``），
不依赖盘上有数据。
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from trajectory_pipeline.executor import branch_eras
from trajectory_pipeline.executor import branches


# ── 1. 登记项 ↔ 代码现状 ───────────────────────────────────────


def test_代与代码现状对账():
    """e3 登记的是「同页守卫收窄到 1 支」——代码必须真的是那样。

    这条是登记表的**防漂**闸：改了守卫集合却没改登记项（或者反过来），
    这里当场红。登记项是「历史事实的抄本」，它抄的那条事实由**代码**
    现在长什么样来兜底。
    """
    e3 = next(e for e in branch_eras.BRANCH_ERAS if e.era_id == "e3")
    assert e3.since == "2026-10-19"
    assert set(branches.SAME_PAGE_GUARD_BRANCHES) == {"component_unverified"}
    # 收窄掉的那 4 支，登记项必须点名（不然「收窄了谁」就丢了）
    assert {
        "subscription_wall", "login_required", "blocked", "partial_free",
    } <= set(e3.changed_branches)


def test_代按时间严格递增不重叠():
    dates = [e.since for e in branch_eras.BRANCH_ERAS]
    assert dates == sorted(dates)
    assert len(set(dates)) == len(dates), "两个代同一天生效 ⇒ 边界重叠"
    ids = [e.era_id for e in branch_eras.BRANCH_ERAS]
    assert len(set(ids)) == len(ids)


def test_每一代都有锚点():
    """每个登记项都要指得到记录（编年史 / 台账 / 方案）——「凭记忆」不算数。"""
    for era in branch_eras.BRANCH_ERAS:
        assert era.anchor.strip(), f"{era.era_id} 没有记录锚点"
        assert era.summary.strip(), f"{era.era_id} 没有一句话说明"


# ── 2. 「不回溯」这个裁定在代码上站得住 ─────────────────────────


def test_守卫降级行不在负样本池():
    """守卫降级 ⇒ 落 ``unresolved`` ⇒ ``unresolved`` 是 NON_SAMPLE 分支
    ⇒ 这些行**从来不进负样本池**。

    这是「不回溯」裁定的**代码根据**：池子里压根没有这批行，
    改标签没有对象；分布跳变只发生在**存档**与**黄金集候选**一侧。
    """
    assert "unresolved" in branches.NON_SAMPLE_BRANCHES
    # 判定层把「不贴这条负标签」写成 unresolved —— 落点必须是它
    assert branches.BRANCH_LABELS["unresolved"], "unresolved 必须有可读标签"


# ── 3. 断点可筛：降级行能逐条捞出来 ────────────────────────────


def _archive(outcomes: list[dict]) -> dict:
    return {"task_id": "E001", "outcomes": outcomes}


def test_降级行能从证据里逐条捞出来():
    blob = _archive(
        [
            {
                "branch": "unresolved",
                "question": "player.ok",
                "url": "https://a.test/",
                "evidence": (
                    "LLM 判 False：……\n"
                    "[记账降级] 本该记 component_unverified，但点击播放控件前后是"
                    "同一个页面——该分支的前置事实不成立，不贴这条负标签"
                ),
            },
            {
                "branch": "not_play_site",
                "question": "player.ok",
                "url": "https://b.test/",
                "evidence": "页面不是播放站点",
            },
        ]
    )
    rows = branch_eras.guard_downgrades(blob)
    assert len(rows) == 1
    assert rows[0].intended == "component_unverified"
    assert rows[0].branch == "unresolved"
    assert rows[0].url == "https://a.test/"


def test_降级行认不出意图分支时给空串不猜():
    """evidence 有标记但认不出「本该记 X」⇒ ``intended`` 给空串。

    **不猜**：猜出来的分支会让「受影响集合」凭空多出不该有的行。
    """
    blob = _archive(
        [
            {
                "branch": "unresolved",
                "question": "player.ok",
                "url": "https://a.test/",
                "evidence": "[记账降级] 前置事实不成立，不贴这条负标签",
            }
        ]
    )
    rows = branch_eras.guard_downgrades(blob)
    assert len(rows) == 1
    assert rows[0].intended == ""


# ── 4. 代判读（单元级，不依赖盘上数据）─────────────────────────


def test_日期落对应代():
    assert branch_eras.era_for_date("2026-10-11") == "e0"
    assert branch_eras.era_for_date("2026-10-12") == "e1"
    assert branch_eras.era_for_date("2026-10-15") == "e1"
    assert branch_eras.era_for_date("2026-10-16") == "e2"
    assert branch_eras.era_for_date("2026-10-19") == "e3"
    assert branch_eras.era_for_date("2026-10-21") == "e4"
    assert branch_eras.era_for_date("2026-10-23") == "e5"
    assert branch_eras.era_for_date("2026-10-26") == "e5"


def test_按mtime切分存档(tmp_path):
    """切分按**文件 mtime**——P1 存档不带时间戳，这是唯一的日期来源。

    ⚠️ mtime 是代理不是事实（见 ``M_TIME_CAVEAT``）。要权威答案就重采。
    """
    import datetime

    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text("{}", encoding="utf-8")
    b.write_text("{}", encoding="utf-8")
    old = datetime.datetime(2026, 10, 13).timestamp()
    new = datetime.datetime(2026, 10, 25).timestamp()
    os.utime(a, (old, old))
    os.utime(b, (new, new))
    got = branch_eras.split_archives_by_era([a, b])
    assert got["e1"] == [a]
    assert got["e5"] == [b]


# ── 5. 盘上实测（有数据才跑）───────────────────────────────────


def _output_dir() -> pathlib.Path:
    return (
        pathlib.Path(__file__).resolve().parents[2]
        / "output" / "pipeline"
    )


def test_盘上存档的守卫降级行落unresolved():
    """实测断言（2026-10-26）：盘上 156 份存档里 20 行守卫降级，
    **全部**落 ``unresolved``。

    这条是「不回溯」裁定的**经验根据**：降级行不进池（``unresolved``
    是 NON_SAMPLE），而现行代码下它们**仍然**落 ``unresolved``
    （``component_unverified`` 走同页守卫、``subscription_wall`` 走
    门禁语境前置）⇒ 盘上这批行的标签**不需要改**。
    """
    out = _output_dir()
    if not out.is_dir():
        pytest.skip("output/pipeline 不在盘上")
    archives = [
        p for p in out.glob("*.json")
        if not p.name.endswith(".reviewed.json")
    ]
    if not archives:
        pytest.skip("output/pipeline 里没有存档")
    total = 0
    intended_counts: dict[str, int] = {}
    for p in archives:
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(blob, dict):
            continue
        for row in branch_eras.guard_downgrades(blob):
            total += 1
            intended_counts[row.intended] = (
                intended_counts.get(row.intended, 0) + 1
            )
            assert row.branch == "unresolved", (
                f"{p.name}：守卫降级行落的是 {row.branch!r}，"
                "不是 unresolved ⇒ 池子口径要重算"
            )
    assert total > 0, "盘上一条守卫降级行都没有——断言失效了"
    # 认不出的意图分支不许有
    assert "" not in intended_counts, (
        f"有 {intended_counts.get('', 0)} 行认不出意图分支"
    )
