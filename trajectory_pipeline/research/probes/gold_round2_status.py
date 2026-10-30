"""**round=2 那 12 行**：重测队列说「pending」，标注里却有 round=2 的标签——查来源。

问三件事（2026-10-30 排「人处理事项」时量出来的）：

1. **队列说什么**：`d9_retest_queue.jsonl` 里那 12 行的 ``round_2_status``
   与 ``round_2_label``（今天全是 ``pending`` / 空）；
2. **标注里有什么**：`annotations.jsonl` 里 round=2 的行（标签 / 署名 / 时刻 /
   ``interval_days``）；
3. **盘上有没有支持它的证据**：那条 URL 在 ``output/pipeline/`` 的存档里，
   有没有哪个存档的 ``outcomes[].branch`` **等于** round=2 那个标签。

第 3 条是关键：**「标签写了」不等于「重测跑过」**。找不到支持它的存档 ⇒
那 12 行的 round=2 标签**在盘上查无实据** ⇒ 要人定性（是「跑过但存档被清」，
还是「压根没跑、按 round-1 重判的」）。

跑法（**必须在仓库根跑**）::

    python trajectory_pipeline/research/probes/gold_round2_status.py

⚠️ **只报读数，不下结论**：两处对不上是事实，「所以标签是假的」是推断——
真要定性得看那次重测的存档（在不在、在哪台机器上）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
GOLD_ROOT = REPO / "evaluation" / "golden" / "data"
#: 产物目录在**包内**（``trajectory_pipeline/output/pipeline/``）——不是仓库根。
PIPELINE = REPO / "output" / "pipeline"

QUEUE_FILE = "d9_retest_queue.jsonl"
ANNOTATIONS_FILE = "annotations.jsonl"


def _load_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _archives_of_task(task_id: str) -> list[pathlib.Path]:
    if not task_id:
        return []
    return sorted(
        p
        for p in PIPELINE.glob(f"{task_id}__*.json")
        if not p.name.endswith(".reviewed.json")
    )


def _branches_for_url(task_id: str, url: str) -> list[tuple[str, str]]:
    """该 task 下所有存档里，这条 URL 的 ``(存档名, branch)``。"""
    out: list[tuple[str, str]] = []
    for path in _archives_of_task(task_id):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for outcome in data.get("outcomes") or []:
            if str(outcome.get("url") or "") == url:
                out.append((path.name, str(outcome.get("branch") or "")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="round=2 那 12 行：队列状态 · 标注内容 · 盘上有没有支持它的存档"
    )
    ap.add_argument(
        "--golden-root", default=str(GOLD_ROOT), help="黄金集数据根（缺省包内 data/）"
    )
    args = ap.parse_args()
    root = pathlib.Path(args.golden_root)

    queue = {str(r.get("golden_id") or ""): r for r in _load_jsonl(root / QUEUE_FILE)}
    ann_rows = _load_jsonl(root / ANNOTATIONS_FILE)
    r2 = {
        str(r.get("golden_id") or ""): r
        for r in ann_rows
        if int(r.get("round", 1)) == 2
    }

    print(f"队列 {QUEUE_FILE}：{len(queue)} 行 · 标注里 round=2：{len(r2)} 行")
    other_run = 0
    same_run = 0
    none = 0
    for gid in sorted(r2):
        row = r2[gid]
        label = str(row.get("label") or "")
        qq = queue.get(gid) or {}
        url = str(qq.get("url") or "")
        parts = gid.split("__")
        task = parts[0] if parts else ""
        # ``golden_id`` = ``{task_id}__{run_id}__{unit}`` ⇒ 第二段就是**本行的 run**
        # （round=1 那次采集的 run）⇒ 同名存档是**初测**证据，**不是重测**证据。
        round1_run = parts[1] if len(parts) > 1 else ""
        hits = _branches_for_url(task, url)
        match_other = [
            name for name, branch in hits if branch == label and round1_run not in name
        ]
        match_same = [
            name for name, branch in hits if branch == label and round1_run in name
        ]
        if match_other:
            other_run += 1
        elif match_same:
            same_run += 1
        else:
            none += 1
        print(f"\n  {gid}")
        print(
            f"    队列   round_1={qq.get('round_1_label')} · "
            f"round_2_label={qq.get('round_2_label')!r} · "
            f"status={qq.get('round_2_status')!r} · "
            f"scheduled={qq.get('scheduled_retest_at')}"
        )
        print(
            f"    标注   label={label!r} · by={row.get('reviewed_by')!r} · "
            f"at={row.get('reviewed_at')} · interval={row.get('interval_days')} · "
            f"dims={row.get('dimension_scores')!r}"
        )
        print(
            f"    盘上   该 URL 的存档分支：{hits or '（无任何存档命中）'}"
            f"\n           ⇒ **别的 run** 支持 round=2 标签：{match_other or '（没有）'}"
            f"\n           ⇒ 只有初测那个 run（{round1_run}）支持：{match_same or '（没有）'}"
        )

    print(
        f"\n合计：round=2 {len(r2)} 行 · "
        f"**有「别的 run」支持（可能是重测存档）{other_run} 行** · "
        f"只有初测 run 支持（**不算重测证据**）{same_run} 行 · "
        f"**没有任何存档支持 {none} 行**"
    )
    print(
        "⇒ **标签写了 ≠ 重测跑过**；定性要人拍（台账 M-55 / 手册 §9.5）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
