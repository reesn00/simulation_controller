"""**把软维度金标从派生副本收回真源**（2026-10-30）。

它解决的那个具体问题
--------------------
2026-10-30 排查发现：软维度金标的**判读结论落在了真源之外**——

- 真源 [`evaluation/golden/data/annotations.jsonl`](../../evaluation/golden/data/annotations.jsonl)
  里，91 个已判样本**只有 210 格数字**；人判「这一维判不了」的 **154 格
  （d2 66 / d4 43 / d6 45）键不在**（键缺 = 「没填」，与「判不了」是两件事）。
- 那 154 格的结论只活在两处：**gitignored 的 v8 工作表**与**冻结档 v9–v15 的
  gold 侧**（派生副本）。
- 后果：① 从真源读覆盖率会把「人判过但判不了」读成「没人看」；
  ② 下一次 `freeze --refresh-gold` 用真源覆盖 gold 侧 ⇒ **154 格判不了的
  裁定从基线上消失**（退化成「没填」）。

本脚本做两件事，都**只动 `dimension_scores` 那一栏**（标签一个字不碰）：

1. **复原 154 格**：从冻结档的 gold 侧取回那 91 行的**完整 4 键 dict**
   （含 null）——数值与库内现有的 210 格**逐格相同**，只是把缺掉的键补回来。
2. **给「不可判」的行显式记 null**：调参池里 40 行压根没有可判的内容
   （35 `missing_sample` + 2 `negative_pool_no_sample` + 3 占位样本）⇒
   四维**显式**记「判不了」（键在、值为 null），而不是「键缺」；
   回归集那 14 行无样本行（13 `missing_sample` + 1 `negative_pool_no_sample`）
   同口径（2026-10-30 第二轮，`--mark-splits calibration,regression`）。

**判据是「内容」，不是「有没有分数」**（2026-10-30 改）：早先的写法是
「冻结行没有 ``dimension_scores`` ⇒ 标 null」——那会把**刚补过分**的行
（回归集 46 条，2026-10-30 刚判好的）也当成「没有可判的内容」⇒ **抹掉真判好的分**。
改后：判据 = `status` ∈ {`missing_sample`, `negative_pool_no_sample`}，
或占位样本（没有 URL、没有 site_url、没有 `code`）；**且**真源里这一行
**还没有分数**（有 ⇒ 不覆盖）。

**哪几档能动**：`--mark-splits` 圈定（默认 `calibration`）——
**holdout 是封存档**（C-05：只在最终验收跑一次）⇒ **不碰**；
`regression` 可选（它的 14 行无样本行同样不可判，与「回归集首次补分」
那一轮一起落，见 §7 手册）。

为什么不直接手改 jsonl
----------------------
真源是金标的唯一落盘入口，**改动必须能复算、能审计**：本脚本只做三件事
——从冻结档读回、从工作表读回 note、生成补丁文件；真正的写入仍走
`builder annotate --dimensions-only`（按 `(golden_id, dimension)` 合并、
只动分数、默认不覆盖已填的分）。

署名与时刻
----------
- **91 行复原**：沿用原判读的署名与时刻（`reesn` / 2026-10-24T18:46:43+08:00）
  ——**复原不是改判**。
- **40 行显式 null**：本轮新增的记账，署名 `reesn`（owner 授权代签，
  与台账 M-34 同一口径——**执行者是 AI，独立性打折的限制照旧**），
  时刻取本轮。
- **14 行回归集无样本行**：同一口径（`reesn` / 2026-10-30）——它们与
  调参池那 40 行同形态（存档里压根没有这条样本）。
  ⚠️ 回归集那 **46 行有样本的**不走本脚本——它们的分是
  [`gold_fill_soft_dims_regression.py`](gold_fill_soft_dims_regression.py)
  判出来的（署名 `claude-ai`，AI 判读 ⇒ 独立性打折的限制照旧）。
- **14 行 `gold_defect` 修正样本**：它们的维度分是 2026-10-25 整行
  `annotate` 带进来的（该路径不写 `dimension_*` 三栏）⇒ **分数在、
  署名与理由都没有**。本脚本把 v8 工作表里的判读 note 补回（判读凭据
  见 [`gold_fill_soft_dims.py`](gold_fill_soft_dims.py)）。

用法**（必须在仓库根跑）**
-----------------------
```bash
uv run python trajectory_pipeline/research/probes/gold_dim_restore.py           # 干跑：报读数
uv run python trajectory_pipeline/research/probes/gold_dim_restore.py --write   # 写补丁文件
```
补丁文件落 `evaluation/golden/data/gold_dim_restore_2026-10-30.jsonl`，
导入命令：

```bash
uv run python -m trajectory_pipeline.evaluation.golden.builder annotate \
    --dimensions-only --input trajectory_pipeline/evaluation/golden/data/gold_dim_restore_2026-10-30.jsonl
```
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, REPO)

GOLDEN_DATA = os.path.join(REPO, "trajectory_pipeline", "evaluation", "golden", "data")
VERSIONS = ("calibration", "holdout", "regression")
DIMS = ("d2", "d4", "d5", "d6")

#: 冻结档版本：软维度金标从 v9 起进 gold 侧（`--refresh-gold`）。
SOURCE_VERSION = 15

#: 补丁文件落哪（真源同目录 ⇒ 进版本控制，审计时能看见「改了什么」）。
PATCH_PATH = os.path.join(GOLDEN_DATA, "gold_dim_restore_2026-10-30.jsonl")

#: 91 行复原：沿用原判读的署名与时刻——**复原不是改判**。
RESTORE_REVIEWED_BY = "reesn"
RESTORE_REVIEWED_AT = "2026-10-24T18:46:43+08:00"

#: 40 行显式 null：本轮新增的记账。owner 授权代签（与台账 M-34 同口径）。
NEW_REVIEWED_BY = "reesn"
NEW_REVIEWED_AT = "2026-10-30T09:09:21+08:00"


def _read_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _annotations() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in _read_jsonl(os.path.join(GOLDEN_DATA, "annotations.jsonl")):
        if int(r.get("round", 1)) == 1:
            out[str(r.get("golden_id") or "")] = r
    return out


def _frozen_rows() -> list[dict]:
    rows: list[dict] = []
    for split in VERSIONS:
        path = os.path.join(GOLDEN_DATA, f"v{SOURCE_VERSION}", f"{split}.jsonl")
        if not os.path.exists(path):
            continue
        for r in _read_jsonl(path):
            r = dict(r)
            r["__split__"] = split
            rows.append(r)
    return rows


def _worksheet_evidence() -> dict[str, dict]:
    """v8 工作表里每条样本的**判读凭据**（d2 那行的 gold_note 就是合并理由）。

    工作表在 `output/` 下（gitignored）⇒ 它的凭据是
    [`gold_fill_soft_dims.py`](gold_fill_soft_dims.py)（脚本重算得出来）。

    署名与时刻一并取回：**补凭据不是改判** ⇒ 沿用原判读那次的署名与时刻。
    """
    path = os.path.join(
        REPO, "trajectory_pipeline", "output", "pipeline", "calibration", f"v8", "gold_worksheet.jsonl"
    )
    if not os.path.exists(path):
        return {}
    out: dict[str, dict] = {}
    for r in _read_jsonl(path):
        if str(r.get("dimension")) != "d2":
            continue
        gid = str(r.get("golden_id") or "")
        if not gid:
            continue
        out[gid] = {
            "note": str(r.get("gold_note") or ""),
            "reviewed_by": str(r.get("reviewed_by") or ""),
            "reviewed_at": str(r.get("reviewed_at") or ""),
        }
    return out


def _why_unauditable(row: dict) -> str:
    """这行**压根没有可判的内容**吗？判不出 ⇒ 返回 ``None``（不标）。

    ⚠️ **判据是「内容」，不是「有没有分数」**（2026-10-30 改）：早先的写法是
    「冻结行没有 ``dimension_scores`` ⇒ 标 null」——那会把**刚补过分**的行
    （回归集 46 条）也当成「没有可判的内容」⇒ 抹掉真判好的分。改判据后：

    - ``status`` ∈ {``missing_sample``, ``negative_pool_no_sample``}
      ⇒ 存档里压根没有这条样本；
    - 占位样本 ⇒ 没有 URL、没有 site_url、没有 ``code``（evidence）
      ⇒ 不可人工验证。

    其余行（正常 scored）⇒ 判得了 ⇒ **不标**。
    """
    status = str(row.get("status") or "")
    if status == "missing_sample":
        return (
            "P1 存档里没有这条样本（status=missing_sample）"
            "⇒ 无观察可核 ⇒ 四维均无适用项 ⇒ 判不了（null，不记 5）"
        )
    if status == "negative_pool_no_sample":
        return (
            "负样本池行但没有对应样本（status=negative_pool_no_sample）"
            "⇒ 无观察可核 ⇒ 四维均无适用项 ⇒ 判不了（null，不记 5）"
        )
    if (
        not str(row.get("url") or "")
        and not str(row.get("site_url") or "")
        and not (row.get("code") or {})
    ):
        return (
            "占位样本：没有 URL、没有 evidence ⇒ 不可人工验证"
            "⇒ 四维均无适用项 ⇒ 判不了（null，不记 5）"
        )
    return None


def build(mark_splits: tuple[str, ...] = ("calibration",)) -> tuple[list[dict], dict]:
    """返回 ``(补丁行, 读数)``。补丁行是 ``{golden_id, dimension_scores, ...}``。"""
    ann = _annotations()
    frozen = _frozen_rows()
    evidence = _worksheet_evidence()

    patches: list[dict] = []
    stats: dict = collections.Counter()
    restored_na: collections.Counter = collections.Counter()

    def _evidence_only(gid: str) -> bool:
        """分数在、**凭据不在** ⇒ 产一条「只补凭据」的补丁（一个分数都不带）。

        来由：2026-10-25 的**整行** `annotate` 会把 `dimension_scores` 一起带进库，
        却不写 `dimension_note` / `dimension_reviewed_by` / `dimension_reviewed_at`
        ⇒ 分数在、判读理由不在 ⇒ **判读凭据只活在 gitignored 的工作表里**。

        已有理由的行**不覆盖**（凭据不重复写）。返回是否真产出了补丁。
        """
        row = ann.get(gid)
        if row is None:
            return False
        if str(row.get("dimension_note") or ""):
            return False
        ev = evidence.get(gid) or {}
        note = str(ev.get("note") or "")
        if not note:
            return False
        patches.append(
            {
                "golden_id": gid,
                "dimension_scores": {},  # **一个分数都不带** ⇒ merge 只写凭据
                "reviewed_by": str(ev.get("reviewed_by") or "") or RESTORE_REVIEWED_BY,
                "reviewed_at": str(ev.get("reviewed_at") or "") or RESTORE_REVIEWED_AT,
                "note": note,
            }
        )
        stats["rows_evidence_only"] += 1
        return True

    # —— 1) 复原：从冻结档 gold 侧取回完整 dict ——
    for row in frozen:
        gid = str(row.get("golden_id") or "")
        gold = (row.get("golden") or {}).get("dimension_scores")
        gold = gold if isinstance(gold, dict) else {}
        current = ann.get(gid, {}).get("dimension_scores")
        current = current if isinstance(current, dict) else {}
        missing = {d: v for d, v in gold.items() if d not in current}
        if not missing:
            # 分数齐了 ⇒ 只可能还缺**凭据** ⇒ 单独走「只补凭据」那条路。
            if not _evidence_only(gid):
                stats["rows_untouched"] += 1
            continue
        # 数值必须与库内已有的逐格相同（**只补键，不改分**）。
        for dim, value in current.items():
            if dim in gold and gold[dim] != value:
                raise SystemExit(
                    f"{gid} / {dim} 冻结档={gold[dim]!r} 与库内={value!r} 不一致"
                    "⇒ 冻结档与真源已经分叉，先人工核，**本脚本不替你挑一个**"
                )
        for dim, value in missing.items():
            if value is None:
                restored_na[dim] += 1
        patch: dict = {
            "golden_id": gid,
            "dimension_scores": dict(gold),
            "reviewed_by": RESTORE_REVIEWED_BY,
            "reviewed_at": RESTORE_REVIEWED_AT,
        }
        # 14 行 gold_defect 样本：分数在、署名与理由都不在 ⇒ 把工作表的 note 补回。
        if not str(ann.get(gid, {}).get("dimension_note") or "") and (evidence.get(gid) or {}).get("note"):
            patch["note"] = str(evidence[gid]["note"])
            stats["note_restored"] += 1
        patches.append(patch)
        stats["rows_restored"] += 1
        stats["cells_restored"] += len(missing)

    # —— 2) 显式 null：「压根没有可判内容」的行 ——
    for row in frozen:
        gid = str(row.get("golden_id") or "")
        gold = (row.get("golden") or {}).get("dimension_scores")
        if isinstance(gold, dict) and gold:
            continue
        if gid not in ann:
            continue
        current = ann[gid].get("dimension_scores")
        current = current if isinstance(current, dict) else {}
        if current:
            # 真源里已经有分数（含显式 null）⇒ **不覆盖**。
            # 这一条是 2026-10-30 补的：回归集那 46 条刚判好的分就在这儿，
            # 早先的判据（「冻结行没有 dimension_scores ⇒ 标 null」）会把它们
            # 连同刚补的分一起抹掉。
            stats["rows_already_scored"] += 1
            continue
        if str(row.get("__split__") or "") not in mark_splits:
            stats["rows_skipped_other_split"] += 1
            continue
        why = _why_unauditable(row)
        if not why:
            stats["rows_judgeable_skipped"] += 1
            continue
        patches.append(
            {
                "golden_id": gid,
                "dimension_scores": {d: None for d in DIMS},
                "reviewed_by": NEW_REVIEWED_BY,
                "reviewed_at": NEW_REVIEWED_AT,
                "note": why,
            }
        )
        stats["rows_marked_na"] += 1
        stats["cells_marked_na"] += len(DIMS)

    stats["restored_na_per_dim"] = dict(restored_na)
    return patches, dict(stats)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把软维度金标从派生副本收回真源")
    ap.add_argument("--write", action="store_true", help="真写补丁文件（默认干跑）")
    ap.add_argument(
        "--mark-splits",
        default="calibration",
        help="给「不可判」的行显式记 null 的档位（逗号分隔；**holdout 封存，别动**）",
    )
    ap.add_argument(
        "--out",
        default=PATCH_PATH,
        help="补丁文件落哪（**默认那份已被 2026-10-30 第一轮用掉**——"
        "第二轮（回归集 14 行）另落一个文件，别覆盖上一轮的审计痕迹）",
    )
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass

    patches, stats = build(
        tuple(s.strip() for s in str(args.mark_splits).split(",") if s.strip())
    )
    print("═══ 补丁读数 ═══")
    for key in sorted(stats):
        print("  %-22s %s" % (key, stats[key]))
    print("  补丁行数：%d" % len(patches))

    # 干跑也要把「导入后覆盖率会变成什么样」算出来——**读数先看，后写盘**。
    from trajectory_pipeline.evaluation.golden import dim_scores

    ann_rows = _read_jsonl(os.path.join(GOLDEN_DATA, "annotations.jsonl"))
    r1 = [r for r in ann_rows if int(r.get("round", 1)) == 1]
    before = dim_scores.coverage(r1, DIMS)
    fills = [
        {
            "golden_id": p["golden_id"],
            "dimension_scores": p["dimension_scores"],
            "reviewed_by": p["reviewed_by"],
            "reviewed_at": p["reviewed_at"],
            # note 必须带上：**只补凭据**的补丁靠它认（空分数 + 有理由）。
            "note": str(p.get("note") or ""),
        }
        for p in patches
    ]
    merged, mstats = dim_scores.merge_dimension_scores(ann_rows, fills)
    after = dim_scores.coverage(
        [r for r in merged if int(r.get("round", 1)) == 1], DIMS
    )
    print("═══ 覆盖率：导入前 → 导入后 ═══")
    for dim in DIMS:
        b, a = before[dim], after[dim]
        print(
            "  %-3s filled %3d→%3d · judged_na %3d→%3d · empty %3d→%3d"
            % (dim, b["filled"], a["filled"], b["judged_na"], a["judged_na"], b["empty"], a["empty"])
        )
    print(
        "  merge：changed %d · unchanged %d · judged_na %d"
        % (mstats["changed"], mstats["unchanged"], mstats["judged_na"])
    )

    if not args.write:
        print("\n（干跑；加 --write 才写补丁文件）")
        return 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for p in patches:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    print("→ 已写 %s（%d 行）" % (args.out, len(patches)))
    print("  下一步：")
    print(
        "  uv run python -m trajectory_pipeline.evaluation.golden.builder annotate "
        "--dimensions-only --input %s" % os.path.relpath(args.out, REPO).replace("\\", "/")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
