"""golden_ls.projects: **版本 → LS 项目** 的本地台账。

为什么必须有台账
----------------
``TRAJECTORY_LS_PROJECT_ID`` 是**单值**环境变量（给 scout 轨迹那个项目
id=77 用的）。而本方案是**一版一项目**（方案 22 §4.1）——每版一个 id，
**没有环境变量可放**。⇒ 台账是必需的，不是可选的。

台账形态：append-only JSONL，一行一个版本::

    {"version": 14, "project_id": 78, "title": "golden-v14", ...}

**纯追加、不重写**（与 ``ls_bridge.push_index`` 同款纪律）：崩溃最多丢最后
一行（下次会补推），不会像「读进来再整体覆盖」那样把一次半截写入放大成
全量损坏。坏行**跳过而不是抛**——台账是索引，LS 才是真相。

``pending``（冻结了但没推的版本）是 owner 裁定「增量版本每个都上传」的
**可执行性抓手**：纪律靠人记会漏，列出来才看得见（方案 22 §2.2 / R10）。
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from trajectory_pipeline.evaluation.golden_ls import dataset

log = logging.getLogger(__name__)

#: 台账文件名（落在 ``trajectory_pipeline/output/ls_bridge/``）。
LEDGER_NAME = "golden_projects.jsonl"

#: **要上传的最低版本号**（低于它的**不算欠账**）。
#:
#: 口径来自 owner 裁定 O-1（方案 22 §2.1）：**存量只上传 v14**，
#: v1..v13 永久留在真源区当只读历史，**不上 LS**；此后每个增量版本都传。
#:
#: ⚠️ 这个常量**必须存在**，否则 ``pending`` 会把 v1..v13 全列成「欠账」——
#: 那等于给一条**要违反 owner 裁定**的指令（有人照着欠账清单就能把历史
#: 全推上去，而历史版本的判定与 v14 不同、复核了也并不到当前基线上）。
#: 裁定变了就改这一个数。
UPLOAD_BASELINE_VERSION = 14


def default_ledger_path(root: pathlib.Path | str | None = None) -> pathlib.Path:
    """台账绝对路径。``root=None`` ⇒ 包内 ``trajectory_pipeline/output/ls_bridge/``。

    ⚠️ 口径与 ``ls_bridge.push_index.default_path`` 一致：包内 ``output/``，
    **不跟 cwd 走**（在仓库根跑与在 ``trajectory_pipeline/`` 里跑读到的不是
    同一个目录，而两处都不会报错）。
    """
    if root is None:
        root = pathlib.Path(__file__).resolve().parents[2] / "output"
    return pathlib.Path(root) / "ls_bridge" / LEDGER_NAME


@dataclass(frozen=True)
class GoldenProject:
    """一个冻结版本对应的 LS 项目。"""

    version: int
    project_id: int
    title: str
    label_config_sha: str = ""
    frozen_at: str = ""
    evaluator_version: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    splits: tuple[str, ...] = ()
    created_at: str = ""

    def to_line(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "project_id": self.project_id,
                "title": self.title,
                "label_config_sha": self.label_config_sha,
                "frozen_at": self.frozen_at,
                "evaluator_version": self.evaluator_version,
                "counts": dict(self.counts),
                "splits": list(self.splits),
                "created_at": self.created_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_dict(cls, raw: Any) -> "GoldenProject | None":
        if not isinstance(raw, dict):
            return None
        version = raw.get("version")
        project_id = raw.get("project_id")
        try:
            version_i = int(version)
            project_id_i = int(project_id)
        except (TypeError, ValueError):
            return None
        counts = raw.get("counts")
        splits = raw.get("splits")
        return cls(
            version=version_i,
            project_id=project_id_i,
            title=str(raw.get("title") or ""),
            label_config_sha=str(raw.get("label_config_sha") or ""),
            frozen_at=str(raw.get("frozen_at") or ""),
            evaluator_version=str(raw.get("evaluator_version") or ""),
            counts=dict(counts) if isinstance(counts, Mapping) else {},
            splits=tuple(str(s) for s in splits) if isinstance(splits, Sequence) else (),
            created_at=str(raw.get("created_at") or ""),
        )


class GoldenProjectIndex:
    """``version → project`` 的本地台账。**只增不改**。"""

    def __init__(
        self, path: pathlib.Path, records: dict[int, GoldenProject] | None = None
    ) -> None:
        self.path = pathlib.Path(path)
        self._records: dict[int, GoldenProject] = records or {}

    @classmethod
    def default_path(cls, root: pathlib.Path | str | None = None) -> pathlib.Path:
        return default_ledger_path(root)

    @classmethod
    def load(cls, path: pathlib.Path | str) -> "GoldenProjectIndex":
        """读台账。**坏行跳过，不抛**——台账是索引，LS 才是真相。"""
        records: dict[int, GoldenProject] = {}
        p = pathlib.Path(path)
        if not p.is_file():
            return cls(p, records)
        skipped = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                skipped += 1
                continue
            record = GoldenProject.from_dict(raw)
            if record is None:
                skipped += 1
                continue
            records[record.version] = record
        if skipped:
            log.warning("GoldenProjectIndex: %s 有 %d 行无法解析, 已跳过", p, skipped)
        return cls(p, records)

    # -- 查询 ---------------------------------------------------------------

    def get(self, version: int) -> GoldenProject | None:
        return self._records.get(int(version))

    def has(self, version: int) -> bool:
        return int(version) in self._records

    def versions(self) -> list[int]:
        return sorted(self._records)

    def __len__(self) -> int:
        return len(self._records)

    # -- 写入 ---------------------------------------------------------------

    def append(self, records: Iterable[GoldenProject]) -> None:
        """追加若干条（**先落盘再改内存**）。"""
        batch = list(records)
        if not batch:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(r.to_line() + "\n" for r in batch)
        # ⚠️ **二进制追加**：文本模式在 Windows 上会把 "\n" 翻成 "\r\n" ⇒
        # 台账变成 CRLF，而 `load()` 用 `splitlines()` 读（读回来没事），
        # 可「同一份台账在不同机器上字节不同」这件事会让以后拿字节比对的
        # 人判成「内容变了」。台账是纯 ASCII + JSON，一行一条，不怕。
        with self.path.open("ab") as handle:
            handle.write(payload.encode("utf-8"))
        for record in batch:
            self._records[record.version] = record

    def record(self, record: GoldenProject) -> None:
        self.append([record])

    def new_record(
        self,
        version: int,
        project_id: int,
        *,
        title: str,
        label_config_sha: str = "",
        frozen_at: str = "",
        evaluator_version: str = "",
        counts: Mapping[str, int] | None = None,
        splits: Sequence[str] = (),
    ) -> GoldenProject:
        """造一条新记录（**不落盘**——调用方决定要不要 :meth:`record`）。"""
        return GoldenProject(
            version=int(version),
            project_id=int(project_id),
            title=title,
            label_config_sha=label_config_sha,
            frozen_at=frozen_at,
            evaluator_version=evaluator_version,
            counts=dict(counts or {}),
            splits=tuple(str(s) for s in splits),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return f"GoldenProjectIndex(path={self.path}, records={len(self._records)})"


def project_title(version: int) -> str:
    """LS 项目标题。**确定性**——``find_project_by_title`` 是精确匹配。"""
    return f"golden-v{int(version)}"


def pending_versions(
    index: GoldenProjectIndex,
    *,
    golden_root: pathlib.Path | str | None = None,
    baseline: int = UPLOAD_BASELINE_VERSION,
) -> list[int]:
    """**冻结了但没推**的版本（升序）。

    「没推」的判据 = 台账里没有这一版。台账在 ``init-project`` 时就写入
    （那时项目已建），所以这个列表就是「欠账清单」。

    ⚠️ **只算 ``baseline`` 及以上的版本**：低于它的按裁定 O-1 **不上 LS**
    （存量只上传 v14），**不算欠账**。把它们列进来就是在发一条会违反
    owner 裁定的指令。
    """
    all_versions = dataset.frozen_versions(root=golden_root)
    return [v for v in all_versions if v >= int(baseline) and not index.has(v)]


__all__ = [
    "LEDGER_NAME",
    "UPLOAD_BASELINE_VERSION",
    "GoldenProject",
    "GoldenProjectIndex",
    "default_ledger_path",
    "pending_versions",
    "project_title",
]
