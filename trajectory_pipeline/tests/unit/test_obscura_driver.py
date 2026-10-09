"""``ObscuraDriver.observe()`` 的降级记账——**驱动层与感知层之间的契约**。

这个文件测的不是 obscura 的返回格式（那是 ``test_dom.py`` 的事，用实测存档
当 fixture），而是：**哪些情况必须被记进 :attr:`Observation.degraded`**。

理由是一条实测教训。iqiyi / ixigua / sohu 三站采集结果是
``body_len=0`` + ``interactive_elements=0``，驱动层当时没记任何降级，
于是感知层把「正文根本没采到」当成「这页一个控件都没有」，
三站全被写成 ``no_play_control`` 负样本——**采集失败被当成了业务结论**。
这类 bug 不会报错，只会让素材池里多出看起来很正常的假条目。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from trajectory_pipeline.executor.browser.mcp_client import McpError, ToolResult
from trajectory_pipeline.executor.browser.obscura_driver import (
    FORBIDDEN_TOOLS,
    ObscuraDriver,
)
from trajectory_pipeline.executor.browser.page_driver import DriverError


class FakeMcp:
    """按 tool 名返回预设文本。缺失的 tool 抛 :class:`McpError`。"""

    def __init__(self, script: dict[str, str | Exception]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        self.calls.append((name, args or {}))
        if name not in self.script:
            raise McpError(f"fake: {name} 未配置")
        value = self.script[name]
        if isinstance(value, Exception):
            raise value
        return ToolResult(text=value, is_error=False, structured=None, latency_ms=1)


#: obscura 各 tool 的**实测返回形态**（取自 ``output/pipeline/obscura_returns_*.json``，
#: 不是按 tool 描述推测的）。这里只保留解析要用到的骨架。
SNAPSHOT = (
    "URL: https://www.hao123.com/\n"
    "Title: hao123_上网从这里开始\n"
    'Snapshot:\n<body><style>.a{color:red}</style><div>电影 预告片 在线观看</div></body>'
)
INNER_TEXT = "hao123_上网从这里开始\n电影 预告片 在线观看"
LINKS = '{"text":"电影","href":"https://v.qq.com/x/play"}\n{"text":"hao123","href":"https://www.hao123.com/"}'
# 列间是**多个空格**、首列带 ``ref=`` 前缀（``parse_interactive_elements``
# 的实测口径）。写成 ``e1\\ta\\t"电影"`` 时每行都被判为格式不符丢弃，
# 解析结果是空元组——而空元组与「采集失败」在数据上同形。
ELEMENTS = 'ref=e1    a                      "电影"\nref=e2    button                  "在线观看"'


def full_script(**overrides: Any) -> dict[str, Any]:
    script: dict[str, Any] = {
        "browser_snapshot": SNAPSHOT,
        "browser_evaluate": INNER_TEXT,
        "browser_links": LINKS,
        "browser_interactive_elements": ELEMENTS,
        "browser_count": "1",
        "browser_navigate": "OK",
    }
    script.update(overrides)
    return script


def driver_for(**overrides: Any) -> ObscuraDriver:
    return ObscuraDriver(FakeMcp(full_script(**overrides)))


class TestRedline:
    @pytest.mark.parametrize("tool", sorted(FORBIDDEN_TOOLS))
    async def test_红线tool_被闸门拦在调用之前(self, tool):
        """闸门在 :meth:`ObscuraDriver._call` 里，调 tool 之前就拦——
        不能先取回再决定不用：取回的那一刻 cookie 已经在进程里了。"""
        fake = FakeMcp({tool: "sid=abc"})
        d = ObscuraDriver(fake)
        with pytest.raises(DriverError):
            await d._call(tool, {}, on_error="empty")
        assert fake.calls == [], f"{tool} 已发出去了才被拦"

    async def test_驱动层从不调用红线tool(self):
        """比逐个拦更根本的保证：这些 tool **根本没有被包装**。
        逐个拦防的是「有人新加一个方法调用它」，这条防的是
        「红线清单本身漂移」（obscura 升级后新增 cookie 类 tool）。"""
        fake = FakeMcp(full_script())
        d = ObscuraDriver(fake)
        await d.observe()
        await d.snapshot()
        await d.links()
        await d.interactive()
        await d.count("iframe")
        await d.goto("https://x.test/")
        used = {name for name, _ in fake.calls}
        assert not (used & FORBIDDEN_TOOLS)
        assert used <= {
            "browser_snapshot", "browser_evaluate", "browser_links",
            "browser_interactive_elements", "browser_count", "browser_navigate",
        }


class TestDegradedAccounting:
    async def test_一切正常时不记降级(self):
        obs = await driver_for().observe()
        assert obs.degraded == ()
        assert obs.body_source == "inner_text"
        assert obs.body_text.startswith("hao123")

    async def test_正文主路径挂掉要记降级(self):
        """``innerText`` 取不到 → 退回快照正文。这份正文可能是
        「整段 CSS 被剥光后的空壳」，不记的话下游会把空壳当成空页面。"""
        obs = await driver_for(browser_evaluate=McpError("挂了")).observe()
        assert "browser_evaluate" in obs.degraded
        assert obs.body_source == "snapshot"

    async def test_正文两条路径都空时记_body_text(self):
        """实测 iqiyi / ixigua / sohu 三站就是这种形态：
        正文 0 字符 + 交互元素 0 个。必须能看出来。"""
        fake = FakeMcp({"browser_snapshot": "URL: https://x.test/\nTitle: T\nSnapshot:\n",
                        "browser_links": "No links found.",
                        "browser_interactive_elements":
                            "No interactive elements on this page.",
                        "browser_count": "0"})
        obs = await ObscuraDriver(fake).observe()
        assert "body_text" in obs.degraded
        assert obs.body_text == ""
        assert obs.interactive_elements == ()

    async def test_evaluate_成功但返回空串也要记(self):
        """JS 页面还没渲染时 evaluate **成功但返回空**——不是错误，
        所以 :meth:`_call` 不会记降级。这才是最常见的一种「没正文」。"""
        obs = await driver_for(
            browser_evaluate="",
            browser_snapshot="URL: https://x.test/\nTitle: T\nSnapshot:\n",
        ).observe()
        assert "body_text" in obs.degraded
        assert obs.body_text == ""

    async def test_哨兵空页不记降级_但也不假装有内容(self):
        """``No interactive elements on this page.`` 是**正常返回**不是错误，
        所以不进 degraded——但空元素本身在感知层会被拒绝当成「无控件」。
        两层分工：驱动层如实说「没报错」，感知层说「我判不了」。"""
        fake = FakeMcp({"browser_snapshot": SNAPSHOT,
                        "browser_evaluate": INNER_TEXT,
                        "browser_links": "No links found.",
                        "browser_interactive_elements":
                            "No interactive elements on this page.",
                        "browser_count": "0"})
        obs = await ObscuraDriver(fake).observe()
        assert obs.degraded == ()
        assert obs.interactive_elements == ()

    async def test_计数降级要记进_同一个列表(self):
        obs = await driver_for(browser_count=McpError("挂了")).observe()
        assert "browser_count" in obs.degraded
        assert obs.video_tag_count == 0        # 降级成 0，但记了账

    async def test_元素采集降级要记(self):
        obs = await driver_for(browser_interactive_elements="Error: timeout").observe()
        assert "browser_interactive_elements" in obs.degraded

    async def test_降级去重且保序(self):
        obs = await driver_for(browser_count=McpError("挂了")).observe()
        assert list(obs.degraded).count("browser_count") == 1


class TestSnapshotContract:
    async def test_snapshot_缺参数不报错(self):
        """``snapshot()`` 单独用（不带 degraded list）时不能崩——
        它在协议上是公开原语，不只服务于 ``observe``。"""
        snap = await driver_for().snapshot(max_chars=2000)
        assert snap.url == "https://www.hao123.com/"
        assert "hao123" in snap.title

    async def test_snapshot_失败抛_DriverError(self):
        """快照是 fail-closed 的锚点——失败必须抛，不能返回空壳。"""
        d = driver_for(browser_snapshot=McpError("挂了"))
        with pytest.raises(DriverError):
            await d.snapshot()

class TestTotalsAccounting:
    """``elements_total`` / ``links_total``——**落盘条数被裁时的分母**。

    D-2 判「这一页采得全不全」，前提是知道**总共该有多少条**。没有总量
    时两种相反的错误在数据上完全同形：页面真有 300 个控件而我们采了 80，
    与页面只有 80 个控件，都落成「80 条」。前者是采集不足（该重试），
    后者正常——判反了就会把正常页面判成不可判定。
    """

    async def test_总量非零且不小于保留条数(self):
        obs = await driver_for().observe()
        assert obs.elements_total > 0 and obs.links_total > 0
        assert obs.elements_total >= len(obs.interactive_elements)
        assert obs.links_total >= len(obs.links)

    async def test_总量等于本次实际采到的条数(self):
        """口径写在字段说明里：**不是页面真总数**，是 driver 这一轮
        拿到的条数（可能被 ``DEFAULT_LIMIT`` 截断）。这个口径够 D-2 用——
        存档侧还有第二层 80/50 裁剪，两层都记下才不会丢信息。"""
        obs = await driver_for().observe()
        assert obs.elements_total == len(obs.interactive_elements)

    async def test_元素采集失败时总量为零(self):
        """失败必须是 0，不能沿用上一次的数字。0 表示「这条没采到」，
        与「采到了 0 条」同形——但那正是 degraded 要说的事。"""
        obs = await driver_for(
            browser_interactive_elements=McpError("挂了")).observe()
        assert obs.elements_total == 0
        assert "browser_interactive_elements" in obs.degraded


class TestExtractWiring:
    """``browser_extract`` 的入参键名——**只能靠取证，不能靠猜**。

    踩坑经过：接成 ``{"fields": …}``，上游回 ``Error: Missing schema object``，
    而 ``extract`` 的 ``on_error="empty"`` 把它吞成空字典。症状是
    「判断点 ① 抽不到结果标题」，而标题抽不到有四种完全不同的原因
    （选择器失效 / 页面没渲染 / 入参错 / 返回键名读错），症状一模一样。

    obscura 的 ``tools/list`` **不返回 inputSchema**（实测四个采集 tool
    全是 ``null``，它把 schema 放在自定义的 ``input_schema`` 键里），
    靠读文档或看类型标注都发现不了这个错。
    """

    def test_入参键叫schema不叫fields(self):
        fake = FakeMcp({"browser_extract": '{"titles": ["功夫"]}'})
        got = asyncio.run(ObscuraDriver(fake).extract({"titles[]": "li.b_algo h2 a"}))
        assert fake.calls[0][0] == "browser_extract"
        assert set(fake.calls[0][1]) == {"schema"}, f"入参键错了：{fake.calls[0][1]}"

    def test_上游错误不被吞成成功(self):
        """``on_error="empty"`` 对可选采集是对的（缺失即缺失），
        但**它必须仍然可被看见**：返回空 → 上游判 ① fail-closed → 报表上
        是一条 ``unresolved``，看不出是上游报错了还是页面真没标题。
        这里锁住的是「错误确实被转成空」，而可见性由
        ``Observation.degraded`` 之外的日志/告警负责。"""
        fake = FakeMcp({"browser_extract": "Error: Missing schema object"})
        got = asyncio.run(ObscuraDriver(fake).extract({"titles[]": "x"}))
        assert got == {}

    def test_空fields不调上游(self):
        fake = FakeMcp({})
        assert asyncio.run(ObscuraDriver(fake).extract({})) == {}
        assert fake.calls == [], "空请求也发上去了，白开一次浏览器往返"

    def test_返回原样透传不配对(self):
        """``titles[]`` 与 ``urls[]`` **不在解析层配对**——
        长度不等时配出来的是查无出处的假对应。对齐在
        ``steps/search.result_titles``，它能看见长度。"""
        fake = FakeMcp({"browser_extract": '{"titles": ["a", "b"], "urls": ["u"]}'})
        got = asyncio.run(ObscuraDriver(fake).extract({"titles[]": "a", "urls[]": "a"}))
        assert got == {"titles": ["a", "b"], "urls": ["u"]}
