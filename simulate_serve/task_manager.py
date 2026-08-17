from __future__ import annotations

import logging
from pathlib import Path

from .application.task_compiler import TaskCompiler
from .config import PACKAGE_DIR
from .configuration.catalog_loader import CatalogLoader
from .domain.task import CompiledTask

logger = logging.getLogger(__name__)


class TaskManager:
    """Catalog facade retained for legacy callers during the runtime migration."""

    def __init__(
        self,
        tasks_file: str,
        scenarios_file: str | None = None,
        *,
        config_dir: Path | None = None,
        max_guide_rounds: int = 3,
    ):
        base = config_dir or PACKAGE_DIR
        tasks_path, tasks_fallback = CatalogLoader.resolve_path(tasks_file, base, PACKAGE_DIR)
        scenarios_path: Path | None = None
        scenarios_fallback = False
        if scenarios_file:
            scenarios_path, scenarios_fallback = CatalogLoader.resolve_path(scenarios_file, base, PACKAGE_DIR)
        if tasks_fallback or scenarios_fallback:
            logger.warning("Deprecated package-relative catalog path fallback was used")

        bundle = CatalogLoader().load(tasks_path, scenarios_path)
        compiled = TaskCompiler(max_guide_rounds=max_guide_rounds).compile(bundle)
        self.compiled_tasks: list[CompiledTask] = list(compiled.tasks)
        self.tasks: list[CompiledTask] = self.compiled_tasks
        self.scenarios = {item.scenario_id: item for item in bundle.scenarios}
        self.diagnostics = compiled.diagnostics
        logger.info(
            "Compiled %d tasks and %d scenarios (%d diagnostics)",
            len(self.compiled_tasks),
            len(self.scenarios),
            len(self.diagnostics),
        )
