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

def _wait_child_up(pid_file: Path, timeout: float = 20.0) -> int | None:
    """detach 的真实语义：main 立即返回，PID 由子进程 start_foreground 异步
    写入（冷启动解释器 + import 需要时间）。轮询直到出现或超时."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                return None
            if pid > 0:
                return pid
        time.sleep(0.2)
    return None


def test_start_detached_spawns_child(env) -> None:
    tmp, cfg = env
    rc = main(["--config", str(cfg), "start", "--detach"])
    assert rc == 0
    pid_file = tmp / "orch.pid"
    try:
        pid = _wait_child_up(pid_file)
        assert pid, "detached child never wrote the pid file"
        # 子进程应在跑
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        assert str(pid) in out.stdout, f"child pid {pid} not alive"
    finally:
        main(["--config", str(cfg), "stop", "--timeout", "5"])


def test_start_already_running_detected(env) -> None:
    tmp, cfg = env
    rc = main(["--config", str(cfg), "start", "--detach"])
    assert rc == 0
    pid_file = tmp / "orch.pid"
    try:
        # 必须等第一个子进程完成注册（写 PID）再发起第二次 start，
        # 否则第二个 master 会并行启动并泄漏（互相覆盖 PID 文件）
        assert _wait_child_up(pid_file), "first detached child never came up"
        rc = main(["--config", str(cfg), "start", "--detach"])
        assert rc == 1  # already running
    finally:
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
