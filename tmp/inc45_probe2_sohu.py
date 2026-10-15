# -*- coding: utf-8 -*-
"""簇 B 二轮探针：乱码形态 + round-trip 恢复可行性 + fetch 调试。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

TIMEOUT_S = 90.0
OUT = Path("tmp/inc45_probe2_sohu.jsonl")

TARGETS = [
    ("E014", "https://tv.sohu.com/v/MjAwOTEyMzEvbjI2OTMyODE3NC5zaHRtbA==.html"),
]

FETCH_DEBUG_EXPR = r"""
(async () => {
  const r = await fetch(document.URL, {credentials: 'include'});
  const buf = new Uint8Array(await r.arrayBuffer());
  let hex = '';
  for (let i = 0; i < Math.min(buf.length, 48); i++) hex += buf[i].toString(16).padStart(2, '0') + ' ';
  return JSON.stringify({status: r.status, ct: r.headers.get('content-type'), n: buf.length, head: hex, url: r.url});
})()
"""

STRONG_KWS = ["VIP", "订阅", "续费", "包月", "包年", "专享", "特惠"]
WEAK_KWS = ["开通", "会员"]


def signals(text):
    return {
        "strong": [k for k in STRONG_KWS if k in text],
        "weak": [k for k in WEAK_KWS if k in text],
        "fffd": text.count("\ufffd"),
        "len": len(text),
    }


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
                    obs = await asyncio.wait_for(
                        driver.observe(max_chars=6000), timeout=TIMEOUT_S
                    )
                    body = obs.body_text or ""
                    rec["dom_excerpt"] = body[:400]
                    rec["dom_signals"] = signals(body)
                    # round-trip：乱码串按 GBK 编回字节、按 UTF-8 重解
                    try:
                        rt = body.encode("gbk", errors="ignore").decode(
                            "utf-8", errors="replace"
                        )
                        rec["rt_signals"] = signals(rt)
                        rec["rt_excerpt"] = rt[:400]
                    except Exception as exc:
                        rec["rt_error"] = f"{type(exc).__name__}: {exc}"
                    # fetch 调试
                    r = await asyncio.wait_for(
                        driver._mc.call(
                            "browser_evaluate", {"expression": FETCH_DEBUG_EXPR}
                        ),
                        timeout=TIMEOUT_S,
                    )
                    rec["fetch_debug_raw"] = r.text.strip()[:500]
                except Exception as exc:
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                print("done", flush=True)
    print("完成 ->", OUT)


if __name__ == "__main__":
    asyncio.run(main())
