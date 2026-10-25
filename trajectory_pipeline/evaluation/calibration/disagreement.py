"""分歧卡（方案 16 §3.1 / C-02）：从全量快照里筛出「有差异」的行。

契约（字段一组一组读，别当成散装 key）
--------------------------------------
::

    身份   disagreement_id / golden_id / golden_version / split / dimension / rubric_ref
    金标   gold_value / gold_anchor / gold_note
    预测   pred_value / pred_reasons / pred_evidence / pred_provider
    出处   pred_provider_version / pred_prompt_version
           pred_face / pred_face_version / pred_face_status / current_face_version
    可比性 pred_comparability（三态：True/False/None）/ pred_comparability_reason
    差异   delta_kind / tier_gap
    分诊   bucket / responsibility / triage_by / triage_at / triage_evidence
    抽样   stratum / sample_weight
    处置   action / change_ref / status / verify_run / net_delta
    动机链 change_kind / expected_effect

**分诊与处置字段一律留空**：本模块只派生，不裁定。空 = 未分诊（fail-closed），
与「分诊为 other」不是一回事——[§4](../../docs/设计方案/16-评估器与rubric双向校准回路方案.md)
的桶表没有 `other`。`rubric_*` / `gold_*` 桶的填写权只在人（D-C3）。

面感知（方案 16 C-11 补齐）
--------------------------
「这批 pred 是哪版评分器打的」**不是一个问题，是三个**——取决于 pred 出自
哪一面：代码面（D-2/D-4/D-5/D-6）/ prompt 面（D-3/D-9）/ 采集侧（D-10 的
``code.branch``）。三栏（``pred_face`` / ``pred_face_version`` /
``pred_face_status``）把这件事写在卡上，比对由
:func:`pred_comparability` 按面做，理由由
:func:`pred_comparability_reason` 给人看。

⚠️ **结论与理由同卡**（``pred_comparability`` / ``pred_comparability_reason``
两栏）——**只定义函数不写进产物等于没答**：答案只活在「会调用它的地方」，
而没有那种地方。这两条栏就是那个「会调用它的地方」：卡是给人看的产物，
读的人不必先知道有这两个函数。

只读，不回写冻结档
------------------
派生自冻结档的派生数据落 ``output/pipeline/calibration/v{N}/``（D-C1）。
人填的分诊档另存 ``evaluation/golden/data/triage/``——**分诊结果是资产，
派生卡是可再生产物**，两者不能放同一个目录。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Iterable, Mapping, Sequence

from trajectory_pipeline.evaluation.calibration.provenance import (
    FACE_CODE,
    FACE_COLLECTION,
    FACE_LLM,
    STATUS_DIVERGED,
    STATUS_UNRECORDED,
    face_of_dimension,
    face_version_of,
)
from trajectory_pipeline.evaluation.calibration.snapshot import (
    DIFF_VERDICTS,
    PRED_VERSION_UNRECORDED,
    provider_version,
)
from trajectory_pipeline.evaluation.golden.splits import SIDE_SPLITS
from trajectory_pipeline.evaluation.scorers.rubric_version import llm_part

#: 卡片状态机的初始态（§3.3）。只有本模块会写这个值——它永远从 ``open`` 起。
STATUS_OPEN = "open"


def disagreement_id(golden_version: int | str, golden_id: str, dimension: str) -> str:
    """稳定 id：``v6:E006__2729220b__e9b9542b:d10``。

    用「版本 + 行 + 维度」而不是自增序号：序号在同一版本重算时会漂，
    而 ``change_ref`` 与 rubric 变更记录要长期指回同一条卡。
    """
    return f"v{golden_version}:{golden_id}:{dimension}"


def cards_from_snapshot(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """快照 → 分歧卡列表（只含 :data:`DIFF_VERDICTS` 三态）。

    ``agree`` 与 ``ungraded`` 不出卡：前者没差异，后者是「证据不足无法判」，
    它要进的是采集层的账（`degraded` / 裁剪），不是分诊桌。

    **旁置档不出卡**（``SIDE_SPLITS`` = 回归集 + 持有集）。分诊的下一步是改
    rubric / 改评估器，那就是调参——拿回归集调参等于毁掉回归门（D-C8），拿
    持有集调参等于烧掉封存（D-C5）。两者的分歧另有归宿：

    - 回归集 → ``verify``（C-08）判 ``fixed`` / ``regressed`` / ``unchanged``；
    - 持有集 → 最终验收跑一次，跑一次 ``holdout_runs`` +1。

    快照本身三档全收（审计与回归判定都要它），卡只从调参池出。
    """
    meta = snapshot.get("meta") or {}
    version = meta.get("golden_version", 0)
    rubric_version = str(meta.get("rubric_version") or "")
    evaluator_version = str(meta.get("evaluator_version") or "")
    code_version = str(meta.get("code_version") or "")
    cards: list[dict[str, Any]] = []
    for entry in snapshot.get("entries", []):
        if entry.get("verdict") not in DIFF_VERDICTS:
            continue
        if entry.get("split") in SIDE_SPLITS:
            continue
        golden_id = str(entry.get("golden_id") or "")
        dimension = str(entry.get("dimension") or "")
        provider = str(entry.get("pred_provider") or "")
        pred_version = provider_version(provider)
        # 面：条目里带了就用带了的；没带就**按维度投影**——面本来就是维度的
        # 函数（:func:`~...provenance.face_of_dimension`），投影不是猜。
        face = str(entry.get("pred_face") or "") or face_of_dimension(dimension)
        # 该面的版本：条目里带了就用带了的；没带就从出处串里取该面那段。
        face_version = (
            str(entry.get("pred_face_version") or "")
            or face_version_of(pred_version, face)
        )
        face_status = (
            str(entry.get("pred_face_status") or "")
            or (
                "recorded"
                if pred_version != PRED_VERSION_UNRECORDED
                else STATUS_UNRECORDED
            )
        )
        card = {
            "disagreement_id": disagreement_id(version, golden_id, dimension),
            "golden_id": golden_id,
            "golden_version": version,
            "split": entry.get("split"),
            "dimension": dimension,
            "rubric_ref": entry.get("rubric_ref") or "",
            "rubric_version": rubric_version,
            # 尺子面指纹（方案 16 C-11）：这张卡出自哪一版 prompt + 锚点。
            "evaluator_version": evaluator_version,
            # 代码面指纹：软维度（D-2/D-4/D-5/D-6）的可比性比的是它。
            "code_version": code_version,
            "gold_value": entry.get("gold_value"),
            "gold_anchor": entry.get("rubric_ref") or "",
            "gold_note": entry.get("gold_note") or "",
            "pred_value": entry.get("pred_value"),
            "pred_reasons": list(entry.get("pred_reasons") or []),
            "pred_evidence": entry.get("pred_evidence"),
            # 出处**带版本**：``<kind>@<version>``。版本没记到时是
            # ``unrecorded``，不是空串——见 snapshot.pred_provider_string。
            "pred_provider": provider,
            "pred_provider_version": pred_version,
            # pred 侧的 prompt 面版本（评分面指纹的 llm 段）——
            # 「旧 prompt 还是新 prompt 判的」要答的就是它。
            "pred_prompt_version": llm_part(pred_version),
            # ↓ 面感知三栏（C-11 补齐）：pred 出自**哪一面**、那一面
            # **是哪版**、那一面的版本**怎么定的**。
            "pred_face": face,
            "pred_face_version": face_version,
            "pred_face_status": face_status,
            # 本卡比的是**哪一面**的当前版本——按面取，不按 prompt 面取。
            "current_face_version": _current_face_version(
                face, code_version=code_version,
                llm_version=evaluator_version,
            ),
            "verdict": entry.get("verdict"),
            "delta_kind": entry.get("delta_kind"),
            "tier_gap": entry.get("tier_gap"),
            # ↓ 以下全部留空：本模块不裁定（见模块 docstring）
            "bucket": None,
            "responsibility": None,
            "triage_by": None,
            "triage_at": None,
            "triage_evidence": None,
            "stratum": None,
            "sample_weight": None,
            "action": None,
            "change_ref": None,
            "status": STATUS_OPEN,
            "verify_run": None,
            "net_delta": None,
            "change_kind": None,
            "expected_effect": None,
        }
        # ↓ 可比性三态 + 人话理由**写在卡上**（C-11 补齐的接线）：
        # 只定义函数不写进产物 ⇒ 答案只活在「会调用它的地方」，而**没有
        # 那种地方** ⇒ 等于没答。卡是给人看的产物 ⇒ 结论跟理由同卡，
        # 读的人不必先知道有这两个函数。
        card["pred_comparability"] = pred_comparability(card)
        card["pred_comparability_reason"] = pred_comparability_reason(card)
        cards.append(card)
    return cards


def _current_face_version(
    face: str, *, code_version: str, llm_version: str,
) -> str:
    """该面的**当前**版本（比 pred 那一面）。

    采集侧**没有**当前版本可比——判定侧从不是评分器，采集侧版本从未被
    指纹化 ⇒ 空串（⇒ 可比性判不出来，**不猜**）。
    """
    if face == FACE_CODE:
        return code_version
    if face == FACE_LLM:
        return llm_version
    return ""


def pred_comparability(card: Mapping[str, Any]) -> bool | None:
    """这张卡的 pred 与卡上记的尺子面**是否同一版判的**（C-11 补齐：面感知）。

    返回三态：

    - ``True`` —— **同一版**（可比）；
    - ``False`` —— **不是同一版**（不可比：拿它跟新读数对账会得到假的
      「评分器变差了」）；
    - ``None`` —— **判不出来 / 不适用**（那一面的版本没记到，或那一面与
      评分面无关）。

    ⚠️ **比的是「那一面」，不是一律比 prompt**：
    D-2/D-4/D-5/D-6 是**代码**打的 ⇒ 比代码面版本；D-3/D-9 是**LLM** 打的
    ⇒ 比 prompt 面版本；D-10 的 pred 是**采集侧**分支 ⇒ **不适用**，
    永远 ``None``（判定侧版本从未记录，且重采会换 run_id，不是同一总体）。

    ``None`` 不是「一样」也不是「不一样」。**不猜**：猜一个「一样」会让
    「不可比」这件事消失在报表里。要看**为什么**是这一态，调用
    :func:`pred_comparability_reason`。
    """
    face = str(card.get("pred_face") or "")
    if face == FACE_COLLECTION:
        return None
    if str(card.get("pred_face_status") or "") == STATUS_DIVERGED:
        # **已知不可比**：重算证明当前该面复现不出这批读数。
        return False
    pred = str(card.get("pred_face_version") or "")
    cur = str(card.get("current_face_version") or "")
    if pred in ("", PRED_VERSION_UNRECORDED) or cur == "":
        return None
    return pred == cur


def pred_comparability_reason(card: Mapping[str, Any]) -> str:
    """可比性结论的**人话理由**（为什么是 True / False / None）。

    没有理由的三态答案是**半份答案**——读的人看不出「不可比」是「已知
    不可比」还是「压根没记」。这一栏把两者分开。
    """
    face = str(card.get("pred_face") or "")
    status = str(card.get("pred_face_status") or "")
    pred = str(card.get("pred_face_version") or "")
    cur = str(card.get("current_face_version") or "")
    if face == FACE_COLLECTION:
        return (
            "pred 是采集时写下的 code.branch，出自**采集侧**；判定侧版本从未"
            "记录 ⇒ 与评分面无关，比不了（不是「不可判定」而是**不适用**）"
        )
    if status == STATUS_DIVERGED:
        return (
            "重算证明**当前这一面复现不出**本批读数 ⇒ **已知不可比**"
            "（当时跑的是哪份源码没记，但「不是同一版」是确定的）"
        )
    if pred in ("", PRED_VERSION_UNRECORDED):
        return f"{face} 面的版本**没记到** ⇒ 比不了（不拿当前指纹顶替）"
    if cur == "":
        return f"{face} 面的**当前**版本没记到 ⇒ 比不了"
    if pred == cur:
        return f"{face} 面同一版（{pred}）⇒ 可比"
    return f"{face} 面**不同版**（pred={pred} / 当前={cur}）⇒ 不可比"


def summarize_cards(cards: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """卡的计数视图：按 ``delta_kind`` / 维度 / split 三个切面。"""
    by_delta: dict[str, int] = {}
    by_dim: dict[str, int] = {}
    by_split: dict[str, int] = {}
    for card in cards:
        delta = str(card.get("delta_kind") or "unknown")
        by_delta[delta] = by_delta.get(delta, 0) + 1
        dim = str(card.get("dimension") or "")
        by_dim[dim] = by_dim.get(dim, 0) + 1
        split = str(card.get("split") or "")
        by_split[split] = by_split.get(split, 0) + 1
    return {
        "n": len(cards),
        "by_delta_kind": dict(sorted(by_delta.items())),
        "by_dimension": dict(sorted(by_dim.items())),
        "by_split": dict(sorted(by_split.items())),
        "untriaged": sum(1 for c in cards if not c.get("bucket")),
    }


def write_cards(path: pathlib.Path | str, cards: Iterable[Mapping[str, Any]]) -> pathlib.Path:
    """落 JSONL（一行一卡；顺序 = 快照顺序，重算可复现）。"""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        for card in cards:
            fh.write(json.dumps(card, ensure_ascii=False) + "\n")
    return p


def read_cards(path: pathlib.Path | str) -> list[dict[str, Any]]:
    p = pathlib.Path(path)
    if not p.exists():
        return []
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


__all__ = [
    "STATUS_OPEN",
    "cards_from_snapshot",
    "disagreement_id",
    "pred_comparability",
    "pred_comparability_reason",
    "read_cards",
    "summarize_cards",
    "write_cards",
]