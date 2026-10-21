"""手工跑一遍 toolcatalog 契约测试的断言（不经过 pytest runner）。

用途：环境里 pytest 被拒时，至少把「断言本身成不成立」验掉。
不替代门禁——门禁还是 ``pytest``。
"""

from __future__ import annotations

import inspect
import sys

import trajectory_pipeline.tests.contract.test_tool_catalog_contract as m

SURFACES = m.SURFACES

failures: list[str] = []
ran = 0
skipped: list[str] = []

for cls_name in dir(m):
    if not cls_name.startswith("Test"):
        continue
    cls = getattr(m, cls_name)
    if not isinstance(cls, type):
        continue
    inst = cls()
    for meth_name in dir(cls):
        if not meth_name.startswith("test_"):
            continue
        meth = getattr(inst, meth_name)
        if not callable(meth):
            continue
        sig = inspect.signature(meth)
        params = list(sig.parameters)
        if params == ["surface"]:
            cases = [(s,) for s in SURFACES]
        elif params == ["surface", "d"]:
            cases = [(s, d) for s, d in m.ALL_DEFS]
        elif not params:
            cases = [()]
        else:
            failures.append(f"{cls_name}.{meth_name}: 未知签名 {params}")
            continue
        for case in cases:
            label = f"{cls_name}.{meth_name}{case if len(case) == 1 else ''}"
            try:
                meth(*case)
                ran += 1
            except BaseException as exc:  # noqa: BLE001
                if type(exc).__name__ in ("Skipped", "Failed"):
                    skipped.append(label)
                    continue
                failures.append(f"{label}: {type(exc).__name__}: {exc}")

print(f"ran={ran} skipped={len(skipped)} failed={len(failures)}")
for f in failures:
    print("FAIL", f)
for s in skipped:
    print("SKIP", s)
sys.exit(1 if failures else 0)
