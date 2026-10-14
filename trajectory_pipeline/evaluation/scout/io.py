"""scout 的 IO 层。

职责:
- 读 P2 样本 → 还原 ``Sample`` 对象(借 :func:`assembler.schema.split_archive`)
- 跑四维评分,产出 :class:`ScoutRecord`
- 三件套 JSONL 原子写(``positive`` / ``negative`` / ``quarantine``)
- 可选 ``--mediabrowse-dir`` 的 L5 证据附加

⚠️ **scout 不反向 import executor**(不变量 2);借 P1 存档也只是为了
拿到 ``Sample`` 对象,scout 自身不读 executor 的运行时状态。
"""

from __future__ import annotations

import collections
import dataclasses
import json
import pathlib
from typing import Any, Iterable, Mapping

from trajectory_pipeline.assembler.schema import Sample, split_archive
from trajectory_pipeline.evaluation.scorers.code_scorer import score_d2
from trajectory_pipeline.evaluation.scorers.d4_scorer import score_d4
from trajectory_pipeline.evaluation.scorers.d5_scorer import score_d5
from trajectory_pipeline.evaluation.scorers.d6_scorer import score_d6

#: 与 :data:`evaluation.cli.SCORERS` 同名短码 → 打分函数。
#: **本模块自己维护一份**,因为 evaluation.cli 走的是
#: ``score_d2(sample: Sample)`` 形态,而 scout 调用时仍要按对象喂;
#: 引入 evaluation.cli 只会把 ``reconfigure(encoding=...)`` 这种 UI 关注
#: 拖进来,违反「scout 是纯计算模块」的设计。
_SCORERS = {
    "d2": score_d2,
    "d4": score_d4,
    "d5": score_d5,
    "d6": score_d6,
}


# ── 读 P2 ──────────────────────────────────────────────────────────


def _load_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def list_p2_samples(p2_root: pathlib.Path) -> list[dict[str, Any]]:
    """读 ``p2_root/samples/*.json`` 返回 P2 样本列表。

    不存在的目录返回空列表(而不是抛错),方便 dry-run。
    """
    samples_dir = p2_root / "samples"
    if not samples_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(samples_dir.glob("*.json")):
        try:
            data = _load_json(p)
        except json.JSONDecodeError:
            continue
        data["_p2_path"] = str(p)
        out.append(data)
    return out


# ── 还原 Sample ────────────────────────────────────────────────────


def _resolve_sample(
    p2_sample: Mapping[str, Any], p2_root: pathlib.Path
) -> Sample | None:
    """把一条 P2 样本回溯到 ``Sample`` 对象。

    通过 ``source_archive`` 字段找到 P1 存档,再借
    :func:`assembler.schema.split_archive` 切出 ``Sample`` 列表,按
    ``sample_id`` 匹配。

    返回 ``None`` 表示源存档缺失或 sample_id 找不到 —— 不抛错,
    让上游决定走 quarantine 还是 skip。
    """
    archive_name = p2_sample.get("source_archive") or ""
    if not archive_name:
        return None
    archive_path = p2_root / archive_name
    if not archive_path.exists():
        return None
    try:
        samples = split_archive(archive_path)
    except Exception:
        return None
    target_id = p2_sample.get("sample_id") or ""
    for s in samples:
        if s.sample_id == target_id:
            return s
    return None


# ── 跑四维评分 ────────────────────────────────────────────────────


def _score_one(sample: Sample) -> list[dict[str, Any]]:
    """跑 D-2/D-4/D-5/D-6 四个评分器,转成统一字典列表。"""
    out: list[dict[str, Any]] = []
    for dim in ("d2", "d4", "d5", "d6"):
        verdict = _SCORERS[dim](sample)
        # ``is_veto`` 来自不同位置:D5 有属性,D-2/D-4/D-6 走 reasons 推断。
        # 但只有 D5 当前会触发 veto,所以这里只查 D5,其它维度默认为 False。
        # 后续若新增 veto 维度,扩这里即可。
        is_veto = bool(getattr(verdict, "is_veto", False))
        out.append({
            "dimension": dim,
            "score": getattr(verdict, "score", None),
            "is_veto": is_veto,
            "reasons": list(getattr(verdict, "reasons", ()) or ()),
        })
    return out


def _status_of(scores: list[dict[str, Any]]) -> str:
    """根据四维 score 推导 :class:`ScoutStatus`。

    任一维 ``score is None`` → ``undetermined``;否则 ``scored``。
    """
    if any(s["score"] is None for s in scores):
        return "undetermined"
    return "scored"


# ── L5 旁路 ────────────────────────────────────────────────────────


