from __future__ import annotations

from typing import Protocol

from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.task import AcceptanceCriterion, CompiledTask
from simulate_serve.domain.validation import CriterionResult

from .claims import Claim


class EvidenceCollector(Protocol):
    async def collect(
        self,
        task: CompiledTask,
        run: TaskRun,
        criterion: AcceptanceCriterion,
        claims: tuple[Claim, ...],
    ) -> CriterionResult: ...
