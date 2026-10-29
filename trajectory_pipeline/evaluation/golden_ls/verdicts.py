"""golden_ls.verdicts: 把 LS 上的人工裁定**导出来**（方案 22 §4.4）。

两套署名分列（裁定④）
--------------------
===========================  ==================================================
``annotator``               **LS 侧复核人**（谁在标注页上做了这次裁定）
``gold.reviewed_by``        **金标侧署名**（谁当初定了这个标签）
===========================  ==================================================

**这两套不是一回事，不能合并成一栏。** ``manual-review`` / ``reesn`` 是
金标侧的署名（owner 当初把标签定成那样）；``annotator`` 是复核侧的署名
（谁在看的时候确认/否掉了它）。合并 ⇒ 「owner 复核过」与「owner 当初定的
金标」分不清，而这两件事在归因上完全相反。

⚠️ **本模块不写金标**。导出的裁定落 ``output/``（可复核的记录），
**不回流** ``annotations.jsonl``——回流要走 ``golden.builder annotate``
整行模式（要 ``access_surface`` + ``observed_at`` + 署名），且「金标变动 =
新基线」⇒ 要重冻结出新版本（方案 22 §0.3 / R9）。

字段缺失一律**显式标注**（「不可用」/「未知」），**不给空串充数**——空串
会被读成「没复核过」，而真相是「导出里没这一项」。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Sequence

from trajectory_pipeline.evaluation.golden_ls.label_config import (
    HUMAN_BRANCH_CONTROL,
    NOTE_CONTROL,
    REASON_CONTROL,
    VERDICT_CONTROL,
)

log = logging.getLogger(__name__)

#: 记「读不到」而不是空串。
UNKNOWN = "（不可用）"

#: LS 侧裁定结果的四个控件名（与 label_config 同源）。
RESULT_CONTROLS: tuple[str, ...] = (
    VERDICT_CONTROL, HUMAN_BRANCH_CONTROL, REASON_CONTROL, NOTE_CONTROL,
)


def _result_values(annotation: Mapping[str, Any]) -> dict[str, str]:
    """一条 annotation 的 ``result`` → ``{控件名: 值}``。

    LS 的 result 是 region 列表：``[{from_name, to_name, type, value}]``。
    文本控件的 ``value`` 是 ``{"text": "..."}``（或 ``{"text": ["...", ...]}``）；
    选项控件的 ``value`` 是 ``{"choices": ["agree"]}``。两种都取。
    """
    out: dict[str, str] = {}
    result = annotation.get("result")
    if not isinstance(result, Sequence):
        return out
    for item in result:
        if not isinstance(item, Mapping):
            continue
        from_name = str(item.get("from_name") or "")
        if from_name not in RESULT_CONTROLS:
            continue
        value = item.get("value")
        text = ""
        if isinstance(value, Mapping):
            if "choices" in value:
                choices = value.get("choices")
                if isinstance(choices, Sequence) and not isinstance(choices, str):
                    text = " / ".join(str(c) for c in choices)
                else:
                    text = str(choices or "")
            else:
                raw = value.get("text")
                if isinstance(raw, Sequence) and not isinstance(raw, str):
                    text = "\n".join(str(x) for x in raw)
                else:
                    text = str(raw or "")
        elif isinstance(value, str):
            text = value
        if from_name in out and out[from_name]:
            # 同控件多次出现（LS 允许多 region）⇒ 拼起来，不覆盖。
            out[from_name] = f"{out[from_name]}\n{text}".strip()
        else:
            out[from_name] = text
    return out


def _annotator_identity(annotation: Mapping[str, Any]) -> tuple[str, str]:
    """``(annotator, annotator_kind)``。

    ⚠️⚠️ **LS 1.23.0 实测打翻了初稿的字段名**：标注对象里**没有**
    ``created_by``——复核人身份在 ``created_username``（实测形态：
    ``" reesn@qq.com, 1"``，即「账号, 用户 id」）与 ``completed_by``
    （用户 id）里。

    初稿只读 ``created_by`` ⇒ **署名列恒为空**⇒ 裁定④（LS 端 annotator
    身份单列一栏）**形同虚设**，而且**不报错**——导出照样出 5 行，只是
    「按复核人」那栏全是「（不可用）」。

    ⇒ 依次试：``created_by``（dict / str / int）→ ``created_username``
    （取逗号前那段）→ ``completed_by`` / ``updated_by``（数字 id）。
    取不到就 ``(UNKNOWN, "unknown")``——**不猜**。

    ⚠️ ``human`` 的判据只是「**取到了账号**」。它判不了「这个账号是不是
    机器」——那是 LS 端账号的性质，不是导出里能读出来的。
    """
    created_by = annotation.get("created_by")
    email = ""
    if isinstance(created_by, Mapping):
        email = str(created_by.get("email") or "")
        if not email:
            first = str(created_by.get("first_name") or "")
            last = str(created_by.get("last_name") or "")
            full = f"{first} {last}".strip()
            if full:
                email = full
    elif isinstance(created_by, str) and created_by.strip():
        email = created_by.strip()
    elif isinstance(created_by, int):
        email = str(created_by)

    if not email:
        # ⚠️ LS 1.23 实测形态：" reesn@qq.com, 1" ⇒ 取逗号前那段。
        username = str(annotation.get("created_username") or "").strip()
        if username:
            email = username.split(",")[0].strip()

    if not email:
        for key in ("completed_by", "updated_by"):
            value = annotation.get(key)
            if isinstance(value, int):
                email = str(value)
                break
            if isinstance(value, str) and value.strip():
                email = value.strip()
                break

    if not email:
        return UNKNOWN, "unknown"
    return email, "human"


def _pick_latest(annotations: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """同一条 task 的多条标注里取**最后一条**（按 ``updated_at``，退回
    ``created_at``）。

    ⚠️ 一个 task 可能有**多条**标注（重复提交 / 改过稿）。取最后一条是因为
    「改过的稿子才是当前的稿子」；但**条数要记进导出**（``annotation_count``）
    ——只留最后一条会让人以为这条只被复核过一次。
    """
    if not annotations:
        return None
    def stamp(item: Mapping[str, Any]) -> str:
        return str(item.get("updated_at") or item.get("created_at") or "")
    return max(annotations, key=stamp)


def parse_export(
    payload: Mapping[str, Any],
    *,
    gold_rows: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """LS 项目导出 → **每条 task 一行**的裁定记录。

    Args:
        payload: ``client.export_project_json`` 的返回体
            （``{"tasks": [...], "annotations": [...]}``）。
        gold_rows: ``golden_id → 冻结行``。给了就把**金标侧署名**
            （``reviewed_by`` / ``reviewed_at``）一并带出来——那是另一套
            署名，与 ``annotator`` **分列**。

    没有任何标注的 task **也要出一行**（``verdict = UNKNOWN``）——
    「没复核」和「没导出来」是不同的事，而漏掉没复核的那些会让「覆盖率」
    读成 100%。
    """
    tasks = payload.get("tasks")
    tasks = tasks if isinstance(tasks, Sequence) else []
    raw_annotations = payload.get("annotations")
    raw_annotations = raw_annotations if isinstance(raw_annotations, Sequence) else []

    by_task: dict[Any, list[Mapping[str, Any]]] = {}
    for item in raw_annotations:
        if not isinstance(item, Mapping):
            continue
        by_task.setdefault(item.get("task"), []).append(item)

    # ⚠️ **标注也可能只挂在 task 自己的 `annotations` 里**（LS 的 task 端点
    # 就是这个形态）。顶层没给时，从每个 task 里再捞一遍——两处都收，
    # 按 annotation id 去重（同一份标注在两处都出现时只算一次）。
    seen_ids: set[Any] = {
        item.get("id")
        for items in by_task.values()
        for item in items
        if item.get("id") is not None
    }
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        nested = task.get("annotations")
        if not isinstance(nested, Sequence):
            continue
        for item in nested:
            if not isinstance(item, Mapping):
                continue
            ident = item.get("id")
            if ident is not None:
                if ident in seen_ids:
                    continue
                seen_ids.add(ident)
            by_task.setdefault(item.get("task") or task.get("id"), []).append(item)

    out: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        data = task.get("data") if isinstance(task.get("data"), Mapping) else {}
        golden_id = str(data.get("golden_id") or "")
        task_id = task.get("id")
        candidates = by_task.get(task_id, [])
        latest = _pick_latest(candidates)

        values = _result_values(latest) if latest else {}
        annotator, annotator_kind = (
            _annotator_identity(latest) if latest else (UNKNOWN, "unknown")
        )
        record: dict[str, Any] = {
            "golden_id": golden_id,
            "ls_task_id": task_id,
            "split": str(data.get("split") or ""),
            "verdict": values.get(VERDICT_CONTROL) or UNKNOWN,
            "human_branch": values.get(HUMAN_BRANCH_CONTROL) or UNKNOWN,
            "reason": values.get(REASON_CONTROL) or UNKNOWN,
            "note": values.get(NOTE_CONTROL) or UNKNOWN,
            "annotator": annotator,
            "annotator_kind": annotator_kind,
            "annotated_at": (
                str((latest or {}).get("updated_at") or (latest or {}).get("created_at") or "")
                if latest else UNKNOWN
            ),
            "annotation_count": len(candidates),
        }
        if gold_rows is not None:
            row = gold_rows.get(golden_id)
            golden = (row or {}).get("golden") if row else None
            golden = golden if isinstance(golden, Mapping) else {}
            record["gold"] = {
                "reviewed_by": str(golden.get("reviewed_by") or UNKNOWN),
                "reviewed_at": str(golden.get("reviewed_at") or UNKNOWN),
                "label": str(golden.get("label") or ""),
            }
        out.append(record)
    return out


def summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """导出记录的计数视图（按档 / 按裁决 / 按复核人）。"""
    by_split: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    by_annotator: dict[str, int] = {}
    reviewed = 0
    for record in records:
        split = str(record.get("split") or "?")
        by_split[split] = by_split.get(split, 0) + 1
        verdict = str(record.get("verdict") or "?")
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        annotator = str(record.get("annotator") or "?")
        by_annotator[annotator] = by_annotator.get(annotator, 0) + 1
        if verdict != UNKNOWN:
            reviewed += 1
    total = len(records)
    return {
        "total": total,
        "reviewed": reviewed,
        "unreviewed": total - reviewed,
        "by_split": dict(sorted(by_split.items())),
        "by_verdict": dict(sorted(by_verdict.items())),
        "by_annotator": dict(sorted(by_annotator.items())),
    }


def dump_jsonl(records: Sequence[Mapping[str, Any]]) -> str:
    """记录 → JSONL 文本（**一行一条**）。"""
    return "".join(
        json.dumps(dict(r), ensure_ascii=False) + "\n" for r in records
    )


__all__ = [
    "RESULT_CONTROLS",
    "UNKNOWN",
    "dump_jsonl",
    "parse_export",
    "summarize",
]
