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
import logging
import pathlib
from typing import Any, Iterable, Mapping

from trajectory_pipeline.assembler.schema import Sample, split_archive
from trajectory_pipeline.evaluation.scorers.code_scorer import score_d2
from trajectory_pipeline.evaluation.scorers.d4_scorer import score_d4
from trajectory_pipeline.evaluation.scorers.d5_scorer import score_d5
from trajectory_pipeline.evaluation.scorers.d6_scorer import score_d6
from trajectory_pipeline.judgement_causes import (
    CAUSE_DATA,
    CAUSE_LLM,
    CAUSE_NO_CLIENT,
    CAUSE_SCORED,
)
from trajectory_pipeline.llm.client import LLMClient

log = logging.getLogger(__name__)

#: 与 :data:`evaluation.cli.SCORERS` 同名短码 → 打分函数。
#: **本模块自己维护一份**,因为 evaluation.cli 走的是
#: ``score_d2(sample: Sample)`` 形态,而 scout 调用时仍要按对象喂;
#: 引入 evaluation.cli 只会把 ``reconfigure(encoding=...)`` 这种 UI 关注
#: 拖进来,违反「scout 是纯计算模块」的设计。
#:
#: ⚠️ **D-2 自 rubric v1.4 起需要 ``llm_client``**（代码筛 + LLM 判语义）。
#: scout 是纯计算模块、**不建客户端**（不读环境变量——那是 CLI 的事），
#: 客户端由调用方经 ``llm_client=`` 注入。不给 ⇒ D-2 回 ``undetermined``
#: （理由写明后端未配置），整条记录因此落 ``quarantine``——fail-closed 的
#: 设计行为，不是判不了。
_SCORERS = {
    "d2": score_d2,
    "d4": score_d4,
    "d5": score_d5,
    "d6": score_d6,
}

#: :data:`_SCORERS` 里接受 ``llm_client`` 的维度。scout 不 import
#: ``evaluation.cli``（见上），所以这行是 :data:`evaluation.cli.LLM_DIMS`
#: 在本模块覆盖范围内的**手工投影**；加 LLM 维度时两处一起改。
_LLM_DIMS: frozenset[str] = frozenset({"d2"})

#: **单元计数键**——相加 = 输入 P2 样本数（scout ``run`` 的 ``sum=`` 行
#: 就是按这几个键加的）。
#:
#: ⚠️ **别把新计数塞进这几个键**：`sum(stats.values())` 的口径是
#: 「每个输入样本恰好落一个桶」，多塞一个分栏计数就等于把分母算错
#: （2026-11-11 那批：pos+neg+quarantine+skip+missing = 919 = 输入数）。
#: 分栏计数（``quarantine_data`` / ``quarantine_llm``）是**另一个维度**
#: 的读数，单独放。
UNIT_COUNTER_KEYS: tuple[str, ...] = (
    "positive", "negative", "quarantine", "skip", "missing_source",
)

#: 批次级熔断的**默认阈值**：连续这么多次「LLM 调用失败」就停本批 D-2
#: 调用（H-36）。
#:
#: ⚠️ **为什么客户端的熔断不够**：:class:`LLMClient` 的
#: :class:`CircuitBreaker` 只数 **4xx**——而 2026-11-11 那批 578 次失败
#: 全是「后端答了但正文为空」这一类（4xx 之外），熔断器一次都没动，
#: 于是整批安静地退化成 91 % quarantine（838/919），**退出码 0**。
#: ⇒ 评估侧要另有一层：**连续失败计数 + 停本批 D-2 调用 + 显式报
#: 「本批判分不可信」**。
#:
#: 默认值与客户端熔断一致（3）；批次层可用
#: ``llm_failure_threshold=`` 覆盖——**长批次调大**（误停一次的代价是
#: 整批重跑）。
DEFAULT_LLM_FAILURE_THRESHOLD: int = 3

#: 批次元信息文件名（H-36）。落「本批判分可不可信」，报告侧与上传侧读它。
BATCH_META_FILE: str = "scout_meta.json"


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


def _units_of(sample: Sample) -> list[tuple[str, Any, Any, int | None]]:
    """把一条 per-Run 样本展开成**评分单元**（H-1）。

    与 ``evaluation.cli._units_of`` 同款口径（两处独立实现——scout 不
    import ``evaluation.cli``，那条依赖会把 CLI 的 UI 关注带进来）：

    - 有站点视图 ⇒ 每个访问点一条（``site_index`` = 访问序号，1 起）；
    - 没有站点视图 ⇒ 回退整条样本（``site_index=None``）。

    返回 ``[(unit_id, unit, outcome, site_index), ...]``。
    """
    views_of = getattr(sample, "site_views", None)
    if callable(views_of):
        views = tuple(views_of())
        if views:
            return [
                (
                    str(getattr(v, "sample_id", "") or ""),
                    v,
                    getattr(v, "outcome", None),
                    int(getattr(v, "index")),
                )
                for v in views
            ]
    return [(
        str(getattr(sample, "sample_id", "") or ""),
        sample,
        getattr(sample, "outcome", None),
        None,
    )]


