"""提取层候选质量修复单测——05 方案 §4.2 的用例清单。

背景铁证（05 文档 §1，2026-10-10 TPIN01 存档）：

- 百度真实搜索结果 100% 是 ``//www.baidu.com/link?url=<密文>`` 包裹——
  url= 是服务端签名参数，**解不开**（不是 URL 编码）；
- 旧逻辑里 ``exclusion_reason`` 的 ``engine_self``（host == www.baidu.com）
  先于 ``redirect_wrapper`` 判定，把 91 条真实结果全吞掉（engine_self=91 +
  excluded_host:baidu.com=17，108/113 被剔）；
- 幸存 5 个候选全是 text 无片名的聚合卡片裸链 → 钉窗钉进的是错候选 →
  kept=0 → D-2 100% 不可判定。

修复三件事（05 方案 §2）：

1. ``_is_result_redirect`` / ``_is_undecodable_redirect``：把「引擎中转
   结果」从「引擎自身功能页」判定里拆出来（纯路由结构判断，不解密）；
2. ``exclusion_reason``：中转路由在 host 类判定**之前**分流——明文 → 维持
   ``redirect_wrapper`` 原口径；密文（url= 值不是明文 http）→ 放行；
3. ``extract_candidates``：密文包裹的去重键降级为「不去重」（按原始链接
   各自保留，同批真实结果不互相撞车），``Candidate.host`` 对密文形态
   返回空串（不伪装成 baidu.com）。

不回归防线：``/s?wd=`` / ``/gaoji/`` / 首页仍按 ``engine_self`` 剔、
明文包裹仍 ``redirect_wrapper``、裸链仍按 host 去重、页面原始顺序不重排。
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from trajectory_pipeline.executor.steps import search
from trajectory_pipeline.executor.steps.search import _is_undecodable_redirect
from trajectory_pipeline.executor.steps.search import exclusion_reason
from trajectory_pipeline.perception.base import LinkItem, Observation

#: 模拟百度密文包裹（真实值是 32 位 + 签名后缀的服务端参数；测试用占位）
OPAQUE = "http://www.baidu.com/link?url=7kZx3FqR9jL2mXy4pQw6NcVb1HtGf8s"
#: 明文包裹（url= 值以 http(s) 开头，可解）
PLAIN = "http://www.baidu.com/link?url=https%3A%2F%2Fwww.iqiyi.com%2Fa%2Fb.html"


def _obs(*links: LinkItem) -> Observation:
    return Observation(
        url="https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB",
        page_title="功夫_百度搜索",
        body_text="搜索结果页正文" * 20,
        links=tuple(links),
    )


class TestRedirectRouteRecognition:
    """中转路由识别：纯结构判断，不解密，逐引擎路由表驱动。"""

    def test_密文包裹命中百度中转路由(self):
        assert search._is_result_redirect(urlparse(OPAQUE), "baidu") is True

    def test_明文包裹同样命中中转路由(self):
        # 路由判定只看 path + url 键存在性，不关心值长什么样
        assert search._is_result_redirect(urlparse(PLAIN), "baidu") is True

    def test_站内搜索页不是中转路由(self):
        assert (
            search._is_result_redirect(
                urlparse("https://www.baidu.com/s?wd=功夫"), "baidu"
            )
            is False
        )

    def test_link路径但无url键不是中转路由(self):
        assert (
            search._is_result_redirect(urlparse("http://www.baidu.com/link"), "baidu")
            is False
        )

    def test_密文包裹判定为不可解(self):
        assert _is_undecodable_redirect(OPAQUE, "baidu") is True

    def test_明文包裹判定为可解(self):
        assert _is_undecodable_redirect(PLAIN, "baidu") is False

    def test_普通外链不是密文中转(self):
        assert (
            _is_undecodable_redirect("https://www.iqiyi.com/a/b.html", "baidu") is False
        )

    def test_bing路由表未启用_不回归(self):
        # 05 方案 §6-1：bing 的 /ck/a?…&u= 属后续独立一轮
        assert "bing" not in search.RESULT_REDIRECT_ROUTES
        assert (
            _is_undecodable_redirect(
                "http://www.bing.com/ck/a?u=aHR0cHM6Ly93d3cu", "bing"
            )
            is False
        )


class TestExclusionReason:
    """剔除判定：中转路由在 host 类判定之前分流。"""

    def test_密文包裹放行(self):
        assert exclusion_reason(OPAQUE, "baidu") == ""

    def test_密文包裹不再被engine_self误杀(self):
        # 修复前的行为：先 engine_self 后 redirect_wrapper，密文死于 engine_self
        reason = exclusion_reason(OPAQUE, "baidu")
        assert reason != "engine_self"
        assert reason != "redirect_wrapper"

    def test_明文包裹仍redirect_wrapper(self):
        assert exclusion_reason(PLAIN, "baidu") == "redirect_wrapper"

    def test_站内功能页仍engine_self(self):
        for nav in (
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB",
            "https://www.baidu.com/gaoji/",
            "https://www.baidu.com/",
        ):
            assert exclusion_reason(nav, "baidu") == "engine_self"

    def test_去重域族等既有剔除不回归(self):
        assert exclusion_reason("https://www.bing.com/search?q=x", "baidu") == (
            "excluded_host:bing.com"
        )
        # 方案 12 阶段 A1（2026-10-16）：zhihu 自 EXCLUDED_HOST_ROOTS 移出，
        # 不再有排除理由——它是路径 C 的语义信源，去向交给分类任务
        # （Q.CLASSIFY_SEARCH_LINKS）判 article_post，不再结构剔除。
        assert exclusion_reason("https://www.zhihu.com/question/1", "baidu") == ""


class TestExtractCandidatesDedup:
    """去重键降级：密文包裹不去重（互不撞车）、明文/裸链维持 host 去重。"""

    def test_多条密文包裹互不撞去重(self):
        links = [
            LinkItem(text=f"《功夫》完整版-站点{i}", href=f"{OPAQUE}{i}")
            for i in range(5)
        ]
        stats: dict[str, int] = {}
        out = search.extract_candidates(_obs(*links), engine="baidu", stats=stats)
        assert len(out) == 5  # 修复前 host 去重会只剩 1 条
        assert stats.get("duplicate_host") is None
        assert [c.rank for c in out] == [1, 2, 3, 4, 5]  # 原始顺序保持

    def test_候选text直接用link_text含片名(self):
        links = [
            LinkItem(text="《功夫》电影完整版", href=f"{OPAQUE}{i}") for i in range(2)
        ]
        out = search.extract_candidates(_obs(*links), engine="baidu")
        assert all("功夫" in c.text for c in out)

    def test_混合输入_密文与裸链都进_同host裸链只留首条(self):
        links = (
            LinkItem(text="《功夫》完整版-站A", href=f"{OPAQUE}1"),
            LinkItem(text="站A 另一个入口", href="https://www.iqiyi.com/entry2.html"),
            LinkItem(text="站A 又一条", href="https://www.iqiyi.com/entry3.html"),
            LinkItem(
                text="站内搜索", href="https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB"
            ),
        )
        stats: dict[str, int] = {}
        out = search.extract_candidates(_obs(*links), engine="baidu", stats=stats)
        assert len(out) == 2  # 1 密文 + 1 首条 iqiyi
        assert out[0].text == "《功夫》完整版-站A"
        assert stats.get("duplicate_host") == 1
        assert stats.get("engine_self") == 1

    def test_明文包裹解包后以直链进候选(self):
        # extract_candidates 先 _unwrap_redirect：明文包裹被解包成真实站直链
        links = (LinkItem(text="《功夫》-腾讯视频", href=PLAIN),)
        out = search.extract_candidates(_obs(*links), engine="baidu")
        assert len(out) == 1
        assert out[0].url.startswith("https://www.iqiyi.com/")
        assert out[0].opaque_redirect is False


class TestCandidateHostOpaque:
    """Candidate.host：密文中转形态返回空串（未知），不伪装成 baidu.com。"""

    def test_密文候选host为空串(self):
        c = search.Candidate(
            url=OPAQUE,
            text="《功夫》完整版",
            rank=1,
            source_href=OPAQUE,
            opaque_redirect=True,
        )
        assert c.host == ""

    def test_裸链候选host为真实域名(self):
        c = search.Candidate(
            url="https://www.iqiyi.com/a.html",
            text="爱奇艺",
            rank=1,
            source_href="https://x.test/",
        )
        assert c.host == "www.iqiyi.com"

    def test_默认非密文时host正常(self):
        c = search.Candidate(
            url="https://v.qq.com/x/1.html",
            text="腾讯视频",
            rank=1,
            source_href="https://v.qq.com/x/1.html",
        )
        assert c.opaque_redirect is False
        assert c.host == "v.qq.com"
