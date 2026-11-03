"""访问级剔除（方案 23 A10 / R4-2(b)）。

口径只有一句：**训练集里访问被排除平台的步必须是 0**。落点有三处，
三处必须认同同一份判据：

- :func:`assembler.excluded.unit_excluded_platform` —— 判据本体；
- P2 :func:`assembler.schema.split_archive_report` —— 整条 unit 剔；
- T :func:`assembler.task_view._excluded_unit_map` —— 同一份判据，
  两边各自读 P1、不互相 import（P3 与 T 的纪律）。

所以这个文件既测判据本身，也测**两处调用方看到的是同一份证据**——
「P2 剔了、T 没剔」会让复核视图里多出模型根本没走的那一段。
"""

from __future__ import annotations

import json

from trajectory_pipeline.assembler import excluded, schema, task_view


def _obs(url: str, **kw):
    base = {
        "url": url,
        "page_title": "",
        "body_text": "正文",
        "truncated": False,
        "degraded": [],
        "video_tag_count": 0,
        "iframe_count": 0,
        "interactive_elements": [],
        "links": [],
    }
    base.update(kw)
    return base


def _action(tool: str, **kw):
    base = {
        "tool": tool,
        "params": {},
        "origin": "model",
        "target": None,
    }
    base.update(kw)
    return base


def _step(action, observation=None, rationale=None, error=""):
    return {
        "action": action,
        "observation": observation,
        "error": error,
        "rationale": rationale,
    }


def _visit(url, steps=None, **over):
    return {
        "url": url,
        "landed_url": url,
        "success": False,
        "notes": [],
        "steps": steps or [],
        "site_obs": _obs(url),
        "player_obs": None,
        "reflection": None,
        **over,
    }


def _arc(**over):
    base = {
        "task_id": "T001",  # tasks.yaml 里声明了六个被排除平台
        "title": "功夫",
        "query": "功夫 在线观看",
        "user_prompt": "有没有能看正片的",
        "search_url": "https://s.test",
        "search_observation": _obs("https://s.test"),
        "search_blocked": "",
        "steps": [],
        "provenance": {},
        "run_config": {
            "engine": "baidu",
            "max_candidates": 20,
            "max_chars": None,
            "stop_after_success": 0,
            "per_site_timeout_s": 90.0,
        },
        "candidates": [],
        "visits": [],
        "outcomes": [],
        "ledger": None,
        "warnings": [],
        "reflection": None,
    }
    base.update(over)
    return base


def _write(tmp_path, arc, name="T001__abc123.json"):
    p = tmp_path / name
    p.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
    return p


class TestPlatformOfUrl:
    def test_裸域与子域都算(self):
        assert excluded.platform_of_url("https://iqiyi.com/v_1.html") == "爱奇艺"
        assert excluded.platform_of_url("https://www.iqiyi.com/v_1.html") == "爱奇艺"

    def test_只在域名边界上匹配(self):
        """``in`` 判子串会把仿冒域也剔掉——那是一条静默的错。"""
        assert excluded.platform_of_url("https://notiqiyi.com/") == ""
        assert excluded.platform_of_url("https://iqiyi.com.evil.test/") == ""

    def test_腾讯只认内容域(self):
        assert excluded.platform_of_url("https://v.qq.com/x/cover/1.html") == "腾讯视频"
        # qq.com 门户不是「腾讯视频」
        assert excluded.platform_of_url("https://www.qq.com/") == ""

    def test_短链域名同样算(self):
        assert excluded.platform_of_url("https://b23.tv/abc") == "哔哩哔哩"

    def test_空串与坏网址不抛(self):
        assert excluded.platform_of_url("") == ""
        assert excluded.platform_of_url("not a url") == ""


class TestUnitExcludedPlatform:
    def test_模型goto命中(self):
        got = excluded.unit_excluded_platform(
            [_action("goto", params={"url": "https://www.iqiyi.com/v_1.html"})],
            [],
            "T001",
        )
        assert got == "爱奇艺"

    def test_基础设施动作不算访问(self):
        """``new_tab`` / ``close_tab`` 是执行器的会话管理，不是模型选的路径。"""
        got = excluded.unit_excluded_platform(
            [
                _action("new_tab", params={"url": "https://www.iqiyi.com/"},
                        origin="infrastructure"),
                _action("close_tab", origin="infrastructure"),
            ],
            [],
            "T001",
        )
        assert got == ""

    def test_基础设施跳过去但观察落在上面仍算(self):
        """跳转后的页面若真被看到，观察会把它记下来——它不是「没发生过」。"""
        got = excluded.unit_excluded_platform(
            [_action("new_tab", params={"url": "https://www.iqiyi.com/"},
                     origin="infrastructure")],
            [_obs("https://www.iqiyi.com/v_1.html")],
            "T001",
        )
        assert got == "爱奇艺"

    def test_点击后的观察命中(self):
        got = excluded.unit_excluded_platform(
            [_action("click", target={"role": "link", "name": "爱奇艺"})],
            [_obs("https://www.iqiyi.com/v_1.html")],
            "T001",
        )
        assert got == "爱奇艺"

    def test_任务没声明排除就不判(self):
        got = excluded.unit_excluded_platform(
            [_action("goto", params={"url": "https://www.iqiyi.com/"})],
            [],
            "NO_SUCH_TASK",
        )
        assert got == ""


