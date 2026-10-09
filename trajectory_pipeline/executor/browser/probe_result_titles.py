"""搜索结果标题抽取探针——回答一个问题：**``RESULT_SELECTORS`` 还跟得上引擎吗？**

与另外两个探针的分工：
    ``probe_returns.py``   obscura tool 的返回格式（喂 ``dom.py`` 单测）
    ``probe_observe.py``   真实 ``goto + observe`` 的接线
    **本探针**            :mod:`trajectory_pipeline.executor.steps.search` 的
                          ``RESULT_SELECTORS`` 对**当前线上页面**还认不认

为什么值得单独一个探针
----------------------
``browser_links`` 对搜索结果页给的 ``text`` 是**面包屑**而不是标题
（实测 bing 每条是 ``qq.com https://v.qq.com › cover``），于是判断点 ①
拿到的输入根本不支持它那道题——URL 与片名之间没有任何对应关系。
``RESULT_SELECTORS`` 是那条修法，但它**会随引擎改版静默失效**。

失效的表现恰好是「修好了但什么也没变」：抽不到 → 空 dict →
退回面包屑 → ① 照旧 fail-closed。而 fail-closed 是**设计**，
不是故障，所以从报表上看不出来。**只有重跑这个探针能看见。**

探针只验接线，不验业务结论：这里不比「选出的站对不对」，
那要对着真实存档读 evidence（CLAUDE.md：每次真实运行后逐条核对）。

用法::

    export OBSCURA_EXE=/path/to/obscura.exe
    uv run python -m trajectory_pipeline.executor.browser.probe_result_titles
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from trajectory_pipeline.executor.browser.mcp_client import McpClient
from trajectory_pipeline.executor.browser.obscura_driver import ObscuraDriver
from trajectory_pipeline.executor.steps.search import (
    RESULT_SELECTORS,
    build_query,
    result_titles,
    search_url,
)

#: 片名固定用「功夫」——与真批次共用同一个目标，且这个词够短、
#: 不会被「电影频道」这类泛词误命中。
PROBE_TITLE = "功夫"

ENGINES: tuple[str, ...] = ("bing", "baidu")


async def _probe(engine: str) -> tuple[dict[str, str], Any, int, int]:
    """跑一次真实搜索页抽取。返回 ``(titles, raw, 面包屑命中数, 链接总数)``。"""
    # ``async with`` 不是可选的：McpClient 的 initialize 握手在 __aenter__ 里，
    # 漏掉它的症状是每一次 tool 调用都回「MCP 会话未初始化」——
    # 而 goto 的 on_error="raise" 会把它说成「browser_navigate 传输失败」，
    # 与网络、与引擎、与选择器都无关，看着像环境坏了。
    async with McpClient.from_env(timeout=120.0) as client:
        driver = ObscuraDriver(client)
        await driver.goto(search_url(build_query(PROBE_TITLE), engine))
        raw = await driver.extract(RESULT_SELECTORS[engine])
        obs = await driver.observe(max_chars=4000)

    titles = result_titles(raw)
    crumb_hits = sum(1 for link in obs.links if PROBE_TITLE in (link.text or ""))
    return titles, raw, crumb_hits, len(obs.links)


async def main() -> int:
    print(f"== 目标片名：《{PROBE_TITLE}》==\n")
    bad: list[str] = []
    for engine in ENGINES:
        titles, raw, crumb_hits, n_links = await _probe(engine)
        hits = sum(1 for t in titles.values() if PROBE_TITLE in t)
        print(f"-- {engine} --")
        print(f"  selector       : {RESULT_SELECTORS[engine]}")
        print(f"  raw 键         : {sorted(raw)}")
        print(f"  raw 数组长度   : "
              f"{[(k, len(raw[k])) for k in sorted(raw) if isinstance(raw[k], list)]}")
        print(f"  配成 {{url:title}}: {len(titles)} 对，标题含片名的 {hits} 条")
        print(f"  面包屑含片名   : {crumb_hits}/{n_links} 条"
              f"（对照组：这才是判断点 ① 原本拿到的东西）")
        for url, title in list(titles.items())[:5]:
            print(f"    - {title[:52]!r}\n      {url[:88]}")
        if not titles:
            print("  [!] 抽不到标题 —— 选择器已随改版失效，① 会退回 fail-closed")
            bad.append(engine)
        elif hits == 0:
            print("  [!] 配上了但没有一条标题含片名 —— 页面结构变了，不是选择器名的问题")
            bad.append(engine)
        print()

    if bad:
        print(f"结论：{', '.join(bad)} 的 RESULT_SELECTORS 需要重取（见 dom.parse_extract）")
        return 1
    print("结论：全部引擎的选择器仍然有效")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))