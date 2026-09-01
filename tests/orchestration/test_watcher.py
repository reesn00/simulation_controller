"""orchestration.watcher 单元测试."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from orchestration.queue import SQLiteQueue, STATE_PENDING
from orchestration.watcher import (
    TrajectoryWatcher,
    parse_trajectory_filename,
)


# ---------------------------------------------------------------------------
# parse_trajectory_filename
# ---------------------------------------------------------------------------

def test_parse_trajectory_filename_with_double_underscore() -> None:
    run, session = parse_trajectory_filename(Path("run_aaa__useramulation-bbb.json"))
    assert run == "run_aaa"
    assert session == "useramulation-bbb"


def test_parse_trajectory_filename_with_multiple_double_underscores() -> None:
    """双下划线 split 一次：session_id 可含单下划线."""
    run, session = parse_trajectory_filename(
        Path("run_xxx__session_yyy_zzz.json")
    )
    assert run == "run_xxx"
    assert session == "session_yyy_zzz"


def test_parse_trajectory_filename_without_double_underscore() -> None:
    run, session = parse_trajectory_filename(Path("single_name.json"))
    assert run == "single_name"
    assert session is None


def test_parse_trajectory_filename_double_underscore_at_edge() -> None:
    """'__session'：run_id 空字符串（边界）."""
    run, session = parse_trajectory_filename(Path("__session.json"))
    assert run == ""
    assert session == "session"


# ---------------------------------------------------------------------------
# scan_once
# ---------------------------------------------------------------------------

def _make_traj(tmp_path: Path, name: str, content: str = "{}") -> Path:
    fp = tmp_path / name
    fp.write_text(content, encoding="utf-8")
    return fp


def test_scan_once_registers_new_files(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    traj_dir = tmp_path / "traj"
    traj_dir.mkdir()
    _make_traj(traj_dir, "run_001__session_aaa.json")
    _make_traj(traj_dir, "run_002__session_bbb.json")

    w = TrajectoryWatcher(
        trajectory_dir=traj_dir, queue=queue, batch_id=1,
        poll_seconds=0.1,
    )
    result = w.scan_once()
    assert result == {"registered": 2, "skipped": 0, "dead": 0}
    counts = queue.count_by_state()
    assert counts.get(STATE_PENDING) == 2


def test_scan_once_idempotent(tmp_path: Path) -> None:
    """重复扫描同一目录，第二次应全 skipped."""
    queue = SQLiteQueue(tmp_path / "q.db")
    traj_dir = tmp_path / "traj"
    traj_dir.mkdir()
    _make_traj(traj_dir, "run_001__s.json")

    w = TrajectoryWatcher(trajectory_dir=traj_dir, queue=queue, batch_id=1)
    r1 = w.scan_once()
    r2 = w.scan_once()
    assert r1 == {"registered": 1, "skipped": 0, "dead": 0}
    assert r2 == {"registered": 0, "skipped": 1, "dead": 0}
    assert queue.count_pending_qf() == 1


def test_scan_once_nonexistent_dir_returns_zeros(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    w = TrajectoryWatcher(
        trajectory_dir=tmp_path / "missing",
        queue=queue, batch_id=1,
    )
    result = w.scan_once()
    assert result == {"registered": 0, "skipped": 0, "dead": 0}


def test_scan_once_mixed_new_and_existing(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    traj_dir = tmp_path / "traj"
    traj_dir.mkdir()
    _make_traj(traj_dir, "run_001__s.json")
    w = TrajectoryWatcher(trajectory_dir=traj_dir, queue=queue, batch_id=1)
    w.scan_once()
    # 新增 2 个
    _make_traj(traj_dir, "run_002__s.json")
    _make_traj(traj_dir, "run_003__s.json")
    result = w.scan_once()
    assert result == {"registered": 2, "skipped": 1, "dead": 0}
    assert queue.count_pending_qf() == 3


def test_scan_once_logs_dead_to_dead_log(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    traj_dir = tmp_path / "traj"
    traj_dir.mkdir()
    # 创建一个内容无法解析的文件——但我们的 insert 不读文件内容，
    # 所以这条不会触发 dead。需要别的注入方式：让 queue 在某些
    # 情况下抛错。简化：用 monkeypatch 让 insert 抛异常。
    _make_traj(traj_dir, "run_001__s.json")

    dead_log = tmp_path / "dead.log"

    from orchestration.queue import SQLiteQueue as _Q
    original_insert = _Q.insert

    def bad_insert(self, **_kw):
        raise RuntimeError("injected")

    _Q.insert = bad_insert
    try:
        w = TrajectoryWatcher(
            trajectory_dir=traj_dir, queue=queue, batch_id=1,
            dead_log_path=dead_log,
        )
        result = w.scan_once()
    finally:
        _Q.insert = original_insert

    assert result["dead"] == 1
    assert dead_log.exists()
    content = dead_log.read_text(encoding="utf-8")
    assert "injected" in content
    assert "run_001__s.json" in content


def test_scan_once_uses_provided_batch_id(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    traj_dir = tmp_path / "traj"
    traj_dir.mkdir()
    _make_traj(traj_dir, "run_001__s.json")
    w = TrajectoryWatcher(
        trajectory_dir=traj_dir, queue=queue, batch_id=42,
    )
    w.scan_once()
    tasks = queue.list_tasks_for_batch(42)
    assert len(tasks) == 1
    assert tasks[0].batch_id == 42


# ---------------------------------------------------------------------------
# run_forever
# ---------------------------------------------------------------------------

def test_run_forever_exits_on_stop_event(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    traj_dir = tmp_path / "traj"
    traj_dir.mkdir()
    _make_traj(traj_dir, "run_001__s.json")

    w = TrajectoryWatcher(
        trajectory_dir=traj_dir, queue=queue, batch_id=1,
        poll_seconds=0.05,
    )
    stop = threading.Event()

    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()

    # 等几轮扫描
    time.sleep(0.2)
    stop.set()
    t.join(timeout=1.0)

    assert queue.count_pending_qf() == 1
    assert not t.is_alive()


def test_run_forever_registers_files_added_after_start(tmp_path: Path) -> None:
    """watcher 启动后新加的文件下一轮应被登记."""
    queue = SQLiteQueue(tmp_path / "q.db")
    traj_dir = tmp_path / "traj"
    traj_dir.mkdir()

    w = TrajectoryWatcher(
        trajectory_dir=traj_dir, queue=queue, batch_id=1,
        poll_seconds=0.05,
    )
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), daemon=True)
    t.start()

    time.sleep(0.1)
    _make_traj(traj_dir, "run_late__session_late.json")
    time.sleep(0.2)
    stop.set()
    t.join(timeout=1.0)

    assert queue.count_pending_qf() == 1