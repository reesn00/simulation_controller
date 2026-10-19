"""goto URL 溯源断言的单测（方案 13 §6.2，A3-2）。

数据面全是**手搓的 P1 存档 dict**——本检查器的纪律就是读档不 import
executor，测试 fixture 也照办：不构造任何 executor 对象，只造
``{"action": ..., "observation": ...}`` 形状的字典。形状本身由
``test_agent_visit.py`` 的端到端用例（真链落盘 → 本检查器过一遍）钉住。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.evaluation.goto_trace import check_batch, check_record


def _step(tool: str, *, origin: str = "model", url: str = "",
          obs_url: str = "", links: list | None = None,
          params: dict | None = None) -> dict:
    """造一步。``links`` 是 ``[{"text","href"}]`` 观察链接。"""
    action = {"tool": tool, "params": params if params is not None
              else ({"url": url} if url else {}),
              "origin": origin, "target": None}
    obs = None
    if obs_url or links is not None:
        obs = {"url": obs_url, "links": links or []}
    return {"action": action, "observation": obs, "error": "",
            "rationale": None}


def _doc(*, steps=(), intermediates=(), visits=(), candidates=(),
         search_url: str = "", search_obs=None,
         executor_mode: str | None = "agent", task_id: str = "T1") -> dict:
    doc = {
        "task_id": task_id,
        "search_url": search_url,
        "search_observation": search_obs,
        "candidates": [{"url": u, "text": "", "rank": i + 1, "host": ""}
                       for i, u in enumerate(candidates)],
        "steps": list(steps),
        "intermediates": [{"url": u, "steps": list(s), "landed_url": u,
                           "notes": [], "obs": None, "is_intermediate": None,
                           "entries": None, "referrals": [], "candidates": []}
                          for u, s in intermediates],
        "visits": [{"url": u, "landed_url": u, "success": False, "notes": [],
                    "steps": list(s), "site_obs": None, "player_obs": None,
                    "reflection": None, "search": None}
                   for u, s in visits],
    }
    if executor_mode is not None:
        doc["executor_mode"] = executor_mode
    return doc


class Test锚点来源:
    def test_goto候选URL可溯源(self):
        """agent 首步 goto 候选页——锚点池（候选 ∪ search_url ∪ …）兜住。"""
        doc = _doc(candidates=["https://site.example/"],
                   visits=[("https://site.example/", [
                       _step("goto", url="https://site.example/",
                             obs_url="https://site.example/"),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 1 and r["traced"] == 1 and not r["violations"]

    def test_goto检索URL可溯源(self):
        """搜索单元的 goto(search_url)：scripted 批也走这步，必须恒过。"""
        doc = _doc(search_url="https://search.example/?q=x",
                   steps=[_step("goto",
                                url="https://search.example/?q=x",
                                obs_url="https://search.example/?q=x")],
                   executor_mode="scripted")
        r = check_record(doc)
        assert r["checked"] == 1 and r["traced"] == 1 and not r["violations"]

    def test_中间页URL在BC替换候选后仍可溯源(self):
        """B/C 存档里 intermediates[].url 不在 candidates（已被引荐候选
        替换）——锚点池并上中间页 URL 才兜得住。"""
        page = "https://agg.example/list"
        doc = _doc(candidates=["https://final.example/play"],
                   intermediates=[(page, [
                       _step("goto", url=page, obs_url=page),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 1 and r["traced"] == 1

    def test_搜索观察的链接进锚点(self):
        """search_observation 的 links 在站点访问**之前**就可见——
        候选本来就来自搜索结果，不喂这一步会出现「候选可溯源但
        搜索页上别的链接全幻觉」的系统性误报。"""
        doc = _doc(search_url="https://search.example/?q=x",
                   search_obs={"url": "https://search.example/?q=x",
                               "links": [{"text": "t", "href": "https://a.example/"}]},
                   visits=[("https://x.example/", [
                       _step("goto", url="https://a.example/",
                             obs_url="https://a.example/"),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 1 and r["traced"] == 1


class Test递推时序:
    def test_前步观察的链接可goto(self):
        """第 N 步观察到的链接，第 N+1 步 goto 可溯源——递推的基本盘。"""
        doc = _doc(candidates=["https://site.example/"],
                   visits=[("https://site.example/", [
                       _step("goto", url="https://site.example/",
                             obs_url="https://site.example/",
                             links=[{"text": "播放", "href": "https://site.example/play/1"}]),
                       _step("click", obs_url="https://site.example/",
                             links=[{"text": "播放", "href": "https://site.example/play/1"}]),
                       _step("goto", url="https://site.example/play/1",
                             obs_url="https://site.example/play/1"),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 2 and r["traced"] == 2 and not r["violations"]

    def test_后见链接救不了先前的goto(self):
        """第 N 步观察到的链接救不了第 N-1 步的 goto——模型做决定时
        还没看见它。**顺序反了检查器就是摆设**：幻觉 goto 走一步就能
        看见自己的落地页，落地页的链接随后就进锚点。"""
        doc = _doc(candidates=["https://site.example/"],
                   visits=[("https://site.example/", [
                       _step("goto", url="https://site.example/play/1",
                             obs_url="https://site.example/play/1",
                             links=[{"text": "播放", "href": "https://site.example/play/1"}]),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 1 and r["traced"] == 0
        assert len(r["violations"]) == 1

    def test_跨流递推_搜索步的链接喂给站点访问(self):
        """搜索单元（record.steps）观察到的链接，站点访问里 goto 可溯源。"""
        doc = _doc(candidates=["https://site.example/"],
                   steps=[_step("goto",
                                url="https://search.example/?q=x",
                                obs_url="https://search.example/?q=x",
                                links=[{"text": "t", "href": "https://site.example/"}])],
                   search_url="https://search.example/?q=x",
                   visits=[("https://site.example/", [
                       _step("goto", url="https://site.example/",
                             obs_url="https://site.example/"),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 2 and r["traced"] == 2


class Test容差:
    def test_相对href以观察页为基解析(self):
        """links[].href 里的相对链接（/play/1）以**产生它的观察页**为基
        解析——agent 把站内相对 URL 补全成绝对 URL 再 goto 是正常导航，
        §6.2 的例外口径由此一般化，不需要特判。"""
        doc = _doc(candidates=["https://site.example/"],
                   visits=[("https://site.example/", [
                       _step("goto", url="https://site.example/",
                             obs_url="https://site.example/",
                             links=[{"text": "播放", "href": "/play/1"}]),
                       _step("goto", url="https://site.example/play/1",
                             obs_url="https://site.example/play/1"),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 2 and r["traced"] == 2

    def test_归一匹配_尾斜杠与大小写(self):
        """原串不同但 (host 小写, 去尾斜杠 path, query) 相同 → 可溯源。"""
        doc = _doc(candidates=["https://Site.Example"],
                   visits=[("https://site.example/", [
                       _step("goto", url="https://site.example/",
                             obs_url="https://site.example/"),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 1 and r["traced"] == 1

    @pytest.mark.parametrize("landed", [
        "https://site.example/play/1?session=abc",   # 重定向补尾巴
        "https://site.example/play",                 # 重定向剥尾巴
    ])
    def test_landed容差_重定向改写(self, landed):
        """goto 的 URL 不在锚点，但落地页与锚点 host 相同、path 互为
        前缀 → 可溯源（容忍重定向/规范化改写）。**host 不同救不了**——
        下一题专门钉。"""
        doc = _doc(candidates=["https://site.example/"],
                   visits=[("https://site.example/", [
                       _step("goto", url="https://site.example/",
                             obs_url="https://site.example/",
                             links=[{"text": "播放", "href": "https://site.example/play/1"}]),
                       _step("goto", url="https://site.example/watch?id=1",
                             obs_url=landed),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 2 and r["traced"] == 2


class Test违规:
    def test_凭空编造的绝对URL记违规(self):
        doc = _doc(candidates=["https://site.example/"],
                   visits=[("https://site.example/", [
                       _step("goto", url="https://evil.example/x",
                             obs_url="https://evil.example/x"),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 1 and r["violations"] == [
            {"stream": "visits", "index": 0,
             "url": "https://evil.example/x",
             "landed": "https://evil.example/x"},
        ]

    def test_host不同landed也救不了(self):
        """landed 容差只在同 host 内放宽——跨 host 的重定向式匹配会把
        「编造了一个 URL 而站点恰好 302 到首页」洗白。"""
        doc = _doc(candidates=["https://site.example/"],
                   visits=[("https://site.example/", [
                       _step("goto", url="https://site.example/",
                             obs_url="https://site.example/",
                             links=[{"text": "t", "href": "https://a.example/p"}]),
                       # host 相同、path 不互为前缀 → 也救不了
                       _step("goto", url="https://site.example/totally/other",
                             obs_url="https://site.example/totally/other"),
                       # host 不同 → 更救不了
                       _step("goto", url="https://other.example/p",
                             obs_url="https://other.example/p"),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 3 and r["traced"] == 1
        assert [v["url"] for v in r["violations"]] == [
            "https://site.example/totally/other",
            "https://other.example/p",
        ]

    def test_infrastructure的goto不查(self):
        """``origin="infrastructure"`` 的 new_tab/goto 是执行器的会话
        管理，URL 来自代码——溯源只核 **LLM 输出**（§6.2）。"""
        doc = _doc(candidates=["https://site.example/"],
                   visits=[("https://site.example/", [
                       _step("new_tab", origin="infrastructure",
                             url="https://anywhere.example/x",
                             obs_url="https://anywhere.example/x"),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 0 and not r["violations"]

    def test_非goto动作不查(self):
        """click/fill/press 没有 URL 参数这回事——检查器只盯 goto。"""
        doc = _doc(candidates=["https://site.example/"],
                   visits=[("https://site.example/", [
                       _step("goto", url="https://site.example/",
                             obs_url="https://site.example/"),
                       _step("click", params={}),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 1 and r["traced"] == 1

    def test_goto缺URL单独计数不判违规(self):
        """malformed（无 params.url）不是「溯源不通过」——判违规会把
        采集缺件记成模型幻觉，两个病因必须分开可见。"""
        doc = _doc(candidates=["https://site.example/"],
                   visits=[("https://site.example/", [
                       _step("goto", url=""),
                   ])])
        r = check_record(doc)
        assert r["checked"] == 0 and r["skipped"] == 1
        assert not r["violations"]


class Test批次汇总:
    def test_按executor_mode分桶(self):
        """验收⑤的读数口径：agent 批的幻觉 goto 计数从 by_mode 里读；
        缺 executor_mode 的老存档进 unknown 桶，不静默丢掉。"""
        clean = _doc(candidates=["https://a.example/"],
                     visits=[("https://a.example/", [
                         _step("goto", url="https://a.example/",
                               obs_url="https://a.example/")])],
                     executor_mode="agent", task_id="T1")
        dirty = _doc(candidates=["https://a.example/"],
                     visits=[("https://a.example/", [
                         _step("goto", url="https://b.example/x",
                               obs_url="https://b.example/x")])],
                     executor_mode="agent", task_id="T2")
        legacy = _doc(steps=[_step("goto", url="https://c.example/",
                                   obs_url="https://c.example/")],
                      search_url="https://c.example/", task_id="T3",
                      executor_mode=None)
        report = check_batch([clean, dirty, legacy])
        assert report["records"] == 3
        assert report["checked"] == 3 and report["violations"] == 1
        assert report["by_mode"]["agent"] == {
            "records": 2, "checked": 2, "traced": 1, "violations": 1}
        assert report["by_mode"]["unknown"] == {
            "records": 1, "checked": 1, "traced": 1, "violations": 0}

    def test_空档零计数(self):
        report = check_batch([_doc()])
        assert report["checked"] == 0 and report["violations"] == 0
        assert report["by_mode"]["agent"]["records"] == 1


class Test端到端形状:
    async def test_真链落的档检查器零误报(self):
        """真 executor 链落一份 P1 → 本检查器过一遍：scripted 与 agent
        都该零违规（恒真式那头是断言的基线，agent 那头是 I6/白名单的
        旁证）。**形状漂移**（字段改名/换层）在这条用例上直接红。
        """
        # 复用 test_agent_visit 的真链 fixture（它跑的是真 visit_site /
        # agent_visit，落的是真 RunRecord.to_json）。
        from trajectory_pipeline.tests.unit import test_agent_visit as tav

        sr, sl, _ = await tav._run_scripted()
        ar, al, _ = await tav._run_agent()
        for doc in (tav._full_archive(sr, sl, "scripted"),
                    tav._full_archive(ar, al, "agent")):
            r = check_record(doc)
            assert r["violations"] == [], (
                f"真链零幻觉基线破了：{r['violations']}")
