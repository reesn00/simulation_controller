"""label_studio CLI 入口 (方案 §8).

子命令:

* ``init-project`` : 创建 / 复用 LS 项目并校验 label_config (幂等)
* ``status``       : 打印连通性 + 配置完整性, **不创建 Run 日志**
* ``upload``       : 扫描 ``output/refine_data/`` 推送 C3 + 评分卡
* ``purge``        : 删除 LS 端项目及其全部标注 (危险, 需 ``--confirm``)

公共参数:
    ``--config PATH`` : 根配置 yaml; 默认走 ``SIMCTL_CONFIG`` env → 仓库根
        ``config/config.yaml``。缺配置时全部开关关闭, 只报"未启用"。

凭据只从 ``${LABEL_STUDIO_API_KEY}`` env 或 ``api_key_path`` 解析,
**任何子命令都不打印凭据内容**。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from label_studio.config_loader import load_label_studio_config
from label_studio.errors import (
    C3ParseError,
    CredentialLeakDetected,
    LabelStudioError,
)
from label_studio.settings import LabelStudioSettings

_log = logging.getLogger("label_studio")

#: C3 产物目录（相对仓库根）。与 etl 的输出目录一致。
DEFAULT_REFINE_DIR = "output/refine_data"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    from shared_config import REPO_ROOT

    return REPO_ROOT


def _resolve_refine_dir(value: str | None) -> Path:
    if not value:
        return _repo_root() / DEFAULT_REFINE_DIR
    path = Path(value)
    return path if path.is_absolute() else _repo_root() / path


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _build_client(settings: LabelStudioSettings):
    from label_studio.client import build_client

    return build_client(settings)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def _cmd_init_project(args: argparse.Namespace) -> int:
    from label_studio.project_manager import init_project

    settings = load_label_studio_config(Path(args.config) if args.config else None)
    if args.label_config:
        settings = replace(
            settings, label_config_path=_repo_root() / args.label_config
        )
    result = init_project(_build_client(settings), settings)
    _print_json(result)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from label_studio.project_manager import describe_status

    settings = load_label_studio_config(Path(args.config) if args.config else None)
    if not settings.has_credentials():
        _print_json(
            {
                "base_url": settings.base_url,
                "health": "skipped (无凭据)",
                "credentials_present": False,
                "hint": "设置 LABEL_STUDIO_API_KEY env 或 label_studio.api_key_path",
                "label_config_path": str(settings.label_config_file()),
                "label_config_exists": Path(settings.label_config_file()).is_file(),
                "upload_enabled": settings.upload.enabled,
                "hook_enabled": settings.hook.enabled,
            }
        )
        return 1

    client = _build_client(settings)
    health = client.health_check()
    _print_json(describe_status(client, settings, health=health))
    return 0


def _cmd_upload(args: argparse.Namespace) -> int:
    from label_studio.project_manager import resolve_project_id
    from label_studio.settings import ScorecardSettings
    from label_studio.task_exporter import export_batch, push_batch

    settings = load_label_studio_config(Path(args.config) if args.config else None)
    if args.no_scorecard:
        settings = replace(settings, scorecard=ScorecardSettings(enabled=False))
    if args.batch_size:
        settings = replace(
            settings, upload=replace(settings.upload, batch_size=args.batch_size)
        )

    refine_dir = _resolve_refine_dir(args.refine_dir)
    if not refine_dir.is_dir():
        _print_json({"error": f"目录不存在: {refine_dir}"})
        return 2

    plan = export_batch(
        refine_dir,
        settings=settings,
        task_id=args.task_id,
        min_score=args.min_score,
        complexity_tier=args.complexity_tier,
    )
    # 拒推不是"跳过": 必须让人看见并处置 (方案 §16 R11 fail-closed)。
    # 与推送结果**合并成一个 JSON 对象**输出 —— 两次 print 会让 stdout 不是
    # 合法 JSON, 脚本化消费方直接解析失败。
    report: dict = {"summary": plan.summary()}
    if plan.rejected:
        report["rejected"] = [
            {"stem": stem, "hits": [h.describe() for h in hits]}
            for stem, hits in plan.rejected
        ]
        report["rejected_note"] = (
            "以上 C3 命中凭据模式, 已 fail-closed 拒推; 请人工核查来源后重跑"
        )

    if args.dry_run:
        report.update(
            {
                "dry_run": True,
                "would_push": [
                    {
                        "task_id": t["data"]["task_id"],
                        "session_id": t["inner_id"],
                        "training_value_score": t["data"].get("training_value_score"),
                        "complexity_tier": t["data"].get("complexity_tier"),
                        "suggested_decision": (
                            t["data"].get("scorecard", {}).get("overall", {}) or {}
                        ).get("suggested_decision"),
                    }
                    for t in plan.tasks[: args.dry_run_show]
                ],
                "total_would_push": plan.count,
                "skipped": [{"stem": s, "reason": r} for s, r in plan.skipped[:50]],
            }
        )
        _print_json(report)
        return 0

    if not plan.tasks:
        report.update(
            {"pushed": False, "note": "无待推送样本; 检查 upload.enabled 与过滤条件"}
        )
        _print_json(report)
        return 0

    threshold = settings.upload.dry_run_skip_threshold
    if plan.count > threshold and not args.force:
        _print_json(
            {
                "error": f"待推送 {plan.count} 条超过 dry_run_skip_threshold={threshold}",
                "hint": "确认无误后加 --force, 或先跑 --dry-run",
            }
        )
        return 2

    client = _build_client(settings)
    project_id = resolve_project_id(client, settings)
    result = push_batch(
        plan,
        settings=settings,
        project_id=project_id,
        # 复用同一个 client: 否则一次 upload 会建两个连接, 且 project 查询
        # 与实际推送落在不同会话上。
        client_factory=lambda: client,
        on_progress=lambda done, total: _log.info("已推送 %d/%d", done, total),
    )
    report.update({"project_id": project_id, **result})
    _print_json(report)
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    from label_studio.project_manager import purge_tasks, resolve_project_id

    settings = load_label_studio_config(Path(args.config) if args.config else None)
    client = _build_client(settings)
    project_id = args.project_id or resolve_project_id(client, settings, sync=False)
    result = purge_tasks(client, project_id, confirm=args.confirm)
    _print_json(result)
    return 0 if result.get("purged") or not args.confirm else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m label_studio",
        description="C3 轨迹 + 评分卡 → Label Studio (终点, 不回流)",
    )
    parser.add_argument("--config", help="根配置 yaml 路径 (默认 SIMCTL_CONFIG / config/config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG 日志")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-project", help="创建 / 复用项目并校验 label_config (幂等)")
    p_init.add_argument("--label-config", help="覆盖 label_config XML 路径 (相对仓库根)")
    p_init.set_defaults(func=_cmd_init_project)

    p_status = sub.add_parser("status", help="连通性与配置自检 (不创建 Run 日志)")
    p_status.set_defaults(func=_cmd_status)

    p_up = sub.add_parser("upload", help="推送 C3 + 评分卡")
    p_up.add_argument("--refine-dir", help=f"C3 目录 (默认 {DEFAULT_REFINE_DIR})")
    p_up.add_argument("--dry-run", action="store_true", help="只打印计划, 不推送")
    p_up.add_argument("--dry-run-show", type=int, default=20, help="dry-run 明细条数 (默认 20)")
    p_up.add_argument("--batch-size", type=int, help="覆盖 upload.batch_size")
    p_up.add_argument("--task-id", help="仅推送单个 task (调试)")
    p_up.add_argument("--complexity-tier", choices=("easy", "medium", "hard"))
    p_up.add_argument("--min-score", type=float, help="覆盖 training_value_score 下限")
    p_up.add_argument("--no-scorecard", action="store_true", help="只推轨迹不带评分卡 (排障)")
    p_up.add_argument("--force", action="store_true", help="跳过 dry_run_skip_threshold 校验")
    p_up.set_defaults(func=_cmd_upload)

    p_purge = sub.add_parser("purge", help="删除 LS 项目及其全部标注 (不可逆)")
    p_purge.add_argument("--project-id", type=int, help="显式项目 id (默认按标题查)")
    p_purge.add_argument("--confirm", action="store_true", help="真正执行; 不加只报告")
    p_purge.set_defaults(func=_cmd_purge)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return int(args.func(args))
    except CredentialLeakDetected as exc:
        _print_json({"error": "credential_leak", "detail": str(exc),
                     "action": "已 fail-closed 拒推; 请人工核查来源"})
        return 3
    except C3ParseError as exc:
        _print_json({"error": "c3_parse", "detail": str(exc)})
        return 4
    except LabelStudioError as exc:
        _print_json({"error": type(exc).__name__, "detail": str(exc)})
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    sys.exit(main())
