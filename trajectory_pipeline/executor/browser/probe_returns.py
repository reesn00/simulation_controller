"""探测 obscura 各 tool 的**真实返回格式**——用于固化 ``Observation`` 的字段设计。

为什么不凭 tool 描述猜：``browser_links`` 官方说是 NDJSON、
``browser_interactive_elements`` 只说"稳定 ref"，
但 ref 到底长什么样、snapshot 的标题/正文怎么分隔、`browser_count`
返回数字还是字符串——这些只有实跑才知道。**猜错的字段名会让 I1（evidence
可溯源）和 I6（ref 可溯源）永远校验失败。**

本脚本是**一次性取证工具**，但保留可重跑：obscura 升级改了返回格式时，
它是第一个该跑的脚本。

用法::

    export OBSCURA_EXE="C:/path/to/obscura.exe"
    uv run python trajectory_pipeline/executor/browser/probe_returns.py

    # 换目标页（默认 HN：链接与交互元素都丰富，且无反爬）
    uv run python trajectory_pipeline/executor/browser/probe_returns.py --url https://example.com
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 支持直接以脚本路径运行
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from trajectory_pipeline.executor.browser.mcp_client import McpClient

DEFAULT_URL = "https://news.ycombinator.com"

#: (label, tool, args) —— 依赖顺序：navigate 必须最先，其余读取当前页。
PROBES: list[tuple[str, str, dict[str, Any]]] = [
    ("navigate", "browser_navigate", {}),          # url 运行时注入
    ("snapshot", "browser_snapshot", {"max_chars": 1500}),
    ("snapshot_default", "browser_snapshot", {}),  # 不传 max_chars 看默认行为
    ("markdown", "browser_markdown", {"max_chars": 1200}),
    ("links", "browser_links", {"limit": 8}),
    ("interactive", "browser_interactive_elements", {"limit": 8}),
    ("count_anchor", "browser_count", {"selector": "a"}),
    ("count_video", "browser_count", {"selector": "video, audio"}),
    ("eval_title", "browser_evaluate", {"expression": "document.title"}),
    ("eval_struct", "browser_evaluate", {
        "expression": "JSON.stringify({url: location.href, title: document.title, "
                      "a: document.querySelectorAll('a').length, "
                      "video: document.querySelectorAll('video,audio').length})",
    }),
    ("network", "browser_network_requests", {}),
]


def _try_parse(text: str) -> dict[str, Any]:
    """猜测返回格式：整体 JSON → 逐行 JSON(NDJSON) → 原样。"""
    stripped = text.strip()
    if not stripped:
        return {"shape": "empty"}
    try:
        return {"shape": "json", "value": json.loads(stripped)}
    except ValueError:
        pass
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) > 1:
        try:
            values = [json.loads(ln) for ln in lines]
        except ValueError:
            return {"shape": "text", "line_count": len(lines), "head": stripped}
        return {"shape": "ndjson", "line_count": len(values), "head": values[0]}
    return {"shape": "text", "line_count": len(lines), "head": stripped}


async def probe(url: str, head_chars: int) -> dict[str, Any]:
    report: dict[str, Any] = {"url": url, "results": {}}
    async with McpClient.from_env() as mc:
        report["server"] = {
            "name": mc.info.name,
            "version": mc.info.version,
            "protocol_version": mc.info.protocol_version,
            "tool_count": mc.info.tool_count,
        }
        for label, tool, args in PROBES:
            call_args = {"url": url, **args} if tool == "browser_navigate" else args
            try:
                result = await mc.call(tool, call_args)
            except Exception as exc:  # 探测脚本：失败要看得见，不吞
                report["results"][label] = {
                    "tool": tool, "error": f"{type(exc).__name__}: {exc}",
                }
                continue
            report["results"][label] = {
                "tool": tool,
                "args": call_args,
                "is_error": result.is_error,
                "latency_ms": result.latency_ms,
                "text_len": len(result.text),
                "text_head": result.text[:head_chars],
                "parse": _try_parse(result.text),
            }
        report["click"] = await _probe_click(mc, head_chars)
    return report


async def _probe_click(mc: McpClient, head_chars: int) -> dict[str, Any]:
    """探测点击的返回形态——代码依赖它拿「点击后落到哪个 URL」。

    先取第一个可交互元素拿 ref；若取不到就退回 CSS selector 路径。
    不可逆（点击会导航），故放在最后一步。
    """
    try:
        elements = await mc.call("browser_interactive_elements", {"limit": 3})
    except Exception as exc:
        return {"error": f"interactive_elements: {type(exc).__name__}: {exc}"}

    target: dict[str, Any] = {}
    parsed = _try_parse(elements.text)
    if parsed.get("shape") in {"json", "ndjson"}:
        first = parsed.get("value") or parsed.get("head")
        if isinstance(first, dict):
            for key in ("ref", "id"):
                if first.get(key):
                    target = {"ref": str(first[key])}
                    break
            if not target and first.get("selector"):
                target = {"selector": str(first["selector"])}
    if not target:
        target = {"selector": "a"}  # 兜底：让 obscura 告诉我们它怎么解析的

    try:
        clicked = await mc.call("browser_click", target)
    except Exception as exc:
        return {"target": target, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "target": target,
        "is_error": clicked.is_error,
        "latency_ms": clicked.latency_ms,
        "text_head": clicked.text[:head_chars],
        "parse": _try_parse(clicked.text),
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"server : {report['server']['name']} {report['server']['version']} "
        f"({report['server']['tool_count']} tools, proto {report['server']['protocol_version']})",
        f"url    : {report['url']}",
        "",
    ]
    for label, res in report["results"].items():
        if "error" in res:
            lines.append(f"[x] {label:<18} {res['tool']}  → {res['error']}")
            continue
        parse = res.get("parse", {})
        flag = "ERR" if res["is_error"] else "ok "
        lines.append(
            f"[{flag}] {label:<18} {res['tool']:<30} "
            f"{res['latency_ms']:>5}ms  len={res['text_len']:<6} shape={parse.get('shape')}"
        )
        lines.append(f"       {res['text_head'][:220]}")

    click = report.get("click") or {}
    lines += ["", "click 探测：", json.dumps(click, ensure_ascii=False, indent=2)[:900]]
    return "\n".join(lines)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="探测 obscura tool 的真实返回格式")
    parser.add_argument("--url", default=DEFAULT_URL, help="探测目标页")
    parser.add_argument("--head-chars", type=int, default=800, help="每个返回保留多少字符")
    parser.add_argument(
        "--json",
        default="trajectory_pipeline/output/pipeline/obscura_returns.json",
        help="存档路径",
    )
    args = parser.parse_args()

    try:
        report = asyncio.run(probe(args.url, args.head_chars))
    except Exception as exc:
        print(f"[x] 探测失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("    提示：先设置 OBSCURA_EXE 环境变量", file=sys.stderr)
        return 1

    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render(report))
    print(f"\n存档: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())