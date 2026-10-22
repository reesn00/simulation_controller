"""分歧卡（方案 16 §3.1 / C-02）：从全量快照里筛出「有差异」的行。

契约（字段一组一组读，别当成散装 key）
--------------------------------------
::

    身份   disagreement_id / golden_id / golden_version / split / dimension / rubric_ref
    金标   gold_value / gold_anchor / gold_note
    预测   pred_value / pred_reasons / pred_evidence / pred_provider
    差异   delta_kind / tier_gap
    分诊   bucket / responsibility / triage_by / triage_at / triage_evidence
    抽样   stratum / sample_weight
    处置   action / change_ref / status / verify_run / net_delta
    动机链 change_kind / expected_effect

**分诊与处置字段一律留空**：本模块只派生，不裁定。空 = 未分诊（fail-closed），
与「分诊为 other」不是一回事——[§4](../../docs/设计方案/16-评估器与rubric双向校准回路方案.md)
的桶表没有 `other`。`rubric_*` / `gold_*` 桶的填写权只在人（D-C3）。

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

from trajectory_pipeline.evaluation.calibration.snapshot import DIFF_VERDICTS
from trajectory_pipeline.evaluation.golden.splits import SIDE_SPLITS

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
    cards: list[dict[str, Any]] = []
    for entry in snapshot.get("entries", []):
        if entry.get("verdict") not in DIFF_VERDICTS:
            continue
        if entry.get("split") in SIDE_SPLITS:
            continue
        golden_id = str(entry.get("golden_id") or "")
        dimension = str(entry.get("dimension") or "")
        cards.append(
            {
                "disagreement_id": disagreement_id(version, golden_id, dimension),
                "golden_id": golden_id,
                "golden_version": version,
                "split": entry.get("split"),
                "dimension": dimension,
                "rubric_ref": entry.get("rubric_ref") or "",
                "rubric_version": rubric_version,
                "gold_value": entry.get("gold_value"),
                "gold_anchor": entry.get("rubric_ref") or "",
                "gold_note": entry.get("gold_note") or "",
                "pred_value": entry.get("pred_value"),
                "pred_reasons": list(entry.get("pred_reasons") or []),
                "pred_evidence": entry.get("pred_evidence"),
                "pred_provider": entry.get("pred_provider") or "",
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
        )
    return cards


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
    "read_cards",
    "summarize_cards",
    "write_cards",
]