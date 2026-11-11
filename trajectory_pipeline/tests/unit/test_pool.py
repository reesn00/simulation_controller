"""分池（方案 23 R3-3）：交付集只含 ``camofox-1``，旧面进 ``legacy/``。

盯四件：

1. **落点**：交付面写交付目录，旧面写 ``legacy/<surface>/<kind>/``；
2. **移动而非删除**：已存在的旧面文件被挪走，内容还在（退役 ≠ 失忆）；
3. **成组移动**：P3 三份视图是一个整体，只搬一份等于把一组拆散；
4. **批次账**：``pool_meta.json`` 记下每个面还剩多少（池变小不是质量变好）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline import toolcatalog
from trajectory_pipeline.assembler import builder, pool, task_view, views


# ── 夹具 ──────────────────────────────────────────────────────────


def _obs(url="u", **kw):
    base = {"url": url, "page_title": "T", "body_text": "正文",
            "truncated": False, "degraded": [], "video_tag_count": 0,
            "iframe_count": 0, "interactive_elements": [], "links": []}
    base.update(kw)
    return base


def _archive(task_id="T001", run_id="a1", driver="camofox", **over):
    base = {
        "task_id": task_id,
        "run_id": run_id,
        "title": "功夫",
        "driver": driver,
        "query": "功夫 在线观看",
        "user_prompt": "有没有能看正片的",
        "search_url": "https://s.test",
        "search_observation": _obs("https://s.test"),
        "search_blocked": "",
        "steps": [{"action": {"tool": "goto",
                              "params": {"url": "https://s.test"},
                              "origin": "model", "target": None},
                   "observation": _obs("https://s.test"), "error": "",
                   "rationale": None}],
        "run_config": {"engine": "bing", "max_candidates": 20},
        "provenance": {}, "candidates": [], "visits": [], "outcomes": [],
        "ledger": None, "warnings": [],
    }
    base.update(over)
    return base


def _write_p1(root: Path, *arcs):
    root.mkdir(parents=True, exist_ok=True)
    for arc in arcs:
        (root / f"{arc['task_id']}__{arc['run_id']}.json").write_text(
            json.dumps(arc, ensure_ascii=False), encoding="utf-8"
        )
    return root


def _write_legacy_p1(root: Path) -> Path:
    """一份交付面 P1 + 一份**旧面** P1（``driver`` 缺省 ⇒ obscura-1）。"""
    _write_p1(root, _archive("T001", "a1", driver="camofox"),
              _archive("T002", "b2", driver=None))
    # driver=None：模拟老档（键在但为空 → 默认面 obscura-1）
    path = root / "T002__b2.json"
    arc = json.loads(path.read_text(encoding="utf-8"))
    arc.pop("driver")
    path.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
    return root


def _write_two_surfaces(root: Path) -> Path:
    """``samples/`` 里放一份交付面样本 + 一份旧面样本。"""
    samples = root / "out" / "samples"
    samples.mkdir(parents=True)
    (samples / "a__1__site-1.json").write_text(
        json.dumps(_sample("a__1__site-1", "camofox-1")), encoding="utf-8"
    )
    (samples / "b__2__site-1.json").write_text(
        json.dumps(_sample("b__2__site-1", "obscura-1")), encoding="utf-8"
    )
    return root


def _sample(sample_id, surface, **over):
    """够 builder 用的最小 P2 JSON（直接摆进 samples/ 模拟存量）。"""
    base = {
        "sample_id": sample_id,
        "task_id": sample_id.split("__")[0],
        "unit": "site-1",
        "title": "功夫",
        "source_archive": "T001__a1.json",
        "tool_surface": surface,
        "six": {
            "system_prompt": "系统提示词",
            "tools": [{"name": "goto", "params": ["url"], "instruction": "打开"}],
            "user_prompt": "有没有能看正片的",
            "provenance": {},
            "actions": [{"tool": "goto", "params": {"url": "https://x.test"},
                         "target": None, "origin": "model"}],
            "observations": [_obs()],
            "rationale": None,
        },
        "gate": {"status": "not_run", "reasons": []},
        "rationale_missing": True,
        "outcome": None,
        "degraded_from": [],
    }
    base.update(over)
    return base


# ── 1. 路径规则 ───────────────────────────────────────────────────


class Test路径规则:
    def test_交付面留在原根(self, tmp_path):
        assert pool.pool_root(tmp_path, "camofox-1") == tmp_path

    @pytest.mark.parametrize("surface", ["obscura-1", "camoufox-1"])
    def test_旧面进legacy(self, tmp_path, surface):
        assert pool.pool_root(tmp_path, surface) == tmp_path / "legacy" / surface

    def test_认不出的面直接抛(self, tmp_path):
        """不猜、不静默落进交付池——错池不会报错，只会让两代格式混排。"""
        with pytest.raises(ValueError):
            pool.pool_root(tmp_path, "some-new-surface")
        with pytest.raises(ValueError):
            pool.pool_root(tmp_path, "")

    def test_交付面就是camofox(self):
        assert pool.DELIVERY_SURFACE == toolcatalog.SURFACE_CAMOFOX


# ── 1.b 渲染口径（owner 裁定 2026-11-10）─────────────────────────


class Test渲染口径:
    """旧面默认不重渲染——**口径本身的单元测试**。

    「退役 ≠ 失忆」指的是「存量证据仍读得出来」（P1 源档在盘上、池里的旧
    文件仍在、``pool_meta.json`` 记着它曾有多少），**不是**「每次重建都把
    旧面再渲染一遍」。后者会让清过的 legacy 池自己长回来，而读的人只看到
    「又多了 N 份」。
    """

    def test_交付面总是渲染(self):
        assert pool.should_render(pool.DELIVERY_SURFACE) is True

    @pytest.mark.parametrize("surface", ["obscura-1", "camoufox-1"])
    def test_旧面默认不渲染(self, surface):
        assert pool.should_render(surface) is False

    @pytest.mark.parametrize("surface", ["obscura-1", "camoufox-1"])
    def test_旧面按需重建(self, surface):
        assert pool.should_render(surface, include_legacy=True) is True

    def test_认不出的面直接抛(self):
        """静默当成「旧面」跳掉 = 把「配错了」伪装成「按口径没渲染」——
        两者的处置完全不同，而报表上只表现为「少了几条」。"""
        with pytest.raises(ValueError):
            pool.should_render("some-new-surface")
        with pytest.raises(ValueError):
            pool.should_render("")


# ── 2. P2 落盘分池 ────────────────────────────────────────────────


class TestP2分池:
    def test_旧面P1默认不重渲染(self, tmp_path):
        """旧面是只读存量（owner 裁定 **2026-11-10**）：**默认不渲染**。

        清过的 legacy 池不该自己长回来——否则「又多了 66 份」没人说得清是
        哪儿来的。而「没渲染」必须**看得见**：池里 legacy 为空若不配一条
        计数，就会被读成「旧面不存在」。
        """
        _write_legacy_p1(tmp_path / "src")

        rep = builder.build_all(tmp_path / "src", target_root=tmp_path / "out")

        delivery = tmp_path / "out" / "samples"
        legacy = tmp_path / "out" / "legacy" / "obscura-1" / "samples"
        # per-Run：一个任务一条轨迹 ⇒ 文件名不带单元后缀
        assert {p.name for p in delivery.glob("*.json")} == {"T001__a1.json"}
        assert not legacy.exists(), "旧面默认不重渲染——legacy 池不该自己长回来"
        assert rep.total_legacy_skipped == 1
        # 池账里也要留下这一条：legacy 空 ≠ 旧面不存在
        meta = json.loads(
            (tmp_path / "out" / pool.POOL_META_FILENAME).read_text(encoding="utf-8")
        )
        assert meta["samples"]["legacy_skipped_now"] == 1

    def test_按需重建旧面(self, tmp_path):
        """``include_legacy=True`` 才渲染旧面——**按需**、显式。"""
        _write_legacy_p1(tmp_path / "src")

        rep = builder.build_all(tmp_path / "src", target_root=tmp_path / "out",
                                include_legacy=True)

        legacy = tmp_path / "out" / "legacy" / "obscura-1" / "samples"
        assert {p.name for p in legacy.glob("*.json")} == {"T002__b2.json"}
        assert rep.total_legacy_skipped == 0

    def test_已存在的旧面样本被挪走(self, tmp_path):
        """存量目录里混着的旧面样本，重建时**移动**进 legacy，不是删除。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        samples = builder.samples_dir(tmp_path / "out")
        samples.mkdir(parents=True)
        legacy_file = samples / "T009__z9__site-1.json"
        legacy_file.write_text(
            json.dumps(_sample("T009__z9__site-1", "obscura-1"), ensure_ascii=False),
            encoding="utf-8",
        )

        builder.build_all(tmp_path / "src", target_root=tmp_path / "out")

        assert not legacy_file.exists()
        moved = tmp_path / "out" / "legacy" / "obscura-1" / "samples" / legacy_file.name
        assert moved.exists()
        assert json.loads(moved.read_text(encoding="utf-8"))["tool_surface"] == "obscura-1"

    def test_pool_meta记下池账(self, tmp_path):
        """池变小不是质量变好——面 + 计数 + 本次移动量都要落盘。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        samples = builder.samples_dir(tmp_path / "out")
        samples.mkdir(parents=True)
        (samples / "T009__z9__site-1.json").write_text(
            json.dumps(_sample("T009__z9__site-1", "obscura-1")), encoding="utf-8"
        )

        builder.build_all(tmp_path / "src", target_root=tmp_path / "out")

        meta = json.loads(
            (tmp_path / "out" / pool.POOL_META_FILENAME).read_text(encoding="utf-8")
        )
        assert meta["delivery_surface"] == "camofox-1"
        assert meta["samples"]["delivery"] == 1
        assert meta["samples"]["legacy"] == {"obscura-1": 1}
        assert meta["samples"]["moved_now"] == 1

    def test_认不出的面留在原地并记账(self, tmp_path):
        """缺 ``tool_surface`` 的坏样本不许被猜进任何池。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        samples = builder.samples_dir(tmp_path / "out")
        samples.mkdir(parents=True)
        unknown = samples / "T009__z9__site-1.json"
        unknown.write_text(json.dumps(_sample("T009__z9__site-1", "")),
                           encoding="utf-8")

        builder.build_all(tmp_path / "src", target_root=tmp_path / "out")

        assert unknown.exists()
        meta = json.loads(
            (tmp_path / "out" / pool.POOL_META_FILENAME).read_text(encoding="utf-8")
        )
        assert unknown.name in meta["samples"]["unclassified"]


