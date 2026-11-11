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
from trajectory_pipeline.executor.steps import search as search_step
from trajectory_pipeline.perception.base import LinkItem, Observation

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
