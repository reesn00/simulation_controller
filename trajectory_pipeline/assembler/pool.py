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
    output/pipeline/quarantine/{samples,views,task_views}/…       # 名单之外的陈旧产物
    output/pipeline/pool_meta.json                                # 批次元数据

本模块**不 import** :mod:`builder` / :mod:`views` / :mod:`task_view`——那三个
是使用方，反向 import 会成环。它只认「某个 kind 目录」（``delivery_dir``）
与 surface 字段，布局字面量只有 ``legacy``、``quarantine`` 与
``pool_meta.json`` 三处。
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


# ── 按名单清点（陈旧产物隔离）──────────────────────────────────────────

#: 隔离目录名（落在 ``output/pipeline/`` 下）。
#:
#: ⚠️ **与 :data:`LEGACY_DIRNAME` 是两个正交的口径，不是同一个池**：
#:
#: - ``legacy/`` 按**面**分——旧面是**只读存量**（owner 裁定 2026-10-10），
#:   永不重渲染，归档的是「另一种格式的交付物」；
#: - ``quarantine/`` 按**名单**分——这里装的是「**本轮名单之外**」的产物，
#:   归档的是「**不该在交付集里**的东西」（H-33）。
#:
#: ⚠️ **隔离 ≠ 删除**：派生产物可从 P1 重渲染 ⇒ 隔离**必须可逆**
#: （搬回原位即可）。P1 存档原样在盘上——**那是证据，不动**。
QUARANTINE_DIRNAME = "quarantine"


@dataclass(frozen=True, slots=True)
class StaleSweep:
    """一次「按本轮名单清点交付目录」的结果。

    与 :class:`PoolMigration` 的区别：那是**按面**分（旧面 → ``legacy/``），
    这里是**按名单**清（陈旧产物 → ``quarantine/``）—— 两套机制、两个目录。
    """

    #: 被清点的 kind 目录名（``samples`` / ``views`` / ``task_views``）。
    kind: str
    #: **名单内、留在原地的**份数。
    kept: int = 0
    #: **留在原地的文件数**（≠ :attr:`kept` 时说明「份」是 id 口径）。
    kept_files: int = 0
    #: **被移走的文件数**（≠ :attr:`count` 时同上）。
    #: ⚠️ ``views/`` 一个 id 是 **3 个文件** ⇒ id 口径与文件口径**不是一回事**。
    moved_files: int = 0
    #: **不在名单、被移进隔离区**的 id（按 id 排序）。
    #: ⚠️ 记 **id** 不记文件名——三个 kind 的 id 口径一致
    #: （``sample_id`` / ``<sample_id>`` / ``<task_id>__<run_id>``），
    #: 印出来的人才知道「是哪几条」。
    moved: tuple[str, ...] = ()
    #: **读不出面**的文件 id：**原样留在原地**等人看，**不隔离**
    #: （与 :func:`_migrate` 的 ``unclassified`` 同一处置——
    #: 猜它在哪个池就是让文件去错地方，而错池不会报错）。
    unclassified: tuple[str, ...] = ()
    #: **没清点的原因**（空串 = 真的清点了）。
    #:
    #: ⚠️ 「没清点」与「清点了、没发现残留」在报表上**长得一模一样**
    #: ⇒ 必须显式带上原因，否则读者会以为「清过了，干净」。
    reason: str = ""

    @property
    def count(self) -> int:
        return len(self.moved)

    def summary(self) -> str:
        if self.reason:
            return f"{self.kind}: **未清点**（{self.reason}）"
        return f"{self.kind}: 留 {self.kept} 份 · 移入隔离区 {self.count} 份"


#: 旧名——本条机制的第一版叫「隔离结果」，改名后留个别名，
#: 免得仓里别处引用旧名直接断掉。
QuarantineResult = StaleSweep


def _id_of_sample(path: Path) -> str:
    """P2 样本的 id = 文件名主键（``<sample_id>.json``）。"""
    return path.stem


def _id_of_view(path: Path) -> str:
    """P3 视图的 id = ``<sample_id>``（三份视图**共一个** id）。

    ``<sample_id>_{messages,openai,meta}.json`` ⇒ ``<sample_id>``。
    不带视图后缀的杂项文件按自己的文件名主键算 id。
    """
    name = path.name
    for view in VIEW_NAMES:
        suffix = f"_{view}.json"
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _id_of_task_view(path: Path) -> str:
    """T 视图的 id = ``<task_id>__<run_id>``（文件名主键）。"""
    return path.stem


