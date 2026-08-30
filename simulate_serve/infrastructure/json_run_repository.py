from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from simulate_serve.application.errors import RepositoryPortError
from simulate_serve.domain.evidence import Evidence
from simulate_serve.domain.run import RunEvent, TaskRun
from simulate_serve.domain.state_machine import RunState, TERMINAL_STATES
from simulate_serve.domain.validation import ValidationReport, Verdict
from simulate_serve.interaction.content_policy import has_internal_reasoning_signals


class RepositoryError(RepositoryPortError):
    pass


class JsonRunRepository:
    def __init__(
        self,
        output_dir: str | Path,
        *,
        max_artifact_bytes: int = 5_000_000,
        max_total_artifact_bytes: int = 50_000_000,
    ):
        self.root = Path(output_dir)
        self.runs_dir = self.root / "runs"
        self.artifacts_dir = self.root / "artifacts"
        self.datasets_dir = self.root / "datasets"
        self.reports_dir = self.root / "reports"
        self.legacy_dir = self.root / "legacy"
        for path in (self.runs_dir, self.artifacts_dir, self.datasets_dir, self.reports_dir, self.legacy_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.max_artifact_bytes = max_artifact_bytes
        self.max_total_artifact_bytes = max_total_artifact_bytes

    def save_run(self, run: TaskRun) -> None:
        run_dir = self.runs_dir / run.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        event_path = run_dir / "events.jsonl"
        event_ids = self._jsonl_ids(event_path, "event_id")
        for event in run.state_events:
            if event.event_id not in event_ids:
                self.append_event(run.run_id, event)
                event_ids.add(event.event_id)
        validation_path = run_dir / "validations.jsonl"
        validation_ids = self._jsonl_ids(validation_path, "report_id")
        for report in run.validation_rounds:
            if report.report_id not in validation_ids:
                self._append_jsonl(validation_path, report.model_dump(mode="json"))
                validation_ids.add(report.report_id)
        self._atomic_json(run_dir / "run.json", run.model_dump(mode="json"))

    def append_event(self, run_id: str, event: object) -> None:
        path = self.runs_dir / run_id / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
        self._append_jsonl(path, value)

    def save_evidence(self, run_id: str, evidence: Evidence) -> None:
        path = self.runs_dir / run_id / "evidence.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._append_jsonl(path, evidence.model_dump(mode="json"))

    def save_artifact(self, content: bytes, suffix: str = ".bin") -> str:
        if len(content) > self.max_artifact_bytes:
            raise RepositoryError(f"Artifact exceeds {self.max_artifact_bytes} bytes")
        digest = hashlib.sha256(content).hexdigest()
        path = self.artifacts_dir / digest[:2] / f"{digest}{suffix}"
        if path.exists():
            return str(path.relative_to(self.root)).replace("\\", "/")
        current_size = sum(item.stat().st_size for item in self.artifacts_dir.rglob("*") if item.is_file())
        if current_size + len(content) > self.max_total_artifact_bytes:
            raise RepositoryError(f"Artifact directory exceeds {self.max_total_artifact_bytes} bytes")
        self._atomic_bytes(path, content)
        return str(path.relative_to(self.root)).replace("\\", "/")

    def load_runs(self) -> list[TaskRun]:
        result: list[TaskRun] = []
        for path in sorted(self.runs_dir.glob("*/run.json")):
            try:
                run = TaskRun.model_validate_json(path.read_text(encoding="utf-8"))
                self._reconcile_append_only_records(run, path.parent)
                result.append(run)
            except Exception as exc:
                if isinstance(exc, RepositoryError):
                    raise
                raise RepositoryError(f"Invalid run checkpoint: {path}: {exc}") from exc
        return result

    def mark_interrupted(self) -> list[TaskRun]:
        interrupted: list[TaskRun] = []
        for run in self.load_runs():
            if run.state in TERMINAL_STATES:
                continue
            previous = run.state
            run.state = RunState.INTERRUPTED
            run.completed_at = datetime.now(UTC)
            run.state_events.append(
                RunEvent(event_type="RUN_INTERRUPTED", from_state=previous, to_state=RunState.INTERRUPTED)
            )
            self.save_run(run)
            interrupted.append(run)
        return interrupted

    def export(self, *, output_format: str = "both") -> dict[str, Any]:
        runs = self.load_runs()
        self._write_jsonl(self.datasets_dir / "all_runs.v2.jsonl", [run.model_dump(mode="json") for run in runs])
        distill = [
            {
                "dataset_schema_version": "2",
                "run_id": run.run_id,
                "task": {
                    "task_id": run.task_id,
                    "task_type": run.task_type,
                    "dimension": run.dimension,
                    "scenario_id": run.scenario_id,
                },
                "persona": {"role_description": run.persona_role},
                "messages": [{"role": turn.role, "content": turn.content} for turn in run.conversation],
                "validation_summary": self._validation_summary(run),
                "lineage": {"source_run_id": run.run_id, "rerun_of": run.rerun_of},
            }
            for run in runs
            if run.state is RunState.SUCCESS and self._is_distillable(run)
        ]
        if output_format in {"v2", "both"}:
            self._write_jsonl(self.datasets_dir / "distill_dataset.v2.jsonl", distill)
        stats = self._stats(runs)
        self._atomic_json(self.reports_dir / "stats.v2.json", stats)
        if output_format in {"legacy", "both"}:
            from simulate_serve.infrastructure.legacy_exporter import LegacyExporter

            LegacyExporter(self.legacy_dir).export(runs, stats)
        return stats

    @staticmethod
    def _is_clean(run: TaskRun) -> bool:
        forbidden = ("<think>", "</think>", "authorization:", "cookie:")
        for turn in run.conversation:
            if any(token in turn.content.casefold() for token in forbidden):
                return False
            if turn.role == "assistant" and has_internal_reasoning_signals(turn.content):
                return False
        return True

    @classmethod
    def _is_distillable(cls, run: TaskRun) -> bool:
        if not cls._is_clean(run) or len(run.conversation) < 2:
            return False
        if not run.validation_rounds:
            return False
        report = run.validation_rounds[-1]
        if report.verdict is not Verdict.PASS or not report.criteria:
            return False
        if any(item.verdict is not Verdict.PASS for item in report.criteria):
            return False
        # Allow a single trailing user turn (the closing-utterance appended on PASS).
        roles = [turn.role for turn in run.conversation]
        if roles[-1] == "user":
            roles = roles[:-1]
        if len(roles) % 2 or not roles or roles[0] != "user":
            return False
        expected = ["user" if index % 2 == 0 else "assistant" for index in range(len(roles))]
        return roles == expected

    @staticmethod
    def _validation_summary(run: TaskRun) -> dict[str, Any]:
        if not run.validation_rounds:
            return {"verdict": Verdict.INCONCLUSIVE.value, "report_id": None, "criteria": []}
        report = run.validation_rounds[-1]
        return {
            "verdict": report.verdict.value,
            "report_id": report.report_id,
            "criteria": [
                {
                    "criterion_id": item.criterion_id,
                    "verdict": item.verdict.value,
                    "reason_code": item.reason_code,
                    "evidence_ids": list(item.evidence_ids),
                }
                for item in report.criteria
            ],
        }

    def _stats(self, runs: list[TaskRun]) -> dict[str, Any]:
        states = Counter(run.state.value for run in runs)
        terminal = [run for run in runs if run.state in TERMINAL_STATES]
        rounds = sorted(run.guide_rounds for run in terminal)
        durations = sorted(
            (run.completed_at - run.started_at).total_seconds()
            for run in terminal
            if run.completed_at is not None
        )
        criteria = Counter(
            item.verdict.value
            for run in runs
            for report in run.validation_rounds
            for item in report.criteria
        )
        tool_status = Counter()
        tool_provider = Counter()
        tool_timeouts = 0
        for run in runs:
            path = self.runs_dir / run.run_id / "evidence.jsonl"
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                evidence = json.loads(line)
                tool_status[str(evidence.get("status", "unknown"))] += 1
                tool_provider[str(evidence.get("source", "unknown"))] += 1
                if "timeout" in str(evidence.get("summary", "")).casefold():
                    tool_timeouts += 1
        return {
            "stats_schema_version": "2",
            "total": len(runs),
            "states": dict(sorted(states.items())),
            "success_rate": states.get(RunState.SUCCESS.value, 0) / len(terminal) if terminal else 0.0,
            "guide_rounds": self._distribution(rounds),
            "run_duration_seconds": self._distribution(durations),
            "criteria": dict(sorted(criteria.items())),
            "tools": {
                "status": dict(sorted(tool_status.items())),
                "providers": dict(sorted(tool_provider.items())),
                "timeouts": tool_timeouts,
            },
            "by_task_type": self._group_states(runs, "task_type"),
            "by_dimension": self._group_states(runs, "dimension"),
            "by_scenario": self._group_states(runs, "scenario_id"),
        }

    @staticmethod
    def _distribution(values: list[float] | list[int]) -> dict[str, float]:
        if not values:
            return {"avg": 0.0, "p50": 0.0, "p95": 0.0}
        return {
            "avg": float(mean(values)),
            "p50": float(JsonRunRepository._percentile(values, 0.50)),
            "p95": float(JsonRunRepository._percentile(values, 0.95)),
        }

    @staticmethod
    def _percentile(values: list[float] | list[int], quantile: float) -> float:
        index = max(0, min(len(values) - 1, int((len(values) - 1) * quantile + 0.5)))
        return float(values[index])

    @staticmethod
    def _group_states(runs: list[TaskRun], field: str) -> dict[str, dict[str, int]]:
        groups: dict[str, Counter[str]] = {}
        for run in runs:
            key = str(getattr(run, field) or "(none)")
            groups.setdefault(key, Counter())[run.state.value] += 1
        return {key: dict(sorted(value.items())) for key, value in sorted(groups.items())}

    def _reconcile_append_only_records(self, run: TaskRun, run_dir: Path) -> None:
        event_values = self._read_jsonl(run_dir / "events.jsonl")
        if event_values:
            events = [RunEvent.model_validate(item) for item in event_values]
            self._ensure_unique_ids(events, "event_id", run.run_id)
            persisted_ids = {item.event_id for item in events}
            checkpoint_ids = {item.event_id for item in run.state_events}
            if not checkpoint_ids.issubset(persisted_ids):
                raise RepositoryError(f"Run {run.run_id} checkpoint contains events missing from events.jsonl")
            for previous, current in zip(events, events[1:]):
                if current.from_state is not previous.to_state:
                    raise RepositoryError(f"Run {run.run_id} has a broken event transition chain")
            run.state_events = events
            run.state = events[-1].to_state
            if run.state in TERMINAL_STATES and run.completed_at is None:
                run.completed_at = events[-1].created_at

        validation_values = self._read_jsonl(run_dir / "validations.jsonl")
        if validation_values:
            reports = [ValidationReport.model_validate(item) for item in validation_values]
            self._ensure_unique_ids(reports, "report_id", run.run_id)
            persisted_ids = {item.report_id for item in reports}
            checkpoint_ids = {item.report_id for item in run.validation_rounds}
            if not checkpoint_ids.issubset(persisted_ids):
                raise RepositoryError(f"Run {run.run_id} checkpoint contains validations missing from validations.jsonl")
            run.validation_rounds = reports

    @staticmethod
    def _ensure_unique_ids(values: list[Any], field: str, run_id: str) -> None:
        ids = [str(getattr(item, field)) for item in values]
        if len(ids) != len(set(ids)):
            raise RepositoryError(f"Run {run_id} has duplicate {field} values")

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        values: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("JSONL records must be objects")
                    values.append(value)
        except Exception as exc:
            raise RepositoryError(f"Invalid append-only record file {path}: {exc}") from exc
        return values

    @classmethod
    def _jsonl_ids(cls, path: Path, field: str) -> set[str]:
        values = cls._read_jsonl(path)
        ids = [str(item.get(field) or "") for item in values]
        if any(not item for item in ids) or len(ids) != len(set(ids)):
            raise RepositoryError(f"Invalid or duplicate {field} in {path}")
        return set(ids)

    @staticmethod
    def _append_jsonl(path: Path, value: Any) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @classmethod
    def _write_jsonl(cls, path: Path, values: list[Any]) -> None:
        content = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in values).encode("utf-8")
        cls._atomic_bytes(path, content)

    @classmethod
    def _atomic_json(cls, path: Path, value: Any) -> None:
        cls._atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    @staticmethod
    def _atomic_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
