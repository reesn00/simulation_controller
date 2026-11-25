"""P0-7 探针：①B 分类题看到的候选视图，以及中间页那三道题到底跑没跑。

跑法（在 trajectory_pipeline/ 下）：
    uv run python research/probes/p07_classify_input.py <存档.json>

回答两个问题：
1. `not_aggregator` 从哪来 —— ①B（搜索页分类，两模式都跑）还是
   ⑤（中间页确认，只有 scripted 跑）？
2. agent 模式下 `is_intermediate` 恒为 None 是「问了答不出」还是
   「压根没问」？
"""
from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    d = json.load(open(argv[0], encoding="utf-8"))
    print(f"存档      : {argv[0]}")
    print(f"executor_mode : {d.get('executor_mode')!r}（None/空 = scripted）")
    print(f"discovery_path: {d.get('discovery_path')!r}")
    print()

    print("── outcomes（逐条看分支与出口）" + "─" * 30)
    for i, o in enumerate(d.get("outcomes") or ()):
        print(f"  [{i}] branch={o.get('branch'):20s} "
              f"exit_stage={o.get('exit_stage') or '-':24s} "
              f"exit_signal={o.get('exit_signal') or '-':16s}")
        print(f"      url={str(o.get('url'))[:70]}")
        q = o.get("exit_question") or ""
        if q:
            print(f"      exit_question={q}")
    print()

    print("── intermediates（中间页）" + "─" * 30)
    for i, ir in enumerate(d.get("intermediates") or (), 1):
        steps = ir.get("steps") or ()
        errs = [s for s in steps if s.get("error")]
        print(f"  #{i} role={ir.get('role')} steps={len(steps)} errs={len(errs)}")
        print(f"      is_intermediate={_ans(ir.get('is_intermediate'))}")
        print(f"      entries        ={_ans(ir.get('entries'))}")
        print(f"      candidates     ={len(ir.get('candidates') or ())}")
        for n in ir.get("notes") or ():
            print(f"      note: {n[:100]}")
    print()

    mode = str(d.get("executor_mode") or "")
    any_q = any(
        ir.get("is_intermediate") is not None or ir.get("entries") is not None
        for ir in (d.get("intermediates") or ())
    )
    print("── 判读" + "─" * 40)
    if mode == "agent":
        print("  mode=agent ⇒ ⑤ IS_AGGREGATOR / ⑥ SELECT_SITE_ENTRIES 按设计**不问**")
        print(f"  is_intermediate/entries 全 None 是**预期**（没问过，不是答不出）：{not any_q}")
        print("  ⚠️ 所以这里出现 not_aggregator 只能来自**①B 分类**（搜索页，两模式都跑）")
    else:
        print("  mode=scripted ⇒ 三道判断题都跑，None = 感知层答不出")
    return 0


def _ans(v) -> str:
    if isinstance(v, dict):
        return f"answer={v.get('answer')!r} source={v.get('source')!r}"
    return repr(v)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
