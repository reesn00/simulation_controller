"""黄金集构建 / 标注 / 冻结 / 一致性测量的入口脚本（模块 6 · evaluation.golden.builder）。

设计来源：``docs/设计方案/00-总体方案.md`` §5.2（黄金集规格与一致性测量 D9）、
§5.3（代码层一致率 D10 = 口径 C）、§4（数据契约）、``docs/05-现状与验收.md``
的「未建的模块」节、``docs/设计方案/02-rubric与代码对齐.md`` §2.9。

为什么是独立 ``python -m`` 入口：
--------------------------------
与 ``evaluation/cli.py`` 同理由：黄金集是模块 6 的落点，executor 不应依赖验收方；
且构建 / 标注 / 冻结 / 测量这组操作有自己的一批子命令，不塞进 scorer CLI。

一次完整构建（三步）：:

    python -m trajectory_pipeline.evaluation.golden.builder candidates
    python -m trajectory_pipeline.evaluation.golden.builder annotate --input labels.jsonl
    python -m trajectory_pipeline.evaluation.golden.builder freeze --version 1

按需测量::

    python -m trajectory_pipeline.evaluation.golden.builder consistency   # D9 test-retest
    python -m trajectory_pipeline.evaluation.golden.builder agreement --version 1   # D10

产出目录（黄金集与训练集 ``samples/`` / ``views/`` 不同命名空间，D8）::

    evaluation/golden/data/    # 黄金集独立于 output/（D8 修订：纳入版本控制）
      candidates.jsonl      # 候选样本（负样本池 + P1 outcomes + P2 样例，不虚构）
      annotations.jsonl     # 人工标注（round 1 = 初测，round 2 = retest，供 D9）
      v<N>/                 # 冻结版本：manifest.json + calibration.jsonl + holdout.jsonl
      versions.jsonl        # 冻结版本索引

黄金集纪律（不虚构）：
---------------------
候选只来自真实原料：负样本池 ``output/pipeline/negative.jsonl``、P1 存档
``output/pipeline/<task_id>__<run_id>.json`` 的 ``outcomes``、P2 样例
``output/pipeline/samples/*.json``。脚本**不制造**样本；条数不足 100–200
时如实报告缺口，等人把真实素材跑出来再补，不做任何编造。

D9 三条边界（对外汇报必须写明，见总体方案 §5.2）：
  1. 这是 test-retest 自相关，不是评分者间一致性；
  2. 间隔 T 必须固定并记录（建议 14 天）；
  3. 单人的系统性偏差（整体偏严/偏松）测不出，只能测出稳不稳。
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import pathlib
import random
import sys
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

#: 管线产物根（负样本池 / P1 存档 / P2 样例）：相对本文件上溯到 trajectory_pipeline/，
#: 不依赖 cwd。output/ 整树在 .gitignore 内（日常产物不跟踪）。
PIPELINE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "output" / "pipeline"
#: 黄金集数据根：与日常 output/ 分离（D8 修订：黄金集随代码纳入版本控制）。
GOLDEN_ROOT = pathlib.Path(__file__).resolve().parent / "data"

CANDIDATES_FILE = "candidates.jsonl"
ANNOTATIONS_FILE = "annotations.jsonl"
VERSIONS_INDEX = "versions.jsonl"
SCHEMA_VERSION = "1"

#: 设计规格（总体方案 §5.2 / §5.3）
TARGET_SIZE = (100, 200)  # 黄金集起步规模
CALIBRATION_RANGE = (0.60, 0.70)  # 校准集占比
HOLDOUT_RANGE = (0.30, 0.40)  # 持有集占比
SPLIT_TARGET = 0.65  # 校准目标占比（65%，落在两区间的中点）
SPLIT_PP_TOLERANCE = 0.05  # 实际占比离目标 65% 的允许偏差（"差距 ≤5pp"）
RETEST_FRACTION = (0.20, 0.30)  # D9 重测子集占比
RETEST_INTERVAL_DAYS = 14  # D9 建议间隔 T
D10_THRESHOLD = 0.90  # 代码层与黄金集一致率上岗线（口径 C = 语义等价类一致）
#: 标签体系版本：1 = 11 类（10 分支 + success）；2 = 15 类
#: （BRANCH_LABELS 14 key = 13 活跃 + deprecated 合并态 + success）。
#: freeze 写入 manifest，跨版本对比 D10 前先核对（对齐方案 §4.3 / §5.2）。
LABEL_SCHEMA_VERSION = "2"
NEGATIVE_BRANCH_FIELDS = (  # 负样本池可直接透传的字段
    "task_id",
    "run_id",
    "persona_id",
    "url",
    "branch",
    "branch_label",
    "evidence",
    "question",
    "source",
    "fallback_used",
    "reached_play_page",
    "content_tier",
    "popularity",
    "genre",
)

#: 成功样本的黄金标签（代码 branch=None 与之对齐）
SUCCESS_LABEL = "success"

#: 每失败模式至少条数（总体方案 §5.2 锚点来源②：每个失败模式至少 3 条）
MIN_PER_BRANCH = 3


class GoldenError(Exception):
    """黄金集操作类错误：校验失败、版本冲突等，报错后回到错误码 2。"""


# --------------------------------------------------------------------------
# 通用 IO（原子写、默认不覆盖）
# --------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise GoldenError(
                    f"{path} 第 {len(rows) + 1} 行不是合法 JSON: {exc}"
                ) from exc
    return rows


def _dump_jsonl(
    path: pathlib.Path, rows: Iterable[Mapping[str, Any]], force: bool = False
) -> None:
    if path.exists() and not force:
        raise GoldenError(
            f"{path} 已存在；请显式 --force 覆盖，或换输出位置（黄金集不静默覆盖）"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        raise GoldenError(f"{path} 不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------------
# D10 / D9 需要的统计量（不引第三方依赖，纯标准库）
# --------------------------------------------------------------------------


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float | None:
    """Cohen's kappa。类别一致时 1.0；随机水平 0；完全不一致为负。

    返回 None 表示无法计算（空样本或同类别无方差）。"""
    n = len(a)
    if n == 0 or len(a) != len(b):
        return None
    cats = sorted(set(a) | set(b))
    if len(cats) < 2:
        return None
    idx = {c: i for i, c in enumerate(cats)}
    m = [[0] * len(cats) for _ in cats]
    for x, y in zip(a, b):
        m[idx[x]][idx[y]] += 1
    po = sum(m[i][i] for i in range(len(cats))) / n
    pe = 0.0
    for i in range(len(cats)):
        row = sum(m[i])
        col = sum(m[j][i] for j in range(len(cats)))
        pe += row * col
    pe /= n * n
    if pe >= 1.0:
        return None
    return (po - pe) / (1.0 - pe)


def linear_weighted_kappa(
    a: Sequence[float], b: Sequence[float], lo: float, hi: float
) -> float | None:
    """线性加权 kappa（软维度，如 rubric 1–5 分档）。

    a / b 为两轮打在同一尺度 [lo, hi] 上的分数序列，一一对应。"""
    n = len(a)
    if n == 0 or len(a) != len(b):
        return None
    scale = hi - lo
    if scale <= 0:
        return None
    cats = sorted(set(a) | set(b))
    if len(cats) < 2:
        return None
    idx = {c: i for i, c in enumerate(cats)}
    m = [[0] * len(cats) for _ in cats]
    for x, y in zip(a, b):
        m[idx[x]][idx[y]] += 1
    w = [[1.0 - abs(x - y) / scale for y in cats] for x in cats]
    po = sum(w[i][j] * m[i][j] for i in range(len(cats)) for j in range(len(cats))) / n
    pe = 0.0
    for i in range(len(cats)):
        row = sum(m[i])
        for j in range(len(cats)):
            col = sum(m[k][j] for k in range(len(cats)))
            pe += w[i][j] * row * col
    pe /= n * n
    if pe >= 1.0:
        return None
    return (po - pe) / (1.0 - pe)


def classification_report(
    y_true: Sequence[str], y_pred: Sequence[str]
) -> dict[str, Any]:
    """按类别输出 precision / recall / F1（micro 汇总）。

    用于 D10 的下钻口径 D（分支路由准确率）——分支判定天然不平衡，
    必须配 F1 而不是只看准确率（总体方案 §5.3）。"""
    pairs = list(zip(y_true, y_pred))
    cats = sorted(set(y_true) | set(y_pred))
    per = {}
    tp_all = tn_all = fp_all = fn_all = 0
    for c in cats:
        tp = sum(1 for t, p in pairs if p == c and t == c)
        fp = sum(1 for t, p in pairs if p == c and t != c)
        fn = sum(1 for t, p in pairs if t == c and p != c)
        tn = sum(1 for t, p in pairs if t != c and p != c)
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else (prec or rec)
        per[c] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": prec,
            "recall": rec,
            "f1": f1,
        }
        tp_all += tp
        fp_all += fp
        fn_all += fn
        tn_all += tn
    micro_prec = tp_all / (tp_all + fp_all) if (tp_all + fp_all) else None
    micro_rec = tp_all / (tp_all + fn_all) if (tp_all + fn_all) else None
    micro_f1 = (
        (2 * micro_prec * micro_rec / (micro_prec + micro_rec))
        if (micro_prec and micro_rec)
        else (micro_prec or micro_rec)
    )
    acc = sum(1 for t, p in pairs if t == p) / len(pairs) if pairs else None
    return {
        "n": len(pairs),
        "accuracy": acc,
        "micro": {"precision": micro_prec, "recall": micro_rec, "f1": micro_f1},
        "per_class": per,
    }


def _fmt_opt(x: float | None) -> str:
    return "—" if x is None else f"{x:.3f}"


# --------------------------------------------------------------------------
# 候选构建（candidates）
# --------------------------------------------------------------------------


def _persona_from_provenance(prov: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not prov:
        return None
    keys = (
        "persona_id",
        "genre",
        "popularity",
        "content_tier",
        "urgency",
        "verbal_style",
        "task_type",
        "dimension",
    )
    out = {k: prov.get(k) for k in keys if prov.get(k) not in (None, "")}
    return out or None


def _scan_archives(root: pathlib.Path) -> list[dict[str, Any]]:
    """读取 P1 存档的 outcomes，生成站点级候选。

    只认 ``<task_id>__<run_id>.json`` 形态（run_id 为 8 位小写 hex）；
    ``obscura_*`` / ``plan_*`` 等非 P1 形态一律跳过。"""
    import re

    out: list[dict[str, Any]] = []
    pat = re.compile(r"^(?P<task>.+?)__(?P<run>[0-9a-f]{8})\.json$")
    # 同一 (task, run, url) 在 outcomes 里可能被记录多次（同一次访问重复落账），
    # 站点级候选只应出现一次，否则 golden_id 重复会污染标注映射与 D10 分母。
    seen: set[tuple[str, str, str]] = set()
    for p in sorted(root.glob("*.json")):
        m = pat.match(p.name)
        if not m:
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        outcomes = data.get("outcomes") or []
        prov = data.get("provenance")
        persona = _persona_from_provenance(prov)
        for i, o in enumerate(outcomes):
            url = o.get("url") or ""
            key = (m.group("task"), m.group("run"), url)
            if key in seen:
                continue
            seen.add(key)
            branch = o.get("branch")
            code = {
                "branch": branch,
                "branch_label": o.get("branch_label") or "",
                "question": o.get("question") or "",
                "evidence": o.get("evidence") or "",
                "source": o.get("source") or "",
                "fallback_used": bool(o.get("fallback_used")),
                "reached_play_page": bool(o.get("reached_play_page")),
            }
            out.append(
                {
                    "golden_id": f"{m.group('task')}__{m.group('run')}__{_url_hash(o.get('url') or '')}",
                    "source_kind": "archive_outcome",
                    "task_id": m.group("task"),
                    "run_id": m.group("run"),
                    "url": o.get("url") or "",
                    "site_url": o.get("site_url") or "",
                    "play_page_url": o.get("play_page_url") or "",
                    "code": code,
                    "persona": persona,
                    "golden": None,
                    "retest": None,
                }
            )
    return out


def _scan_negative_pool(
    root: pathlib.Path, existing: Mapping[tuple[str, str, str], dict[str, Any]]
) -> list[dict[str, Any]]:
    """读取负样本池，补充 P1 outcomes 未覆盖的真实失败样本。

    按 (task_id, run_id, url) 去重：P1 已有同键则跳过（P1 outcome 更完整）。"""
    neg_path = root / "negative.jsonl"
    rows = _load_jsonl(neg_path)
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        key = (
            str(r.get("task_id", "")),
            str(r.get("run_id", "")),
            str(r.get("url", "")),
        )
        if key in existing:
            continue
        persona = {
            k: r[k]
            for k in ("persona_id", "content_tier", "popularity", "genre")
            if r.get(k) not in (None, "")
        } or None
        out.append(
            {
                "golden_id": f"{key[0]}__{key[1]}__{_url_hash(key[2])}__neg{i:02d}",
                "source_kind": "negative_pool",
                "task_id": key[0],
                "run_id": key[1],
                "url": key[2],
                "site_url": key[2],
                "play_page_url": "",
                "code": {
                    "branch": r.get("branch"),
                    "branch_label": r.get("branch_label") or "",
                    "question": r.get("question") or "",
                    "evidence": r.get("evidence") or "",
                    "source": r.get("source") or "",
                    "fallback_used": bool(r.get("fallback_used")),
                    "reached_play_page": bool(r.get("reached_play_page")),
                },
                "persona": persona,
                "golden": None,
                "retest": None,
            }
        )
    return out


def _scan_p2_samples(root: pathlib.Path, existing: set[str]) -> list[dict[str, Any]]:
    """P2 样例作为「真实首轮轨迹」锚点（总体方案 §5.2 锚点来源①）。

    轨迹级样本没有单一站点结局，code 为空；用于 D9 对轨迹打分的 gold 载体。"""
    out: list[dict[str, Any]] = []
    for p in sorted((root / "samples").glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        sample_id = data.get("sample_id") or p.stem
        if sample_id in existing:
            continue
        prov = (data.get("six") or {}).get("provenance")
        source_archive = data.get("source_archive") or ""
        task_id = data.get("task_id") or sample_id.split("__", 1)[0]
        run_id = ""
        if "_" in p.stem:
            parts = p.stem.split("__")
            if len(parts) >= 2:
                run_id = parts[1]
        out.append(
            {
                "golden_id": sample_id,
                "source_kind": "p2_sample",
                "task_id": task_id,
                "run_id": run_id,
                "sample_id": sample_id,
                "source_archive": source_archive,
                "url": "",
                "site_url": "",
                "play_page_url": "",
                "code": None,
                "persona": _persona_from_provenance(prov),
                "golden": None,
                "retest": None,
            }
        )
    return out


def cmd_candidates(args: argparse.Namespace) -> int:
    print("WARNING: 'candidates' 是 deprecated 入口,推荐 'from-scout'(走 rubric 评分门禁)。该入口在 v05 可能被移除。", file=sys.stderr)
    root = pathlib.Path(args.root)
    golden_root = pathlib.Path(args.golden_root)
    if not (root / "negative.jsonl").exists() and not golden_root.exists():
        print(
            f"警告：{root} 下未找到负样本池，且 {golden_root} 不存在，候选大概率为空",
            file=sys.stderr,
        )

    archives = _scan_archives(root)
    keys = {}
    for a in archives:
        keys[(a["task_id"], a["run_id"], a["url"])] = a
    negatives = _scan_negative_pool(root, keys)
    # P2 候选的 golden_id 可能与站点级冲突（不同命名空间前缀），去重按 golden_id
    p2_existing = {c["golden_id"] for c in archives + negatives}
    p2 = _scan_p2_samples(root, p2_existing)

    candidates = archives + negatives + p2
    if not candidates:
        print("没有生成任何候选", file=sys.stderr)
        return 0

    dst = golden_root / CANDIDATES_FILE
    if args.dry_run:
        _report_candidates(candidates)
        return 0
    _dump_jsonl(dst, candidates, force=args.force)
    print(f"→ 已写 {dst}（{len(candidates)} 条候选）")
    _report_candidates(candidates)
    return 0


def _report_candidates(candidates: Sequence[Mapping[str, Any]]) -> None:
    total = len(candidates)
    by_kind = collections.Counter(c["source_kind"] for c in candidates)
    site = [c for c in candidates if c["source_kind"] != "p2_sample"]
    by_branch = collections.Counter(
        (c["code"] or {}).get("branch") or SUCCESS_LABEL for c in site
    )
    print(f"候选总数 {total}：来源 {dict(by_kind)}")
    print("站点级分支分布（None = 成功）：")
    for branch, n in sorted(by_branch.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        flag = ""
        if branch not in (SUCCESS_LABEL, None) and n < MIN_PER_BRANCH:
            flag = f"  ⚠️ < {MIN_PER_BRANCH} 条/失败模式"
        print(f"  {str(branch):26s} {n:3d}{flag}")
    lo, hi = TARGET_SIZE
    if total < lo:
        print(
            f"⚠️ 黄金集起步规模为 {lo}–{hi} 条，当前 {total} 条，缺口 {lo - total} 条。"
            "候选只来自真实原料，缺口靠继续跑真实轨迹补齐，不虚构。"
        )
    elif total > hi:
        print(f"⚠️ 已超过目标上限 {hi} 条；可考虑按失败模式分层抽样或拆批次。")


# --------------------------------------------------------------------------
# 标注（annotate）
# --------------------------------------------------------------------------

_VALID_LABELS = None


def _load_branch_labels() -> dict[str, str]:
    """加载分支注册表；import 延后以免 builder 顶层依赖 executor。

    与负样本池/存档的 branch_label 同源，避免二次翻译走样。
    label 校验集由此自动派生：阶段 1 新增 4 分支 + deprecated 兼容在
    ``BRANCH_LABELS`` 注册即生效，无需维护第二份合法值常量（对齐方案 §5.2）。"""
    global _VALID_LABELS
    if _VALID_LABELS is None:
        from trajectory_pipeline.executor.branches import BRANCH_LABELS

        _VALID_LABELS = dict(BRANCH_LABELS)
        _VALID_LABELS[SUCCESS_LABEL] = "成功（可正常播放）"
    return _VALID_LABELS


def cmd_annotate(args: argparse.Namespace) -> int:
    """合并人工标注到 annotations.jsonl。

    输入 JSONL，每行::

        {"golden_id": "...", "round": 1|2, "label": "not_play_site" | "success",
         "reviewed_by": "name", "reviewed_at": "ISO", "note": "...",
         "dimension_scores": {"rationale_consistency": 4}, "interval_days": null}

    round=2 为 retest（D9），必须带 interval_days。同一 (golden_id, round)
    重复写入时校验 label 一致，不一致需 --force。"""
    golden_root = pathlib.Path(args.golden_root)
    cand_path = golden_root / CANDIDATES_FILE
    if not cand_path.exists():
        raise GoldenError(f"{cand_path} 不存在；先跑 candidates")
    candidates = _load_jsonl(cand_path)
    known = {c["golden_id"]: c for c in candidates}
    labels = _load_branch_labels()

    ann_path = golden_root / ANNOTATIONS_FILE
    existing = _load_jsonl(ann_path)
    by_key = {(r["golden_id"], r.get("round", 1)): r for r in existing}

    inp = pathlib.Path(args.input)
    if not inp.exists():
        raise GoldenError(f"标注文件 {inp} 不存在")
    new_rows = _load_jsonl(inp)
    merged: dict[tuple[str, int], dict[str, Any]] = dict(by_key)
    accepted = 0
    for r in new_rows:
        gid = str(r.get("golden_id") or "")
        if gid not in known:
            raise GoldenError(
                f"标注里 {gid} 不在 {len(candidates)} 条候选中；先 candidates 或核对 id"
            )
        rnd = int(r.get("round", 1))
        if rnd not in (1, 2):
            raise GoldenError(f"{gid} round={rnd} 非法，只支持 1（初测）/ 2（retest）")
        label = r.get("label")
        if label not in labels:
            raise GoldenError(f"{gid} label={label!r} 不在注册表 {sorted(labels)}")
        if rnd == 2 and not r.get("interval_days"):
            raise GoldenError(
                f"{gid} retest 必须带 interval_days（D9 边界②：固定并记录实际间隔）"
            )
        dims = r.get("dimension_scores")
        if dims is not None:
            if not isinstance(dims, dict) or not dims:
                raise GoldenError(f"{gid} dimension_scores 须为非空 dict {{dim: 1..5}}")
            bad = {
                k: v
                for k, v in dims.items()
                if not isinstance(v, (int, float)) or not (1 <= v <= 5)
            }
            if bad:
                raise GoldenError(f"{gid} dimension_scores 越界 {bad}，允许 1..5")
        prev = by_key.get((gid, rnd))
        if prev and prev.get("label") != label and not args.force:
            raise GoldenError(
                f"{gid} round={rnd} 已有 label={prev.get('label')!r}，"
                f"新标 {label!r} 不一致；确认后 --force"
            )
        merged[(gid, rnd)] = {
            "golden_id": gid,
            "round": rnd,
            "label": label,
            "label_label": labels[label],
            "reviewed_by": r.get("reviewed_by") or "",
            "reviewed_at": r.get("reviewed_at") or _now_iso(),
            "note": r.get("note") or "",
            "dimension_scores": dims,
            "interval_days": r.get("interval_days"),
        }
        accepted += 1

    ordered = [merged[k] for k in sorted(merged, key=lambda k: (k[0], k[1]))]
    _dump_jsonl(ann_path, ordered, force=True)
    r1 = sum(1 for x in ordered if x["round"] == 1)
    r2 = sum(1 for x in ordered if x["round"] == 2)
    print(
        f"→ 已写 {ann_path}：累计初测 {r1} 条、retest {r2} 条（本次合并 {accepted} 条）"
    )
    if r2:
        intervals = sorted(
            x["interval_days"] for x in ordered if x.get("interval_days")
        )
        print(f"  实际重测间隔天数：{intervals}")
    return 0


# --------------------------------------------------------------------------
# 冻结（freeze）
# --------------------------------------------------------------------------


def _labeled_rows(
    candidates: Sequence[Mapping[str, Any]], annotations: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """把 round=1 标注合并进候选，产出「可冻结」样本。"""
    ann1 = {r["golden_id"]: r for r in annotations if r.get("round") == 1}
    out = []
    for c in candidates:
        a = ann1.get(c["golden_id"])
        if not a:
            continue
        row = dict(c)
        row["golden"] = {
            "label": a["label"],
            "label_label": a["label_label"],
            "reviewed_by": a["reviewed_by"],
            "reviewed_at": a["reviewed_at"],
            "note": a["note"],
            "dimension_scores": a.get("dimension_scores"),
        }
        out.append(row)
    return out


def _stratified_split(
    rows: Sequence[Mapping[str, Any]], seed: int, target: float, lo: float, hi: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """按元标签分层切校准/持有。分层键 = golden.label（p2 轨迹无 label 用 'trajectory'）。

    返回 (calibration, holdout, used_seed)。在 seeds=seed..seed+99 里找第一个
    calib 占比落在 [lo, hi] 的切法；找不到返回最优（最接近 target）并附带警告，
    由调用方决定是否 --force。"""
    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        g = (r.get("golden") or {}).get("label") or "trajectory"
        groups[g].append(dict(r))

    best: tuple[list[dict[str, Any]], list[dict[str, Any]], int, float] | None = None
    best_dist = float("inf")
    for s in range(seed, seed + 100):
        rng = random.Random(s)
        calib, hold = [], []
        for items in groups.values():
            items = list(items)
            rng.shuffle(items)
            n = max(0, min(len(items), int(round(len(items) * target))))
            calib += items[:n]
            hold += items[n:]
        frac = len(calib) / len(rows) if rows else 0.0
        dist = abs(frac - target)
        if lo <= frac <= hi:
            return calib, hold, s
        if dist < best_dist:
            best_dist = dist
            best = (calib, hold, s, frac)
    # 循环至少执行一次且 best_dist 初值 inf，best 必然非 None；此防御只防未来改动引入空输入
    assert best is not None, "stratified split 未产生任何切分"
    return best[0], best[1], best[2]


def cmd_freeze(args: argparse.Namespace) -> int:
    golden_dir = pathlib.Path(args.golden_root)
    cand_path = golden_dir / CANDIDATES_FILE
    ann_path = golden_dir / ANNOTATIONS_FILE
    if not cand_path.exists():
        raise GoldenError(f"{cand_path} 不存在；先跑 candidates")
    if not ann_path.exists():
        raise GoldenError(f"{ann_path} 不存在；先 annotate（冻结只含已人工标注样本）")
    candidates = _load_jsonl(cand_path)
    annotations = _load_jsonl(ann_path)

    labeled = _labeled_rows(candidates, annotations)
    site = [r for r in labeled if r["source_kind"] != "p2_sample"]
    traj = [r for r in labeled if r["source_kind"] == "p2_sample"]
    if not labeled:
        raise GoldenError("没有任何已标注候选；先 annotate 至少 1 条")
    if not site:
        print(
            "⚠️ 站点级（可支撑 D10）标注为 0；仅轨迹级样本时 D10 不可算", file=sys.stderr
        )

    # 版本号
    versions_path = golden_dir / VERSIONS_INDEX
    versions = _load_jsonl(versions_path)
    if args.version:
        version = args.version
        if any(int(v["version"]) == version for v in versions):
            raise GoldenError(
                f"版本 v{version} 已冻结；换版本号（golden 不覆盖历史版本）"
            )
    else:
        version = max([int(v["version"]) for v in versions], default=0) + 1
    vdir = golden_dir / f"v{version}"
    if vdir.exists():
        raise GoldenError(f"{vdir} 已存在；换版本号或人工核对（不静默覆盖）")

    calib, hold, seed = _stratified_split(
        labeled, args.seed, SPLIT_TARGET, CALIBRATION_RANGE[0], CALIBRATION_RANGE[1]
    )
    frac = len(calib) / len(labeled) if labeled else 0.0
    out_of_range = not (CALIBRATION_RANGE[0] <= frac <= CALIBRATION_RANGE[1])
    if out_of_range and not args.force:
        raise GoldenError(
            f"当前切分校准占比 {frac:.1%} 不在 [{CALIBRATION_RANGE[0]:.0%}, "
            f"{CALIBRATION_RANGE[1]:.0%}]，且与目标 {SPLIT_TARGET:.0%} 差 "
            f"{abs(frac - SPLIT_TARGET):.1%}pp 超过 5pp；要么补样本到能分层，要么 --force "
            "（强制冻结并如实记录偏差，回调时按偏差重新切）"
        )
    if abs(frac - SPLIT_TARGET) > SPLIT_PP_TOLERANCE:
        print(
            f"⚠️ 校准占比 {frac:.1%} 与目标 {SPLIT_TARGET:.0%} 偏差超过 5pp，"
            "已在 manifest 记录；下次回调建议补样本重新切",
            file=sys.stderr,
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "label_schema_version": LABEL_SCHEMA_VERSION,  # §5.2：11 类 → 15 类
        "version": version,
        "frozen_at": _now_iso(),
        "seed": seed,
        "counts": {
            "candidates": len(candidates),
            "annotated": len(labeled),
            "site": len(site),
            "trajectory": len(traj),
            "calibration": len(calib),
            "holdout": len(hold),
        },
        "split": {
            "target_calibration": SPLIT_TARGET,
            "calibration_ratio": round(frac, 4),
            "holdout_ratio": round(1 - frac, 4),
            "pp_diff_from_target": round(abs(frac - SPLIT_TARGET), 4),
        },
        "thresholds": {
            "d10_min_agreement": D10_THRESHOLD,
            "retest_interval_days": RETEST_INTERVAL_DAYS,
            "retest_fraction": list(RETEST_FRACTION),
        },
        "source_files": {
            "candidates": f"{cand_path.name}",
            "annotations": f"{ann_path.name}",
        },
    }
    by_label = collections.Counter(
        (r.get("golden") or {}).get("label") or "trajectory" for r in labeled
    )
    manifest["by_label"] = dict(
        sorted(by_label.items(), key=lambda kv: (-kv[1], str(kv[0])))
    )

    if args.dry_run:
        print(json.dumps(manifest, ensure_ascii=False, indent=1))
        print(f"[dry-run] 将写 {vdir}/ 与 {versions_path}")
        return 0

    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    _dump_jsonl(vdir / "calibration.jsonl", calib, force=True)
    _dump_jsonl(vdir / "holdout.jsonl", hold, force=True)
    versions.append(
        {
            "version": version,
            "label_schema_version": LABEL_SCHEMA_VERSION,
            "frozen_at": manifest["frozen_at"],
            "annotated": len(labeled),
            "calibration": len(calib),
            "holdout": len(hold),
        }
    )
    _dump_jsonl(versions_path, versions, force=True)
    print(f"→ 已冻结 v{version} 到 {vdir}/")
    print(
        f"  校准 {len(calib)} 条（{frac:.1%}） / 持有 {len(hold)} 条（{1 - frac:.1%}），"
        f"seed={seed}"
    )
    print(f"  分布：{json.dumps(manifest['by_label'], ensure_ascii=False)}")
    print(
        f"  D10 上岗线 ≥{D10_THRESHOLD:.0%}；D9 建议间隔 {RETEST_INTERVAL_DAYS} 天、"
        f"重测 {RETEST_FRACTION[0]:.0%}–{RETEST_FRACTION[1]:.0%}"
    )
    return 0


# --------------------------------------------------------------------------
# D9 一致性（consistency）
# --------------------------------------------------------------------------


def cmd_consistency(args: argparse.Namespace) -> int:
    """D9 单人 test-retest 自我一致性。

    读 annotations.jsonl 中 round1/round2 配对：
    - 硬维度：站点级 label —— Cohen's kappa（要求 ≥0.7）；
    - 软维度：dimension_scores（1–5）—— 线性加权 kappa（要求 ≥0.50）；
    总闸门：加权后 ≥0.7（由汇报方综合）。"""
    golden_root = pathlib.Path(args.golden_root)
    ann_path = golden_root / ANNOTATIONS_FILE
    if not ann_path.exists():
        raise GoldenError(
            f"{ann_path} 不存在；先 annotate 两轮（round=1 初测、round=2 retest）"
        )
    ann = _load_jsonl(ann_path)
    r1 = {r["golden_id"]: r for r in ann if r.get("round") == 1}
    r2 = {r["golden_id"]: r for r in ann if r.get("round") == 2}
    paired_ids = sorted(set(r1) & set(r2))
    if not paired_ids:
        print(
            "⚠️ 没有 (golden_id, round=1 & round=2) 配对样本。D9 需要间隔重测：",
            file=sys.stderr,
        )
        print(
            "  建议：先 annotate round=1，隔 T=14 天后对 20%–30% 子集 annotate round=2。",
            file=sys.stderr,
        )
        return 0

    # 硬维度：label 一致
    hard_a = [r1[i]["label"] for i in paired_ids]
    hard_b = [r2[i]["label"] for i in paired_ids]
    hard_k = cohen_kappa(hard_a, hard_b)
    hard_agree = sum(1 for x, y in zip(hard_a, hard_b) if x == y) / len(paired_ids)

    # 软维度：dimension_scores 逐维度
    dims: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    for i in paired_ids:
        d1 = r1[i].get("dimension_scores") or {}
        d2 = r2[i].get("dimension_scores") or {}
        for k in set(d1) & set(d2):
            dims[k].append((float(d1[k]), float(d2[k])))
    soft = {}
    for k, pairs in sorted(dims.items()):
        aa = [p[0] for p in pairs]
        bb = [p[1] for p in pairs]
        soft[k] = {
            "n": len(pairs),
            "weighted_kappa": linear_weighted_kappa(aa, bb, 1.0, 5.0),
        }

    intervals: list[int] = sorted(
        int(d)
        for i in paired_ids
        if isinstance((d := r2[i].get("interval_days")), (int, float))
    )

    print(f"D9 test-retest 一致性（配对 {len(paired_ids)} 条）")
    print(
        f"  硬维度 label 一致率: {hard_agree:.1%}  Cohen's kappa: {_fmt_opt(hard_k)}  "
        f"(上岗线 ≥0.7)"
    )
    if soft:
        print("  软维度（1–5 线性加权 kappa，上岗线 ≥0.50）：")
        for k, v in soft.items():
            print(f"    {k:28s} n={v['n']:3d}  kappa={_fmt_opt(v['weighted_kappa'])}")
    else:
        print("  软维度：无 dimension_scores 配对，未测")
    if intervals:
        print(
            f"  实际重测间隔天数：{intervals}（建议 {RETEST_INTERVAL_DAYS} 天；"
            "间隔记录是 D9 边界②）"
        )
    print(
        "  边界提醒：① 这是 test-retest 自相关，不是评分者间一致性；"
        "② 间隔 T 已按上记录；③ 单人的系统性偏差测不出，只能测稳不稳。"
    )
    return 0


# --------------------------------------------------------------------------
# D10 一致率（agreement）
# --------------------------------------------------------------------------


def cmd_agreement(args: argparse.Namespace) -> int:
    """D10 = 口径 C：代码判定 == 黄金集人工标签 / 黄金集样本数（≥90%）。

    站点级样本才有 code，轨迹级样本不计入。另出下钻口径 D（分支路由准确率，
    micro precision/recall/F1）。"""
    golden_root = pathlib.Path(args.golden_root)
    if args.version:
        vdir = golden_root / f"v{args.version}"
        files = [vdir / "calibration.jsonl", vdir / "holdout.jsonl"]
        rows = []
        for f in files:
            if f.exists():
                rows += _load_jsonl(f)
        if not rows:
            raise GoldenError(f"{vdir} 下没有样本；先 freeze")
        label_note = f"冻结版本 v{args.version}"
    else:
        cand_path = golden_root / CANDIDATES_FILE
        ann_path = golden_root / ANNOTATIONS_FILE
        if not cand_path.exists():
            raise GoldenError(f"{cand_path} 不存在；先 candidates")
        candidates = _load_jsonl(cand_path)
        annotations = _load_jsonl(ann_path) if ann_path.exists() else []
        rows = _labeled_rows(candidates, annotations)
        label_note = "未冻结的已标注候选"

    site = [r for r in rows if r["source_kind"] != "p2_sample" and r.get("golden")]
    if not site:
        print(f"⚠️ {label_note} 中站点级已标注样本为 0，D10 不可算", file=sys.stderr)
        return 0

    code_branch_of = lambda r: (r.get("code") or {}).get("branch") or SUCCESS_LABEL
    gold_label_of = lambda r: (r.get("golden") or {}).get("label") or SUCCESS_LABEL
    y_true = [gold_label_of(r) for r in site]
    y_pred = [code_branch_of(r) for r in site]
    # 对齐方案 §4.3：口径 C 升级为「语义等价类一致」——新细分分支与
    # 历史合并态（如 blocked vs login_wall_or_blocked）判一致，避免
    # 标签域扩展当天 D10 假性暴跌。映射只有 alignment.EQUIV_CLASS 一份。
    from trajectory_pipeline.evaluation.golden.alignment import (
        equiv_class,
        is_equivalent,
    )

    agree = sum(1 for t, p in zip(y_true, y_pred) if is_equivalent(t, p))
    ratio = agree / len(site)
    # 下钻口径 D 的分支路由 P/R/F1 也基于等价类标签算，与口径 C 同一把尺；
    # 细分 err 仍可从原始标签（y_true / y_pred）下钻查看。
    report = classification_report(
        [equiv_class(t) for t in y_true], [equiv_class(p) for p in y_pred]
    )

    print(
        f"D10 与黄金集标签一致率（{label_note}，分母 = 黄金集样本数 {len(site)}"
        "；口径 C = 语义等价类一致）"
    )
    print(
        f"  口径 C：{agree}/{len(site)} = {ratio:.1%}  "
        f"{'✅ ≥90% 上岗' if ratio >= D10_THRESHOLD else '❌ 未达 90% 上岗线'}"
    )
    print(
        "  下钻口径 D（分支路由，micro P/R/F1）："
        f"P={_fmt_opt(report['micro']['precision'])}  "
        f"R={_fmt_opt(report['micro']['recall'])}  "
        f"F1={_fmt_opt(report['micro']['f1'])}"
    )
    if args.json_out:
        out = {
            "label_note": label_note,
            "n": len(site),
            "agreed": agree,
            "agreement_ratio": ratio,
            "threshold": D10_THRESHOLD,
            "criterion": "equiv_class",  # 口径 C：语义等价类一致（§4.3）
            "label_schema_version": LABEL_SCHEMA_VERSION,
            "report": report,
        }
        p = pathlib.Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"→ 已写 {p}")
    return 0

# --------------------------------------------------------------------------
# scout 帮手：把 candidates / scout 三件套回溯到 Sample / OutcomeRef
# --------------------------------------------------------------------------
#
# 黄金集的两条入口（cmd_candidates 与 cmd_from_scout）都依赖一个共同事实：
# candidates.jsonl 里的 code 字典要么直接来自 P1 的 outcome（站点级），
# 要么来自 P2 样本的 Sample.outcome（轨迹级）。前者已经在
# _scan_archives 里做完，后者必须先把 scout row 的 sample_id
# （<task>__<run>__<unit>）还原成 Sample 对象再从 outcome 提炼。
# 这两个 helper 就是为这条「P2 → Sample → OutcomeRef → code」链路服务的，
# 不存第二份映射关系。
def _resolve_sample_for_scout_record(
    sample_id: str, p2_root: pathlib.Path
) -> "Sample | None":
    """把 scout 三件套里的 sample_id 还原成 Sample。

    sample_id 形态：<task>__<run>__<unit>（P2 命名空间）。
    通过 <task>__<run>.json 找 P1 存档，借 split_archive 切样本列表，
    再按 sample_id 精确匹配。找不到返回 None，由调用方决定走 quarantine
    还是兜底空 code。
    """
    if "__" not in sample_id:
        return None
    parts = sample_id.split("__")
    if len(parts) < 3:
        return None
    archive_name = f"{parts[0]}__{parts[1]}.json"
    archive_path = p2_root / archive_name
    if not archive_path.exists():
        return None
    from trajectory_pipeline.assembler.schema import split_archive  # 延后 import
    try:
        samples = split_archive(archive_path)
    except Exception:
        return None
    for s in samples:
        if s.sample_id == sample_id:
            return s
    return None


def _code_from_sample(sample: "Sample | None") -> dict[str, Any] | None:
    """从 Sample.outcome 提炼 code 字典。

    与 _scan_archives 给出的 code 字典同构（共享下游对 branch 字段的依赖）。
    Sample.outcome 不含 branch_label / evidence / question ——那是 P1 outcome
    的字段，archive_outcome 的 code 已经直接来自 P1，不必经 Sample；这里
    只服务 from-scd 的 P2 路径。
    """
    if sample is None or getattr(sample, "outcome", None) is None:
        return None
    o = sample.outcome
    return {
        "branch": o.branch,
        "source": o.source,
        "fallback_used": o.fallback_used,
        "succeeded": o.succeeded,
        "reached_play_page": o.reached_play_page,
    }


def _resolve_sample_for_candidate(
    c: dict[str, Any], p2_root: pathlib.Path
) -> "Sample | None":
    """把 candidates.jsonl 的任一行还原成 Sample。

    三种 source_kind 分支：
      - archive_outcome：按 task_id__run_id.json 切存档，再按 url 匹配站点 Sample；
      - p2_sample：按 sample_id 直接切（同 from-scd 路径）；
      - negative_pool：返回 None（负样本池只有 outcome 行，无存档可切）。

    与 cmd_overlay_scout 配套使用；from-scd 只需 P2 形态，
    走 _resolve_sample_for_scout_record 即可。
    """
    kind = c.get("source_kind")
    task_id = str(c.get("task_id") or "")
    rid = str(c.get("run_id") or "")
    if kind == "p2_sample":
        return _resolve_sample_for_scout_record(
            str(c.get("sample_id") or ""), p2_root
        )
    if kind == "archive_outcome":
        if not task_id or not rid:
            return None
        archive_path = p2_root / f"{task_id}__{rid}.json"
        if not archive_path.exists():
            return None
        from trajectory_pipeline.assembler.schema import split_archive
        try:
            samples = split_archive(archive_path)
        except Exception:
            return None
        url = str(c.get("url") or "")
        for s in samples:
            if s.outcome is not None and s.outcome.url == url:
                return s
        return None
    return None


def _report_overlay(
    rows: Sequence[Mapping[str, Any]],
    before_by_kind: Mapping[str, int],
    counters: Mapping[str, int],
) -> None:
    """回填报表：保留原 code.branch 分布 + 评分分布，便于审计。
    """
    print(f"回填前候选来源 {dict(before_by_kind)}；总计 {len(rows)} 条")
    print(
        f"  scored={counters['scored']} missing={counters['missing']} "
        f"skipped={counters['skipped']}"
    )
    branch_counts: collections.Counter = collections.Counter()
    score_dists: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter
    )
    for c in rows:
        if c.get("source_kind") == "negative_pool":
            continue
        branch = (c.get("code") or {}).get("branch")
        branch_counts[branch] += 1
        for dim, score in (c.get("dimension_scores") or {}).items():
            score_dists[dim][score] += 1
    print("分支分布（overlay 不重写 code.branch，仅回填评分）：")
    for branch, n in sorted(
                branch_counts.items(), key=lambda kv: (-kv[1], str(kv[0]))
            ):
        print(f"  {str(branch):26s} {n:3d}")
    print("评分分布（仅已 scored 的样本）：")
    for dim in ("d2", "d4", "d5", "d6"):
        if dim in score_dists:
            print(f"  {dim}: {dict(score_dists[dim])}")


# --------------------------------------------------------------------------
# overlay：把 rubric 四维评分回填到现有 candidates.jsonl
# --------------------------------------------------------------------------
#
# 为什么单独开 subcommand 而不是塞进 from-scd：
#   from-scd 是「从 scout 三件套 → 全量重生成 candidates」，会丢人工标注
#   与历史 freeze 链路；overlay 只挂评分不动 code.branch，保留现有 D10
#   校准过的分支结论。两条命令职责分离：
#     from-scd    → 候选重生成（清老/P2 重切 / scout 改判时用）
#     overlay-scd → 评分回填（保留 code，仅补 dimension_scores）
def cmd_overlay_scout(args: argparse.Namespace) -> int:
    """把 d2 / d4 / d5 / d6 评分回填进 candidates.jsonl。

    写回策略
    --------
      - archive_outcome：按 task_id__run_id.json 切存档 → 按 url 匹配
        Sample → 跑评分。挂 dimension_scores（{dim: N}）与
        scores_detail（完整 verdict 列表）字段；保留 code 不动。
      - p2_sample：按 sample_id 切 Sample → 跑评分。同上保留 code 不动。
      - negative_pool：负样本池只有 outcome 行没有 P2 样本，写
        status='negative_pool_no_sample' 不阻塞流程。

    写回使用 force=True（合规回填操作，不是覆盖新数据）；保留所有
    现有 code.branch、golden、retest 字段值不变。
    """
    from trajectory_pipeline.evaluation.scout.io import _score_one  # type: ignore

    golden_root = pathlib.Path(args.golden_root)
    p2_root = (
        pathlib.Path(args.p2_root) if getattr(args, "p2_root", None)
        else PIPELINE_ROOT
    )
    src = golden_root / CANDIDATES_FILE
    if not src.exists():
        raise GoldenError(f"{src} 不存在；先跑 candidates / from-scd 生成候选")
    rows = _load_jsonl(src)
    if not rows:
        raise GoldenError(f"{src} 为空；驳回；先跑 candidates / from-scd")
    before_by_kind: collections.Counter = collections.Counter(
        c.get("source_kind") for c in rows
    )
    out_rows: list[dict[str, Any]] = []
    counters: collections.Counter = collections.Counter(
        scored=0, missing=0, skipped=0
    )
    for c in rows:
        row = dict(c)  # 浅拷：保留原 code / golden / retest
        sample = _resolve_sample_for_candidate(row, p2_root)
        if sample is None:
            if row.get("source_kind") == "negative_pool":
                row.setdefault("dimension_scores", {})
                row.setdefault("scores_detail", [])
                row["status"] = "negative_pool_no_sample"
                counters["skipped"] += 1
            else:
                row.setdefault("dimension_scores", {})
                row.setdefault("scores_detail", [])
                row["status"] = "missing_sample"
                counters["missing"] += 1
            out_rows.append(row)
            continue
        scores = _score_one(sample)
        # 紧凑型 dimension_scores 仅保留成功打分的维度；None 表示
        # 该评分器判不可判定（例如 D6 在未走到播放页时返回 None），
        # 不写进 dict，以免下游误读成「0 分」。
        dim_scores: dict[str, int] = {
            s["dimension"]: int(s["score"])
            for s in scores
            if s.get("dimension") in ("d2", "d4", "d5", "d6")
            and s.get("score") is not None
        }
        row["dimension_scores"] = dim_scores
        row["scores_detail"] = scores
        row["status"] = "scored"
        counters["scored"] += 1
        out_rows.append(row)
    if args.dry_run:
        _report_overlay(out_rows, before_by_kind, counters)
        return 0
    _dump_jsonl(src, out_rows, force=True)
    _report_overlay(out_rows, before_by_kind, counters)
    print(
        f"→ 已回填 {src}（{len(out_rows)} 条：scored={counters['scored']} "
        f"missing={counters['missing']} skipped={counters['skipped']}）"
    )
    return 0

def cmd_from_scout(args: argparse.Namespace) -> int:
    scout_dir = pathlib.Path(args.scout_dir)
    if not scout_dir.is_dir():
        raise GoldenError(f"{scout_dir} 不是目录;先跑 scout")
    p2_root = (
        pathlib.Path(args.p2_root) if getattr(args, "p2_root", None)
        else PIPELINE_ROOT
    )
    pos_path = scout_dir / "positive_candidates.jsonl"
    neg_path = scout_dir / "negative_candidates.jsonl"
    if not pos_path.exists() and not neg_path.exists():
        raise GoldenError(f"{scout_dir} 下没有 positive/negative_candidates.jsonl;先 scout 跑出三件套")
    pos_rows = _load_jsonl(pos_path) if pos_path.exists() else []
    neg_rows = _load_jsonl(neg_path) if neg_path.exists() else []
    candidates: list[dict[str, Any]] = []
    for src_kind, rows in (("positive_scored", pos_rows), ("negative_scored", neg_rows)):
        for r in rows:
            scores = r.get("scores") or []
            dim_scores = {
                s["dimension"]: (s["score"] if s["score"] is not None else 0)
                for s in scores if s.get("dimension") in ("d2", "d4", "d5", "d6")
            }
            sample_id = str(r.get("sample_id") or "")
            task_id = str(r.get("task_id") or sample_id.split("__", 1)[0])
            run_id = str(r.get("run_id") or "")
            sample_obj = _resolve_sample_for_scout_record(
                sample_id, p2_root
            )
            code = _code_from_sample(sample_obj) if sample_obj else None
            if code is None:
                # 没有 P1 存档就老老实实写空 —— D-10 会把这批的 code.branch
                # 兜成 SUCCESS_LABEL，与 fallback 一致；不静默填错分支。
                code = {"branch": None}
            candidates.append({
                "golden_id": sample_id,
                "source_kind": src_kind,
                "task_id": task_id,
                "run_id": run_id,
                "sample_id": sample_id,
                "url": "",
                "site_url": "",
                "play_page_url": "",
                "code": code,
                "persona": None,
                "dimension_scores": dim_scores,
                "tags": list(r.get("tags") or []),
                "evidence_supplement": dict(r.get("evidence_supplement") or {}),
                "golden": None,
                "retest": None,
            })
    if not candidates:
        print("WARNING: scout 三件套均为空,候选为 0 条", file=sys.stderr)
    dst = pathlib.Path(args.golden_root) / CANDIDATES_FILE
    if args.dry_run:
        _report_candidates(candidates)
        return 0
    _dump_jsonl(dst, candidates, force=args.force)
    print(f"-> 已写 {dst}({len(candidates)} 条候选,其中 pos={len(pos_rows)} neg={len(neg_rows)})")
    _report_candidates(candidates)
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m trajectory_pipeline.evaluation.golden.builder",
        description="黄金集构建 / 标注 / 冻结 / 一致性测量（D9 / D10）",
    )
    ap.add_argument(
        "--root",
        default=str(PIPELINE_ROOT),
        help=f"管线产物根：负样本池/P1 存档/P2 样例（默认 {PIPELINE_ROOT}）",
    )
    ap.add_argument(
        "--golden-root",
        default=str(GOLDEN_ROOT),
        help=f"黄金集数据根（默认 {GOLDEN_ROOT}，独立于 output/ 纳入版本控制）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_cand = sub.add_parser("candidates", help="[DEPRECATED] 从负样本池/P1 outcomes/P2 样例生成候选（不跑 rubric 评分）")
    p_cand.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    p_cand.add_argument(
        "--force", action="store_true", help="覆盖已存在的 candidates.jsonl"
    )
    p_cand.set_defaults(func=cmd_candidates)

    p_from_scout = sub.add_parser("from-scout", help="只读 scout 三件套 -> candidates.jsonl(quarantine 永不入)")
    p_from_scout.add_argument("--scout-dir", required=True,
                              help="指向 evaluation/scout 落盘目录")
    p_from_scout.add_argument("--p2-root", default=str(PIPELINE_ROOT),
                              help=f"P1 存档根（默认 {PIPELINE_ROOT}）；cmd 会借 split_archive 回溯 Sample.outcome 推 code")
    p_from_scout.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    p_from_scout.add_argument("--force", action="store_true", help="覆盖已存在的 candidates.jsonl")
    p_from_scout.set_defaults(func=cmd_from_scout)

    p_overlay = sub.add_parser(
        "overlay-scout",
        help="回填 rubric 四维评分(d2/d4/d5/d6)到现有 candidates.jsonl,保留 code.branch 不动",
    )
    p_overlay.add_argument(
        "--p2-root", default=str(PIPELINE_ROOT),
        help=f"P1 存档根（默认 {PIPELINE_ROOT}）",
    )
    p_overlay.add_argument("--dry-run", action="store_true", help="只统计不落盘")
    p_overlay.set_defaults(func=cmd_overlay_scout)

    p_ann = sub.add_parser(
        "annotate", help="合并人工标注（round1 初测 / round2 retest）"
    )
    p_ann.add_argument("--input", required=True, help="标注 JSONL 路径")
    p_ann.add_argument(
        "--force",
        action="store_true",
        help="同一 golden_id 同 round 的 label 不一致时允许覆盖",
    )
    p_ann.set_defaults(func=cmd_annotate)

    p_frz = sub.add_parser("freeze", help="版本化冻结 + 校准/持有切块")
    p_frz.add_argument("--version", type=int, default=0, help="版本号；缺省自动 +1")
    p_frz.add_argument("--seed", type=int, default=42, help="分层切分随机种子")
    p_frz.add_argument("--dry-run", action="store_true", help="只输出 manifest 不落盘")
    p_frz.add_argument(
        "--force", action="store_true", help="切分偏差超 5pp 时也强制冻结（如实记录）"
    )
    p_frz.set_defaults(func=cmd_freeze)

    p_con = sub.add_parser(
        "consistency", help="D9 单人 test-retest 一致性（读当前 annotations.jsonl）"
    )
    p_con.set_defaults(func=cmd_consistency)

    p_agr = sub.add_parser("agreement", help="D10 代码层与黄金集一致率")
    p_agr.add_argument(
        "--version", type=int, default=0, help="冻结版本号；缺省用未冻结的已标注候选"
    )
    p_agr.add_argument("--json-out", default="", help="结果落成 JSON")
    p_agr.set_defaults(func=cmd_agreement)
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except GoldenError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
