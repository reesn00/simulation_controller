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

#: rubric 版本与评估器指纹——每个冻结版本 / 每次 agreement 都要记下来，
#: 否则「这批数字是哪版尺子量的」答不出（方案 16 §1.2 / C-01）。
#: 只 import 纯常量模块；LLM 侧 prompt 在本模块函数内惰性加载。
from trajectory_pipeline.evaluation.scorers.rubric_version import (
    RUBRIC_VERSION,
    VERSION_UNRECORDED,
    code_part,
    code_scorer_version,
    evaluator_version,
    scorer_fingerprint,
)

#: 判定线**唯一真源**（方案 16 C-14；owner 2026-10-23 裁定 D-C6：
#: 阈值调整**不算** rubric 级变更 ⇒ 不升版本 / 不进 rubric 变更记录 /
#: 不设签名门，但阈值只此一份，各处不许再写字面量）。
from trajectory_pipeline.evaluation.scorers import thresholds

#: 三档切分的档名 / 文件名 / 头条档位——**只从 splits.py 取，不手抄字符串**
#: （方案 16 C-04 / C-17：头条分母认错档位不会报错，只会静默算成 0）。
from trajectory_pipeline.evaluation.golden.splits import (
    HEADLINE_SPLIT,
    REGRESSION_SEED,
    REGRESSION_SIZE,
    SPLIT_CALIBRATION,
    SPLIT_FILES,
    SPLIT_HOLDOUT,
    SPLIT_REGRESSION,
    SPLITS,
    split_of_file,
)

#: 软维度金标的写入规则（只动分数、不碰标签）。`annotate --dimensions-only`
#: 与 `freeze --refresh-gold` 两条路都走这一份，不各写一套合并逻辑。
from trajectory_pipeline.evaluation.golden.dim_scores import (
    DimScoreError,
    group_fills,
    merge_dimension_scores,
)

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
RETEST_INTERVAL_DAYS = 14  # D9 建议间隔 T（INC41 起 degrade 为纯建议：2026-10-15 owner 拍板解除法定间隔，实际间隔以 d9_retest_queue.jsonl / annotations.jsonl 的 interval_days 为准）
#: 代码层与黄金集一致率上岗线（口径 C = 语义等价类一致）。
#: **值不写在这里**——从阈值表取（方案 16 C-14）。改线请改
#: ``evaluation/scorers/thresholds.py`` 那张表，别动这一行（D-C6：
#: 改阈值不升 rubric 版本，所以这一行也不该成为「偷偷改线」的地方）。
D10_THRESHOLD = thresholds.value("d10_min_agreement")
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

#: ``overlay-scout`` 负责回填的四个软维度。**键齐全是硬要求**：
#: 有样本时四维都要出现（弃权写 ``None``），没有样本才留空 dict——
#: 「弃权」与「没测」在快照里长得一样，正是 M-29 要修的那件事。
OVERLAY_DIMS: tuple[str, ...] = ("d2", "d4", "d5", "d6")

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


