# -*- coding: utf-8 -*-
"""URL 直采：绕过搜索漂移，用主链 visit_site 直访 90 行并集里的访问级 URL。

与 cli run 的差别**只有一处**：候选不从搜索来，直接用黄金行的 URL 构造
Candidate。感知链（obscura 驱动 + LLM perceptor + 判断点 ②③）一行没变——
这保证新 code.branch 与旧档可比。

产物：outcomes.jsonl（同目录），每行 = 一个 (task_id, url) 的新判定，
字段与 P1 outcome 同构（branch/question/evidence/source/fallback_used/
reached_play_page/branch_label）。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

HERE = Path(__file__).resolve().parent
TIMEOUT_S = 90.0
OUT = HERE / "outcomes.jsonl"


def load_targets():
    from trajectory_pipeline.evaluation.golden.alignment import is_equivalent

    manifest = {m["task_id"]: m for m in json.loads(
        (HERE / "manifest.json").read_text(encoding="utf-8"))}
    rows = []
    for f in ["calibration.jsonl", "holdout.jsonl"]:
        p = Path("trajectory_pipeline/evaluation/golden/data/v3") / f
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    site = [r for r in rows if r["source_kind"] != "p2_sample" and r.get("golden")]
    br = lambda r: (r.get("code") or {}).get("branch") or "success"
    gl = lambda r: (r.get("golden") or {}).get("label") or "success"
    union = [r for r in site
             if (not is_equivalent(gl(r), br(r)))
             or ("采集降级" in ((r.get("code") or {}).get("evidence") or ""))]
    # select.play_sites 行的判定发生在搜索页（①），直访不等价——跳过
    visit_level = [r for r in union
                   if (r.get("code") or {}).get("question") != "select.play_sites"]
    # manifest 同 task_id 多存档时 title 一致（E/T 系列均如此），取任一
    targets = {}
    for r in visit_level:
        m = manifest.get(r["task_id"])
        if not m:
            print(f"  [skip] {r['task_id']} 不在 manifest", flush=True)
            continue
        targets.setdefault((r["task_id"], r["url"]), m["title"])
    return targets


async def main():
    from trajectory_pipeline.executor.browser.mcp_client import McpClient
    from trajectory_pipeline.executor.browser.obscura_driver import ObscuraDriver
    from trajectory_pipeline.executor.branches import RunLedger
    from trajectory_pipeline.executor.steps.search import Candidate
    from trajectory_pipeline.executor.steps.visit import visit_site
    from trajectory_pipeline.perception.factory import build_perceptor

    targets = load_targets()
    print(f"待直访 (task_id, url) 对：{len(targets)}", flush=True)

    client = McpClient.from_env()
    done = 0
    async with client:
        driver = ObscuraDriver(client)
        perceptors = {}
        with OUT.open("w", encoding="utf-8") as fh:
            for (task_id, url) in sorted(targets):
                title = targets[(task_id, url)]
                if title not in perceptors:
                    perceptors[title] = build_perceptor(mode="llm", target_title=title)
                perceptor = perceptors[title]
                ledger = RunLedger(task_id=task_id)
                cand = Candidate(url=url, text=title, rank=1)
                try:
                    await asyncio.wait_for(
                        visit_site(driver, perceptor, ledger, cand, max_chars=6000),
                        timeout=TIMEOUT_S)
                except asyncio.TimeoutError:
                    ledger.record(url, "unresolved", f"直访超时（>{TIMEOUT_S}s）",
                                  site_url=url)
                except Exception as exc:
                    ledger.record(url, "unresolved",
                                  f"直访异常: {type(exc).__name__}: {exc}",
                                  site_url=url)
                for o in ledger.outcomes:
                    fh.write(json.dumps({
                        "task_id": task_id,
                        "url": o.url,
                        "branch": o.branch,
                        "branch_label": getattr(o, "branch_label", ""),
                        "question": o.question,
                        "evidence": o.evidence,
                        "source": o.source,
                        "fallback_used": o.fallback_used,
                        "reached_play_page": o.reached,
                    }, ensure_ascii=False) + "\n")
                fh.flush()
                done += 1
                b = ledger.outcomes[-1].branch if ledger.outcomes else "?"
                print(f"[{done}/{len(targets)}] {task_id} {url[:60]} -> {b}", flush=True)
    print("完成", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
