"""P0-5(a) 的反向验证：两处修复是否真的被测试守着。

跑法（在 trajectory_pipeline/ 下）：
    uv run python research/probes/p05a_reverse_check.py

背景：2026-11-25 跑出的第一批 agent+B/C 真数据（``p05_bc_agent_20261125``）
3 次 ``stale_context``，全部由「``goto`` 超时后继续用旧清单」触发。
两处修复：

- (1) ``DriverError`` 分支补清 ``_read_elements``（失败 ≠ 页面没动）
- (2) ``StaleContextError`` 从「整单元收口」改成「留证 + 引导重观察」

**各自回退，看对应测试会不会红。** 只测一处红的等于另一处没守住。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

LOOP = "trajectory_pipeline/executor/agent_loop.py"
TEST = "tests/unit/test_agent_loop.py::Test动作流::test_{}"

#: (名字, 文件, 原文, 替换, 测试节点)
CASES: list[tuple[str, str, str, str, str]] = [
    (
        "动作失败不清读到的元素",
        LOOP,
        "            self._read_elements.clear()\n"
        "            self.steps.settle(index, error=f\"{instr.tool} 失败: {exc}\")\n",
        "            self.steps.settle(index, error=f\"{instr.tool} 失败: {exc}\")\n",
        TEST.format("动作失败也清读到的元素"),
    ),
    (
        "stale_context 又变成收口",
        LOOP,
        "            self._note(_GUIDANCE_STALE.format(\n"
        "                tool=instr.tool, expected=exc.expected, actual=exc.actual,\n"
        "            ))\n"
        "            return None\n",
        "            return self._close(\n"
        "                reason=\"定位词来自另一页（接线错误，不是站点事实）\",\n"
        "                verdict=None, branch=UNRESOLVED,\n"
        "                exit_stage=\"agent.stale_context\",\n"
        "                exit_signal=\"stale_context\",\n"
        "                exit_question=self._exit_question(),\n"
        "            )\n",
        TEST.format("定位词来自另一页_不収口且引导重观察"),
    ),
    (
        "引导只说换动作、不说重观察",
        LOOP,
        "    \"**先重新观察当前页面**（interactive / observe），再按新页面上的元素\"\n"
        "    \"决定动作。别拿旧清单重试：那不会成功，只会白烧预算。\"\n",
        "    \"请换一个动作，或如实报告放弃。\"\n",
        TEST.format("定位词来自另一页_不収口且引导重观察"),
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
