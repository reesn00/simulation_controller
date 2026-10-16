"""``executor/steps/link_check.py`` 单测——外链校验 + 跳转确认页检查。

**不碰真浏览器**：值得测的是分类判据（词表 + 长度双门槛、URL 正则、
自域剔除）与「开 tab → 观察 → 关 tab」的动作流记账，fake driver
把这些测得又快又全。确认页判定是纯函数，直接喂文本。
"""

from __future__ import annotations

from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.executor.steps import link_check
from trajectory_pipeline.perception.base import Observation


# ═══════════════════════════════════════════════════════════════════════
# Fake
# ═══════════════════════════════════════════════════════════════════════


class FakeDriver:
    """new_tab 会切换 current 的 driver——外链校验唯一的驱动面。"""

    name = "fake"

    def __init__(
        self,
        pages: dict[str, Observation] | None = None,
        *,
        new_tab_fails: bool = False,
    ) -> None:
        self.pages = pages or {}
        self.opened: list[str | None] = []
        self.closed = 0
        self.new_tab_fails = new_tab_fails
        self.current: Observation | None = None

    async def new_tab(self, url: str | None = None) -> str:
        self.opened.append(url)
        if self.new_tab_fails:
            raise DriverError("fake: 开不了 tab")
        if url in self.pages:
            self.current = self.pages[url]
        return f"tab{len(self.opened)}"

    async def close_tab(self, tab_id: str | None = None) -> None:
        self.closed += 1

    async def observe(self, *, max_chars=None) -> Observation:
        if self.current is None:
            raise DriverError("fake: 没有 current")
        return self.current


def obs_at(url: str, *, body: str = "正文", title: str = "T") -> Observation:
    return Observation(url=url, page_title=title, body_text=body)


# ═══════════════════════════════════════════════════════════════════════
# 确认页判定（纯函数）
# ═══════════════════════════════════════════════════════════════════════


class TestIsConfirmPage:
    def test_词表命中且正文短(self):
        assert link_check.is_confirm_page("即将跳转到外部站点，请确认") is True

    def test_词表不命中(self):
        assert link_check.is_confirm_page("一部很好看的电影，点击观看") is False

    def test_超长正文命中词表也不算确认页(self):
        """INC43 教训的词表通用坑：长正文里出现「点击进入」多半是
        内容的一部分（error.html 页脚命中付费墙词表同款形态），
        双门槛的长度那一半就是防它。"""
        long_body = "影评正文。" * 400 + "点击进入下一页"
        assert len(long_body) > link_check.CONFIRM_PAGE_MAX_CHARS
        assert link_check.is_confirm_page(long_body) is False

    def test_空正文(self):
        assert link_check.is_confirm_page("") is False


class TestExtractRedirectTarget:
    def test_取第一个合法_url(self):
        body = "即将跳转，请稍候 https://v.example.com/play/1?x=2 谢谢"
        got = link_check._extract_redirect_target(
            body, base_host="hub.test", engine="baidu"
        )
        assert got == "https://v.example.com/play/1?x=2"

    def test_尾部标点剥离(self):
        body = "前往站点：https://v.example.com/p/1。"
        got = link_check._extract_redirect_target(
            body, base_host="hub.test", engine="baidu"
        )
        assert got == "https://v.example.com/p/1"

    def test_自域被跳过_取下一个(self):
        """确认页上的「返回本站」链接不是跳转目标。"""
        body = "即将跳转 https://hub.test/home 或 https://v.example.com/p/1"
        got = link_check._extract_redirect_target(
            body, base_host="hub.test", engine="baidu"
        )
        assert got == "https://v.example.com/p/1"

    def _engine_host_reason(self, url: str) -> str:
        # baidu 引擎域应被 exclusion_reason 剔除
        return link_check._drop_reason(url, "hub.test", "baidu")

    def test_引擎域目标被剔除(self):
        assert self._engine_host_reason("https://www.baidu.com/x") != ""

    def test_提不出目标返回空串(self):
        assert (
            link_check._extract_redirect_target(
                "即将跳转，但没有地址", base_host="hub.test", engine="baidu"
            )
            == ""
        )


# ═══════════════════════════════════════════════════════════════════════
# resolve_referral 控制流
# ═══════════════════════════════════════════════════════════════════════


class TestResolveReferral:
    async def test_直接落地_direct(self):
        href = "https://v.example.com/movie"
        d = FakeDriver({href: obs_at(href)})
        steplog = StepLog()
        stats: dict[str, int] = {}
        r = await link_check.resolve_referral(
            d, steplog, href, base_host="hub.test", stats=stats
        )
        assert r.status == link_check.DIRECT
        assert r.url == href
        assert r.ok
        assert stats == {}
        assert d.closed == 1  # 校验完必须关 tab

    async def test_重定向落地_redirected(self):
        href = "https://go.example.com/r/123"
        landed = "https://v.example.com/play/123"
        d = FakeDriver({href: obs_at(landed)})
        r = await link_check.resolve_referral(
            d, StepLog(), href, base_host="go.example.com"
        )
        assert r.status == link_check.REDIRECTED
        assert r.url == landed
        assert r.source_href == href
        assert "重定向" in r.note

    async def test_确认页提取目标(self):
        href = "https://hub.test/jump?to=1"
        body = "即将跳转至 https://v.example.com/p/1，请知悉"
        d = FakeDriver({href: obs_at(href, body=body)})
        r = await link_check.resolve_referral(
            d, StepLog(), href, base_host="hub.test"
        )
        assert r.status == link_check.CONFIRM_PAGE
        assert r.url == "https://v.example.com/p/1"

    async def test_确认页提不出目标_dropped且不计负分支(self):
        """词表命中的页不一定是真确认页——提不出目标就丢弃，
        只进 stats，不碰 RunLedger（外链不是站点结论）。"""
        href = "https://hub.test/jump?to=1"
        d = FakeDriver({href: obs_at(href, body="即将跳转，但没有地址")})
        stats: dict[str, int] = {}
        r = await link_check.resolve_referral(
            d, StepLog(), href, base_host="hub.test", stats=stats
        )
        assert r.status == link_check.DROPPED
        assert not r.ok
        assert stats.get("confirm_page_no_target") == 1

    async def test_落地回到中间页自域_dropped(self):
        href = "https://hub.test/jump?to=1"
        d = FakeDriver({href: obs_at("https://hub.test/category")})
        r = await link_check.resolve_referral(
            d, StepLog(), href, base_host="hub.test"
        )
        assert r.status == link_check.DROPPED
        assert "same_host" in r.note

    async def test_开tab失败_dropped且动作流记错(self):
        d = FakeDriver(new_tab_fails=True)
        steplog = StepLog()
        r = await link_check.resolve_referral(
            d, steplog, "https://v.example.com/x", base_host="hub.test"
        )
        assert r.status == link_check.DROPPED
        assert "开 tab 失败" in r.note
        assert steplog.steps[0].error  # 「试过且失败」留在动作流里

    async def test_动作流记录开与关(self):
        href = "https://v.example.com/movie"
        d = FakeDriver({href: obs_at(href)})
        steplog = StepLog()
        await link_check.resolve_referral(
            d, steplog, href, base_host="hub.test"
        )
        tools = [s.action.tool for s in steplog.steps]
        assert tools == ["new_tab", "close_tab"]
        # 开 tab 的观察要回填——「点了之后看到了什么」是复核依据
        assert steplog.steps[0].observation is not None
        # 关 tab 失败不影响解析结果，这里成功路径 error 为空
        assert steplog.steps[1].error == ""