# ── 3. P3 落盘分池 ────────────────────────────────────────────────


class TestP3分池:
    def test_旧面样本的视图默认不渲染(self, tmp_path):
        """旧面默认不重渲染（owner 裁定 2026-11-10）。"""
        _write_two_surfaces(tmp_path)

        rep = views.build_views_from_dir(root=tmp_path / "out")

        delivery = tmp_path / "out" / "views"
        legacy = tmp_path / "out" / "legacy" / "obscura-1" / "views"
        assert {p.name for p in delivery.glob("*_meta.json")} == {
            "a__1__site-1_meta.json"
        }
        assert not legacy.exists(), "旧面默认不重渲染"
        assert rep.total_legacy_skipped == 1

    def test_按需重建旧面视图(self, tmp_path):
        """``include_legacy=True`` 才渲染旧面视图——按需、显式。"""
        _write_two_surfaces(tmp_path)

        views.build_views_from_dir(root=tmp_path / "out", include_legacy=True)

        legacy = tmp_path / "out" / "legacy" / "obscura-1" / "views"
        assert {p.name for p in legacy.glob("*_meta.json")} == {
            "b__2__site-1_meta.json"
        }
        # 三份视图成组：一份都不能少
        for name in ("messages", "openai", "meta"):
            assert (legacy / f"b__2__site-1_{name}.json").exists()

    def test_存量旧面视图成组挪走(self, tmp_path):
        """交付目录里已经躺着的一组旧面视图 → 三份一起 move。"""
        target = tmp_path / "out" / "views"
        target.mkdir(parents=True)
        for name in ("messages", "openai", "meta"):
            (target / f"c__3__site-1_{name}.json").write_text(
                json.dumps({"tool_surface": "camoufox-1"} if name == "meta" else {}),
                encoding="utf-8",
            )
        samples = tmp_path / "out" / "samples"
        samples.mkdir(parents=True)
        (samples / "a__1__site-1.json").write_text(
            json.dumps(_sample("a__1__site-1", "camofox-1")), encoding="utf-8"
        )

        views.build_views_from_dir(root=tmp_path / "out")

        legacy = tmp_path / "out" / "legacy" / "camoufox-1" / "views"
        for name in ("messages", "openai", "meta"):
            assert (legacy / f"c__3__site-1_{name}.json").exists()
            assert not (target / f"c__3__site-1_{name}.json").exists()

    def test_缺tool_surface不落盘(self, tmp_path):
        """面认不出就 skipped——猜一个面等于让三份视图去错池。"""
        sample = _sample("a__1__site-1", "")
        rep = views.build_views(sample, root=tmp_path / "out")
        assert rep.written == 0
        assert "no-tool_surface" in rep.skipped[0]