def _score_one(sample: Sample, *,
               llm_client: LLMClient | None = None,
               d2_circuit_reason: str | None = None) -> list[dict[str, Any]]:
    """跑 D-2/D-4/D-5/D-6 四个评分器,转成统一字典列表。

    ⚠️ 入参是**一个评分单元**（``SiteView`` 或整条样本），不是必然的
    ``Sample``。``overlay-scout`` 也把它当单单元打分器复用。

    ``llm_client`` 只传给 :data:`_LLM_DIMS` 里的维度（今天只有 D-2）；
    ``None`` ⇒ D-2 落 ``undetermined``（fail-closed），D-4/D-5/D-6 不受影响。

    ``d2_circuit_reason`` 非空 ⇒ **批次级熔断已打开**：D-2 直接落
    ``undetermined`` 并带这句话，**不再调用后端**（D-4/D-5/D-6 是纯代码
    判据，照跑不误——熔断的是裁判后端，不是全部评分）。

    每条结果带一个 ``cause``（H-36）：``""`` = 已判出；``"data"`` =
    数据判不出；``"llm"`` = 裁判后端没答；``"no_client"`` = 没配后端。
    汇总分栏（「数据判不出」/「LLM 没答」）就靠它。
    """
    out: list[dict[str, Any]] = []
    for dim in ("d2", "d4", "d5", "d6"):
        if dim == "d2" and d2_circuit_reason:
            out.append({
                "dimension": "d2",
                "score": None,
                "is_veto": False,
                "reasons": [d2_circuit_reason],
                "cause": CAUSE_LLM,
            })
            continue
        if llm_client is not None and dim in _LLM_DIMS:
            verdict = _SCORERS[dim](sample, llm_client=llm_client)
        else:
            verdict = _SCORERS[dim](sample)
        # ``is_veto`` 来自不同位置:D5 有属性,D-2/D-4/D-6 走 reasons 推断。
        # 但只有 D5 当前会触发 veto,所以这里只查 D5,其它维度默认为 False。
        # 后续若新增 veto 维度,扩这里即可。
        is_veto = bool(getattr(verdict, "is_veto", False))
        score = getattr(verdict, "score", None)
        cause = str(getattr(verdict, "cause", "") or "")
        if not cause and score is None:
            # 非 LLM 维（D-4/D-5/D-6）的缺判都是「事实缺失」⇒ 数据侧。
            # 它们没有 ``cause`` 字段（评分器未升级），这里统一补齐，
            # 免得「数据判不出」这一栏被算成空。
            cause = CAUSE_DATA
        out.append({
            "dimension": dim,
            "score": score,
            "is_veto": is_veto,
            "reasons": list(getattr(verdict, "reasons", ()) or ()),
            "cause": cause,
        })
    return out


def _unit_cause(scores: list[dict[str, Any]]) -> str:
    """单元级缺判归因（**LLM 侧优先**）。

    任一维因「后端没答」而缺判 ⇒ 整条记 ``"llm"``（``"no_client"`` 也算
    LLM 侧——它同样是 LLM 能力缺口，只是没发过请求）。否则有缺判 ⇒
    ``"data"``。全判出 ⇒ ``""``。
    """
    causes = {str(s.get("cause") or "") for s in scores}
    if causes & {CAUSE_LLM, CAUSE_NO_CLIENT}:
        return CAUSE_LLM
    if any(s.get("score") is None for s in scores):
        return CAUSE_DATA
    return CAUSE_SCORED


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


def _failure_threshold_of(
    llm_client: LLMClient | None, explicit: int | None
) -> int:
    """批次级熔断阈值：显式值 > 客户端 cfg > 默认。

    客户端 cfg 里的那个数就是环境变量 ``TRAJECTORY_LLM_CIRCUIT_THRESHOLD``
    （:meth:`LLMConfig.from_env` 读的）⇒ **同一个旋钮**：客户端数 4xx、
    批次层数「任何 LLM 调用失败」，阈值一致。
    """
    if explicit is not None:
        return max(1, int(explicit))
    cfg = getattr(llm_client, "cfg", None)
    value = getattr(cfg, "circuit_breaker_threshold", None)
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_LLM_FAILURE_THRESHOLD


