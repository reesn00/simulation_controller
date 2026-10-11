"""候选钉窗（同源修复）单测——04 方案 §4.2 的五条用例。

背景铁证（05 文档 L95-121，2026-10-10 baidu 搜「功夫 在线观看」）：

- 全量 111 条 links，``extract_candidates`` 滤出 5 个候选；
- 感知层只看 ``_link_window = _ordered_links(...)[:LLM_LINKS]``（20 条）；
- 百度把导航排在前面，唯一在窗口内的候选是 hao123 的首页，四个真实
  候选（爱奇艺/西瓜/搜狐/优酷）index≥50，模型看不见；
- 模型只能选 ``baidu.com/s?wd=…`` 站内搜索链接，又被
  ``excluded_host:baidu.com`` 剔出候选 → 选中 ∩ 候选 = 空 → ``kept=0``，
  5 个候选全 unresolved。

修复（:func:`search.pin_candidates_first`）：把候选链接按 rank 钉到
links 最前，其余保原序。候选 text 已经 ``title_for`` 写回片名，落在
感知层 ``_ordered_links`` 的 hit 区最前——「候选 ⊆ 窗口」由构造保证。

校验（:func:`search.candidates_outside_window`）：executor 不 import
感知层实现（01-架构总览 L49-50），按同一套确定性规则重放排序，返回
被挤出窗口的候选。调用方记 warning，**不中断采集**。

依赖红线：executor 侧只能复制窗口宽度常量，由
``test_窗口常量与感知层同步`` 断言与 ``llm_perceptor.LLM_LINKS`` 相等，
防止两侧漂移。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.executor import orchestrator
from trajectory_pipeline.executor.steps import search
from trajectory_pipeline.perception import llm_perceptor as lp
from trajectory_pipeline.perception.base import LinkItem, Observation


def _obs(*links: LinkItem) -> Observation:
    return Observation(
        url="https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB",
        page_title="功夫_百度搜索",
        body_text="搜索结果页正文" * 20,
        links=tuple(links),
    )


def _cand(rank: int, href: str, text: str = "功夫 在线观看") -> search.Candidate:
    """构造候选：``source_href`` 指向 links 里的原始 href（判断点 ①
    回传的就是它，``extract_candidates`` 正是这样填的）。"""
    return search.Candidate(
        url=f"https://real{rank}.test/play",
        text=text,
        rank=rank,
        source_href=href,
    )


def _baidu_page(
    n_nav: int = 60, n_results: int = 5
) -> tuple[list[LinkItem], list[LinkItem]]:
    """复现铁证场景：导航在前、结果在后（results index≥50）。"""
    nav = [
        LinkItem(text=f"导航条{i}（百度首页）", href=f"https://nav{i}.test/")
        for i in range(n_nav)
    ]
    results = [
        LinkItem(text="《功夫》电影完整版-站点", href=f"https://result{i}.test/play")
        for i in range(n_results)
    ]
    return nav, results


class TestPinCandidatesFirst:
    """钉窗重排：候选按 rank 到最前、其余保原序。"""

    def test_铁证场景候选钉到最前(self):
        """111 条 links、5 个候选（index≥50）→ 重排后前 5 条就是候选，
        且按 rank 排序；其余链接保原序。"""
        nav, results = _baidu_page(n_nav=106, n_results=5)  # 111 条
        candidates = [_cand(r, results[r - 1].href) for r in range(1, 6)]
        out = search.pin_candidates_first(tuple(nav + results), candidates)
        pinned = list(out[:5])
        assert [l.href for l in pinned] == [
            f"https://result{i}.test/play" for i in range(5)
        ]
        # 其余保原序：导航相对顺序不变
        assert list(out[5 : 5 + 3]) == nav[:3]
        assert len(out) == 111

    def test_候选顺序按rank不按原始位置(self):
        """reverse 传入候选顺序也不能打乱 rank——钉窗以引擎相关性序为准。"""
        nav, results = _baidu_page(n_nav=10, n_results=3)
        candidates = [_cand(r, results[r - 1].href) for r in range(3, 0, -1)]
        out = search.pin_candidates_first(tuple(nav + results), candidates)
        assert [l.href for l in out[:3]] == [results[i].href for i in range(3)]

    def test_重复候选只钉一次(self):
        """同一 href 出现两条候选（不该发生，但防御）：只钉一次。"""
        nav, results = _baidu_page(n_nav=5, n_results=2)
        dup = [_cand(1, results[0].href), _cand(2, results[0].href)]
        out = search.pin_candidates_first(tuple(nav + results), dup)
        shown = [l.href for l in out if l.href == results[0].href]
        assert len(shown) == 1

    def test_空候选原样返回(self):
        nav, results = _baidu_page(n_nav=5, n_results=2)
        out = search.pin_candidates_first(tuple(nav + results), [])
        assert list(out) == nav + results

    def test_候选数等于窗口大小恰好装下(self):
        """候选数 = LLM_LINKS：全部钉进窗口，of窗口校验不报。"""
        window = lp.LLM_LINKS
        nav, results = _baidu_page(n_nav=5, n_results=window)
        candidates = [_cand(r, results[r - 1].href) for r in range(1, window + 1)]
        out = search.pin_candidates_first(tuple(nav + results), candidates)
        outside = search.candidates_outside_window(
            out, candidates, target_title="功夫", window_size=window
        )
        assert outside == ()
        # 且窗口内正好是全部候选
        assert [l.href for l in out[:window]] == [
            f"https://result{i}.test/play" for i in range(window)
        ]


class TestCandidatesOutsideWindow:
    """「窗口 ⊇ 候选」重放校验。"""

    def test_铁证场景钉窗后无出窗候选(self):
        """修复后的闭环：重放感知层排序，5 个候选都在窗口内 → 空。"""
        nav, results = _baidu_page(n_nav=106, n_results=5)
        candidates = [_cand(r, results[r - 1].href) for r in range(1, 6)]
        links = search.pin_candidates_first(tuple(nav + results), candidates)
        outside = search.candidates_outside_window(
            links, candidates, target_title="功夫", window_size=lp.LLM_LINKS
        )
        assert outside == ()

    def test_候选对应链接text无片名_撞上满窗片名链接被挤出(self):
        """候选链接的 text 没有片名（``title_for`` 没取到标题，text 还是
        面包屑），而窗口里有 ≥ LLM_LINKS 条**其他**含片名的链接占满 hit
        区 → 候选整批落 miss 区被挤出。这是「钉了也白钉」最隐蔽的形态，
        fail-closed 校验的意义就是让它显形。

        注意窗口按 **link.text** 划分（感知层读的是 ``obs.links``），
        不是 ``candidate.text``——所以必须让**候选对应的链接** text 无片名，
        而不是候选对象自身的 text。
        """
        # 前 20 条链接 text 含片名（模拟其他结果标题，占满 hit 区）
        full = [
            LinkItem(text="《功夫》其他结果站", href=f"https://hit{i}.test/")
            for i in range(20)
        ]
        # 候选对应链接 text 是面包屑，不含片名
        results = [
            LinkItem(text="某视频站 › 播放", href=f"https://result{i}.test/play")
            for i in range(3)
        ]
        candidates = [_cand(r, results[r - 1].href) for r in range(1, 4)]
        links = search.pin_candidates_first(tuple(full + results), candidates)
        outside = search.candidates_outside_window(
            links, candidates, target_title="功夫", window_size=lp.LLM_LINKS
        )
        assert [l.href for l in outside] == [
            f"https://result{i}.test/play" for i in range(3)
        ]

    def test_target_title为空_钉窗后候选仍在窗口内(self):
        """感知层对空片名不排序、直接取前 window_size 条；候选已被
        钉到 links 最前 → 构造上仍在窗口内，本校验不出窗。
        （空片名由感知层 _title_link_sufficiency 负责 fail-closed，
        那是另一道闸，不在这。）"""
        nav, results = _baidu_page(n_nav=106, n_results=3)
        candidates = [_cand(r, results[r - 1].href) for r in range(1, 4)]
        links = search.pin_candidates_first(tuple(nav + results), candidates)
        outside = search.candidates_outside_window(
            links, candidates, target_title="", window_size=lp.LLM_LINKS
        )
        assert outside == ()

    def test_候选数超过窗口大小超窗告警(self):
        """max_candidates > LLM_LINKS：窗口装不下，超出部分必出窗。
        这正是 04 方案 §2.4-3 要求常量保持 ≤ 关系的告警面。"""
        window = lp.LLM_LINKS
        nav, results = _baidu_page(n_nav=5, n_results=window + 5)
        candidates = [_cand(r, results[r - 1].href) for r in range(1, window + 6)]
        links = search.pin_candidates_first(tuple(nav + results), candidates)
        outside = search.candidates_outside_window(
            links, candidates, target_title="功夫", window_size=window
        )
        assert len(outside) == 5  # 后 5 个候选被挤出
        assert [l.href for l in outside] == [
            f"https://result{i}.test/play" for i in range(window, window + 5)
        ]

    def test_匹配用原样title与感知层一致(self):
        """重放必须与 _ordered_links 逐字节一致：target_title 带首尾空白
        时仍按原样匹配（感知层 `.strip()` 只用于判空）。"""
        nav, results = _baidu_page(n_nav=5, n_results=2)
        candidates = [_cand(r, results[r - 1].href) for r in range(1, 3)]
        links = search.pin_candidates_first(tuple(nav + results), candidates)
        outside = search.candidates_outside_window(
            links, candidates, target_title="  功夫  ", window_size=lp.LLM_LINKS
        )
        assert outside == ()


class Test窗口常量与感知层同步:
    """executor 侧复制了窗口宽度常量——不得与感知层漂移。

    漂移 = 钉窗校验按错的宽度算，钉了也白钉且校验还报绿。
    """

    def test_窗口常量与感知层LLM_LINKS一致(self):
        assert orchestrator.PERCEPTION_LINK_WINDOW == lp.LLM_LINKS

    def test_窗口常量为正整数(self):
        assert isinstance(orchestrator.PERCEPTION_LINK_WINDOW, int)
        assert orchestrator.PERCEPTION_LINK_WINDOW > 0
