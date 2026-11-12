"""**候选提取与 body 质量门禁解耦**（H-32 · all80 批次事故 P1-4/P1-5）。

事故形状
--------
all80 批次里 **13 条任务 0 候选空跑**（字节量占全批 19 %）。它们的
搜索页 **links 明明有 102–111 条**，只因**正文**被判乱码
（``body_mojibake``）就**整批候选被清空**——同一批里 T029 立刻取到
14 个候选 ⇒ **间歇性触发**，不是百度页面的稳定属性。

⇒ 代码把**采集侧的一次失败**写成了**内容质量的结论**。

这里封三件事：

    1. **降级按内容块分类**（``executor/degraded.py``）——「我这一块坏了吗」
       而不是「有没有块坏了」；
    2. **候选提取只被「链接那块」的降级否决**——body 乱码 / 正文缺失 /
       元素枚举失败**动不了从 links 提取的候选**；
    3. **乱码先重取 snapshot 再判**——HTML 源码里可能仍带着正确的
       ``<meta charset>`` 声明；**重取过还是乱**才记降级。

⚠️ **降级必须留痕**：否决与否是**消费侧**的事，**记录**始终在
``Observation.degraded`` 里——隔离候选 ≠ 抹掉降级事实。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from trajectory_pipeline.executor import degraded as degraded_mod
from trajectory_pipeline.executor.browser import camofox_driver
from trajectory_pipeline.executor.browser.camofox_driver import CamofoxDriver
from trajectory_pipeline.executor.dom import TEXT_CONTENT_EXPR
from trajectory_pipeline.executor.orchestrator import Orchestrator, RunConfig
from trajectory_pipeline.executor.steps import search as search_step
from trajectory_pipeline.perception.base import (
    Decision,
    InteractiveElement,
    LinkItem,
    Observation,
    Q,
)

REPO = Path(__file__).resolve().parents[3]
DRIVER_SRC = REPO / "trajectory_pipeline" / "executor" / "browser"


# ─────────────── 分类 ───────────────


class Test降级按内容块分类:
    def test_正文类标记归正文(self):
        for marker in ("body_text", "body_mojibake", "title_mojibake"):
            assert degraded_mod.block_of(marker) == degraded_mod.BLOCK_BODY

    def test_链接类标记归链接(self):
        for marker in ("links", "browser_links"):
            assert degraded_mod.block_of(marker) == degraded_mod.BLOCK_LINKS

    def test_未知标记归other不猜(self):
        """⚠️ **不猜它属于哪一块**——猜「无害」会让它悄悄作废整批数据，
        猜「致命」又会误杀。归 ``other`` = 不否决候选，但**原样留在
        ``degraded`` 里**。"""
        assert degraded_mod.block_of("将来某个新标记") == degraded_mod.BLOCK_OTHER

    def test_正文降级不否决候选提取(self):
        assert not degraded_mod.blocks_candidate_extraction(["body_mojibake"])
        assert not degraded_mod.blocks_candidate_extraction(["body_text"])
        assert not degraded_mod.blocks_candidate_extraction(
            ["title_mojibake", "interactive"])

    def test_链接与导航降级否决候选提取(self):
        assert degraded_mod.blocks_candidate_extraction(["links"])
        assert degraded_mod.blocks_candidate_extraction(
            [degraded_mod.DEGRADED_NAV_UNSETTLED])

    def test_没有降级时不否决(self):
        assert not degraded_mod.blocks_candidate_extraction([])
        assert not degraded_mod.blocks_candidate_extraction(None)


# ─────────────── 候选提取 ───────────────


def _obs(*, degraded=(), links=()) -> Observation:
    return Observation(
        url="https://www.bing.com/search?q=%E5%8A%9F%E5%A4%AB",
        page_title="功夫 在线观看 - 搜索",
        body_text="搜索结果页正文",
        links=tuple(LinkItem(text=t, href=h) for t, h in links),
        degraded=tuple(degraded),
    )


_LINKS = (
    ("优酷 - 功夫在线观看", "https://v.youku.com/x.html"),
    ("爱奇艺 - 功夫", "https://www.iqiyi.com/y.html"),
    ("腾讯视频 - 功夫", "https://v.qq.com/z.html"),
)


class Test候选提取不再一刀切:
    def test_body乱码但链接完整时候选照取(self):
        """**H-32 的验收条件**：一条 body 乱码但 links 完整的搜索页 ⇒
        断言**候选数 > 0**。"""
        stats: dict[str, int] = {}
        out = search_step.extract_candidates(
            _obs(degraded=["body_mojibake"], links=_LINKS),
            engine="bing", stats=stats,
        )
        assert len(out) == 3, f"候选被清空了（{len(out)} 条）——降级一刀切还在"
        assert "degraded" not in stats, (
            f"记账键 degraded 被当成「候选被否决」：{stats}"
        )
        assert stats.get("degraded_other") == 1, (
            f"非链接类降级没留痕：{stats}"
        )

    def test_标题乱码也不否决(self):
        out = search_step.extract_candidates(
            _obs(degraded=["title_mojibake"], links=_LINKS), engine="bing")
        assert len(out) == 3

    def test_元素枚举失败也不否决(self):
        out = search_step.extract_candidates(
            _obs(degraded=["interactive"], links=_LINKS), engine="bing")
        assert len(out) == 3

    def test_链接那块坏了仍否决(self):
        """对照组：**链接**这块坏了 ⇒ 候选无从谈起（旧行为保留）。"""
        stats: dict[str, int] = {}
        out = search_step.extract_candidates(
            _obs(degraded=["links"], links=()), engine="bing", stats=stats)
        assert out == []
        assert stats.get("degraded") == 1

    def test_读到一半仍否决(self):
        """``nav_unsettled`` = 读数跨了文档 ⇒ 链接可能来自另一个文档。"""
        stats: dict[str, int] = {}
        out = search_step.extract_candidates(
            _obs(degraded=[degraded_mod.DEGRADED_NAV_UNSETTLED],
                 links=_LINKS),
            engine="bing", stats=stats)
        assert out == []
        assert stats.get("degraded") == 1

    def test_降级事实仍落在观察里(self):
        """**隔离候选 ≠ 抹掉降级事实**——降级照旧在 ``obs.degraded``。"""
        obs = _obs(degraded=["body_mojibake"], links=_LINKS)
        search_step.extract_candidates(obs, engine="bing")
        assert "body_mojibake" in obs.degraded


# ─────────────── 乱码先重取再判 ───────────────


class _FakeSession:
    """假 camofox 会话——只提供 ``snapshot`` 这条路径用到的方法。"""

    def __init__(self, evals: dict[str, object]) -> None:
        self.evals = dict(evals)
        self.stack: list[str] = ["tab-1"]
        self.asked: list[str] = []

    @property
    def current(self) -> str:
        return self.stack[-1]

    async def evaluate(self, tab_id: str, expression: str) -> object:
        self.asked.append(expression)
        if expression not in self.evals:
            raise AssertionError(f"fake: 表达式未配置 {expression!r}")
        value = self.evals[expression]
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self) -> None:
        pass


def _page_json(**kw) -> str:
    payload = {"href": "https://example.com/watch",
               "title": "示例影片 在线观看", "innerText": ""}
    payload.update(kw)
    return json.dumps(payload, ensure_ascii=False)


_MOJIBAKE = "�" * 3


class Test乱码先重取snapshot再判:
    async def test_重取到可读正文就不记乱码(self):
        """**innerText 乱但 HTML 可读 ⇒ 用 HTML 那份，且不记降级。**

        依据：``innerText`` 是**浏览器已经解码过的文本**，页面编码不对
        时它回来就是乱的；而**同一页的 HTML 源码**可能仍带着正确的
        ``<meta charset>`` 声明。
        """
        session = _FakeSession({
            camofox_driver._PAGE_TEXT_JS: _page_json(
                innerText=f"正文{_MOJIBAKE}"),
            camofox_driver._HTML_JS:
                "<html><body><div>正文在 HTML 里</div></body></html>",
        })
        driver = CamofoxDriver(session)          # type: ignore[arg-type]
        degraded: list[str] = []
        snap = await driver.snapshot(max_chars=1000, degraded=degraded)
        assert "正文在 HTML 里" in snap.body
        assert snap.body_source == "snapshot"
        assert "body_mojibake" not in degraded, (
            f"重取成功了还记乱码：{degraded}"
        )

    async def test_重取后仍乱才记降级(self):
        """**HTML 也乱 ⇒ 才记降级**——「判不出」与「没查过」是两件事。"""
        session = _FakeSession({
            camofox_driver._PAGE_TEXT_JS: _page_json(
                innerText=f"正文{_MOJIBAKE}"),
            camofox_driver._HTML_JS:
                f"<html><body><div>正文{_MOJIBAKE}</div></body></html>",
        })
        driver = CamofoxDriver(session)          # type: ignore[arg-type]
        degraded: list[str] = []
        await driver.snapshot(max_chars=1000, degraded=degraded)
        assert "body_mojibake" in degraded

    async def test_重取失败才记降级(self):
        """**HTML 取不到 ⇒ 记乱码**（且**不**记 ``body_text``——
        正文不是没采到，是采到了但不可读）。"""
        from trajectory_pipeline.executor.browser.page_driver import DriverError

        session = _FakeSession({
            camofox_driver._PAGE_TEXT_JS: _page_json(
                innerText=f"正文{_MOJIBAKE}"),
            camofox_driver._HTML_JS: DriverError("页面没了"),
        })
        driver = CamofoxDriver(session)          # type: ignore[arg-type]
        degraded: list[str] = []
        snap = await driver.snapshot(max_chars=1000, degraded=degraded)
        assert "body_mojibake" in degraded
        assert "body_text" not in degraded
        assert snap.body  # 保留那份乱的正文——它仍是**证据**

    async def test_正文不乱时不重取(self):
        """**没坏就不查**——多一次 evaluate 往返是纯浪费。"""
        session = _FakeSession({
            camofox_driver._PAGE_TEXT_JS: _page_json(innerText="正常正文"),
        })
        driver = CamofoxDriver(session)          # type: ignore[arg-type]
        degraded: list[str] = []
        await driver.snapshot(max_chars=1000, degraded=degraded)
        assert camofox_driver._HTML_JS not in session.asked
        assert degraded == []


# ─────────────── 标记表不许漂 ───────────────


def _literals_appended_to_degraded(path: Path) -> set[str]:
    """源码里 ``degraded.append("字面量")`` 的全部字面量。

    **AST 读，不执行**——执行会把实现绑死在当前版本上，而这里要守的
    恰恰是「驱动新增了一个标记而分类表没跟上」。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "append" or not node.args:
            continue
        target = func.value
        if not (isinstance(target, ast.Name)
                and target.id == "degraded"):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.add(arg.value)
    return out


