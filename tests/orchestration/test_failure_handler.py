"""orchestration.failure_handler 单元测试.

新架构 ``simulation server → gdr → etl``: 死的 task 可能在任意阶段;
failure_handler 把 ``gdr_refined_path`` + ``etl_*_path`` (若存在) 一并移入 dead/.
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestration.failure_handler import reap_dead
from orchestration.queue import SQLiteQueue


def _force_dead(queue: SQLiteQueue, src_path: Path, *, gdr_refined: Path | None = None,
                etl_outputs: dict[str, Path] | None = None) -> int:
    """登记 task 并通过 gdr → etl → 手动 mark_dead 走到 dead 状态.

    gdr_refined: 模拟 gdr 已写 C2 单文件路径
    etl_outputs: 模拟 etl 已写 4 视图, key 必须是 'messages'/'openai'/'qwenjina'/'meta'
    """
    tid, _ = queue.insert(src_path=src_path, run_id="r", session_id="s", batch_id=1)
    queue.pull_pending_gdr(worker_id="w", n=1)
    if gdr_refined is not None:
        gdr_refined.write_text("{}", encoding="utf-8")
        queue.mark_gdr_done(tid, gdr_refined_path=gdr_refined)
        queue.pull_pending_etl(worker_id="w", n=1)
        if etl_outputs is not None:
            queue.mark_etl_done(
                tid,
                etl_messages_path=etl_outputs["messages"],
                etl_openai_path=etl_outputs["openai"],
                etl_qwenjina_path=etl_outputs.get("qwenjina"),
                etl_meta_path=etl_outputs["meta"],
            )
    queue.mark_dead(tid, error_msg="forced")
    return tid


def test_reap_dead_moves_src(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    _force_dead(queue, src)

    dead_dir = tmp_path / "dead"
    archives = reap_dead(queue, dead_dir=dead_dir)
    assert len(archives) == 1
    assert archives[0].moved_to  # 至少移了 1 个
    moved = archives[0].moved_to[0]
    assert moved.startswith(str(dead_dir))
    assert "raw.json" in moved
    assert not src.exists()
    assert Path(moved).is_file()


def test_reap_dead_moves_src_and_gdr_refined(tmp_path: Path) -> None:
    """gdr 阶段已写 C2 → failure_handler 应一并归档."""
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    gdr_refined = tmp_path / "gdr_refined.json"
    _force_dead(queue, src, gdr_refined=gdr_refined)
    # src + gdr_refined 都存在 → 都应被移
    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert len(archives[0].moved_to) == 2
    assert not src.exists()
    assert not gdr_refined.exists()


def test_reap_dead_moves_all_etl_outputs(tmp_path: Path) -> None:
    """etl 阶段已写 4 视图 → failure_handler 应一并归档."""
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    gdr_refined = tmp_path / "refined.json"
    etl_messages = tmp_path / "refined.messages.json"
    etl_openai = tmp_path / "refined.openai.json"
    etl_meta = tmp_path / "refined.meta.json"
    for p in (gdr_refined, etl_messages, etl_openai, etl_meta):
        p.write_text("{}", encoding="utf-8")
    _force_dead(
        queue, src,
        gdr_refined=gdr_refined,
        etl_outputs={
            "messages": etl_messages,
            "openai": etl_openai,
            "meta": etl_meta,
            # qwenjina 故意 None
        },
    )
    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    # src + gdr_refined + 3 个 etl 视图 = 5 个文件
    assert len(archives[0].moved_to) == 5


def test_reap_dead_missing_src_no_crash(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    _force_dead(queue, src)
    src.unlink()  # 模拟源文件已丢失

    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert len(archives) == 1
    assert archives[0].moved_to == []  # 没移成


def test_reap_dead_appends_log(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    _force_dead(queue, src)

    log = tmp_path / "dead.log"
    reap_dead(queue, dead_dir=tmp_path / "dead", dead_log_path=log)
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert "task_id" in entry
    assert "attempts_gdr" in entry
    assert "attempts_etl" in entry
    assert "moved_to" in entry


def test_reap_dead_ignores_non_dead(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    queue.insert(src_path=src, run_id="r", session_id="s", batch_id=1)
    # state = pending, 没走到 dead

    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert archives == []
    assert src.exists()  # 没动


def test_reap_dead_empty_when_no_dead(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert archives == []