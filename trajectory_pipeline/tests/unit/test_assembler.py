"""P2 切分的单元测试——**一个决策单元一条**。

重点不是「能切出几条」，而是四件切错了就静默的事：

1. **粒度**：搜索与每个站点各自成条。压成长轨迹会把「选哪个站」与
   「这个站行不行」塞进同一次预测里。
2. **outcome 配对**：存档里 ``visits[i].url`` 是候选链接，
   ``outcomes[j].url`` 是落地地址，跳转过的站两者不等——配不上就是
   每条负样本都显示成「无结论」，报表上负样本一条不少、分子全空。
3. **降级可见**：老存档缺 ``steps`` / ``user_prompt`` / 全文正文，
   这些样本必须**自带标记**，不能和新存档的样本混着用。
4. **被反爬拦的 run 不切搜索单元**：那一步的观察是验证码页，
   切成「模型该怎么做」的样本只会教它对着验证码页编答案。
"""

from __future__ import annotations

import json

import pytest

from trajectory_pipeline.assembler import schema
from trajectory_pipeline.assembler.schema import P1ShapeError


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


def _arc(**over):
    base = {
        "task_id": "T001", "title": "功夫", "query": "功夫 在线观看",
        "user_prompt": "有没有能看正片的", "search_url": "https://s.test",
        "search_observation": _obs("https://s.test"), "search_blocked": "",
        "steps": [], "provenance": {"genre": "喜剧"},
        "candidates": [], "visits": [], "outcomes": [], "ledger": None,
        "warnings": [],
    }
    base.update(over)
    return base


def _write(tmp_path, arc, name="T001__abc123.json"):
    p = tmp_path / name
    p.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
    return p


class TestGranularity:
    def test_搜索与每个站点各自成条(self, tmp_path):
        arc = _arc(visits=[_visit("https://a.test/"), _visit("https://b.test/")])
        units = [s.unit for s in schema.split_archive(_write(tmp_path, arc))]
        assert units == ["search", "site-1", "site-2"]

    def test_搜索单元在前(self, tmp_path):
        """顺序即时间序。乱序会让按 id 排序的批次读起来像时序错乱。"""
        arc = _arc(visits=[_visit("https://a.test/")])
        assert schema.split_archive(_write(tmp_path, arc))[0].unit == "search"

    def test_站点单元带落地前后两个观察(self, tmp_path):
        arc = _arc(visits=[_visit("https://a.test/", "https://a.test/play",
                                  site_obs=_obs("https://a.test"),
                                  player_obs=_obs("https://a.test/play", video_tag_count=1))])
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert [o["url"] for o in site.observations] == \
            ["https://a.test", "https://a.test/play"]

    def test_没有观察的单元不产出(self, tmp_path):
        """两者皆空 → 切不出东西。抛出来而不是给一条空样本。"""
        p = _write(tmp_path, _arc(visits=[], search_observation=None))
        with pytest.raises(P1ShapeError, match="切不出样本"):
            schema.split_archive(p)

    def test_搜索阶段无outcome(self, tmp_path):
        """搜索阶段不记站点级 outcome。给它硬配一条是编造。"""
        s = schema.split_archive(_write(tmp_path, _arc()))[0]
        assert s.outcome is None


