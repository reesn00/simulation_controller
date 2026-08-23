"""ETL 主入口：批量处理 origindata 下的 session JSON，输出 SFT 微调数据。

用法:
    python run_etl.py
    python run_etl.py --input origindata --output output
    python run_etl.py --no-thinking        # 不保留 thinking
    python run_etl.py --no-summary-system  # 不把 summary 作为 system
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from extract import iter_session_files, load_session
from load import load
from transform import TransformOptions, transform_session


HERE = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QwenPaw session -> SFT ETL")
    parser.add_argument("--input", default=str(HERE / "origindata"),
                        help="原始 session JSON 所在目录（递归）")
    parser.add_argument("--output", default=str(HERE / "output"),
                        help="产物输出目录")
    parser.add_argument("--no-thinking", action="store_true",
                        help="不保留 thinking 为 reasoning_content")
    parser.add_argument("--no-summary-system", action="store_true",
                        help="不把 summary 作为 system 消息")
    parser.add_argument("--drop-empty-assistant", action="store_true",
                        help="丢弃空 content 且无 tool_calls 的 assistant 消息")
    parser.add_argument("--no-keep-tool-state", action="store_true",
                        help="不在 error tool result 前加 [state] 标记")
    args = parser.parse_args(argv)

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    if not input_dir.exists():
        print(f"[etl] 输入目录不存在: {input_dir}", file=sys.stderr)
        return 2

    opts = TransformOptions(
        include_thinking=not args.no_thinking,
        include_summary_as_system=not args.no_summary_system,
        drop_empty_assistant=args.drop_empty_assistant,
        keep_tool_result_state=not args.no_keep_tool_state,
    )

    files = list(iter_session_files(input_dir))
    if not files:
        print(f"[etl] 未在 {input_dir} 下找到 .json session 文件", file=sys.stderr)
        return 1

    pairs = []
    for fp in files:
        try:
            record = load_session(fp)
        except Exception as e:
            print(f"[etl] 跳过 {fp}: 解析失败 {e}", file=sys.stderr)
            continue
        sample = transform_session(record, opts)
        pairs.append((record, sample))
        print(f"[etl] {fp.name} -> session={record.session_id} "
              f"user={sample.stats['user_turns']} "
              f"asst={sample.stats['assistant_turns']} "
              f"tc={sample.stats['tool_calls']} "
              f"tr={sample.stats['tool_results']} "
              f"out_msgs={sample.stats['output_messages']}")

    if not pairs:
        print("[etl] 没有可用样本", file=sys.stderr)
        return 1

    result = load(pairs, output_dir)
    print(f"[etl] 完成: {result.sample_count} 个样本")
    print(f"[etl] jsonl : {result.jsonl_path}")
    print(f"[etl] json  : {result.json_path}")
    print(f"[etl] stats : {result.stats_path}")
    print(f"[etl] audit : {result.audit_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
