from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from .bootstrap import (
    build_application,
    render_validation_readiness,
    validation_readiness_gaps,
)
from .config import AppConfig, load_config
from .configuration.catalog_loader import CatalogValidationError
from .infrastructure.json_run_repository import JsonRunRepository
from .infrastructure.camel_model_factory import model_runtime_configured
from .task_manager import TaskManager
from .tools.factories import create_default_registry
from .tools.registry import RequiredToolUnavailableError

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """simulate_serve 只读开关入口 — 任务执行已移交 orchestration.

    设计依据 ``docs/设计方案/pipeline-contracts.md`` §7.6 — 保留:
        --validate-config / --check-tools / --readiness / --list-interrupted

    删除 (移交 orchestration):
        --tasks / --rerun-task / --limit / --include-offline / --max-run-retries

    任务执行入口请用 ``python -m orchestration start ...``。
    """
    parser = argparse.ArgumentParser(
        description=(
            "CAMEL-AI User Simulator for Agent Data Distillation "
            "(只读开关入口; 任务执行请用 orchestration start)"
        ),
    )
    parser.add_argument("--config", default=None, help="Path to config file")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--list-interrupted", action="store_true")
    parser.add_argument("--check-tools", action="store_true")
    parser.add_argument(
        "--readiness", action="store_true",
        help="Report local validation readiness without calling QwenPaw",
    )
    parser.add_argument("--validate-config", action="store_true")
    return parser


def _setup_logging(output_dir: str, verbose: bool, *, file_logging: bool = True) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if file_logging:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(directory / "run.log", encoding="utf-8", mode="a"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _log_config(config: AppConfig) -> None:
    logger.info(
        "model=%s/%s api_key_configured=%s executor=%s max_guide_rounds=%d output=%s",
        config.model.model_type,
        config.model.model_name,
        bool(config.model.api_key),
        config.agent_endpoint.base_url,
        config.max_guide_rounds,
        config.output_dir,
    )


async def _check_tools(config: AppConfig) -> int:
    registry = create_default_registry(config.model)
    try:
        report = await registry.start(config.tools)
        print(report.render())
        return 0
    finally:
        await registry.close()


async def _check_readiness(config: AppConfig) -> int:
    manager = TaskManager(
        config.tasks_file,
        config.scenarios_file,
        config_dir=config.config_dir,
        max_guide_rounds=config.max_guide_rounds,
    )
    registry = create_default_registry(config.model)
    try:
        await registry.start(config.tools)
        judge_available = (
            config.validation.semantic_judge_enabled
            and model_runtime_configured(config.model)
        )
        gaps = validation_readiness_gaps(
            manager.compiled_tasks,
            registry,
            judge_available=judge_available,
        )
        print(render_validation_readiness(manager.compiled_tasks, gaps))
        return 0
    finally:
        await registry.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        _setup_logging(
            config.output_dir,
            args.verbose,
            file_logging=not args.validate_config and not args.check_tools and not args.readiness,
        )
        _log_config(config)
        if args.validate_config:
            manager = TaskManager(
                config.tasks_file,
                config.scenarios_file,
                config_dir=config.config_dir,
                max_guide_rounds=config.max_guide_rounds,
            )
            print(f"Catalog valid: tasks={len(manager.compiled_tasks)} diagnostics={len(manager.diagnostics)}")
            return 0
        if args.check_tools:
            return asyncio.run(_check_tools(config))
        if args.readiness:
            return asyncio.run(_check_readiness(config))
        if args.list_interrupted:
            repository = JsonRunRepository(config.output_dir)
            interrupted = [item for item in repository.load_runs() if item.state.value == "interrupted"]
            for run in interrupted:
                print(f"{run.run_id}\t{run.task_id}\t{run.completed_at or ''}")
            return 0
        # 无任务入口: simulate_serve 不再直接跑 batch, 提示用 orchestration start.
        print(
            "[simulate_serve] task execution has moved to "
            "`python -m orchestration start --all-tasks`.",
            file=sys.stderr,
        )
        return 0
    except (FileNotFoundError, ValueError, ValidationError, CatalogValidationError) as exc:
        logger.error("Configuration/CLI error: %s", exc)
        return 2
    except RequiredToolUnavailableError as exc:
        logger.error("%s", exc.report.render())
        return 3
    except KeyboardInterrupt:
        return 130
    except Exception:
        logger.exception("Application startup/runtime failure")
        return 5


if __name__ == "__main__":
    raise SystemExit(main())