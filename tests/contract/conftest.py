from __future__ import annotations

from pathlib import Path

import pytest

from simulate_serve.configuration.catalog_loader import CatalogLoader
from simulate_serve.configuration.catalog_schema import ScenarioDocument, TaskDocument


def _load_bundle(project_root: Path):
    bundle = CatalogLoader().load(
        project_root / "simulate_serve" / "config" / "tasks.yaml",
        project_root / "simulate_serve" / "config" / "scenarios.yaml",
    )
    assert bundle.diagnostics == (), bundle.diagnostics
    return bundle


@pytest.fixture
def scenarios(project_root: Path) -> list[ScenarioDocument]:
    return list(_load_bundle(project_root).scenarios)


@pytest.fixture
def tasks(project_root: Path) -> list[TaskDocument]:
    return list(_load_bundle(project_root).tasks)
