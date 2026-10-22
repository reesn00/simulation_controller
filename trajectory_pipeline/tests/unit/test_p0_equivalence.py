"""P0 的「零行为变化」——**离线可复跑的等价性门禁**（2026-10-21）。

P0 是什么
----------
2026-10-20 的 P0 把 ``executor/cli.py`` 里 **6 处**硬编码
``ObscuraDriver(client)`` 收敛到 ``make_driver``（设计方案 04 §2.3）。
当时的验收判据是「**存档逐字节同形**」。

⚠️ **那条判据本身写错了，此处作废并换掉**
------------------------------------------------
「跑两次 ``run``，比存档的哈希」**分不出**「重构改了行为」与
「站点变了」这两件事：

- 同一个 task 连跑两次，**搜出来的结果页就不是同一份**——广告位、
  时间戳、推荐位都在动；
- 于是判据**没法判**：重构完全正确它也会红，它红了也没人知道该怪谁。

**一条分不出「谁造成的」的判据不是判据。** 换掉它。

现在的判据：**同一份输入 ⇒ 同一份存档**
----------------------------------------
把输入从「活着的站点」换成「**录下来的驱动应答**」——
:class:`FakeMcp` 按 tool 名返回固定文本，于是整条链（驱动 → 编排 →
存档）**完全确定**。这时两条装配路径（现行入口 vs P0 前的内联构造）
产出的存档**必须逐字节相同**：任何差异**都是重构造成的**，
没有别的解释。

判据拆成三条
------------
1. **构造等价**：``make_driver(client)`` 与 ``ObscuraDriver(client)``
   产出的对象逐属性相同（装配入口不许偷偷加默认参数）。
2. **存档等价**：同一份假 MCP 应答下，两条路径产出的存档
   **逐字节相同**（只屏蔽时间与身份那几个必然变的字段）。
3. **装配入口不许长出第三个入参**：AST 判——obscura 分支只准把
   ``client`` 与 ``collect_element_basis`` 传下去。多传一个就是
   「有人给所有调用点偷偷改了几个字的默认行为」。

⚠️ **第 2 条只证明「装配没改行为」**——它**不**证明
「重构前的存档与重构后的存档一样」，因为那需要真的跑一次活的站点
（而站点会动）。**能证明的那部分，本文件证明；不能证明的那部分，
不假装能证明。**
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_pipeline.executor.browser.driver_factory import make_driver
from trajectory_pipeline.executor.browser.mcp_client import ToolResult
from trajectory_pipeline.executor.browser.obscura_driver import ObscuraDriver
from trajectory_pipeline.executor.orchestrator import Orchestrator, RunConfig
from trajectory_pipeline.perception.base import Decision, Q

ROOT = Path(__file__).resolve().parents[2]          # trajectory_pipeline/
FACTORY_SRC = ROOT / "executor" / "browser" / "driver_factory.py"


# ── 录下来的驱动应答 ────────────────────────────────────────────────


class FakeMcp:
    """按 tool 名返回预设文本。缺失的 tool 抛 :class:`McpError`。

    与 ``test_obscura_driver.py`` 同款——**同一份应答在两条装配路径上
    各跑一次**，输入相同才有「输出必须相同」这回事。
    """

    def __init__(self, script: dict[str, str]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        args = args or {}
        self.calls.append((name, args))
        if name not in self.script:
            raise AssertionError(f"fake: {name} 未配置")
        return ToolResult(text=self.script[name], is_error=False, structured=None,
                          latency_ms=1)


#: obscura 各 tool 的**实测返回形态**（骨架取自 ``test_obscura_driver.py``）。
SNAPSHOT = (
    "URL: https://www.hao123.com/\n"
    "Title: hao123_上网从这里开始\n"
    "Snapshot:\n<body><style>.a{color:red}</style><div>功夫 在线观看</div></body>"
)
INNER_TEXT = "hao123_上网从这里开始\n功夫 在线观看"
LINKS = (
    '{"text":"功夫","href":"https://v.qq.com/x/cover/abcd.html"}\n'
    '{"text":"hao123","href":"https://www.hao123.com/"}'
)
#: 列间是**多个空格**、首列带 ``ref=`` 前缀（实测口径）。
ELEMENTS = 'ref=e1    a                      "功夫"\nref=e2    button                  "在线观看"'


def _script() -> dict[str, str]:
    return {
        "browser_snapshot": SNAPSHOT,
        "browser_evaluate": INNER_TEXT,
        "browser_links": LINKS,
        "browser_interactive_elements": ELEMENTS,
        "browser_count": "1",
        "browser_navigate": "OK",
    }


class _ScriptedPerceptor:
    """按题返回预设答案。**不用 LLM、不联网**——两次跑必须完全一样。"""

    name = "scripted"

    def __init__(self, script: dict[str, Decision]) -> None:
        self.script = script
        self.calls: list[str] = []

    def decide(self, question: str, obs: Any) -> Decision:
        self.calls.append(question)
        d = self.script.get(question)
        if d is None:
            return Decision(question, None, 1.0, "未配置", "rule", {})
        return Decision(question, d.answer, 1.0, d.evidence, "rule", d.payload)


def _ok(question: str, payload: dict | None = None) -> Decision:
    return Decision(question, True, 1.0, "e", "rule", payload or {})


def _perceptor() -> _ScriptedPerceptor:
    return _ScriptedPerceptor({
        Q.CLASSIFY_SEARCH_LINKS: _ok(Q.CLASSIFY_SEARCH_LINKS, {
            "video_site": [{"url": "https://v.qq.com/x/cover/abcd.html",
                            "why": "标题含片名"}],
        }),
        Q.IS_REACHABLE: _ok(Q.IS_REACHABLE),
        Q.FIND_PLAY_CONTROL: _ok(Q.FIND_PLAY_CONTROL),
        Q.PLAYER_OK: _ok(Q.PLAYER_OK),
    })


# ── 存档怎么比 ──────────────────────────────────────────────────────

#: **必然变**的字段：时间与身份。其余字段**必须**一样。
#:
#: 抹多了就等于「不比」；抹少了会因为计时抖动假红。判据是
#: 「两次跑同一份输入，这些字段必然不同，其余字段必然相同」。
_VOLATILE = frozenset({
    "run_id", "batch_id", "elapsed_ms", "duration_ms", "latency_ms",
})


def _mask(value: Any) -> Any:
    """递归抹掉 :data:`_VOLATILE` 里的键。**只抹这些**。"""
    if isinstance(value, dict):
        return {k: _mask(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def _canonical(archive: dict) -> str:
    """存档的**可比较形式**：屏蔽易变字段 + 键排序 + 无空格分隔。"""
    return json.dumps(_mask(archive), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


async def _run_with(make_driver_call) -> dict:
    """用给定的装配方式跑一条完整的 run，返回存档。"""
    driver = make_driver_call(FakeMcp(_script()))
    orch = Orchestrator(driver, _perceptor(), RunConfig())
    record = await orch.run("P001", "功夫")
    return record.to_json()


# ── 判据 1 · 构造等价 ───────────────────────────────────────────────


class Test装配入口与内联构造等价:
    def test_两个构造出的驱动逐属性相同(self):
        """``make_driver(client)`` ≡ ``ObscuraDriver(client)``。

        逐属性比，**不比 ``id``**——那是对象身份，不是行为。
        """
        client = FakeMcp(_script())
        a = make_driver(client)
        b = ObscuraDriver(client)
        assert type(a) is type(b)
        assert a.__dict__ == b.__dict__

    def test_默认调用不采元素账目(self):
        """``collect_element_basis`` **默认关**。

        开着会多一次 ``browser_evaluate`` 往返，存档里多一列——
        那正是「装配入口偷偷改了行为」的样子。**默认必须是关的。**
        """
        driver = make_driver(FakeMcp(_script()))
        assert driver._collect_element_basis is False


# ── 判据 2 · 存档等价 ───────────────────────────────────────────────


class Test存档逐字节同形:
    async def test_两条装配路径产出同一份存档(self):
        """现行入口 vs P0 前的内联构造——**同一份存档**。

        输入是录下来的驱动应答，所以**站点不动**；任何差异都是
        装配路径造成的。
        """
        via_factory = await _run_with(lambda c: make_driver(c))
        via_inline = await _run_with(lambda c: ObscuraDriver(c))
        assert _canonical(via_factory) == _canonical(via_inline), (
            "装配入口与内联构造产出了不同的存档——"
            "P0 的「零行为变化」已经不成立了"
        )

    async def test_存档不是空的(self):
        """**别让这条测试空转**。

        两边都产出 ``{}`` 时上面那条会绿——那是什么都没比。
        这里钉住「存档里真的有东西」：有搜索观察、有动作流、有分支。
        """
        archive = await _run_with(lambda c: make_driver(c))
        assert archive["search_observation"], "搜索观察没落进存档——比的是空壳"
        assert archive["steps"], "动作流没落进存档——比的是空壳"
        assert archive["outcomes"], "分支没落进存档——比的是空壳"


# ── 判据 3 · 装配入口不许长出第三个入参 ─────────────────────────────


class Test装配入口不许偷改:
    def test_obscura分支只传client与元素账目开关(self):
        """AST 判：**不许多传**。

        装配入口每多传一个参数，就是**所有调用点**的行为被悄悄改了
        ——而这种改动在报告上看不出任何异常。允许传的只有两个：
        ``client`` 与 ``collect_element_basis``。
        """
        tree = ast.parse(FACTORY_SRC.read_text(encoding="utf-8-sig"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ObscuraDriver"
        ]
        assert calls, "driver_factory 里找不到 ObscuraDriver 的构造——P0 的等价性无从谈起"
        for call in calls:
            assert len(call.args) == 1, (
                f"ObscuraDriver 的构造多传了位置参数："
                f"{[ast.dump(a) for a in call.args[1:]]}"
            )
            kwargs = {kw.arg for kw in call.keywords if kw.arg}
            assert kwargs <= {"collect_element_basis"}, (
                f"ObscuraDriver 的构造多传了关键字参数：{sorted(kwargs - {'collect_element_basis'})}"
            )