def _compute_soft_dim_agreement(
    site_rows: Sequence[Mapping[str, Any]],
    *,
    requested_dims: Sequence[str],
    tolerance: int,
) -> dict[str, dict[str, Any]]:
    """D10 软维度一致率（plan §三·第一步）的逐样单列计算。

    与硬分类一致率（``is_equivalent``）共用一组 ``site`` 行，但**只走
    calibration set**（plan contract 5「calibration 优先于 holdout」——
    holdout 留给最终评估，软维度对齐先在 calibration 上跑）。

    输入侧口径：
      - **gold 侧** = ``golden.dimension_scores``（黄金集人工 1-5 分档）；
      - **pred 侧** = 行级 ``dimension_scores``（overlay-scout 写回的
        d2/d4/d5/d6 scorer 输出——judge 上岗前的占位 pred，与 LLM score
        **不是同一物**，由 ``cmd_overlay_scout`` 落盘后保证）。

    维度名发现（``requested_dims`` 为空）：取 gold 侧出现过的 dim 与
    ``alignment.SOFT_DIM_NAMES`` 的交集；白名单外的 dim 静默抛弃，不参与计算。
    N=0 的 dim **不进 dict**（contract 3「空样本不参与」），输出报表里
    不会出现「0/0 = 0%」误导。
    """
    from trajectory_pipeline.evaluation.golden.alignment import (
        SOFT_DIM_NAMES,
        dim_agreement as _dim_agree,
    )

    # 只认同头条档（``HEADLINE_SPLIT``）：软维度与硬分类必须看同一个池子，
    # 否则「D-10 在调参池上算、软维度在别的档上算」这种错配看不出来。
    calib = [r for r in site_rows if r.get("__split__") == HEADLINE_SPLIT]

    gold_dims: set[str] = set()
    for r in calib:
        gd = (r.get("golden") or {}).get("dimension_scores")
        if isinstance(gd, Mapping):
            gold_dims |= set(gd.keys())

    if requested_dims:
        dims = list(requested_dims)
    else:
        dims = sorted(gold_dims & set(SOFT_DIM_NAMES))

    out: dict[str, dict[str, Any]] = {}
    for dim in dims:
        n = 0
        agreed = 0
        skipped_no_pred = 0
        skipped_no_gold = 0
        skipped_invalid = 0
        for r in calib:
            gd = (r.get("golden") or {}).get("dimension_scores") or {}
            pd = r.get("dimension_scores") or {}
            t = gd.get(dim)
            p = pd.get(dim)
            if t is None:
                skipped_no_gold += 1
                continue
            if p is None:
                skipped_no_pred += 1
                continue
            verdict = _dim_agree(t, p, tolerance=tolerance)
            if verdict is None:
                skipped_invalid += 1
                continue
            n += 1
            if verdict:
                agreed += 1
        if n == 0:
            continue
        out[dim] = {
            "n": n,
            "agreed": agreed,
            "agreement_ratio": agreed / n,
            "skipped_no_gold": skipped_no_gold,
            "skipped_no_pred": skipped_no_pred,
            "skipped_invalid": skipped_invalid,
            "tolerance": tolerance,
        }
    return out


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
    重复写入时校验 label 一致，不一致需 --force。

    ``--dimensions-only`` 是**另一条路**（方案 16 · 填软维度金标）：输入只带
    分数，``label`` 一字不碰。补 d2/d4/d5/d6 的人手里往往没有分支裁定，
    走整行模式就得抄一个标签——那等于伪造金标。"""
    golden_root = pathlib.Path(args.golden_root)
    if getattr(args, "dimensions_only", False):
        return _annotate_dimensions_only(args, golden_root)
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


def _annotate_dimensions_only(args: argparse.Namespace, golden_root: pathlib.Path) -> int:
    """``annotate --dimensions-only``：**只合并软维度分数**，标签一字不碰。

    输入接受两种形态（见 :func:`group_fills`）：工作表行
    ``{golden_id, dimension, gold_score}`` 或手写补丁
    ``{golden_id, dimension_scores: {d5: 4}}``。

    三条与整行模式**故意不同**的规矩：

    - **不要求 label**——补分数的人不该被迫抄一个分支裁定；
    - **只认已存在的 round=1 标注**——分数不能凭空造样本；
    - **默认不覆盖已填的分**（``--force`` 才覆盖）——同键同值幂等跳过。

    谁填的记在 ``dimension_reviewed_by`` / ``dimension_reviewed_at``，
    **不覆盖标签的原署名人**：那是两笔账。
    """
    ann_path = golden_root / ANNOTATIONS_FILE
    existing = _load_jsonl(ann_path)
    if not existing:
        raise GoldenError(
            f"{ann_path} 里还没有标注：软维度分数必须挂在一条已有标注上，"
            "先跑 `annotate`（整行模式）补标签"
        )
    inp = pathlib.Path(args.input)
    if not inp.exists():
        raise GoldenError(f"标注文件 {inp} 不存在")
    rows = _load_jsonl(inp)
    if not rows:
        raise GoldenError(f"{inp} 是空的")
    try:
        fills = group_fills(rows)
        if not fills:
            raise DimScoreError(
                "输入里没有一个填好的格子（gold_score 全是空）——"
                "**没填不是判不了**：判不了写 null，没看就留空"
            )
        merged, stats = merge_dimension_scores(
            existing, fills, allow_overwrite=bool(args.force)
        )
    except DimScoreError as exc:
        raise GoldenError(str(exc)) from exc
    if not stats["changed"]:
        print(
            f"→ 无变化：{stats['unchanged']} 条与现有分数相同（幂等跳过），未写盘"
        )
        return 0
    ordered = sorted(
        merged, key=lambda r: (str(r.get("golden_id") or ""), int(r.get("round", 1)))
    )
    _dump_jsonl(ann_path, ordered, force=True)
    per_dim = " · ".join(
        f"{dim} {n}" for dim, n in sorted(stats["per_dim"].items())
    )
    print(
        f"→ 已写 {ann_path}：合并 {stats['changed']} 条"
        f"（幂等跳过 {stats['unchanged']} 条 · 人判判不了 {stats['judged_na']} 格）"
    )
    print(f"  本次补的格子：{per_dim}")
    if stats["overwritten"]:
        print(f"  ⚠ 覆盖已有分数 {stats['overwritten']} 格（--force）")
    print(
        "  下一步：`freeze --from-version <上一版> --refresh-gold`"
        " 把分数带进新版本——**金标变动 = 新基线**，与前版不可比"
    )
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


def _rows_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    """回归集的 12 位内容指纹（方案 16 C-17「金标冻结 + diff 守卫」）。

    对**整行规范 JSON**（``sort_keys`` + 按 ``golden_id`` 排序）取 sha256：
    只指纹 id 列表挡不住"标签被人改了"，而这正是回归集最需要防的一种改法
    ——把原本判对的标签改成与模型一致，`regressed` 就永远是 0。

    ``__`` 开头的键是读冻结档时**运行时加的标注**（``__split__``），不是内容，
    指纹前剔掉：否则同一批行「freeze 时算一次、agreement 读时算一次」永远对不上。
    """
    blob = "\n".join(
        json.dumps(
            {k: v for k, v in r.items() if not str(k).startswith("__")},
            ensure_ascii=False,
            sort_keys=True,
        )
        for r in sorted(rows, key=lambda r: str(r.get("golden_id") or ""))
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _regression_slice(
    rows: Sequence[Mapping[str, Any]], k: int, *, seed: int = REGRESSION_SEED
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """从 calibration 里切固定 ``k`` 条**站点级**行作回归集（方案 16 C-17）。

    返回 ``(regression, rest)``。三条规矩：

    1. **只收站点级**（``source_kind != "p2_sample"`` 且有 ``golden``）。
       轨迹级行没有 D-10 标签，放进回归集等于往门禁里塞永远判不了的条目。
    2. **按 ``golden.label`` 分层**，配额按层大小比例 + 余数给最大的层。
       均匀抽样会让稀有分支（``subscription_wall`` 之类）在 60 条里落到个位数，
       那条分支出回归时读不出来。
    3. **确定性**：层内用 ``random.Random(f"{seed}:{label}")``，同一输入
       两次切出同一批行。回归集一旦冻结就不再重切——重切等于把"原本判对的"
       换成"这次判对的"，门禁自证为空。
    """
    if k <= 0:
        return [], [dict(r) for r in rows]
    site = [
        r for r in rows
        if r.get("source_kind") != "p2_sample" and r.get("golden")
    ]
    if k >= len(site):
        raise GoldenError(
            f"回归集要切 {k} 条，但 calibration 只有 {len(site)} 条站点级样本；"
            "调参池会剩不下——要么少切，要么先补标注"
        )

    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in site:
        groups[str((r.get("golden") or {}).get("label") or SUCCESS_LABEL)].append(r)

    total = len(site)
    quotas = {
        label: (k * len(items)) // total for label, items in groups.items()
    }
    remainder = k - sum(quotas.values())
    # 余数分配顺序显式写死（层大优先、同大同名），不依赖 dict 插入顺序
    for label in sorted(groups, key=lambda s: (-len(groups[s]), s)):
        if remainder <= 0:
            break
        if quotas[label] < len(groups[label]):
            quotas[label] += 1
            remainder -= 1

    picked: list[dict[str, Any]] = []
    for label in sorted(groups):
        items = sorted(groups[label], key=lambda r: str(r.get("golden_id") or ""))
        rng = random.Random(f"{seed}:{label}")
        for i in sorted(rng.sample(range(len(items)), quotas[label])):
            picked.append(items[i])

    picked_ids = {str(r.get("golden_id") or "") for r in picked}
    picked.sort(key=lambda r: str(r.get("golden_id") or ""))
    rest = [
        dict(r) for r in rows if str(r.get("golden_id") or "") not in picked_ids
    ]
    return [dict(r) for r in picked], rest


def _score_tally(
    rows: Sequence[Mapping[str, Any]], dim: str
) -> dict[str, int]:
    """一维的三态计数：``scored`` / ``abstain`` / ``no_sample``。

    判据是**行的 ``status``**，不是「键在不在 dict 里」：
    ``status`` 是 overlay-scout 落盘时写死的三态（scored / missing_sample /
    negative_pool_no_sample），比键的形状稳。
    """
    out = {"scored": 0, "abstain": 0, "no_sample": 0}
    for row in rows:
        status = str(row.get("status") or "")
        if status != "scored":
            out["no_sample"] += 1
            continue
        value = (row.get("dimension_scores") or {}).get(dim)
        if value is None:
            out["abstain"] += 1
        else:
            out["scored"] += 1
    return out


def _dim_dist(rows: Sequence[Mapping[str, Any]], dim: str) -> dict[str, int]:
    """一维的取值分布，落盘用（键统一成字符串，``None`` → ``"null"``）。

    JSON 对象的键本来就是字符串。不提前统一的话，内存里是 ``{5: 15}``、
    落盘读回来是 ``{"5": 15}``——同一份东西两种长相，审计时对不上。
    """
    counter: collections.Counter = collections.Counter()
    for row in rows:
        counter[(row.get("dimension_scores") or {}).get(dim)] += 1
    return {
        ("null" if k is None else str(k)): v
        for k, v in _sorted_score_dist(counter).items()
    }


def _refresh_scores(
    parts: Mapping[str, Sequence[Mapping[str, Any]]],
    cand_path: pathlib.Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """继承式冻结时把 pred 侧的评分按 ``golden_id`` 从 candidates 重挂。

    为什么需要它（M-29）
    ------------------
    继承式冻结的设计是「**行内容不动**」——v7 冻结时写进行里的分数是
    **当时**评分器的输出。评分器换了之后那批分数就过期了：v7 的 d5 pred
    全是 5，因为那时 D-5 还「恒真」（四条检查全从自己的账本里取输入）。
    快照与评分器于是不再是同一个东西，而这件事**一个字都不报**。

    换哪三个字段
    ------------
    只换 ``dimension_scores`` / ``scores_detail`` / ``status``；
    **``golden`` 与 ``code`` 一个字不动**——金标与分支结论不归这条路管，
    这条路上没有任何东西被重算（d10 的 pred 仍是冻结行的 ``code.branch``）。

    fail-closed
    -----------
    - ``golden_id`` 在 candidates 里找不到 ⇒ 退 2 并**列出全部缺失 id**，
      绝不「找不到就留着旧分数」（那等于把过期分数继续盖着，看起来像刷过了）。
    - candidates 行缺 ``status`` ⇒ 退 2：那份文件没经 ``overlay-scout``
      回填，拿它覆盖只会把行里的分数抹成空。

    审计口径
    --------
    ``per_dim`` 的 before/after 分布取**有样本的行**（与快照里该维的条目
    同口径）；``tally`` 取**全部站点级行**，按**刷新后**的状态分三态。
    """
    if not cand_path.exists():
        raise GoldenError(
            f"{cand_path} 不存在；--refresh-scores 要从 candidates 重挂评分"
        )
    candidates = {
        str(r.get("golden_id") or ""): r for r in _load_jsonl(cand_path)
    }
    missing: list[str] = []
    new_parts: dict[str, list[dict[str, Any]]] = {}
    for split, rows in parts.items():
        refreshed: list[dict[str, Any]] = []
        for row in rows:
            gid = str(row.get("golden_id") or "")
            cand = candidates.get(gid)
            if cand is None:
                missing.append(gid)
                continue
            if not cand.get("status"):
                raise GoldenError(
                    f"candidates 里的 {gid} 没有 status；这份 candidates 没经 "
                    "overlay-scout 回填，先跑 overlay-scout 再刷新评分"
                )
            new = dict(row)
            new["dimension_scores"] = cand.get("dimension_scores") or {}
            new["scores_detail"] = cand.get("scores_detail") or []
            new["status"] = cand.get("status")
            refreshed.append(new)
        new_parts[split] = refreshed
    if missing:
        raise GoldenError(
            "candidates.jsonl 里找不到这些 golden_id（"
            f"{len(missing)} 个）：{' / '.join(missing)}；"
            "**不刷新**（留着过期分数比刷一半更糟），先补齐候选"
        )

    before_rows = [r for rows in parts.values() for r in rows]
    after_rows = [r for rows in new_parts.values() for r in rows]

    def _site(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [r for r in rows if r.get("source_kind") != "p2_sample"]

    before_site = _site(before_rows)
    after_site = _site(after_rows)
    per_dim: dict[str, Any] = {}
    tally: dict[str, Any] = {}
    for dim in OVERLAY_DIMS:
        per_dim[dim] = {
            "before": _dim_dist(
                [r for r in before_site if r.get("status") == "scored"], dim
            ),
            "after": _dim_dist(
                [r for r in after_site if r.get("status") == "scored"], dim
            ),
        }
        tally[dim] = _score_tally(after_site, dim)
    info = {
        "enabled": True,
        "source": cand_path.name,
        "rows_matched": len(after_rows),
        "rows_missing": 0,
        "population": "site_rows",
        "per_dim": per_dim,
        "tally": tally,
        "note": (
            "per_dim 的 before/after 取有样本的行（与快照该维条目同口径）；"
            "tally 取全部站点级行，按刷新后的状态分三态"
        ),
    }
    return new_parts, info


def _refresh_gold(
    parts: Mapping[str, Sequence[Mapping[str, Any]]],
    ann_path: pathlib.Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """继承式冻结时把 **gold 侧的软维度分数**按 ``golden_id`` 从标注重挂。

    为什么需要它（填软维度金标的最后一段）
    ------------------------------------
    软维度金标住在 ``annotations.jsonl`` 的 ``dimension_scores`` 里，冻结时由
    :func:`_labeled_rows` 抄进 ``golden.dimension_scores``。而**继承式冻结是
    「行内容不动」**——今天填的分数，走 ``--from-version`` 永远进不了新版本。
    不补这条路，「填金标」这件事在冻结链上**没有出口**。

    换哪一栏
    --------
    **只换** ``golden.dimension_scores``。``golden.label`` / ``note`` /
    ``reviewed_by`` / ``code`` / ``dimension_scores``（pred 侧）一个字不动
    ——标签金标的改动是另一件事（Q-11 裁定：不重标），这条路上没有东西被重判。

    ⚠️ **金标变动 = 新基线**：两版之间的分数变动**不能**算进「净改善」——
    修好/弄坏只有在同一份金标上才可比（§10「改金标要新版本 + 人裁」）。
    所以 manifest 落 ``baseline_reset: true``，``gate`` 读到它就退 2
    （与前版不可比），下一对可比是**本版与它的后继**。

    fail-closed：标注里一条软维度分都没有 ⇒ 退 2（刷空等于什么都没做，
    却消耗掉一个版本号）；``golden_id`` 找不到 round=1 标注 ⇒ 退 2 并列出全部。
    """
    annotations = {
        str(r.get("golden_id") or ""): r
        for r in _load_jsonl(ann_path)
        if int(r.get("round", 1)) == 1
    }
    if not annotations:
        raise GoldenError(f"{ann_path} 里没有 round=1 标注；先 annotate 再 --refresh-gold")
    filled = [
        r for r in annotations.values()
        if isinstance(r.get("dimension_scores"), Mapping) and r.get("dimension_scores")
    ]
    if not filled:
        raise GoldenError(
            f"{ann_path} 里一条软维度分数都没有：先填（`annotate --dimensions-only`）"
            "再 --refresh-gold——刷空等于什么都没做，却把版本号用掉了"
        )

    missing: list[str] = []
    new_parts: dict[str, list[dict[str, Any]]] = {}
    before_rows: list[Mapping[str, Any]] = []
    after_rows: list[Mapping[str, Any]] = []
    changed_rows = 0
    for split, rows in parts.items():
        refreshed: list[dict[str, Any]] = []
        for row in rows:
            gid = str(row.get("golden_id") or "")
            ann = annotations.get(gid)
            if ann is None:
                missing.append(gid)
                continue
            dims = ann.get("dimension_scores")
            dims = dict(dims) if isinstance(dims, Mapping) and dims else None
            new = dict(row)
            golden = dict(row.get("golden") or {})
            old_dims = golden.get("dimension_scores")
            golden["dimension_scores"] = dims
            new["golden"] = golden
            before_rows.append(row)
            after_rows.append(new)
            if (old_dims or {}) != (dims or {}):
                changed_rows += 1
            refreshed.append(new)
        new_parts[split] = refreshed
    if missing:
        raise GoldenError(
            "annotations.jsonl 里找不到这些 golden_id 的 round=1 标注（"
            f"{len(missing)} 个）：{' / '.join(missing)}；不刷新"
        )

    per_dim: dict[str, Any] = {}
    for dim in OVERLAY_DIMS:
        per_dim[dim] = {
            "before": _dim_dist(
                [_as_gold(r) for r in before_rows if _as_gold(r).get(dim) is not None],
                dim,
            ),
            "after": _dim_dist(
                [_as_gold(r) for r in after_rows if _as_gold(r).get(dim) is not None],
                dim,
            ),
        }
    info = {
        "enabled": True,
        "source": ann_path.name,
        "rows_matched": len(after_rows),
        "rows_missing": 0,
        "changed_rows": changed_rows,
        "per_dim": per_dim,
        # ⚠ 金标变了 = 新基线：gate 读到它就拒绝跟上一版比（退 2）。
        "baseline_reset": True,
        "note": (
            "只重挂 golden.dimension_scores；label / code / pred 侧未动。"
            "金标变动 ⇒ 与前版不可比（新基线），下一对可比是本版与它的后继"
        ),
    }
    return new_parts, info


def _as_gold(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """取 ``golden`` 子字典（只读视图）；没有就空 dict。"""
    golden = row.get("golden")
    return golden if isinstance(golden, Mapping) else {}


def _refresh_label(
    parts: Mapping[str, Sequence[Mapping[str, Any]]],
    ann_path: pathlib.Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """继承式冻结时把 **gold 侧的 D-10 label** 按 ``golden_id`` 从标注重挂。

    为什么需要它（修正 gold_defect 金标错的最后一段）
    ------------------------------------------------
    D-10 金标 label 住在 ``annotations.jsonl`` 的 ``label`` 里，冻结时由
    :func:`_labeled_rows` 抄进 ``golden.label``。而**继承式冻结是「行内容不动」**——
    今天修正的 label，走 ``--from-version`` 永远进不了新版本。软维度有
    ``--refresh-gold``、评分有 ``--refresh-scores``，唯独 D-10 label 没有出口，
    这是 Q-11「改标签 = 新版本 + 人裁」在冻结链上断掉的那一截。

    换哪一栏
    --------
    **只换** ``golden.label`` / ``label_label`` / ``reviewed_by`` /
    ``reviewed_at`` / ``note``（五个与 label 绑定的字段）。
    ``golden.dimension_scores`` / ``code`` / pred 侧（``dimension_scores`` /
    ``scores_detail`` / ``status``）**一个字不动**——软维度金标归
    ``--refresh-gold``、评分归 ``--refresh-scores`` 管，各走各的出口。

    ⚠️ **label 变动 = 新基线**：与 ``--refresh-gold`` 同理，label 改动**不能**
    算进「净改善」——所以 manifest 落 ``baseline_reset: true``，``gate`` 读到它
    就退 2（与前版不可比），下一对可比是**本版与它的后继**。

    fail-closed：``golden_id`` 找不到 round=1 标注 ⇒ 退 2 并列出全部；本次要
    重挂的 label 与冻结行里的旧 label 完全相同 ⇒ 退 2（刷空等于什么都没做，
    却消耗掉一个版本号）。
    """
    annotations = {
        str(r.get("golden_id") or ""): r
        for r in _load_jsonl(ann_path)
        if int(r.get("round", 1)) == 1
    }
    if not annotations:
        raise GoldenError(f"{ann_path} 里没有 round=1 标注；先 annotate 再 --refresh-label")

    missing: list[str] = []
    new_parts: dict[str, list[dict[str, Any]]] = {}
    before_by_label: dict[str, int] = collections.Counter()
    after_by_label: dict[str, int] = collections.Counter()
    changed_rows = 0
    for split, rows in parts.items():
        refreshed: list[dict[str, Any]] = []
        for row in rows:
            gid = str(row.get("golden_id") or "")
            ann = annotations.get(gid)
            if ann is None:
                missing.append(gid)
                continue
            new = dict(row)
            golden = dict(row.get("golden") or {})
            old_label = golden.get("label")
            before_by_label[old_label or ""] += 1
            new_label = ann.get("label")
            golden["label"] = new_label
            golden["label_label"] = ann.get("label_label")
            golden["reviewed_by"] = ann.get("reviewed_by")
            golden["reviewed_at"] = ann.get("reviewed_at")
            golden["note"] = ann.get("note")
            new["golden"] = golden
            after_by_label[new_label or ""] += 1
            if old_label != new_label:
                changed_rows += 1
            refreshed.append(new)
        new_parts[split] = refreshed
    if missing:
        raise GoldenError(
            "annotations.jsonl 里找不到这些 golden_id 的 round=1 标注（"
            f"{len(missing)} 个）：{' / '.join(missing)}；不刷新"
        )
    if changed_rows == 0:
        raise GoldenError(
            "annotations.jsonl 里所有行 label 都与冻结行一致：没有需要重挂的 label"
            "（刷空等于什么都没做，却把版本号用掉了）；先 annotate 改 label 再 --refresh-label"
        )
    info = {
        "enabled": True,
        "source": ann_path.name,
        "rows_matched": len(after_by_label),
        "rows_missing": 0,
        "changed_rows": changed_rows,
        "label_shift": {
            "before": dict(sorted(before_by_label.items(), key=lambda kv: (-kv[1], str(kv[0])))),
            "after": dict(sorted(after_by_label.items(), key=lambda kv: (-kv[1], str(kv[0])))),
        },
        # ⚠ label 变了 = 新基线：gate 读到它就拒绝跟上一版比（退 2）。
        "baseline_reset": True,
        "note": (
            "只重挂 golden.label / label_label / reviewed_by / reviewed_at / note；"
            "dimension_scores / code / pred 侧未动。label 变动 ⇒ 与前版不可比（新基线），"
            "下一对可比是本版与它的后继"
        ),
    }
    return new_parts, info


def cmd_freeze(args: argparse.Namespace) -> int:
    golden_dir = pathlib.Path(args.golden_root)
    cand_path = golden_dir / CANDIDATES_FILE
    ann_path = golden_dir / ANNOTATIONS_FILE
    # `--from-version` 是**继承式重冻结**：切分从上一版原样带过来，不再回头读
    # candidates/annotations。回头重切会重新洗牌，"v7 与 v6 逐行可比"就没了。
    carrying = bool(getattr(args, "from_version", 0))
    refresh_scores = bool(getattr(args, "refresh_scores", False))
    refresh_gold = bool(getattr(args, "refresh_gold", False))
    refresh_label = bool(getattr(args, "refresh_label", False))
    if refresh_scores and not carrying:
        raise GoldenError(
            "--refresh-scores 只能配 --from-version 用：不继承的冻结本来就"
            "直接读 candidates 的评分字段，没有「带过来的旧分数」这回事"
        )
    if refresh_gold and not carrying:
        raise GoldenError(
            "--refresh-gold 只能配 --from-version 用：不继承的冻结本来就"
            "从 annotations 直接抄金标，没有「带过来的旧金标」这回事"
        )
    if refresh_label and not carrying:
        raise GoldenError(
            "--refresh-label 只能配 --from-version 用：不继承的冻结本来就"
            "从 annotations 直接抄 label，没有「带过来的旧 label」这回事"
        )
    src_manifest: dict[str, Any] = {}
    derived_from: dict[str, Any] = {}
    score_refresh: dict[str, Any] = {"enabled": False}
    gold_refresh: dict[str, Any] = {"enabled": False}
    label_refresh: dict[str, Any] = {"enabled": False}
    if carrying:
        src_vdir = golden_dir / f"v{args.from_version}"
        if not src_vdir.exists():
            raise GoldenError(f"{src_vdir} 不存在；--from-version 只能继承已冻结的版本")
        src_manifest = _load_manifest(src_vdir)
        parts: dict[str, list[dict[str, Any]]] = {}
        for split, fname in SPLIT_FILES.items():
            p = src_vdir / fname
            if p.exists():
                parts[split] = _load_jsonl(p)
        if not parts:
            expected = " / ".join(SPLIT_FILES.values())
            raise GoldenError(
                f"{src_vdir} 下没有 {expected} 任何一个；没有可继承的切分"
            )
        if refresh_gold:
            parts, gold_refresh = _refresh_gold(parts, ann_path)
        if refresh_label:
            parts, label_refresh = _refresh_label(parts, ann_path)
        if refresh_scores:
            parts, score_refresh = _refresh_scores(parts, cand_path)
        else:
            # S3：M-29 之所以存在，是因为「带过来的分数已经过期」这件事
            # **一个字都不报**。继承式冻结默认就带着上一版的分数走，
            # 不喊一声的话，下一版快照的 pred 会被当成当前评分器的输出。
            print(
                f"⚠️ 本次继承式冻结未刷新评分：快照 pred 将是 v{args.from_version} "
                "冻结时的分数，不是当前评分器的输出（要刷新加 --refresh-scores）",
                file=sys.stderr,
            )
        labeled = [r for split in SPLITS for r in parts.get(split, [])]
        derived_from = {
            "version": int(args.from_version),
            "counts": dict(src_manifest.get("counts") or {}),
            "holdout_runs": int(src_manifest.get("holdout_runs") or 0),
        }
    else:
        if not cand_path.exists():
            raise GoldenError(f"{cand_path} 不存在；先跑 candidates")
        if not ann_path.exists():
            raise GoldenError(f"{ann_path} 不存在；先 annotate（冻结只含已人工标注样本）")
        candidates = _load_jsonl(cand_path)
        annotations = _load_jsonl(ann_path)
        labeled = _labeled_rows(candidates, annotations)
        parts = {}

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

    if carrying:
        calib = parts.get(SPLIT_CALIBRATION, [])
        hold = parts.get(SPLIT_HOLDOUT, [])
        reg = parts.get(SPLIT_REGRESSION, [])
        seed = int(src_manifest.get("seed") or 0)
        if reg and args.regression:
            raise GoldenError(
                f"v{args.from_version} 已有 {SPLIT_FILES[SPLIT_REGRESSION]}"
                f"（{len(reg)} 条）；**回归集一经冻结不得重切**（方案 16 C-17）——"
                "重切等于把「原本判对的」换成「这次判对的」，门禁自证为空"
            )
        if args.regression and not reg:
            reg, calib = _regression_slice(
                calib, args.regression, seed=args.regression_seed
            )
    else:
        calib, hold, seed = _stratified_split(
            labeled, args.seed, SPLIT_TARGET, CALIBRATION_RANGE[0], CALIBRATION_RANGE[1]
        )
        reg = []
        if args.regression:
            reg, calib = _regression_slice(
                calib, args.regression, seed=args.regression_seed
            )
    total_rows = len(calib) + len(reg) + len(hold)
    frac = len(calib) / total_rows if total_rows else 0.0
    # 范围判的是「调参池 + 回归集」合计占比：回归集是从调参池里切出去的，
    # 切完再看单档 calibration 必然低于 60%——那不是切坏了，是口径变了。
    pool_frac = (len(calib) + len(reg)) / total_rows if total_rows else 0.0
    out_of_range = not (CALIBRATION_RANGE[0] <= pool_frac <= CALIBRATION_RANGE[1])
    if out_of_range and not args.force:
        raise GoldenError(
            f"当前切分校准占比 {pool_frac:.1%} 不在 [{CALIBRATION_RANGE[0]:.0%}, "
            f"{CALIBRATION_RANGE[1]:.0%}]，且与目标 {SPLIT_TARGET:.0%} 差 "
            f"{abs(pool_frac - SPLIT_TARGET):.1%}pp 超过 5pp；要么补样本到能分层，要么 --force "
            "（强制冻结并如实记录偏差，回调时按偏差重新切）"
        )
    if abs(pool_frac - SPLIT_TARGET) > SPLIT_PP_TOLERANCE and not carrying:
        print(
            f"⚠️ 校准占比 {pool_frac:.1%} 与目标 {SPLIT_TARGET:.0%} 偏差超过 5pp，"
            "已在 manifest 记录；下次回调建议补样本重新切",
            file=sys.stderr,
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "label_schema_version": LABEL_SCHEMA_VERSION,  # §5.2：11 类 → 15 类
        # 尺子编号（方案 16 §1.2 / C-01）：没有这两行，D10 数字无法回答
        # 「是哪版 rubric、哪版 judge prompt 量出来的」，跨版本对比会读成
        # 「质量变好了」而其实换了尺子。
        "rubric_version": RUBRIC_VERSION,
        "evaluator_version": evaluator_version(),
        # **整张评分面**的指纹（代码面 + prompt 面；方案 16 C-11）。
        # 存量 v1–v8 没有这一栏（那时还不写），补不回来——只能靠档外的
        # 版本级台账重算取证。新版从 v9 起**逐版带上**：
        # 「这版的分数是哪版评分器打的」从此是 manifest 里的一行字。
        "scorer_fingerprint": scorer_fingerprint(),
        # holdout 已被消费的次数。冻结时恒 0；**解封一次记一次**。
        # 继承式重冻结**不许归零**——归零等于把封存计数抹掉。
        "holdout_runs": (
            int(src_manifest.get("holdout_runs") or 0) if carrying else 0
        ),
        "version": version,
        "frozen_at": _now_iso(),
        "seed": seed,
        "counts": {
            "candidates": len(candidates) if not carrying else
            int((src_manifest.get("counts") or {}).get("candidates") or 0),
            "annotated": len(labeled),
            "site": len(site),
            "trajectory": len(traj),
            "calibration": len(calib),
            "regression": len(reg),
            "holdout": len(hold),
        },
        "split": {
            "target_calibration": SPLIT_TARGET,
            # ``calibration_ratio`` 是调参池单独占比；``pool_ratio`` 是
            # 「调参池 + 回归集」合计——设计目标是后者（切出回归集之后单看
            # 前者必然掉到 45%，拿它跟 65% 比会说"切坏了"，其实是口径变了）。
            "calibration_ratio": round(frac, 4),
            "pool_ratio": round(pool_frac, 4),
            "regression_ratio": (
                round(len(reg) / total_rows, 4) if total_rows else 0.0
            ),
            "holdout_ratio": round(len(hold) / total_rows, 4) if total_rows else 0.0,
            "pp_diff_from_target": round(abs(pool_frac - SPLIT_TARGET), 4),
            # 三档口径（C-04 / C-17）：头条只用 calibration，回归集按内容指纹
            # 冻住——指纹只盖 id 挡不住"标签被改成与模型一致"，所以盖整行。
            "headline_split": HEADLINE_SPLIT,
            "regression_size": len(reg),
            "regression_seed": int(args.regression_seed),
            "regression_fingerprint": _rows_fingerprint(reg) if reg else "",
        },
        "derived_from": derived_from,
        # 评分是否重挂过（M-29）。``enabled=false`` 时 pred 侧是**上一版冻结时**
        # 的分数——不是当前评分器的输出，读数时必须知道这件事。
        "score_refresh": score_refresh,
        # 金标是否重挂过（填软维度金标）。``baseline_reset=true`` 时本版是
        # **新基线**：``gate`` 拒绝拿它与上一版比（退 2），因为修好/弄坏
        # 只有在同一份金标上才可比。
        "gold_refresh": gold_refresh,
        # D-10 label 是否重挂过（修正 gold_defect 金标错）。与 gold_refresh 同理，
        # ``baseline_reset=true`` 时本版是新基线（label 变了 = 与前版不可比）。
        "label_refresh": label_refresh,
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
    _dump_jsonl(vdir / SPLIT_FILES[SPLIT_CALIBRATION], calib, force=True)
    _dump_jsonl(vdir / SPLIT_FILES[SPLIT_HOLDOUT], hold, force=True)
    # 回归集只在真切出来时才落文件：写一个空的 regression.jsonl 会让
    # 「这一版有没有回归门」变成看文件存在与否之外的第二个判据。
    if reg:
        _dump_jsonl(vdir / SPLIT_FILES[SPLIT_REGRESSION], reg, force=True)
    versions.append(
        {
            "version": version,
            "label_schema_version": LABEL_SCHEMA_VERSION,
            # 索引层也记一份：翻 versions.jsonl 就能看出哪版换了尺子，
            # 不必逐版打开 manifest。
            "rubric_version": RUBRIC_VERSION,
            "frozen_at": manifest["frozen_at"],
            "annotated": len(labeled),
            "calibration": len(calib),
            "regression": len(reg),
            "holdout": len(hold),
            "derived_from": int(derived_from.get("version") or 0),
        }
    )
    _dump_jsonl(versions_path, versions, force=True)
    print(f"→ 已冻结 v{version} 到 {vdir}/")
    if carrying:
        print(
            f"  继承自 v{derived_from['version']}（切分原样带过来，行内容未改）"
        )
        if gold_refresh.get("enabled"):
            print(
                "  ⚠ 金标已按 annotations 重挂："
                f"变动 {gold_refresh.get('changed_rows')} 行"
                "（**金标变动 = 新基线**，与前版不可比；下一对可比是本版与它的后继）"
            )
            for dim in OVERLAY_DIMS:
                dist = (gold_refresh.get("per_dim") or {}).get(dim) or {}
                print(
                    f"    {dim}（金标非空）: "
                    f"{json.dumps(dist.get('before'), ensure_ascii=False)}"
                    f" → {json.dumps(dist.get('after'), ensure_ascii=False)}"
                )
        if label_refresh.get("enabled"):
            shift = label_refresh.get("label_shift") or {}
            print(
                "  ⚠ D-10 label 已按 annotations 重挂："
                f"变动 {label_refresh.get('changed_rows')} 行"
                "（**label 变动 = 新基线**，与前版不可比；下一对可比是本版与它的后继）"
            )
            print(
                "    label 分布: "
                f"{json.dumps(shift.get('before'), ensure_ascii=False)}"
                f" → {json.dumps(shift.get('after'), ensure_ascii=False)}"
            )
        if score_refresh.get("enabled"):
            print(
                "  评分已按当前评分器重挂（golden / code 未动）："
                f"匹配 {score_refresh.get('rows_matched')} 行"
            )
            for dim in OVERLAY_DIMS:
                t = (score_refresh.get("tally") or {}).get(dim) or {}
                print(
                    f"    {dim}: before={json.dumps((score_refresh['per_dim'][dim])['before'], ensure_ascii=False)}"
                    f" → after={json.dumps((score_refresh['per_dim'][dim])['after'], ensure_ascii=False)}"
                    f"（scored {t.get('scored')} / 弃权 {t.get('abstain')} / 无样本 {t.get('no_sample')}）"
                )
        else:
            print("  评分未刷新：pred 侧仍是源版本冻结时的分数（不是当前评分器的输出）")
    hold_frac = f"{len(hold) / total_rows:.1%}" if total_rows else "0.0%"
    reg_note = f" / 回归集 {len(reg)} 条" if reg else ""
    print(
        f"  调参池 {len(calib)} 条（{frac:.1%}）{reg_note}"
        f" / 持有 {len(hold)} 条（{hold_frac}），seed={seed}"
    )
    if reg:
        print(
            f"  回归集指纹 {manifest['split']['regression_fingerprint']}"
            f"（种子 {args.regression_seed}）；一经冻结不得重切（C-17）"
        )
    print(f"  分布：{json.dumps(manifest['by_label'], ensure_ascii=False)}")
    print(
        f"  D10 上岗线 ≥{D10_THRESHOLD:.0%}；头条分母档 = {HEADLINE_SPLIT}"
        f"（回归集 / 持有集旁置，均不进头条）"
    )
    print(
        f"  D9 建议间隔 {RETEST_INTERVAL_DAYS} 天、"
        f"重测 {RETEST_FRACTION[0]:.0%}–{RETEST_FRACTION[1]:.0%}"
    )
    return 0


# --------------------------------------------------------------------------
# D9 一致性（consistency）
# --------------------------------------------------------------------------


def cmd_consistency(args: argparse.Namespace) -> int:
    """D9 单人 test-retest 自我一致性。

    读 annotations.jsonl 中 round1/round2 配对：
    - 硬维度：站点级 label —— Cohen's kappa（上岗线见阈值表 ``t2_hard_kappa``）；
    - 软维度：dimension_scores（1–5）—— 线性加权 kappa（``t2_soft_kappa``）；
    总闸门：加权后按硬维度线（由汇报方综合）。

    上岗线**不在本模块写字面量**——取自 ``evaluation/scorers/thresholds.py``
    （阈值唯一真源，方案 16 C-14 / D-C6）。
    """
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
        f"(上岗线 ≥{thresholds.value('t2_hard_kappa'):g})"
    )
    if soft:
        print(
            "  软维度（1–5 线性加权 kappa，"
            f"上岗线 ≥{thresholds.value('t2_soft_kappa'):.2f}）："
        )
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
# 评分面出处台账（provenance）——方案 16 C-11 补齐
# --------------------------------------------------------------------------
#
# 存量冻结档 v1–v8 的行里**没有** ``scorer_version``，而冻结档**一字不许改**
# ⇒ 逐行版本补不回来。这条命令走**档外**的路：重算当前代码面、逐行比对存档里
# 的读数，把结论落进**版本级台账** ``golden/data/scorer_provenance.jsonl``。
#
# ⚠️ **它不改任何数**：冻结档不写、``candidates.jsonl`` 不写、P1 存档不写。
# 唯一的写目标是台账（可重算的旁路账）。
#
# 重算取证的效力边界：「当前代码面逐行复现了本版读数」证明的是**读数层面
# 的一致**，不是「当时跑的就是这份源码」。可比性要的正是前者。


def _recompute_code_face(
    rows: Sequence[Mapping[str, Any]],
    *,
    p2_root: pathlib.Path,
) -> dict[str, Any]:
    """用**当前**代码面重跑某版的软维度评分，逐行比对存档里的读数。

    返回取证字典（不抛）：

    - ``rows_compared``：至少有一维可比对的行数；
    - ``rows_matched``：比对的维全部一致的行数；
    - ``rows_diverged``：有任一维不一致的行数；
    - ``diverged``：不一致行的明细（``golden_id`` + 逐维 before/after）；
    - ``per_dim``：逐维计数；
    - ``no_sample``：存档里有读数但**样本已取不到**的行数（**不计为一致**，
      也不计为不一致——它是「比不了」）。

    比不了的行**不进分母**：把「取不到样本」算成「一致」会让「没量到」看起来
    像「量过且一致」。
    """
    from trajectory_pipeline.evaluation.scout.io import _score_one  # 延后 import

    per_dim: dict[str, dict[str, int]] = {
        dim: {"compared": 0, "matched": 0, "diverged": 0}
        for dim in OVERLAY_DIMS
    }
    rows_compared = 0
    rows_matched = 0
    diverged: list[dict[str, Any]] = []
    no_sample = 0

    for row in rows:
        stored = row.get("dimension_scores")
        if not isinstance(stored, Mapping) or not stored:
            continue
        sample = _resolve_sample_for_candidate(dict(row), p2_root)
        if sample is None:
            no_sample += 1
            continue
        scores = _score_one(sample)
        recomputed = {
            str(s.get("dimension")): s.get("score") for s in scores
        }
        diffs: dict[str, Any] = {}
        for dim in OVERLAY_DIMS:
            if dim not in stored:
                continue
            before = stored.get(dim)
            after = recomputed.get(dim)
            if before == after:
                per_dim[dim]["compared"] += 1
                per_dim[dim]["matched"] += 1
                continue
            per_dim[dim]["compared"] += 1
            per_dim[dim]["diverged"] += 1
            diffs[dim] = {"stored": before, "recomputed": after}
        if not diffs:
            rows_compared += 1
            rows_matched += 1
            continue
        rows_compared += 1
        diverged.append({
            "golden_id": str(row.get("golden_id") or ""),
            "diffs": diffs,
        })

    return {
        "rows_compared": rows_compared,
        "rows_matched": rows_matched,
        "rows_diverged": len(diverged),
        "no_sample": no_sample,
        "diverged": diverged,
        "per_dim": per_dim,
        "population": "site_rows_with_stored_scores",
    }


def cmd_provenance(args: argparse.Namespace) -> int:
    """核验并登记**版本级评分面出处**（方案 16 C-11 补齐）。

    对每个版本产出三条**面**的记录：

    - ``code``：代码面（D-2/D-4/D-5/D-6）。行里记了指纹 ⇒ 直接读；没记 ⇒
      **重算**当前代码面、逐行比对 ⇒ ``reproduced``（可比）或 ``diverged``
      （**已知不可比**）；
    - ``llm``：prompt 面（D-3/D-9）。从 manifest 的 ``evaluator_version`` 读；
      但**归档里没有 LLM 产的 pred**（overlay 只回填 d2/d4/d5/d6）⇒ 该面与
      本版 pred **无关**（``not_applicable``）；
    - ``collection``：采集侧（D-10 的 ``code.branch``）。判定侧版本**从未
      记录** ⇒ ``unrecorded``（**不猜**）。

    退出码：``0`` 成功；``2`` 测不成（版本不存在 / 台账读不了）。
    """
    from trajectory_pipeline.evaluation.calibration.provenance import (
        FACE_CODE,
        FACE_COLLECTION,
        FACE_LLM,
        STATUS_DIVERGED,
        STATUS_NOT_APPLICABLE,
        STATUS_RECORDED,
        STATUS_REPRODUCED,
        STATUS_UNRECORDED,
        face_record,
        provenance_path,
        read_provenance,
        write_provenance,
    )

    golden_root = pathlib.Path(args.golden_root)
    p2_root = (
        pathlib.Path(args.p2_root) if getattr(args, "p2_root", None)
        else PIPELINE_ROOT
    )
    reg_path = provenance_path(golden_root)
    registry = read_provenance(reg_path)

    versions: list[int] = []
    if getattr(args, "version", 0):
        versions = [int(args.version)]
    else:
        versions = sorted(
            int(p.name[1:])
            for p in golden_root.glob("v[0-9]*")
            if p.is_dir() and p.name[1:].isdigit()
        )
    if not versions:
        print(f"❌ {golden_root} 下没有冻结版本目录（v{N}/）——无从登记", file=sys.stderr)
        return 2

    out_rows: list[dict[str, Any]] = []
    for version in versions:
        vdir = golden_root / f"v{version}"
        if not vdir.is_dir():
            print(f"❌ {vdir} 不存在；--version 只能指向已冻结的版本", file=sys.stderr)
            return 2
        rows: list[dict[str, Any]] = []
        for split in SPLITS:
            f = vdir / SPLIT_FILES[split]
            if f.exists():
                rows.extend(_load_jsonl(f))
        manifest = _load_manifest(vdir)

        # ── code 面 ──────────────────────────────────────────────────
        recorded = {
            str(r.get("scorer_version"))
            for r in rows
            if r.get("scorer_version")
        }
        if recorded:
            # **行数**与**指纹数**是两件事——v9 起行里真会记版本，
            # 两个字段填同一个数的话，「rows_with_scorer_version」会
            # 把「500 行记了同一个版本」读成「只有 1 行记了」。
            rows_recorded = sum(1 for r in rows if r.get("scorer_version"))
            code_parts = {
                code_part(fp) for fp in recorded
            }
            if len(code_parts) > 1:
                print(
                    f"❌ v{version} 的行里记了 {len(code_parts)} 个不同的代码面"
                    f"（{sorted(code_parts)}）——同一版里混着两版评分器，"
                    "出处登记不出来，停下。",
                    file=sys.stderr,
                )
                return 2
            code_face = {
                "version": code_parts.pop(),
                "status": STATUS_RECORDED,
                "comparable": True,
                "evidence": {
                    "rows_with_scorer_version": rows_recorded,
                    "distinct_fingerprints": len(recorded),
                },
                "note": "行里记了 scorer_version（内容指纹）⇒ 直接读",
            }
        else:
            evidence = _recompute_code_face(rows, p2_root=p2_root)
            if evidence["rows_compared"] == 0:
                code_face = {
                    "version": VERSION_UNRECORDED,
                    "status": STATUS_UNRECORDED,
                    "comparable": None,
                    "evidence": evidence,
                    "note": (
                        "本版没有可比对的软维度读数"
                        f"（行里没记 scorer_version；存档里的读数 {evidence['no_sample']} "
                        "行取不到样本）⇒ 该面版本不可知"
                    ),
                }
            elif evidence["rows_diverged"] == 0:
                code_face = {
                    "version": code_scorer_version(),
                    "status": STATUS_REPRODUCED,
                    "comparable": True,
                    "evidence": evidence,
                    "note": (
                        "当前代码面**逐行复现**了本版的软维度读数 ⇒ "
                        "这批读数与当前代码面可比"
                    ),
                }
            else:
                code_face = {
                    "version": VERSION_UNRECORDED,
                    "status": STATUS_DIVERGED,
                    "comparable": False,
                    "evidence": evidence,
                    "note": (
                        f"当前代码面**复现不出**本版的软维度读数"
                        f"（{evidence['rows_diverged']} / {evidence['rows_compared']} "
                        "行不一致）⇒ **已知不可比**（当时跑的是哪份源码没记，"
                        "但「不是同一版」是确定的）"
                    ),
                }

        # ── llm 面 ───────────────────────────────────────────────────
        llm_version = str(manifest.get("evaluator_version") or "")
        llm_face = {
            "version": llm_version or VERSION_UNRECORDED,
            "status": STATUS_RECORDED if llm_version else STATUS_UNRECORDED,
            "comparable": None,
            "evidence": {},
            "note": (
                "本版 manifest 记的 prompt 面；**归档里没有 LLM 面产出的 pred**"
                "（overlay 只回填 d2/d4/d5/d6）⇒ 该面与本版 pred 无关"
                if llm_version else
                "本版 manifest 没记 prompt 面（v6 及更早）⇒ 不可知"
            ),
        }

        # ── collection 面 ────────────────────────────────────────────
        collection_face = {
            "version": None,
            "status": STATUS_UNRECORDED,
            "comparable": None,
            "evidence": {},
            "note": (
                "branch 由**采集时**判定（``code.source`` 可能是 ``llm``——"
                "感知层也是 LLM 判的），判定侧版本从未被指纹化 ⇒ 不可判定；"
                "且重采会换 run_id，不是同一总体，不能拿新判的来比"
            ),
        }

        out_rows.append({
            "version": version,
            "method": "recompute" if not recorded else "recorded",
            "faces": {
                FACE_CODE: code_face,
                FACE_LLM: llm_face,
                FACE_COLLECTION: collection_face,
            },
            "verified_at": _now_iso(),
            "note": (
                "版本级出处台账（方案 16 C-11 补齐）：冻结档一字未改，"
                "结论由重算取证"
            ),
        })

    merged = {int(r["version"]): r for r in registry.values() if "version" in r}
    for r in out_rows:
        merged[int(r["version"])] = r
    if getattr(args, "dry_run", False):
        for r in out_rows:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        print(f"[dry-run] 将写 {reg_path}")
        return 0
    write_provenance(reg_path, merged.values())
    print(f"→ 已写出处台账 {reg_path}（{len(out_rows)} 版）")
    for r in out_rows:
        code = r["faces"][FACE_CODE]
        print(
            f"  v{r['version']}: code 面 {code['status']}"
            f"（版本 {code['version']}，可比={code['comparable']}）"
        )
    return 0


# --------------------------------------------------------------------------
# D10 一致率（agreement）
# --------------------------------------------------------------------------


def _load_manifest(vdir: pathlib.Path) -> dict[str, Any]:
    """读冻结版 manifest；缺失 / 损坏都返回 ``{}``，**不抛**。

    v6 及更早的冻结档没有 ``rubric_version`` / ``evaluator_version`` /
    ``holdout_runs`` 三个字段。测量这类历史版本时应当如实报「未记录」，
    而不是拿当前值顶上去——顶上去等于把新尺子的编号贴在旧数字旁边。
    """
    path = vdir / "manifest.json"
    if not path.exists():
        return {}
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _read_provenance_registry(
    golden_root: pathlib.Path,
) -> dict[int, dict[str, Any]]:
    """读**版本级出处台账**（方案 16 C-11 补齐）；读不到 → 空字典。

    台账**读坏了**（行不是合法 JSON）→ **大声报出来并按「没有台账」处理**，
    **不静默吞掉**：台账是旁路账，读坏了不影响测量本身（出处栏如实写
    ``unrecorded``），但读的人必须知道「台账此刻不可信」。
    """
    from trajectory_pipeline.evaluation.calibration.provenance import (
        provenance_path,
        read_provenance,
    )

    path = provenance_path(golden_root)
    try:
        return read_provenance(path)
    except ValueError as exc:
        print(
            f"⚠️ 出处台账 {path} 读坏了（{exc}）——本次测量按「没有台账」处理，"
            "出处栏一律记 unrecorded。修好台账再跑一次 `provenance` 即可。",
            file=sys.stderr,
        )
        return {}


def _sample_on_disk_map(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, bool]:
    """``golden_id → 该行对应的样本现在还能不能从盘上取到``。

    判据（两种来源分开判）：

    - ``archive_outcome``：P1 存档文件在盘上**且**能从里面切出同 url 的站点
      Sample——存档在但样本已被裁掉，也算取不到；
    - ``negative_pool``：负样本池文件里有这一行。

    这一项**真的查了**，所以取不到就是 ``False``（明确），不是 ``None``
    （未检查）——两者混成同一个值，读的人会把「查了没有」当成「查了，没有」。
    """
    by_id = {str(r.get("golden_id") or ""): r for r in rows}
    need: dict[str, list[str]] = collections.defaultdict(list)
    want_urls: dict[str, set[str]] = collections.defaultdict(set)
    neg_ids: list[str] = []
    for row in rows:
        gid = str(row.get("golden_id") or "")
        kind = str(row.get("source_kind") or "")
        if kind == "archive_outcome":
            key = f"{row.get('task_id') or ''}__{row.get('run_id') or ''}"
            need[key].append(gid)
            want_urls[key].add(str(row.get("url") or ""))
        elif kind == "negative_pool":
            neg_ids.append(gid)

    out: dict[str, bool] = {}
    for key, gids in need.items():
        path = PIPELINE_ROOT / f"{key}.json"
        present: set[str] = set()
        if path.exists():
            try:
                from trajectory_pipeline.assembler.schema import split_archive

                for s in split_archive(path):
                    outcome = getattr(s, "outcome", None)
                    if outcome is not None:
                        present.add(str(getattr(outcome, "url", "") or ""))
            except Exception:  # noqa: BLE001 - 存档读不出来 = 取不到，不炸
                present = set()
        for gid in gids:
            url = str((by_id.get(gid) or {}).get("url") or "")
            out[gid] = bool(present) and url in present

    if neg_ids:
        neg_urls: set[str] = set()
        neg_path = PIPELINE_ROOT / "negative.jsonl"
        if neg_path.exists():
            for line in neg_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    neg_urls.add(str(json.loads(line).get("url") or ""))
                except json.JSONDecodeError:
                    continue
        for gid in neg_ids:
            url = str((by_id.get(gid) or {}).get("url") or "")
            out[gid] = bool(url) and url in neg_urls
    return out


def _emit_url_conflicts_markdown(
    *,
    path: str,
    conflicts: Mapping[str, Mapping[str, Any]],
    leakage: Mapping[str, Any],
    generated_by: str,
) -> pathlib.Path:
    """落 URL 级金标冲突清单（机器事实块 + 人填的两列结论）。

    **机器重跑不抹掉人的结论**：先回读旧文件里的「定性」「裁定依据」两列，
    再连同刷新后的事实块一起写回；旧文件认不出的节按新事实重起一节，
    结论列留空等人填（fail-closed：没人裁过的东西不许机器替他裁）。
    """
    from trajectory_pipeline.evaluation.calibration.urlconflict import (
        parse_verdicts_markdown,
        render_conflicts_markdown,
    )

    p = pathlib.Path(path)
    verdicts: dict[str, dict[str, str]] = {}
    if p.exists():
        verdicts = parse_verdicts_markdown(p.read_text(encoding="utf-8"))
    text = render_conflicts_markdown(
        conflicts,
        leakage=leakage,
        verdicts=verdicts,
        generated_by=generated_by,
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    multi = {u for u, b in conflicts.items() if len(b.get("labels") or {}) > 1}
    filled = sum(1 for u in multi if (verdicts.get(u) or {}).get("定性"))
    try:
        shown: Any = p.relative_to(pathlib.Path.cwd())
    except ValueError:
        shown = p
    print(
        f"→ 已写 {shown}（{len(multi)} 个多头 URL；"
        f"已填定性 {filled} 个 / 待填 {len(multi) - filled} 个）"
    )
    return p


def _emit_calibration_artifacts(
    *,
    rows: Sequence[Mapping[str, Any]],
    version: int,
    rubric_version: str,
    tolerance: int,
    target_dir: str,
    snapshot: Mapping[str, Any] | None = None,
    cards: Sequence[Mapping[str, Any]] | None = None,
    force_snapshot: bool = False,
    provenance: Mapping[int, Mapping[str, Any]] | None = None,
) -> tuple[pathlib.Path, pathlib.Path]:
    """落「全量快照 + 分歧卡」两份派生数据（方案 16 C-15 / C-02）。

    为什么两份一起出：回归判定问「上一轮对、这一轮错」，那一行在上一轮的
    分歧卡里不存在 ⇒ 只存卡判不了回归，必须先有全量快照（见
    ``evaluation/calibration/snapshot.py`` 模块 docstring）。两者由同一次
    计算派生，口径不会分叉。

    **只读派生**：不回写 ``v{N}/*.jsonl``，产物落 ``output/``（gitignore 内）。

    ``snapshot`` / ``cards`` 可由调用方**预计算后传入**（``cmd_agreement``
    要拿同一份快照算 URL 级读数，同一份东西算两遍只会多一次漂移的机会）。

    ``provenance`` 是**版本级出处台账**（方案 16 C-11 补齐）：给存量冻结档
    （行里没记 ``scorer_version``）补「那一面的版本是哪版」。台账没有的
    ⇒ ``unrecorded``，**不拿当前指纹顶替**。

    **快照已存在时不静默覆盖**（M-29 S3′）：重跑一次 ``agreement --version N``
    就会把历史那份快照重新盖章成今天的 ``evaluator_version``——那份文件是
    「上一轮判了什么」的唯一凭据，盖掉等于把回归判定的地基抽了。
    要重算得显式 ``--force-snapshot``。
    """
    from trajectory_pipeline.evaluation.calibration.disagreement import (
        cards_from_snapshot,
        summarize_cards,
        write_cards,
    )
    from trajectory_pipeline.evaluation.calibration.snapshot import (
        build_snapshot,
        summarize_snapshot,
        write_snapshot,
    )

    if not version:
        raise GoldenError(
            "--emit-disagreements 需要 --version：快照必须绑定冻结版本，"
            "否则「上一轮这一行判了什么」无从对齐（方案 16 C-15）"
        )
    if snapshot is None:
        snapshot = build_snapshot(
            rows,
            golden_version=version,
            success_label=SUCCESS_LABEL,
            rubric_version=rubric_version,
            evaluator_version=evaluator_version(),
            tolerance=tolerance,
            provenance=provenance,
            code_version=code_scorer_version(),
        )
    outdir = pathlib.Path(target_dir) / f"v{version}"
    snap_path = outdir / "snapshot.json"
    if snap_path.exists() and not force_snapshot:
        raise GoldenError(
            f"{snap_path} 已存在；**不静默覆盖**（重盖章等于把「上一轮判了什么」"
            "的凭据改掉）。要重算加 --force-snapshot，或换个 --emit-disagreements 目录"
        )
    snap_path = write_snapshot(snap_path, snapshot)
    if cards is None:
        cards = cards_from_snapshot(snapshot)
    cards_path = write_cards(outdir / "disagreements.jsonl", cards)

    stats = summarize_snapshot(snapshot)
    cstats = summarize_cards(cards)
    verdict_str = " / ".join(
        f"{k}={v}" for k, v in stats["by_verdict"].items() if v
    )
    delta_str = " / ".join(f"{k}={v}" for k, v in cstats["by_delta_kind"].items())
    print(
        f"→ 已写快照 {snap_path}（{stats['entries']} 条：{verdict_str}）"
        f" · 分歧卡 {cards_path}（{cstats['n']} 张：{delta_str or '无'}）"
    )
    return snap_path, cards_path


def cmd_agreement(args: argparse.Namespace) -> int:
    """D10 = 口径 C：代码判定 == 黄金集人工标签 / **调参池**样本数（≥90%）。

    站点级样本才有 code，轨迹级样本不计入。另出下钻口径 D（分支路由准确率，
    micro precision/recall/F1）。

    **分母口径（方案 16 C-04）**：头条只用 ``calibration`` 档；``regression``
    与 ``holdout`` 旁置、只报条数。同时打印"旧口径（三档合计）"——分母一换，
    历史数字必须还能对齐，否则跨版本对比会读成"质量暴跌"。老冻结档
    （v6 及更早）没有 regression.jsonl，这一行就是它原来的头条。"""
    golden_root = pathlib.Path(args.golden_root)
    manifest_meta: dict[str, Any] = {}
    split_rows: dict[str, list[dict[str, Any]]] = {}
    if args.version:
        vdir = golden_root / f"v{args.version}"
        rows = []
        for split in SPLITS:
            f = vdir / SPLIT_FILES[split]
            if not f.exists():
                continue
            loaded = _load_jsonl(f)
            split_rows[split] = loaded
            for row in loaded:
                # 标注 ``__split__``：软维度只走 calibration（plan §三
                # 第一步 contract 5「calibration 优先于 holdout」）、头条
                # 只认 calibration（C-04）、快照与分歧卡按档打标。
                row["__split__"] = split
                rows.append(row)
        if not rows:
            raise GoldenError(f"{vdir} 下没有样本；先 freeze")
        manifest_meta = _load_manifest(vdir)
        label_note = f"冻结版本 v{args.version}"
        # 回归集 diff 守卫（C-17）：冻结时记了整行指纹，读的时候重算一遍。
        # 标签被改成"与模型一致"是最省力的作弊，也是 `regressed == 0` 唯一
        # 测不出来的那种；指纹对不上就当场炸，不静默放过。
        want = str((manifest_meta.get("split") or {}).get("regression_fingerprint") or "")
        if want:
            got = _rows_fingerprint(split_rows.get(SPLIT_REGRESSION, []))
            if got != want:
                raise GoldenError(
                    f"v{args.version} 的回归集内容与冻结时不一致（manifest "
                    f"{want} / 实际 {got}）：{SPLIT_FILES[SPLIT_REGRESSION]} "
                    "不是在 freeze 之后改的吧？回归集不得改动（C-17）"
                )
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
    # 对齐方案 §4.3：口径 C 升级为「语义等价类一致」——新细分分支与
    # 历史合并态（如 blocked vs login_wall_or_blocked）判一致，避免
    # 标签域扩展当天 D10 假性暴跌。映射只有 alignment.EQUIV_CLASS 一份。
    from trajectory_pipeline.evaluation.golden.alignment import (
        equiv_class,
        is_equivalent,
        validate_soft_dim_name,
    )

    def _score(subset: Sequence[Mapping[str, Any]]) -> tuple[int, float]:
        t = [gold_label_of(r) for r in subset]
        p = [code_branch_of(r) for r in subset]
        n_ok = sum(1 for a, b in zip(t, p) if is_equivalent(a, b))
        return n_ok, (n_ok / len(subset) if subset else 0.0)

    # 头条 = 调参池（C-04）。未冻结口径没有 ``__split__``，退化为全量算——
    # 那是"还没切档"，不是"调参池为空"，两者不能混成同一个 0。
    has_split = any(r.get("__split__") for r in site)
    headline = (
        [r for r in site if r.get("__split__") == HEADLINE_SPLIT]
        if has_split
        else list(site)
    )
    if has_split and not headline:
        raise GoldenError(
            f"{label_note} 的 {HEADLINE_SPLIT} 档站点级样本为 0；头条分母为空——"
            "先核对切分文件，不要用旁置档顶替头条"
        )
    agree, ratio = _score(headline)
    # 旧口径（三档合计）：分母口径变更（C-04）之后必须同时出，否则跨版本
    # 对比会从 289 掉到 188 而被读成"质量暴跌"。
    legacy_agree, legacy_ratio = _score(site)
    # 下钻口径 D 的分支路由 P/R/F1 也基于等价类标签算，与口径 C 同一把尺；
    # 细分 err 仍可从原始标签（y_true / y_pred）下钻查看。
    report = classification_report(
        [equiv_class(gold_label_of(r)) for r in headline],
        [equiv_class(code_branch_of(r)) for r in headline],
    )

    # ── 软维度一致率（plan §三·第一步） ─────────────────────────
    # 软维度只走 calibration set；holdout 留给最终评估（contract 5）。
    # ``--soft-dim`` 为空 → 自动从 gold 侧 ``golden.dimension_scores``
    # 出现的 dim 里挑白名单内的（contract 1）；显式指定 → 白名单校验
    # （contract 1 fail-closed）。
    _t = getattr(args, "dim_tolerance", None)
    tolerance = (
        int(_t)
        if _t is not None
        else int(thresholds.value("soft_dim_tolerance"))
    )
    requested_dims = list(getattr(args, "soft_dim", []) or [])
    for d in requested_dims:
        try:
            validate_soft_dim_name(d)
        except ValueError as exc:
            # 白名单外 → fail-closed 转 GoldenError，exit code 2
            raise GoldenError(str(exc)) from exc
    soft = _compute_soft_dim_agreement(
        site, requested_dims=requested_dims, tolerance=tolerance,
    )

    print(
        f"D10 与黄金集标签一致率（{label_note}，口径 C = 语义等价类一致）"
    )
    print(
        f"  口径 C（头条，分母 = {HEADLINE_SPLIT if has_split else '全部站点级'} "
        f"{len(headline)} 条）：{agree}/{len(headline)} = {ratio:.1%}  "
        f"{f'✅ ≥{D10_THRESHOLD:.0%} 上岗' if ratio >= D10_THRESHOLD else f'❌ 未达 {D10_THRESHOLD:.0%} 上岗线'}"
    )
    if has_split:
        print(
            f"  口径 C（旧口径，三档合计 {len(site)} 条）："
            f"{legacy_agree}/{len(site)} = {legacy_ratio:.1%}"
            "   ← 与 v6 及更早的报表对齐用（C-04 换分母，不是质量变了）"
        )
    print(
        "  下钻口径 D（分支路由，micro P/R/F1）："
        f"P={_fmt_opt(report['micro']['precision'])}  "
        f"R={_fmt_opt(report['micro']['recall'])}  "
        f"F1={_fmt_opt(report['micro']['f1'])}"
    )

    # ── 尺子编号与分母构成（方案 16 §1.2 / C-01） ───────────────
    # 三件事必须和数字一起出现，否则「这批数是谁量的」答不出：
    #   ① rubric 版本（人写，来自 manifest；老冻结档没有就如实报"未记录"）；
    #   ② judge 指纹（机器算，当前代码的 D-3/D-9 prompt 状态）；
    #   ③ D10 的 pred 侧来源——**不是当前评分器**，是冻结行里采集时写下的
    #      code.branch。把 (2) 当成 D10 的来源是这类报表最常见的误读。
    ruler_rubric = str(manifest_meta.get("rubric_version") or "")
    ruler_at_freeze = str(manifest_meta.get("evaluator_version") or "")
    holdout_runs = int(manifest_meta.get("holdout_runs") or 0)
    rows_by_split = {
        split: sum(1 for r in rows if r.get("__split__") == split) for split in SPLITS
    }
    site_by_split = {
        split: sum(1 for r in site if r.get("__split__") == split) for split in SPLITS
    }
    hold_site_n = site_by_split[SPLIT_HOLDOUT]
    regression_site_n = site_by_split[SPLIT_REGRESSION]

    if ruler_rubric:
        ruler_desc = f"rubric v{ruler_rubric}"
    elif args.version:
        ruler_desc = "rubric 版本未记录（v6 及更早冻结档无此字段）"
    else:
        ruler_desc = "rubric 版本未记录（未冻结口径，无 manifest）"
    print(
        f"  尺子：{ruler_desc} · judge 指纹 {evaluator_version()}"
        f" · 冻结时指纹 {ruler_at_freeze or '未记录'} · holdout_runs={holdout_runs}"
    )
    print("  D10 pred 侧 = 冻结行 code.branch（采集时写下，**不由当前评分器重算**）")
    if has_split:
        aside = " · ".join(
            f"{s} {site_by_split[s]} 条" for s in SPLITS if s != HEADLINE_SPLIT
        )
        print(f"  旁置（不进头条）：{aside or '无'}")
    if hold_site_n:
        print(
            f"  🔒 持有集 {hold_site_n} 条只报条数、不报一致率——封存纪律（D-C5）；"
            "最终验收才跑一次，跑一次 holdout_runs +1"
        )
    if regression_site_n:
        print(
            f"  🛡 回归集 {regression_site_n} 条不进头条：「原本判对的现在判错」由"
            " verify 判（C-08），这里不解读"
        )

    # 软维度打印：N=0 的 dim **不报**（plan §三·第一步 contract 3「空样本
    # 不参与」）——输出 "0/0 = 0%" 是误导，dim 还没上场就别预占位。
    print(
        f"\n  软维度一致率（calibration；容忍档差 ±{tolerance}；"
        "N=0 的 dim 不报——黄金集还没填这个维度的就别刻）"
    )
    if not soft:
        print("    ⚠ 无任何软维度可算——黄金集 dimension_scores 全为空或全在 holdout。"
              "  本次不发阈值（plan §三·第一步风险「首次跑只发 README 不准出数字」）")
    else:
        for dim, st in soft.items():
            n = st["n"]
            ratio_dim = st["agreement_ratio"]
            tol_str = f"|t-p|≤{tolerance}"
            mark = "  ⚠ 阈值未设（plan §三·第一步：先跑分布再定）"
            print(
                f"    {dim}: {st['agreed']}/{n} = {ratio_dim:.1%}  "
                f"({tol_str}){mark}"
            )

    # ── URL 级金标读数（M-30）──────────────────────────────────────
    # 两件已裁定的事都会让「按 URL 读」与「按行读」对不上，这里把差在哪摆
    # 出来，**只摆事实，不判缺陷**：
    #   ① 同一 URL 上的 d10 金标可能多头 —— 金标粒度按访问（Q-11）时那是
    #      **正常事实**（站点漂移 / 地区 / 登录态），不是标注缺陷；
    #   ② 切档按行（Q-12）⇒ 持有集可能有行与调参池/回归集同 URL ⇒ 持有集
    #      不独立，「持有集 N 条」不能读成「N 条独立证据」。
    from trajectory_pipeline.evaluation.calibration.disagreement import (
        cards_from_snapshot,
    )
    from trajectory_pipeline.evaluation.calibration.snapshot import (
        HARD_DIM,
        build_snapshot,
    )
    from trajectory_pipeline.evaluation.calibration.urlconflict import (
        URL_UNKNOWN,
        project_d10_rows,
        split_leakage,
        summarize_conflicts,
        url_gold_conflicts,
    )

    snapshot = build_snapshot(
        rows,
        golden_version=int(args.version or 0),
        success_label=SUCCESS_LABEL,
        rubric_version=ruler_rubric,
        evaluator_version=evaluator_version(),
        tolerance=tolerance,
        provenance=_read_provenance_registry(golden_root),
        code_version=code_scorer_version(),
    )
    cards = cards_from_snapshot(snapshot)
    rows_by_id = {str(r.get("golden_id") or ""): r for r in rows}
    url_of = {gid: str(r.get("url") or "") for gid, r in rows_by_id.items()}
    projected = project_d10_rows(
        [e for e in snapshot.get("entries", []) if e.get("dimension") == HARD_DIM],
        rows_by_id,
        on_disk=_sample_on_disk_map(rows),
    )
    conflicts = url_gold_conflicts(projected, url_of)
    csum = summarize_conflicts(conflicts)
    leak = split_leakage(projected, url_of)
    multi_urls = {
        u for u, b in conflicts.items() if len(b.get("labels") or {}) > 1
    }
    on_conflict = sum(
        1
        for c in cards
        if url_of.get(str(c.get("golden_id") or ""), URL_UNKNOWN) in multi_urls
    )
    print("\n  URL 级金标读数")
    # 分母为 0 时不写 "0/0"——那个比值是误导（与「N=0 的软维度不报」同款）。
    card_part = (
        f"本轮分歧卡 {on_conflict}/{len(cards)} 张落其上"
        if cards
        else "本轮无分歧卡"
    )
    if csum["urls_with_gold"]:
        print(
            f"    同 URL 多头金标：{csum['urls_multi_label']}/"
            f"{csum['urls_with_gold']} 个 URL（跨档 "
            f"{leak['cross_split_urls']} 个；{card_part}）"
        )
    else:
        print("    同 URL 多头金标：无带 d10 金标的 URL，无可统计项")
    if leak["holdout_rows"]:
        print(
            f"    切档按行 ⇒ 持有集 {leak['holdout_rows_on_cross_split_urls']}/"
            f"{leak['holdout_rows']} 行与调参池/回归集同 URL"
            "（持有集不独立，所有持有集读数须带此附注）"
        )
    else:
        print("    切档按行 ⇒ 未切档（无持有集可算泄漏）")
    print(
        "    金标粒度按访问（Q-11）⇒ 同一 URL 多头标签是正常事实，"
        "此处只标注不判缺陷；逐 URL 的定性见 golden/data/url_gold_conflicts.md"
    )

    if args.emit_disagreements is not None:
        _emit_calibration_artifacts(
            rows=rows,
            version=args.version,
            rubric_version=ruler_rubric,
            tolerance=tolerance,
            target_dir=args.emit_disagreements,
            snapshot=snapshot,
            cards=cards,
            force_snapshot=bool(getattr(args, "force_snapshot", False)),
            provenance=_read_provenance_registry(golden_root),
        )

    if getattr(args, "emit_url_conflicts", ""):
        _uc_path = pathlib.Path(args.emit_url_conflicts)
        try:
            _uc_shown = str(_uc_path.relative_to(pathlib.Path.cwd()))
        except ValueError:
            _uc_shown = str(_uc_path)
        _emit_url_conflicts_markdown(
            path=args.emit_url_conflicts,
            conflicts=conflicts,
            leakage=leak,
            generated_by=(
                "python -m trajectory_pipeline.evaluation.golden.builder "
                f"agreement --version {args.version or 0} "
                f"--emit-url-conflicts {_uc_shown}"
            ),
        )

    if args.json_out:
        out = {
            "label_note": label_note,
            # ``n`` / ``agreed`` / ``agreement_ratio`` = **头条**（调参池，C-04）。
            "n": len(headline),
            "agreed": agree,
            "agreement_ratio": ratio,
            "threshold": D10_THRESHOLD,
            "criterion": "equiv_class",  # 口径 C：语义等价类一致（§4.3）
            "label_schema_version": LABEL_SCHEMA_VERSION,
            # 尺子编号（方案 16 C-01）。老冻结档缺字段时给空串，不拿当前值顶替
            # ——顶替等于把新尺子的编号贴在旧数字旁边。
            "rubric_version": ruler_rubric,
            "evaluator_version": evaluator_version(),  # 当前代码的 D-3/D-9 prompt 指纹
            "evaluator_version_at_freeze": ruler_at_freeze,
            "code_branch_provenance": "frozen_row",  # D10 pred 侧来源
            "holdout_runs": holdout_runs,
            "rows_by_split": rows_by_split,
            "site_rows_by_split": site_by_split,
            "d10_denominator": {
                "n": len(headline),
                "split": HEADLINE_SPLIT if has_split else "all",
                "excluded_site_rows": {
                    s: site_by_split[s]
                    for s in SPLITS
                    if s != HEADLINE_SPLIT and site_by_split[s]
                },
            },
            # 旧口径（三档合计）：换分母之前头条就是它。留着做跨版本对齐，
            # 不是为了"两个数取好看的哪个"——两个数都在，谁降谁升一目了然。
            "legacy_denominator": {
                "n": len(site),
                "agreed": legacy_agree,
                "agreement_ratio": legacy_ratio,
                "criterion": "all_splits",
            },
            "report": report,
            # plan §三·第一步 contract 7「输出 JSON 含 soft_dims 键」——
            # 不出数字的 dim 不进 dict（N=0 不计入，contract 3）。
            # contract 6「manifest_schema_version 不破」——上层的
            # ``label_schema_version`` 字段不动；soft dim 接口扩在
            # ``soft_dims`` 子字典，跨字段不影响。
            "soft_dims": soft,
            "soft_dim_tolerance": tolerance,
            "soft_dim_criterion": (
                f"|t-p| <= {tolerance} (rubric 软维度 1-5)"
            ),
            # URL 级金标读数（M-30）。两件裁定过的事在这里摆事实：
            # 金标按访问 ⇒ 同一 URL 多头标签是正常事实；切档按行 ⇒ 持有集
            # 有跨档泄漏。**都只是读数，不判缺陷**（定性由人做）。
            "url_gold_conflicts": {
                "summary": csum,
                "leakage": leak,
                "conflicts": conflicts,
            },
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


def _sorted_score_dist(counter: "collections.Counter") -> dict[Any, int]:
    """评分分布的落定序：条数降序，``None``（弃权）排在最后。

    顺序写死是为了**每次跑出来的读数能直接 diff**——分布对不上时，
    顺序不固定就分不清是真变了还是只是打印顺序变了。
    """
    return {
        k: counter[k]
        for k in sorted(counter, key=lambda x: (x is None, -counter[x]))
    }


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
    print("评分分布（仅已 scored 的样本；None = 该维评分器弃权，不计入分母）：")
    for dim in OVERLAY_DIMS:
        if dim in score_dists:
            print(f"  {dim}: {_sorted_score_dist(score_dists[dim])}")


# --------------------------------------------------------------------------
# overlay：把 rubric 四维评分回填到现有 candidates.jsonl
# --------------------------------------------------------------------------
#
# 为什么单独开 subcommand 而不是塞进 from-scd：
#   from-scd 是「从 scout 三件套 → 全量重生成 candidates」，会丢人工标注
#   与历史 freeze 链路；overlay 只挂评分不动 code.branch，保留现有 D10
#   校准过的分支结论。两条命令职责分离：
#     from-scd    → 候选重生成（清老/P2 重切 / scout 改判时用）
#     overlay-scout → 评分回填（保留 code，仅补 dimension_scores）
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
            # 显式写空，**不用 setdefault**：上一轮留下的旧分数若留在行里，
            # 「这行现在没样本」这件事就看不见了（与 S1 要修的是同一类病）。
            if row.get("source_kind") == "negative_pool":
                row["dimension_scores"] = {}
                row["scores_detail"] = []
                row["status"] = "negative_pool_no_sample"
                counters["skipped"] += 1
            else:
                row["dimension_scores"] = {}
                row["scores_detail"] = []
                row["status"] = "missing_sample"
                counters["missing"] += 1
            # 没有分数 ⇒ 没有「哪版评分器打的」可言。**显式写空**，
            # 别让上一轮留下的 ``scorer_version`` 留在行里——那会把
            # 「这行现在没分数」伪装成「这行有分数且版本是 X」。
            row["scorer_version"] = None
            out_rows.append(row)
            continue
        scores = _score_one(sample)
        # 弃权维度写 ``null``，**不删键**。
        # 删键时「评分器判不可判定」与「压根没测」在快照里**长得一模一样**
        # （都是条目消失）——M-29 审计的正是这个：弃权行无声消失，读数对不上
        # 也看不出异常。键齐全 = 有样本；值为 ``None`` = 该维不可判定
        # （下游 ``dim_agreement`` 遇 None 不计入分母，与「0 分」不是一回事）。
        dim_scores: dict[str, int | None] = {dim: None for dim in OVERLAY_DIMS}
        for s in scores:
            dim = s.get("dimension")
            if dim not in dim_scores:
                continue
            raw = s.get("score")
            dim_scores[dim] = int(raw) if raw is not None else None
        row["dimension_scores"] = dim_scores
        row["scores_detail"] = scores
        row["status"] = "scored"
        # C-11：这批分数是**哪版评分器**打的，写进行里。少了这一栏，
        # 「旧 prompt 还是新 prompt 判的」在分歧卡上就答不出来——
        # 而答不出来这件事本身会藏在「看起来一样」的分数里。
        row["scorer_version"] = scorer_fingerprint()
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
        "--dimensions-only",
        action="store_true",
        help=(
            "只合并软维度分数（方案 16 · 填软维度金标）：输入可带"
            "`{golden_id, dimension, gold_score}` 的工作表行或"
            "`{golden_id, dimension_scores}` 的补丁；**label 一字不碰**，"
            "`reviewed_by` 记进 `dimension_reviewed_by`（不覆盖标签署名人）"
        ),
    )
    p_ann.add_argument(
        "--force",
        action="store_true",
        help="允许覆盖：整行模式 = label 不一致时；--dimensions-only = 已有分数不同时",
    )
    p_ann.set_defaults(func=cmd_annotate)

    p_frz = sub.add_parser(
        "freeze", help="版本化冻结 + 调参池/回归集/持有集三档切分"
    )
    p_frz.add_argument("--version", type=int, default=0, help="版本号；缺省自动 +1")
    p_frz.add_argument("--seed", type=int, default=42, help="分层切分随机种子")
    p_frz.add_argument(
        "--from-version",
        type=int,
        default=0,
        metavar="N",
        help=(
            "继承 vN 的切分**原样重冻结**（行内容不动，只补/带回归集与三档口径）。"
            "切分不读 candidates/annotations——回头重切会重新洗牌，"
            "「v7 与 v6 逐行可比」就没了；给了 --refresh-scores 时**只**读 candidates "
            "的三个评分字段。源版本已有 regression.jsonl 时原样带过来。"
        ),
    )
    p_frz.add_argument(
        "--refresh-scores",
        action="store_true",
        help=(
            "继承式冻结时把每行的 pred 侧评分按 golden_id 从 candidates.jsonl 重挂"
            "（dimension_scores / scores_detail / status；golden 与 code 不动）。"
            "**不给它就带着上一版的旧分数走**，快照 pred 那一刻不是当前评分器的"
            "输出（M-29）。只能配 --from-version 用。"
        ),
    )
    p_frz.add_argument(
        "--refresh-gold",
        action="store_true",
        help=(
            "继承式冻结时把每行的 **gold 侧软维度分数**按 golden_id 从 "
            "annotations.jsonl 重挂（只动 golden.dimension_scores；"
            "label / code / pred 侧不动）。**金标变动 = 新基线**：manifest 落 "
            "`gold_refresh.baseline_reset=true`，gate 拒绝拿本版与上一版比（退 2）。"
            "只能配 --from-version 用。"
        ),
    )
    p_frz.add_argument(
        "--refresh-label",
        action="store_true",
        help=(
            "继承式冻结时把每行的 **gold 侧 D-10 label**按 golden_id 从 "
            "annotations.jsonl 重挂（只动 golden.label / label_label / "
            "reviewed_by / reviewed_at / note；dimension_scores / code / pred "
            "侧不动）。**label 变动 = 新基线**：manifest 落 "
            "`label_refresh.baseline_reset=true`，gate 拒绝拿本版与上一版比（退 2）。"
            "只能配 --from-version 用。"
        ),
    )
    p_frz.add_argument(
        "--regression",
        type=int,
        default=0,
        metavar="K",
        help=(
            f"从调参池切 K 条站点级行作回归集（黄金校准集；D-C8 建议 {REGRESSION_SIZE}）。"
            "默认 0 = 不切（manifest 里如实记 0，不静默假定已切）。"
            "源版本已有回归集时再给本选项直接报错：回归集一经冻结不得重切。"
        ),
    )
    p_frz.add_argument(
        "--regression-seed",
        type=int,
        default=REGRESSION_SEED,
        help=f"回归集抽样种子（默认 {REGRESSION_SEED}；与切校准/持有的 --seed 分开）",
    )
    p_frz.add_argument("--dry-run", action="store_true", help="只输出 manifest 不落盘")
    p_frz.add_argument(
        "--force", action="store_true", help="切分偏差超 5pp 时也强制冻结（如实记录）"
    )
    p_frz.set_defaults(func=cmd_freeze)

    p_con = sub.add_parser(
        "consistency", help="D9 单人 test-retest 一致性（读当前 annotations.jsonl）"
    )
    p_con.set_defaults(func=cmd_consistency)

    p_prov = sub.add_parser(
        "provenance",
        help=(
            "登记**版本级评分面出处**（方案 16 C-11 补齐）：冻结行没记 "
            "scorer_version 时，用当前代码面**重算**并逐行比对，得出"
            "「复现得出来（可比）/ 复现不出来（已知不可比）」。"
            "**只写档外的台账，不改冻结档、不改 candidates.jsonl**"
        ),
    )
    p_prov.add_argument(
        "--version", type=int, default=0,
        help="只登记这一版；缺省 = 全部已冻结版本",
    )
    p_prov.add_argument(
        "--p2-root", default=str(PIPELINE_ROOT),
        help=f"P1 存档根（默认 {PIPELINE_ROOT}）",
    )
    p_prov.add_argument("--dry-run", action="store_true", help="只打印不落盘")
    p_prov.set_defaults(func=cmd_provenance)

    p_agr = sub.add_parser("agreement", help="D10 代码层与黄金集一致率")
    p_agr.add_argument(
        "--version", type=int, default=0, help="冻结版本号；缺省用未冻结的已标注候选"
    )
    p_agr.add_argument("--json-out", default="", help="结果落成 JSON")
    p_agr.add_argument(
        "--soft-dim",
        nargs="*",
        default=None,
        help=(
            "软维度名列表（白名单 d2..d9）；空 = 自动从黄金集 "
            "``golden.dimension_scores`` 出现的 dim 探测（仅白名单内的）。"
            "未登记名 fail-closed 抛 ValueError（plan §三·第一步 contract 1）。"
        ),
    )
    p_agr.add_argument(
        "--dim-tolerance",
        type=int,
        default=int(thresholds.value("soft_dim_tolerance")),
        help=(
            "软维度一致率容忍档差，|t-p| <= tolerance 计一致。"
            "默认值取自 evaluation/scorers/thresholds.py（阈值唯一真源，"
            "方案 16 C-14 / D-C6：改阈值不算 rubric 级变更）；"
            "首次跑只发分布再定阈值（plan §三·第一步风险）。"
        ),
    )
    p_agr.add_argument(
        "--emit-disagreements",
        nargs="?",
        const=str(PIPELINE_ROOT / "calibration"),
        default=None,
        metavar="DIR",
        help=(
            "派生「全量预测快照 + 分歧卡」到 DIR/v{N}/（方案 16 C-15 / C-02）。"
            "只读派生，不回写冻结档。不带参数 = output/pipeline/calibration；"
            "不写该选项 = 不落盘。要求同时给 --version。"
        ),
    )
    p_agr.add_argument(
        "--force-snapshot",
        action="store_true",
        help=(
            "允许覆盖已存在的 snapshot.json。**默认不许**：那份文件是"
            "「上一轮判了什么」的唯一凭据，重盖章等于改历史（M-29 S3′）。"
        ),
    )
    p_agr.add_argument(
        "--emit-url-conflicts",
        nargs="?",
        const=str(GOLDEN_ROOT / "url_gold_conflicts.md"),
        default="",
        metavar="PATH",
        help=(
            "落「URL 级金标冲突清单」（M-30）：机器填事实块，人填「定性 / "
            "裁定依据」两列。重跑只刷新事实块，**已填结论原样保留**。"
            "不带参数 = golden/data/url_gold_conflicts.md。"
        ),
    )
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
