"""P0-4 修复的反向验证：逐条回退修复，确认对应测试会红。

跑法（在 trajectory_pipeline/ 下）：
    uv run python research/probes/p04_reverse_check.py

每条变异：改源码 → 跑指定测试 → 恢复源码（finally 保证恢复）。
任何一条变异下测试**仍绿** ⇒ 那条测试没有守住它声称守住的东西。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

def _repo_root() -> pathlib.Path:
    """从本文件往上找到含 ``trajectory_pipeline/executor`` 的那一级。

    写死 ``parents[N]`` 试过两次都错在层数上——而层数会随目录调整变，
    找标志目录不会。
    """
    for p in pathlib.Path(__file__).resolve().parents:
        if (p / "trajectory_pipeline" / "executor").is_dir():
            return p
    raise SystemExit("找不到仓库根（trajectory_pipeline/executor 不在任何上级目录里）")


ROOT = _repo_root()

# (名字, 文件, 原文, 改后, 测试节点)
CASES = [
    (
        "队列写不抢出旧裁定",
        "trajectory_pipeline/executor/review_queue.py",
        "    if p.exists():\n"
        "        _rescue_verdicts_from_queue(p)\n",
        "    if False:\n"
        "        _rescue_verdicts_from_queue(p)\n",
        "tests/unit/test_review_queue.py::TestVerdictsFile"
        "::test_旧队列里的裁定在重跑前被抢出来",
    ),
    (
        "队列写不剔除裁定栏" ,
        "trajectory_pipeline/executor/review_queue.py",
        '            row = item.to_json()\n'
        '            for k in ("verdict", "reviewed_by", "note"):\n'
        '                row.pop(k, None)\n',
        '            row = item.to_json()\n',
        "tests/unit/test_review_queue.py::TestIO::test_写出可回读",
    ),
    (
        "队列写不拒绝裁定档路径",
        "trajectory_pipeline/executor/review_queue.py",
        "    if p.name.endswith(VERDICTS_SUFFIX):\n"
        "        raise ReviewQueueError(\n"
        "            f\"{p.name} 是**裁定档**（{VERDICTS_SUFFIX}），"
        "不得用队列写覆盖它；\"\n",
        "    if False:\n"
        "        raise ReviewQueueError(\n"
        "            f\"{p.name} 是**裁定档**（{VERDICTS_SUFFIX}），"
        "不得用队列写覆盖它；\"\n",
        "tests/unit/test_review_queue.py::TestVerdictsFile"
        "::test_队列写拒绝对裁定的覆盖",
    ),
    (
        "apply 读队列而不切裁定档",
        "trajectory_pipeline/executor/cli.py",
        "    v = verdicts_path_for(p)\n"
        "    return v if v.exists() else p\n",
        "    return p\n",
        "tests/unit/test_review_queue.py::TestVerdictsFile"
        "::test_load_verdicts_优先读裁定档",
    ),
    (
        "坏复核档静默回退原档",
        "trajectory_pipeline/executor/cli.py",
        "                broken.append(prev.name)\n"
        "                continue\n",
        "                data = json.loads(path.read_text(encoding=\"utf-8\"))\n",
        "tests/unit/test_pool_add.py::Test回填接线"
        "::test_坏复核档不复算且退出码非零",
    ),
    (
        "复核档非原子写",
        "trajectory_pipeline/executor/cli.py",
        "        write_json_atomic(dst, merged, indent=2)\n",
        "        dst.write_text(json.dumps(merged, ensure_ascii=False, indent=2),\n"
        "                       encoding=\"utf-8\")\n",
        "tests/unit/test_pool_add.py::Test回填接线::test_复核档原子写",
    ),
]


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
            print(f"[!!] {name}: 锚点没命中，变异没发生（{rel}）")
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
        print(f"{len(survivors)}/{len(CASES)} 条变异存活——这些测试没守住：")
        for s in survivors:
            print(f"  - {s}")
        return 1
    print(f"{len(CASES)}/{len(CASES)} 条变异全部杀死")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
