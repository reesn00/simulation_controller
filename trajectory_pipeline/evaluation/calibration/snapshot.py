"""全量逐行预测快照（方案 16 §9.2 C-15）。

为什么需要它（这句话决定了本模块的形态）
----------------------------------------
分歧卡只记「**这一轮**不一致」。而回归判定问的是「**上一轮对、这一轮错**」——
那一行在上一轮的卡里**根本不存在**（上一轮它是对的啊）。所以：

    只有分歧卡 ⇒ 回归判不出来。必须先有全量快照，卡是快照的视图。

快照按 ``(v{N}, rubric_version, evaluator_version)`` 落盘。三样都记，是因为
三者任一变了，「同一行判了什么」就不再可比（方案 16 §5.5：改一条款就作废该
条款上旧金标的可比性）。

判定口径（五态封闭集）
----------------------
每条 ``(golden_id, dimension)`` 一条 entry，:data:`VERDICTS` 五选一：

=============== ==========================================================
``agree``       两侧都有值且一致（硬分类按口径 C 的语义等价类）
``detail_mismatch`` 口径 C 一致、但细分标签不同——**D-10 不算错**，
                记下来是因为细分支对账还需要它（§3.2）
``disagree``    口径 C 不一致（``eq_class_mismatch``）——这才是 D-10 分子
``tier_gap``    软维度档差超过 tolerance
``ungraded``    任一侧缺失 / 越界 / 维度未登记——**不计分母**（``None ≠ 0``）
=============== ==========================================================

``label_mismatch`` / ``eq_class_mismatch`` / ``tier_gap`` 是三态 ``delta_kind``
（§3.2），只有后三态进分歧卡；``agree`` 与 ``ungraded`` 不进。

与 ``builder.cmd_agreement`` 的同源要求
---------------------------------------
:func:`iter_site_rows` 的过滤判据**必须**与 ``cmd_agreement`` 里算 ``site``
那两行一致（非 ``p2_sample`` 且 ``golden`` 非空）。两处会漂，由
``tests/unit/test_calibration_snapshot.py::test_快照与agreement同源`` 钉住：
快照在**头条档**上的 ``n`` / ``agreed`` 必须与命令行打印的逐字相等。

分档（方案 16 C-04 / C-17）
--------------------------
每条的 ``split`` 原样带出行上的 ``__split__``（``calibration`` / ``regression``
/ ``holdout``）。快照**三档全收**：回归集要能判"原本判对的现在判错"，
光靠调参池判不出来；但 :func:`summarize_snapshot` 的 ``d10`` 只算头条档，
并把分档读数拆在 ``d10_by_split`` 里——两个数都在，不靠"读的人记得是哪个"。

fail-closed
-----------
- 未登记标签 → :func:`~...alignment.equiv_class` 抛 ``KeyError``，本模块**不捕获**
  （D-10 当场炸，而不是悄悄算错）；
- 维度不在 :data:`SOFT_DIM_NAMES` 白名单 → 记 ``ungraded`` + ``note``，
  **不猜它的条款号**（``rubric_ref`` 留空）。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Iterable, Iterator, Mapping, Sequence

from trajectory_pipeline.evaluation import access_surface
from trajectory_pipeline.evaluation.calibration.provenance import (
    FACE_COLLECTION,
    STATUS_NOT_APPLICABLE,
    STATUS_UNRECORDED,
    face_of_dimension,
    face_record,
    face_version_of,
)
from trajectory_pipeline.evaluation.golden.alignment import (
    SOFT_DIM_NAMES,
    dim_agreement,
    is_equivalent,
)
from trajectory_pipeline.evaluation.golden.splits import HEADLINE_SPLIT
from trajectory_pipeline.evaluation.scorers.rubric_version import (
    VERSION_UNRECORDED,
    clause_of,
    code_scorer_version,
)

#: 快照文件 schema 版本。字段增删必须升它（回归判定吃的是这份文件）。
#:
#: ``"2"``（方案 20 §R3）：条目加 ``gold_access_surface`` /
#: ``pred_access_surface`` / ``surface_comparable`` 三栏——「这一条的分歧
#: 是不是跨面读出来的」。升它的代价是**旧的 v1 快照不能再当基线**（
#: ``verify._check_comparable`` 拒跨 schema 差分），而那是想要的：
#: 没有面这一栏的旧快照判不出「跨面分歧」，拿它当基线，「这一轮多出来的
#: 分歧」会被读成模型变差。
SNAPSHOT_SCHEMA_VERSION = "2"

VERDICT_AGREE = "agree"
VERDICT_DETAIL_MISMATCH = "detail_mismatch"
VERDICT_DISAGREE = "disagree"
VERDICT_TIER_GAP = "tier_gap"
VERDICT_UNGRADED = "ungraded"

#: 五态封闭集（顺序即报表顺序）。新增状态先问：它属于哪一档？
VERDICTS: tuple[str, ...] = (
    VERDICT_AGREE,
    VERDICT_DETAIL_MISMATCH,
    VERDICT_DISAGREE,
    VERDICT_TIER_GAP,
    VERDICT_UNGRADED,
)

#: 进分歧卡的三态（有差异、且差异有责任方）。``agree`` / ``ungraded`` 不进。
DIFF_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_DETAIL_MISMATCH, VERDICT_DISAGREE, VERDICT_TIER_GAP}
)

#: ``delta_kind`` 三态（方案 16 §3.2）。
DELTA_LABEL_MISMATCH = "label_mismatch"
DELTA_EQ_CLASS_MISMATCH = "eq_class_mismatch"
DELTA_TIER_GAP = "tier_gap"

#: 硬分类维度名（D-10 口径 C 的那一列）。
HARD_DIM = "d10"

#: 预测侧出处标签。**D-10 的 pred 不是当前评分器重算的**，是冻结行里采集时
#: 写下的 ``code.branch``——把它当成本次代码的产物是这类报表最常见的误读。
#:
#: ⚠️ **两个 ``code:`` 前缀不是同一件事**（这是 C-11 补齐时踩出来的坑）：
#: ``PROVIDER_FROZEN_BRANCH`` 的 ``code`` 指「**采集侧代码**判的分支」，
#: ``PROVIDER_ROW_DIMS`` 的 ``code`` 指「**代码评分器**（D-2/D-4/D-5/D-6）」。
#: 要判「这批 pred 出自哪一面」，**别看 provider 字符串**——看
#: :func:`~...provenance.face_of_dimension`（按维度投影的唯一真源）。
PROVIDER_FROZEN_BRANCH = "code:frozen_row"
PROVIDER_ROW_DIMS = "code:row.dimension_scores"

#: ``pred_provider`` 里的版本占位：**没记到**。
#:
#: ⚠️ 存量缺口已由**版本级台账**补上（方案 16 C-11 补齐）：
#: 冻结行 v1–v8 是旧 ``overlay-scout`` 跑出来的，那时还不写 ``scorer_version``；
#: 而 v1–v8 是**冻结档，一字不许改** ⇒ 逐行版本补不回来。
#: 补法是**档外**的版本级台账（``evaluation/golden/data/scorer_provenance.jsonl``）：
#: **重算**当前代码面、逐行比对存档里的读数 ⇒ 得出「复现得出来（可比）」
#: 或「复现不出来（已知不可比）」。
#: 答不出来的那一档仍然写 ``unrecorded``，**不拿当前指纹顶替**——顶替会让
#: 「没量到」看起来像「量过且一致」，正是 M-29 那类误读。
PRED_VERSION_UNRECORDED = VERSION_UNRECORDED


def pred_provider_string(kind: str, version: str | None) -> str:
    """``<kind>@<version>``——出处 + 版本（方案 16 C-11）。

    §3.1 的契约原本就写着「``pred_provider`` = ``code:…`` 或
    ``llm:<model>/<prompt_version>``」——**版本本来就该在出处里**，C-11 只是
    把它填上。版本没记到时写 ``unrecorded``，**不省略 ``@`` 段**：省掉它，
    「没记版本」与「有版本但没记」会长得一样。
    """
    return f"{kind}@{version or PRED_VERSION_UNRECORDED}"


def provider_version(provider: str) -> str:
    """从 ``pred_provider`` 取版本段；没有 ``@`` → ``unrecorded``。"""
    if "@" not in provider:
        return PRED_VERSION_UNRECORDED
    return provider.split("@", 1)[1] or PRED_VERSION_UNRECORDED


def provider_kind(provider: str) -> str:
    """从 ``pred_provider`` 取**种类段**（``@`` 之前那截）；没有 → 空串。

    ⚠️ **别拿整个 ``pred_provider`` 去比常量**：C-11 之后它带版本后缀
    （``code:frozen_row@unrecorded``），整串比对**永远不相等**⇒ 依赖它的
    判定会静默失效（worklist 的 ``_detect_rule_decided`` 就踩过这个）。
    比种类段才对。
    """
    return str(provider or "").split("@", 1)[0]


def row_scorer_version(row: Mapping[str, Any]) -> str:
    """冻结行里记的评分面指纹；没记 → ``unrecorded``。

    **不拿当前评分器的指纹顶替**：顶替会让「这行是旧评分器打的」看起来像
    「这行是当前评分器打的」，而这两件事的可比性完全不同。
    """
    v = row.get("scorer_version")
    return str(v) if v else PRED_VERSION_UNRECORDED


def iter_site_rows(
    rows: Iterable[Mapping[str, Any]],
) -> Iterator[Mapping[str, Any]]:
    """站点级已标注行——**与 ``cmd_agreement`` 的 ``site`` 同一判据**。

    轨迹级样本（``source_kind == "p2_sample"``）没有可比的人工标签，
    未标注的行（``golden`` 为空）无从判对错。两处过滤必须一致，
    否则快照的分母和头条的分母是两个数（见模块 docstring 的同源要求）。
    """
    for row in rows:
        if row.get("source_kind") == "p2_sample":
            continue
        if not row.get("golden"):
            continue
        yield row


def _row_surfaces(
    row: Mapping[str, Any],
) -> tuple[str, str, bool | None]:
    """这一行的两个面：``(金标侧, 预测侧, 两面是否同一张)``（方案 20 §R3）。

    - **金标侧** = ``golden.access_surface``：人工复核时用的是哪张面；
    - **预测侧** = 行顶层 ``access_surface``：这条结论是哪张采集面上得出的
      （候选行由 ``builder._scan_archives`` 从存档 ``driver`` 折算）。

    三态的后一格直接走 :func:`access_surface.same_surface`——**不在这里再写
    一遍「面相同吗」**：两侧口径各写一份，报表上就会出现两个「跨面」结论
    （方案 20 §R3 那条「R3 的比较逻辑不许自己再写一遍」）。

    历史行两侧都可能没记 ⇒ ``(unrecorded, unrecorded, None)``：不猜、不补。
    """
    gold = access_surface.surface_of_annotation(
        row.get("golden") if isinstance(row.get("golden"), Mapping) else None
    )
    pred = access_surface.check_surface(row.get("access_surface"))
    return gold, pred, access_surface.same_surface(gold, pred)


def _dim_scores(side: Mapping[str, Any] | None) -> Mapping[str, Any]:
    scores = side.get("dimension_scores") if isinstance(side, Mapping) else None
    return scores if isinstance(scores, Mapping) else {}


def _pred_reasons(row: Mapping[str, Any], dimension: str) -> list[str]:
    """该维度的判分理由原文（``scores_detail`` 里按 dimension 取）。"""
    detail = row.get("scores_detail")
    if not isinstance(detail, Sequence):
        return []
    for item in detail:
        if isinstance(item, Mapping) and item.get("dimension") == dimension:
            reasons = item.get("reasons")
            if isinstance(reasons, Sequence) and not isinstance(reasons, str):
                return [str(r) for r in reasons]
            return []
    return []


def _clause_or_empty(dimension: str, tier: Any) -> tuple[str, str]:
    """``(rubric_ref, note)``；维度未登记条款号时留空并说明原因。"""
    try:
        tier_int = int(tier) if isinstance(tier, (int, float)) else None
    except (TypeError, ValueError):
        tier_int = None
    try:
        return clause_of(dimension, tier_int), ""
    except (KeyError, ValueError) as exc:
        return "", f"条款号不可用：{exc}"


def _entry(
    *,
    golden_id: str,
    split: str,
    dimension: str,
    rubric_ref: str,
    gold_value: Any,
    pred_value: Any,
    pred_provider: str,
    pred_face: str,
    pred_face_version: str,
    pred_face_status: str,
    verdict: str,
    delta_kind: str | None,
    tier_gap: int | None,
    gold_note: str,
    pred_reasons: Sequence[str],
    pred_evidence: str | None,
    note: str = "",
    gold_access_surface: str = access_surface.SURFACE_UNRECORDED,
    pred_access_surface: str = access_surface.SURFACE_UNRECORDED,
    surface_comparable: bool | None = None,
    golden_id_legacy: str = "",
) -> dict[str, Any]:
    if verdict not in VERDICTS:  # pragma: no cover - 由构造点保证
        raise ValueError(f"未登记 verdict {verdict!r}；先扩 VERDICTS")
    return {
        "golden_id": golden_id,
        # ⚠️ **迁移期的旧 id**（v14 起）：池行候选换过 id 口径，旧 id 留在
        # 冻结行里 ⇒ 跨版本配对按它配（``verify._entry_key``），否则同一行
        # 在迁移前后配不上 ⇒ 回归集里有换键行时整对版本直接"测不成"。
        # 旧快照没有这个字段 ⇒ 空串 ⇒ 配对键就是 ``golden_id``（不变）。
        "golden_id_legacy": golden_id_legacy,
        "split": split,
        "dimension": dimension,
        "rubric_ref": rubric_ref,
        "gold_value": gold_value,
        "pred_value": pred_value,
        "pred_provider": pred_provider,
        # ↓ 面感知的三栏（方案 16 C-11 补齐）：「这批 pred 出自哪一面 /
        # 那一面是哪版 / 那一面的版本是怎么定的」。
        "pred_face": pred_face,
        "pred_face_version": pred_face_version,
        "pred_face_status": pred_face_status,
        # ↓ **采集面**三栏（方案 20 §R3）：这一段的两侧是不是在**同一张浏览
        # 身份**上读出来的。与上面三栏不是一回事——那三栏问「pred 是哪版
        # 评分器打的」，这三栏问「两次观察是不是同一张面」。``None`` =
        # 任一侧没记 ⇒ 不可判定（**不是**「跨面」）。
        "gold_access_surface": gold_access_surface,
        "pred_access_surface": pred_access_surface,
        "surface_comparable": surface_comparable,
        "verdict": verdict,
        "delta_kind": delta_kind,
        "tier_gap": tier_gap,
        "gold_note": gold_note,
        "pred_reasons": list(pred_reasons),
        "pred_evidence": pred_evidence,
        "note": note,
    }


def _hard_entry(
    row: Mapping[str, Any], *, split: str, success_label: str,
) -> dict[str, Any]:
    """D-10 硬分类条目：金标标签 ↔ 冻结行的 ``code.branch``。"""
    golden = row.get("golden") or {}
    code = row.get("code") or {}
    gold_value = golden.get("label") or success_label
    pred_value = code.get("branch") or success_label
    rubric_ref, note = _clause_or_empty(HARD_DIM, None)

    if is_equivalent(gold_value, pred_value):
        if gold_value == pred_value:
            verdict, delta = VERDICT_AGREE, None
        else:
            # 等价类相同、字面不同：D-10 不算错，但细分对账要它。
            verdict, delta = VERDICT_DETAIL_MISMATCH, DELTA_LABEL_MISMATCH
    else:
        verdict, delta = VERDICT_DISAGREE, DELTA_EQ_CLASS_MISMATCH

    gold_surface, pred_surface, comparable = _row_surfaces(row)
    return _entry(
        golden_id=str(row.get("golden_id") or ""),
        golden_id_legacy=str(row.get("golden_id_legacy") or ""),
        split=split,
        dimension=HARD_DIM,
        rubric_ref=rubric_ref,
        gold_value=gold_value,
        pred_value=pred_value,
        # 硬分类的 pred 是**采集时**写下的 code.branch，出处是采集侧，
        # 不是评分器 ⇒ 版本段记 ``unrecorded``（采集侧版本没进冻结行）。
        # 拿行里的 ``scorer_version`` 顶替会是撒谎：那栏说的是「分数谁打的」。
        pred_provider=pred_provider_string(PROVIDER_FROZEN_BRANCH, None),
        # 面 = 采集侧。**这一面从没有过版本**：判定是采集时做的（而且
        # ``code.source`` 可能是 ``llm``——感知层也是 LLM 判的），
        # 判定侧的版本从未被指纹化 ⇒ 状态 ``not_applicable``：
        # 「旧 prompt 还是新 prompt 判的」对它**不适用**，不是「不可判定」。
        pred_face=FACE_COLLECTION,
        pred_face_version=VERSION_UNRECORDED,
        pred_face_status=STATUS_NOT_APPLICABLE,
        verdict=verdict,
        delta_kind=delta,
        tier_gap=None,
        gold_note=str(golden.get("note") or ""),
        pred_reasons=(),
        pred_evidence=code.get("evidence"),
        note=note,
        gold_access_surface=gold_surface,
        pred_access_surface=pred_surface,
        surface_comparable=comparable,
    )


def _soft_entry(
    row: Mapping[str, Any],
    *,
    split: str,
    dimension: str,
    tolerance: int,
    provenance_row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """软维度条目：人工 1–5 档 ↔ 冻结行的 ``dimension_scores``。

    **面感知**（方案 16 C-11 补齐）：这一维的 pred 出自哪一面由
    :func:`~...provenance.face_of_dimension` 投影——D-2/D-4/D-5/D-6 是**代码**
    打的（比代码面版本），D-3/D-9 是**LLM** 打的（比 prompt 面版本）。

    版本怎么来的，两条路：
    1. 行里记了 ``scorer_version`` ⇒ 直接取该面的段（状态 ``recorded``）；
    2. 行里没记（存量 v1–v8）⇒ 查**版本级台账**：台账说「当前该面逐行复现
       了本版读数」就给出版本（状态 ``reproduced``），说「复现不出」就给
       状态 ``diverged``（**已知不可比**），没有记录就 ``unrecorded``。
    """
    golden = row.get("golden") or {}
    gold_dims = _dim_scores(golden)
    pred_dims = _dim_scores(row)
    gold_value = gold_dims.get(dimension)
    pred_value = pred_dims.get(dimension)
    rubric_ref, note = _clause_or_empty(dimension, gold_value)

    if dimension not in SOFT_DIM_NAMES:
        note = note or f"维度 {dimension!r} 不在软维度白名单"
        verdict, delta, gap = VERDICT_UNGRADED, None, None
    else:
        gap = None
        if gold_value is not None and pred_value is not None:
            try:
                gap = abs(int(gold_value) - int(pred_value))
            except (TypeError, ValueError):
                gap = None
        ok = dim_agreement(gold_value, pred_value, tolerance)
        if ok is None:
            verdict, delta = VERDICT_UNGRADED, None
        elif ok:
            verdict, delta = VERDICT_AGREE, None
        else:
            verdict, delta = VERDICT_TIER_GAP, DELTA_TIER_GAP

    face = face_of_dimension(dimension)
    row_version = row_scorer_version(row)
    if row_version != PRED_VERSION_UNRECORDED:
        # 行里记了 ⇒ 出处写**全指纹**（``code:…|llm:…``），面版本取该面那段。
        provider_version = row_version
        face_version = face_version_of(row_version, face)
        face_status = "recorded"
    else:
        rec = face_record(provenance_row, face)
        status = str(rec.get("status") or STATUS_UNRECORDED)
        provider_version = None
        if status == "reproduced":
            face_version = str(rec.get("version") or VERSION_UNRECORDED)
            face_status = status
            # 出处只写**该面**的版本（``code:<fp>``）：台账证明的只有这一面，
            # 另一面没记录就不写——不拿当前值顶替。
            provider_version = f"{face}:{face_version}"
        elif status == "diverged":
            # 已知不可比：当前该面复现不出本版读数。**版本仍不可知**
            # （当时跑的是哪份源码没记），但「不可比」这件事是**已知的**。
            face_version = VERSION_UNRECORDED
            face_status = status
        else:
            face_version = VERSION_UNRECORDED
            face_status = STATUS_UNRECORDED

    gold_surface, pred_surface, comparable = _row_surfaces(row)
    return _entry(
        golden_id=str(row.get("golden_id") or ""),
        golden_id_legacy=str(row.get("golden_id_legacy") or ""),
        split=split,
        dimension=dimension,
        rubric_ref=rubric_ref,
        gold_value=gold_value,
        pred_value=pred_value,
        pred_provider=pred_provider_string(PROVIDER_ROW_DIMS, provider_version),
        pred_face=face,
        pred_face_version=face_version,
        pred_face_status=face_status,
        verdict=verdict,
        delta_kind=delta,
        tier_gap=gap,
        gold_note=str(golden.get("note") or ""),
        pred_reasons=_pred_reasons(row, dimension),
        pred_evidence=None,
        note=note,
        gold_access_surface=gold_surface,
        pred_access_surface=pred_surface,
        surface_comparable=comparable,
    )


def build_snapshot(
    rows: Sequence[Mapping[str, Any]],
    *,
    golden_version: int,
    success_label: str,
    rubric_version: str = "",
    evaluator_version: str = "",
    tolerance: int = 1,
    provenance: Mapping[int, Mapping[str, Any]] | None = None,
    code_version: str = "",
) -> dict[str, Any]:
    """全量逐行快照。

    ``rows`` 是冻结档读出的行（可含 ``__split__`` 标注，与 ``cmd_agreement``
    同一份）。D-10 每条站点行一条 entry；软维度按「金标侧 ∪ 预测侧」出现的
    dim 逐条出（缺一侧即 ``ungraded``——缺失本身是要记的事实，不是要抹掉的噪声）。

    ``rubric_version`` / ``evaluator_version`` 由调用方从 manifest 与
    ``rubric_version.evaluator_version()`` 取。老冻结档（v6 及更早）没有
    manifest 字段时传空串——**不拿当前值顶替**（方案 16 §9.2 C-01 口径 1）。

    ``provenance`` 是**版本级出处台账**（``{版本号: 行}``）：给存量冻结档
    （v1–v8，行里没记 ``scorer_version``）补「那一面的版本是哪版」。
    台账没有的维度 ⇒ ``unrecorded``，**不拿当前指纹顶替**。

    ``code_version`` 是快照取样时**代码面**的指纹——与 ``evaluator_version``
    （prompt 面）并列，因为 D-2/D-4/D-5/D-6 的可比性比的是**它**。
    缺省空串 ⇒ 不顶替（比不出来就写「比不出来」）。
    """
    prov_row = (provenance or {}).get(int(golden_version))
    if not code_version:
        # 缺省 = **当前**代码面。这是快照这一侧的面（比 pred 那一侧新），
        # 不是「给 pred 顶替版本」——两者别混。
        code_version = code_scorer_version()
    entries: list[dict[str, Any]] = []
    for row in iter_site_rows(rows):
        split = str(row.get("__split__") or "")
        entries.append(_hard_entry(row, split=split, success_label=success_label))
        gold_dims = _dim_scores(row.get("golden") or {})
        pred_dims = _dim_scores(row)
        for dimension in sorted(set(gold_dims) | set(pred_dims)):
            entries.append(
                _soft_entry(
                    row,
                    split=split,
                    dimension=dimension,
                    tolerance=tolerance,
                    provenance_row=prov_row,
                )
            )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "meta": {
            "golden_version": int(golden_version),
            "rubric_version": rubric_version,
            "evaluator_version": evaluator_version,
            # 代码面指纹：软维度（D-2/D-4/D-5/D-6）的可比性比的是它，
            # 不是 prompt 面——**面感知**（方案 16 C-11 补齐）。
            "code_version": code_version,
            "tolerance": int(tolerance),
            "success_label": success_label,
            "hard_dim": HARD_DIM,
            "merged_rows": len(rows),
            "entries": len(entries),
        },
        "entries": entries,
    }


def summarize_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """快照的计数视图（报表 / 断言用；不重算判定）。"""
    by_verdict = {v: 0 for v in VERDICTS}
    by_split: dict[str, int] = {}
    by_dimension: dict[str, dict[str, int]] = {}
    for entry in snapshot.get("entries", []):
        verdict = entry.get("verdict")
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        split = str(entry.get("split") or "")
        by_split[split] = by_split.get(split, 0) + 1
        dim = str(entry.get("dimension") or "")
        bucket = by_dimension.setdefault(dim, {v: 0 for v in VERDICTS})
        bucket[verdict] = bucket.get(verdict, 0) + 1

    # D-10 计数：分档各算一份，``d10`` 取头条档（方案 16 C-04）。
    # ``ungraded`` 不进分母（None ≠ 0）——两侧都没判的条目记不进对错。
    d10_by_split: dict[str, dict[str, int]] = {}
    for entry in snapshot.get("entries", []):
        if entry.get("dimension") != HARD_DIM:
            continue
        split = str(entry.get("split") or "")
        bucket = d10_by_split.setdefault(split, {"n": 0, "agreed": 0})
        verdict = entry.get("verdict")
        if verdict in (VERDICT_AGREE, VERDICT_DETAIL_MISMATCH):
            bucket["n"] += 1
            bucket["agreed"] += 1
        elif verdict == VERDICT_DISAGREE:
            bucket["n"] += 1
    # 老冻结档没有 ``__split__``（空串）→ 全部算头条，与换分母前一致。
    spills = [s for s in d10_by_split if s]
    d10 = (
        d10_by_split.get(HEADLINE_SPLIT, {"n": 0, "agreed": 0})
        if spills
        else d10_by_split.get("", {"n": 0, "agreed": 0})
    )
    return {
        "entries": sum(by_verdict.values()),
        "by_verdict": by_verdict,
        "by_split": dict(sorted(by_split.items())),
        "by_dimension": {k: by_dimension[k] for k in sorted(by_dimension)},
        # ``d10`` = 头条档口径（与 ``cmd_agreement`` 打印等价）；分档在
        # ``d10_by_split``，旧口径（三档合计）在 ``d10_all_splits``。
        "d10": d10,
        "d10_by_split": {k: d10_by_split[k] for k in sorted(d10_by_split)},
        "d10_all_splits": {
            "n": sum(b["n"] for b in d10_by_split.values()),
            "agreed": sum(b["agreed"] for b in d10_by_split.values()),
        },
    }


def summarize_surfaces(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """快照的**采集面**读数（方案 20 §R3 的报表用）。

    只数 ``HARD_DIM``（一行一条）——面是**行级**事实，按维度数会把同一行
    数 N 遍，报表上看起来像「294 行里有 1200 个不可判定」。

    三态计数 ``comparable`` 是给读报表的人看的：``none`` 大就说明这一版
    **判不出跨面**（旧行没记面，方案 20 §R2 不回填），而不是「没有跨面问题」。
    """
    gold: dict[str, int] = {}
    pred: dict[str, int] = {}
    comparable = {"true": 0, "false": 0, "none": 0}
    rows = 0
    for entry in snapshot.get("entries", []):
        if entry.get("dimension") != HARD_DIM:
            continue
        rows += 1
        g = str(entry.get("gold_access_surface") or access_surface.SURFACE_UNRECORDED)
        p = str(entry.get("pred_access_surface") or access_surface.SURFACE_UNRECORDED)
        gold[g] = gold.get(g, 0) + 1
        pred[p] = pred.get(p, 0) + 1
        state = entry.get("surface_comparable")
        key = "true" if state is True else "false" if state is False else "none"
        comparable[key] += 1
    return {
        "rows": rows,
        "gold_by_surface": dict(sorted(gold.items())),
        "pred_by_surface": dict(sorted(pred.items())),
        "comparable": comparable,
    }


def verdicts_of(snapshot: Mapping[str, Any], dimension: str) -> list[str]:
    """某维度上逐条的 verdict（测试与下钻用）。"""
    return [
        str(e.get("verdict"))
        for e in snapshot.get("entries", [])
        if e.get("dimension") == dimension
    ]


def write_snapshot(path: pathlib.Path | str, snapshot: Mapping[str, Any]) -> pathlib.Path:
    """落 JSON（确定性：条目顺序 = 行序，不写时间戳，便于跨版本 diff）。"""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return p


def load_snapshot(path: pathlib.Path | str) -> dict[str, Any]:
    """读回快照；结构不对直接抛（回归判定不许吃半份文件）。"""
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "entries" not in data or "meta" not in data:
        raise ValueError(f"{path} 不是快照文件（缺 entries / meta）")
    return data


__all__ = [
    "DELTA_EQ_CLASS_MISMATCH",
    "DELTA_LABEL_MISMATCH",
    "DELTA_TIER_GAP",
    "DIFF_VERDICTS",
    "HARD_DIM",
    "PRED_VERSION_UNRECORDED",
    "PROVIDER_FROZEN_BRANCH",
    "PROVIDER_ROW_DIMS",
    "SNAPSHOT_SCHEMA_VERSION",
    "VERDICTS",
    "VERDICT_AGREE",
    "VERDICT_DETAIL_MISMATCH",
    "VERDICT_DISAGREE",
    "VERDICT_TIER_GAP",
    "VERDICT_UNGRADED",
    "build_snapshot",
    "iter_site_rows",
    "load_snapshot",
    "pred_provider_string",
    "provider_kind",
    "provider_version",
    "row_scorer_version",
    "summarize_snapshot",
    "summarize_surfaces",
    "verdicts_of",
    "write_snapshot",
]