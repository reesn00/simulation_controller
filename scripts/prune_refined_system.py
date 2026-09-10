"""scripts/prune_refined_system.py: 按真实调用裁剪 refined session 的 system/tools 并泛化路径.

对拆分四视图产物 ``<stem>_refined.messages.json`` 就地执行
``etl.qwenformat.usage_prune.prune_session_in_place`` (同 stem 的 openai.json /
qwenjina.txt / meta.json 一并更新; 仅支持新拆分格式, 见
docs/refined_split_plan.md):

    - 删除未被调用的 skill 条目 / 工具 schema / 功能段 (recall、memory 等);
    - 保留被用到内容的原文 (不做压缩, 避免失真);
    - 本机路径按 session 种子替换为 persona 池中的多样化路径;
    - 重渲染 metadata.openai_messages / tools / qf_text 保持一致.

注: pipeline 内 gdr 落盘前已自动裁剪 (``cfg.enable_usage_prune``), 本脚本
仅用于对存量产物重跑.

用法:
    python scripts/prune_refined_system.py [glob_pattern] [--dry-run]
    缺省匹配 output/refine_data/*_refined.messages.json
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from etl.qwenformat.transform import build_chat_env, load_chat_template  # noqa: E402
from etl.qwenformat.usage_prune import (  # noqa: E402
    load_refined_session,
    prune_session_in_place,
    write_refined_session,
)


def _discover(pattern: str | None) -> list[str]:
    if pattern:
        return sorted(glob.glob(pattern))
    base = str(REPO_ROOT / "output" / "refine_data")
    return sorted(glob.glob(f"{base}/*_refined.messages.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "pattern",
        nargs="?",
        default=None,
        help="refined 文件的 glob 模式 (缺省匹配 output/refine_data/ 两种形态)",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告, 不写回")
    args = parser.parse_args()

    template = load_chat_template(str(REPO_ROOT / "etl" / "qwenformat" / "chat_template.jinja"))
    env = build_chat_env()

    files = _discover(args.pattern)
    if not files:
        print(f"no files matched: {args.pattern or 'output/refine_data/*_refined.messages.json'}")
        return 1

    import json

    for f in files:
        session = load_refined_session(f)
        size_before = Path(f).stat().st_size
        stats = prune_session_in_place(session, template, env)
        if not args.dry_run:
            write_refined_session(session, f)
        size_after = len(json.dumps(session, ensure_ascii=False))
        print(f"{Path(f).name}")
        print(
            f"  system: {stats['system_chars_before']} -> {stats['system_chars_after']} chars"
            f"  | tools: {stats['tools_before']} -> {stats['tools_after']}"
            f"  | qf_text: -> {stats['qf_text_chars_after']} chars"
            f"  | file: {size_before} -> {size_after} bytes"
        )
        print(f"  dropped sections: {stats['dropped_sections']}")
        print(f"  kept skills: {stats['kept_skills']}  dropped: {len(stats['dropped_skills'])}")
        print(f"  path new roots: {stats['path_new_roots']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
