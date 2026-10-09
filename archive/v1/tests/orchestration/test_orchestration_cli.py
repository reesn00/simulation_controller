"""orchestration CLI 测试 (新架构 simulation server → python → etl).

覆盖契约 §7 + §11.2:
* start --dry-run 打印 parallelism / 不含 --batch-size / 不含 --exit-when-done
* start --parallelism N 透传
* status 子命令读 SQLite phases / 最近 10 task
* replay (无 --batch) 重置所有 dead → pending
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestration.__main__ import main
from orchestration.queue import (
    PHASE_DEAD,
    PHASE_DONE,
    PHASE_PENDING,
    SQLiteQueue,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path) -> Path:
    """写一个最小可用 config + sim.yaml (供 load_config 解析).

    注: 模拟 catalog 加载需要 tasks.yaml/scenarios.yaml; 这里直接给
    simulate_serve_config 内显式指定 task/scenario 路径指向空文件,
    TaskManager 加载空文件 = 空 catalog, _load_all_task_ids 返 [].
    """
    cfg_path = tmp_path / "orch.yaml"
    sim_path = tmp_path / "sim.yaml"
    tasks_path = tmp_path / "tasks.yaml"
    scenarios_path = tmp_path / "scenarios.yaml"
    # 空 catalog (TaskManager 接受空 list, 返 0 task)
    tasks_path.write_text(
        json.dumps({"schema_version": "2", "tasks": []}), encoding="utf-8",
    )
    scenarios_path.write_text(
        json.dumps({"schema_version": "2", "scenarios": []}), encoding="utf-8",
    )
    sim_path.write_text("{}", encoding="utf-8")
    cfg_path.write_text(json.dumps({
        "simulate_serve": {
            "tasks_file": str(tasks_path),
            "scenarios_file": str(scenarios_path),
        },
        "orchestration": {
            "pipeline": {
                "max_parallelism": 1,
                "max_retry_gdr": 1,
                "max_retry_etl": 1,
                "retry_poll_seconds": 0.05,
            },
            "paths": {
                "simulate_serve_config": str(sim_path),
                "trajectory_dir": str(tmp_path / "traj"),
                "runs_dir": str(tmp_path / "runs"),
                "refined_dir": str(tmp_path / "refined"),
                "etl_outputs_dir": str(tmp_path / "etl_outputs"),
                "sqlite_db": str(tmp_path / "q.db"),
                "dead_dir": str(tmp_path / "dead"),
                "pid_file": str(tmp_path / "orch.pid"),
                "log_dir": str(tmp_path / "logs"),
            },
        },
    }), encoding="utf-8")
    return cfg_path


# ---------------------------------------------------------------------------
# start --dry-run
# ---------------------------------------------------------------------------


def test_start_dry_run_prints_plan(tmp_path: Path, capsys) -> None:
    cfg = _write_config(tmp_path)
    rc = main(["--config", str(cfg), "start", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "parallelism" in out
    assert str(tmp_path / "q.db") in out


def test_start_dry_run_with_parallelism_override(tmp_path: Path, capsys) -> None:
    cfg = _write_config(tmp_path)
    rc = main([
        "--config", str(cfg),
        "start", "--dry-run", "--parallelism", "4",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "parallelism" in out
    assert "= 4" in out


def test_start_dry_run_no_batch_size_option(tmp_path: Path, capsys) -> None:
    """--batch-size 选项已删除 (契约 §7.7): argparse 直接 exit(2)."""
    cfg = _write_config(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(cfg), "start", "--dry-run", "--batch-size", "3"])
    assert exc_info.value.code == 2


def test_start_no_exit_when_done_option(tmp_path: Path, capsys) -> None:
    """--exit-when-done 已删除 (契约 §7.7): argparse 直接 exit(2)."""
    cfg = _write_config(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(cfg), "start", "--dry-run", "--exit-when-done"])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# start --detach
# ---------------------------------------------------------------------------


def _wait_child_up(pid_file: Path, timeout: float = 20.0) -> int | None:
    """detach 的真实语义: main 立即返回, PID 由子进程 start_foreground 异步
    写入 (冷启动解释器 + import 需要时间). 轮询直到出现或超时."""
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


def test_start_detached_spawns_child(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    rc = main(["--config", str(cfg), "start", "--detach"])
    assert rc == 0
    pid_file = tmp_path / "orch.pid"
    try:
        pid = _wait_child_up(pid_file)
        assert pid, "detached child never wrote the pid file"
        # 子进程应在跑
        import subprocess
        import sys
        run_kwargs: dict[str, object] = {
            "capture_output": True, "text": True, "timeout": 5,
        }
        if sys.platform == "win32":
            # CREATE_NO_WINDOW = 0x08000000: 禁止 tasklist 弹出 cmd 窗口
            run_kwargs["creationflags"] = 0x08000000
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            **run_kwargs,
        )
        assert str(pid) in out.stdout, f"child pid {pid} not alive"
    finally:
        main(["--config", str(cfg), "stop", "--timeout", "5"])


def test_start_already_running_detected(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    rc = main(["--config", str(cfg), "start", "--detach"])
    assert rc == 0
    pid_file = tmp_path / "orch.pid"
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


def test_status_no_db(tmp_path: Path, capsys) -> None:
    cfg = _write_config(tmp_path)
    rc = main(["--config", str(cfg), "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "phases" in out or "sqlite_db" in out
    assert "(missing" in out


def test_status_with_db(tmp_path: Path, capsys) -> None:
    cfg = _write_config(tmp_path)
    # 写一个 SQLite + 一些 task
    queue = SQLiteQueue(tmp_path / "q.db")
    queue.upsert_task("T_DONE")
    queue.mark_phase("T_DONE", new_phase=PHASE_DONE)
    queue.upsert_task("T_DEAD")
    queue.mark_phase("T_DEAD", new_phase="gdr")
    queue.mark_failed("T_DEAD", stage="gdr", error_msg="oops")
    queue.upsert_task("T_PENDING")

    rc = main(["--config", str(cfg), "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "phases" in out
    assert "T_DONE" in out
    assert "T_DEAD" in out


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_stop_when_not_running(tmp_path: Path, capsys) -> None:
    cfg = _write_config(tmp_path)
    rc = main(["--config", str(cfg), "stop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not running" in out


def test_stop_cleans_stale_pid_file(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    pid_file = tmp_path / "orch.pid"
    pid_file.write_text("9999999", encoding="utf-8")  # 不存在
    rc = main(["--config", str(cfg), "stop"])
    assert rc == 0
    assert not pid_file.exists()


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def test_replay_no_db(tmp_path: Path, capsys) -> None:
    cfg = _write_config(tmp_path)
    rc = main(["--config", str(cfg), "replay"])
    assert rc == 1
    assert "missing" in capsys.readouterr().out


def test_replay_resets_all_dead_to_pending(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    queue = SQLiteQueue(tmp_path / "q.db")
    queue.upsert_task("T1")
    queue.mark_phase("T1", new_phase="gdr")
    queue.mark_failed("T1", stage="gdr", error_msg="x")
    queue.upsert_task("T2")
    queue.mark_phase("T2", new_phase="etl")
    queue.mark_failed("T2", stage="etl", error_msg="y")

    rc = main(["--config", str(cfg), "replay"])
    assert rc == 0

    t1 = queue.get_task("T1")
    t2 = queue.get_task("T2")
    assert t1 is not None and t1.phase == PHASE_PENDING
    assert t2 is not None and t2.phase == PHASE_PENDING


def test_replay_no_batch_option(tmp_path: Path, capsys) -> None:
    """--batch 选项已删除 (契约 §7.5/§7.7): argparse 直接 exit(2)."""
    cfg = _write_config(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(cfg), "replay", "--batch", "1"])
    assert exc_info.value.code == 2


def test_replay_writes_health(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    queue = SQLiteQueue(tmp_path / "q.db")
    queue.upsert_task("T1")
    queue.mark_phase("T1", new_phase="gdr")
    queue.mark_failed("T1", stage="gdr", error_msg="x")
    main(["--config", str(cfg), "replay"])
    health = tmp_path / "logs" / "health.json"
    assert health.exists()
    data = json.loads(health.read_text(encoding="utf-8"))
    assert "phases" in data
    assert "total" in data