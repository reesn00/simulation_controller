"""``executor.steps.search`` 单测。

重点是**结构性剔除规则**与**百度中转链接解包**——
后者不实现就完全拿不到候选（W1 无法跑）。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.executor.steps import search
from trajectory_pipeline.perception.base import LinkItem, Observation


def obs_with(*links: tuple[str, str], degraded=()) -> Observation:
    return Observation(
        url="https://www.baidu.com/s?wd=x", page_title="搜索", body_text="结果",
        links=tuple(LinkItem(text=t, href=h) for t, h in links),
        degraded=tuple(degraded),
    )


class TestQuery:
    def test_默认意图(self):
        assert search.build_query("功夫") == "功夫 在线观看"

    @pytest.mark.parametrize("intent,expect", [
        ("watch", "功夫 在线观看"),
        ("play", "功夫 在线播放"),
        ("raw", "功夫"),
    ])
    def test_意图词表(self, intent, expect):
        assert search.build_query("功夫", intent=intent) == expect

    def test_未知意图抛错(self):
        """静默 fallback 到默认意图，会让 persona 维度的效果无从验证。"""
        with pytest.raises(ValueError):
            search.build_query("功夫", intent="unknown")

    def test_persona_参数先留着(self):
        """接口先留出 persona——模块 1 若接不上采集侧就成摆设。"""
        assert search.build_query("功夫", persona=None) == "功夫 在线观看"
        assert search.build_query("功夫", persona=object()) == "功夫 在线观看"


class TestSearchUrl:
    def test_空格转义为加号(self):
        url = search.search_url("功夫 在线观看")
        assert "%E5%8A%9F%E5%A4%AB" in url
        assert "+" in url

    def test_特殊字符被转义(self):
        """不转义的话 `&`、`#` 会把查询串截断或注入额外参数。"""
        url = search.search_url("功夫&watch#1")
        assert "&watch" not in url.replace("%26watch", "")

    def test_未知引擎抛错(self):
        with pytest.raises(ValueError):
            search.search_url("x", engine="google-cn")

    def test_三个引擎都有模板(self):
        for e in ("baidu", "bing", "duckduckgo"):
            assert search.search_url("x", e).startswith("https://")


class TestCandidates:
    def test_保留页面顺序(self):
        """搜索结果本就按相关性排过——重新排序等于用自己的启发式
        覆盖引擎的判断，那是没有依据的。"""
        obs = obs_with(
            ("第三条", "https://c.test/1"),
            ("第一条", "https://a.test/1"),
            ("第二条", "https://b.test/1"),
        )
        cands = search.extract_candidates(obs)
        assert [c.url for c in cands] == [
            "https://c.test/1", "https://a.test/1", "https://b.test/1"]
        assert [c.rank for c in cands] == [1, 2, 3]

    def test_按_host_去重(self):
        """同站多入口不重复遍历——否则遍历阶段会在同一站反复卡住。"""
        obs = obs_with(
            ("首页", "https://a.test/"),
            ("剧集页", "https://a.test/list/1"),
            ("别的站", "https://b.test/"),
        )
        cands = search.extract_candidates(obs)
        assert [c.host for c in cands] == ["a.test", "b.test"]

    @pytest.mark.parametrize("url", [
        "https://www.zhihu.com/q/1",
        "https://weibo.com/x",
        "https://www.google.com/analytics",
        "https://fonts.googleapis.com/css2",
    ])
    def test_结构性剔除(self, url):
        assert search.extract_candidates(obs_with(("x", url))) == []

    @pytest.mark.parametrize("url", [
        "https://static.example.com/",
        "https://cdn.example.com/",
    ])
    def test_子域前缀_不误杀(self, url):
        """`cdn.` / `static.` **不能**当排除项：搜索结果页给的是页面链接，
        资源 CDN 几乎不出现；而前缀匹配会误杀本身就是内容站的域名。"""
        assert search.extract_candidates(obs_with(("x", url))) != []

    def test_剔除搜索引擎_自身(self):
        obs = obs_with(
            ("站内页", "https://www.baidu.com/baidu"),
            ("真站点", "https://real.test/m"),
        )
        cands = search.extract_candidates(obs, engine="baidu")
        assert [c.url for c in cands] == ["https://real.test/m"]

    @pytest.mark.parametrize("url", [
        "javascript:void(0)",
        "mailto:a@b.test",
        "tel:123",
    ])
    def test_非_http_剔除(self, url):
        assert search.extract_candidates(obs_with(("x", url))) == []

    def test_剔除功能页(self):
        obs = obs_with(
            ("登录", "https://a.test/login"),
            ("帮助", "https://a.test/help"),
            ("首页", "https://a.test/index"),
        )
        assert [c.host for c in search.extract_candidates(obs)] == ["a.test"]

    def test_新闻类域名不剔除(self):
        """`news.` 前缀不能当新闻站判——`newsletter.com`、国内带 news 的
        视频站域名都不少见。代码只做**结构性**剔除。"""
        obs = obs_with(("新闻", "https://news.b.test/x"))
        assert [c.url for c in search.extract_candidates(obs)] == ["https://news.b.test/x"]

    def test_limit_生效(self):
        obs = obs_with(*[(f"s{i}", f"https://s{i}.test/") for i in range(10)])
        assert len(search.extract_candidates(obs, limit=3)) == 3

    def test_采集降级返回空(self):
        """降级 → 空候选。调用方据此记 unresolved，不把「没采到」
        当成「没有可看的站」。"""
        obs = obs_with(("x", "https://a.test/"), degraded=("browser_links",))
        assert search.extract_candidates(obs) == []

    def test_空结果返回空(self):
        assert search.extract_candidates(obs_with()) == []


class TestBaiduRedirect:
    def test_解包_link_query(self):
        """百度结果页大量用 ``/link?url=`` 包裹真实地址。
        **不剥就完全拿不到候选**，而剥是纯字符串事实，不涉及语义。"""
        obs = obs_with(
            ("电影", "https://www.baidu.com/link?url=https%3A%2F%2Freal.test%2Fmovie"),
        )
        cands = search.extract_candidates(obs)
        assert [c.url for c in cands] == ["https://real.test/movie"]

    def test_解包_ck_redirect(self):
        raw = "https://www.baidu.com/ck/a?!-=&p=1&url=https%3A%2F%2Freal.test%2Fx"
        assert search._unwrap_redirect(raw) == "https://real.test/x"

    def test_非中转链接原样返回(self):
        assert search._unwrap_redirect("https://a.test/x") == "https://a.test/x"

    def test_中转到搜索引擎自身仍被剔除(self):
        """解包后再判排除——顺序反了就等于给 SEO 跳转开后门。"""
        obs = obs_with(
            ("百度", "https://www.baidu.com/link?url=https%3A%2F%2Fwww.baidu.com%2Fx"),
        )
        assert search.extract_candidates(obs) == []


class TestCandidate:
    def test_host_解析(self):
        c = search.Candidate(url="https://www.a.test/movie?q=1", text="t", rank=1)
        assert c.host == "www.a.test"

    def test_畸形_url_不抛(self):
        assert search.Candidate(url="ht!tp://%%%", text="t", rank=1).host in ("", ) or True


class TestHostFamilyMatching:
    """注册域族匹配。**必须带点边界**，否则会误杀真站点。

    实测 bing 结果里的 ``help.bing.microsoft.com`` 曾漏网进了候选，
    最后被判成 ``no_play_control`` 进了负样本池——一个帮助中心页
    成为「这站没有播放控件」的证据，是纯粹的污染。
    """

    @pytest.mark.parametrize("host", [
        "help.bing.microsoft.com", "bing.com", "www.bing.com",
        "go.microsoft.com", "api.bing.com", "beian.miit.gov.cn",
        "www.gov.cn", "baidu.com", "img.baidu.com",
    ])
    def test_命中族内(self, host):
        assert search._host_in(host, search.EXCLUDED_HOST_ROOTS) or \
               search._host_in(host, search.IMPOSSIBLE_CONTENT_ROOTS), host

    @pytest.mark.parametrize("host", [
        "notbing.com",            # 无点边界会被 bing.com 误杀
        "mybaidu.com",
        "microsoftonline.evil.cn",  # microsoft.com 不是它的后缀
        "qq.com", "youku.com", "iqiyi.com", "sohu.com", "bilibili.com",
    ])
    def test_不误杀真站点(self, host):
        assert not search._host_in(host, search.EXCLUDED_HOST_ROOTS)
        assert not search._host_in(host, search.IMPOSSIBLE_CONTENT_ROOTS)

    def test_edu_cn_不连带动(self):
        """部分高校图书馆确有影视资料页，误杀是**可避免**的，故不设。"""
        assert not search._host_in("lib.sjtu.edu.cn", search.IMPOSSIBLE_CONTENT_ROOTS)

    def test_实测漏网样本(self):
        """这四条是真实 bing 结果里混进来的政府页。"""
        for url in ("https://help.bing.microsoft.com/#apex/18/en-US",
                    "https://beian.miit.gov.cn/",
                    "https://beian.mps.gov.cn/#/query/webSearch",
                    "https://dxzhgl.miit.gov.cn/dxxzsp/xkz/xkzgl/resource"):
            assert search.exclusion_reason(url, "bing"), url

    def test_真播放站不排除(self):
        for url in ("https://v.qq.com/x/cover/abc",
                    "https://www.iqiyi.com/v_19rrk.html",
                    "https://tv.sohu.com/v/MjAx.html",
                    "https://www.bilibili.com/bangumi/play/ep313572",
                    "https://www.mgtvtv.com/tv/32080/",
                    "https://m.ixigua.com/video/658208"):
            assert search.exclusion_reason(url, "bing") == "", url


class TestFilterAccounting:
    """剔除必须留痕——「引擎给了 50 个链接、我们只跑了 11 个」不能静默。"""

    def test_记每种剔除原因(self):
        obs = obs_with(
            ("百度", "https://www.baidu.com/s?wd=x"),
            ("备案", "https://beian.miit.gov.cn/"),
            ("腾讯", "https://v.qq.com/x/cover/abc"),
            ("社交", "https://weibo.com/x"),
        )
        stats: dict[str, int] = {}
        cands = search.extract_candidates(obs, engine="bing", stats=stats)
        assert [c.host for c in cands] == ["v.qq.com"]
        assert stats["excluded_host:baidu.com"] == 1
        assert stats["impossible_content:gov.cn"] == 1
        assert stats["excluded_host:weibo.com"] == 1

    def test_引擎自身单独记账(self):
        """``engine_self`` 与 ``excluded_host:<root>`` 分开：
        前者是"引擎自家页"，后者是"命中排除族"，读的人处置不同。"""
        obs = obs_with(("结果", "https://www.bing.com/search?q=x"),
                       ("腾讯", "https://v.qq.com/x/1"))
        stats: dict[str, int] = {}
        search.extract_candidates(obs, engine="bing", stats=stats)
        assert stats["engine_self"] == 1

    def test_去重也记账(self):
        obs = obs_with(("a", "https://a.test/x"), ("b", "https://a.test/y"))
        stats: dict[str, int] = {}
        assert len(search.extract_candidates(obs, stats=stats)) == 1
        assert stats["duplicate_host"] == 1

    def test_子域不同不算重复(self):
        """``a.test`` 与 ``www.a.test`` 是不同 host——别误去重。"""
        obs = obs_with(("a", "https://a.test/x"), ("b", "https://www.a.test/y"))
        assert len(search.extract_candidates(obs)) == 2

    def test_超限记账(self):
        obs = obs_with(("a", "https://a.test/x"), ("b", "https://b.test/x"))
        stats: dict[str, int] = {}
        search.extract_candidates(obs, limit=1, stats=stats)
        assert stats["over_limit"] == 1

    def test_降级时也记账(self):
        stats: dict[str, int] = {}
        assert search.extract_candidates(obs_with(degraded=("links",)), stats=stats) == []
        assert stats == {"degraded": 1}

    def test_不给stats不报错(self):
        obs = obs_with(("a", "https://a.test/x"))
        assert len(search.extract_candidates(obs)) == 1