class TestOutcomePairing:
    def test_跳转过的站按落地地址配(self, tmp_path):
        """实测存档：4 个 run 各有 3 个访问点是 http→https 跳转过的，
        候选 url 与 outcome url 对不上。只按候选 url 配的后果是静默的——
        负样本一条不少，分子全空。"""
        arc = _arc(
            visits=[_visit("http://x.test/movie", "https://x.test/movie")],
            outcomes=[_outcome("https://x.test/movie", "no_play_control")],
        )
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert site.outcome is not None
        assert site.outcome.branch == "no_play_control"

    def test_候选地址也能配(self, tmp_path):
        arc = _arc(visits=[_visit("https://x.test/movie")],
                   outcomes=[_outcome("https://x.test/movie", "no_play_control")])
        assert schema.split_archive(_write(tmp_path, arc))[1].outcome.branch \
            == "no_play_control"

    def test_配不上要留痕(self, tmp_path):
        """静默 None 与「搜索阶段本来就没有 outcome」同形。"""
        arc = _arc(visits=[_visit("https://x.test/movie")], outcomes=[])
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert site.outcome is None
        assert "outcome_missing" in site.degraded_from

    def test_成功即无分支(self, tmp_path):
        """``ledger`` 只记失败分支，成功的访问不记账——
        所以 ``branch=None`` 是**成功**，不是「没结论」。"""
        arc = _arc(visits=[_visit("https://x.test/", success=True)],
                   outcomes=[_outcome("https://x.test/", None)])
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert site.outcome.succeeded is True

    def test_成功与分支对不上要留痕(self, tmp_path):
        """「成功」直接决定样本进不进训练集，而它必须只有一个来源说了算。
        两个来源打架时标记出来，而不是挑一个——挑一个就是在猜。"""
        arc = _arc(visits=[_visit("https://x.test/", success=True)],
                   outcomes=[_outcome("https://x.test/", "no_play_control")])
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert "success_branch_mismatch" in site.degraded_from

    def test_不从存档的is_negative_sample取判据(self, tmp_path):
        """那份判据由 executor 的 ``NON_SAMPLE_BRANCHES`` 定义，assembler
        不 import executor，两份口径就可能不同步。存档里把
        ``is_negative_sample`` 写成 True 也不采信。"""
        o = _outcome("https://x.test/", None)
        o["is_negative_sample"] = True
        arc = _arc(visits=[_visit("https://x.test/", success=True)], outcomes=[o])
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert site.outcome.branch is None


class TestBlockedRun:
    def test_被反爬拦时不切搜索单元(self, tmp_path):
        """拦截发生在取候选之前，那一步的观察是验证码页。切成样本只会
        教模型对着验证码页编答案。"""
        arc = _arc(search_blocked="captcha", visits=[])
        units = [s.unit for s in schema.split_archive(_write(tmp_path, arc))]
        assert units == []

    def test_被拦但还有访问记录时站点仍成条(self, tmp_path):
        """拦截的是搜索页，已访问的站点不受影响。"""
        arc = _arc(search_blocked="captcha",
                   visits=[_visit("https://a.test/")],
                   outcomes=[_outcome("https://a.test/", "unresolved")])
        assert [s.unit for s in schema.split_archive(_write(tmp_path, arc))] == ["site-1"]


class TestDegradedMarkers:
    def test_老存档缺steps要留痕(self, tmp_path):
        arc = _arc(visits=[_visit("https://a.test/")])
        for s in schema.split_archive(_write(tmp_path, arc)):
            assert "steps" in s.degraded_from

    def test_有steps时不留痕(self, tmp_path):
        arc = _arc(steps=[{"action": {"tool": "goto", "params": {"url": "u"},
                                      "origin": "model", "target": None},
                          "observation": None, "error": ""}],
                   visits=[dict(_visit("https://a.test/"), steps=[
                       {"action": {"tool": "goto", "params": {"url": "u"},
                                   "origin": "model", "target": None},
                        "observation": None, "error": ""}])])
        for s in schema.split_archive(_write(tmp_path, arc)):
            assert "steps" not in s.degraded_from

    def test_缺用户轮次要留痕(self, tmp_path):
        arc = _arc()
        arc.pop("user_prompt")
        assert "user_prompt" in schema.split_archive(_write(tmp_path, arc))[0].degraded_from

    def test_只有预览正文标preview_only(self, tmp_path):
        """400 字符冒充全文 → rationale 的实体核查会把落在预览外的实体
        判成幻觉。那不是幻觉，是**没存**。"""
        arc = _arc(search_observation={"url": "u", "body_preview": "前四百字"})
        assert "body_preview_only" in \
            schema.split_archive(_write(tmp_path, arc))[0].degraded_from

    def test_完全没正文标no_body(self, tmp_path):
        arc = _arc(search_observation={"url": "u", "body_text": ""})
        assert "no_body" in \
            schema.split_archive(_write(tmp_path, arc))[0].degraded_from

    def test_标记去重且有序(self, tmp_path):
        arc = _arc(visits=[_visit("https://a.test/")])
        s = schema.split_archive(_write(tmp_path, arc))[1]
        assert list(s.degraded_from) == list(dict.fromkeys(s.degraded_from))

    def test_新存档零降级(self, tmp_path):
        """一条干净的样本必须能一眼认出来。标记若总在，整条标记就失效了。"""
        arc = _arc(steps=[{"action": {"tool": "goto", "params": {"url": "u"},
                                      "origin": "model", "target": None},
                          "observation": None, "error": ""}],
                   visits=[dict(_visit("https://a.test/"), steps=[
                       {"action": {"tool": "goto", "params": {"url": "u"},
                                   "origin": "model", "target": None},
                        "observation": None, "error": ""}])])
        assert schema.split_archive(_write(tmp_path, arc))[0].degraded_from == ()