class Test标记表不许漂:
    @pytest.mark.parametrize("src", sorted(DRIVER_SRC.glob("*_driver.py")),
                             ids=lambda p: p.name)
    def test_驱动写的字面量标记都在表里(self, src):
        """⚠️ **新增标记而表里没登记 ⇒ 它掉进 ``other``** ⇒ 悄悄不否决
        候选。所以这张表必须跟着驱动走。"""
        missing = {
            m for m in _literals_appended_to_degraded(src)
            if m not in degraded_mod.MARKER_BLOCKS
            and not m.startswith("count:")
        }
        assert not missing, (
            f"{src.name} 写了这些降级标记，但分类表里没有：{sorted(missing)}"
            "——它们会掉进 other（不否决候选）"
        )

    def test_常量与分类表同源(self):
        """``camofox_driver`` 的两个常量**从分类表模块取**——
        两处各写一份字面量必然会漂。"""
        assert camofox_driver.DEGRADED_NAV_UNSETTLED == \
            degraded_mod.DEGRADED_NAV_UNSETTLED
        assert camofox_driver.DEGRADED_PRESS_UNSUPPORTED == \
            degraded_mod.DEGRADED_PRESS_UNSUPPORTED


# ─────────────── 端到端：降级不再让整条 run 空跑 ───────────────