# ── 4. T 落盘分池 ─────────────────────────────────────────────────


class TestT分池:
    def test_旧面P1的任务视图默认不渲染(self, tmp_path):
        """旧面默认不重渲染（owner 裁定 2026-11-10）。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1", driver="camofox"),
                  _archive("T002", "b2", driver="camoufox"))

        rep = task_view.build_all_task_views(tmp_path / "src",
                                              target_root=tmp_path / "out")

        delivery = tmp_path / "out" / "task_views"
        legacy = tmp_path / "out" / "legacy" / "camoufox-1" / "task_views"
        assert {p.name for p in delivery.glob("*.json")} == {"T001__a1.json"}
        assert not legacy.exists(), "旧面默认不重渲染"
        assert rep.total_legacy_skipped == 1

    def test_按需重建旧面任务视图(self, tmp_path):
        """``include_legacy=True`` 才渲染旧面任务视图。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1", driver="camofox"),
                  _archive("T002", "b2", driver="camoufox"))

        task_view.build_all_task_views(tmp_path / "src",
                                       target_root=tmp_path / "out",
                                       include_legacy=True)

        legacy = tmp_path / "out" / "legacy" / "camoufox-1" / "task_views"
        assert {p.name for p in legacy.glob("*.json")} == {"T002__b2.json"}

    def test_存量旧面任务视图被挪走(self, tmp_path):
        target = tmp_path / "out" / "task_views"
        target.mkdir(parents=True)
        old = target / "T009__z9.json"
        old.write_text(
            json.dumps({"meta": {"tool_surface": "obscura-1"}}), encoding="utf-8"
        )
        _write_p1(tmp_path / "src", _archive("T001", "a1"))

        task_view.build_all_task_views(tmp_path / "src",
                                       target_root=tmp_path / "out")

        assert not old.exists()
        moved = tmp_path / "out" / "legacy" / "obscura-1" / "task_views" / old.name
        assert moved.exists()

    def test_pool_meta记task_views(self, tmp_path):
        _write_p1(tmp_path / "src", _archive("T001", "a1", driver="camofox"),
                  _archive("T002", "b2", driver="camoufox"))
        task_view.build_all_task_views(tmp_path / "src",
                                       target_root=tmp_path / "out")
        meta = json.loads(
            (tmp_path / "out" / pool.POOL_META_FILENAME).read_text(encoding="utf-8")
        )
        assert meta["task_views"]["delivery"] == 1
        # 旧面默认不渲染 ⇒ legacy 空；但**空的原因**要记下来——
        # 否则「legacy 空」会被读成「旧面不存在」。
        assert meta["task_views"]["legacy"] == {}
        assert meta["task_views"]["legacy_skipped_now"] == 1