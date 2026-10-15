"""ls_bridge CLI: scout 候选 → LabelStudio 的单向推送入口。

用法::

    python -m trajectory_pipeline.evaluation.ls_bridge status
    python -m trajectory_pipeline.evaluation.ls_bridge init-project
    python -m trajectory_pipeline.evaluation.ls_bridge upload --scout-dir output/scout/<date>
    python -m trajectory_pipeline.evaluation.ls_bridge purge --project-id 7 --confirm

配置走 ``TRAJECTORY_LS_*`` 环境变量（URL / API_KEY / PROJECT_ID / ENABLED）,
CLI 参数覆盖。风格对齐 ``evaluation/scout/cli.py``: ``main(argv) -> int``、
argparse 子命令、Windows 下 stdout 重配 UTF-8。
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
from typing import Any

from trajectory_pipeline.evaluation.ls_bridge.errors import (
    CredentialLeakDetected,
    LabelStudioError,
)
from trajectory_pipeline.evaluation.ls_bridge.settings import (
    ENV_API_KEY,
    ENV_URL,
    load_settings,
)


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m trajectory_pipeline.evaluation.ls_bridge",
        description="LabelStudio 单向推送: scout 候选(positive+negative) + scorecard.v2",
    )
    ap.add_argument("--url", default=None, help=f"LS 地址(默认取 ${ENV_URL})")
    ap.add_argument("--api-key", default=None, help=f"API key(默认取 ${ENV_API_KEY})")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="连通性 + 配置检查(不推数据)")

    p_init = sub.add_parser("init-project", help="创建/复用 LS 项目(幂等)")
    p_init.add_argument("--label-config", default=None, help="label_config XML 路径(默认包内 v2)")

    p_up = sub.add_parser("upload", help="推送 scout 候选到 LS")
    p_up.add_argument("--scout-dir", required=True, help="scout 三件套目录")
    p_up.add_argument(
        "--buckets", default="positive,negative",
        help="要上传的 bucket, 逗号分隔(默认 positive,negative; quarantine 不支持)",
    )
    p_up.add_argument("--views-root", default=None, help="P3 视图根(默认 storage 默认根)")
    p_up.add_argument("--limit", type=int, default=None, help="最多处理 N 条(取样调试)")
    p_up.add_argument("--batch-size", type=int, default=None, help="单批推送条数(默认 50)")
    p_up.add_argument("--no-scorecard", action="store_true", help="不带 scorecard 预标注")
    p_up.add_argument("--dry-run", action="store_true", help="只构建计划, 不推送")
    p_up.add_argument(
        "--dry-run-show", type=int, default=20,
        help="dry-run 时打印前 N 条 skipped/rejected 明细(默认 20)",
    )
    p_up.add_argument(
        "--force", action="store_true",
        help="待推送条数超过安全闸时仍强制推送",
    )

    p_purge = sub.add_parser("purge", help="删除 LS 项目(不可逆, 连同标注)")
    p_purge.add_argument("--project-id", type=int, required=True)
    p_purge.add_argument("--confirm", action="store_true", help="确认执行(不加仅 dry-run)")
    return ap


def _cmd_status(args: argparse.Namespace) -> int:
    from trajectory_pipeline.evaluation.ls_bridge.client import build_client
    from trajectory_pipeline.evaluation.ls_bridge.project_manager import describe_status

    settings = load_settings(base_url=args.url, api_key=args.api_key)
    if not settings.has_credentials():
        print(f"错误:未设置 {ENV_API_KEY}, status 也需要凭据", file=sys.stderr)
        return 1
    client = build_client(settings)
    health = client.health_check()
    for key, value in describe_status(client, settings, health=health).items():
        print(f"{key}: {value}")
    return 0


def _cmd_init_project(args: argparse.Namespace) -> int:
    from trajectory_pipeline.evaluation.ls_bridge.client import build_client
    from trajectory_pipeline.evaluation.ls_bridge.project_manager import init_project

    settings = load_settings(base_url=args.url, api_key=args.api_key)
    if args.label_config:
        settings = dataclasses.replace(
            settings, label_config_path=pathlib.Path(args.label_config)
        )
    client = build_client(settings)
    result = init_project(client, settings)
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


def _cmd_upload(args: argparse.Namespace) -> int:
    from trajectory_pipeline.evaluation.ls_bridge.exporter import export_batch, push_batch
    from trajectory_pipeline.evaluation.ls_bridge.push_index import PushIndex
    from trajectory_pipeline.evaluation.ls_bridge.settings import ScorecardSettings

    buckets = [b.strip() for b in args.buckets.split(",") if b.strip()]
    if not buckets:
        print("错误:--buckets 不能为空", file=sys.stderr)
        return 2

    settings = load_settings(
        base_url=args.url,
        api_key=args.api_key,
        batch_size=args.batch_size,
    )
    if args.no_scorecard:
        settings = dataclasses.replace(
            settings, scorecard=ScorecardSettings(enabled=False)
        )

    scout_dir = pathlib.Path(args.scout_dir)
    if not scout_dir.is_dir():
        print(f"错误:{scout_dir} 不是目录", file=sys.stderr)
        return 2

    plan = export_batch(
        scout_dir,
        settings=settings,
        buckets=buckets,
        views_root=args.views_root,
        limit=args.limit,
    )
    print(f"[upload] {plan.summary()}")

    show = max(0, args.dry_run_show)
    for sample_id, reason in plan.skipped[:show]:
        print(f"  skipped: {sample_id} — {reason}")
    for sample_id, hits in plan.rejected[:show]:
        described = "; ".join(h.describe() for h in hits)
        print(f"  rejected: {sample_id} — {described}")

    if args.dry_run:
        print("[dry-run] 未推送。去掉 --dry-run 执行实际推送。")
        return 0

    if not plan.tasks:
        print("[upload] 无可推送条目, 结束。")
        return 0

    # 数量安全闸(不是分数过滤): 超阈值且未 --force 时拒推, 逼人先 dry-run。
    threshold = settings.upload.dry_run_skip_threshold
    if plan.count > threshold and not args.force:
        print(
            f"错误:待推送 {plan.count} 条超过安全闸 {threshold}; "
            f"先跑 --dry-run 核对, 确认无误后加 --force",
            file=sys.stderr,
        )
        return 3

    if not settings.has_credentials():
        print(f"错误:未设置 {ENV_API_KEY}", file=sys.stderr)
        return 1

    from trajectory_pipeline.evaluation.ls_bridge.client import build_client
    from trajectory_pipeline.evaluation.ls_bridge.project_manager import (
        resolve_project_id,
    )

    client = build_client(settings)
    project_id = resolve_project_id(client, settings)
    index = PushIndex.load(PushIndex.default_path(project_id))
    result = push_batch(plan, settings=settings, project_id=project_id,
                        client=client, index=index)
    print(
        f"[upload] project={project_id} "
        f"tasks_pushed={result['tasks_pushed']} "
        f"predictions_pushed={result['predictions_pushed']} "
        f"batches={result['batches']} "
        f"skipped_duplicate={result['skipped_duplicate']}"
    )
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    from trajectory_pipeline.evaluation.ls_bridge.client import build_client
    from trajectory_pipeline.evaluation.ls_bridge.project_manager import purge_tasks

    settings = load_settings(base_url=args.url, api_key=args.api_key)
    if not settings.has_credentials():
        print(f"错误:未设置 {ENV_API_KEY}", file=sys.stderr)
        return 1
    client = build_client(settings)
    result = purge_tasks(client, args.project_id, confirm=args.confirm)
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


_COMMANDS: dict[str, Any] = {
    "status": _cmd_status,
    "init-project": _cmd_init_project,
    "upload": _cmd_upload,
    "purge": _cmd_purge,
}


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _COMMANDS.get(str(args.command))
    if handler is None:  # pragma: no cover - argparse required=True 已拦
        parser.error(f"未知子命令: {args.command}")
        return 2
    try:
        return handler(args)
    except CredentialLeakDetected as exc:
        print(f"凭据扫描命中, 拒推: {exc}", file=sys.stderr)
        return 3
    except LabelStudioError as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
