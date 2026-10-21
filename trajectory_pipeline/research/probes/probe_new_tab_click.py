"""探针：会话中途新开的 tab 上**点击挂死**——定位到「哪个环节不返回」。

症状（P5 双跑实测）：camoufox 面上 `Locator.click` 停在
``performing click action`` 然后 5s 超时，而 obscura 同一站点能点；
同一 URL 作为**会话首页**打开就点得动（0.74s）。这是 P5 切换的唯一硬阻塞。

本探针不猜机理，只做三件事：

1. **复现**：会话首页点得动、中途新开页点不动——用本地 ``set_content``
   页面（无网络、无站点 JS）验证「换 tab」这一个变量**是否足够**；
2. **切分**：挂死发生在哪一层——定位器解析（``evaluate``）/ JS 派发
   （``el.click()``）/ 鼠标原语（``move`` / ``down`` / ``up``，分开测）/
   键盘（``Enter``）；
3. **错投检测**：p1 与 p2 各放一个全屏按钮 + 各自计数器。若 p2 的点击
   被投到 p1，``p1.__clicks`` 会涨——这条能一票判定「事件没送到这一页」。

每条动作都有硬超时（外层 ``asyncio.wait_for``）：**挂死是读数，不是事故**。
一个场景一次 launch（互不污染），单场景出错只记进结果、不影响后续。

用法（必须在仓库根跑）::

    uv run python trajectory_pipeline/research/probes/probe_new_tab_click.py

产物：同目录 ``new_tab_click.json``（证据，不放结论）。
"""

from __future__ import annotations

import asyncio
import json
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any, Awaitable, Callable

from playwright.async_api import TimeoutError as PWTimeout

from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession

#: 动作超时**压到 1.5s**：这里量的是「返回不返回」，不是「多久合理」。
#: 生产是 5s（``camoufox_driver.ACTION_TIMEOUT_MS``），压短只影响读数不影响判定。
ACTION_MS = 1500

#: 外层兜底。比 :data:`ACTION_MS` 大得多——只为「playwright 自己不返回时
#: 探针别跟着挂住」，不该被它触发。
HARD_WAIT = 20.0

OUT = Path(__file__).with_name("new_tab_click.json")

#: 全屏按钮页。两个计数器分开记：
#: - ``__clicks``   —— ``click`` 事件（``el.click()`` 也会涨，故它只证明「页面活着」）；
#: - ``__pointerdown`` —— 真实指针输入（``el.click()`` **不会**让它涨）：
#:   它涨了才是「事件真的送到了这一页」。
BUTTONS = """<!doctype html><html><head><meta charset="utf-8">
<title>__TITLE__</title></head>
<body style="margin:0">
<button id="b" style="position:fixed;left:0;top:0;width:100%;height:100%;
font-size:28px;z-index:2">立即播放</button>
<script>
window.__clicks = 0; window.__pointerdown = 0; window.__keydown = 0;
document.getElementById('b').addEventListener('click', () => { window.__clicks += 1; });
document.addEventListener('pointerdown', () => { window.__pointerdown += 1; }, true);
document.addEventListener('keydown', () => { window.__keydown += 1; }, true);
</script></body></html>"""

STATE_JS = """() => ({
  vis: document.visibilityState,
  focus: document.hasFocus(),
  clicks: window.__clicks,
  pointerdown: window.__pointerdown,
  keydown: window.__keydown,
})"""

#: 真实站点交叉验证用：`body` 一定存在，避免选择器脆弱性盖住变量。
REAL_URL = "https://jijikb.com/vod/20056.html"

RESULTS: list[dict[str, Any]] = []


def _brief(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:200]


class Rec:
    """单场景的记录器：每步一条，最后整场景写进 ``RESULTS``。"""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.steps: list[dict[str, Any]] = []

    def value(self, label: str, value: Any) -> None:
        self.steps.append({"step": label, "status": "value", "value": value})

    def state(self, label: str, value: dict[str, Any]) -> None:
        self.steps.append({"step": label, "status": "state", **value})

    def outcome(self, label: str, status: str, t0: float,
                **extra: Any) -> None:
        self.steps.append({
            "step": label, "status": status,
            "ms": round((time.perf_counter() - t0) * 1000),
            **extra,
        })

    def done(self) -> None:
        RESULTS.append({"scenario": self.scenario, "steps": self.steps})


