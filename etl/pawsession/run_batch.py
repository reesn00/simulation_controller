"""Batch helper: 分批调用 run_etl.main，按固定步长写入独立子目录。

用法:
    python run_batch.py --total 49 --batch 10
    python run_batch.py --total 49 --batch 10 --input origindata --output output

每个 batch 写盘到:
    <output>/batch_<index>/
        sft_openai.jsonl
        sft_openai.json
        stats.json
        audit/...

最终汇总到:
    <output>/batch_manifest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_etl  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QwenPaw session 分批 ETL")
    parser.add_argument("--input", default=str(HERE / "origindata"),
                        help="原始 session JSON 所在目录")
    parser.add_argument("--output", default=str(HERE / "output"),
                        help="产物根目录（每个 batch 写入子目录）")
    parser.add_argument("--total", type=int, required=True,
                        help="session 总数（>=1）")
    parser.add_argument("--batch", type=int, default=10,
                        help="每批处理数量（默认 10）")
    parser.add_argument("--offset", type=int, default=0,
                        help="起始 offset，便于断点续跑")
    parser.add_argument("--shuffle", action="store_true",
                        help="与 run_etl 一致：按种子打乱文件顺序")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--no-thinking", action="store_true")
    parser.add_argument("--no-summary-system", action="store_true")
    parser.add_argument("--drop-empty-assistant", action="store_true")
    parser.add_argument("--no-keep-tool-state", action="store_true")
    args = parser.parse_args(argv)

    if args.total < 1 or args.batch < 1:
        print("[batch] --total 和 --batch 必须为正整数", file=sys.stderr)
        return 2

    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, object]] = []
    index = 0
    cursor = args.offset
    while cursor < args.total:
        take = min(args.batch, args.total - cursor)
        batch_dir = output_root / f"batch_{index:04d}"
        print(f"[batch] #{index} offset={cursor} limit={take} -> {batch_dir}")
        rc = run_etl.main([
            "--input", args.input,
            "--output", str(batch_dir),
            "--offset", str(cursor),
            "--limit", str(take),
        ] + (["--shuffle"] if args.shuffle else [])
          + (["--seed", str(args.seed)] if args.shuffle else [])
          + (["--no-thinking"] if args.no_thinking else [])
          + (["--no-summary-system"] if args.no_summary_system else [])
          + (["--drop-empty-assistant"] if args.drop_empty_assistant else [])
          + (["--no-keep-tool-state"] if args.no_keep_tool_state else []))
        manifest.append({
            "index": index,
            "offset": cursor,
            "limit": take,
            "output_dir": str(batch_dir),
            "returncode": rc,
        })
        if rc != 0:
            print(f"[batch] batch #{index} 失败 (rc={rc})", file=sys.stderr)
            break
        cursor += take
        index += 1

    manifest_path = output_root / "batch_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[batch] 完成 {index} 个 batch，manifest -> {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