def _quarantine_offroster(
    delivery_dir: str | Path,
    *,
    pattern: str,
    id_of: Callable[[Path], str],
    roster: Iterable[str],
    skip_names: Iterable[str] = (),
) -> StaleSweep:
    """把**本轮名单之外**的文件移进隔离区。

    ⚠️ **判据是「名单」，不是「面」**——:func:`_migrate` 那套只认「工具面 ≠
    交付面」（旧面存量），**认不出「本轮没产出」**（H-33 的根因）。
    两者正交：旧面按面分池，陈旧产物按名单清点。

    ⚠️ **名单 = 「本轮该产出」的 id 集合**，**不是**「本轮实际写出的」——
    默认不覆盖（``overwrite=False``）时，已存在的文件是**跳过**而不是
    **没产出**；拿「写出的」当名单会把整池都判成残留。

    Args:
        delivery_dir: 交付目录（``samples/`` / ``views/`` / ``task_views/``）。
        pattern: 收哪些文件。
        id_of: 从文件路径取 id。
        roster: 本轮该产出的 id 集合。
        skip_names: **不隔离**的文件名（读不出面的那些——原样留给人看）。

    Returns:
        :class:`StaleSweep`——留下的份数 + 被移走的 id 清单 + 读不出面的 id。
    """
    d = Path(delivery_dir)
    if not d.is_dir():
        return StaleSweep(kind=d.name, reason="交付目录不存在 ⇒ 无从清点")
    keep = set(roster)
    skip_ids = {id_of(Path(n)) for n in skip_names}

    groups: dict[str, list[Path]] = {}
    for f in sorted(d.glob(pattern)):
        if not f.is_file():
            continue
        groups.setdefault(id_of(f), []).append(f)

    dest = d.parent / QUARANTINE_DIRNAME / d.name
    moved: list[str] = []
    kept = 0
    kept_files = 0
    moved_files = 0
    for gid in sorted(groups):
        if gid in keep:
            kept += 1
            kept_files += len(groups[gid])
            continue
        if gid in skip_ids:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        for f in groups[gid]:
            os.replace(f, dest / f.name)
        moved_files += len(groups[gid])
        moved.append(gid)
    return StaleSweep(
        kind=d.name,
        kept=kept,
        kept_files=kept_files,
        moved=tuple(moved),
        moved_files=moved_files,
        unclassified=tuple(sorted(skip_ids)),
    )


def quarantine_offroster_samples(
    delivery_dir: str | Path,
    *,
    roster: Iterable[str],
    skip_names: Iterable[str] = (),
) -> QuarantineResult:
    """``samples/`` → **本轮名单之外**的样本移进 ``quarantine/samples/``。"""
    return _quarantine_offroster(
        delivery_dir,
        pattern="*.json",
        id_of=_id_of_sample,
        roster=roster,
        skip_names=skip_names,
    )


def quarantine_offroster_views(
    delivery_dir: str | Path,
    *,
    roster: Iterable[str],
    skip_names: Iterable[str] = (),
) -> QuarantineResult:
    """``views/`` → 名单之外的**三份视图成组**移进 ``quarantine/views/``。"""
    return _quarantine_offroster(
        delivery_dir,
        pattern="*.json",
        id_of=_id_of_view,
        roster=roster,
        skip_names=skip_names,
    )


def quarantine_offroster_task_views(
    delivery_dir: str | Path,
    *,
    roster: Iterable[str],
    skip_names: Iterable[str] = (),
) -> QuarantineResult:
    """``task_views/`` → 名单之外的任务视图移进 ``quarantine/task_views/``。"""
    return _quarantine_offroster(
        delivery_dir,
        pattern="*.json",
        id_of=_id_of_task_view,
        roster=roster,
        skip_names=skip_names,
    )


def quarantine_dir(root: str | Path, kind: str) -> Path:
    """某个 kind 的隔离目录（**只是路径，不建目录**）。"""
    return Path(root) / QUARANTINE_DIRNAME / kind


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
    quarantined: Sequence[str] = (),
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
        quarantined: 本次**因「不在本轮名单」而移进隔离区**的 id。
            ⚠️ **新开一个顶层字段**（``quarantine``），**不改旧字段的含义**
            ——旧字段记的是「面」的计数，加一维就新开字段，改旧字段会漂读数。
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
    # ⚠️ **「按名单」这一维新开顶层字段**（不塞进上面的 kind 段）：
    # 上面那段记的是**面**的计数，语义是「交付面还剩多少」；
    # 这里记的是**本轮名单之外**被移走的 id——两件事的**分母不一样**
    #（一个是目录里的文件数，一个是本轮该产出的 id 数）。
    # 混进旧字段会漂读数（§六·6）。
    quarantine = payload.setdefault("quarantine", {})
    if not isinstance(quarantine, dict):  # 旧文件写坏了 ⇒ 顶掉，不让它带着跑
        quarantine = {}
        payload["quarantine"] = quarantine
    quarantine.update({
        "dir": QUARANTINE_DIRNAME,
        "note": (
            "「本轮名单之外」的陈旧派生产物移到这里——**不是删除**："
            "可从 P1 重渲染，也可直接搬回原位（隔离可逆）"
        ),
        "updated_at": _now_iso(),
    })
    quarantine[d.name] = {
        "count": len(quarantined),
        "ids": list(quarantined),
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
    "migrate_samples",
    "migrate_views",
    "migrate_task_views",
    "QuarantineResult",
    "quarantine_offroster_samples",
    "quarantine_offroster_views",
    "quarantine_offroster_task_views",
    "quarantine_dir",
    "record_pool",
]