async def _try(rec: Rec, label: str,
                fn: Callable[[], Awaitable[Any]]) -> None:
    """跑一个动作：超时/异常都记成读数，**不往上抛**。"""
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(fn(), timeout=HARD_WAIT)
    except asyncio.TimeoutError:                 # 连 playwright 都没返回
        rec.outcome(label, "hard_timeout", t0)
        return
    except PWTimeout as exc:
        rec.outcome(label, "pw_timeout", t0, msg=_brief(exc))
        return
    except Exception as exc:                     # noqa: BLE001 — 探针要全收
        rec.outcome(label, f"error:{type(exc).__name__}", t0, msg=_brief(exc))
        return
    rec.outcome(label, "ok", t0)


async def _state(page: Any) -> dict[str, Any]:
    try:
        return dict(await page.evaluate(STATE_JS))
    except Exception as exc:                     # noqa: BLE001
        return {"error": _brief(exc)}


async def _pair(session: CamoufoxSession) -> tuple[Any, Any]:
    """p1 = **会话首页**（launch 时就有的那张）；p2 = 中途新开的一页。

    两条都用本地内容——**没有网络、没有站点 JS**。若这样也能复现，
    说明「换 tab」这一个变量就足够，站点脚本/遮罩/iframe 都不是必要条件。
    """
    p1 = session.page
    await p1.set_content(BUTTONS.replace("__TITLE__", "first"))
    p2 = await session.new_page()                # 与 driver.new_tab 同一条路径
    await p2.set_content(BUTTONS.replace("__TITLE__", "second"))
    return p1, p2


async def _click(page: Any, *, force: bool = False) -> None:
    await page.locator("#b").click(timeout=ACTION_MS, force=force)


# ── 场景 ────────────────────────────────────────────────────────


async def sc_first_page_click(session: CamoufoxSession, rec: Rec) -> None:
    """基线：会话首页点得动吗？（P5 双跑里首页是好的，这里要能复现这个「好」）"""
    p1 = session.page
    await p1.set_content(BUTTONS.replace("__TITLE__", "first"))
    rec.state("p1_before", await _state(p1))
    await _try(rec, "p1_click", lambda: _click(p1))
    rec.state("p1_after", await _state(p1))


async def sc_second_page_click(session: CamoufoxSession, rec: Rec) -> None:
    """主场景：新开页点击 + **错投检测**（p2 的点击有没有掉进 p1）。"""
    p1, p2 = await _pair(session)
    rec.value("pages", len(session.pages))
    rec.state("p1_after_open", await _state(p1))
    rec.state("p2_after_open", await _state(p2))

    # 定位器本身能不能用（挂死是发生在 resolved 之后，这里先排掉「找不到元素」）
    await _try(rec, "p2_locator_read_tag",
               lambda: p2.locator("#b").evaluate("el => el.tagName"))

    await _try(rec, "p2_click_1", lambda: _click(p2))
    rec.state("p1_after_p2_click", await _state(p1))   # ← 错投会在这里露出来
    rec.state("p2_after_p2_click", await _state(p2))

    # 第二次点击：一键区分「一次性激活问题」与「这一页永远收不到输入」
    await _try(rec, "p2_click_2", lambda: _click(p2))
    rec.state("p2_after_p2_click2", await _state(p2))


async def sc_js_click(session: CamoufoxSession, rec: Rec) -> None:
    """JS 派发：证明**页面活着、监听器在**——挂的只是「真实输入」。"""
    _, p2 = await _pair(session)
    await _try(rec, "p2_js_click",
               lambda: p2.evaluate("document.getElementById('b').click()"))
    rec.state("p2_after_js_click", await _state(p2))


async def sc_mouse_primitives(session: CamoufoxSession, rec: Rec) -> None:
    """把 ``click`` 拆成三个原语：移 / 按下 / 抬起，看是哪一步不返回。

    ``down`` 不返回 = 浏览器侧输入派发本身挂住（我们这层之上）；
    ``move`` 就挂 = 连指针定位都不通。
    """
    p1, p2 = await _pair(session)
    await _try(rec, "p2_mouse_move", lambda: p2.mouse.move(300, 300))
    rec.state("p1_after_move", await _state(p1))
    rec.state("p2_after_move", await _state(p2))
    await _try(rec, "p2_mouse_down", lambda: p2.mouse.down())
    await _try(rec, "p2_mouse_up", lambda: p2.mouse.up())
    rec.state("p2_after_mouse", await _state(p2))


