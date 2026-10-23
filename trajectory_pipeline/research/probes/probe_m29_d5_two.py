"""探针：把 **D-5 判 2 分**的样本逐条摊开（正文原话 + 命中的形态词）。

为什么
------
M-29 收口时要人逐条读「D-5 判 2 分」的正文原话，判断这 2 分是
**评分器判对了**（分支贴反了）、**评分器判反了**（正文里其实是付费门禁），
还是**词表不全**（两种形态都没命中）。这三种的处置完全不同：
只有第二种才该改代码。

怎么读这些正文
--------------
链条是三层，**原话在第三层**::

    candidates.jsonl（按 golden_id 找行）
      → task_id + run_id
      → output/pipeline/{task_id}__{run_id}.json      ← P1 存档
      → split_archive() → Sample
      → Sample.observations[i]["body_text"]           ← 原话在这个字段

⚠️ **只认正文，不认 rationale**：``rationales`` 是**动作前动机**
（「是否存在地区限制还需确认」），不是对页面的观察。2026-10-23 第一版把
理由也当证据，10 条低分里 2 条是这么来的（搜狐那条正文写的是
「您可以试看5分钟，点击开通会员」，真·付费门禁，却被动机句里的
「无地区限制」判成地区证据）。

判据（借感知层那两张词表，不另立一套）
--------------------------------------
- **环境阻断形态**：``rule_perceptor.infer_access_defect`` ——
  region / captcha / anti_bot / not_found（**不含 login**）。
- **付费门禁形态**：``rule_perceptor.paywall_evidence`` ——
  L1 门禁短语命中即成立；L2 CTA 短语必须**同句**配观看语境词
  （裸「开通会员」是导航栏广告，不算）。

⚠️ **方向不对称**：H-16 裁定「环境阻断让位在先」，所以
**分支贴了付费墙 + 正文有环境阻断形态 ⇒ D-5 判错（锚点 2）**；
反过来（分支贴了阻断、正文是付费门禁）**没有裁定，不判**。

产物
----
同目录 ``m29_d5_two.json``（**证据，不放结论**）：每条给
正文全文、命中的形态词、命中处的上下文窗口、以及机器能直接读出的
**形态组合**（``defect_only`` / ``paywall_only`` / ``both`` / ``neither``）。
**裁定归人**——形状组合给出的是读数，不是结论。

用法（必须在仓库根跑）::

    uv run python trajectory_pipeline/research/probes/probe_m29_d5_two.py

⚠️ Windows 控制台先设 ``PYTHONIOENCODING=utf-8``（否则中文打印在
GBK 控制台报错，看起来像脚本跑完了但出错）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

# 必须在仓库根跑：脚本目录在 sys.path[0]，CWD 才是包根。
sys.path.insert(0, ".")

from trajectory_pipeline.evaluation.golden.builder import (  # noqa: E402
    _load_jsonl,
    _resolve_sample_for_candidate,
)
from trajectory_pipeline.evaluation.scorers.d5_scorer import score_d5  # noqa: E402
from trajectory_pipeline.perception.rule_perceptor import (  # noqa: E402
    PAYWALL_CTA_PHRASES,
    _context_outside,
    _paywall_units,
    access_evidence,
    infer_access_defect,
    paywall_evidence,
)

REPO = Path(__file__).resolve().parents[3]
GOLD_DIR = REPO / "trajectory_pipeline" / "evaluation" / "golden" / "data"
ARCHIVE_ROOT = REPO / "trajectory_pipeline" / "output" / "pipeline"
FROZEN_VERSION = 7
SPLIT_FILES = ("calibration", "regression", "holdout")

#: 上下文窗口：命中形态词的前后各留这么多字，人一眼能看到原话。
PAD = 80

#: 一条样本最多列出几个命中窗口（页面常把同一块重复渲染）。
MAX_WINDOWS = 2


def _split_of() -> dict[str, str]:
    """冻结档 → 档位映射（``golden_id`` → split 名）。"""
    out: dict[str, str] = {}
    for split in SPLIT_FILES:
        p = GOLD_DIR / f"v{FROZEN_VERSION}" / f"{split}.jsonl"
        if not p.exists():
            continue
        for row in _load_jsonl(p):
            out[str(row.get("golden_id") or "")] = split
    return out


def _annotations() -> dict[str, list[dict[str, Any]]]:
    """``golden_id`` → 全部轮次的标注（round=1 是冻结用的那一轮）。"""
    p = GOLD_DIR / "annotations.jsonl"
    if not p.exists():
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for r in _load_jsonl(p):
        out.setdefault(str(r.get("golden_id") or ""), []).append(r)
    return out


def _context_windows(text: str, phrase: str, pad: int = PAD) -> list[str]:
    """命中短语前后的上下文窗口（**全部**命中位置，去重，最多 MAX_WINDOWS 个）。

    没命中返回**空列表**——不拿正文开头冒充「上下文」：那会让「没命中」
    看起来像「命中了但没显示」。
    """
    if not phrase:
        return []
    # 命中词常带引号与类别（「70017001」（region）），取词本身去定位。
    token = phrase.strip("「」（) ").split("（")[0].strip("「」")
    if not token:
        return []
    out: list[str] = []
    pos = text.find(token)
    while pos >= 0 and len(out) < MAX_WINDOWS:
        start = max(0, pos - pad)
        end = min(len(text), pos + len(token) + pad)
        out.append(
            ("…" if start else "")
            + text[start:end]
            + ("…" if end < len(text) else "")
        )
        pos = text.find(token, pos + 1)
    return out


def _raw_cta_hits(text: str) -> list[dict[str, Any]]:
    """正文里**裸 CTA** 的命中清单（不判门禁，只列给人工看）。

    这是人工核对的那一刀：H-16 口径下「开通会员」独行是导航栏广告，
    必须**同句**有观看语境词才算门禁。所以这里把每个 CTA 的
    **同句语境词**摊开——有没有语境词，一眼可见。
    """
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _start, _end, unit in _paywall_units(text):
        ulow = unit.lower()
        for cta in PAYWALL_CTA_PHRASES:
            clow = cta.lower()
            pos = ulow.find(clow)
            while pos >= 0:
                ctx = _context_outside(ulow, pos, pos + len(clow))
                key = f"{cta}|{ctx}"
                if key not in seen:
                    seen.add(key)
                    hits.append({
                        "phrase": cta,
                        "context": ctx,
                        # 有语境词才是门禁；没有就是裸 CTA（导航栏广告）。
                        "gate_ready": bool(ctx),
                    })
                pos = ulow.find(clow, pos + 1)
    return hits


def _form_profile(defect: str, paywall: str) -> str:
    """形态组合——**读数**，不是裁定。

    - ``defect_only``：有环境阻断、无付费门禁 ⇒ D-5 判 2 分**大概率正确**
      （分支贴反了）。
    - ``paywall_only``：有付费门禁、无环境阻断 ⇒ D-5 **可能判反**。
    - ``both``：两者都有 ⇒ 让位裁定生效，方向单一。
    - ``neither``：两种形态都没命中 ⇒ **词表可能不全**。
    """
    if defect and paywall:
        return "both"
    if defect:
        return "defect_only"
    if paywall:
        return "paywall_only"
    return "neither"


def collect() -> dict[str, Any]:
    split_of = _split_of()
    anns = _annotations()
    rows: list[dict[str, Any]] = []
    cands = _load_jsonl(GOLD_DIR / "candidates.jsonl")
    for c in cands:
        gid = str(c.get("golden_id") or "")
        sample = _resolve_sample_for_candidate(c, ARCHIVE_ROOT)
        if sample is None:
            continue
        verdict = score_d5(sample)
        if verdict.score != 2:
            continue
        text = " ".join(
            str(o.get("body_text") or "") for o in (sample.observations or ())
        )
        defect = infer_access_defect(text)
        paywall = paywall_evidence(text)
        defect_hit = access_evidence(text, kind=defect) if defect else ""
        rounds = anns.get(gid, [])
        r1 = next((r for r in rounds if int(r.get("round") or 1) == 1), None)
        rows.append({
            "golden_id": gid,
            "split": split_of.get(gid, ""),
            "in_frozen_v7": gid in split_of,
            "source_kind": c.get("source_kind"),
            "task_id": c.get("task_id"),
            "run_id": c.get("run_id"),
            "archive": (
                f"trajectory_pipeline/output/pipeline/{c.get('task_id')}"
                f"__{c.get('run_id')}.json"
                if c.get("task_id") and c.get("run_id") else ""
            ),
            "url": c.get("url"),
            "code_branch": (c.get("code") or {}).get("branch"),
            # 金标取 round=1——与 ``_labeled_rows``（冻结用）同一口径。
            "golden_label": (r1 or {}).get("label"),
            "golden_reviewed_by": (r1 or {}).get("reviewed_by"),
            "golden_rounds": [
                {
                    "round": r.get("round"),
                    "label": r.get("label"),
                    "reviewed_by": r.get("reviewed_by"),
                    "reviewed_at": r.get("reviewed_at"),
                }
                for r in rounds
            ],
            "branch_eq_gold": (
                (c.get("code") or {}).get("branch") == (r1 or {}).get("label")
                if r1 else None
            ),
            "d5_score": verdict.score,
            "d5_why": [
                ck.why for ck in verdict.checks if ck.node == "结论的证据核"
            ],
            "form_profile": _form_profile(defect, paywall),
            "access_defect": defect,
            "access_defect_hit": defect_hit,
            "access_all_hits": access_evidence(text),
            "paywall_hit": paywall,
            "paywall_cta_raw": _raw_cta_hits(text),
            "n_observations": len(sample.observations or ()),
            "body_chars": len(text),
            "context_defect": _context_windows(text, defect_hit),
            "context_paywall": _context_windows(text, paywall),
            "body_text": text,
        })
    rows.sort(key=lambda r: (r["split"] or "~", r["golden_id"]))
    return {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": (
            "读数取自存档原文（body_text），非临场重采 ⇒ 站点可能已漂移。"
            "form_profile 是形态组合读数，**裁定归人**。"
        ),
        "source": {
            "candidates": "trajectory_pipeline/evaluation/golden/data/candidates.jsonl",
            "annotations": "trajectory_pipeline/evaluation/golden/data/annotations.jsonl",
            "archives_root": "trajectory_pipeline/output/pipeline",
            "frozen_version": FROZEN_VERSION,
        },
        "rows": rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        default=str(Path(__file__).with_name("m29_d5_two.json")),
        help="产物路径（默认与脚本同目录）",
    )
    args = parser.parse_args(argv)
    data = collect()
    out = Path(args.json)
    out.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"d5=2 共 {len(data['rows'])} 条")
    for r in data["rows"]:
        print(
            f"[{r['split'] or '(不在 v7 冻结档)'}] {r['golden_id']}"
            f"  分支={r['code_branch']}  金标={r['golden_label']}"
            f"  形态组合={r['form_profile']}"
            f"  阻断={r['access_defect'] or '-'}  门禁={r['paywall_hit'] or '-'}"
        )
    print(f"\n产物：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
