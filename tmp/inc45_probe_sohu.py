# -*- coding: utf-8 -*-
"""簇 B 前研探针：tv.sohu.com mojibake 形态 + 恢复可行性。

三个问题：
1. 浏览器认为页面是什么编码（characterSet / inputEncoding / meta charset）？
2. DOM 正文里 U+FFFD 有多少（真实乱码 vs 传输层引入）？
3. 原始字节能否通过 in-browser fetch → base64 取回、本地按正确
   charset 重解码得到可读正文（含付费墙词）？
"""
import asyncio
import base64
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

TIMEOUT_S = 90.0
OUT = Path("tmp/inc45_probe_sohu.jsonl")

TARGETS = [
    ("E014", "https://tv.sohu.com/v/MjAwOTEyMzEvbjI2OTMyODE3NC5zaHRtbA==.html"),
    ("E023", "https://tv.sohu.com/v/MjAxMjA3MjYvbjM0OTExNDM1Mi5zaHRtbA==.html"),
    ("TPIN02", "https://tv.sohu.com/v/MjAxNTAxMTkvbjQwNzg5MTgxOC5zaHRtbA==.html?txid=2501c78c3e10c8cdcbc7f701c70db188"),
]

#: 取回原始字节的浏览器侧表达式：content-type + meta charset + 截断字节的 b64。
#: 截 300KB——sohu 播放页正文（含付费墙文案）在 HTML 前部。
FETCH_BYTES_EXPR = r"""
(async () => {
  const r = await fetch(document.URL, {credentials: 'include'});
  const ct = r.headers.get('content-type') || '';
  const buf = new Uint8Array(await r.arrayBuffer()).subarray(0, 300000);
  let bin = '';
  for (let i = 0; i < buf.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
  }
  const meta = document.querySelector('meta[charset]');
  const metaCs = meta ? meta.getAttribute('charset') : (document.querySelector('meta[http-equiv="Content-Type"]') || '').content || '';
  return JSON.stringify({ct: ct, metaCs: metaCs, n: buf.length, b64: btoa(bin)});
})()
"""

CHARSET_EXPR = (
    "JSON.stringify({cs: document.characterSet, ie: document.inputEncoding})"
)

STRONG_KWS = ["VIP", "订阅", "续费", "包月", "包年", "专享", "特惠"]
WEAK_KWS = ["开通", "会员"]


def strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"(?s)<[^>]+>", " ", html)


def signals(text: str) -> dict:
    return {
        "strong": [k for k in STRONG_KWS if k in text],
        "weak": [k for k in WEAK_KWS if k in text],
        "fffd": text.count("\ufffd"),
    }


async def main():
    from trajectory_pipeline.executor.browser.mcp_client import McpClient
    from trajectory_pipeline.executor.browser.obscura_driver import ObscuraDriver

    client = McpClient.from_env()
    done = 0
    async with client:
        driver = ObscuraDriver(client)
        with OUT.open("w", encoding="utf-8") as fh:
            for task_id, url in TARGETS:
                rec = {"task_id": task_id, "url": url}
                try:
                    await asyncio.wait_for(driver.goto(url), timeout=TIMEOUT_S)
                    r = await asyncio.wait_for(
                        driver._mc.call(
                            "browser_evaluate", {"expression": CHARSET_EXPR}
                        ),
                        timeout=TIMEOUT_S,
                    )
                    rec["browser_charset"] = r.text.strip()[:200]

                    obs = await asyncio.wait_for(
                        driver.observe(max_chars=6000), timeout=TIMEOUT_S
                    )
                    rec["dom_body_len"] = len(obs.body_text or "")
                    rec["dom_fffd"] = (obs.body_text or "").count("\ufffd")
                    rec["degraded"] = list(obs.degraded or ())

                    r2 = await asyncio.wait_for(
                        driver._mc.call(
                            "browser_evaluate", {"expression": FETCH_BYTES_EXPR}
                        ),
                        timeout=TIMEOUT_S,
                    )
                    info = json.loads(r2.text.strip())
                    rec["content_type"] = info.get("ct")
                    rec["meta_charset"] = info.get("metaCs")
                    raw = base64.b64decode(info.get("b64") or "")
                    rec["raw_len"] = len(raw)
                    # 本地按 GBK 解码（sohu 历史形态），剥标签后验信号
                    gbk_text = strip_tags(raw.decode("gbk", errors="replace"))
                    rec["gbk"] = signals(gbk_text)
                    rec["gbk_excerpt"] = gbk_text[:300]
                    utf_text = strip_tags(raw.decode("utf-8", errors="replace"))
                    rec["utf8"] = signals(utf_text)
                except Exception as exc:
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                done += 1
                print(f"[{done}/{len(TARGETS)}] {task_id} done", flush=True)
    print("完成 ->", OUT)


if __name__ == "__main__":
    asyncio.run(main())
