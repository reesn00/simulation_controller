from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from .catalog_schema import (
    ScenarioCatalogDocument,
    ScenarioDocument,
    TaskCatalogDocument,
    TaskDocument,
)
from .diagnostics import CatalogDiagnostic, DiagnosticSeverity


class CatalogValidationError(ValueError):
    def __init__(self, diagnostics: list[CatalogDiagnostic]):
        self.diagnostics = tuple(diagnostics)
        detail = "\n".join(f"{d.source}:{d.path}: {d.message}" for d in diagnostics)
        super().__init__(f"Task catalog validation failed:\n{detail}")


class CatalogBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    tasks: tuple[TaskDocument, ...]
    scenarios: tuple[ScenarioDocument, ...]
    diagnostics: tuple[CatalogDiagnostic, ...] = ()
    tasks_path: Path
    scenarios_path: Path | None = None


class CatalogLoader:
    """Load legacy, v1 and v2 catalogs with version-aware validation."""

    def load(self, tasks_path: Path, scenarios_path: Path | None = None) -> CatalogBundle:
        diagnostics: list[CatalogDiagnostic] = []
        raw_tasks = self._read_yaml(tasks_path, diagnostics)
        raw_scenarios: Any = []
        if scenarios_path is not None:
            if scenarios_path.exists():
                raw_scenarios = self._read_yaml(scenarios_path, diagnostics)
            else:
                diagnostics.append(self._error("SCENARIO_FILE_MISSING", "Scenario file does not exist", scenarios_path))

        task_version, task_items = self._unwrap(raw_tasks, "tasks", tasks_path, diagnostics)
        scenario_version, scenario_items = self._unwrap(raw_scenarios, "scenarios", scenarios_path, diagnostics)
        tasks = self._validate_items(TaskDocument, task_items, tasks_path, "tasks", diagnostics)
        scenarios = self._validate_items(ScenarioDocument, scenario_items, scenarios_path, "scenarios", diagnostics)
        self._validate_versions(task_version, scenario_version, tasks, scenarios, tasks_path, scenarios_path, diagnostics)
        self._check_duplicate_ids(tasks, "task_id", tasks_path, diagnostics)
        self._check_duplicate_ids(scenarios, "scenario_id", scenarios_path, diagnostics)

        scenario_ids = {item.scenario_id for item in scenarios}
        for index, task in enumerate(tasks):
            if task.scenario and task.scenario not in scenario_ids:
                diagnostics.append(
                    self._error(
                        "UNKNOWN_SCENARIO",
                        f"Unknown scenario '{task.scenario}' referenced by task '{task.task_id}'",
                        tasks_path,
                        f"tasks[{index}].scenario",
                    )
                )

        errors = [d for d in diagnostics if d.severity is DiagnosticSeverity.ERROR]
        if errors:
            raise CatalogValidationError(errors)
        version = task_version if task_version != "0" else scenario_version if scenario_version != "0" else "0"
        return CatalogBundle(
            schema_version=version,
            tasks=tuple(tasks),
            scenarios=tuple(scenarios),
            diagnostics=tuple(diagnostics),
            tasks_path=tasks_path.resolve(),
            scenarios_path=scenarios_path.resolve() if scenarios_path else None,
        )

    @staticmethod
    def _validate_versions(
        task_version: str,
        scenario_version: str,
        tasks: list[TaskDocument],
        scenarios: list[ScenarioDocument],
        tasks_path: Path,
        scenarios_path: Path | None,
        diagnostics: list[CatalogDiagnostic],
    ) -> None:
        supported = {"0", "1", "2"}
        if task_version not in supported:
            diagnostics.append(CatalogLoader._error("SCHEMA_VERSION_UNSUPPORTED", task_version, tasks_path, "schema_version"))
        if scenario_version not in supported:
            diagnostics.append(CatalogLoader._error("SCHEMA_VERSION_UNSUPPORTED", scenario_version, scenarios_path, "schema_version"))
        if task_version != "0" and scenario_version != "0" and task_version != scenario_version:
            diagnostics.append(
                CatalogLoader._error(
                    "SCHEMA_VERSION_MISMATCH",
                    f"Task schema {task_version} does not match scenario schema {scenario_version}",
                    tasks_path,
                    "schema_version",
                )
            )
        if task_version != "2":
            return
        for index, task in enumerate(tasks):
            if not task.initial_request:
                diagnostics.append(CatalogLoader._error("V2_INITIAL_REQUEST_REQUIRED", "v2 task requires initial_request", tasks_path, f"tasks[{index}].initial_request"))
            if task.intent is None:
                diagnostics.append(CatalogLoader._error("V2_INTENT_REQUIRED", "v2 task requires intent", tasks_path, f"tasks[{index}].intent"))
            if task.validation_rules is not None:
                diagnostics.append(CatalogLoader._error("V2_LEGACY_RULES_FORBIDDEN", "v2 task cannot use validation_rules", tasks_path, f"tasks[{index}].validation_rules"))
            if task.expected_reference is not None:
                diagnostics.append(CatalogLoader._error("V2_LEGACY_REFERENCE_FORBIDDEN", "v2 task must use reference", tasks_path, f"tasks[{index}].expected_reference"))
        for index, scenario in enumerate(scenarios):
            if scenario.dialogue_policy is None:
                diagnostics.append(CatalogLoader._error("V2_DIALOGUE_POLICY_REQUIRED", "v2 scenario requires dialogue_policy", scenarios_path, f"scenarios[{index}].dialogue_policy"))

    @staticmethod
    def resolve_path(value: str | Path, config_dir: Path, package_dir: Path) -> tuple[Path, bool]:
        path = Path(value)
        if path.is_absolute():
            return path, False
        preferred = config_dir / path
        if preferred.exists():
            return preferred, False
        return package_dir / path, True

    @staticmethod
    def _read_yaml(path: Path, diagnostics: list[CatalogDiagnostic]) -> Any:
        if not path.exists():
            diagnostics.append(CatalogLoader._error("FILE_MISSING", "Catalog file does not exist", path))
            return []
        try:
            with path.open(encoding="utf-8") as stream:
                return yaml.safe_load(stream) or []
        except (OSError, yaml.YAMLError) as exc:
            diagnostics.append(CatalogLoader._error("YAML_INVALID", str(exc), path))
            return []

    @staticmethod
    def _unwrap(raw: Any, key: str, path: Path | None, diagnostics: list[CatalogDiagnostic]) -> tuple[str, list[Any]]:
        if isinstance(raw, list):
            diagnostics.append(
                CatalogDiagnostic(
                    severity=DiagnosticSeverity.WARNING,
                    code="LEGACY_CATALOG_V0",
                    message=f"Top-level list is deprecated; wrap it in schema_version + {key}",
                    source=str(path or ""),
                )
            )
            return "0", raw
        if isinstance(raw, dict):
            unknown = set(raw) - {"schema_version", key}
            if unknown:
                for name in sorted(unknown):
                    diagnostics.append(CatalogLoader._error("UNKNOWN_CATALOG_FIELD", f"Unknown field '{name}'", path, name))
            version = str(raw.get("schema_version", "1"))
            items = raw.get(key, [])
            if not isinstance(items, list):
                diagnostics.append(CatalogLoader._error("CATALOG_ITEMS_INVALID", f"'{key}' must be a list", path, key))
                return version, []
            return version, items
        diagnostics.append(CatalogLoader._error("CATALOG_ROOT_INVALID", "Catalog root must be a list or mapping", path))
        return "0", []

    @staticmethod
    def _validate_items(model: type[BaseModel], items: list[Any], path: Path | None, key: str, diagnostics: list[CatalogDiagnostic]) -> list[Any]:
        validated: list[Any] = []
        for index, item in enumerate(items):
            try:
                validated.append(model.model_validate(item))
            except ValidationError as exc:
                for error in exc.errors(include_url=False):
                    field_path = ".".join(str(part) for part in error["loc"])
                    diagnostics.append(
                        CatalogLoader._error(
                            "SCHEMA_INVALID",
                            error["msg"],
                            path,
                            f"{key}[{index}]" + (f".{field_path}" if field_path else ""),
                        )
                    )
        return validated

    @staticmethod
    def _check_duplicate_ids(items: list[Any], field: str, path: Path | None, diagnostics: list[CatalogDiagnostic]) -> None:
        seen: dict[str, int] = {}
        for index, item in enumerate(items):
            item_id = getattr(item, field)
            if item_id in seen:
                diagnostics.append(
                    CatalogLoader._error(
                        "DUPLICATE_ID",
                        f"Duplicate {field} '{item_id}' (first at index {seen[item_id]})",
                        path,
                        f"[{index}].{field}",
                    )
                )
            else:
                seen[item_id] = index

    @staticmethod
    def _error(code: str, message: str, path: Path | None, field_path: str = "") -> CatalogDiagnostic:
        return CatalogDiagnostic(
            severity=DiagnosticSeverity.ERROR,
            code=code,
            message=message,
            source=str(path or ""),
            path=field_path,
        )
