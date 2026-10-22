"""执行层装配入口的单元测试——**生产代码里不许直接 new 驱动**。

这条不变式为什么值得一个门禁
--------------------------
``cli.py`` 曾经有 6 处 ``ObscuraDriver(client)``。硬编码不痛，痛的是
**换实现时会漏一处**：漏掉的那条命令继续跑旧实现，而两边的 P1 存档结构
一模一样——混池统计时**看不出来**（方案 04 §8 禁止混池）。

所以这里守两件事：

1. ``make_driver`` 认不出实现名时**报错不回落**——静默回落到默认实现，
   会让「配错了」与「配对了」产出同一份数据；
2. 生产代码（``executor/browser/`` 之外、``tests/`` 与 ``research/`` 之外）
   **不许 import 或构造具体驱动类**。第 7 处硬编码要在这里红，而不是在
   换实现那天才被发现。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trajectory_pipeline.executor.browser.driver_factory import (
    DEFAULT_KIND,
    SUPPORTED_KINDS,
    make_driver,
)

ROOT = Path(__file__).resolve().parents[2]          # trajectory_pipeline/
BROWSER_DIR = ROOT / "executor" / "browser"

#: 显式豁免：``tests/`` 是替身，``research/`` 是取证脚本（每支探针
#: **故意钉住某一个实现**——探针跟着默认实现漂，就不是取证了）。
EXEMPT_DIRS = ("tests", "research")


class _StubClient:
    """驱动构造只存引用、不调 tool，所以这里只需一个不透明对象。"""


class TestMakeDriver:
    def test_默认实现是camoufox(self):
        """**2026-10-22 owner 裁定**：生产链默认实现 = camoufox。

        改的是 :data:`DEFAULT_KIND` 这一处事实，不是六处调用点各自的
        隐式约定——所以这条断言是「默认实现是什么」的唯一真源。
        """
        assert DEFAULT_KIND == "camoufox"
        assert DEFAULT_KIND in SUPPORTED_KINDS

    def test_默认实现仍在白名单里(self):
        """白名单与默认值的关系要被钉住：默认值必须在表内，
        否则「默认实现」本身就是一个认不出的名字。"""
        assert DEFAULT_KIND in SUPPORTED_KINDS

    def test_默认调用不带会话就报错不回落(self):
        """默认值改成 camoufox 之后，**裸调用必须报错**。

        如果这里悄悄回落到 obscura，那么「忘了传 session」会跑出
        一份 obscura 产出的数据，而调用方以为自己在跑 camoufox——
        两边产出物结构一模一样，**混池统计时看不出来**。
        """
        with pytest.raises(ValueError) as exc:
            make_driver(_StubClient())
        assert "CamoufoxSession" in str(exc.value)

    def test_显式点名obscura仍然可用(self):
        """obscura **没被删掉**——它是存量产物的方言与口径回溯参照系。
        「不再比对」≠「删掉实现」。"""
        driver = make_driver(_StubClient(), kind="obscura")
        assert driver.name == "obscura"

    def test_未知实现名报错不回落(self):
        """回落会让「配错了」产出与「配对了」同一份数据。"""
        with pytest.raises(ValueError) as exc:
            make_driver(_StubClient(), kind="playwright")
        assert "playwright" in str(exc.value)
        assert "obscura" in str(exc.value)

    def test_camoufox缺会话时报错不代建(self):
        """P1 的 camoufox 分支**不自己 launch**——一个 Run 一次 launch，
        生命周期由调用方持有（方案 §4.3）。代建会让「谁该关浏览器」失主。"""
        with pytest.raises(ValueError) as exc:
            make_driver(kind="camoufox")
        assert "CamoufoxSession" in str(exc.value)

    def test_obscura缺连接时报错不代建(self):
        with pytest.raises(ValueError) as exc:
            make_driver(kind="obscura")
        assert "McpClient" in str(exc.value)

    def test_实现名不区分大小写以外的猜测一律拒绝(self):
        """**不做规范化**：``Obscura`` / ``obscura-v2`` 都不认。
        猜名字等于替调用方决定跑哪个实现。"""
        for bad in ("Obscura", "obscura ", "obscura-v2", ""):
            with pytest.raises(ValueError):
                make_driver(_StubClient(), kind=bad)


def _production_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts and rel.parts[0] in EXEMPT_DIRS:
            continue
        if path.is_relative_to(BROWSER_DIR):
            # 实现本体与 ``probe_*.py`` 都在这里：探针**故意**指名实现。
            continue
        files.append(path)
    return sorted(files)


def _violations(path: Path, names: set[str]) -> list[str]:
    # ``utf-8-sig``：仓里有带 BOM 的源文件（``evaluation/golden/builder.py``），
    # BOM 对 Python 解释器合法、对 ``ast.parse`` 是非法字符。
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.rsplit(".", 1)[-1] in ("obscura_driver", "camoufox_driver"):
                imported = {a.name for a in node.names} & names
                if imported:
                    found.append(
                        f"{path}:{node.lineno} 直接 import {sorted(imported)}"
                    )
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else ""
            )
            if name in names:
                found.append(f"{path}:{node.lineno} 直接构造 {name}(…)")
    return found


class TestNoHardcodedDriver:
    def test_生产代码只有一个装配入口(self):
        """第 7 处硬编码要在这里红。"""
        names = {"ObscuraDriver", "CamoufoxDriver"}
        violations = [
            v for path in _production_files() for v in _violations(path, names)
        ]
        assert not violations, (
            "生产代码里出现了具体驱动的构造/import；走 "
            "driver_factory.make_driver() 这一个入口：\n"
            + "\n".join(violations)
        )