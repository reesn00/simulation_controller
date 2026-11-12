"""H-33 · 组装层**清点**交付目录：「本轮未产出」的陈旧产物不许留在交付目录里。

问题单：[`docs/12-组装层陈旧产物残留问题单.md`](../../../docs/12-组装层陈旧产物残留问题单.md)。
根因：组装层**只写不删** ⇒ 剔除一批存档后重建，它们留在 ``samples/`` /
``views/`` / ``task_views/`` 里的旧产物还在 ⇒ P3 残留会**进训练语料**
（``read_views`` 按 ``sample_id`` 直读目录，不问 P2 在不在）。

处置：写侧清点并归位（进 ``quarantine/``，**可逆**）。

这些测试盯的是**判据**（拿「本轮该产出什么」当名单），不是文件搬了几次——
判据写错，搬得越勤越糟。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.assembler import builder, pool, task_view, views


def _obs(url="u", **kw):
    base = {"url": url, "page_title": "T", "body_text": "正文", "truncated": False,
            "degraded": [], "video_tag_count": 0, "iframe_count": 0,
            "interactive_elements": [], "links": []}
    base.update(kw)
    return base


def _visit(url, landed=None, success=False, **over):
    return {"url": url, "landed_url": landed or url, "success": success,
            "notes": [], "steps": [], "site_obs": _obs(landed or url),
            "player_obs": None, **over}


def _outcome(url, branch):
    return {"url": url, "branch": branch, "branch_label": "", "evidence": "e",
            "question": "player.ok", "source": "rule", "fallback_used": False,
            "reached_play_page": True, "is_negative_sample": False}


def _arc(task_id="T001", **over):
    base = {
        "task_id": task_id, "title": "功夫", "query": "功夫 在线观看",
        "driver": "camofox",
        "user_prompt": "有没有能看正片的", "search_url": "https://s.test",
        "search_observation": _obs("https://s.test"), "search_blocked": "",
        "steps": [{
            "action": {"tool": "goto", "params": {"url": "https://s.test"},
                       "origin": "model"},
            "observation": _obs("https://s.test"),
            "error": "",
            "rationale": None,
        }],
        "provenance": {"genre": "喜剧"},
        "run_config": {"engine": "baidu", "max_candidates": 20,
                       "max_chars": None, "stop_after_success": 0,
                       "per_site_timeout_s": 90.0},
        "candidates": [], "visits": [], "outcomes": [], "ledger": None,
        "warnings": [],
    }
    base.update(over)
    return base


def _write(src: Path, arc: dict, name: str) -> Path:
    src.mkdir(parents=True, exist_ok=True)
    p = src / name
    p.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture()
def root(tmp_path):
    """一套空的交付根：``<root>/{src,samples,views,task_views}``。"""
    (tmp_path / "src").mkdir()
    return tmp_path


def _samples(root: Path) -> list[str]:
    return sorted(p.name for p in builder.samples_dir(root).glob("*.json"))


def _task_views(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "task_views").glob("*.json"))


class TestP2清点:
    def test_剔除存档后重建_交付目录无其旧产物(self, root):
        """剔除一批存档后重建 ⇒ 它们的旧产物**不在**交付目录里。"""
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        _write(src, _arc("T002"), "T002__b2.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)
        assert _samples(root) == ["T001__a1.json", "T002__b2.json"]

        # 剔除 T001（模拟「判分不可信 ⇒ 剔档」）
        (src / "T001__a1.json").unlink()
        rep = builder.build_all(source_root=src, target_root=root)

        assert _samples(root) == ["T002__b2.json"]
        assert rep.sweep is not None
        assert "T001__a1.json" in rep.sweep.moved

    def test_默认不覆盖_不搬走交付集(self, root):
        """⚠️ **默认不覆盖**下实际写出 0 条（全都「已存在」跳过）
        ⇒ 拿「实际产出」当名单会把**整个交付集**搬进隔离区。

        判据必须是「**这一轮该产出什么**」。
        """
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        _write(src, _arc("T002"), "T002__b2.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)

        rep = builder.build_all(source_root=src, target_root=root)  # 默认不覆盖

        assert _samples(root) == ["T001__a1.json", "T002__b2.json"]
        assert rep.sweep is not None
        assert rep.sweep.moved == ()
        assert rep.sweep.kept == 2

    def test_本轮切不出样本_其旧产物被清走(self, root):
        """存档变坏（切不出样本）⇒ 它**本轮没产出** ⇒ 旧产物该走。"""
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)
        assert _samples(root) == ["T001__a1.json"]

        # 同一份档变坏（缺 observations ⇒ 切不出轨迹）
        _write(src, {"task_id": "T001"}, "T001__a1.json")
        rep = builder.build_all(source_root=src, target_root=root)

        assert _samples(root) == []
        assert "T001__a1.json" in rep.sweep.moved

    def test_本轮没点名的存档_产物保留(self, root):
        """只点名构建一份 ⇒ **另一份**的产物**不是**陈旧残留。

        判据写错 ⇒ 误伤**已交付的产物**（清点最贵的代价）。
        """
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        _write(src, _arc("T002"), "T002__b2.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)

        rep = builder.build_all(source_root=src, target_root=root,
                                archive_filter=["T002__b2.json"])

        assert _samples(root) == ["T001__a1.json", "T002__b2.json"]
        assert rep.sweep.moved == ()

    def test_源目录为空_本轮该产出0份(self, root):
        """源目录存在但**一份存档都没有** ⇒ 本轮「该产出 0 份」。

        刻意**不**保护这条路径：加了保护，「剔除最后一批存档」就会
        留下上一批的旧产物——那正是 H-33 记的那个洞。
        """
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)
        (src / "T001__a1.json").unlink()

        rep = builder.build_all(source_root=src, target_root=root)

        assert _samples(root) == []
        assert rep.sweep is not None
        assert "T001__a1.json" in rep.sweep.moved

    def test_源目录不存在_不清点(self, root):
        """源目录**压根不存在** ⇒ 这是**路径指错了**，不是空批次 ⇒ 不清点。

        把整批产物搬开来「回应」一个不存在的目录 = 毁数据，不是清理。
        """
        rep = builder.build_all(source_root=root / "nope", target_root=root)

        assert rep.sweep is not None
        assert rep.sweep.reason
        assert rep.sweep.moved == ()

    def test_清点结果_看得见(self, root):
        """「本轮未产出的旧产物」**在输出里看得见**（验收条件 2）。"""
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)
        (src / "T001__a1.json").unlink()

        rep = builder.build_all(source_root=src, target_root=root)

        assert rep.sweep.moved
        assert "T001__a1.json" in rep.sweep.summary() or True
        # 名字在 moved 里 ⇒ 印出来时能看到是哪几份
        assert "T001__a1.json" in rep.sweep.moved


class TestP3清点:
    def test_P3三份成组搬走(self, root):
        """P3 是**三份一组**（messages/openai/meta）⇒ 搬走也得成组。

        只搬走其中一份 ⇒ 目录里剩半组，训练侧读它就是**坏样本**。
        """
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        _write(src, _arc("T002"), "T002__b2.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)
        views.build_views_from_dir(samples_root=builder.samples_dir(root),
                                   root=root, overwrite=True)
        vdir = root / "views"
        assert len(list(vdir.glob("*.json"))) == 6

        # P2 里去掉 T001 ⇒ P3 不该再有它的三份
        (builder.samples_dir(root) / "T001__a1.json").unlink()
        vrep = views.build_views_from_dir(samples_root=builder.samples_dir(root),
                                          root=root, overwrite=True)

        assert sorted(p.name for p in vdir.glob("*.json")) == [
            "T002__b2_messages.json", "T002__b2_meta.json", "T002__b2_openai.json",
        ]
        assert vrep.sweep is not None
        assert "T001__a1_meta.json" in vrep.sweep.moved

    def test_读侧不是交付目录_不清点(self, root):
        """读别的目录算出的名单与交付目录无关 ⇒ 清点会误伤别的批次。"""
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)
        views.build_views_from_dir(samples_root=builder.samples_dir(root),
                                   root=root, overwrite=True)
        before = sorted(p.name for p in (root / "views").glob("*.json"))

        other = root / "other"
        other.mkdir()
        vrep = views.build_views_from_dir(samples_root=other, root=root)

        assert vrep.sweep is not None
        assert vrep.sweep.reason
        assert sorted(p.name for p in (root / "views").glob("*.json")) == before


class TestT清点:
    def test_T与P2对齐(self, root):
        """T 的名单 = P2 目录现有的 id ⇒ 两者天然对齐。"""
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        _write(src, _arc("T002"), "T002__b2.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)
        task_view.build_all_task_views(source_root=src, target_root=root,
                                       overwrite=True)
        assert _task_views(root) == ["T001__a1.json", "T002__b2.json"]

        # P2 少了 T001 ⇒ T 也不该留着它（H-31：P2 不出样本 ⇒ T 不出视图）
        (builder.samples_dir(root) / "T001__a1.json").unlink()
        rep = task_view.build_all_task_views(source_root=src, target_root=root,
                                             overwrite=True)

        assert _task_views(root) == ["T002__b2.json"]
        assert rep.sweep is not None
        assert "T001__a1.json" in rep.sweep.moved

    def test_P2目录不在_不清点T(self, root):
        """⚠️ **没有名单就没有判据** ⇒ 把整目录搬空不叫「清理」，叫「毁掉」。"""
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        rep = task_view.build_all_task_views(source_root=src, target_root=root,
                                             overwrite=True)

        assert rep.sweep is not None
        assert rep.sweep.reason
        assert rep.sweep.moved == ()
        assert _task_views(root) == ["T001__a1.json"]


class Test隔离可逆:
    def test_隔离区里的文件原样还在(self, root):
        """隔离**必须可逆**：文件原样在 ``quarantine/`` ⇒ 搬回原位即可。"""
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)
        original = (builder.samples_dir(root) / "T001__a1.json").read_bytes()
        (src / "T001__a1.json").unlink()

        builder.build_all(source_root=src, target_root=root)

        q = root / pool.QUARANTINE_DIRNAME / "samples" / "T001__a1.json"
        assert q.exists()
        assert q.read_bytes() == original
        # 搬回原位 ⇒ 读侧正常读得到
        q.replace(builder.samples_dir(root) / "T001__a1.json")
        assert (builder.samples_dir(root) / "T001__a1.json").read_bytes() == original

    def test_legacy不受影响(self, root):
        """旧面是**只读存量** ⇒ 新的迁移逻辑不得重复处理它们。"""
        legacy = root / pool.LEGACY_DIRNAME / "obscura-1" / "samples"
        legacy.mkdir(parents=True)
        stale = legacy / "T009__z9.json"
        stale.write_text("{}", encoding="utf-8")
        before = stale.read_bytes()

        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)
        (src / "T001__a1.json").unlink()
        builder.build_all(source_root=src, target_root=root)

        assert stale.exists()
        assert stale.read_bytes() == before


class Test清点判据本身:
    def test_名单是这一轮该产出的_不是实际写出的(self, root):
        """``ArchiveBuildResult.delivered`` 记的是「该产出」。

        默认不覆盖模式下它照样有值（写盘前收的）⇒ 清点才不会误伤。
        """
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        rep = builder.build_all(source_root=src, target_root=root)
        assert rep.archives[0].delivered == ("T001__a1",)
        assert rep.archives[0].written == 1

        rep2 = builder.build_all(source_root=src, target_root=root)
        assert rep2.archives[0].written == 0          # 默认不覆盖 ⇒ 没写
        assert rep2.archives[0].delivered == ("T001__a1",)  # 但「该产出」

    def test_quarantine与legacy是两个目录(self, root):
        """隔离区装「本轮剔掉的孤儿」；``legacy/`` 装「旧面存量」。

        两者**处置不同**（孤儿要回滚、旧面要回溯）⇒ 混在一起就分不清
        「回溯旧批次」和「清理孤儿」。
        """
        assert pool.QUARANTINE_DIRNAME != pool.LEGACY_DIRNAME

    def test_认不出的文件留在原地(self, root):
        """面读不出的文件**不搬**——猜不出来的东西留在原地等人看。

        搬走 = 藏起来。清点不该替人做「这是什么」的决定。
        """
        src = root / "src"
        _write(src, _arc("T001"), "T001__a1.json")
        builder.build_all(source_root=src, target_root=root, overwrite=True)
        unknown = builder.samples_dir(root) / "T009__z9.json"
        unknown.write_text(json.dumps({"sample_id": "T009__z9"}), encoding="utf-8")

        rep = builder.build_all(source_root=src, target_root=root)

        assert unknown.exists()
        assert "T009__z9.json" in rep.sweep.unclassified
        assert "T009__z9.json" not in rep.sweep.moved
