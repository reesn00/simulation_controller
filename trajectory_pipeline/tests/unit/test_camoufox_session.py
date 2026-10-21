"""camoufox 会话层的**口径**测试——**不联网、不开浏览器**。

三件事，每一件都是「改错了不报错、但批次不可归因」的形状：

1. **无人值守默认无头**（2026-10-21 口径反转）。headful 窗口不可渲染时
   （最小化 / 息屏 / 锁屏 / 遮挡）Firefox 会降频渲染与输入派发，
   页内 ``requestAnimationFrame`` 滞后从 4–17ms 涨到 902ms–62s，
   单次点击随之从 0.4–1.5s 涨到 **5–13s**——超出动作预算，
   于是「点击没做成」被下游读成「页面没有播放器」（理由通顺的假负样本）。
   证据在 ``research/probes/probe_click_*.py`` 与同目录的 ``click_*.json``。
2. **``headless`` 必须进批次元数据**。它是请求值（读不回来），
   但它决定了「这批为什么快」——不记就解释不了跨批次的分布跳变，
   而分布跳变**会被读成质量变化**。
3. **launch 的失败不许泄漏进程**。半途失败必须把 browser/playwright 一起收掉。
"""

from __future__ import annotations

from typing import Any

import pytest

from trajectory_pipeline.executor.browser import camoufox_session as mod
from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession


class _FakePage:
    def __init__(self) -> None:
        self.goto_calls: list[str] = []
        self.evaluate_calls: list[str] = []

    async def goto(self, url: str, **kw: Any) -> None:
        self.goto_calls.append(url)

    async def evaluate(self, expression: str) -> dict[str, Any]:
        self.evaluate_calls.append(expression)
        return {
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0",
            "oscpu": "Windows NT 10.0; Win64; x64",
            "platform": "Win32",
            "webdriver": False,
            "languages": ["zh-CN", "zh"],
            "cores": 8,
            "intl": "zh-CN",
            "timezone": "Asia/Shanghai",
            "screen": [1920, 1080],
            "window": [1905, 940],
            "dpr": 1.0,
        }


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page


class _FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[_FakeContext] = []

    async def new_context(self) -> _FakeContext:
        ctx = _FakeContext()
        self.contexts.append(ctx)
        return ctx


class _FakeManager:
    """记录 ``AsyncCamoufox`` 收到的参数，并按 launch 的流程把假对象递上去。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.browser = _FakeBrowser()
        self.exited = False

    async def __aenter__(self) -> _FakeBrowser:
        return self.browser

    async def __aexit__(self, *exc: Any) -> None:
        self.exited = True


@pytest.fixture()
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> list[_FakeManager]:
    """把 ``AsyncCamoufox`` 换成假实现，返回收参数用的列表。"""
    seen: list[_FakeManager] = []

    def _factory(**kwargs: Any) -> _FakeManager:
        manager = _FakeManager(**kwargs)
        seen.append(manager)
        return manager

    monkeypatch.setattr(mod, "AsyncCamoufox", _factory)
    return seen


class TestHeadless口径:
    async def test_无人值守默认无头(self, fake_manager: list[_FakeManager]) -> None:
        """缺省必须是无头。headful 是「人守着的桌面」那条**显式**的路。

        这条断言盯的是**默认值**——口径反转时它就该红。
        """
        session = await CamoufoxSession.launch()
        assert fake_manager[0].kwargs["headless"] is True
        assert session.fingerprint["headless"] is True
        await session.close()

    async def test_显式要_headful_也能拿到(self, fake_manager: list[_FakeManager]) -> None:
        """``headless=False`` 是「人守着的桌面」那条路，必须仍然可用。"""
        session = await CamoufoxSession.launch(headless=False)
        assert fake_manager[0].kwargs["headless"] is False
        assert session.fingerprint["headless"] is False
        await session.close()

    async def test_其余指纹维度不因无头而变(self, fake_manager: list[_FakeManager]) -> None:
        """口径反转只动「有没有可见窗口」这一维。

        其余维度（os / screen / window / locale / humanize / geoip /
        ``main_world_eval``）照旧钉死——它们各自都有理由，
        不能顺手跟着改（改了就是另一件事，得另开一次决策）。
        """
        await CamoufoxSession.launch()
        kwargs = fake_manager[0].kwargs
        assert kwargs["os"] == mod.DEFAULT_OS
        assert kwargs["window"] == mod.DEFAULT_WINDOW
        assert kwargs["locale"] == mod.DEFAULT_LOCALE
        assert kwargs["humanize"] is True
        assert kwargs["geoip"] is False
        assert kwargs["main_world_eval"] is False

    async def test_指纹读数是实测值不是请求值(self, fake_manager: list[_FakeManager]) -> None:
        """``screen`` / ``window`` / ``dpr`` / ``timezone`` 是浏览器自己报的。

        请求值钉不住实测——§4.3.1 实测内高在 940/948 间摆动，
        所以批次元数据只信实测值。
        """
        session = await CamoufoxSession.launch()
        fp = session.fingerprint
        assert fp["screen"] == [1920, 1080]
        assert fp["window"] == [1905, 940]
        assert fp["dpr"] == 1.0
        assert fp["timezone"] == "Asia/Shanghai"
        await session.close()


class Test批次元数据:
    async def test_九项齐全且_含_headless(self, fake_manager: list[_FakeManager]) -> None:
        """批次元数据是「事后能解释分布跳变」的凭据——**少一项就少一条解释**。

        ``headless`` 是 2026-10-21 加的第九项（口径反转带来的）。
        """
        session = await CamoufoxSession.launch()
        fp = session.fingerprint
        assert set(fp) == {
            "os", "geoip", "locale", "humanize", "headless",
            "screen", "window", "dpr", "timezone",
        }
        await session.close()

    async def test_不含任何身份态(self, fake_manager: list[_FakeManager]) -> None:
        """指纹读数**只**是几何与时区，不含 Cookie / 登录态 / Profile。

        红线：这些要推给 Label Studio。
        """
        session = await CamoufoxSession.launch()
        blob = repr(session.fingerprint) + repr(session.readings)
        for banned in ("cookie", "localStorage", "sessionStorage", "token"):
            assert banned not in blob
        await session.close()


class Test失败时不泄漏进程:
    async def test_launch_半途失败会收掉_manager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """browser 起来了但读指纹挂了 → 必须把 playwright 一起收掉。

        不收就是**进程泄漏**：一批跑下来攒下一堆僵尸浏览器，
        而症状是「机器越来越慢、最后 OOM」，跟这里完全对不上。
        """
        seen: list[_FakeManager] = []

        class _BoomPage(_FakePage):
            async def evaluate(self, expression: str) -> dict[str, Any]:
                raise RuntimeError("boom")

        class _BoomContext(_FakeContext):
            async def new_page(self) -> _FakePage:
                page = _BoomPage()
                self.pages.append(page)
                return page

        class _BoomBrowser(_FakeBrowser):
            async def new_context(self) -> _FakeContext:
                ctx = _BoomContext()
                self.contexts.append(ctx)
                return ctx

        class _Boom(_FakeManager):
            async def __aenter__(self) -> _FakeBrowser:
                return _BoomBrowser()

        def _factory(**kwargs: Any) -> _Boom:
            manager = _Boom(**kwargs)
            seen.append(manager)
            return manager

        monkeypatch.setattr(mod, "AsyncCamoufox", _factory)
        with pytest.raises(RuntimeError):
            await CamoufoxSession.launch()
        assert seen and seen[0].exited is True