class TestSplitArchiveReport:
    def test_搜索单元点进被排除平台也剔(self, tmp_path):
        """搜索单元的 ``Sample.observations`` 只有一张汇总快照（与动作不
        等长）；判定必须回到**逐步** observation，否则点进去的那一页看不见。"""
        arc = _arc(
            steps=[
                _step(_action("goto", params={"url": "https://s.test"})),
                _step(
                    _action("click", target={"role": "link", "name": "爱奇艺"}),
                    _obs("https://www.iqiyi.com/v_1.html"),
                ),
            ],
            visits=[_visit("https://a.test/")],
        )
        result = schema.split_archive_report(_write(tmp_path, arc))
        # per-Run：一份存档切**一条**轨迹；被剔的 unit 记在剔除账里
        assert len(result.samples) == 1
        assert result.excluded == (("search", "爱奇艺"),)

    def test_整条unit剔而不是只剔命中步(self, tmp_path):
        """只剔命中步会把一个 unit 截成前半段：前面的步还在为这个平台
        服务，outcome 却已经被扔掉。"""
        arc = _arc(
            visits=[
                _visit("https://www.iqiyi.com/v_1.html", steps=[
                    _step(_action("goto", params={"url": "https://www.iqiyi.com/v_1.html"})),
                    _step(_action("click", target={"role": "button", "name": "播放"}),
                          _obs("https://www.iqiyi.com/v_1.html")),
                ]),
                _visit("https://a.test/", steps=[
                    _step(_action("goto", params={"url": "https://a.test/"}),
                          _obs("https://a.test/")),
                ]),
            ],
        )
        result = schema.split_archive_report(_write(tmp_path, arc))
        # site-1 整条不见；site-2 **保留原始编号**（P2 的 site_outcomes 与
        # T 的 unit_sample_ids 用的是同一套编号，重排会让两边对不上）
        assert result.excluded == (("site-1", "爱奇艺"),)
        assert len(result.samples) == 1
        kept = [s.index for s in result.samples[0].site_outcomes]
        assert kept == [2]

    def test_split_archive仍返回样本列表(self, tmp_path):
        """老调用方拿的是 list——剔除口径变了，返回形态不变。"""
        arc = _arc(visits=[_visit("https://a.test/")])
        got = schema.split_archive(_write(tmp_path, arc))
        assert isinstance(got, list)
        # per-Run：一份存档 ⇒ **一条**轨迹（不是一个单元一条）
        assert len(got) == 1
        assert [s.index for s in got[0].site_outcomes] == [1]

    def test_证据只认步_不认访问地址本身(self, tmp_path):
        """判据的证据是**逐步**动作与观察，不是 ``visit.url``。

        口径是「训练集里访问被排除平台的**步** = 0」：steps 缺失的降级
        访问在样本里既没有动作也没有观察（``observations`` 为空），
        所以它不违反这条；而它的 ``visit.url`` 不进样本正文（只留在
        存档里）。按 ``visit.url`` 剔会超出 owner 裁定的口径，也会让
        「剔掉多少条」与我上报的影响面读数对不上。
        """
        arc = _arc(visits=[_visit("https://www.iqiyi.com/v_1.html")])
        result = schema.split_archive_report(_write(tmp_path, arc))
        assert result.excluded == ()
        assert len(result.samples) == 1
        assert [s.index for s in result.samples[0].site_outcomes] == [1]


class TestTaskViewExclusion:
    """T 侧读 P1、不经过 P2——同一份证据必须得出同一组被剔 unit。"""

    def _arc(self):
        return _arc(
            visits=[
                _visit("https://www.iqiyi.com/v_1.html", steps=[
                    _step(_action("goto", params={"url": "https://www.iqiyi.com/v_1.html"}),
                          _obs("https://www.iqiyi.com/v_1.html")),
                ]),
                _visit("https://a.test/", steps=[
                    _step(_action("goto", params={"url": "https://a.test/"}),
                          _obs("https://a.test/")),
                ]),
            ],
            outcomes=[
                {"url": "https://a.test/", "branch": "no_play_control",
                 "branch_label": "", "evidence": "无播放器",
                 "site_url": "https://a.test/", "play_page_url": ""},
            ],
        )

    def test_被剔unit不进flatten(self):
        units = task_view._flatten_units(self._arc())
        assert [u["unit"] for u in units] == ["search", "site-2"]

    def test_meta留剔除账(self):
        meta = task_view.to_task_meta(self._arc())
        assert meta["excluded_units"] == [{"unit": "site-1", "platform": "爱奇艺"}]
        assert meta["unit_count"] == 2
        assert all("__site-1" not in sid for sid in meta["unit_sample_ids"])
        assert all(row["unit"] != "site-1" for row in meta["gate_breakdown"])

    def test_与P2同一份证据(self, tmp_path):
        """同一条 unit 不能一个剔一个留——那会让复核视图里多出一段
        模型根本没走过的路，而报告上看不出异常。"""
        arc = self._arc()
        p2 = schema.split_archive_report(_write(tmp_path, arc))
        t_side = {
            unit for unit in
            (u["unit"] for u in task_view._flatten_units(arc))
        }
        # P2 侧：被剔的 unit **一条轨迹都不出** ⇒ 留下来的 site 编号
        # 与 T 侧一致（两边读同一份判据：`assembler.excluded`）。
        p2_kept = {f"site-{s.index}" for s in p2.samples[0].site_outcomes}
        t_kept = {u for u in t_side if u.startswith("site-")}
        assert p2_kept == t_kept
        assert "site-1" not in p2_kept