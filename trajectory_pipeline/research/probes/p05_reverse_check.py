"""P0-5 契约的反向验证：两条新测试是否真的在守。

跑法（在 trajectory_pipeline/ 下）：
    uv run python research/probes/p05_reverse_check.py

P0-5 走的是「写清契约、不动代码」，所以它的风险与 P0-4 不同：
**风险是下一个人读不到那行契约，顺手把中间页接进 P2**（或反过来删掉文档）。
两条测试各守一个方向，这里逐条回退看它们会不会红。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

SCHEMA = "trajectory_pipeline/assembler/schema.py"
DOC = "trajectory_pipeline/docs/contracts/P2-sample.md"
TEST = ("tests/contract/test_p2_contract.py::Test中间页不进样本契约"
        "::test_{}")

CASES = [
    (
        "中间页被接进 P2（代码侧）",
        SCHEMA,
        "    for i, visit in enumerate(archive.get(\"visits\") or (), 1):\n",
        "    for i, ir in enumerate(archive.get(\"intermediates\") or (), 1):\n"
        "        units.append(_Unit(f\"agg-{i}\", tuple(ir.get(\"steps\") or ()), ir))\n"
        "    for i, visit in enumerate(archive.get(\"visits\") or (), 1):\n",
        TEST.format("代码确实不收中间页"),
    ),
    (
        "契约从文档里消失",
        DOC,
        "### `intermediates[]` 不进样本",
        "### （本条已删除）",
        TEST.format("契约文档写了这条"),
    ),
]


def _repo_root() -> pathlib.Path:
    for p in pathlib.Path(__file__).resolve().parents:
        if (p / "trajectory_pipeline" / "executor").is_dir():
            return p
    raise SystemExit("找不到仓库根")


ROOT = _repo_root()


def run(node: str) -> bool:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "--no-header", "-x"],
        cwd=ROOT / "trajectory_pipeline", capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return r.returncode == 0


def main() -> int:
    survivors: list[str] = []
    for name, rel, old, new, node in CASES:
        p = ROOT / rel
        src = p.read_text(encoding="utf-8")
        if old not in src:
            print(f"[!!] {name}: 锚点没命中")
            survivors.append(name + "（锚点未命中）")
            continue
        try:
            p.write_text(src.replace(old, new, 1), encoding="utf-8")
            ok = run(node)
        finally:
            p.write_text(src, encoding="utf-8")
        print(f"[{'存活' if ok else '杀死'}] {name}")
        if ok:
            survivors.append(name)
    print()
    if survivors:
        print(f"{len(survivors)}/{len(CASES)} 条变异存活：")
        for s in survivors:
            print(f"  - {s}")
        return 1
    print(f"{len(CASES)}/{len(CASES)} 条变异全部杀死")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
