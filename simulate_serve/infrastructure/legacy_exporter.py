from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.state_machine import RunState
from simulate_serve.domain.validation import Verdict


class LegacyExporter:
    """Deprecated one-way v2 -> legacy projection retained for one release cycle."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export(self, runs: list[TaskRun], stats: dict[str, Any]) -> None:
        dataset = self.output_dir / "distill_dataset.jsonl"
        with dataset.open("w", encoding="utf-8", newline="\n") as stream:
            for run in runs:
                if run.state is not RunState.SUCCESS or not self._is_distillable(run):
                    continue
                stream.write(
                    json.dumps(
                        {
                            "task_id": run.task_id,
                            "task_type": run.task_type,
                            "session_id": run.remote_session_id,
                            "success": True,
                            "guide_rounds": run.guide_rounds,
                            "conversation": [turn.model_dump(mode="json") for turn in run.conversation],
                            "validation_detail": {},
                            "fail_reason": "",
                            "internal_thoughts": [],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        (self.output_dir / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _is_clean(run: TaskRun) -> bool:
        forbidden = ("<think>", "</think>", "authorization:", "cookie:")
        return all(not any(token in turn.content.casefold() for token in forbidden) for turn in run.conversation)

    @classmethod
    def _is_distillable(cls, run: TaskRun) -> bool:
        if not cls._is_clean(run) or not run.validation_rounds:
            return False
        report = run.validation_rounds[-1]
        if report.verdict is not Verdict.PASS or not report.criteria:
            return False
        if any(item.verdict is not Verdict.PASS for item in report.criteria):
            return False
        if len(run.conversation) < 2 or len(run.conversation) % 2:
            return False
        expected = ["user" if index % 2 == 0 else "assistant" for index in range(len(run.conversation))]
        return [turn.role for turn in run.conversation] == expected
