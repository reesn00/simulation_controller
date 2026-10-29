"""**面依赖检查**：同一个 URL，**换一副客户端面孔**，结论会不会变？

**为什么要有这个脚本**（2026-10-29）

起源于 E021（`m.xacjs.com`）那行现场读数「**403 Forbidden\\nnginx**」，5/5 路径全中。
⚠️ 这个读数**分不清两件事**：

* 「**站点自己**不给看」⇒ 该行是**真负样本**（`broken`，进池）；
* 「**只挡我们**」⇒ 该行是**采集侧账**（`blocked`，不进池）。

⚠️ 护栏那侧的分界线是「**人工能进、agent 被拦**」（设计方案 20 §观测面）：
**camofox 是隐身指纹**（camoufox 内核）⇒ 它被拦**不能**外推到「站点看不了」。
⇒ 要判这件事，得换一个**不是隐身指纹**的客户端再看一眼。

⚠️ 换成普通客户端之后，**结论可能翻面**（2026-10-29 实测三例）：

| 目标 | camofox（生产面） | 普通客户端 | 判读 |
|---|---|---|---|
| `kmslxsh.com/v/31445/…` | 404 Not Found | **404 Not Found** | 换面不变 ⇒ **站点事实** ✓ |
| `www.wanminggangguan.com/wan/170472.html` | 404 Not Found | **404 Not Found** | 换面不变 ⇒ **站点事实** ✓ |
| `m.dinggetv.com/dingge/84421.html` | 404 Not Found | **200 OK + 电影页** | 换面变 ⇒ **面的事实**，不是站点事实 ✗ |
| `m.xacjs.com/weihu/1416804.html` | 403 Forbidden | **403 Forbidden** | 换面不变，但**始终没看到站点页面** ⇒ fail-closed ⇒ 不是真负样本 ✗ |

⇒ **「换面会不会变」是判「这是站点的事实还是我们这个面的事实」的那把尺子**。

**这个脚本只记事实，不下结论**：
不改 P1、不改代码、不并池、不动金标，只写一份自己的 `.txt`（`--out`）。

**三种客户端**（同一台机器、同一出口 IP，**只换请求头**）：

| profile | 请求头 | 逼近谁 |
|---|---|---|
| ``chrome`` | 常见桌面 Chrome UA + Accept 头 | 「一个正常浏览器」 |
| ``bare`` | 不带 UA | 「一个什么都不要的客户端」 |
| ``referer`` | chrome + ``Referer: https://www.baidu.com/`` | 「从搜索点进来的浏览器」 |

**红线**：只 GET 公开页、**不登录、不带凭据**；
**响应头只记** `server` / `content-type` / `content-length` / 状态行 ——
**`set-cookie` 一律不记**（仓库红线：不保存 Cookie）。

**用法**（必须在仓库根跑；**联网**）::

    uv run python -m trajectory_pipeline.research.probes.q24_q25_guard_facecheck \\
        --out trajectory_pipeline/research/probes/q24_q25_guard_facecheck_xacjs_2026-10-29.txt

    # 换 URL（可重复）
    uv run python -m trajectory_pipeline.research.probes.q24_q25_guard_facecheck \\
        --url https://example.com/ --out /tmp/probe.txt
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import time
import urllib.parse
from pathlib import Path

#: 默认目标：E021 那条 URL + 4 个 host 级对照路径（**5/5 全 403** 的那组）。
DEFAULT_URLS = [
    "https://m.xacjs.com/weihu/1416804.html",
    "https://m.xacjs.com/",
    "https://m.xacjs.com/index.html",
    "https://m.xacjs.com/weihu/",
    "https://m.xacjs.com/voddetail/1.html",
]

#: 三种客户端的请求头。**没有 cookie、没有任何凭据**。
PROFILES: dict[str, dict[str, str]] = {
    "chrome": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    },
    "bare": {},
    "referer": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.baidu.com/",
    },
}

#: 响应头里**不记**的（红线：不保存 Cookie）。
_SKIP_HEADERS = {"set-cookie", "set-cookie2"}

#: 正文只留这么多字（``--body-chars`` 可调）。
BODY_PREVIEW = 200


def _fetch(url: str, headers: dict[str, str], timeout: float) -> dict:
    """发一次 GET，返回**只含事实**的记录。"""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    ctx = ssl.create_default_context()
    conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                else http.client.HTTPConnection)
    conn = conn_cls(host, port, timeout=timeout, context=ctx) \
        if parts.scheme == "https" else conn_cls(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(4096)
        rec = {
            "status": resp.status,
            "reason": resp.reason,
            "headers": {
                k.lower(): v for k, v in resp.getheaders()
                if k.lower() not in _SKIP_HEADERS
            },
            "body_prefix": raw.decode("utf-8", errors="replace")[:BODY_PREVIEW],
            "bytes_read": len(raw),
        }
        return rec
    except Exception as exc:                      # 事实：连不上也是事实
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    global BODY_PREVIEW
    ap = argparse.ArgumentParser(description="面依赖检查探针（只记事实，不下结论）")
    ap.add_argument("--url", action="append", default=None,
                    help="要探的 URL（可重复；不给就用默认那 5 个）")
    ap.add_argument("--out", default=None, help="落盘路径（.txt）")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--body-chars", type=int, default=BODY_PREVIEW,
                    help="正文预览长度（默认 %d）" % BODY_PREVIEW)
    ap.add_argument("--sleep", type=float, default=0.8,
                    help="两次请求之间的间隔（秒）")
    args = ap.parse_args(argv)

    BODY_PREVIEW = max(0, args.body_chars)

    urls = args.url or DEFAULT_URLS
    lines: list[str] = []
    lines.append("== 面依赖检查 · 同一台机器、同一出口 IP，三种客户端（**只记事实，不下结论**）==")
    lines.append(f"  URL 数 = {len(urls)}   profile = {', '.join(PROFILES)}")
    lines.append("  ⚠ 只记状态行 / server / content-type / 正文前 "
                 f"{BODY_PREVIEW} 字；**set-cookie 不记**（红线）")
    lines.append("")

    records = []
    for url in urls:
        lines.append(f"── {url}")
        for name, headers in PROFILES.items():
            rec = _fetch(url, dict(headers), args.timeout)
            records.append({"url": url, "profile": name, **rec})
            if "error" in rec:
                lines.append(f"  [{name}] 请求失败：{rec['error']}")
            else:
                hh = rec["headers"]
                lines.append(
                    f"  [{name}] HTTP {rec['status']} {rec['reason']}  "
                    f"server={hh.get('server', '-')}  "
                    f"type={hh.get('content-type', '-')}  "
                    f"len={hh.get('content-length', '-')}"
                )
                lines.append(f"      正文前 {BODY_PREVIEW}: "
                             f"{rec['body_prefix']!r}")
            time.sleep(args.sleep)
        lines.append("")

    lines.append("⚠ 上面是**事实**。同一台机器、同一出口 IP，**只换请求头**——")
    lines.append("  · 换面**结论不变**（都 404 / 都 403）⇒ **站点的事实**；")
    lines.append("  · 换面**结论变了**（普通客户端拿到页面）⇒ **我们这个面的事实**，"
                 "不能外推成「站点看不了」；")
    lines.append("  · 换面**仍是拒绝**（都 403）⇒ 我们**始终没看到站点页面** ⇒ "
                 "fail-closed，**不能**写成真负样本。")
    lines.append("  **结论仍需人工裁定**（review --verdicts ... --apply）。")

    text = "\n".join(lines) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        # 产物用 CRLF（对齐 research/probes 下 .txt 的约定）
        out.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        print(f"已写 {out}（{len(records)} 次请求）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
