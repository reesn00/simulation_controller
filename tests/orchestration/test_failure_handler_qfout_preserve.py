"""orchestration.failure_handler Fix C: 保留 qf_out + INDEX.jsonl + reprocess_dead.

设计:
  - reap_dead 默认 preserve_qf_out_in_dead=True, 把 qf_output 同步复制到
    dead_dir/qf_out/<basename>; dead.log 加 qf_out_dead_path 字段
  - dead_index_path 追加一行 INDEX.jsonl, 含 gdr_status / score / reason
    (从 error_msg 解析)
  - reprocess_dead 从 INDEX.jsonl 读, 按 score / status 过滤, 把 qf_out
    拷回目标目录
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestration.failure_handler import reap_dead, reprocess_dead
from orchestration.queue import SQLiteQueue


def _force_dead(queue: SQLiteQueue, src_path: Path, *, qf_output: Path | None = None,
                error_msg: str = "forced", qf_content: str = "{}") -> int:
    tid, _ = queue.insert(src_path=src_path, run_id="r", session_id="s", batch_id=1)
    queue.pull_pending_qf(worker_id="w", n=1)
    if qf_output is not None:
        qf_output.write_text(qf_content, encoding="utf-8")
        queue.mark_qf_done(tid, qf_output_path=qf_output)
    queue.mark_dead(tid, error_msg=error_msg)
    return tid


# ---------------------------------------------------------------------------
# qf_out 保留到 dead/qf_out/
# ---------------------------------------------------------------------------


class TestQfOutPreservedInDead:
    def test_qf_out_copied_to_dead_qf_out(self, tmp_path: Path) -> None:
        queue = SQLiteQueue(tmp_path / "q.db")
        src = tmp_path / "raw.json"
        src.write_text("{}", encoding="utf-8")
        qf = tmp_path / "qf.json"
        _force_dead(queue, src, qf_output=qf, qf_content='{"k": 1}')

        dead_dir = tmp_path / "dead"
        archives = reap_dead(queue, dead_dir=dead_dir)
        assert len(archives) == 1

        qf_out_dead = dead_dir / "qf_out" / "qf.json"
        assert qf_out_dead.is_file(), "qf_out 必须被复制到 dead/qf_out/"
        assert qf_out_dead.read_text(encoding="utf-8") == '{"k": 1}'

    def test_qf_out_dead_path_in_log(self, tmp_path: Path) -> None:
        queue = SQLiteQueue(tmp_path / "q.db")
        src = tmp_path / "raw.json"
        src.write_text("{}", encoding="utf-8")
        qf = tmp_path / "qf.json"
        qf.write_text("{}", encoding="utf-8")
        _force_dead(queue, src, qf_output=qf)

        log = tmp_path / "dead.log"
        dead_dir = tmp_path / "dead"
        reap_dead(queue, dead_dir=dead_dir, dead_log_path=log)
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert entry["qf_out_dead_path"].endswith("qf.json")
        assert "qf_out" in entry["qf_out_dead_path"]

    def test_preserve_disabled_falls_back_to_legacy(self, tmp_path: Path) -> None:
        """preserve_qf_out_in_dead=False 退回旧行为: 不创建 dead/qf_out/, 日志无 qf_out_dead_path."""
        queue = SQLiteQueue(tmp_path / "q.db")
        src = tmp_path / "raw.json"
        src.write_text("{}", encoding="utf-8")
        qf = tmp_path / "qf.json"
        qf.write_text("{}", encoding="utf-8")
        _force_dead(queue, src, qf_output=qf)

        log = tmp_path / "dead.log"
        dead_dir = tmp_path / "dead"
        reap_dead(
            queue, dead_dir=dead_dir, dead_log_path=log,
            preserve_qf_out_in_dead=False,
        )
        assert not (dead_dir / "qf_out").exists()
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert entry.get("qf_out_dead_path") is None

    def test_no_qf_output_skips_preserve(self, tmp_path: Path) -> None:
        """task 没有 qf_output_path 时, 不应崩溃也不应建 dead/qf_out/."""
        queue = SQLiteQueue(tmp_path / "q.db")
        src = tmp_path / "raw.json"
        src.write_text("{}", encoding="utf-8")
        _force_dead(queue, src)  # 无 qf_output

        dead_dir = tmp_path / "dead"
        log = tmp_path / "dead.log"
        # preserve=True 但无 qf_output, 不应报错
        archives = reap_dead(queue, dead_dir=dead_dir, dead_log_path=log)
        assert len(archives) == 1
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert entry.get("qf_out_dead_path") is None


# ---------------------------------------------------------------------------
# INDEX.jsonl: gdr_status / score / reason 解析
# ---------------------------------------------------------------------------


class TestDeadIndexJsonl:
    def test_index_written_with_parsed_fields(self, tmp_path: Path) -> None:
        queue = SQLiteQueue(tmp_path / "q.db")
        src = tmp_path / "raw.json"
        src.write_text("{}", encoding="utf-8")
        qf = tmp_path / "qf.json"
        qf.write_text("{}", encoding="utf-8")
        _force_dead(
            queue, src, qf_output=qf,
            error_msg=(
                "[non-retryable] NonRetryableError: gdr worker gdr_0: "
                "gdr status='discard' (task=1) \n"
                "Traceback...\n"
                "score=2 min_score=7"
            ),
        )

        index = tmp_path / "INDEX.jsonl"
        dead_dir = tmp_path / "dead"
        reap_dead(queue, dead_dir=dead_dir, dead_index_path=index)
        assert index.exists()
        records = [json.loads(ln) for ln in index.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 1
        rec = records[0]
        assert rec["gdr_status"] == "discard"
        assert rec["score"] == 2
        assert "NonRetryableError" in rec["reason"]
        assert rec["qf_out_dead_path"] is not None

    def test_index_skipped_when_no_dead_index_path(self, tmp_path: Path) -> None:
        """dead_index_path=None 时不创建 INDEX.jsonl."""
        queue = SQLiteQueue(tmp_path / "q.db")
        src = tmp_path / "raw.json"
        src.write_text("{}", encoding="utf-8")
        _force_dead(queue, src)

        reap_dead(queue, dead_dir=tmp_path / "dead")  # 无 dead_index_path
        # 不应创建 INDEX.jsonl
        assert not (tmp_path / "dead" / "INDEX.jsonl").exists()

    def test_index_handles_unparseable_error_msg(self, tmp_path: Path) -> None:
        """error_msg 无 status / score 时, 字段落 None (不崩溃)."""
        queue = SQLiteQueue(tmp_path / "q.db")
        src = tmp_path / "raw.json"
        src.write_text("{}", encoding="utf-8")
        _force_dead(queue, src, error_msg="plain error without status")

        index = tmp_path / "INDEX.jsonl"
        dead_dir = tmp_path / "dead"
        reap_dead(queue, dead_dir=dead_dir, dead_index_path=index)
        records = [json.loads(ln) for ln in index.read_text(encoding="utf-8").splitlines()]
        rec = records[0]
        assert rec["gdr_status"] is None
        assert rec["score"] is None


# ---------------------------------------------------------------------------
# reprocess_dead CLI: 按 score / status 过滤回灌
# ---------------------------------------------------------------------------


class TestReprocessDead:
    def _make_dead_with_qf_out(
        self, tmp_path: Path, qf_basename: str, error_msg: str,
    ) -> Path:
        """构造 dead 归档 (含 qf_out + INDEX.jsonl), 返回 dead_dir 路径."""
        queue = SQLiteQueue(tmp_path / "q.db")
        src = tmp_path / f"raw_for_{qf_basename}.json"
        src.write_text("{}", encoding="utf-8")
        qf = tmp_path / qf_basename
        _force_dead(queue, src, qf_output=qf, error_msg=error_msg)

        dead_dir = tmp_path / "dead"
        index = dead_dir / "INDEX.jsonl"
        reap_dead(queue, dead_dir=dead_dir, dead_index_path=index)
        return dead_dir

    def test_reprocess_copies_all_when_no_filter(self, tmp_path: Path) -> None:
        self._make_dead_with_qf_out(
            tmp_path, "low.json",
            "NonRetryableError: gdr status='discard' (task=1) score=2",
        )
        dead_dir = tmp_path / "dead"
        index = dead_dir / "INDEX.jsonl"
        target = tmp_path / "qf_out"

        copied = reprocess_dead(
            dead_dir=dead_dir, qf_out_target=target, dead_index_path=index,
        )
        assert len(copied) == 1
        assert (target / "low.json").is_file()

    def test_reprocess_filters_by_score(self, tmp_path: Path) -> None:
        """filter_score_lt=5 时, 只拷 score<5 的 task."""
        self._make_dead_with_qf_out(
            tmp_path, "low2.json",
            "NonRetryableError: gdr status='discard' score=2",
        )
        self._make_dead_with_qf_out(
            tmp_path, "high.json",
            "NonRetryableError: gdr status='discard' score=8",
        )
        dead_dir = tmp_path / "dead"
        index = dead_dir / "INDEX.jsonl"
        target = tmp_path / "qf_out"

        copied = reprocess_dead(
            dead_dir=dead_dir, qf_out_target=target, dead_index_path=index,
            filter_score_lt=5,
        )
        assert len(copied) == 1
        assert (target / "low2.json").is_file()
        assert not (target / "high.json").exists()

    def test_reprocess_filters_by_status(self, tmp_path: Path) -> None:
        """filter_gdr_status='dead' 时, 只拷 status=dead 的 task."""
        self._make_dead_with_qf_out(
            tmp_path, "d1.json",
            "NonRetryableError: gdr status='dead' (task=1)",
        )
        self._make_dead_with_qf_out(
            tmp_path, "d2.json",
            "NonRetryableError: gdr status='discard' (task=1)",
        )
        dead_dir = tmp_path / "dead"
        index = dead_dir / "INDEX.jsonl"
        target = tmp_path / "qf_out"

        copied = reprocess_dead(
            dead_dir=dead_dir, qf_out_target=target, dead_index_path=index,
            filter_gdr_status="dead",
        )
        assert len(copied) == 1
        assert (target / "d1.json").is_file()
        assert not (target / "d2.json").exists()

    def test_reprocess_no_index_returns_empty(self, tmp_path: Path) -> None:
        target = tmp_path / "qf_out"
        copied = reprocess_dead(
            dead_dir=tmp_path / "dead", qf_out_target=target,
            dead_index_path=tmp_path / "missing_INDEX.jsonl",
        )
        assert copied == []

    def test_reprocess_skips_missing_qf_out_files(self, tmp_path: Path) -> None:
        """INDEX.jsonl 引用了已不存在的 qf_out_dead_path → 跳过不崩."""
        dead_dir = tmp_path / "dead"
        (dead_dir / "qf_out").mkdir(parents=True)
        index = dead_dir / "INDEX.jsonl"
        index.write_text(
            json.dumps({
                "task_id": 1, "batch_id": 1,
                "gdr_status": "discard", "score": 2,
                "qf_out_dead_path": str(dead_dir / "qf_out" / "gone.json"),
            }) + "\n",
            encoding="utf-8",
        )
        target = tmp_path / "qf_out"
        copied = reprocess_dead(
            dead_dir=dead_dir, qf_out_target=target, dead_index_path=index,
        )
        assert copied == []  # 跳过不存在的文件