def index_mediabrowse(mediabrowse_dir: pathlib.Path) -> dict[tuple[str, str], dict[str, Any]]:
    """把 ``mediabrowse/<task>__<run>.json`` 索引成 ``(task_id, url) → l5_outcome``。

    只保留有 ``l5_outcome`` 字段的存档;其它文件忽略(可能是 L0-L4 的中间产物)。
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not mediabrowse_dir.is_dir():
        return out
    for p in sorted(mediabrowse_dir.glob("*.json")):
        try:
            data = _load_json(p)
        except json.JSONDecodeError:
            continue
        l5 = data.get("l5_outcome")
        if not isinstance(l5, dict):
            continue
        task_id = str(data.get("task_id") or "")
        url = str(l5.get("url") or "")
        if not task_id or not url:
            continue
        out[(task_id, url)] = {
            "branch": l5.get("branch"),
            "evidence": l5.get("evidence") or "",
            "note": l5.get("note") or "",
        }
    return out


def _attach_mediabrowse(
    rec: dict[str, Any], mediabrowse_index: Mapping[tuple[str, str], dict[str, Any]]
) -> dict[str, Any]:
    """把 L5 证据挂到 ``evidence_supplement``。

    找不key 的处理:留空 dict,**不**算缺件(不变量 4)。
    """
    supplement: dict[str, Any] = {}
    key = (rec["task_id"], rec.get("_url") or "")
    if key in mediabrowse_index:
        supplement["l5"] = mediabrowse_index[key]
    rec["evidence_supplement"] = supplement
    rec.pop("_url", None)
    return rec


# ── 整批主流程 ────────────────────────────────────────────────────


def run_scout(
    p2_root: pathlib.Path,
    out_dir: pathlib.Path,
    *,
    mediabrowse_dir: pathlib.Path | None = None,
) -> dict[str, int]:
    """跑 scout 全流程,落三件套 JSONL,返回 :class:`Counter`-able 统计。

    Args:
        p2_root: P1 存档与 P2 samples 的共同根(默认 ``output/pipeline``)。
        out_dir: 三件套落盘目录(会被创建)。
        mediabrowse_dir: 可选,指向 ``output/pipeline/mediabrowse``。

    Returns:
        ``{"positive": int, "negative": int, "quarantine": int, "skip": int,
            "missing_source": int}``。
    """
    p2_samples = list_p2_samples(p2_root)
    mb_index = (
        index_mediabrowse(mediabrowse_dir) if mediabrowse_dir else {}
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    buckets: dict[str, list[dict[str, Any]]] = {
        "positive": [], "negative": [], "quarantine": [],
    }
    counters: collections.Counter = collections.Counter(
        {"positive": 0, "negative": 0, "quarantine": 0,
         "skip": 0, "missing_source": 0}
    )

    for p2 in p2_samples:
        sample = _resolve_sample(p2, p2_root)
        if sample is None:
            counters["skip"] += 1
            continue
        scores = _score_one(sample)
        status = _status_of(scores)
        # 取 task_id / run_id 优先用 P1 存档(更稳)
        task_id = p2.get("task_id") or getattr(sample, "task_id", "")
        run_id = ""
        if "_" in pathlib.Path(p2.get("_p2_path", "")).stem:
            parts = pathlib.Path(p2["_p2_path"]).stem.split("__")
            if len(parts) >= 2:
                run_id = parts[1]
        record: dict[str, Any] = {
            "sample_id": sample.sample_id,
            "task_id": task_id,
            "run_id": run_id,
            "status": status,
            "scores": scores,
            "tags": [],
            "evidence_supplement": {},
            "_url": str(getattr(getattr(sample, "outcome", None), "url", "") or ""),
        }
        if mediabrowse_dir is not None:
            _attach_mediabrowse(record, mb_index)

        from trajectory_pipeline.evaluation.scout.policy import classify, tags_for
        kind = classify(record)  # type: ignore[arg-type]
        if kind == "skip":
            counters["skip"] += 1
            continue
        if kind == "quarantine":
            record["tags"] = []
            buckets["quarantine"].append(record)
            counters["quarantine"] += 1
            continue
        if kind == "negative":
            record["tags"] = list(tags_for(record))  # type: ignore[arg-type]
            buckets["negative"].append(record)
            counters["negative"] += 1
            continue
        # positive
        record["tags"] = []
        buckets["positive"].append(record)
        counters["positive"] += 1

    # 落盘:三件套
    _dump_jsonl(out_dir / "positive_candidates.jsonl", buckets["positive"])
    _dump_jsonl(out_dir / "negative_candidates.jsonl", buckets["negative"])
    _dump_jsonl(out_dir / "quarantine.jsonl", buckets["quarantine"])

    # 写 scout.jsonl(总览,含所有 scored/quarantine,不含 skip)
    overview = buckets["positive"] + buckets["negative"] + buckets["quarantine"]
    _dump_jsonl(out_dir / "scout.jsonl", overview)

    return dict(counters)


def _dump_jsonl(path: pathlib.Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(_clean(row), ensure_ascii=False, sort_keys=True) + "\n")


def _clean(obj: Any) -> Any:
    """去掉 ``_p2_path`` / ``_url`` 这类 scout 内部临时字段。"""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _clean(dataclasses.asdict(obj))
    return obj


__all__ = [
    "list_p2_samples",
    "index_mediabrowse",
    "run_scout",
]