class _降级搜索页驱动:
    """搜索页**正文乱码但链接完整**——all80 那 13 条 0 候选空跑的现场形状。

    ⚠️ **一次 ``observe`` 出口，三种页**：按调用序与「有没有点过」区分
    （第 1 次 = 搜索页 ⇒ 进站观察 = 站点页 ⇒ 点击后的观察 = 播放页）。
    真实驱动也是这个形状——同一个 ``snapshot`` 出口按当前页返回。

    ⚠️ **搜索页的 links 必须是三个不同 host**：``extract_candidates``
    按 host 去重，同 host 只留一条 ⇒ 候选数会被压到 1，
    「遍历到站点」这件事就没有可比性了。
    """

    fingerprint: dict[str, object] = {"engine": "camofox"}

    def __init__(self, links, *, degraded=("body_mojibake",)) -> None:
        self._links = tuple(LinkItem(text=t, href=h) for t, h in links)
        self._degraded = tuple(degraded)
        self._obs_n = 0
        self._clicked = False
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.current_url = ""

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        self.current_url = url
        self.visited.append(url)
        # 换页 ⇒ 「点过了」的状态作废（下一个候选的站点页不是播放页）
        self._clicked = False

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        self._obs_n += 1
        if self._obs_n == 1:
            return Observation(
                url=self.current_url,
                page_title="功夫 在线观看_百度搜索",
                body_text="搜索结果页正文",
                links=self._links,
                degraded=self._degraded,
            )
        if self._clicked:
            return Observation(
                url=f"{self.current_url}/play",
                page_title="电影站 - 播放页",
                body_text="正在播放",
                interactive_elements=(
                    InteractiveElement(ref="e1", role="button", name="播放"),
                ),
            )
        return Observation(
            url=self.current_url,
            page_title="电影站",
            body_text="正文",
            interactive_elements=(
                InteractiveElement(ref="e1", role="button", name="播放"),
            ),
        )

    async def extract(self, fields) -> dict[str, list[str]]:
        """结果标题（真实驱动从当前页取）——与 links 同序同 href。"""
        return {
            "titles": [t for t, _ in self._links],
            "urls": [h for _, h in self._links],
        }

    async def click(self, *, ref: str | None = None, **kw) -> None:
        self.clicked.append(ref or "")
        self._clicked = True

    async def fill(self, *, text: str, **kw) -> None: ...

    async def press(self, key: str, **kw) -> None: ...

    async def new_tab(self, url: str | None = None) -> str:
        return "tab-1"

    async def close_tab(self, tab_id: str | None = None) -> None: ...

    async def close(self) -> None: ...


