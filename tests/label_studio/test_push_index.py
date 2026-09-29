"""label_studio.push_index: 推送台账。

背景见模块 docstring —— 方案 §16 R7「靠 LS 原生 ``inner_id = session_id``
去重, 不引本地索引文件」在 LS 1.23 上被实测推翻, 这个索引就是那个被排除掉的
本地索引。这里守它的三条性质: **只增不改**、**坏行不炸**、**同 session 留最新**。
"""

from __future__ import annotations

import json
from pathlib import Path

from label_studio.push_index import PushIndex, PushRecord


def test_default_path_is_per_project(tmp_path: Path):
    a = PushIndex.default_path(9, tmp_path)
    b = PushIndex.default_path(10, tmp_path)
    assert a != b
    assert a.parent == tmp_path / "label_studio"
    assert a.name == "push_index__9.jsonl"


def test_roundtrip(tmp_path: Path):
    index = PushIndex.default_path(1, tmp_path)
    PushIndex.load(index).record("sess-A", 42, task_ref="T001")
    reloaded = PushIndex.load(index)
    assert reloaded.has("sess-A")
    assert reloaded.task_id("sess-A") == 42
    assert reloaded.get("sess-A").task_ref == "T001"
    assert reloaded.max_task_id() == 42


def test_missing_file_is_empty_not_an_error(tmp_path: Path):
    index = PushIndex.load(tmp_path / "nope.jsonl")
    assert len(index) == 0
    assert index.has("sess-A") is False
    assert index.task_id("sess-A") is None


def test_append_never_rewrites(tmp_path: Path):
    """崩溃最坏丢最后一行, 不会把一次半截写入放大成全量损坏。"""
    path = PushIndex.default_path(1, tmp_path)
    PushIndex.load(path).record("sess-A", 1)
    PushIndex.load(path).record("sess-B", 2)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["session_id"] for line in lines] == ["sess-A", "sess-B"]


def test_corrupt_lines_are_skipped_not_fatal(tmp_path: Path):
    """台账是优化不是真相来源 —— LS 才是。坏了不能拦住推送。"""
    path = PushIndex.default_path(1, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"session_id": "good", "ls_task_id": 7}\n'
        "{这行是半截写入\n"
        '{"session_id": "no-id"}\n'
        "\n"
        '[1, 2, 3]\n',
        encoding="utf-8",
    )
    index = PushIndex.load(path)
    assert index.known_session_ids() == {"good"}
    assert index.task_id("good") == 7


def test_repush_keeps_latest_task_id(tmp_path: Path):
    """同 session 推了多次时, 拿到的是最新那条 task。"""
    path = PushIndex.default_path(1, tmp_path)
    index = PushIndex.load(path)
    index.record("sess-A", 1)
    index.record("sess-A", 9)
    assert index.task_id("sess-A") == 9
    # 但磁盘上是两行 —— 只增不改
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    assert PushIndex.load(path).task_id("sess-A") == 9


def test_record_many_shares_one_timestamp(tmp_path: Path):
    path = PushIndex.default_path(1, tmp_path)
    index = PushIndex.load(path)
    index.record_many([("s1", 1), ("s2", 2)], task_refs={"s1": "T001"})
    assert index.get("s1").task_ref == "T001"
    assert index.get("s2").task_ref == ""
    assert index.get("s1").pushed_at == index.get("s2").pushed_at


def test_append_is_flushed_before_memory_updates(tmp_path: Path):
    """崩了宁可内存里没有, 也不能出现"内存说有、盘上没有"的幽灵记录。"""
    path = PushIndex.default_path(1, tmp_path)
    index = PushIndex.load(path)
    index.record("sess-A", 42)
    # 立即用一个**新实例**读盘, 不依赖内存态
    assert PushIndex.load(path).task_id("sess-A") == 42


def test_from_dict_rejects_bad_shapes():
    assert PushRecord.from_dict(None) is None
    assert PushRecord.from_dict({"ls_task_id": 1}) is None       # 缺 session_id
    assert PushRecord.from_dict({"session_id": "a"}) is None     # 缺 id
    assert PushRecord.from_dict({"session_id": "a", "ls_task_id": "x"}) is None
    assert PushRecord.from_dict({"session_id": "a", "ls_task_id": "5"}).ls_task_id == 5


# ---------------------------------------------------------------------------
# 并发写
# ---------------------------------------------------------------------------


def test_concurrent_processes_do_not_lose_records(tmp_path: Path):
    """真并发: 4 个**独立进程**写同一台账, 一行都不能丢、不能撕。

    ``orchestration --parallelism ≥2`` 就是这么跑的 —— N 个
    ``multiprocessing.Pool`` worker 各推各的 task, 全部写同一个
    ``push_index__<project_id>.jsonl``。

    丢行的后果不是"少一条历史", 是**重复推送**: LS 1.23 不去重, 台账是
    唯一防线。所以这个测试守的是正确性, 不是性能。

    用独立 ``python -c`` 子进程而不是 in-process stub: 线程共享 GIL 且单次
    ``write`` 通常原子, 根本测不出无锁的交错。worker 代码写成字符串是刻意
    的 —— ``tests/`` 不是包, 模块级函数没法被 spawn 重新 import。
    """
    import subprocess
    import sys

    path = PushIndex.default_path(1, tmp_path)
    PushIndex.load(path).record("seed", 0)  # 先落一条, 让文件非空

    repo_root = Path(__file__).resolve().parents[2]
    procs = []
    for worker in range(4):
        src = (
            "import sys\n"
            f"sys.path.insert(0, {str(repo_root)!r})\n"
            "from label_studio.push_index import PushIndex\n"
            f"idx = PushIndex.load({str(path)!r})\n"
            "for i in range(40):\n"
            f"    idx.record('p{worker}-' + str(i), i)\n"
        )
        procs.append(subprocess.Popen([sys.executable, "-c", src]))

    for proc in procs:
        assert proc.wait(timeout=120) == 0, "worker 非零退出 = 锁或写坏了"

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 4 * 40 + 1
    # 每行都必须是完整可解析的记录 —— 半行交错是"看起来没丢但其实丢了"
    sessions = {json.loads(ln)["session_id"] for ln in lines}
    assert len(sessions) == 4 * 40 + 1


def test_lock_file_never_pollutes_the_ledger(tmp_path: Path):
    """锁加在 ``<台账>.lock`` 上, 台账本身必须保持纯 JSONL。

    Windows 的 ``msvcrt.locking`` 要求被锁字节真实存在; 拿去锁台账就得往
    台账里塞占位字节, 首行会被污染。这里守住改法 —— 曾踩过。
    """
    path = PushIndex.default_path(1, tmp_path)
    PushIndex.load(path).record("sess-A", 1)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("{"), f"台账首行被占位字节污染: {text[:40]!r}"
    for line in text.splitlines():
        assert json.loads(line)["session_id"] == "sess-A"
    assert path.with_name(path.name + ".lock").exists()
