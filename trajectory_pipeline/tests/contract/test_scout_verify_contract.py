"""scout.verify 契约:模块边界 + 关键不变量。

盯:

* 不 import executor / assembler(evaluation 层纪律)
* 不 import 仓库根 output/(D8 隔离)
* :data:`EXPECTED_STATS_KEYS` 三键固定,与 :data:`ScoutStatus` 字面量对齐
* :class:`VerifyReport` 关键属性稳定(ok / error_count / warning_count)
* 模块本身**只读**(不写盘)——grep 验证
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trajectory_pipeline.evaluation.scout import verify
from trajectory_pipeline.evaluation.scout.verify import (
    EXPECTED_STATS_KEYS,
    VerifyIssue,
    VerifyReport,
)

VERIFY_PATH = Path("trajectory_pipeline/evaluation/scout/verify.py")


def _imports(path: Path) -> set[str]:
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


# ── A: 不依赖 executor / assembler ─────────────────────


class TestImportBoundary:
    @pytest.mark.parametrize(
        "mod",
        ["trajectory_pipeline.executor", "trajectory_pipeline.assembler"],
    )
    def test_不import_executor_或_assembler(self, mod: str):
        offenders = {
            i for i in _imports(VERIFY_PATH) if i.startswith(mod)
        }
        assert not offenders, (
            f"verify.py 偷偷依赖 {mod}: {offenders}; "
            "scout.verify 是 evaluation 层,不允许跨到执行/组装"
        )

    def test_不读仓库根output(self):
        offenders = {
            i for i in _imports(VERIFY_PATH)
            if "output" in i and "trajectory_pipeline" not in i
        }
        assert not offenders, (
            f"verify.py 引用了 {offenders}; verify 只读 scout 产物 + T 视图,"
            "T 视图路径由 storage.task_views_dir 提供,不在 verify 里写死仓库根 output/"
        )


# ── B: 关键不变量 ────────────────────────────────────────


class TestStableContract:
    def test_三键固定(self):
        assert EXPECTED_STATS_KEYS == ("scored", "undetermined", "not_run")

    def test_VerifyIssue是frozen_dataclass(self):
        # frozen → 不能在创建后改 code/severity
        issue = VerifyIssue(code="missing_t_view", severity="warning", detail="x")
        with pytest.raises((AttributeError, Exception)):
            issue.code = "changed"  # type: ignore[misc]

    def test_VerifyReport默认ok(self):
        assert VerifyReport().ok
        assert VerifyReport().error_count == 0
        assert VerifyReport().warning_count == 0

    def test_VerifyReport_warning不破坏ok(self):
        r = VerifyReport(issues=[
            VerifyIssue(code="missing_t_view", severity="warning", detail=""),
        ])
        assert r.ok
        assert r.warning_count == 1

    def test_反例_error必破ok(self):
        r = VerifyReport(issues=[
            VerifyIssue(code="missing_jsonl", severity="error", detail=""),
        ])
        assert not r.ok

    def test_公开符号不引executor_assembler(self):
        # 出口处 __all__ 内的名字,递归看源码不 import executor/assembler
        import inspect
        for name in verify.__all__:
            obj = getattr(verify, name)
            try:
                src = inspect.getsource(obj)
            except (TypeError, OSError):
                continue
            assert "from trajectory_pipeline.executor" not in src
            assert "from trajectory_pipeline.assembler" not in src
