"""scout 契约测试:交接面是格式不是 import(沿用 tests/contract/test_p2_contract.py 的风格)。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCOUT_DIR = Path("trajectory_pipeline/evaluation/scout")


def _sources() -> list[Path]:
    return sorted(p for p in SCOUT_DIR.glob("*.py") if p.name != "__init__.py")


def _imports(path: Path) -> set[str]:
    """抓所有 import 的模块名(绝对 + 相对)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                mod = f"{'.' * node.level}{mod}"
            out.add(mod)
    return out


# ── A1:scout 不 import executor 顶层 ────────────────────────────


class TestNoExecutorTopLevelImport:
    """scout 必须只走 P2 文件格式,不 import executor 运行时。

    理由:scout 是「评估侧的纯计算」,如果 import executor 顶层,
    executor 改 dataclass / 字段名都会让 scout 编译红,
    而 scout 自身用不到那些字段。
    """

    @pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
    def test_不import_executor_top_level(self, path: Path) -> None:
        for mod in _imports(path):
            assert not mod.startswith("trajectory_pipeline.executor"), (
                f"{path.name} import 了 {mod};"
                "scout 不该绑死 executor 当前版本"
            )


# ── A2:fail_closed.py 字面量唯一性 ──────────────────────────────


class TestFailClosedLiteralSync:
    """scout/fail_closed.py 的 L5/AUTO 字面量必须从 executor 镜像,**不**许复制。"""

    FAIL_CLOSED_LITERALS = (
        "credential_leak", "driver_missing", "timeout", "driver_error",
        "vlm_error",
    )

    def test_只import不写字面量(self) -> None:
        p = SCOUT_DIR / "fail_closed.py"
        src = p.read_text(encoding="utf-8")
        # 该模块只允许 import L5_FAIL_CLOSED_NOTES / AUTO_REVIEW_NOTES
        # 不许手写这些字符串
        for tok in self.FAIL_CLOSED_LITERALS:
            assert tok not in src, (
                f"fail-closed 字面量 {tok!r} 出现于 {p.name};"
                "只能从 executor.arbitration.L5_FAIL_CLOSED_NOTES 或"
                "executor.review_queue.AUTO_REVIEW_NOTES 镜像"
            )

    def test_导入的是真源常量(self) -> None:
        # scout 子包不直接 import executor;真源镜像在
        # evaluation 包内的 _fail_closed_notes。锁这个链路的两端:
        # 1. 镜像模块从 executor 真源 import
        # 2. scout 子包从镜像模块 import
        mirror = Path("trajectory_pipeline/evaluation/_fail_closed_notes.py")
        mirror_imps = _imports(mirror)
        assert "trajectory_pipeline.executor.arbitration" in mirror_imps
        assert "trajectory_pipeline.executor.review_queue" in mirror_imps

        scout_fc = SCOUT_DIR / "fail_closed.py"
        scout_imps = _imports(scout_fc)
        assert "trajectory_pipeline.evaluation._fail_closed_notes" in scout_imps
        # 反向:scout 自己不能再 import executor,否则破坏「不绑死当前版本」约束
        assert not any(
            m.startswith("trajectory_pipeline.executor")
            for m in scout_imps
        )


# ── A3:scout cli 是独立入口 ──────────────────────────────────────


class TestScoutCliIsStandalone:
    """scout/cli.py 是 evaluation.cli 的 sibling,不是其内部函数。"""

    def test_cli_可被runpy_直接调用(self) -> None:
        # 仅 import,不 invoke;以防环境变化让 main() 跑起来
        from trajectory_pipeline.evaluation.scout import cli as _cli  # noqa: F401
        assert hasattr(_cli, "main")
        assert callable(_cli.main)
