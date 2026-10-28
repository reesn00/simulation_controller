"""把 ``check-archives`` 的日志压成**可比的计数**（处置前后各跑一次）。

只读：读日志文件，打印/落盘各条目的计数。用于「处置没有把门禁读数弄坏」的对照。
"""
from __future__ import annotations

import pathlib
import re
import sys

HEADER = re.compile(r"^──\s*(\S+)\s*·")
ITEM = re.compile(r"^\s{2}(\S+)\s+×(\d+)\s+—\s*(.*)$")


def summarize(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8")
    out: dict[str, dict] = {}
    severity = ""
    for line in text.splitlines():
        head = HEADER.match(line)
        if head:
            severity = head.group(1)
            continue
        item = ITEM.match(line)
        if item:
            code, count, desc = item.group(1), int(item.group(2)), item.group(3)
            out.setdefault(code, {"severity": severity, "count": 0,
                                  "desc": desc})["count"] += count
    return out


def main(argv: list[str]) -> int:
    for raw in argv[1:]:
        path = pathlib.Path(raw)
        data = summarize(path)
        print(f"=== {path.name} ===")
        for code, info in sorted(data.items(),
                                 key=lambda kv: -kv[1]["count"]):
            print(f"  {info['severity']:9s} {code:34s} ×{info['count']:<4d} "
                  f"{info['desc'][:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
