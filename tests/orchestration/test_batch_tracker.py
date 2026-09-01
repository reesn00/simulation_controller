"""orchestration.batch_tracker 单元测试.

不依赖 simulate_serve；直接造 run.json.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from orchestration.batch_tracker import (
    BatchTrackerTimeout,
    wait_for_terminal,
)
from simulate_serve.domain.state_machine import RunState


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _write_run_json(runs_dir: Path, run_id: str, state: str) -> None:
    rd = runs_dir / run_id
    rd.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": run_id, "state": state}
    (rd / "run.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# wait_for_terminal 全部终态
# ---------------------------------------------------------------------------

def test_wait_for_terminal_all_already_terminal(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    _write_run_json(runs_dir, "r1", RunState.SUCCESS.value)
    _write_run_json(runs_dir, "r2", RunState.EXECUTOR_ERROR.value)
    _write_run_json(runs_dir, "r3", RunState.INTERRUPTED.value)

    result = wait_for_terminal(
        ["r1", "r2", "r3"], runs_dir=runs_dir, poll_seconds=0.05,
    )
    assert result == {
        "r1": RunState.SUCCESS,
        "r2": RunState.EXECUTOR_ERROR,
        "r3": RunState.INTERRUPTED,
    }


def test_wait_for_terminal_waits_for_late_terminal(tmp_path: Path) -> None:
    """部分 run.json 还未落 → 等到终态."""
    runs_dir = tmp_path / "runs"
    _write_run_json(runs_dir, "r1", RunState.SUCCESS.value)

    # 另一线程延迟写入 r2 run.json（先 missing 然后 SUCCESS）
    def writer():
        time.sleep(0.15)
        _write_run_json(runs_dir, "r2", RunState.INCONCLUSIVE.value)

    threading.Thread(target=writer, daemon=True).start()

    start = time.monotonic()
    result = wait_for_terminal(
        ["r1", "r2"], runs_dir=runs_dir, poll_seconds=0.05,
    )
    elapsed = time.monotonic() - start
    assert result["r1"] == RunState.SUCCESS
    assert result["r2"] == RunState.INCONCLUSIVE
    # 应至少等到 writer 完成（>0.1s）
    assert elapsed >= 0.1


def test_wait_for_terminal_picks_up_state_change(tmp_path: Path) -> None:
    """run.json 已落但 state 非终态 → 等到变终态."""
    runs_dir = tmp_path / "runs"
    _write_run_json(runs_dir, "r1", RunState.VALIDATING.value)

    def mutator():
        time.sleep(0.15)
        _write_run_json(runs_dir, "r1", RunState.SUCCESS.value)

    threading.Thread(target=mutator, daemon=True).start()
    result = wait_for_terminal(["r1"], runs_dir=runs_dir, poll_seconds=0.05)
    assert result["r1"] == RunState.SUCCESS


# ---------------------------------------------------------------------------
# 超时
# ---------------------------------------------------------------------------

def test_wait_for_terminal_timeout_when_missing(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    with pytest.raises(BatchTrackerTimeout) as exc_info:
        wait_for_terminal(
            ["never1", "never2"],
            runs_dir=runs_dir,
            poll_seconds=0.05,
            timeout=0.1,
        )
    assert set(exc_info.value.pending) == {"never1", "never2"}
    assert exc_info.value.timeout == 0.1


def test_wait_for_terminal_timeout_when_non_terminal_state(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    _write_run_json(runs_dir, "r1", RunState.PREPARING.value)
    with pytest.raises(BatchTrackerTimeout):
        wait_for_terminal(["r1"], runs_dir=runs_dir, poll_seconds=0.05, timeout=0.1)


# ---------------------------------------------------------------------------
# 错误 run.json 不挂死
# ---------------------------------------------------------------------------

def test_wait_for_terminal_skips_bad_json(tmp_path: Path) -> None:
    """run.json 损坏 → 跳过本轮；后续 termiant 仍能等到."""
    runs_dir = tmp_path / "runs"
    (runs_dir / "r1").mkdir(parents=True, exist_ok=True)
    (runs_dir / "r1" / "run.json").write_text("{not json", encoding="utf-8")

    def mutator():
        time.sleep(0.15)
        _write_run_json(runs_dir, "r1", RunState.SUCCESS.value)

    threading.Thread(target=mutator, daemon=True).start()
    result = wait_for_terminal(["r1"], runs_dir=runs_dir, poll_seconds=0.05)
    assert result["r1"] == RunState.SUCCESS


def test_wait_for_terminal_unknown_state_keeps_trying(tmp_path: Path) -> None:
    """state 字段为不可识别值 → 警告 + 继续轮询."""
    runs_dir = tmp_path / "runs"
    _write_run_json(runs_dir, "r1", "definitely_not_a_state")

    def mutator():
        time.sleep(0.15)
        _write_run_json(runs_dir, "r1", RunState.SUCCESS.value)

    threading.Thread(target=mutator, daemon=True).start()
    result = wait_for_terminal(["r1"], runs_dir=runs_dir, poll_seconds=0.05)
    assert result["r1"] == RunState.SUCCESS


# ---------------------------------------------------------------------------
# 空输入
# ---------------------------------------------------------------------------

def test_wait_for_terminal_empty_returns_empty(tmp_path: Path) -> None:
    result = wait_for_terminal([], runs_dir=tmp_path / "runs", poll_seconds=0.05)
    assert result == {}
