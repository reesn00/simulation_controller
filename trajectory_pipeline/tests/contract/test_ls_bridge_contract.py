"""ls_bridge 契约测试: import 边界 + 控件名一致性 + XML 标签白名单。

沿用 ``tests/contract/test_scout_contract.py`` 的风格: 交接面是格式与常量,
不是运行时行为。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

LS_BRIDGE_DIR = Path("trajectory_pipeline/evaluation/ls_bridge")
XML_PATH = LS_BRIDGE_DIR / "label_configs" / "trajectory_review_v2.xml"

#: ls_bridge 允许 import 的 trajectory_pipeline 模块（依赖方向:
#: evaluation 层 → storage 读 P3 / scout 纯类型; **禁** executor/assembler）。
_ALLOWED_TP_IMPORTS = {
    "trajectory_pipeline.evaluation.ls_bridge",
    "trajectory_pipeline.evaluation.scout.types",
    "trajectory_pipeline.storage",
    "trajectory_pipeline.storage.read_p3",
}


def _sources() -> list[Path]:
    return sorted(p for p in LS_BRIDGE_DIR.glob("*.py") if p.name != "__init__.py")


def _imports(path: Path) -> set[str]:
    """抓所有 import 的模块名(绝对 + 相对, 含函数内延迟 import)。"""
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


# ── A1: 不 import archive, 依赖白名单 ─────────────────────────────


class TestImportBoundary:
    """ls_bridge 是照读重写(CLAUDE.md: archive/v1 禁止 import),
    且依赖方向必须锁死: evaluation 层, 只读 storage 与 scout 纯类型。
    """

    @pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
    def test_不import_archive(self, path: Path) -> None:
        for mod in _imports(path):
            assert not mod.startswith("archive"), (
                f"{path.name} import 了 {mod}; archive/v1 禁止 import, 只能照读重写"
            )

    @pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
    def test_不import旧包名label_studio(self, path: Path) -> None:
        for mod in _imports(path):
            assert not mod.startswith("label_studio"), (
                f"{path.name} import 了 {mod}; v1 的顶层包名不存在于 v2 环境"
            )

    @pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
    def test_trajectory_pipeline依赖白名单(self, path: Path) -> None:
        offenders = {
            mod for mod in _imports(path)
            if mod.startswith("trajectory_pipeline")
            and not any(
                mod == allowed or mod.startswith(allowed + ".")
                for allowed in _ALLOWED_TP_IMPORTS
            )
        }
        assert not offenders, (
            f"{path.name} import 了白名单外的模块: {sorted(offenders)}; "
            "ls_bridge 只准依赖 storage(读 P3) 与 scout.types(纯类型)"
        )

    @pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
    def test_第三方依赖只有httpx(self, path: Path) -> None:
        allowed = {"httpx"}
        for mod in _imports(path):
            if mod.split(".")[0] in ("trajectory_pipeline", "archive", "label_studio"):
                continue
            root = mod.split(".")[0]
            if root in ("", "."):  # 相对 import(包内)
                continue
            assert root in allowed or root in sys.stdlib_module_names, (
                f"{path.name} import 了 {mod}; 第三方只允许 httpx"
            )


# ── A2: 控件名与 label_config 逐字一致 ────────────────────────────


class TestControlNameSync:
    """``import/predictions`` 按 ``from_name`` 匹配控件, 对不上时 LS 静默
    丢弃整条预测(201 {"created": 0}) —— v1 实测教训, 必须静态盯住。
    """

    def test_risk_hints控件名与XML一致(self) -> None:
        from trajectory_pipeline.evaluation.ls_bridge.exporter import (
            RISK_HINTS_CONTROL,
            TASK_ANCHOR_CONTROL,
        )
        xml = XML_PATH.read_text(encoding="utf-8")
        assert f'name="{RISK_HINTS_CONTROL}"' in xml, (
            f"label_config 缺少控件 {RISK_HINTS_CONTROL}; "
            "predictions 将被 LS 静默丢弃"
        )
        assert f'name="{TASK_ANCHOR_CONTROL}"' in xml, (
            f"label_config 缺少锚点 {TASK_ANCHOR_CONTROL}"
        )

    def test_task_data键都有XML占位(self) -> None:
        """task.data 的展示键必须能在 XML 里找到 ``$key`` 占位,
        否则标注页该块渲染为空 —— 且 import 不报错。
        """
        xml = XML_PATH.read_text(encoding="utf-8")
        for key in (
            "task_id", "sample_id", "status", "tags_text", "audit_text",
            "scorecard_text", "risk_hints_text", "messages_text",
            "openai_text", "meta_text",
        ):
            assert f"${key}" in xml, f"label_config 缺少占位符 ${key}"

    def test_负例阈值与scout一致(self) -> None:
        from trajectory_pipeline.evaluation.ls_bridge.scorecard import (
            NEGATIVE_SCORE_THRESHOLD,
        )
        from trajectory_pipeline.evaluation.scout.policy import (
            NEGATIVE_SCORE_THRESHOLD as SCOUT_THRESHOLD,
        )
        assert NEGATIVE_SCORE_THRESHOLD == SCOUT_THRESHOLD, (
            "scorecard 的负例阈值漂移了; 两边必须同口径"
        )


# ── A3: XML 只用已注册标签 ────────────────────────────────────────


class TestOnlyRegisteredTags:
    """v1 实测: <Tabs>/<Tab>/<List>/<TextEditor> 在本机 LS 1.23 未注册
    或不渲染; 标注页直接报 "Tag ... is not registered"。白名单静态守住。

    扫描前先剥掉 ``<!-- ... -->`` 注释 —— 注释里引用这些名字正是为了
    警告别用它们, 不能被误判。
    """

    REGISTERED = {"View", "Header", "Text", "TextArea", "Choices", "Choice", "Style"}

    @staticmethod
    def _xml_without_comments() -> str:
        return re.sub(r"<!--.*?-->", "", XML_PATH.read_text(encoding="utf-8"), flags=re.S)

    def test_只用已注册标签(self) -> None:
        xml = self._xml_without_comments()
        used = set(re.findall(r"<([A-Za-z]+)[\s>]", xml))
        unknown = used - self.REGISTERED
        assert not unknown, (
            f"label_config 用了未注册标签: {sorted(unknown)}; "
            "换标签形态前必须先在标注页实测(见 v1 playbook)"
        )

    def test_Choice不用alias(self) -> None:
        """alias= 会把 LS 认的选项标识从 value 静默换成 alias —— v1 实测。"""
        xml = self._xml_without_comments()
        assert "alias=" not in xml, "label_config 用了 alias=; 会静默换掉选项契约键"

    def test_隐藏锚点是容器display_none(self) -> None:
        """隐藏只能靠容器 <View style="display:none">; <Text visible=...>
        属性是无声无效的 —— v1 实测。"""
        xml = self._xml_without_comments()
        assert 'style="display:none"' in xml
        assert "visible=" not in xml
