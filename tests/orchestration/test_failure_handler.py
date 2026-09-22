"""orchestration.failure_handler 单元测试 (新架构 simulation server → gdr → etl).

死信可能落在任意阶段: simulate / gdr / etl.
failure_handler 把 ``src_path`` + ``gdr_refined_path`` + ``etl_*_path`` (若存在)
一并移入 dead/. 旧 batch_id 字段已删除 (契约 §6.4).
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestration.failure_handler import reap_dead
from orchestration.queue import SQLiteQueue


def _seed_dead(
    queue: SQLiteQueue, src_path: Path,
    gdr_refined: Path | None = None,
    etl_outputs: dict[str, Path] | None = None,
) -> None:
    """登记 task + src_path + (可选 gdr_refined + etl) 后 mark_failed → dead."""
    queue.upsert_task("r__s")
    queue.mark_phase("r__s", new_phase="gdr", src_path=src_path)
    if gdr_refined is not None:
        gdr_refined.write_text("{}", encoding="utf-8")
        queue.mark_phase(
            "r__s", new_phase="etl", gdr_refined_path=gdr_refined,
        )
        if etl_outputs is not None:
            queue.mark_phase(
                "r__s", new_phase="done",
                etl_messages_path=etl_outputs["messages"],
                etl_openai_path=etl_outputs["openai"],
                etl_qwenjina_path=etl_outputs.get("qwenjina"),
                etl_meta_path=etl_outputs["meta"],
            )
    queue.mark_failed("r__s", stage="gdr", error_msg="forced")


def test_reap_dead_moves_src(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    _seed_dead(queue, src)

    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert len(archives) == 1
    assert archives[0].moved_to  # 至少移了 1 个
    moved = archives[0].moved_to[0]
    assert moved.startswith(str(tmp_path / "dead"))
    assert "raw.json" in moved
    assert not src.exists()
    assert Path(moved).is_file()


def test_reap_dead_moves_src_and_gdr_refined(tmp_path: Path) -> None:
    """gdr 阶段已写 C2 → failure_handler 应一并归档."""
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    gdr_refined = tmp_path / "gdr_refined.json"
    gdr_refined.write_text("{}", encoding="utf-8")
    _seed_dead(queue, src, gdr_refined=gdr_refined)
    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    # src + gdr_refined = 2 个
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
    _seed_dead(
        queue, src, gdr_refined=gdr_refined,
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
    _seed_dead(queue, src)
    src.unlink()  # 模拟源文件已丢失

    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert len(archives) == 1
    assert archives[0].moved_to == []  # 没移成


def test_reap_dead_appends_log(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    _seed_dead(queue, src)

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
    # 新架构删 batch_id (契约 §6.4)
    assert "batch_id" not in entry


def test_reap_dead_log_no_batch_id(tmp_path: Path) -> None:
    """契约 §6.4: log_entry 不再有 batch_id 字段."""
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    _seed_dead(queue, src)
    reap_dead(
        queue, dead_dir=tmp_path / "dead",
        dead_log_path=tmp_path / "dead.log",
    )
    entry = json.loads(
        (tmp_path / "dead.log").read_text(encoding="utf-8").strip(),
    )
    assert "batch_id" not in entry


def test_reap_dead_ignores_non_dead(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    queue.upsert_task("r__s")
    # phase = pending, 没走到 dead

    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert archives == []
    assert src.exists()  # 没动


def test_reap_dead_empty_when_no_dead(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert archives == []


def test_dead_archive_no_batch_id_field(tmp_path: Path) -> None:
    """DeadArchive dataclass 不再有 batch_id 字段 (契约 §6.4)."""
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    _seed_dead(queue, src)
    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert not hasattr(archives[0], "batch_id")