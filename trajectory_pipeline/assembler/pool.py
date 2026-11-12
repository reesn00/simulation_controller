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
- **只读存量** = 新批次不再往里写。**默认连重建都不渲染**
  （owner 裁定 **2026-11-10**）：``cli build`` / ``cli task-view`` 只构
  交付面，旧面 P2 / P3 / T **不重渲染**。

  ⚠️ **「退役 ≠ 失忆」指的是「存量证据仍读得出来」**（P1 源档在盘上、
  池里的旧文件仍在、``pool_meta.json`` 记着它曾经有多少），**不是**
  「每次重建都把旧面再渲染一遍」。后者会让清过的池自己长回来——
  清理不持久，而读的人只看到「又多了 66 份」，看不到为什么。

  真要重建旧面（回溯某个旧批次）：``--include-legacy``，**按需**、显式。

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
from typing import Any, Callable, Container, Iterable, Mapping, Sequence

from trajectory_pipeline import toolcatalog
from trajectory_pipeline.storage import VIEW_NAMES

#: 交付面。**只此一个**——owner 裁定 O-3：「交付集只含 ``camofox-1``」。
DELIVERY_SURFACE = toolcatalog.SURFACE_CAMOFOX

#: 旧面归档目录名（落在 ``output/pipeline/`` 下）。
LEGACY_DIRNAME = "legacy"

#: 隔离区目录名（落在 ``output/pipeline/`` 下）。
#:
#: ⚠️ **与 :data:`LEGACY_DIRNAME` 的区别**（两者都是**移动不是删除**）：
#:
#: - ``legacy/<surface>/<kind>/`` 装的是**旧面存量**（只读、按需重建）——
#:   那是「**另一种口径**的合法产物」。
#: - ``quarantine/<kind>/`` 装的是**本轮名单之外**的产物——
#:   那是「**这一轮不该存在**的孤儿」（被判分不可信剔除的档、
#:   被 ``--rationale-era`` 筛掉的旧口径样本、切不出样本的退化空壳留下的 T 视图）。
#:
#: 为什么要单独一个目录：两者的**处置不同**——旧面是「口径退役，要能回溯」，
#: 孤儿是「**这一轮剔掉的**」，混在一起就分不清「回溯旧批次」和「清理孤儿」。
#:
#: ⚠️ **可逆**：移过去的文件**原样还在** ⇒ 要恢复就搬回来（可逆是这里的硬要求，
#: 因为清点的判据一旦写错，误伤的是**已交付的产物**）。
QUARANTINE_DIRNAME = "quarantine"

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


#: 旧面跳过时用的**稳定标记**。CLI 按它把「旧面未渲染」从「已存在／写失败」
#: 那组里摘出来——两者的处置完全不同（后者加 ``--overwrite`` 重来，
#: 前者加 ``--include-legacy`` 重建旧面），混在一组里印就等于没印。
LEGACY_SKIP_MARK = "旧面只读存量"

#: 旧面跳过的原因全文（印在 stdout / 记进 ``skipped``）。
LEGACY_SKIP_REASON = (
    f"{LEGACY_SKIP_MARK}：默认不重渲染（按需重建加 --include-legacy）"
)


def should_render(surface: str, *, include_legacy: bool = False) -> bool:
    """这个面**要不要被重渲染**落盘？

    交付面永远要。旧面**默认不要**（owner 裁定 2026-11-10，见模块
    docstring）：旧面是只读存量，重建它是**按需**动作（``--include-legacy``），
    不是默认路径。

    ⚠️ **这与「迁移」是两件事**：已经躺在交付目录里的旧面文件仍要**挪**进
    legacy（:func:`migrate_samples` 等）——那是「并池是写入时的动作」；
    本函数管的是**要不要新渲染**。前者不挪就混池，后者渲染就长回旧面。

    ⚠️ **认不出的面直接抛**（同 :func:`pool_root`）：静默把它当成「旧面」
    跳掉，就是把「配错了」伪装成「按口径没渲染」——两者处置完全不同，
    而报表上只表现为「少了几条」。
    """
    if surface not in KNOWN_SURFACES:
        raise ValueError(
            f"认不出的工具面 {surface!r}（已知：{sorted(KNOWN_SURFACES)}）"
            "——不猜、不静默当旧面"
        )
    return bool(include_legacy) or is_delivery(surface)


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


# ── 按本轮名单清点（2026-11-12 · H-33）────────────────────────────


@dataclass(frozen=True)
class StaleSweep:
    """一次「按名单清点」的结果。

    与 :class:`PoolMigration` 的区别：那是**按面**分（旧面 → ``legacy/``），
    这里是**按名单**清（孤儿 → ``quarantine/``）—— 两套机制、两个目录。
    """

    #: 被清点的 kind 目录名（``samples`` / ``views`` / ``task_views``）。
    kind: str
    #: **本轮名单内、留在原地的**份数。
    kept: int = 0
    #: **不在名单、被移进隔离区**的文件名（按文件名排序）。
    moved: tuple[str, ...] = ()
    #: **认不出的文件**（读不出面 / 面不在已知集合里）——**留在原地**，
    #: 不搬走（同迁移纪律：猜不出来的东西留在原地等人看，搬走 = 藏起来）。
    unclassified: tuple[str, ...] = ()
    #: **没清点的原因**（空串 = 真的清点了）。
    #:
    #: ⚠️ 「没清点」与「清点了、没发现残留」在报表上**长得一模一样**
    #: ⇒ 必须显式带上原因，否则读者会以为「清过了，干净」。
    reason: str = ""

    def summary(self) -> str:
        if self.reason:
            return f"{self.kind}: **未清点**（{self.reason}）"
        tail = f" · 认不出 {len(self.unclassified)} 份（留在原地）" \
            if self.unclassified else ""
        return (
            f"{self.kind}: 留 {self.kept} 份 · 移入隔离区 {len(self.moved)} 份"
            f"{tail}"
        )