async def sc_bring_to_front(session: CamoufoxSession, rec: Rec) -> None:
    """``bring_to_front`` 之后还挂不挂——「后台 tab 收不到输入」假设的直接检验。"""
    p1, p2 = await _pair(session)
    await _try(rec, "p2_bring_to_front", lambda: p2.bring_to_front())
    rec.state("p1_after_front", await _state(p1))
    rec.state("p2_after_front", await _state(p2))
    await _try(rec, "p2_click", lambda: _click(p2))
    rec.state("p2_after_click", await _state(p2))


async def sc_close_first(session: CamoufoxSession, rec: Rec) -> None:
    """关掉首页再点——「同时存在两页」是不是必要条件。"""
    p1, p2 = await _pair(session)
    await _try(rec, "close_p1", lambda: session.close_page(p1))
    rec.value("pages", len(session.pages))
    rec.state("p2_after_close", await _state(p2))
    await _try(rec, "p2_click", lambda: _click(p2))
    rec.state("p2_after_click", await _state(p2))


async def sc_force_click(session: CamoufoxSession, rec: Rec) -> None:
    """``force=True``：跳过可操作性等待后还挂不挂（跳过的是检查，不是派发）。"""
    _, p2 = await _pair(session)
    await _try(rec, "p2_force_click", lambda: _click(p2, force=True))
    rec.state("p2_after_force", await _state(p2))


async def sc_keyboard(session: CamoufoxSession, rec: Rec) -> None:
    """键盘：与鼠标无关的另一条输入通道。焦点先给按钮，再按 Enter。

    Enter 会触发按钮的 ``click``；``keydown`` 计数则说明按键有没有落地。
    """
    _, p2 = await _pair(session)
    await _try(rec, "p2_focus", lambda: p2.locator("#b").focus())
    await _try(rec, "p2_press_enter",
               lambda: p2.keyboard.press("Enter", timeout=ACTION_MS))
    rec.state("p2_after_enter", await _state(p2))


async def sc_real_site(session: CamoufoxSession, rec: Rec) -> None:
    """真实站点交叉验证：同一 URL，首页 vs 新开页，点 ``body``。

    用 ``body`` 是为了让选择器**不参与**判定——这一条只回答
    「真站点上是不是也按 tab 分好坏」。
    """

    async def click_body(page: Any) -> None:
        await page.locator("body").click(
            position={"x": 200, "y": 200}, timeout=ACTION_MS)

    p1 = session.page
    await _try(rec, "p1_goto_real", lambda: p1.goto(REAL_URL, wait_until="load"))
    await _try(rec, "p1_click_body", lambda: click_body(p1))

    p2 = await session.new_page()
    await _try(rec, "p2_goto_real", lambda: p2.goto(REAL_URL, wait_until="load"))
    await _try(rec, "p2_click_body", lambda: click_body(p2))


#: (场景名, 函数, headless)
SCENARIOS: list[tuple[str, Callable[[CamoufoxSession, Rec], Awaitable[None]], bool]] = [
    ("first_page_click", sc_first_page_click, False),
    ("second_page_click", sc_second_page_click, False),
    ("js_click", sc_js_click, False),
    ("mouse_primitives", sc_mouse_primitives, False),
    ("bring_to_front", sc_bring_to_front, False),
    ("close_first", sc_close_first, False),
    ("force_click", sc_force_click, False),
    ("keyboard", sc_keyboard, False),
    ("real_site", sc_real_site, False),
    ("second_page_headless", sc_second_page_click, True),
]


async def _run(name: str,
               fn: Callable[[CamoufoxSession, Rec], Awaitable[None]],
               headless: bool) -> None:
    rec = Rec(name)
    session: CamoufoxSession | None = None
    try:
        session = await CamoufoxSession.launch(headless=headless)
        rec.value("headless", headless)
        await fn(session, rec)
    except Exception as exc:                     # noqa: BLE001
        rec.outcome("scenario", f"failed:{type(exc).__name__}", time.perf_counter(),
                    msg=_brief(exc))
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception as exc:             # noqa: BLE001
                rec.outcome("close", f"failed:{type(exc).__name__}",
                            time.perf_counter(), msg=_brief(exc))
        rec.done()
        print(f"-- {name}: " + ", ".join(
            f"{s['step']}={s.get('status', '')}" for s in rec.steps
            if s.get("status") not in ("value", "state")), flush=True)


async def main() -> None:
    env = {
        "playwright": version("playwright"),
        "camoufox": version("camoufox"),
        "python": ".".join(map(str, __import__("sys").version_info[:3])),
    }
    print("env:", env, flush=True)
    for name, fn, headless in SCENARIOS:
        await _run(name, fn, headless)
    OUT.write_text(json.dumps({"env": env, "results": RESULTS},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())