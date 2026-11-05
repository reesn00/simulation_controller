"""收尾文本（``assembler.closing``）与任务合同（``assembler.contracts``）。

收尾是**训练语料里唯一一句「助手对用户说的话」**（末条 assistant），
它出错的形态都很安静：

- 播报内部分支名 / 计数（审计 G3）——模型学会对用户念台账；
- 失败答复里带了被排除站点的完整 URL——「答复含 URL」这个验收指标
  当场失效（用户要的是**可播放**网址）；
- P3 与 T 各写一句——同一条 run 在训练方与 LS 里变成两个故事（O-5）。

所以这里逐条盯住，而不是靠「看起来差不多」。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.assembler import closing, contracts, task_view, views


class Test成功答复:
    def test_给播放页地址(self):
        text = closing.closing_for_unit(
            title="功夫",
            unit="site-1",
            outcome={"url": "https://x.test", "branch": None,
                     "play_page_url": "https://x.test/play"},
        )
        assert text == "找到了《功夫》的可播放网址：https://x.test/play。"
        assert closing.count_urls(text) == 1

    def test_缺播放页地址退回落地地址(self):
        """落地地址也是网址——不编一个，也不装作没有。"""
        text = closing.closing_for_unit(
            title="功夫", unit="site-1",
            outcome={"url": "https://x.test", "branch": None},
        )
        assert "https://x.test" in text

    def test_连网址都没有时如实说(self):
        text = closing.closing_for_unit(
            title="功夫", unit="site-1", outcome={"branch": None},
        )
        assert "没有记下网址" in text
        assert closing.count_urls(text) == 0

    def test_成功也要交代被排除的站点(self):
        text = closing.closing_for_unit(
            title="功夫", unit="site-1",
            outcome={"url": "https://x.test/play", "branch": None},
        )
        assert "《功夫》" in text


class Test失败答复:
    def test_给站点与原因(self):
        text = closing.closing_for_unit(
            title="功夫", unit="site-1",
            outcome={"site_url": "https://a.test/1", "branch": "no_play_control"},
        )
        assert text == (
            "没有找到《功夫》的可播放网址，"
            "已排除的站点：a.test（页面上没有播放控件，排除）。"
        )

    def test_站点只写host不带协议(self):
        """失败答复里有 ``http`` 会让「答复含 URL」的验收指标失去意义——
        用户要的是**可播放**网址，不是被排除站点的地址。"""
        text = closing.closing_for_unit(
            title="功夫", unit="site-1",
            outcome={"site_url": "https://a.test/very/long/path?x=1",
                     "branch": "blocked"},
        )
        assert "http" not in text
        assert "a.test" in text
        assert closing.count_urls(text) == 0

    def test_未知分支不写分支名(self):
        """分支枚举名是并池口径。未知分支退化成「原因未能确认」，
        分支本身仍在 outcome / ledger 里可查——**退役 ≠ 失忆**。"""
        text = closing.closing_for_unit(
            title="功夫", unit="site-1",
            outcome={"site_url": "https://a.test", "branch": "future_branch"},
        )
        assert "future_branch" not in text
        assert closing.UNKNOWN_REASON in text

    def test_同一站点同一原因只写一次(self):
        text = closing.closing_for_unit(
            title="功夫", unit="site-1",
            outcome={"site_url": "https://a.test", "branch": "blocked"},
        )
        assert text.count("a.test") == 1

    def test_多站点按序并列(self):
        text = closing.closing_text(closing.ClosingFacts(
            title="功夫",
            outcomes=(
                {"site_url": "https://a.test", "branch": "blocked"},
                {"site_url": "https://b.test", "branch": "broken"},
            ),
        ))
        assert "a.test（地区限制/验证码/反爬阻断，排除）" in text
        assert "b.test（内容损坏，排除）" in text


class Test站点标签取落地页:
    """候选链接常常是搜索结果的跳转链接（``baidu.com/link?url=…``）。

    取候选的 host ⇒ 把**搜索引擎**写成「被排除的站点」。实测 A13 那
    8 份存档里，per-Run 的失败清单大半是 ``www.baidu.com``。
    """

    def test_优先取落地页(self):
        text = closing.closing_text(closing.ClosingFacts(
            title="功夫",
            outcomes=({
                "url": "https://www.teyihealth.com/xcvod/35193.html",
                "site_url": "http://www.baidu.com/link?url=abc",
                "branch": "broken",
            },),
        ))
        assert "www.teyihealth.com（内容损坏，排除）" in text
        assert "baidu" not in text

    def test_落地页缺失才退回候选(self):
        text = closing.closing_text(closing.ClosingFacts(
            title="功夫",
            outcomes=({"site_url": "https://a.test/1", "branch": "blocked"},),
        ))
        assert "a.test（地区限制/验证码/反爬阻断，排除）" in text

    def test_两个都没有时不编(self):
        text = closing.closing_text(closing.ClosingFacts(
            title="功夫",
            outcomes=({"branch": "blocked"},),
        ))
        assert "该站点（地区限制/验证码/反爬阻断，排除）" in text
        assert "http" not in text


class Test没有结论的答复:
    def test_搜索单元交代下一步(self):
        text = closing.closing_for_unit(title="功夫", unit="search")
        assert text == "已找到《功夫》的候选站点，接下来逐一核验。"

    def test_有动作但没访问到站点(self):
        text = closing.closing_text(closing.ClosingFacts(
            title="功夫", no_sites=True,
        ))
        assert text == "没有找到《功夫》的可访问站点。"

    def test_站点单元缺outcome不编原因(self):
        text = closing.closing_for_unit(title="功夫", unit="site-1")
        assert text == "没有找到《功夫》的可播放网址，原因未能确认。"

    def test_无片名时不出现空书名号(self):
        text = closing.closing_for_unit(title="", unit="site-1")
        assert "《》" not in text
        assert text == "没有找到可播放的网址，原因未能确认。"


class Test结论:
    def test_结论另起一句(self):
        text = closing.closing_for_unit(
            title="功夫", unit="site-1",
            outcome={"site_url": "https://a.test", "branch": "blocked"},
            conclusion="这站要登录",
        )
        assert text.endswith("这站要登录。")

    def test_结论内部句号不被截断(self):
        text = closing.closing_for_unit(
            title="功夫", unit="site-1",
            outcome={"site_url": "https://a.test", "branch": "blocked"},
            conclusion="先看了首页。再点了播放",
        )
        assert "先看了首页。再点了播放。" in text


class Test轨迹级反思:
    """P1 没有轨迹级 reflection——取「末个写成单元」的那份。

    收尾文本只关心「有没有可读的结论」：``unavailable`` / ``empty`` 与
    「压根没生成」走同一条路（退回纯 disposition 话术）。
    """

    def test_取末个写成单元(self):
        bodies = [
            {"reflection": {"status": "written",
                            "rationale": {"conclusion": "搜索页结论"}}},
            {"reflection": {"status": "written",
                            "rationale": {"conclusion": "播放页结论"}}},
        ]
        r = closing.last_written_reflection(bodies)
        assert closing.reflection_conclusion(r) == "播放页结论"

    def test_跳过未写成单元(self):
        bodies = [
            {"reflection": {"status": "written",
                            "rationale": {"conclusion": "前面的结论"}}},
            {"reflection": {"status": "unavailable", "rationale": None}},
        ]
        r = closing.last_written_reflection(bodies)
        assert closing.reflection_conclusion(r) == "前面的结论"

    def test_全没有返回None(self):
        assert closing.last_written_reflection([{}, {"reflection": None}]) is None

    def test_written但没rationale给空串(self):
        r = closing.last_written_reflection(
            [{"reflection": {"status": "written"}}]
        )
        assert closing.reflection_conclusion(r) == ""

    def test_结论去首尾空白(self):
        assert closing.reflection_conclusion(
            {"rationale": {"conclusion": "  央视网可播全集  "}}
        ) == "央视网可播全集"


class Test网址计数:
    @pytest.mark.parametrize("text, n", [
        ("", 0),
        ("没有网址", 0),
        ("https://a.test/x", 1),
        ("见 http://a.test 与 https://b.test/y", 2),
        ("同一句两个 https://a.test/x https://a.test/x", 2),
        ("带句号 https://a.test/x。结尾", 1),
    ])
    def test_count_urls(self, text, n):
        assert closing.count_urls(text) == n

    def test_has_url(self):
        assert closing.has_url("https://a.test") is True
        assert closing.has_url("a.test") is False


class Test两侧共用:
    def test_词表是同一个对象(self):
        """P3 与 T 的词表别名指向同一份 dict——改一处不会只改一侧。"""
        assert views._CLOSING_DISPOSITION is closing.CLOSING_DISPOSITION
        assert task_view._CLOSING_DISPOSITION is closing.CLOSING_DISPOSITION

    def test_deprecated分支仍读得懂(self):
        """退役的是「新存档不再产出」，不是「证据失忆」。"""
        assert "login_wall_or_blocked" in closing.CLOSING_DISPOSITION


class Test任务合同:
    def test_按task_id取min_urls(self):
        assert contracts.min_urls_of("T001") == 1
        assert contracts.min_urls_of("T019") == 2

    def test_合同没写min_urls就是不适用(self):
        assert contracts.min_urls_of("T025") is None

    def test_未知task不猜(self):
        assert contracts.min_urls_of("ZZZ999") is None

    def test_任务库缺失返回None(self, tmp_path):
        """读不到任务库 = 判据不适用，**不是**「不要求网址」。"""
        assert contracts._tasks_index(str(tmp_path / "nope.yaml")) is None

    def test_任务库坏了返回None(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("tasks: [unclosed", encoding="utf-8")
        assert contracts._tasks_index(str(bad)) is None

    def test_没有output_contract的任务返回None(self, tmp_path):
        p = tmp_path / "tasks.yaml"
        p.write_text(
            "tasks:\n  - task_id: X1\n    intent: {}\n"
            "  - task_id: X2\n    output_contract: {format: list}\n",
            encoding="utf-8",
        )
        index = contracts._tasks_index(str(p))
        assert index is not None
        assert index["X1"].get("output_contract") is None
        # 缓存按路径分，不影响真实任务库的读数
        assert contracts.min_urls_of("T001") == 1