# -*- coding: utf-8 -*-
"""INC41：D-9 judge 提前重测（owner 2026-10-15 拍板解除 14 天间隔）。

对 INC40 工作表 15 对 (observation, rationale) 逐对：
  1. 跑真实三查闸门（rationale.gates.gate）记录真实状态；
  2. 强制触发 D-9 judge（few-shot 池冻结为当前 D9_FEW_SHOT_EXAMPLES，即 v1 六条）；
  3. 连跑两轮（roundA / roundB）量测 judge test-retest 稳定性。

输出 tmp/inc41_judge_retest.jsonl（每对两行，round 字段区分）。
"""
import json
import pathlib
import sys
import time

sys.path.insert(0, ".")

from trajectory_pipeline.evaluation.scorers.d9_scorer import score_d9
from trajectory_pipeline.llm.client import LLMClient, LLMConfig
from trajectory_pipeline.rationale.gates import gate as rationale_gate

WS = pathlib.Path("tmp/inc40_retest_worksheet_data.jsonl")
OUT = pathlib.Path("tmp/inc41_judge_retest.jsonl")

pairs = [json.loads(l) for l in WS.read_text(encoding="utf-8").splitlines() if l.strip()]
client = LLMClient(LLMConfig.from_env())

rows = []
t0 = time.time()
for i, p in enumerate(pairs):
    if p["kind"] == "MISS" or not p.get("rationale"):
        rows.append({"golden_id": p["golden_id"], "step": p.get("step"), "round": "-",
                     "kind": "no_data", "verdict": None, "score": None,
                     "gate_status": None, "evidence": "", "reasons": ["结构性无数据"]})
        continue
    obs, rat = p["observation"], p["rationale"]
    sample = {
        "observations": [obs],
        "rationales": [{"step_index": p["step"], "text": rat.get("text", ""),
                        "conclusion": rat.get("conclusion", "")}],
        "actions": [],
    }
    # 真实闸门（记录状态；不触发时下面强制 failed 触发——INC40 冒烟同款纪律）
    gr = rationale_gate(rat.get("text", ""), observations=[obs], step_index=0)
    gate_status = gr.status
    trigger, _why = True, "forced"
    gr_forced = gr
    if gr.status == "passed" and not gr.undeterminable:
        gr_forced = {"status": "failed", "findings": [], "undeterminable": []}
    for round_name in ("A", "B"):
        v = score_d9(sample, gate_result=gr_forced, llm_client=client)
        rows.append({
            "golden_id": p["golden_id"], "step": p["step"], "round": round_name,
            "kind": p["kind"], "verdict": v.verdict, "score": v.score,
            "d9_kind": v.kind, "gate_status": gate_status,
            "evidence": (v.evidence or "")[:600],
            "reasons": list(v.reasons),
        })
        print(f"[{i+1}/{len(pairs)}] {p['golden_id']} step={p['step']} round={round_name} "
              f"gate={gate_status} → {v.verdict or v.kind}", flush=True)

OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
print(f"\n完成：{len(rows)} 行 → {OUT}（耗时 {time.time()-t0:.0f}s）")
