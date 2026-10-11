"""P3 视图落盘——三份视图 JSON 的**唯一写实现**。

与 :mod:`trajectory_pipeline.assembler.views` 的分工：
    views.py     渲染语义（P2 JSON → 三份 dict）
    write_p3.py  落盘语义（三份 dict → 磁盘文件，原子写）

渲染与落盘的边界是 **payload dict**：views.py 把 ``{messages, openai, meta}``
渲染完交给这里；这里不关心内容是什么、不重新渲染，只负责「写进
``views_dir``、默认不覆盖、失败进 ``skipped`` 不抛出」。这样只有一处
在写 P3——改存储布局（文件名、原子写方式、隔离规则）不动渲染，反之亦然。

原子写与 ``assembler/builder.py`` 同款：先 ``.json.tmp``、``os.replace``
覆盖目标。批次中断不会留下半截视图文件——半截视图比缺视图更难查。

产物隔离（D8）：路径权威在 :func:`trajectory_pipeline.storage.views_dir`，
只落 ``trajectory_pipeline/output/pipeline/views/``，禁止仓库根 ``output/``。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from trajectory_pipeline.storage import VIEW_NAMES, views_dir

__all__ = ["write_views", "WriteReport"]


class WriteReport:
    """一次 :func:`write_views` 的结果。不 import dataclass 也可（用
    frozen 简单类），但与其他模块的报告形状保持一致更省心。
    """

    __slots__ = ("sample_id", "written", "skipped")

    def __init__(self, sample_id: str, written: int, skipped: tuple[str, ...]):
        self.sample_id = sample_id
        self.written = written
        self.skipped = skipped

    @property
    def ok(self) -> bool:
        return self.skipped == ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"WriteReport(sample_id={self.sample_id!r}, "
            f"written={self.written}, skipped={self.skipped!r})"
        )


def write_views(
    payloads: Mapping[str, Any],
    sample_id: str,
    *,
    root: str | Path | None = None,
    overwrite: bool = False,
) -> WriteReport:
    """把三份视图 dict 原子写成 ``views_dir(root)`` 下的三个文件。

    Args:
        payloads: 渲染好的视图 dict——键是 ``VIEW_NAMES``（``messages`` /
            ``openai`` / ``meta``）。**多余的键也照写**，但不为缺键补空
            （缺键 = 渲染侧没给出，这里不猜）。
        sample_id: 视图的文件主键；三份文件名 ``<sample_id>_<view>.json``。
            空 id 连文件名都起不出来——报 ``skipped``，不写 ``.json``。
        root: 输出根；``None`` 取 :func:`views_dir` 的默认根
            （``trajectory_pipeline/output/pipeline/``）。
        overwrite: 目标已存在时是否覆盖。**默认 False**——重建批不该
            悄悄把别人刚标好的视图抹掉。批量重建时显式传 True。

    Returns:
        :class:`WriteReport`——写成几份、跳过几份（带原因）。
        **出错不抛出**（序列化失败 / 写盘失败都进 ``skipped``），让整批
        渲染不被一条样本卡住——与 ``builder.build_from_archive`` 同款。
    """
    out_dir = views_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not sample_id:
        return WriteReport(
            sample_id="", written=0, skipped=("<no-sample_id>: 空 id 起不出文件名",)
        )

    written = 0
    skipped: list[str] = []
    for name in VIEW_NAMES:
        target = out_dir / f"{sample_id}_{name}.json"
        if not overwrite and target.exists():
            skipped.append(f"{sample_id}_{name}")
            continue
        ok, reason = _write_one(target, payloads.get(name))
        if not ok:
            assert reason is not None
            skipped.append(f"{sample_id}_{name} ({reason})")
            continue
        written += 1
    return WriteReport(sample_id=sample_id, written=written, skipped=tuple(skipped))


def _write_one(target: Path, payload: Any) -> tuple[bool, str | None]:
    """原子写单个 JSON 文件。

    Returns:
        ``(ok, skip_reason)``——写成功 ``(True, None)``；
        失败 ``(False, reason)``，**不抛异常**，让整批不被一条卡住。
    """
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
        # 等于把唯一能恢复的中间产物一并擦掉。
        return False, f"写盘失败：{type(exc).__name__}: {exc}"
    return True, None
