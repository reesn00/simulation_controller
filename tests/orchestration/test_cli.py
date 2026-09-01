"""orchestration CLI 测试.

测 ``main()`` 入口的各子命令行为.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestration.__main__ import main
from orchestration.queue import (
    STATE_DEAD,
    STATE_DONE,
    STATE_PENDING,
    SQLiteQueue,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """创建最小可用 config.yaml + 空 SQLite + 切换到 tmp_path."""
    cfg_path = tmp_path / "orch.yaml"
    cfg_path.write_text(json.dumps({
        "orchestration": {"max_retry_qf": 1, "max_retry_gdr": 1},
        "paths": {
            "simulate_serve_config": str(tmp_path / "sim.yaml"),
            "trajectory_dir": str(tmp_path / "traj"),
            "qf_output_dir": str(tmp_path / "qf_out"),
            "gdr_output_dir": str(tmp_path / "gdr_out"),
            "sqlite_db": str(tmp_path / "q.db"),
            "dead_dir": str(tmp_path / "dead"),
            "pid_file": str(tmp_path / "orch.pid"),
            "log_dir": str(tmp_path / "logs"),
            "runs_dir": str(tmp_path / "runs"),
        },
    }), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path, cfg_path


# ---------------------------------------------------------------------------
# start --dry-run
# ---------------------------------------------------------------------------

def test_start_dry_run_prints_plan(env, capsys) -> None:
    tmp, cfg = env
    rc = main(["--config", str(cfg), "start", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert str(tmp / "q.db") in out


# ---------------------------------------------------------------------------
# start --detach
# ---------------------------------------------------------------------------

def test_start_detached_spawns_child(env) -> None:
    tmp, cfg = env
    rc = main(["--config", str(cfg), "start", "--detach"])
    assert rc == 0
    pid_file = tmp / "orch.pid"
    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert pid > 0
    # 子进程应在跑
    import os, subprocess, sys
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True, text=True, timeout=5,
    )
    assert str(pid) in out.stdout, f"child pid {pid} not alive"
    # 清理
    main(["--config", str(cfg), "stop", "--timeout", "5"])


def test_start_already_running_detected(env) -> None:
    tmp, cfg = env
    main(["--config", str(cfg), "start", "--detach"])
    rc = main(["--config", str(cfg), "start", "--detach"])
    assert rc == 1  # already running
    # 清理
    main(["--config", str(cfg), "stop", "--timeout", "5"])


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_no_db(env, capsys) -> None:
    _tmp, cfg = env
    rc = main(["--config", str(cfg), "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sqlite_db" in out
    assert "(missing" in out


def test_status_with_db(env, capsys) -> None:
    tmp, cfg = env
    # 写一个 SQLite + 一些 task
    queue = SQLiteQueue(tmp / "q.db")
    fp = tmp / "traj" / "r__s.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("{}", encoding="utf-8")
    queue.insert(src_path=fp, run_id="r", session_id="s", batch_id=1)
    queue.mark_dead(1, error_msg="oops")

    rc = main(["--config", str(cfg), "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "queue_counts" in out
    assert "dead_tasks" in out
    assert "r" in out


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

def test_stop_when_not_running(env, capsys) -> None:
    _tmp, cfg = env
    rc = main(["--config", str(cfg), "stop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not running" in out


def test_stop_cleans_stale_pid_file(env) -> None:
    tmp, cfg = env
    pid_file = tmp / "orch.pid"
    pid_file.write_text("9999999", encoding="utf-8")  # 不存在
    rc = main(["--config", str(cfg), "stop"])
    assert rc == 0
    assert not pid_file.exists()


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def test_replay_no_db(env, capsys) -> None:
    _tmp, cfg = env
    rc = main(["--config", str(cfg), "replay"])
    assert rc == 1
    assert "missing" in capsys.readouterr().out


def test_replay_all_resets_dead_to_pending(env) -> None:
    tmp, cfg = env
    queue = SQLiteQueue(tmp / "q.db")
    fp = tmp / "traj" / "r__s.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("{}", encoding="utf-8")
    queue.insert(src_path=fp, run_id="r", session_id="s", batch_id=1)
    queue.mark_dead(1, error_msg="x")

    rc = main(["--config", str(cfg), "replay"])
    assert rc == 0
    refreshed = queue.get(1)
    assert refreshed.state == STATE_PENDING


def test_replay_batch_filters(env) -> None:
    tmp, cfg = env
    queue = SQLiteQueue(tmp / "q.db")
    traj_dir = tmp / "traj"
    traj_dir.mkdir()
    for i in (1, 2):
        fp = traj_dir / f"r{i}__s{i}.json"
        fp.write_text("{}", encoding="utf-8")
        tid, _ = queue.insert(src_path=fp, run_id=f"r{i}", session_id=f"s{i}", batch_id=i)
        queue.mark_dead(tid, error_msg="x")

    rc = main(["--config", str(cfg), "replay", "--batch", "1"])
    assert rc == 0
    # batch=1 应被重置为 pending；batch=2 应仍 dead
    with queue._conn() as conn:
        rows = conn.execute(
            "SELECT batch_id, state FROM tasks ORDER BY batch_id",
        ).fetchall()
    states_by_batch = {r["batch_id"]: r["state"] for r in rows}
    assert states_by_batch[1] == STATE_PENDING
    assert states_by_batch[2] == STATE_DEAD


def test_replay_writes_health(env) -> None:
    tmp, cfg = env
    queue = SQLiteQueue(tmp / "q.db")
    fp = tmp / "traj" / "r__s.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("{}", encoding="utf-8")
    queue.insert(src_path=fp, run_id="r", session_id="s", batch_id=1)
    queue.mark_dead(1, error_msg="x")
    main(["--config", str(cfg), "replay"])
    health = tmp / "logs" / "health.json"
    assert health.exists()
    data = json.loads(health.read_text(encoding="utf-8"))
    assert "queue_counts" in data
