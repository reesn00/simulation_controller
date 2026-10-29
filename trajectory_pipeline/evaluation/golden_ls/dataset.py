"""golden_ls.dataset: 读冻结档（manifest / 三档行 / 版本列表）。

**只读**。冻结档是真源区（``evaluation/golden/data/``），本模块一个字都不写。

三档行读取**复用** ``calibration.worklist.load_frozen_rows``——它已经在做
「三档都读 + 打 ``__split__`` 标注」这件事；另写一份就是两份查找逻辑必然
漂，而漂了不会报错（只会读到别的档的行）。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from trajectory_pipeline.evaluation.calibration.worklist import load_frozen_rows
from trajectory_pipeline.evaluation.golden.builder import GOLDEN_ROOT

#: 版本目录的命名形态（``v{N}``）——冻结档的唯一形态。
VERSION_DIR_PREFIX = "v"


class DatasetError(RuntimeError):
    """冻结档读不出来（版本不存在 / manifest 缺失 / 坏档）。

    **不是驳回**：是「输入不成立」，调用方退 2，不猜、不给空读数。
    """


def golden_root(root: pathlib.Path | str | None = None) -> pathlib.Path:
    """黄金集真源根。``None`` ⇒ 包内 ``evaluation/golden/data``（不跟 cwd 走）。"""
    return pathlib.Path(root) if root else GOLDEN_ROOT


def version_dir(version: int, *, root: pathlib.Path | str | None = None) -> pathlib.Path:
    return golden_root(root) / f"{VERSION_DIR_PREFIX}{int(version)}"


def read_manifest(
    version: int, *, root: pathlib.Path | str | None = None
) -> dict[str, Any]:
    """读某一版的 ``manifest.json``。读不到 ⇒ 抛 :class:`DatasetError`。"""
    path = version_dir(version, root=root) / "manifest.json"
    if not path.exists():
        raise DatasetError(
            f"v{version} 的 manifest 不存在：{path}；先 freeze 出这一版"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"v{version} 的 manifest 读不出来（{path}）：{exc}") from exc
    if not isinstance(data, dict):
        raise DatasetError(f"v{version} 的 manifest 形态异常（{path}）")
    return data


def version_meta(
    version: int, *, root: pathlib.Path | str | None = None
) -> dict[str, Any]:
    """本版的元信息（进 task.data 区块①）。

    取自 manifest；manifest 里没有的字段留空串——**不猜**。
    """
    manifest = read_manifest(version, root=root)
    return {
        "version": int(version),
        "frozen_at": str(manifest.get("frozen_at") or ""),
        "label_schema_version": str(manifest.get("label_schema_version") or ""),
        "rubric_version": str(manifest.get("rubric_version") or ""),
        "evaluator_version": str(manifest.get("evaluator_version") or ""),
        "scorer_fingerprint": str(manifest.get("scorer_fingerprint") or ""),
        "counts": dict(manifest.get("counts") or {}),
    }


def load_rows(
    version: int, *, root: pathlib.Path | str | None = None
) -> list[dict[str, Any]]:
    """某一版的**全部三档**行（带 ``__split__``）。"""
    return load_frozen_rows(golden_root(root), int(version))


def frozen_versions(*, root: pathlib.Path | str | None = None) -> list[int]:
    """真源区里存在的全部版本号（升序）。

    只认目录名是 ``v{N}`` 的（N 是数字）——``v`` 开头但不是数字的目录
    （比如 ``v1_backup``）不是冻结档，不算版本。
    """
    base = golden_root(root)
    if not base.is_dir():
        return []
    out: list[int] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not name.startswith(VERSION_DIR_PREFIX):
            continue
        suffix = name[len(VERSION_DIR_PREFIX):]
        if not suffix.isdigit():
            continue
        out.append(int(suffix))
    return sorted(out)


__all__ = [
    "DatasetError",
    "VERSION_DIR_PREFIX",
    "frozen_versions",
    "golden_root",
    "load_rows",
    "read_manifest",
    "version_dir",
    "version_meta",
]