def migrate_by_manifest(
    delivery_dir: str | Path,
    *,
    primary_glob: str,
    companions: Callable[[Path], Iterable[Path]],
    keep_ids: Container[str],
    read_surface: Callable[[Path], str],
) -> StaleSweep:
    """把 ``delivery_dir`` 里**不在本轮名单**的文件组移进 ``quarantine/<kind>/``。

    ⚠️ **判据是「本轮名单」，不是「本轮实际写出了什么」**——
    默认**不覆盖**模式下实际写出 0 条（全都「已存在」跳过了），
    拿它当判据会把**整个交付集搬进隔离区**。
    ⇒ ``keep_ids`` 必须是「**这一轮应该产出什么**」。

    ⚠️ **移走不是删除**：文件原样还在 ``quarantine/<kind>/`` ⇒ 可逆。
    （清点的判据一旦写错，误伤的是**已交付的产物** ⇒ 可逆是硬要求。）

    ⚠️ **认不出的文件留在原地**（同迁移纪律）：面读不出、或者面不在
    :data:`KNOWN_SURFACES` 里 ⇒ **不搬**、记进 ``unclassified`` 等人看。
    搬走 = 藏起来——「猜不出来的东西」不该由清点替人做决定。

    ⚠️ **只在名单已知时做**：``keep_ids`` 为空**且**目录非空 ⇒ **不清点**
    （见调用方的保护：``samples/`` 不存在时不得清点 T 视图——那会把整批 T 搬空）。
    """
    d = Path(delivery_dir)
    if not d.is_dir():
        return StaleSweep(kind=d.name)
    keep = set(keep_ids)
    moved: list[str] = []
    unclassified: list[str] = []
    kept = 0
    dest = d.parent / QUARANTINE_DIRNAME / d.name
    for primary in sorted(d.glob(primary_glob)):
        try:
            surface = read_surface(primary)
        except (OSError, ValueError):
            surface = ""
        if surface not in KNOWN_SURFACES:
            # 认不出 ⇒ 留在原地等人看（同迁移纪律，不猜）。
            unclassified.append(primary.name)
            continue
        stem = primary.stem
        # 视图的主文件是 ``<id>_meta.json`` ⇒ 名单键是 ``<id>``。
        base = stem[:-len("_meta")] if primary_glob.endswith("_meta.json") else stem
        if base in keep:
            kept += 1
            continue
        dest.mkdir(parents=True, exist_ok=True)
        for f in companions(primary):
            if f.exists():
                os.replace(f, dest / f.name)
        moved.append(primary.name)
    return StaleSweep(
        kind=d.name, kept=kept, moved=tuple(moved), unclassified=tuple(unclassified)
    )


def sweep_samples(delivery_dir: str | Path, keep_ids: Container[str]) -> StaleSweep:
    """``samples/`` → 不在本轮名单的样本移进 ``quarantine/samples/``。"""
    return migrate_by_manifest(
        delivery_dir,
        primary_glob="*.json",
        companions=_self,
        keep_ids=keep_ids,
        read_surface=_surface_of_sample,
    )


def sweep_views(delivery_dir: str | Path, keep_ids: Container[str]) -> StaleSweep:
    """``views/`` → 不在本轮名单的**三份视图成组**移进 ``quarantine/views/``。"""
    return migrate_by_manifest(
        delivery_dir,
        primary_glob="*_meta.json",
        companions=_view_group,
        keep_ids=keep_ids,
        read_surface=_surface_of_view,
    )


def sweep_task_views(delivery_dir: str | Path, keep_ids: Container[str]) -> StaleSweep:
    """``task_views/`` → 不在本轮名单的任务视图移进 ``quarantine/task_views/``。"""
    return migrate_by_manifest(
        delivery_dir,
        primary_glob="*.json",
        companions=_self,
        keep_ids=keep_ids,
        read_surface=_surface_of_task_view,
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
    legacy_skipped: int = 0,
) -> Path:
    """把一个 kind 的池现状写进 ``pool_meta.json``（原子写）。

    每个 kind 一段：交付面还剩多少、各旧面归档了多少、本次移动了多少、
    哪些文件认不出。顶层带 ``delivery_surface`` / ``updated_at`` /
    ``note``——**池变小不是质量变好**那句话要跟数据在一起，不能只写在文档里。

    Args:
        legacy_skipped: 本次**因为旧面默认不重渲染而没渲染**的条数。
            记进去是因为「池里 legacy 为空」有两种成因（**没数据** vs
            **有数据但按口径不渲染**），而两者在报表上长得一模一样。
            不记这条，下一个读 ``pool_meta.json`` 的人会把口径当事故。
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
        "旧面按只读存量归档，**默认不重渲染**（按需重建加 --include-legacy）；"
        "交付面池变小不是质量变好——"
        "读分母前先看这一段（方案 23 R-5）"
    )
    payload[d.name] = {
        "delivery": _count(d, primary_glob),
        "legacy": _legacy_counts(root, d.name, primary_glob),
        "moved_now": len(moved),
        "legacy_skipped_now": legacy_skipped,
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
    "QUARANTINE_DIRNAME",
    "POOL_META_FILENAME",
    "KNOWN_SURFACES",
    "is_delivery",
    "pool_root",
    "PoolMigration",
    "StaleSweep",
    "migrate_samples",
    "migrate_views",
    "migrate_task_views",
    "migrate_by_manifest",
    "sweep_samples",
    "sweep_views",
    "sweep_task_views",
    "record_pool",
]