# -*- coding: utf-8 -*-
"""D-3 / D-9 提示词 A/B：新版（锚点表渲染）vs 旧版（v8 冻结时那版）。

为什么要有这个脚本
------------------
方案 16 C-10 把「评分锚」一栏从**手抄散文**改成**从锚点表渲染**
（owner 2026-10-23 裁定 D-C7 = rubric 变更强制连带改 prompt）。改完 prompt
就变了 ⇒ ``evaluator_version`` 从 ``9b3ec1c46545``（v8 冻结时那版）变成
``1046e67994aa``。

**改了 prompt 却不验它有没有改判**，等于把「口径对齐」当成了「读数对齐」——
文档对齐了，线上读数可能已经漂了。这个脚本回答一个问题：
**加进去的锚点一栏，改判了吗？**

三条臂（同一份输入）
-------------------
- ``old``：v8 冻结时那版提示词（= 现在这版**去掉锚点块**）；
- ``new``：现在这版；
- ``new2``：现在这版**再跑一遍**——噪声地板。

为什么要有第三条臂：LLM judge 有运行间噪声（温度 0 也不保证逐字一致）。
old↔new 的不一致里，**有一部分是噪声而不是提示词效应**。有了 new↔new
这一条地板，才能说清「差异里有多少是提示词造成的」。

旧版提示词怎么来的（**不靠记忆，靠指纹**）
------------------------------------------
旧版提示词在生产代码里已经不存在了（锚点块渲染取代了手抄）。本脚本
**从现版里把锚点块抠掉**重建它，然后用**旧版算法的指纹算法**（不含
``anchors`` 那一栏——它是 C-10 才加进指纹的）算一遍，要求**逐位等于
v8 manifest 里的 ``evaluator_version``**（``9b3ec1c46545``）。
对不上就**当场停**（fail-closed）——那一刻测的就不是同一件事了。

取样口径（确定性，可复算）
------------------------
- 存档：``trajectory_pipeline/output/pipeline/*.json``（按文件名排序）；
- 样本：逐份 ``split_archive`` 切出来的 Sample；
- D-3 候选：观察充分（``d3_scorer._all_observations_insufficient`` 为空）；
- D-9 候选：有 rationale，且**真实闸门触发**（``gate.status == failed``）——
  **不强制触发**：强制触发会把「闸门已过的样本」也拉进来，那是另一个总体，
  读数会被读成生产面上的分布（INC41 那种强制触发法是**另一次实验**的口径）；
- 选取：按 ``(archive, sample_id)`` 排序取前 N 条——**规则写在输出里**。

落盘
----
- ``prompt_ab_anchor.jsonl``：逐条（每条样本三臂的原始判定 + evidence 截断）；
- ``prompt_ab_anchor.json``：汇总（一致率 / 分数一致率 / 混淆表 / 分歧清单）。

⚠️ **凭据**：只读 ``TRAJECTORY_LLM_*`` 环境变量，**不打印、不落盘**。
⚠️ **raw CoT**：judge 的输出经 ``extract_json_reporting`` 剥过 think；
落盘的 ``evidence`` 是裁判引用的短语（截断 600 字），不是思维链。

用法（**必须在仓库根跑**）
------------------------
    PYTHONIOENCODING=utf-8 python trajectory_pipeline/research/probes/prompt_ab_anchor.py \\
        --d3-limit 30 --d9-limit 15 \\
        --json trajectory_pipeline/research/probes/prompt_ab_anchor.json \\
        --jsonl trajectory_pipeline/research/probes/prompt_ab_anchor.jsonl
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import sys
import time

sys.path.insert(0, ".")

from trajectory_pipeline.assembler.schema import split_archive
from trajectory_pipeline.evaluation.scorers import d3_scorer, d9_scorer, rubric_version
from trajectory_pipeline.llm.client import LLMClient, LLMConfig

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PIPELINE_ROOT = REPO_ROOT / "trajectory_pipeline" / "output" / "pipeline"
V8_MANIFEST = (
    REPO_ROOT / "trajectory_pipeline" / "evaluation" / "golden" / "data" / "v8"
    / "manifest.json"
)

#: v8 冻结时那版的 ``evaluator_version``——**旧版提示词的指纹基准**。
#: 从 v8 的 manifest 现读（不写死）：写死的话，manifest 换了基准就跟着错。
def _v8_evaluator_version() -> str:
    data = json.loads(V8_MANIFEST.read_text(encoding="utf-8"))
    return str(data.get("evaluator_version") or "")


def old_style_fingerprint(d3_prompt: str, d9_prompt: str) -> str:
    """**C-10 之前**那套指纹算法（不含 ``anchors`` 栏）。

    为什么单写一份：C-10 给 :func:`rubric_version.evaluator_version` 加了
    ``anchors`` 键（表变了指纹要变）。而**旧版提示词的指纹是旧算法算的**——
    用新算法去核旧提示词，对不上是必然的，对上了才是巧合。这份复制只用于
    **核验重建出的旧提示词**，不进任何产品路径。
    """
    parts = {
        "rubric_version": rubric_version.RUBRIC_VERSION,
        "d3_prompt": d3_prompt,
        "d3_categories": sorted(d3_scorer.D3_CATEGORIES),
        "d3_subtypes": sorted(d3_scorer.D3_SUBTYPES),
        "d9_prompt": d9_prompt,
        "d9_verdicts": sorted(d9_scorer.D9_VERDICTS),
        "d9_few_shot": json.loads(
            json.dumps(
                d9_scorer.D9_FEW_SHOT_EXAMPLES,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        ),
    }
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def rebuild_old_prompts() -> tuple[str, str, dict[str, str]]:
    """重建旧版提示词 + 指纹核验。核验不过 → **抛异常当场停**。

    重建方式：把现版里的锚点块**抠掉**（块是 :func:`render_anchor_block`
    的确定性输出，抠掉即回到手抄散文那版）。
    """
    block_d3 = rubric_version.render_anchor_block("d3")
    block_d9 = rubric_version.render_anchor_block("d9")
    for name, prompt, block in (
        ("d3", d3_scorer.D3_SYSTEM_PROMPT, block_d3),
        ("d9", d9_scorer.D9_SYSTEM_PROMPT, block_d9),
    ):
        if prompt.count(block) != 1:
            raise SystemExit(
                f"❌ {name} 现版提示词里的锚点块出现 {prompt.count(block)} 次——"
                "重建旧版的前提不成立（锚点块不再唯一），停。"
            )
    old_d3 = d3_scorer.D3_SYSTEM_PROMPT.replace(block_d3, "")
    old_d9 = d9_scorer.D9_SYSTEM_PROMPT.replace(block_d9, "")
    want = _v8_evaluator_version()
    got = old_style_fingerprint(old_d3, old_d9)
    if not want:
        raise SystemExit("❌ v8 manifest 里没有 evaluator_version——基准读不到，停。")
    if got != want:
        raise SystemExit(
            f"❌ 重建出的旧版提示词指纹 {got} ≠ v8 manifest 的 {want}——"
            "重建不是逐位还原，测的就不是同一件事，停。"
        )
    return old_d3, old_d9, {
        "old_fingerprint": got,
        "new_fingerprint": rubric_version.evaluator_version(),
        "baseline_source": str(V8_MANIFEST.relative_to(REPO_ROOT)),
    }


# ── 取样 ──────────────────────────────────────────────────────────


def _archive_samples() -> list[tuple[str, object]]:
    """(archive 名, Sample) 列表——按 archive 名排序，确定性。

    切不开的存档**跳过并计数**（不猜、不补）：形状不对的存档不是「没有样本」。
    """
    out: list[tuple[str, object]] = []
    skipped: list[str] = []
    for path in sorted(PIPELINE_ROOT.glob("*.json")):
        if not path.is_file():
            continue
        try:
            samples = list(split_archive(path))
        except Exception as exc:  # noqa: BLE001 - 单份坏档不拖垮整批
            skipped.append(f"{path.name}:{type(exc).__name__}")
            continue
        for sample in samples:
            out.append((path.name, sample))
    out.sort(key=lambda pair: (pair[0], str(getattr(pair[1], "sample_id", ""))))
    return out, skipped


def _d3_candidates(samples: list[tuple[str, object]]) -> list[dict[str, object]]:
    """D-3 候选：观察充分（不足的判不了，两版都一样判不了 ⇒ 不进 A/B）。"""
    out = []
    for archive, sample in samples:
        obs = d3_scorer._observations(sample)
        if d3_scorer._all_observations_insufficient(obs):
            continue
        out.append({
            "archive": archive,
            "sample_id": str(getattr(sample, "sample_id", "")),
            "sample": sample,
        })
    return out


def _d9_candidates(samples: list[tuple[str, object]]) -> list[dict[str, object]]:
    """D-9 候选：有 rationale 且**真实闸门触发**。

    触发判据走 :func:`d9_scorer._should_trigger_llm`（同生产路径）：
    ``gate.status == failed`` → 触发；``passed`` → 不触发（成本控制）。
    """
    out = []
    for archive, sample in samples:
        rats = d9_scorer._rationales(sample)
        if not rats:
            continue
        should, why = d9_scorer._should_trigger_llm(sample, None)
        if not should:
            continue
        out.append({
            "archive": archive,
            "sample_id": str(getattr(sample, "sample_id", "")),
            "sample": sample,
            "trigger_reason": why,
        })
    return out


# ── 跑一条 ────────────────────────────────────────────────────────


def _run_d3(sample: object, client: LLMClient, prompt: str) -> dict[str, object]:
    v = d3_scorer.score_d3(sample, llm_client=client, system_prompt=prompt)
    return {
        "kind": v.kind,
        "score": v.score,
        "label": v.category,
        "subtype": v.playable_subtype,
        "evidence": (v.evidence or "")[:600],
        "reasons": list(v.reasons),
    }


def _run_d9(sample: object, client: LLMClient, prompt: str) -> dict[str, object]:
    v = d9_scorer.score_d9(sample, llm_client=client, system_prompt=prompt)
    return {
        "kind": v.kind,
        "score": v.score,
        "label": v.verdict,
        "evidence": (v.evidence or "")[:600],
        "reasons": list(v.reasons),
    }


def _agree(a: dict[str, object], b: dict[str, object]) -> bool:
    """两臂是否同判。

    **只比「判出来的那个东西」**：``kind`` 不一致（一边 scored 一边
    undetermined）⇒ 不算一致；两边都没判出来（kind 相同且无 label）⇒
    **算一致**——它们对「判不了」这件事没有分歧。
    """
    if a["kind"] != b["kind"]:
        return False
    if a["label"] is None and b["label"] is None:
        return True
    return a["label"] == b["label"]


def _rate(num: int, den: int) -> float | None:
    """比率；分母为 0 → ``None``（**不是 0**——「没数据」与「零分歧」不同）。"""
    return round(num / den, 4) if den else None


# ── 主流程 ────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D-3/D-9 提示词 A/B（锚点块效应）")
    ap.add_argument("--d3-limit", type=int, default=30)
    ap.add_argument("--d9-limit", type=int, default=15)
    ap.add_argument(
        "--json",
        default=str(HERE / "prompt_ab_anchor.json"),
        help="汇总输出路径",
    )
    ap.add_argument(
        "--jsonl",
        default=str(HERE / "prompt_ab_anchor.jsonl"),
        help="逐条输出路径",
    )
    args = ap.parse_args(argv)

    old_d3, old_d9, fingerprints = rebuild_old_prompts()
    print(f"→ 旧版提示词重建并核验通过：{fingerprints}")

    client = LLMClient(LLMConfig.from_env())
    samples, skipped = _archive_samples()
    print(f"→ 存档 {len(list(PIPELINE_ROOT.glob('*.json')))} 份，"
          f"切出样本 {len(samples)} 条（切不开 {len(skipped)} 份，跳过）")

    rows: list[dict[str, object]] = []
    summary: dict[str, object] = {
        "fingerprints": fingerprints,
        "selection_rule": (
            "存档按文件名排序 → split_archive 切样本 → 候选按 "
            "(archive, sample_id) 排序取前 N；D-3 候选=观察充分，"
            "D-9 候选=有 rationale 且真实闸门触发"
        ),
        "skipped_archives": skipped,
        "dims": {},
    }

    for dim, candidates, run, old_prompt, new_prompt in (
        ("d3", _d3_candidates(samples)[: args.d3_limit], _run_d3, old_d3,
         d3_scorer.D3_SYSTEM_PROMPT),
        ("d9", _d9_candidates(samples)[: args.d9_limit], _run_d9, old_d9,
         d9_scorer.D9_SYSTEM_PROMPT),
    ):
        agree_old_new = 0
        agree_new_new = 0
        score_agree = 0
        scored_n = 0
        confusion: collections.Counter = collections.Counter()
        diffs: list[dict[str, object]] = []
        t0 = time.time()
        for i, cand in enumerate(candidates, 1):
            sample = cand["sample"]
            old = run(sample, client, old_prompt)
            new = run(sample, client, new_prompt)
            new2 = run(sample, client, new_prompt)   # 噪声地板：同版再跑一遍
            same = _agree(old, new)
            same2 = _agree(new, new2)
            agree_old_new += int(same)
            agree_new_new += int(same2)
            if old["kind"] == "scored" and new["kind"] == "scored":
                scored_n += 1
                score_agree += int(old["score"] == new["score"])
                confusion[f"{old['label']} -> {new['label']}"] += 1
            row = {
                "dim": dim,
                "archive": cand["archive"],
                "sample_id": cand["sample_id"],
                "old": old,
                "new": new,
                "new2": new2,
                "agree_old_new": same,
                "agree_new_new": same2,
            }
            rows.append(row)
            if not same:
                diffs.append(row)
            print(
                f"[{dim} {i}/{len(candidates)}] {cand['sample_id']} "
                f"old={old['label']}({old['score']}) new={new['label']}({new['score']}) "
                f"new2={new2['label']}({new2['score']}) "
                f"{'同' if same else '★异'} / 地板{'同' if same2 else '异'}",
                flush=True,
            )
        n = len(candidates)
        summary["dims"][dim] = {
            "n": n,
            "n_scored_both": scored_n,
            "label_agreement_old_new": _rate(agree_old_new, n),
            "label_agreement_new_new": _rate(agree_new_new, n),
            "score_agreement_old_new": _rate(score_agree, scored_n),
            "confusion": dict(sorted(confusion.items())),
            "diff_count": len(diffs),
            "elapsed_s": round(time.time() - t0, 1),
        }

    summary["rows"] = len(rows)
    out_rows = pathlib.Path(args.jsonl)
    out_rows.parent.mkdir(parents=True, exist_ok=True)
    out_rows.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    out_json = pathlib.Path(args.json)
    out_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\n→ 逐条 {out_rows}（{len(rows)} 行）· 汇总 {out_json}")
    for dim, s in summary["dims"].items():
        print(f"  {dim}: n={s['n']} 判出一致率 old↔new={s['label_agreement_old_new']}"
              f" · 噪声地板 new↔new={s['label_agreement_new_new']}"
              f" · 分数一致率={s['score_agreement_old_new']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
