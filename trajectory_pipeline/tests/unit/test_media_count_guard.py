"""通用守卫：**媒体计数不许许诺成功**（2026-10-23 · M-26）。

M-26 修掉的是一道具体的短路，但它其实是**一类病**：代码层凭
「页面上有 ``<video>`` 标签」就断言**成功**。这条守卫按**语义**扫
两个感知层模块，抓任何

    「判据里出现媒体计数 ⇒ 分支里 ``return`` 常量 ``True``」

的形状——不论它写 ``video_tag_count``、``media_count`` 还是经一个
中间变量 ``media`` 绕一道（规则版 ``_player_ok`` 就是绕过的那个）。

**为什么用 AST 而不是字符串匹配**：字符串会被注释与墓碑文案骗过去
（改了实现却留着旧文案、或反过来），而 AST 只看**真正会执行的结构**。

⚠️ **这条守卫认不出的形态**：如果有人把计数先存进一个与媒体无关的名字
（``tags = obs.video_tag_count`` ⇒ ``if tags >= 1: return True``），
守卫抓不到——那种得靠代码评审。守卫的价值是**拦住最自然的那种回退**
（把删掉的那段照抄回来）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trajectory_pipeline.perception import llm_perceptor, rule_perceptor

#: 媒体计数相关的变量名。**只认这三个**——``iframe_count`` 不算
#: （iframe 数量从来不是「是不是播放器」的判据，见
#: :func:`~trajectory_pipeline.perception.rule_perceptor.RulePerceptor._player_ok`）。
_MEDIA_NAMES = frozenset({"video_tag_count", "media_count", "media"})


def _mentions_media(node: ast.AST) -> bool:
    """子树里是否出现媒体计数的变量名。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in _MEDIA_NAMES:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in _MEDIA_NAMES:
            return True
    return False


def _returns_true(nodes: ast.AST | list[ast.stmt]) -> bool:
    """子树里是否有 ``return True`` / ``return 常量真``。"""
    if isinstance(nodes, ast.AST):
        nodes = [nodes]
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Constant):
                if sub.value.value is True:
                    return True
    return False


MODULES = [
    pytest.param(llm_perceptor, id="llm_perceptor"),
    pytest.param(rule_perceptor, id="rule_perceptor"),
]


@pytest.mark.parametrize("module", MODULES)
def test_媒体计数不许直接产出成功(module) -> None:
    """任何「判据含媒体计数 ⇒ 直接 ``return True``」的形状都不许存在。

    护栏可以**否**，不能**许**——媒体计数只能当**必要条件**，
    不能当**充分条件**（台账 §M-26）。
    """
    src = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not _mentions_media(node.test):
            continue
        if _returns_true(node.body):
            offenders.append(node.lineno)
    assert not offenders, (
        f"{module.__name__} 里出现了「凭媒体计数直接判成功」的形状，"
        f"行号 {offenders}——媒体计数只能当必要条件（M-26）"
    )


@pytest.mark.parametrize("module", MODULES)
def test_短路墓碑注释还在(module) -> None:
    """删掉的那道短路必须留**墓碑注释**——否则下一个人会按
    「确定性事实优先」把它加回来（那正是 M-26 的由来）。"""
    src = Path(module.__file__).read_text(encoding="utf-8")
    assert "M-26" in src, f"{module.__name__} 没有留下 M-26 的说明"
