"""T 视图落盘——单文件三视图合一的**唯一写实现**（与 P3 同模态）。

与 :mod:`trajectory_pipeline.assembler.task_view` 的分工：

    task_view.py    渲染语义（P1 JSON → {messages, openai, meta} dict + meta 元字段）
    task_views.py   落盘语义（dict → 磁盘单文件，原子写）

渲染与落盘的边界是 **payload dict**：task_view.py 把渲染完的 dict
交给这里；这里不关心内容是什么、不重新渲染，只负责「写进
``task_views_dir(root)``、默认不覆盖、失败进 ``skipped`` 不抛出」。

T 与 P3 的落盘差异：
- **P3 是三份文件**（``<sample_id>_{messages,openai,meta}.json``）；
- **T 是单文件**（``<trajectory_id>.json``，三视图打包）——

单文件的理由：T 是 per-run 复核视图（消费方是 LS 上传 / 人工复核 / L4
任务级判定），不会按消息视图单独读，三视图一体交付更便于落盘与传输。
P3 是 per-unit 训练视图（消费方是 SFT 训练框架），按视图拆开读更便于
训练侧按需加载。

原子写与 :mod:`assembler.builder` 同款：先 ``.json.tmp``、``os.replace``
覆盖目标。批次中断不会留下半截视图文件——半截视图比缺视图更难查。

产物隔离（D8）：路径权威在 :func:`trajectory_pipeline.storage.task_views_dir`，
只落 ``trajectory_pipeline/output/pipeline/task_views/``，禁止仓库根
``output/``。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from trajectory_pipeline.storage import task_views_dir

__all__ = ["write_task_view", "read_task_view", "TaskViewReport"]


class TaskViewReport:
    """一次 :func:`write_task_view` 的结果。"""

    __slots__ = ("trajectory_id", "written", "skipped")

    def __init__(self, trajectory_id: str, written: int, skipped: tuple[str, ...]):
        self.trajectory_id = trajectory_id
        self.written = written
        self.skipped = skipped

    @property
    def ok(self) -> bool:
        return self.skipped == ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"TaskViewReport(trajectory_id={self.trajectory_id!r}, "
            f"written={self.written}, skipped={self.skipped!r})"
        )


#: payload 必备的顶层键。三视图打包 = ``messages`` / ``openai`` / ``meta``
#: 三键（与 P3 同款视图名）。由契约测试盯住：渲染侧输出一份 dict 必须
#: 含此三键，否则落盘侧 ``skipped`` 留痕并返回。
PAYLOAD_KEYS: tuple[str, ...] = ("messages", "openai", "meta")


def write_task_view(
    payload: Mapping[str, Any],
    trajectory_id: str,
    *,
    root: str | Path | None = None,
    overwrite: bool = False,
) -> TaskViewReport:
    """把 T 视图 dict 原子写成 ``task_views_dir(root)`` 下的单文件。

    Args:
        payload: 渲染好的视图 dict——**必须含** ``PAYLOAD_KEYS`` 三键
            （``messages`` / ``openai`` / ``meta``）。缺键 = 渲染侧没给
            全三视图，落盘侧 ``skipped`` 留痕**不补默认空视图**。
            多余的键也照写进文件（同 P3 纪律）。
        trajectory_id: 视图的文件主键；文件名
            ``<trajectory_id>.json`` = ``<task_id>__<run_id>.json``。
            空 trajectory_id 连文件名都起不出来——报 ``skipped``，不写。
        overwrite: 目标已存在时是否覆盖。**默认 False**——重建批不该
            悄悄把别人刚标好的视图抹掉。批量重建时显式传 True。

    Returns:
        :class:`TaskViewReport`——写成 1 / 跳过 1（带原因）。
        **出错不抛出**（序列化失败 / 写盘失败都进 ``skipped``），让
        整批渲染不被一条样本卡住——与 ``write_p3.write_views`` 同款。
    """
    out_dir = task_views_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not trajectory_id:
        return TaskViewReport(
            trajectory_id="",
            written=0,
            skipped=("<no-trajectory_id>: 空 id 起不出文件名",),
        )

    missing_keys = [k for k in PAYLOAD_KEYS if k not in payload]
    if missing_keys:
        return TaskViewReport(
            trajectory_id=trajectory_id,
            written=0,
            skipped=(f"{trajectory_id}: 缺 payload 键 {missing_keys}",),
        )

    target = out_dir / f"{trajectory_id}.json"
    if not overwrite and target.exists():
        return TaskViewReport(
            trajectory_id=trajectory_id,
            written=0,
            skipped=(f"{trajectory_id}: 已存在",),
        )

    ok, reason = _write_one(target, payload)
    if not ok:
        assert reason is not None
        return TaskViewReport(
            trajectory_id=trajectory_id,
            written=0,
            skipped=(f"{trajectory_id} ({reason})",),
        )
    return TaskViewReport(trajectory_id=trajectory_id, written=1, skipped=())


def _write_one(target: Path, payload: Any) -> tuple[bool, str | None]:
    """原子写单个 JSON 文件（与 write_p3._write_one 同款）。"""
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        blob = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return False, f"序列化失败：{type(exc).__name__}: {exc}"
    try:
        tmp.write_text(blob, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        # 残留的 .tmp 留着无所谓——下次重建会覆盖。**不删**：删错了
        # 等于把唯一能恢复的中间产物一并擦掉（与 write_p3 同款）。
        return False, f"写盘失败：{type(exc).__name__}: {exc}"
    return True, None


def read_task_view(
    trajectory_id: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any] | None:
    """读回一份 T 视图，返回完整 dict（含 ``messages`` / ``openai`` / ``meta``）。

    Args:
        trajectory_id: 视图的文件主键。
        root: 读取根；``None`` 取 :func:`task_views_dir` 的默认根。

    Returns:
        视图 JSON 解析后的 dict；文件缺失 / 坏档返回 ``None``——fail-closed：
        能力不可用（缺文件 / 坏档）时返回 ``None``，不返回半份数据、不猜。
        调用方拿 ``None`` 当「这条读不到」处理，而不是当成一份假视图。
        与 :func:`trajectory_pipeline.storage.read_p3.read_views` 同款纪律。
    """
    if not trajectory_id:
        return None
    path = task_views_dir(root) / f"{trajectory_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None