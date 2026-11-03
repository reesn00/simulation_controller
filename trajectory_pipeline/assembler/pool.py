"""分池：交付集只含 ``camofox-1``，旧面进 ``legacy/`` 只读存量（方案 23 R3-3）。

**为什么必须分池**
------------------

``obscura-1`` 的 assistant 消息**没有 ``tool_calls``**（纯 ``[动作] goto(…)``
文本形态），``camofox-1`` 是 function-calling。两代面混在同一批交付集里，
训练方看到的是两种互斥的「模型该怎么说话」，而报表上 131 条样本一条不少
——分母没错，**口径错了**。这不是质量瑕疵，是标签轴上的两代格式。

**owner 裁定 O-3（2026-11-02）**：旧面按「只读存量」**单独归档**，
交付集只含 :data:`DELIVERY_SURFACE`。两个词各有分量：

- **归档** = 移动，不是删除。磁盘上仍读得出来（``legacy/<surface>/<kind>/``），
  需要回溯旧批次时它还在那里——「退役 ≠ 失忆」。
- **只读存量** = 新批次不再往里写。重建旧面 P1 时（``--all --overwrite``）
  仍会重渲染到对应 legacy 池，但那是重建既有证据，不是新增交付样本。

**分池是写入时的动作，不是读取时的推导**
------------------------------------------

池目录就是事实本身：``samples/`` / ``views/`` / ``task_views/`` 里出现的
每一份都属于交付面；旧面文件被**移动**（``os.replace``）到
``legacy/<surface>/<kind>/``，读取端不需要再按字段过滤一遍。理由同
CLAUDE.md 的并池铁律——读取时推导会让「池里有什么」随代码版本漂移，
而写入时落定的文件不会。

**池变小不是质量变好**
----------------------

旧面 66 份退出交付集（52 obscura-1 + 14 camoufox-1），分母从 131 掉到 65。
不记批次元数据就会把这个跳变读成「质量提升了」。所以每次分池都写
:data:`POOL_META_FILENAME`（面 + 计数 + 时间 + 本次移动了多少），
风险 R-5 的落点就在这张表上。

**路径**
--------

::

    output/pipeline/samples/<sample_id>.json                      # 交付（camofox-1）
    output/pipeline/views/<sample_id>_{messages,openai,meta}.json # 交付
    output/pipeline/task_views/<task_id>__<run_id>.json           # 交付
    output/pipeline/legacy/<surface>/{samples,views,task_views}/… # 只读存量
    output/pipeline/pool_meta.json                                # 批次元数据

本模块**不 import** :mod:`builder` / :mod:`views` / :mod:`task_view`——那三个
是使用方，反向 import 会成环。它只认「某个 kind 目录」（``delivery_dir``）
与 surface 字段，布局字面量只有 ``legacy`` 与 ``pool_meta.json`` 两处。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from trajectory_pipeline import toolcatalog
from trajectory_pipeline.storage import VIEW_NAMES

#: 交付面。**只此一个**——owner 裁定 O-3：「交付集只含 ``camofox-1``」。
DELIVERY_SURFACE = toolcatalog.SURFACE_CAMOFOX

#: 旧面归档目录名（落在 ``output/pipeline/`` 下）。
LEGACY_DIRNAME = "legacy"

#: 池元数据文件名（落在 ``output/pipeline/`` 下）。
POOL_META_FILENAME = "pool_meta.json"

#: 认得出的面。三个都是 ``toolcatalog`` 声明过的——**认不出的面不移动、
#: 不归类**（记进 ``unclassified`` 等人看）：猜一个面就是让文件去错池，
#: 而错池不会报错，只会在训练方那里变成两代格式混排。
KNOWN_SURFACES = frozenset({
    toolcatalog.SURFACE_OBSCURA,
    toolcatalog.SURFACE_CAMOFOX,
    toolcatalog.SURFACE_CAMOUFOX,
})


def is_delivery(surface: str) -> bool:
    """这个面进交付池吗？"""
    return surface == DELIVERY_SURFACE


def pool_root(root: str | Path, surface: str) -> Path:
    """某个面该用的**落盘根**（可直接喂给 ``storage.*_dir`` / ``samples_dir``）。

    交付面 → 原根不变；旧面 → ``<root>/legacy/<surface>``。
    认不出的面直接抛：**不猜**（见 :data:`KNOWN_SURFACES`）。
    """
    if surface not in KNOWN_SURFACES:
        raise ValueError(
            f"认不出的工具面 {surface!r}（已知：{sorted(KNOWN_SURFACES)}）"
            "——不猜、不静默落进交付池"
        )
    base = Path(root)
    if is_delivery(surface):
        return base
    return base / LEGACY_DIRNAME / surface


# ── 移动 ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PoolMigration:
    """一个 kind 目录的分池结果。"""

    kind: str
    moved: tuple[tuple[str, str], ...] = ()  # (文件名, 去的面)
    unclassified: tuple[str, ...] = ()  # 读不出 / 认不出的文件，原样留着

    @property
    def moved_count(self) -> int:
        return len(self.moved)


def _surface_by_key(payload: Mapping[str, Any]) -> str:
    return str(payload.get("tool_surface") or "")


def _surface_of_sample(path: Path) -> str:
    return _surface_by_key(json.loads(path.read_text(encoding="utf-8")))


def _surface_of_view(path: Path) -> str:
    return _surface_by_key(json.loads(path.read_text(encoding="utf-8")))


def _surface_of_task_view(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    meta = payload.get("meta")
    return _surface_by_key(meta) if isinstance(meta, Mapping) else ""


def _self(path: Path) -> Iterable[Path]:
    return (path,)


def _view_group(path: Path) -> Iterable[Path]:
    """P3 三份视图是一组：``<sample_id>_{messages,openai,meta}.json``。

    只认 ``*_meta.json`` 当主文件——三份里只有 meta 带 ``tool_surface``。
    """
    base = path.name[: -len("_meta.json")]
    return (path.parent / f"{base}_{name}.json" for name in VIEW_NAMES)


def _migrate(
    delivery_dir: str | Path,
    *,
    primary_glob: str,
    read_surface: Callable[[Path], str],
    companions: Callable[[Path], Iterable[Path]],
) -> PoolMigration:
    """把 ``delivery_dir`` 里非交付面的文件组移到 ``legacy/<surface>/``。

    单份读不出（坏 JSON / 缺字段）不打断整批——记进 ``unclassified``，
    留在原地等人看。**不猜它在哪个面**。
    """
    d = Path(delivery_dir)
    if not d.is_dir():
        return PoolMigration(kind=d.name)

    moved: list[tuple[str, str]] = []
    unclassified: list[str] = []
    for primary in sorted(d.glob(primary_glob)):
        try:
            surface = read_surface(primary)
        except (OSError, ValueError):
            surface = ""
        if surface not in KNOWN_SURFACES:
            unclassified.append(primary.name)
            continue
        if is_delivery(surface):
            continue
        dest = d.parent / LEGACY_DIRNAME / surface / d.name
        dest.mkdir(parents=True, exist_ok=True)
        for f in companions(primary):
            if f.exists():
                os.replace(f, dest / f.name)
        moved.append((primary.name, surface))
    return PoolMigration(kind=d.name, moved=tuple(moved), unclassified=tuple(unclassified))


def migrate_samples(delivery_dir: str | Path) -> PoolMigration:
    """``samples/`` → 非 ``camofox-1`` 的样本移进 ``legacy/<surface>/samples/``。"""
    return _migrate(
        delivery_dir,
        primary_glob="*.json",
        read_surface=_surface_of_sample,
        companions=_self,
    )


def migrate_views(delivery_dir: str | Path) -> PoolMigration:
    """``views/`` → 非 ``camofox-1`` 的**三份视图成组**移进 legacy。"""
    return _migrate(
        delivery_dir,
        primary_glob="*_meta.json",
        read_surface=_surface_of_view,
        companions=_view_group,
    )


def migrate_task_views(delivery_dir: str | Path) -> PoolMigration:
    """``task_views/`` → ``meta.tool_surface`` 非交付面的任务视图移进 legacy。"""
    return _migrate(
        delivery_dir,
        primary_glob="*.json",
        read_surface=_surface_of_task_view,
        companions=_self,
    )


# ── 池元数据（批次账）──────────────────────────────────────────────


def _count(directory: Path, pattern: str) -> int:
    return len(list(directory.glob(pattern))) if directory.is_dir() else 0


def _legacy_counts(root: Path, kind: str, pattern: str) -> dict[str, int]:
    legacy_root = root / LEGACY_DIRNAME
    if not legacy_root.is_dir():
        return {}
    out: dict[str, int] = {}
    for surface_dir in sorted(legacy_root.iterdir()):
        if not surface_dir.is_dir():
            continue
        n = _count(surface_dir / kind, pattern)
        if n:
            out[surface_dir.name] = n
    return out


def record_pool(
    delivery_dir: str | Path,
    *,
    moved: Sequence[tuple[str, str]] = (),
    unclassified: Sequence[str] = (),
    primary_glob: str = "*.json",
) -> Path:
    """把一个 kind 的池现状写进 ``pool_meta.json``（原子写）。

    每个 kind 一段：交付面还剩多少、各旧面归档了多少、本次移动了多少、
    哪些文件认不出。顶层带 ``delivery_surface`` / ``updated_at`` /
    ``note``——**池变小不是质量变好**那句话要跟数据在一起，不能只写在文档里。
    """
    d = Path(delivery_dir)
    root = d.parent
    root.mkdir(parents=True, exist_ok=True)
    path = root / POOL_META_FILENAME

    payload: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, ValueError):
            payload = {}

    payload["schema"] = "pool-meta.v1"
    payload["delivery_surface"] = DELIVERY_SURFACE
    payload["legacy_dir"] = LEGACY_DIRNAME
    payload["note"] = (
        "旧面按只读存量归档；交付面池变小不是质量变好——"
        "读分母前先看这一段（方案 23 R-5）"
    )
    payload[d.name] = {
        "delivery": _count(d, primary_glob),
        "legacy": _legacy_counts(root, d.name, primary_glob),
        "moved_now": len(moved),
        "unclassified": list(unclassified),
        "updated_at": _now_iso(),
    }
    payload["updated_at"] = _now_iso()

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "DELIVERY_SURFACE",
    "LEGACY_DIRNAME",
    "POOL_META_FILENAME",
    "KNOWN_SURFACES",
    "is_delivery",
    "pool_root",
    "PoolMigration",
    "migrate_samples",
    "migrate_views",
    "migrate_task_views",
    "record_pool",
]