def run_scout(
    p2_root: pathlib.Path,
    out_dir: pathlib.Path,
    *,
    mediabrowse_dir: pathlib.Path | None = None,
    llm_client: LLMClient | None = None,
    llm_failure_threshold: int | None = None,
) -> dict[str, int]:
    """跑 scout 全流程,落三件套 JSONL,返回 :class:`Counter`-able 统计。

    Args:
        p2_root: P1 存档与 P2 samples 的共同根(默认 ``output/pipeline``)。
        out_dir: 三件套落盘目录(会被创建)。
        mediabrowse_dir: 可选,指向 ``output/pipeline/mediabrowse``。
        llm_client: 裁判后端,D-2 用;**由调用方注入**（CLI 按
            ``TRAJECTORY_LLM_*`` 建），本函数不读环境变量。``None`` ⇒
            D-2 全部 ``undetermined``，记录落 ``quarantine``。
        llm_failure_threshold: 批次级熔断阈值（连续多少次 LLM 调用失败就
            停本批 D-2 调用）。``None`` ⇒ 取客户端 cfg 的
            ``circuit_breaker_threshold``（即环境变量
            ``TRAJECTORY_LLM_CIRCUIT_THRESHOLD``）。

    Returns:
        ``{"positive": int, "negative": int, "quarantine": int, "skip": int,
            "missing_source": int, "quarantine_data": int,
            "quarantine_llm": int, "llm_failure_total": int,
            "llm_circuit_open": int}``。前五个按**评分单元**（有站点明细时
        = 站点）计数——与 per-Run 之前「一个 unit 一条记录」的口径一致，
        且**相加 = 输入 P2 样本数**（见 :data:`UNIT_COUNTER_KEYS`）。
        ``quarantine_data`` / ``quarantine_llm`` 是 **quarantine 的两栏**
        （数据判不出 / LLM 没答），是另一个维度的读数，**不加进上面那个和**。
        ``llm_failure_total`` 数的是**真发过请求且失败**的次数——熔断打开后
        D-2 被跳过（压根没发请求），那些条只进 ``quarantine_llm`` 分栏，
        **不计入失败数**（那个数要回答的是「后端失败了几次」）。

    ⚠️ **批次级熔断（H-36）**：连续 N 次「LLM 调用失败」（后端挂了 / 答了
    但正文空 / 预算耗尽）⇒ **停本批 D-2 调用**、剩余单元落
    ``undetermined``（归因 ``llm``）、并写 :data:`BATCH_META_FILE` 标
    「本批判分不可信」。D-4/D-5/D-6 是纯代码判据，**照跑不误**。
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
         "skip": 0, "missing_source": 0,
         # H-36：quarantine 的两栏（数据判不出 / LLM 没答）。
         # ⚠️ 这两个键**不在** UNIT_COUNTER_KEYS 里——它们是 quarantine
         # 的再分栏，不是「每个输入样本落一个桶」那个和的一部分。
         "quarantine_data": 0, "quarantine_llm": 0}
    )
    # ── 批次级熔断与归因统计 ────────────────────────────────────
    threshold = _failure_threshold_of(llm_client, llm_failure_threshold)
    streak = 0            # 连续 LLM 调用失败数（后端健康 ⇒ 清零）
    streak_max = 0        # 本批见过的最长连续失败数
    llm_failure_total = 0
    circuit_open = False
    circuit_reason = ""
    last_error_code = ""

    for p2 in p2_samples:
        sample = _resolve_sample(p2, p2_root)
        if sample is None:
            counters["skip"] += 1
            continue
        # 取 task_id / run_id 优先用 P1 存档(更稳)
        task_id = p2.get("task_id") or getattr(sample, "task_id", "")
        run_id = ""
        if "_" in pathlib.Path(p2.get("_p2_path", "")).stem:
            parts = pathlib.Path(p2["_p2_path"]).stem.split("__")
            if len(parts) >= 2:
                run_id = parts[1]
        # ⚠️ **逐站点打分**（H-1）：一条 per-Run 样本可能覆盖多个访问点，
        # 对整条轨迹跑四维只会留下末条 outcome 的结论 ⇒ 失败站点在
        # scout 侧消失，aggregate 的 min 退化成「最差 run」。
        for unit_id, unit, outcome, site_index in _units_of(sample):
            d2_circuit_reason = circuit_reason if circuit_open else None
            if not circuit_open:
                # 客户端自己的 4xx 熔断打开 ⇒ 直接开批次熔断，
                # 不要再发请求（那 170 次 401 的教训）。
                breaker = getattr(llm_client, "breaker", None)
                if breaker is not None and bool(getattr(breaker, "open", False)):
                    circuit_open = True
                    circuit_reason = (
                        f"LLM 熔断打开（{getattr(breaker, 'reason', lambda: '')() or '连续 4xx'}）"
                        f"——本批判分不可信，已停止 D-2 调用"
                    )
                    d2_circuit_reason = circuit_reason
            scores = _score_one(
                unit, llm_client=llm_client,
                d2_circuit_reason=d2_circuit_reason,
            )
            # ── 连续失败计数（**只看 D-2**：只有它会发请求）────────
            d2 = next((s for s in scores if s.get("dimension") == "d2"), None)
            d2_cause = str((d2 or {}).get("cause") or "")
            if d2_cause == CAUSE_LLM:
                # ⚠️ **只算真发过请求的失败**：熔断已开时 D-2 是**被跳过**
                # 的（压根没发请求），把它算成「调用失败」会让
                # ``llm_failure_total`` 虚高——那个数要回答的是
                # 「后端失败了几次」。
                if d2_circuit_reason is None:
                    streak += 1
                    streak_max = max(streak_max, streak)
                    llm_failure_total += 1
                    last_error_code = str(
                        getattr(llm_client, "last_call_error", "") or ""
                    )
                    if not circuit_open and streak >= threshold:
                        circuit_open = True
                        circuit_reason = (
                            f"LLM 连续 {streak} 次调用失败"
                            f"（最后 {last_error_code or '未记录错误码'}）"
                            f"——本批判分不可信，已停止 D-2 调用"
                        )
            elif d2_cause == CAUSE_SCORED:
                # D-2 判出来了 ⇒ 后端这一刻是好的 ⇒ 连续计数清零。
                streak = 0
            # ⚠️ ``data`` / ``no_client`` **不动 streak**：它们没发过请求，
            # 证明不了后端好坏（拿它们清零会让坏后端被数据缺口掩护）。
            status = _status_of(scores)
            cause = _unit_cause(scores)
            url = str(getattr(outcome, "url", "") or "")
            record: dict[str, Any] = {
                # ``sample_id`` 仍是 **Run 级 id**：P3 视图按它取名
                # （``<task>__<run>_openai.json``），改掉会让 LS 导出
                # 一条都读不到。站点身份放在 ``unit_id`` / ``site_index``。
                "sample_id": str(getattr(sample, "sample_id", "") or ""),
                "unit_id": unit_id,
                "site_index": site_index,
                "url": url,
                "task_id": task_id,
                "run_id": run_id,
                "status": status,
                "scores": scores,
                "tags": [],
                "evidence_supplement": {},
                # H-36：单元级缺判归因（"" = 已判出）。
                "undetermined_cause": cause,
                "_url": url,
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
                counters["quarantine_llm" if cause == CAUSE_LLM
                          else "quarantine_data"] += 1
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

    counters["llm_failure_total"] = llm_failure_total
    counters["llm_circuit_open"] = 1 if circuit_open else 0
    _write_batch_meta(
        out_dir,
        counters=counters,
        circuit_open=circuit_open,
        circuit_reason=circuit_reason,
        threshold=threshold,
        streak_max=streak_max,
        llm_failure_total=llm_failure_total,
        last_error_code=last_error_code,
    )

    return dict(counters)


def _write_batch_meta(
    out_dir: pathlib.Path,
    *,
    counters: Mapping[str, int],
    circuit_open: bool,
    circuit_reason: str,
    threshold: int,
    streak_max: int,
    llm_failure_total: int,
    last_error_code: str,
) -> None:
    """写批次元信息（:data:`BATCH_META_FILE`）——「本批判分可不可信」。

    ⚠️ **为什么单独一个文件而不是某个记录上的一个字段**：可信性是
    **批次级**结论（熔断打开 ⇒ 整批判分不可信），而记录级只知道自己
    那条缺没缺判。混在一起会让「这批能不能用」这个问题没有单一答案。

    ⚠️ **判分不可信 ≠ 产物是垃圾**：熔断前的单元是后端健康时判出来的，
    照样有效。**上传侧按记录过滤**（归因 ``llm`` 的缺判不进人工队列），
    批次元信息只回答「这批整体可不可信」。
    """
    payload = {
        "judgement_trustworthy": not circuit_open,
        "llm_circuit_open": circuit_open,
        "circuit_reason": circuit_reason,
        "llm_failure_threshold": threshold,
        "llm_failure_streak_max": streak_max,
        "llm_failure_total": llm_failure_total,
        "last_error_code": last_error_code,
        "counters": dict(counters),
    }
    try:
        (out_dir / BATCH_META_FILE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:  # pragma: no cover - 落盘失败不阻塞整批
        log.warning("scout: 写 %s 失败", out_dir / BATCH_META_FILE)


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
    "UNIT_COUNTER_KEYS",
    "DEFAULT_LLM_FAILURE_THRESHOLD",
    "BATCH_META_FILE",
]
