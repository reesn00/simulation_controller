# -*- coding: utf-8 -*-
"""B 项诊断探针：iqiyi 正文是「永远拿不到」还是「采集时点太早」。

背景：iqiyi 14 条 body_text 降级（innerText 和 textContent 都空）。
INC43 历史数据显示 iqiyi 页面 body 里 2651 字符全是 JS/CSS（@keyframes/
defaultTrace），剥掉后为空——说明正文由 JS 异步渲染。

本探针直访 iqiyi，分多个时间点（1s/3s/5s/10s/15s/20s）采样：
  - document.body.innerText 长度
  - textContent 剥 script/style 后长度
  - 关键业务词（视频标题/付费墙信号）是否出现
  - document.readyState / 是否有 iframe / 是否有明显的异步渲染标记
看正文到底会不会在某个延迟后出现。
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, ".")

HERE = __import__("pathlib").Path(__file__).resolve().parent
OUT = HERE / "probe_iqiyi_body_timing.jsonl"

# iqiyi 两个视频详情 URL（14 条样本去重后）
TARGETS = [
    ("v_19rrk40at8", "https://www.iqiyi.com/v_19rrk40at8.html"),
    ("v_19rrk40ajc", "https://www.iqiyi.com/v_19rrk40ajc.html"),
]

TIMEOUT_S = 60

# 采样时点（秒）
SAMPLES = [0, 1, 2, 3, 5, 8, 12, 20]

# 业务信号词：视频标题区 / 付费墙 / 播放器
PROBE_JS = """(() => {
  const strip = (root) => {
    const c = root.cloneNode(true);
    c.querySelectorAll('script,style,noscript,template').forEach(n => n.remove());
    return c.textContent || '';
  };
  const it = document.body ? document.body.innerText : '';
  const tc = document.body ? strip(document.body) : '';
  const title = document.title || '';
  const h1 = document.querySelector('h1') ? document.querySelector('h1').innerText : '';
  // 常见视频信息容器
  const sel = ['h1', '.title', '.info', '.video-title', '.tit', '.name'];
  const containers = {};
  for (const s of sel) {
    const el = document.querySelector(s);
    if (el) containers[s] = (el.innerText || '').slice(0, 80);
  }
  return JSON.stringify({
    readyState: document.readyState,
    innerTextLen: it.length,
    textContentStrippedLen: tc.length,
    title: title.slice(0, 60),
    h1: h1.slice(0, 60),
    containers,
    iframes: document.querySelectorAll('iframe').length,
    bodyChildren: document.body ? document.body.children.length : 0,
    // 付费墙/播放器信号词
    hasVIP: /VIP|会员|开通|付费|订阅|专享/.test(it + tc + title),
    hasPlay: /播放|正片|选集|剧集|立即观看/.test(it + tc + title),
  });
})()"""


async def main():
    from trajectory_pipeline.executor.browser.mcp_client import McpClient
    from trajectory_pipeline.executor.browser.obscura_driver import ObscuraDriver

    client = McpClient.from_env()
    async with client:
        driver = ObscuraDriver(client)
        with OUT.open("w", encoding="utf-8") as fh:
            for (tag, url) in TARGETS:
                rec = {"tag": tag, "url": url, "samples": []}
                try:
                    t0 = time.monotonic()
                    await asyncio.wait_for(driver.goto(url), timeout=TIMEOUT_S)
                    for delay in SAMPLES:
                        if delay > 0:
                            await asyncio.sleep(delay)
                        try:
                            inner = await driver._call(
                                "browser_evaluate", {"expression": PROBE_JS},
                                on_error="empty")
                            # inner 可能是 str 或 dict，尝试解析
                            if isinstance(inner, str):
                                try:
                                    data = json.loads(inner)
                                except Exception:
                                    data = {"raw": inner[:200]}
                            else:
                                data = inner
                            data["elapsed"] = round(time.monotonic() - t0, 1)
                            rec["samples"].append(data)
                            print(f"  [{tag}] t={delay}s elapsed={data.get('elapsed')} "
                                  f"innerText={data.get('innerTextLen')} "
                                  f"stripped={data.get('textContentStrippedLen')} "
                                  f"hasVIP={data.get('hasVIP')} hasPlay={data.get('hasPlay')}",
                                  flush=True)
                        except Exception as e:
                            rec["samples"].append(
                                {"elapsed": round(time.monotonic() - t0, 1),
                                 "error": f"{type(e).__name__}: {e}"})
                            print(f"  [{tag}] t={delay}s ERROR: {e}", flush=True)
                except Exception as exc:
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                    print(f"[{tag}] goto FAIL: {exc}", flush=True)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
    print("完成 ->", OUT)


if __name__ == "__main__":
    asyncio.run(main())
