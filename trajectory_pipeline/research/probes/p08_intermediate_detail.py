import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "output/pipeline/p08_bc_agent_20261125")
print("存档 | 中间页 | 步数 | 失败步 | 动作构成 | 候选 | 结局")
for p in sorted(root.glob("*.json")):
    d = json.loads(p.read_text(encoding="utf-8"))
    for i, ir in enumerate(d.get("intermediates") or (), 1):
        st = ir.get("steps") or ()
        errs = [s for s in st if s.get("error")]
        kinds: dict[str, int] = {}
        for s in st:
            t = (s.get("action") or {}).get("tool") or "?"
            kinds[t] = kinds.get(t, 0) + 1
        notes = " ".join(ir.get("notes") or ())
        outcome = "预算耗尽" if "预算耗尽" in notes else (
            "超时" if notes.strip() == "超时" else (
                "导航失败" if "导航失败" in notes else (
                    "异常" if "异常" in notes else "其他")))
        print(f"{p.stem[:20]:20s} ir{i} {len(st):3d} {len(errs):3d} "
              f"{str(kinds):44s} {len(ir.get('candidates') or ()):3d} {outcome}")
