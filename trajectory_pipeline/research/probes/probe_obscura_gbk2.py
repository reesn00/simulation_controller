# -*- coding: utf-8 -*-
"""A 项诊断2：定位 sohu 乱码发生在浏览器端还是传输层。

在浏览器内：
1. document.body.innerText 的字符，逐个 charCodeAt 看乱码位置的码点
2. 检查这些位置在 DOM 里对应节点的 textContent（是否也是 U+FFFD）
3. 检查 document.characterSet / meta charset
如果浏览器端 charCodeAt 就是 0xFFFD，说明浏览器解码时就坏了（HTML 字节本身有问题）；
如果浏览器端码点正常（如中文字符），说明是 obscura MCP 传输层引入的。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

HERE = Path(__file__).resolve().parent
TIMEOUT_S = 90.0
OUT = HERE / "probe_obscura_gbk2.jsonl"

TARGETS = [
    ("E014", "https://tv.sohu.com/v/MjAwOTEyMzEvbjI2OTMyODE3NC5zaHRtbA==.html"),
]

# 在浏览器内找「简介」附近的内文，返回码点
PROBE_EXPR = r"""
(() => {
  const t = document.body.innerText;
  // 找第一个 U+FFFD 的位置
  const idx = t.indexOf('\uFFFD');
  let around = '';
  if (idx >= 0) {
    around = t.slice(Math.max(0, idx-5), idx+10);
  }
  // 找「简介」二字后面的片段（已知乱码位置）
  const jj = t.indexOf('简介');
  let afterIntro = '';
  if (jj >= 0) {
    afterIntro = t.slice(jj, jj+20);
  }
  return JSON.stringify({
    cs: document.characterSet,
    ie: document.inputEncoding,
    metaCharset: (document.querySelector('meta[charset]')||{}).getAttribute ? (document.querySelector('meta[charset]')||{}).getAttribute('charset') : null,
    fffdIdx: idx,
    fffdAround: around,
    afterIntro: afterIntro,
    // 关键：把「简介」后20字逐字符码点
    afterIntroCodes: afterIntro ? afterIntro.split('').map(c => c.charCodeAt(0).toString(16)) : [],
  });
})()
"""


async def main():
    from trajectory_pipeline.executor.browser.mcp_client import McpClient
    from trajectory_pipeline.executor.browser.obscura_driver import ObscuraDriver

    client = McpClient.from_env()
    async with client:
        driver = ObscuraDriver(client)
        with OUT.open("w", encoding="utf-8") as fh:
            for task_id, url in TARGETS:
                rec = {"task_id": task_id, "url": url}
                try:
                    await asyncio.wait_for(driver.goto(url), timeout=TIMEOUT_S)
                    r = await asyncio.wait_for(
                        driver._mc.call("browser_evaluate", {"expression": PROBE_EXPR}),
                        timeout=TIMEOUT_S)
                    rec["probe"] = r.text.strip()[:600]
                except Exception as exc:
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                print(json.dumps(rec, ensure_ascii=False), flush=True)
    print("完成 ->", OUT)


if __name__ == "__main__":
    asyncio.run(main())
