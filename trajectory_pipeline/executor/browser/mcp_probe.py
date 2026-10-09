#!/usr/bin/env python
"""探测 obscura MCP server 暴露的 tool 清单。

为什么做成探针而不是一次性命令：执行层的 ``PageDriver`` 原语映射依赖这份清单，
obscura 升级可能导致 tool 增删改名（= schema 漂移）。所以它必须能随时重跑做回归对比，
清单本身也要落盘存档。

用法::

    uv run python trajectory_pipeline/executor/browser/mcp_probe.py --exe "C:/path/to/obscura.exe"

    # 指定落盘位置（默认 output/pipeline/obscura_tools.json）
    uv run python ... --exe "..." --json output/pipeline/obscura_tools.json

    # 冒烟：真调一次导航，确认 tool 不只是列得出来、而是真能用
    uv run python ... --exe "..." --smoke https://example.com --smoke-tool browser_navigate

    # 关掉 stealth（对比用）
    uv run python ... --exe "..." --no-stealth
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# PageDriver 原语 ← obscura tool 的期望映射（见 docs/设计方案/00-总体方案.md §3.2）。
# 探针会把实际清单打出来供人工比对；缺口在此登记，不靠猜。
# 每项 = (同义词, 用途)。同义词用于模糊匹配 tool 名——obscura 的命名用 navigate
# 而非 goto，两边对不上会报假缺口。
EXPECTED_PRIMITIVES = {
    "goto": (("navigate", "goto"), "打开搜索结果页 / 站点页 / 播放页"),
    "snapshot": (("snapshot", "markdown"), "取页面结构 → Observation（须真实回放，非转述）"),
    "click": (("click",), "进入播放页（会话式，isTrusted 真实）"),
    "type": (("type", "fill"), "搜索框输入（可选，缺失可退化为 URL 拼接搜索）"),
    "evaluate": (("evaluate", "extract", "count"), "执行 JS / 结构化提取 / 存在性探测"),
    "interactive": (
        ("interactive", "links"),
        "可交互元素 ref 与链接表 → Observation 的 interactive_elements / links",
    ),
    "media_probe": (
        ("count", "evaluate"),
        "video 计数 + currentTime 推进探测（由 count/evaluate 组合自建，无单一 tool）",
    ),
}


async def _smoke(
    session: ClientSession, tool: str, url: str, timeout: float
) -> dict[str, Any]:
    """Call ``tool`` once with a best-guess argument shape and summarize the result."""
    attempts: list[dict[str, Any]] = []
    # obscura 的工具参数形状未实测，两种常见形态各试一次。
    for args in ({"url": url}, {}):
        label = json.dumps(args, ensure_ascii=False)
        try:
            result = await asyncio.wait_for(session.call_tool(tool, args), timeout=timeout)
        except Exception as exc:  # 探测脚本：任何失败都要看得见，不吞
            attempts.append({"args": label, "error": f"{type(exc).__name__}: {exc}"})
            continue
        text = "".join(
            getattr(block, "text", "") or "" for block in (result.content or [])
        )
        attempts.append(
            {
                "args": label,
                "is_error": bool(getattr(result, "isError", False)),
                "text_head": text[:600],
                "text_len": len(text),
            }
        )
        if text:
            break
    return {"tool": tool, "attempts": attempts}


async def probe(
    exe: str,
    extra_args: list[str],
    url: str | None,
    smoke_tool: str | None,
    timeout: float,
) -> dict[str, Any]:
    params = StdioServerParameters(command=exe, args=["mcp", *extra_args])

    # obscura 的 banner / 日志走 stderr。Windows 下 sys.stderr 默认 GBK，
    # obscura 输出 UTF-8 时会抛 UnicodeEncodeError 把整条探针带崩——故丢弃而非转发。
    with open(os.devnull, "w", encoding="utf-8") as devnull:
        async with stdio_client(params, errlog=devnull) as (read, write):
            async with ClientSession(read, write) as session:
                init = await asyncio.wait_for(session.initialize(), timeout=timeout)
                listing = await asyncio.wait_for(session.list_tools(), timeout=timeout)
                smoke = (
                    await _smoke(session, smoke_tool, url, timeout)
                    if url and smoke_tool
                    else None
                )

    tools = [
        {
            "name": t.name,
            "description": (t.description or "").strip(),
            "input_schema": t.inputSchema,
        }
        for t in listing.tools
    ]
    return {
        "server": exe,
        "server_args": ["mcp", *extra_args],
        "protocol_version": getattr(init, "protocolVersion", None),
        "server_info": getattr(getattr(init, "serverInfo", None), "name", None),
        "server_version": getattr(getattr(init, "serverInfo", None), "version", None),
        "tool_count": len(tools),
        "tools": tools,
        "smoke": smoke,
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        f"server    : {report['server']} {' '.join(report['server_args'])}",
        f"protocol  : {report['protocol_version']}  "
        f"server    : {report['server_info']} {report['server_version'] or ''}",
        f"tool 数   : {report['tool_count']}",
        "",
    ]
    for tool in report["tools"]:
        props = (tool["input_schema"] or {}).get("properties") or {}
        required = (tool["input_schema"] or {}).get("required") or []
        sig = ", ".join(
            f"{k}{'*' if k in required else ''}" for k in props
        ) or "(no args)"
        first_line = (tool["description"].splitlines() or [""])[0]
        lines.append(f"  {tool['name']}({sig})")
        if first_line:
            lines.append(f"      {first_line}")

    lines += ["", "PageDriver 映射核对："]
    names = [t["name"].lower() for t in report["tools"]]
    for primitive, (synonyms, purpose) in EXPECTED_PRIMITIVES.items():
        hits = sorted({n for n in names for s in synonyms if s in n})
        optional = primitive in ("type",)
        mark = "✅" if hits else ("➖ 可选，缺失需降级" if optional else "⚠️  缺口")
        lines.append(f"  {mark} {primitive:<12} {purpose}")
        if hits:
            lines.append(f"       候选: {', '.join(hits)}")

    if report.get("smoke"):
        lines += ["", "冒烟结果：", json.dumps(report["smoke"], ensure_ascii=False, indent=2)]
    return "\n".join(lines)


def main() -> int:
    # Windows 控制台默认 GBK，stdout 里的 ✅/⚠️ 会抛 UnicodeEncodeError 把探针带崩。
    # 只放宽 errors 而不改 encoding——PowerShell 下当前编码本就正常，不该被改掉。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="探测 obscura MCP server 的 tool 清单")
    parser.add_argument("--exe", required=True, help="obscura 可执行文件路径")
    parser.add_argument(
        "--json",
        default="trajectory_pipeline/output/pipeline/obscura_tools.json",
        help="清单落盘路径",
    )
    parser.add_argument("--smoke", metavar="URL", help="冒烟：真调一次该 URL")
    parser.add_argument("--smoke-tool", default="browser_navigate", help="冒烟用哪个 tool")
    parser.add_argument("--timeout", type=float, default=60.0, help="单步超时秒数")
    parser.add_argument("--no-stealth", action="store_true", help="关掉 stealth")
    parser.add_argument("extra", nargs="*", help="透传给 obscura mcp 的额外参数")
    args = parser.parse_args()

    if not Path(args.exe).exists():
        print(f"[x] 找不到 obscura 可执行文件: {args.exe}", file=sys.stderr)
        return 2

    extra = ([] if args.no_stealth else ["--stealth"]) + list(args.extra)
    try:
        report = asyncio.run(
            probe(args.exe, extra, args.smoke, args.smoke_tool, args.timeout)
        )
    except Exception as exc:
        print(f"[x] 探测失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(render(report))
    print(f"\n清单已存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())