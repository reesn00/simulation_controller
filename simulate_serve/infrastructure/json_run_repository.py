from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from simulate_serve.application.errors import RepositoryPortError
from simulate_serve.domain.evidence import Evidence
from simulate_serve.domain.run import RunEvent, TaskRun
from simulate_serve.domain.state_machine import RunState, TERMINAL_STATES
from simulate_serve.domain.validation import ValidationReport


class RepositoryError(RepositoryPortError):
    pass


class JsonRunRepository:
    """模拟 server 端的 Run 持久化 (审计用).

    新架构 ``simulation server → gdr → etl`` 下:
    - simulation server 只产出 Run 审计数据 (`runs/<run_id>/`).
    - SFT 蒸馏 / 训练数据由 gdr + etl 末端产出 (C2 → C3 契约, 见
      ``docs/contracts/``); 本类不再写 ``datasets/`` 或 ``reports/stats.json``.
    """

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
        for path in (self.runs_dir, self.artifacts_dir):
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
        ids = [str(getattr(item, field) or "") for item in values]
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