class _端到端感知:
    """脚本化感知层：站点页给**肯定**答案。

    ⚠️ **分类题故意不配** ⇒ 答 ``None`` ⇒ 候选按启发式**全遍历**
    （fail-closed 管的是「结论」不是「采集」）——这正是 all80 那批
    真实跑法（W1 规则版对分类题恒 ``None``）。
    """

    name = "scripted"

    def decide(self, question: str, obs: Observation) -> Decision:
        if question == Q.IS_REACHABLE:
            return Decision(question, True, 1.0, "站点可达", "rule", {})
        if question == Q.FIND_PLAY_CONTROL:
            return Decision(question, True, 1.0, "有播放控件", "rule", {"ref": "e1"})
        if question == Q.PLAYER_OK:
            return Decision(question, True, 1.0, "播放页可用", "rule", {})
        return Decision(question, None, 1.0, "未配置", "rule", {})


class Test端到端_降级不再让整条run空跑:
    """**H-32 验收条件的后半段**：``degraded`` 里含 ``body_mojibake`` 时
    **不再**导致 ``sites == 0``。

    ⚠️ 单测只钉住「候选数 > 0」——**那只是半条**。候选取到了却没被
    遍历（分类把它全 drop、遍历期全跳过、站点页一进站就失败），
    最终 ``sites`` 仍是 0 ⇒ 事故形状（**NOCAND**：有观测无候选）照旧。
    ⇒ 这一组跑**真 orchestrator**，断言的是「账本上真的有站点」。
    """

    async def test_body乱码时整条run仍遍历到站点(self):
        """一条 body 乱码但 links 完整的搜索页 ⇒ 整条 run 走到站点。

        ⚠️ **对照组是「链接那块坏了」**（``links`` 降级）——那种仍
        0 候选、``sites == 0``（**这是对的**：链接坏了候选无从谈起）。
        两条一起读才看得出门禁的**边界**：**正文坏了不否决，链接坏了否决**。
        """
        orch = Orchestrator(
            _降级搜索页驱动(_LINKS), _端到端感知(), RunConfig(),
        )
        rec = await orch.run("T001", "功夫")

        assert rec.candidates, f"候选被清空了：{rec.candidate_filter}"
        assert rec.candidate_filter.get("degraded_other") == 1, (
            f"非致命降级没留痕：{rec.candidate_filter}"
        )
        assert "body_mojibake" in rec.search_obs.degraded, (
            "降级事实没留在观察里"
        )
        # ① 候选确实进了遍历队列
        assert len(rec.visits) == len(rec.candidates), (
            f"候选 {len(rec.candidates)} 个，访问 {len(rec.visits)} 个"
        )
        # ② **账本上有站点**——这就是 H-32 那句「不再导致 sites == 0」
        assert rec.ledger.summary()["total_sites"] >= 1, (
            f"sites == 0——事故形状照旧：{rec.warnings}"
        )
        assert rec.succeeded, f"一个成功都没有：{rec.ledger.summary()}"

    async def test_对照组_链接坏了仍0候选且sites为0(self):
        """**链接那块坏了 ⇒ 仍 0 候选 ⇒ ``sites == 0``**（旧行为保留）。

        ⚠️ 这条是**门禁的另一半**：只钉「乱码不再清空候选」而没有这条
        对照，改成「任何降级都不否决」也会全绿——而那正是
        **链接真的坏了**的场景（all80 的另一种形状）。
        """
        orch = Orchestrator(
            _降级搜索页驱动(_LINKS, degraded=("links",)), _端到端感知(),
            RunConfig(),
        )
        rec = await orch.run("T001", "功夫")

        assert rec.candidates == [], f"链接坏了还有候选：{rec.candidates}"
        assert rec.candidate_filter.get("degraded") == 1
        assert rec.ledger.summary()["total_sites"] == 0, (
            f"链接坏了还遍历出站点：{rec.ledger.summary()}"
        )


