"""orchestration.batch_tracker: 等 simulate_serve 的 ``run.json`` 终态.

设计见 ``docs/orchestration-design.md`` §6.6。

职责：
    1. 轮询 ``<output_dir>/runs/<run_id>/run.json``，检查 ``state`` 字段
    2. ``RunState.is_terminal`` 为 True 时视为完成
    3. 提供 ``wait_for_terminal(run_ids, runs_dir, ...)`` 阻塞等待

边界：
    - 仅做"读 run.json + 判定终态"，不修改 run.json
    - 超时（可选）后抛 ``BatchTrackerTimeout``，仍返回已确认的状态
    - run.json 不存在视为"还在跑"，继续轮询
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from simulate_serve.domain.state_machine import TERMINAL_STATES, RunState

_log = logging.getLogger(__name__)


class BatchTrackerTimeout(RuntimeError):
    """wait_for_terminal 超过指定 timeout 仍未全部终态."""

    def __init__(self, pending: dict[str, str], timeout: float) -> None:
        self.pending = pending
        self.timeout = timeout
        super().__init__(
            f"batch_tracker: {len(pending)} run(s) not terminal after {timeout}s"
        )


def wait_for_terminal(
    run_ids: list[str] | tuple[str, ...],
    *,
    runs_dir: Path,
    poll_seconds: float = 5.0,
    timeout: float | None = None,
) -> dict[str, RunState]:
    """阻塞等待所有 run.json 终态.

    Parameters
    ----------
    run_ids:
        要等的 ``run_id`` 列表
    runs_dir:
        ``<output_dir>/runs`` 路径；run.json 路径 = ``runs_dir / run_id / run.json``
    poll_seconds:
        轮询间隔（秒）
    timeout:
        最长等待时间；None 表示无限等。超时抛 ``BatchTrackerTimeout``。

    Returns
    -------
    ``{run_id: RunState}``，所有 run 都已终态（state ∈ TERMINAL_STATES）。

    异常
    ----
    ``BatchTrackerTimeout``：超时（仅在 timeout 非 None 且超时时）。
    """
    pending: dict[str, str] = {rid: "pending" for rid in run_ids}  # run_id → "missing"|state
    deadline = time.monotonic() + timeout if timeout else None
    runs_dir = Path(runs_dir)

    while pending:
        for run_id in list(pending.keys()):
            run_json = runs_dir / run_id / "run.json"
            if not run_json.is_file():
                continue
            try:
                data = json.loads(run_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                _log.warning("batch_tracker: bad run.json for %s: %s", run_id, exc)
                continue
            state_str = data.get("state")
            try:
                state = RunState(state_str)
            except ValueError:
                _log.warning("batch_tracker: unknown state %r for %s", state_str, run_id)
                continue
            if state in TERMINAL_STATES:
                pending.pop(run_id)
            else:
                pending[run_id] = state.value

        if not pending:
            break
        if deadline is not None and time.monotonic() >= deadline:
            raise BatchTrackerTimeout(pending=pending, timeout=timeout)
        time.sleep(poll_seconds)

    result: dict[str, RunState] = {}
    for run_id in run_ids:
        run_json = runs_dir / run_id / "run.json"
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
            result[run_id] = RunState(data.get("state"))
        except (OSError, json.JSONDecodeError, ValueError):
            # 不应发生：pending 空表示刚才读成功过；保守降级
            result[run_id] = RunState.INTERRUPTED
    return result
