"""ls_bridge.push_index: LS 端已推 task 的**本地台账**。

照读自 ``archive/v1/label_studio/push_index.py``, 主键 ``session_id`` 语义
改为 ``sample_id``（P3 视图按 sample_id 命名, 天然唯一）。

**为什么需要它**（全部是对 LS 1.23.0 的实测结论, 不是推测）:

* ``Task.inner_id`` 是**整数字段**。发字符串直接 400; 批量 ``/import`` 更是
  **静默丢弃** —— 发 ``s-A / s-A / s-B`` 得到 3 条独立 task。
* **LS 自己不去重**。整数 inner_id 重复导入照样每次新建 task。
* ``GET /api/tasks?inner_id=`` 过滤被**忽略**; ``fields=`` 参数也被忽略,
  永远返回全量 task。

所以本地台账是**唯一去重防线** —— 重跑一次 ``upload`` 不会堆重复样本。
顺带解决第二个问题: ``import/predictions`` 的 ``task`` 字段只认 LS 侧的
**数字 task id**, 而 ``/import`` 的返回体只有计数, 手上没有 id 就推不了
预标注。

格式: append-only JSONL, 一行一条::

    {"sample_id": "...", "ls_task_id": 42, "task_ref": "T001", "pushed_at": "..."}

刻意保持**纯追加、不重写** —— 崩溃最多丢最后一行（下次会重推这一条）,
不会像"整份读进来再整体覆盖"那样把一次半截写入放大成全量损坏。
坏行一律跳过而不是抛异常: 台账是优化, 不是真相来源, LS 才是。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger(__name__)


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[Any]:
    """跨平台排他文件锁, 包住「判重-追加」的整个写窗口。

    ``yield`` 出去的是那个已持锁的二进制句柄（``a+b``, 写入恒在末尾）。

    锁加在**独立的 ``<台账>.lock`` 文件**上, 不动台账本身:
    Windows 的 ``msvcrt.locking`` 锁的是"当前文件位置起的 N 字节", 要求
    目标至少 1 字节存在; 拿去锁台账就得往台账里塞占位字节, 污染首行。
    锁文件只写一个 0 字节占位, 永不删除, 台账保持纯 JSONL。
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if lock_path.stat().st_size == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # 锁只保护写入窗口; 台账句柄在锁内另开, 保持 a+b 的追加语义
            with path.open("a+b") as handle:
                yield handle
                handle.flush()
        finally:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        lock.close()


@dataclass(frozen=True)
class PushRecord:
    """一条已推送记录。"""

    sample_id: str
    ls_task_id: int
    task_ref: str = ""
    pushed_at: str = ""

    def to_line(self) -> str:
        return json.dumps(
            {
                "sample_id": self.sample_id,
                "ls_task_id": self.ls_task_id,
                "task_ref": self.task_ref,
                "pushed_at": self.pushed_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_dict(cls, raw: Any) -> PushRecord | None:
        if not isinstance(raw, dict):
            return None
        sample_id = raw.get("sample_id")
        ls_task_id = raw.get("ls_task_id")
        if not isinstance(sample_id, str) or not sample_id:
            return None
        try:
            task_id = int(ls_task_id)
        except (TypeError, ValueError):
            return None
        return cls(
            sample_id=sample_id,
            ls_task_id=task_id,
            task_ref=str(raw.get("task_ref") or ""),
            pushed_at=str(raw.get("pushed_at") or ""),
        )


class PushIndex:
    """``sample_id → LS task id`` 的本地映射。**只增不改**。"""

    def __init__(self, path: Path, records: dict[str, PushRecord] | None = None) -> None:
        self.path = Path(path)
        self._records: dict[str, PushRecord] = records or {}

    # -- 构造 ---------------------------------------------------------------

    @classmethod
    def default_path(cls, project_id: int, root: Path | str | None = None) -> Path:
        """``<root>/ls_bridge/push_index__<project_id>.jsonl``。

        按项目 id 分文件: 换项目 / ``purge`` 重建后不会串味。
        ``root=None`` 时落在包内 ``trajectory_pipeline/output/ls_bridge/``
        （D8 产物隔离: 不碰仓库根 output/）, 口径同
        :func:`trajectory_pipeline.storage.views_dir`。
        """
        if root is None:
            root = Path(__file__).resolve().parents[2] / "output"
        return Path(root) / "ls_bridge" / f"push_index__{int(project_id)}.jsonl"

    @classmethod
    def load(cls, path: Path) -> PushIndex:
        """读台账。**坏行跳过, 不抛异常** —— 见模块 docstring。"""
        records: dict[str, PushRecord] = {}
        path = Path(path)
        if not path.is_file():
            return cls(path, records)
        skipped = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                skipped += 1
                continue
            record = PushRecord.from_dict(raw)
            if record is None:
                skipped += 1
                continue
            # 同 sample 多次推送: 留**最后一次** —— 拿到的是最新那条 task。
            records[record.sample_id] = record
        if skipped:
            log.warning("PushIndex: %s 有 %d 行无法解析, 已跳过", path, skipped)
        return cls(path, records)

    # -- 查询 ---------------------------------------------------------------

    def has(self, sample_id: str) -> bool:
        return sample_id in self._records

    def task_id(self, sample_id: str) -> int | None:
        record = self._records.get(sample_id)
        return record.ls_task_id if record else None

    def get(self, sample_id: str) -> PushRecord | None:
        return self._records.get(sample_id)

    def known_sample_ids(self) -> set[str]:
        return set(self._records)

    def max_task_id(self) -> int:
        return max((r.ls_task_id for r in self._records.values()), default=0)

    def __len__(self) -> int:
        return len(self._records)

    # -- 写入 ---------------------------------------------------------------

    def record(self, sample_id: str, ls_task_id: int, *, task_ref: str = "") -> None:
        self.append([PushRecord(
            sample_id=sample_id,
            ls_task_id=int(ls_task_id),
            task_ref=task_ref,
            pushed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )])

    def append(self, records: Iterable[PushRecord]) -> None:
        """追加若干条。**先落盘再改内存** —— 崩了宁可内存里没有, 也不能
        出现"内存说有、盘上没有"的幽灵记录。

        整个写窗口在 :func:`_exclusive_lock` 里: 并发写不锁就会交错出半行,
        而台账丢行 = 下次 upload 重复推送。
        """
        batch = list(records)
        if not batch:
            return
        payload = "".join(record.to_line() + "\n" for record in batch)
        with _exclusive_lock(self.path) as handle:
            handle.write(payload.encode("utf-8"))
        for record in batch:
            self._records[record.sample_id] = record

    def record_many(
        self, pairs: Iterable[tuple[str, int]], *, task_refs: dict[str, str] | None = None
    ) -> None:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        refs = task_refs or {}
        self.append(
            PushRecord(
                sample_id=sample_id,
                ls_task_id=int(ls_task_id),
                task_ref=refs.get(sample_id, ""),
                pushed_at=stamp,
            )
            for sample_id, ls_task_id in pairs
        )

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return f"PushIndex(path={self.path}, records={len(self._records)})"


__all__ = ["PushIndex", "PushRecord"]