class TestSampleId:
    def test_格式可反查源存档(self, tmp_path):
        sid = schema.split_archive(_write(tmp_path, _arc()))[0].sample_id
        assert sid == "T001__abc123__search"
        task, run, unit = sid.split("__")
        assert (task, unit) == ("T001", "search")
        assert run

    def test_同批次内按unit单调(self, tmp_path):
        arc = _arc(visits=[_visit(f"https://a{i}.test/") for i in range(12)])
        units = [s.sample_id.split("__")[2] for s in schema.split_archive(_write(tmp_path, arc))]
        assert units == ["search"] + [f"site-{i}" for i in range(1, 13)]

    def test_取run_id(self):
        assert schema.run_id_of("T001__004b6be9.json") == "004b6be9"

    def test_探针取证取run_id不炸(self):
        """``obscura_tools.json`` 没有 ``__``。它走不到这里，但要炸得
        明白而不是抛 IndexError。"""
        assert schema.run_id_of("obscura_tools.json") == "obscura_tools"


class TestIterSamples:
    def test_跳过探针取证(self, tmp_path):
        """取证文件顶层没有 ``task_id``，当存档读会直接打断整批。"""
        _write(tmp_path, _arc(), "T001__a1.json")
        (tmp_path / "obscura_tools.json").write_text('{"tools": []}', encoding="utf-8")
        assert len(list(schema.iter_samples(tmp_path))) == 1

    def test_跳过复核档(self, tmp_path):
        """复核档由 executor 侧统一挑选（``select_archives``）。
        这里再选一次就会同一条跑两遍，而两遍的复核结论可能不同。"""
        _write(tmp_path, _arc(), "T001__a1.json")
        _write(tmp_path, _arc(), "T001__a1.reviewed.json")
        assert [s.sample_id for s in schema.iter_samples(tmp_path)] == \
            ["T001__a1__search"]

    def test_按文件名排序(self, tmp_path):
        _write(tmp_path, _arc(task_id="T002"), "T002__b1.json")
        _write(tmp_path, _arc(task_id="T001"), "T001__a1.json")
        assert [s.task_id for s in schema.iter_samples(tmp_path)] == ["T001", "T002"]

    def test_被拦的run不打断整批(self, tmp_path):
        """一批里混进几个被反爬拦的 run 是常态（executor 的警告里就写着
        「直接重跑同一引擎大概率仍是验证码页」）。抛出去等于整批陪葬。"""
        _write(tmp_path, _arc(task_id="T001", search_blocked="captcha",
                              search_observation=None, visits=[]), "T001__a1.json")
        _write(tmp_path, _arc(task_id="T002"), "T002__a1.json")
        assert [s.task_id for s in schema.iter_samples(tmp_path)] == ["T002"]

    def test_坏档抛出且带文件名(self, tmp_path):
        """坏档静默变成「零样本」的话，批次看起来只是少了几条，
        没人会去查。异常里必须点名是哪个文件。"""
        (tmp_path / "T009__bad.json").write_text('{"visits": []}', encoding="utf-8")
        with pytest.raises(P1ShapeError, match="T009__bad.json"):
            list(schema.iter_samples(tmp_path))


class TestReadArchive:
    def test_不是JSON(self, tmp_path):
        p = tmp_path / "T001__a1.json"
        p.write_text("{ 不是", encoding="utf-8")
        with pytest.raises(P1ShapeError, match="不是合法 JSON"):
            schema.read_archive(p)

    def test_顶层不是对象(self, tmp_path):
        p = tmp_path / "T001__a1.json"
        p.write_text("[1,2]", encoding="utf-8")
        with pytest.raises(P1ShapeError, match="顶层不是 JSON 对象"):
            schema.read_archive(p)

    def test_缺task_id(self, tmp_path):
        p = tmp_path / "T001__a1.json"
        p.write_text('{"visits": []}', encoding="utf-8")
        with pytest.raises(P1ShapeError, match="缺 task_id"):
            schema.read_archive(p)


class TestToolSpec:
    def test_可见动作排除执行器内部动作(self):
        assert {t.name for t in schema.TOOL_SPECS if t.visible_to_model()} == \
            {"goto", "click"}

    def test_参数名可序列化(self):
        assert schema.TOOL_SPECS[0].to_json()["params"] == ["url"]
