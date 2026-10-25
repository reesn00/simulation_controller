# -*- coding: utf-8 -*-
"""URL 直采：绕过搜索漂移，用主链 visit_site 直访 90 行并集里的访问级 URL。

与 cli run 的差别**只有一处**：候选不从搜索来，直接用黄金行的 URL 构造
Candidate。感知链（obscura 驱动 + LLM perceptor + 判断点 ②③）一行没变——
这保证新 code.branch 与旧档可比。

产物：outcomes.jsonl（同目录），每行 = 一个 (task_id, url) 的新判定，
字段与 P1 outcome 同构（branch/question/evidence/source/fallback_used/
reached_play_page/branch_label）。
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

HERE = Path(__file__).resolve().parent
TIMEOUT_S = 90.0
OUT = HERE / "outcomes.jsonl"

#: 默认读取的黄金集冻结版本（A 类付费墙修复后对准的当前档）。
#: 早先脚本硬编码 v3；金标经 14 条核改后已漂到 v10，重采必须对准最新冻结。
DEFAULT_VERSION = 10


def load_targets(version: int = DEFAULT_VERSION, only: list[str] | None = None):
    from trajectory_pipeline.evaluation.golden.alignment import is_equivalent

    manifest = {m["task_id"]: m for m in json.loads(
        (HERE / "manifest.json").read_text(encoding="utf-8"))}
    rows = []
    for f in ["calibration.jsonl", "holdout.jsonl"]:
        p = Path(f"trajectory_pipeline/evaluation/golden/data/v{version}") / f
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
    # 定点重采：--only 传入 golden_id 列表时，只保留这些行（2026-10-25
    # 路径 1「付费墙弃权」——定点重采 LLM 不可用/采集降级样本，不重跑全量）。
    if only:
        want = set(only)
        visit_level = [r for r in visit_level if r["golden_id"] in want]
        missed = want - {r["golden_id"] for r in visit_level}
        if missed:
            print(f"  [warn] --only 指定的 {sorted(missed)} 不在并集/visit_level，忽略", flush=True)
    # manifest 同 task_id 多存档时 title 一致（E/T 系列均如此），取任一
    targets = {}
    for r in visit_level:
        m = manifest.get(r["task_id"])
        if not m:
            print(f"  [skip] {r['task_id']} 不在 manifest", flush=True)
            continue
        targets.setdefault((r["task_id"], r["url"]), m["title"])
    return targets


async def main(version: int = DEFAULT_VERSION, only: list[str] | None = None):
    from trajectory_pipeline.executor.browser.mcp_client import McpClient
    from trajectory_pipeline.executor.browser.obscura_driver import ObscuraDriver
    from trajectory_pipeline.executor.branches import RunLedger
    from trajectory_pipeline.executor.steps.search import Candidate
    from trajectory_pipeline.executor.steps.visit import visit_site
    from trajectory_pipeline.perception.factory import build_perceptor

    targets = load_targets(version, only=only)
    print(f"冻结档 v{version}：待直访 (task_id, url) 对：{len(targets)}", flush=True)

    # 定点重采（--only）时**合并**旧 outcomes：新采到的行覆盖同 (task_id,url)
    # 的旧行，其余保留——避免定点重采丢掉上一轮已修复的样本。
    old_outcomes: dict[tuple[str, str], dict] = {}
    if only and OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            if line.strip():
                o = json.loads(line)
                old_outcomes[(o["task_id"], o["url"])] = o

    client = McpClient.from_env()
    done = 0
    async with client:
        driver = ObscuraDriver(client)
        perceptors = {}
        with OUT.open("w", encoding="utf-8") as fh:
            # 先回写非本次重采的旧行（定点模式）
            if only:
                revisit = set(targets)
                for (k, o) in old_outcomes.items():
                    if k not in revisit:
                        fh.write(json.dumps(o, ensure_ascii=False) + "\n")
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
    parser = argparse.ArgumentParser(
        description="URL 直采：直访黄金集并集里的访问级 URL，产出 outcomes.jsonl")
    parser.add_argument("--version", type=int, default=DEFAULT_VERSION,
                        help=f"黄金集冻结版本号（默认 {DEFAULT_VERSION}）")
    parser.add_argument("--only", type=str, default="",
                        help="定点重采：逗号分隔的 golden_id 列表；只重采这些行，"
                             "其余旧 outcomes 保留合并（默认空 = 全量重采）")
    args = parser.parse_args()
    only = [g.strip() for g in args.only.split(",") if g.strip()] or None
    asyncio.run(main(args.version, only=only))