# ─────────────── 非致命降级不许混进「剔除」口径 ───────────────


class Test非致命降级不进剔除口径:
    """``degraded_other`` 记的是「有降级但**候选照取**」。

    ⚠️ **它不是「剔除了候选」**——混进「已过滤」或「结构性剔除」
    就会把「候选照取」读成「候选被剔掉了」（凭空多出一条告警）。
    """

    def test_记账键名只有一处(self):
        """键名写在 ``degraded.py``——写侧与两个读侧**用同一个字面量**。"""
        from trajectory_pipeline.executor.steps import search as s2

        assert degraded_mod.FILTER_KEY_DEGRADED_OTHER == "degraded_other"
        assert "degraded_other" in Path(s2.__file__).read_text(encoding="utf-8")

    def test_批次层打印不把它算进已过滤(self, capsys):
        from trajectory_pipeline.executor import cli as cli_mod
        from trajectory_pipeline.executor.orchestrator import RunRecord

        rec = RunRecord(task_id="T001", title="功夫", query="功夫",
                        search_url="https://www.bing.com/search?q=x")
        rec.candidate_filter = {
            degraded_mod.FILTER_KEY_DEGRADED_OTHER: 2,
            "excluded_host:baidu.com": 3,
        }
        cli_mod._print_summary(rec, Path("x.json"))
        out = capsys.readouterr().out
        assert "已过滤 3" in out, f"真剔除的计数丢了：{out}"
        assert "已过滤 5" not in out, f"非致命降级被算进「已过滤」了：{out}"
        assert "降级       = 2" in out, f"降级那一段没单独报出来：{out}"

    def test_批次级断言不因它多出告警(self):
        """B-2 的「剔除动作不可复核」caveat **只对真剔除触发**。"""
        from trajectory_pipeline.evaluation.batch import asserts as asserts_mod

        def _archive(**kw):
            base = {
                "run_config": {"max_candidates": 5},
                "candidates": [{"url": "https://a.test/"}],
                "outcomes": [],
            }
            base.update(kw)
            return base

        r = asserts_mod.assert_run(_archive(
            candidate_filter={degraded_mod.FILTER_KEY_DEGRADED_OTHER: 1},
        )).by_clause("B-2")
        assert not any("不可复核" in c for c in r.caveats), (
            f"非致命降级触发了「剔除不可复核」告警：{r.caveats}"
        )

        r2 = asserts_mod.assert_run(_archive(
            candidate_filter={"excluded_host:baidu.com": 3},
        )).by_clause("B-2")
        assert any("不可复核" in c for c in r2.caveats), (
            "真剔除反而没有告警——口径被改坏了"
        )
