"""软维度金标的**填表盘**（方案 16 · d2 / d4 / d5 / d6 的 `ungraded` 清空）。

它补的是哪一段
--------------
回路的两半都建完了（读数、抽样、分诊、闸门），但**条款级读数只覆盖 D-10**：
v8 快照 1133 条里 **844 条 `ungraded`**，T-2 / T-3 测不成，抽样 S2 / S3 两层
总体为 0 —— 因为黄金集的 `dimension_scores` **一条都没填**。

填它要两个东西：一条**只改分数、不碰标签**的写入路径（
:mod:`...golden.dim_scores` + `annotate --dimensions-only`），和一张**人真的
填得下去的表**。本模块是后者。

为什么不能直接发个 CSV 让人自己找证据
------------------------------------
判 D-2（观察-动作一致性）/ D-4（播放组件验证）/ D-5（记录-排除决策）/
D-6（记录字段完整性）都要看**现场**：页面上有什么、走了哪几步、结论依据哪一句。
工作表里只给「分数 + 一句理由」，人就判不了——这不是人不肯判，是**表里没有
可判的东西**（C-06 踩过同一坑，补了两轮才把 `scene` 栏塞进来）。

所以每一格带四样只读上下文：

- ``anchors``：rubric 文档里该维 1–5 档的**原文**（现读文档，不手抄）；
- ``scene``：P1 存档的现场摘要（标题 / 每步动作与观察 / 正文预览）；
- ``pred_*``：当前评分器打了多少、理由原话、分支结论（**这是要判的对象，
  不是答案**——照抄 pred 等于把尺子交给被量的人）；
- ``existing``：这张卡上**已有**的金标（内容键预填，L-19 的教训：换一版重出
  表不能让人把同一格再填一遍）；
- ``record``：这条样本**记了什么**（站点 URL / 播放页 URL / 单元 URL）与
  **P1 存档在哪**（``archive`` 路径 + 在不在）。D-6 要核「记录字段完整性」，
  而片名与验证证据原文只在存档里（P2 不复制，见打分器的理由），
  少了这一栏人就得自己拼路径。

三条口径（写出来免得后人读错）
------------------------------
1. **只出「有样本」的行**：``status != scored`` 的行没有观察，人无从判分 ⇒
   不进表，但**计数单列**（``no_sample``）。把它混成「没人填」会让覆盖率
   永远上不去，而真相是那一条压根没有可看的东西。
2. **``null`` 是结论**：人判「这一条这一维判不了」就写 ``null``；**留空**才是
   「还没看」。两者进快照都是 `ungraded`，但覆盖率分得开。
3. **表是派生物**：重跑只重写本文件，**不写** `annotations.jsonl`
   （写入走 ``annotate --dimensions-only``，一个写入口）。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any, Iterable, Mapping, Sequence

from trajectory_pipeline.evaluation.calibration.provenance import DIM_FACES, FACE_CODE
from trajectory_pipeline.evaluation.calibration.worklist import (
    DEFAULT_CALIBRATION_ROOT,
    WorklistError,
    build_scenes,
    load_frozen_rows,
)
from trajectory_pipeline.evaluation.golden.builder import GOLDEN_ROOT, PIPELINE_ROOT, _load_jsonl
from trajectory_pipeline.evaluation.golden.dim_scores import coverage
from trajectory_pipeline.evaluation.golden.splits import SPLIT_CALIBRATION, SPLITS
from trajectory_pipeline.evaluation.scorers.rubric_version import clause_of, rubric_tier_text

#: 要填的维度 = **代码面**打分的软维度（D-2 / D-4 / D-5 / D-6）。
#: 从 :data:`~.provenance.DIM_FACES` 投影，**不手抄**：面表改了这里跟着改，
#: 抄一份就会在「新增一个代码维度」时漏掉它。
GOLD_DIMS: tuple[str, ...] = tuple(
    dim for dim, face in sorted(DIM_FACES.items()) if face == FACE_CODE
)

GOLD_WORKSHEET_FILE = "gold_worksheet.jsonl"


def anchors_of(dimension: str) -> dict[str, str]:
    """rubric 文档里该维的评分锚原文（读不到就返回空 dict，不编）。"""
    out: dict[str, str] = {}
    for tier in (5, 4, 3, 2, 1):
        try:
            out[str(tier)] = rubric_tier_text(dimension, tier)
        except (KeyError, ValueError):
            continue
    return out


def _ann_index(annotations: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {
        str(r.get("golden_id") or ""): r
        for r in annotations
        if int(r.get("round", 1)) == 1
    }


def _record_of(row: Mapping[str, Any], pipeline_root: pathlib.Path | str) -> dict[str, Any]:
    """这条样本记了什么 + 它的 P1 存档在哪。

    存档名与 :func:`...perception.evidence.locate_sample` 同一条口径
    （``{task_id}__{run_id}.json``）——**同一条规则抄两份就会漂**，
    所以这里只拼路径、不重新解析存档内容。路径尽可能给成相对仓库根的，
    方便直接粘进编辑器打开。
    """
    task_id = str(row.get("task_id") or "")
    run_id = str(row.get("run_id") or "")
    code = row.get("code") if isinstance(row.get("code"), Mapping) else {}
    path = pathlib.Path(pipeline_root) / f"{task_id}__{run_id}.json"
    try:
        shown = pathlib.Path(os.path.relpath(path))
        if str(shown).startswith(".."):
            shown = path
    except ValueError:  # 跨盘符，relpath 会抛
        shown = path
    return {
        "task_id": task_id,
        "run_id": run_id,
        "unit_url": str(row.get("url") or ""),
        "site_url": str(row.get("site_url") or ""),
        "play_page_url": str(row.get("play_page_url") or ""),
        "reached_play_page": bool(code.get("reached_play_page")),
        "fallback_used": bool(code.get("fallback_used")),
        "pred_source": str(code.get("source") or ""),
        "archive": str(shown),
        "archive_exists": path.exists(),
    }


def build_gold_worksheet(
    rows: Sequence[Mapping[str, Any]],
    *,
    dims: Sequence[str] = GOLD_DIMS,
    annotations: Iterable[Mapping[str, Any]] = (),
    scenes: Mapping[str, Mapping[str, Any]] | None = None,
    split: str = "",
    pipeline_root: pathlib.Path | str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """冻结行 → 一张**一维一行**的填表盘。返回 ``(行, 统计)``。

    行按 ``(golden_id, dimension)`` 排序（确定性：同一输入两次出表逐行相同）。
    """
    dims = list(dims)
    for dim in dims:
        if dim not in DIM_FACES:
            raise WorklistError(f"维度 {dim!r} 没登记在 DIM_FACES 里，不能出金标表")
    ann = _ann_index(annotations)
    scenes = scenes or {}
    root = pathlib.Path(pipeline_root) if pipeline_root else PIPELINE_ROOT
    anchor_cache = {dim: anchors_of(dim) for dim in dims}

    sheet: list[dict[str, Any]] = []
    no_sample: dict[str, int] = {dim: 0 for dim in dims}
    skipped_other_split = 0
    for row in rows:
        if row.get("source_kind") == "p2_sample":
            continue
        row_split = str(row.get("__split__") or "")
        if split and row_split != split:
            skipped_other_split += 1
            continue
        if str(row.get("status") or "") != "scored":
            # 没有样本 = 没有可看的东西。**不进表**，但要报数。
            for dim in dims:
                no_sample[dim] += 1
            continue
        gid = str(row.get("golden_id") or "")
        pred_dims = row.get("dimension_scores")
        pred_dims = pred_dims if isinstance(pred_dims, Mapping) else {}
        golden = row.get("golden")
        golden = golden if isinstance(golden, Mapping) else {}
        gold_dims = golden.get("dimension_scores")
        gold_dims = gold_dims if isinstance(gold_dims, Mapping) else {}
        detail = row.get("scores_detail")
        reasons_by_dim: dict[str, list[str]] = {}
        if isinstance(detail, Sequence) and not isinstance(detail, str):
            for item in detail:
                if not isinstance(item, Mapping):
                    continue
                d = str(item.get("dimension") or "")
                rs = item.get("reasons")
                if d and isinstance(rs, Sequence) and not isinstance(rs, str):
                    reasons_by_dim[d] = [str(r) for r in rs]
        scene = dict(scenes.get(gid) or {"found": False, "status": "not_read", "note": "没读现场"})
        record = _record_of(row, root)
        for dim in dims:
            golden_row = ann.get(gid) or {}
            existing_dims = golden_row.get("dimension_scores")
            existing_dims = existing_dims if isinstance(existing_dims, Mapping) else {}
            if dim in existing_dims:
                existing_score = existing_dims[dim]
                state = "filled" if existing_score is not None else "judged_na"
            elif dim in gold_dims:
                # 冻结行里的金标（不经 annotations 的历史路径）——同样算「已有」。
                existing_score = gold_dims[dim]
                state = "filled" if existing_score is not None else "judged_na"
            else:
                existing_score = None
                state = "to_fill"
            sheet.append(
                {
                    "golden_id": gid,
                    "split": row_split,
                    "dimension": dim,
                    "rubric_ref": clause_of(dim),
                    # —— 只读上下文 ——
                    "anchors": anchor_cache[dim],
                    "scene": scene,
                    "record": record,
                    "gold_label": str(golden.get("label") or ""),
                    "pred_value": pred_dims.get(dim),
                    "pred_status": str(row.get("status") or ""),
                    "pred_reasons": reasons_by_dim.get(dim, []),
                    "pred_branch": str((row.get("code") or {}).get("branch") or "")
                    if isinstance(row.get("code"), Mapping)
                    else "",
                    "pred_evidence": str((row.get("code") or {}).get("evidence") or "")
                    if isinstance(row.get("code"), Mapping)
                    else "",
                    "existing": {
                        "score": existing_score,
                        "reviewed_by": str(golden_row.get("dimension_reviewed_by") or ""),
                        "note": str(golden_row.get("dimension_note") or ""),
                    },
                    "state": state,
                    # —— 人填的三格 ——
                    "gold_score": existing_score if existing_score is not None else "",
                    "gold_note": str(golden_row.get("dimension_note") or ""),
                    "reviewed_by": str(golden_row.get("dimension_reviewed_by") or ""),
                    "reviewed_at": "",
                }
            )
    sheet.sort(key=lambda r: (str(r["golden_id"]), str(r["dimension"])))
    stats = summarize(sheet)
    stats["no_sample"] = no_sample
    stats["skipped_other_split"] = skipped_other_split
    return sheet, stats


def summarize(sheet: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """表的状态计数：``to_fill`` / ``filled`` / ``judged_na``（按维度分开）。

    另给 ``by_reviewer``：**已判格**（``filled`` + ``judged_na``）按署名人分桶。
    用途是**提交前最后一眼**——「这 364 格是人核过的还是 AI 判的」光看
    计数看不出来：全是 AI 名 ⇒ 人没核过就提交了。
    """
    per_dim = {dim: {"to_fill": 0, "filled": 0, "judged_na": 0} for dim in GOLD_DIMS}
    by_reviewer: dict[str, int] = {}
    for row in sheet:
        dim = str(row.get("dimension") or "")
        state = str(row.get("state") or "")
        if dim in per_dim and state in per_dim[dim]:
            per_dim[dim][state] += 1
        if state in ("filled", "judged_na"):
            who = str(row.get("reviewed_by") or "").strip() or "(未署名)"
            by_reviewer[who] = by_reviewer.get(who, 0) + 1
    totals = {"to_fill": 0, "filled": 0, "judged_na": 0}
    for counts in per_dim.values():
        for key in totals:
            totals[key] += counts[key]
    return {
        "rows": len(sheet),
        "per_dim": per_dim,
        "totals": totals,
        "by_reviewer": by_reviewer,
    }


def load_annotations(golden_root: pathlib.Path | str) -> list[dict[str, Any]]:
    return _load_jsonl(pathlib.Path(golden_root) / "annotations.jsonl")


def _print_stats(stats: Mapping[str, Any]) -> None:
    print(f"→ 工作表 {stats['rows']} 格（一维一行）；按维度：")
    # `无样本` 只由出表那一刻知道（表里没有这些行），`status` 读表算不出来——
    # 所以**没有这个键就不印**，印一个 `0` 会被读成「一条无样本都没有」。
    has_no_sample = isinstance(stats.get("no_sample"), Mapping)
    for dim, counts in stats["per_dim"].items():
        line = (
            f"    {clause_of(dim)}: 待填 {counts['to_fill']} · 已填 {counts['filled']}"
            f" · 人判判不了 {counts['judged_na']}"
        )
        if has_no_sample:
            line += f" · 无样本(不进表) {stats['no_sample'].get(dim, 0)}"
        print(line)
    totals = stats["totals"]
    print(
        f"  合计：待填 {totals['to_fill']} / 已填 {totals['filled']}"
        f" / 判不了 {totals['judged_na']}"
    )
    # 署名分桶：已判格是谁判的——提交前最后一眼（全是 AI 名 = 人没核过）。
    by_reviewer = stats.get("by_reviewer") or {}
    if by_reviewer:
        print("  署名（已判格）：")
        for who, n in sorted(by_reviewer.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {who}: {n} 格")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_worksheet(args: argparse.Namespace) -> int:
    rows = load_frozen_rows(args.golden_root, args.version)
    split = str(args.split or "")
    if split and split not in SPLITS:
        raise WorklistError(f"--split {split!r} 不是三档之一：{list(SPLITS)}")
    sheet_rows = [r for r in rows if not split or str(r.get("__split__")) == split]
    scenes = build_scenes(
        [{"golden_id": str(r.get("golden_id") or ""), "dimension": GOLD_DIMS[0]} for r in sheet_rows],
        sheet_rows,
        p2_root=args.pipeline_root,
    )
    sheet, stats = build_gold_worksheet(
        rows,
        annotations=load_annotations(args.golden_root),
        scenes=scenes,
        split=split,
        pipeline_root=args.pipeline_root,
    )
    if not sheet:
        raise WorklistError(
            "这一档没有一条「有样本」的站点级行：工作表会是空的。"
            "先确认版本号与档位（无样本的行不进表，计数见 no_sample）"
        )
    stats["scenes_located"] = sum(1 for s in scenes.values() if s.get("found"))
    _print_stats(stats)
    print(
        f"  现场：{stats['scenes_located']}/{len(scenes)} 个 golden_id 定位到存档"
        "（`scene.found=false` 的行要先确认存档在不在）"
    )
    out = (
        pathlib.Path(args.path)
        if args.path
        else pathlib.Path(args.out) / f"v{args.version}" / GOLD_WORKSHEET_FILE
    )
    if out.exists() and not args.force:
        raise WorklistError(
            f"{out} 已存在；人填到一半的表不能被重算冲掉。"
            "确认要重出加 --force（已填的格子按内容键预填，不会丢）"
        )
    if args.dry_run:
        print(f"（--dry-run）不落盘；目标 {out}")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in sheet:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"→ 已写 {out}")
    print(
        "  填法：`gold_score` 填 1–5 的整数；判不了写 null；**没看就留空**。"
        "填完跑 `annotate --dimensions-only --input <这张表>`（label 不会被碰）"
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    path = (
        pathlib.Path(args.path)
        if args.path
        else pathlib.Path(args.out) / f"v{args.version}" / GOLD_WORKSHEET_FILE
    )
    if not path.exists():
        raise WorklistError(f"{path} 不存在：先跑 `goldwork worksheet --version {args.version}`")
    sheet = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    _print_stats(summarize(sheet))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="goldwork",
        description=(
            "软维度金标的填表盘（方案 16）：出 d2/d4/d5/d6 的一维一行工作表"
            "（带 rubric 锚点原文 + 现场），人填完后交给 "
            "`annotate --dimensions-only` 导入。**本命令不写 annotations.jsonl**"
        ),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--version", type=int, required=True, help="冻结版本号")
        p.add_argument("--golden-root", default=str(GOLDEN_ROOT), help="黄金集数据根")
        p.add_argument(
            "--out",
            default=str(DEFAULT_CALIBRATION_ROOT),
            help=f"派生数据根（缺省 {DEFAULT_CALIBRATION_ROOT}；与 worklist 的 --out 同义）",
        )

    p_ws = sub.add_parser("worksheet", help="出一张金标填表盘")
    _common(p_ws)
    p_ws.add_argument(
        "--split",
        default=SPLIT_CALIBRATION,
        help=f"哪一档（缺省 {SPLIT_CALIBRATION}；传空串 = 三档全出）",
    )
    p_ws.add_argument(
        "--pipeline-root", default=str(PIPELINE_ROOT), help="P1 存档根（scene 栏从这儿读）"
    )
    p_ws.add_argument(
        "--path", default="", help="显式指定表路径（缺省 {--out}/v{N}/gold_worksheet.jsonl）"
    )
    p_ws.add_argument("--force", action="store_true", help="已存在也重出")
    p_ws.add_argument("--dry-run", action="store_true", help="只报计数不落盘")
    p_ws.set_defaults(func=_cmd_worksheet)

    p_st = sub.add_parser("status", help="看一张表的填写进度")
    _common(p_st)
    p_st.add_argument(
        "--path", default="", help="表路径（缺省 {--out}/v{N}/gold_worksheet.jsonl）"
    )
    p_st.set_defaults(func=_cmd_status)

    args = ap.parse_args(argv)
    try:
        return int(args.func(args))
    except (WorklistError, OSError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2


__all__ = [
    "GOLD_DIMS",
    "GOLD_WORKSHEET_FILE",
    "anchors_of",
    "build_gold_worksheet",
    "summarize",
]


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())