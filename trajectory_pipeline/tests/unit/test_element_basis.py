"""**跨面可比的元素计数口径**（设计方案 04 §4.9.6）——离线测试，不联网。

这个文件封的是三件事：

1. **同一段 JS、同一把尺子**。两个执行层跑的是**同一个** ``SHARED_DOM_JS``
   与**同一个** ``filter_ladder``——抄两份必然漂，而漂了之后「−70%」
   又变成不可解释的量（本轮双跑的原始结论就是这样来的）。
2. **fail-closed**。共享口径拿不到就抛，**不返回空账**——空账会被读成
   「这个页面上一个交互元素都没有」，而真相是「没测到」。
3. **``None`` ≠ 空账**。``Observation.element_basis`` 为 ``None`` 的意思是
   「这一批没开 ``--element-basis``」，不是「页面上没有元素」。
   这两种「没有」在读档时必须分开。
4. **测量失败不进 ``degraded``**（2026-10-21 由 P0 等价性门禁的变异测试
   挖出）。``degraded`` 是**内容块**的降级通道，下游只判它非空——
   ``extract_candidates`` 见到降级就**不取候选**，于是「尺子坏了」
   会**作废整批数据**。失败只记进 ``element_basis_error``。

⚠️ **它不测「两个浏览器真的差多少」**——那要真浏览器。
这里测的是「口径本身没写错」：同一段 JS 送进两个驱动，送出去的**是同一段**。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from trajectory_pipeline.executor.browser import camofox_driver, obscura_driver
from trajectory_pipeline.executor.browser.element_basis import (
    LADDER_STAGES,
    SHARED_BASE_STAGES,
    SHARED_DOM_JS,
    SHARED_LIMIT,
    filter_ladder,
    parse_shared_basis,
)
from trajectory_pipeline.perception.base import Observation


# ── 过滤阶梯 ────────────────────────────────────────────────────


class Test过滤阶梯:
    def test_四档过滤的顺序与计数(self):
        """role 词表 → 无名 → 真同名整组 → 截断。**顺序不能换**：
        先去同名再判无名，账目就分不出「无名」与「同名」各丢几个。
        """
        pairs = [
            ("button", "播放"),      # 留
            ("link", "详情"),        # 真同名（两个）→ 整组丢
            ("link", "详情"),        # 真同名
            ("textbox", ""),         # 无名 → 丢
            ("generic", "杂项"),     # 词表外 → 丢
            ("button", "取消"),      # 留
        ]
        kept, ledger = filter_ladder(pairs)
        assert kept == [("button", "播放"), ("button", "取消")]
        assert ledger == {
            # role_matched 是**过了词表**的数（还没判有没有名字）：
            # button×2 + link×2 + textbox×1 = 5。
            "role_matched": 5,
            "role_unmatched": 1,      # generic
            "nameless_dropped": 1,    # 空名字的 textbox
            "ambiguous_dropped": 2,   # 两个「详情」
            "kept": 2,
            "truncated": 0,
        }

    def test_账目恒等式(self):
        """每一档的数都能对上——**账目自证**是这一层的全部价值。

        ``nodes = role_matched + role_unmatched``；
        ``role_matched = nameless_dropped + ambiguous_dropped + kept + truncated``。
        """
        pairs = [("button", f"b{i}") for i in range(5)]
        pairs += [("link", "x"), ("link", "x"), ("textbox", ""), ("zzz", "q")]
        kept, ledger = filter_ladder(pairs)
        assert ledger["role_matched"] + ledger["role_unmatched"] == len(pairs)
        assert (ledger["nameless_dropped"] + ledger["ambiguous_dropped"]
                + ledger["kept"] + ledger["truncated"]) == ledger["role_matched"]
        assert len(kept) == ledger["kept"]

    def test_截断记在账目里(self):
        kept, ledger = filter_ladder(
            [("button", f"b{i}") for i in range(5)], limit=2)
        assert len(kept) == 2
        assert ledger["truncated"] == 3
        assert ledger["kept"] == 2

    def test_同名整组丢而不是留第一个(self):
        """留第一个是赌，**两个面各有各的赌注**：

        - camoufox/camofox 面的上游是 ``getByRole(role, {name}).nth(nth)``
          ——``nth`` 按**精确同名**计数，同名两个时 ``nth(0)`` 拿到的
          **未必是清单里那个**；
        - 留下等于给动作面埋一颗**点错元素**的雷——而点错的症状是
          「点了但页面没变」，报告上看不出异常。"""
        kept, ledger = filter_ladder([("link", "详情"), ("link", "详情")])
        assert kept == []
        assert ledger["ambiguous_dropped"] == 2
        assert ledger["kept"] == 0

    def test_档位名是共用的一套(self):
        """两把尺子共用档位名 ⇒ 两本账能逐档对齐。"""
        assert set(LADDER_STAGES) == {
            "role_matched", "role_unmatched", "nameless_dropped",
            "ambiguous_dropped", "kept", "truncated",
        }
        assert set(SHARED_BASE_STAGES) == {"candidates", "hidden", "visible"}


# ── 共享口径的解析（fail-closed）───────────────────────────────


class Test共享口径解析:
    def test_正常返回给出基数加阶梯(self):
        raw = json.dumps({
            "nodes": 12, "hidden": 3,
            "pairs": [["button", "播放"], ["link", "详情"], ["generic", "杂项"]],
        })
        kept, ledger = parse_shared_basis(raw)
        assert kept == [("button", "播放"), ("link", "详情")]
        assert ledger["candidates"] == 12
        assert ledger["hidden"] == 3
        assert ledger["visible"] == 3        # 可见候选数 = pairs 长度
        assert ledger["role_matched"] == 2
        assert ledger["role_unmatched"] == 1
        assert ledger["kept"] == 2

    def test_空串抛而不是给空账(self):
        """空账会被读成「这个页面上一个交互元素都没有」——
        而真相是「没测到」。这两件事必须分开。"""
        with pytest.raises(ValueError):
            parse_shared_basis("")

    def test_不是JSON抛(self):
        with pytest.raises(ValueError):
            parse_shared_basis("not json at all")

    def test_缺pairs抛(self):
        with pytest.raises(ValueError):
            parse_shared_basis(json.dumps({"nodes": 3}))

    def test_pair形状不对抛(self):
        with pytest.raises(ValueError):
            parse_shared_basis(json.dumps({"pairs": [["button"]]}))
        with pytest.raises(ValueError):
            parse_shared_basis(json.dumps({"pairs": ["button"]}))

    def test_不是对象抛(self):
        with pytest.raises(ValueError):
            parse_shared_basis(json.dumps([1, 2, 3]))

    def test_已经解析好的对象也收(self):
        kept, ledger = parse_shared_basis(
            {"nodes": 1, "hidden": 0, "pairs": [["button", "播放"]]})
        assert kept == [("button", "播放")]
        assert ledger["kept"] == 1


# ── 两个驱动共用同一段 JS ──────────────────────────────────────


class Test两个驱动共用同一段JS:
    def test_两个驱动跑的是同一段(self):
        """**抄两份必然漂**，而漂了之后差值又变成不可解释的量。"""
        assert obscura_driver.SHARED_DOM_JS is SHARED_DOM_JS
        assert camofox_driver.SHARED_DOM_JS is SHARED_DOM_JS

    def test_共享JS是JSON字符串输出(self):
        """obscura 的 ``browser_evaluate`` 返回 JS 值的 ``toString()``——
        对象会变成 ``[object Object]``（tools.md 记过）。
        所以共享口径**必须**自己 ``JSON.stringify``。"""
        assert "JSON.stringify" in SHARED_DOM_JS
        assert "pairs" in SHARED_DOM_JS

    def test_测量上限与生产枚举上限不是一回事(self):
        """共享口径数的是「这个页面有多少」，生产枚举数的是「我们留了多少」。
        两者混用会把 D-2 的「被裁掉 vs 不存在」判据弄坏。"""
        assert SHARED_LIMIT == 500


# ── 驱动层：采集与记账 ─────────────────────────────────────────


class _FakeCamofoxSession:
    """假 camofox 会话——共享口径只走 ``evaluate`` 这一条。

    ⚠️ **同形要求**（2026-10-26）：camofox 驱动的会话接口是
    ``evaluate(tab_id, expression)`` + ``current``，**不是** playwright 的
    ``page.evaluate(expr)``。这里照着**生产接口**写，别照着旧 camoufox
    那个 ``page`` 写——形状不对，测的就不是生产链的调用路径了。
    """

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.evals: list[tuple[str, str]] = []
        #: 标签页栈（一上来就有一页——驱动每次动作都要当前页）。
        self.stack: list[str] = ["tab-1"]

    @property
    def current(self) -> str:
        return self.stack[-1]

    @property
    def pages(self) -> tuple[str, ...]:
        return tuple(self.stack)

    async def evaluate(self, tab_id: str, expression: str) -> Any:
        self.evals.append((tab_id, expression))
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw

    async def close(self) -> None:
        return None


class _FakeMcp:
    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict[str, Any] | None = None) -> Any:
        from trajectory_pipeline.executor.browser.mcp_client import ToolResult

        self.calls.append((name, args or {}))
        if isinstance(self.raw, Exception):
            raise self.raw
        return ToolResult(text=self.raw, is_error=False, structured=None,
                          latency_ms=1)


class Test驱动采集:
    async def test_camofox跑共享口径(self):
        raw = json.dumps({"nodes": 2, "hidden": 0,
                          "pairs": [["button", "播放"], ["link", "详情"]]})
        session = _FakeCamofoxSession(raw)
        driver = camofox_driver.CamofoxDriver(session)  # type: ignore[arg-type]
        ledger = await driver.element_basis()
        assert session.evals == [("tab-1", SHARED_DOM_JS)]
        assert ledger["kept"] == 2
        assert ledger["candidates"] == 2

    async def test_camofox共享口径坏了要抛(self):
        """抛 ``DriverError``——空账会被读成「这个页面上一个交互元素都没有」，
        而真相是「没测到」。"""
        from trajectory_pipeline.executor.browser.page_driver import DriverError

        session = _FakeCamofoxSession(DriverError("页面没了"))
        driver = camofox_driver.CamofoxDriver(session)  # type: ignore[arg-type]
        with pytest.raises(DriverError):
            await driver.element_basis()

    async def test_obscura跑同一段共享口径(self):
        raw = json.dumps({"nodes": 2, "hidden": 0,
                          "pairs": [["button", "播放"], ["link", "详情"]]})
        client = _FakeMcp(raw)
        driver = obscura_driver.ObscuraDriver(client)  # type: ignore[arg-type]
        ledger = await driver.element_basis()
        assert client.calls[0][0] == "browser_evaluate"
        assert client.calls[0][1]["expression"] == SHARED_DOM_JS
        assert ledger["kept"] == 2


# ── 测量失败不污染降级通道 ──────────────────────────────────────


class _ScriptedMcp:
    """按 tool / expression 分派的假 MCP——只为驱动层的 ``observe`` 造。

    共享口径那一路**故意给坏值**：``parse_shared_basis`` 收到非 JSON 会抛
    ``ValueError`` → 驱动包成 ``DriverError``——这就是「尺子坏了」的最小复现。
    其余各路都答得好好的，**好让 ``degraded`` 保持为空**——
    否则测的就不是「测量失败有没有污染降级」，而是「别的东西有没有污染降级」。
    """

    def __init__(self, *, shared_raw: str = "这不是 JSON",
                 elements: str = 'ref=e1    a    "Learn more"') -> None:
        self.shared_raw = shared_raw
        self.elements = elements
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict[str, Any] | None = None) -> Any:
        from trajectory_pipeline.executor.browser.mcp_client import ToolResult

        args = args or {}
        self.calls.append((name, args))
        if name == "browser_snapshot":
            text = "URL: https://example.com/\nTitle: 例子\n\n快照正文"
        elif name == "browser_evaluate":
            expr = str(args.get("expression") or "")
            text = self.shared_raw if expr == SHARED_DOM_JS else "页面正文"
        elif name == "browser_links":
            text = ""
        elif name == "browser_interactive_elements":
            text = self.elements
        elif name == "browser_count":
            text = "0"
        else:
            raise AssertionError(f"没料到的 tool：{name}")
        return ToolResult(text=text, is_error=False, structured=None, latency_ms=1)


class Test测量失败不污染降级通道:
    """**尺子坏了 ≠ 页面不可信**。

    2026-10-21 的实际缺陷：共享口径失败时被塞进了 ``Observation.degraded``，
    而下游只判 ``degraded`` 非空——``extract_candidates`` 见到降级就
    **不取候选**。于是一把坏尺子**作废整批数据**：搜索页零候选，
    run 提前返回，产出零样本且**零报错**。

    修法：失败只记进 ``element_basis_error``，face 账目照旧给。
    """

    async def test_obscura共享口径坏了不进degraded(self):
        client = _ScriptedMcp(shared_raw="这不是 JSON")
        driver = obscura_driver.ObscuraDriver(  # type: ignore[arg-type]
            client, collect_element_basis=True)
        obs = await driver.observe()
        assert obs.degraded == (), f"测量失败混进了 degraded：{obs.degraded}"
        assert "element_basis" not in obs.degraded

    async def test_obscura共享口径坏了记下原因(self):
        client = _ScriptedMcp(shared_raw="这不是 JSON")
        driver = obscura_driver.ObscuraDriver(  # type: ignore[arg-type]
            client, collect_element_basis=True)
        obs = await driver.observe()
        assert obs.element_basis_error, "共享口径坏了却没记原因——等于静默"
        assert "共享口径" in obs.element_basis_error

    async def test_obscura共享口径坏了face账目照旧给(self):
        """face 账目**不依赖**共享口径——它来自上游工具的最终列表。
        共享口径坏了不该连累它。"""
        client = _ScriptedMcp(shared_raw="这不是 JSON")
        driver = obscura_driver.ObscuraDriver(  # type: ignore[arg-type]
            client, collect_element_basis=True)
        obs = await driver.observe()
        assert obs.element_basis is not None
        assert "shared" not in obs.element_basis, "共享口径没测到却落了账"
        assert obs.element_basis["face"]["nodes"] == 1

    async def test_obscura共享口径好了两本账都在(self):
        raw = json.dumps({"nodes": 2, "hidden": 0,
                          "pairs": [["button", "播放"], ["link", "详情"]]})
        client = _ScriptedMcp(shared_raw=raw)
        driver = obscura_driver.ObscuraDriver(  # type: ignore[arg-type]
            client, collect_element_basis=True)
        obs = await driver.observe()
        assert obs.element_basis_error is None
        assert set(obs.element_basis) == {"shared", "face"}
        assert obs.element_basis["shared"]["kept"] == 2

    async def test_obscura没开开关时两本账都没有(self):
        client = _ScriptedMcp()
        driver = obscura_driver.ObscuraDriver(client)  # type: ignore[arg-type]
        obs = await driver.observe()
        assert obs.element_basis is None
        assert obs.element_basis_error is None
        assert obs.degraded == ()

    async def test_camofox共享口径坏了不进degraded也不丢face(self):
        from trajectory_pipeline.executor.browser.page_driver import DriverError

        session = _FakeCamofoxSession(DriverError("页面没了"))
        driver = camofox_driver.CamofoxDriver(  # type: ignore[arg-type]
            session, collect_element_basis=True)
        basis, basis_error = await driver._collect_basis([], {"nodes": 3, "kept": 2})
        assert basis_error, "共享口径坏了却没记原因"
        assert "shared" not in basis
        assert basis["face"] == {"nodes": 3, "kept": 2}

    def test_降级非空会让下游不取候选(self):
        """**为什么**测量失败不能进 ``degraded``——把后果摆出来。

        这条不是装饰：它是「不进 degraded」这条纪律的**理由**。
        没有它，下一个改驱动的人会问「为什么不统一记进 degraded」，
        而那时没人记得这个后果。
        """
        from trajectory_pipeline.executor.steps.search import extract_candidates
        from trajectory_pipeline.perception.base import LinkItem

        def _obs(degraded: tuple[str, ...]):
            return Observation(
                url="https://www.baidu.com/s?wd=x",
                page_title="百度",
                body_text="正文",
                links=(LinkItem("腾讯视频", "https://v.qq.com/x/cover/a.html"),),
                degraded=degraded,
            )

        assert extract_candidates(_obs(()))
        assert extract_candidates(_obs(("element_basis",))) == [], (
            "降级会让候选整个消失——所以测量失败绝不能走 degraded 这条路")


# ── Observation 层的语义 ───────────────────────────────────────


class Test观察字段语义:
    def test_缺省是None不是空账(self):
        """``None`` = 「这一批没开 --element-basis」，
        **不是**「页面上一个元素都没有」。"""
        obs = Observation(url="u", page_title="t", body_text="b")
        assert obs.element_basis is None

    def test_结构是两本账平级(self):
        obs = Observation(
            url="u", page_title="t", body_text="b",
            element_basis={"shared": {"kept": 3}, "face": {"nodes": 9}},
        )
        assert set(obs.element_basis) == {"shared", "face"}


# ── 存档往返 ───────────────────────────────────────────────────


class Test存档往返:
    def test_元素账目进存档也能读回来(self):
        from trajectory_pipeline.executor.orchestrator import _obs_json
        from trajectory_pipeline.perception.replay import observation_of

        obs = Observation(
            url="u", page_title="t", body_text="b",
            element_basis={"shared": {"candidates": 12, "kept": 3},
                           "face": {"nodes": 9, "kept": 3}},
        )
        doc = _obs_json(obs)
        assert doc is not None
        assert doc["element_basis"]["shared"]["kept"] == 3
        assert doc["element_basis"]["face"]["nodes"] == 9
        back = observation_of(doc)
        assert back.element_basis == {"shared": {"candidates": 12, "kept": 3},
                                      "face": {"nodes": 9, "kept": 3}}

    def test_没开开关时存档里是null而不是空账(self):
        """读档的人要把「没测」与「真没有」分开。"""
        from trajectory_pipeline.executor.orchestrator import _obs_json
        from trajectory_pipeline.perception.replay import observation_of

        obs = Observation(url="u", page_title="t", body_text="b")
        doc = _obs_json(obs)
        assert doc is not None
        assert doc["element_basis"] is None
        assert observation_of(doc).element_basis is None

    def test_形状不对的账当没测(self):
        """半截的账比没有账更坏——读的人会拿它当「测过」的数去比。"""
        from trajectory_pipeline.perception.replay import observation_of

        back = observation_of({"element_basis": {"shared": "这不是账"}})
        assert back.element_basis is None

    def test_只测到一本账时另一本缺键而不是空账(self):
        """**缺键 ≠ 空账**。

        共享口径坏了时 face 账目照旧给，于是存档里只有 ``face``。
        落一个 ``"shared": {}`` 进去，读的人会以为「测了，什么都没有」——
        而真相是「没测到」。"""
        from trajectory_pipeline.executor.orchestrator import _obs_json
        from trajectory_pipeline.perception.replay import observation_of

        obs = Observation(
            url="u", page_title="t", body_text="b",
            element_basis={"face": {"nodes": 9, "kept": 9}},
            element_basis_error="共享口径坏了",
        )
        doc = _obs_json(obs)
        assert doc is not None
        assert set(doc["element_basis"]) == {"face"}
        assert "shared" not in doc["element_basis"]
        back = observation_of(doc)
        assert back.element_basis == {"face": {"nodes": 9, "kept": 9}}

    def test_测量失败的原因进存档也能读回来(self):
        """原因不落盘就等于没记——下一批的人会重新踩一遍同样的坑。"""
        from trajectory_pipeline.executor.orchestrator import _obs_json
        from trajectory_pipeline.perception.replay import observation_of

        obs = Observation(
            url="u", page_title="t", body_text="b",
            element_basis={"face": {"nodes": 9, "kept": 9}},
            element_basis_error="obscura 共享口径返回不合法",
        )
        doc = _obs_json(obs)
        assert doc is not None
        assert doc["element_basis_error"] == "obscura 共享口径返回不合法"
        assert observation_of(doc).element_basis_error == "obscura 共享口径返回不合法"

    def test_没失败时存档里原因栏是null(self):
        """没失败就写 ``null``——不写空串。空串会被读成「失败但没原因」。"""
        from trajectory_pipeline.executor.orchestrator import _obs_json

        obs = Observation(
            url="u", page_title="t", body_text="b",
            element_basis={"shared": {"kept": 1}, "face": {"nodes": 1, "kept": 1}},
        )
        doc = _obs_json(obs)
        assert doc is not None
        assert doc["element_basis_error"] is None

    def test_空串原因读回来是None(self):
        """fail-closed：原因不是字符串就当「没测到且没记原因」。"""
        from trajectory_pipeline.perception.replay import observation_of

        assert observation_of({"element_basis_error": ""}).element_basis_error is None
        assert observation_of({"element_basis_error": 42}).element_basis_error is None


# ── CLI 的读数 ──────────────────────────────────────────────────


class TestCLI读数:
    """``check --element-basis`` 打出来的读数**要把「没测到」说出来**。

    少了这一句，「shared 那一行没打」会被读成「这一档是 0」——
    而它其实是「没测到」。这是读数契约的一部分，归测试管。
    """

    @staticmethod
    def _print(basis: dict[str, Any], error: str | None = None) -> str:
        import io
        from contextlib import redirect_stdout

        from trajectory_pipeline.executor.cli import _print_element_basis

        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_element_basis(basis, error)
        return buf.getvalue()

    def test_两本账都在时逐档打出来(self):
        out = self._print({"shared": {"kept": 3}, "face": {"nodes": 9}})
        assert "kept=3" in out and "nodes=9" in out
        assert "FAIL" not in out

    def test_共享口径缺键且有原因时说出原因(self):
        out = self._print({"face": {"nodes": 9}}, "共享口径返回不合法")
        assert "FAIL" in out
        assert "共享口径返回不合法" in out
        assert "nodes=9" in out, "face 账目照旧要打——它不依赖共享口径"

    def test_共享口径缺键且没原因时说本面给不出(self):
        """**没开开关**与**测失败**是两件事——读数不能混。"""
        out = self._print({"face": {"nodes": 9}})
        assert "本面给不出这一档" in out
        assert "FAIL" not in out
