"""``evaluation.golden.alignment`` 单测。

等价类映射是 D10 口径 C 的**唯一出处**（对齐方案 §4.3）——它的正确性
必须由测试钉住，重点不是「映射记得对不对」（那是实现细节），而是：

    1. 分支注册表里的每一条都登记了等价类——新增分支忘登记会让
       D10 假性暴跌（新代码输出与历史标签不相等），或者 fail-closed
       直接炸；两类都该被当场发现；
    2. ``broken`` 与 ``unreachable_hard`` **刻意分离**（内容损坏 vs
       环境导航失败）——有人误合并会改变口径 C 的语义，必须红；
    3. 历史合并态挂入等价类：代码判 ``blocked`` 与历史标注
       ``login_wall_or_blocked`` 算一致，不因口径升级假性下降。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.evaluation.golden import alignment
from trajectory_pipeline.executor.branches import BRANCH_LABELS
from trajectory_pipeline.perception import questions


def test_成功标签有映射():
    assert alignment.equiv_class("success") == "SUCCESS"


def test_分支注册表全部登记等价类():
    """注册表 14 个 key（13 活跃 + 历史合并态）都必须可映射。

    这个测试是「新增分支时同步登记映射」的闸门：``BRANCH_LABELS``
    与 ``EQUIV_CLASS`` 两份清单由它强制同步，漏一张当场红。"""
    registered = {"success"} | set(BRANCH_LABELS)
    missing = registered - set(alignment.EQUIV_CLASS)
    assert not missing, f"分支注册表里这些标签没登记等价类: {sorted(missing)}"


def test_活跃分支全部登记等价类():
    """all_branches() 的 13 条属于注册表子集，单独再钉一次
    （有的实现可能把 deprecated 放注册表、活跃分支走别的表）。"""
    missing = set(questions.all_branches()) - set(alignment.EQUIV_CLASS)
    assert not missing, f"all_branches() 未登记等价类: {sorted(missing)}"


def test_broken_与_unreachable_hard_刻意分离():
    """对齐方案 §4.3：一个是「内容损坏」（页面/媒体在但坏），
    一个是「环境导航失败」（连页面都没到），不是同一等价类。"""
    assert alignment.equiv_class("broken") == "BROKEN_CONTENT"
    assert alignment.equiv_class("unreachable_hard") == "REACH_FAIL"
    assert alignment.equiv_class("broken") != alignment.equiv_class("unreachable_hard")


def test_历史合并态挂入_不计为不一致():
    """代码判细分（blocked）与历史标注合并态（login_wall_or_blocked）
    在等价类口径下应判一致——这正是 D10 升级的原因（§4.3）。"""
    assert alignment.is_equivalent("login_wall_or_blocked", "blocked")
    assert alignment.is_equivalent("login_wall_or_blocked", "login_required")


def test_新标签与旧体系锚点同义():
    assert alignment.is_equivalent("no_play_control", "empty")
    assert alignment.is_equivalent("component_unverified", "empty")
    assert alignment.is_equivalent("unresolved", "unknown")
    assert alignment.is_equivalent("subscription_wall", "paywall")
    assert alignment.is_equivalent("partial_free", "paywall")


def test_不同等价类_判不一致():
    assert not alignment.is_equivalent("success", "subscription_wall")
    assert not alignment.is_equivalent("broken", "unreachable_hard")
    assert not alignment.is_equivalent("trailer_only", "not_play_site")


def test_未知标签_fail_closed_抛KeyError():
    """未登记标签静默放行会把拼错的分支名算进一致率——fail-closed：
    谁漏登记谁当场炸。"""
    with pytest.raises(KeyError):
        alignment.equiv_class("not_a_branch")


def test_全量等价类名由映射派生():
    assert alignment.ALL_EQUIV_CLASSES == frozenset(alignment.EQUIV_CLASS.values())
    assert len(alignment.ALL_EQUIV_CLASSES) >= 8


# --------------------------------------------------------------------------
# ``dim_agreement`` —— D10 软维度一致率判定（plan §三·第一步）
# --------------------------------------------------------------------------


def test_dim_agreement_默认_tolerance_档差1_一致():
    """默认 ±1 容忍：档差 0 / 1 都一致；档差 >= 2 → False；越界仍是 None。"""
    assert alignment.dim_agreement(4, 4) is True
    assert alignment.dim_agreement(4, 3) is True
    assert alignment.dim_agreement(4, 5) is True
    assert alignment.dim_agreement(1, 2) is True
    assert alignment.dim_agreement(4, 2) is False
    # 6 越界（scale=1..5），按 fail-closed 当 None 处理，不计分母
    assert alignment.dim_agreement(4, 6) is None


def test_dim_agreement_tolerance_0_只允许相等():
    assert alignment.dim_agreement(4, 4, tolerance=0) is True
    assert alignment.dim_agreement(4, 5, tolerance=0) is False


def test_dim_agreement_tolerance_2_放宽():
    assert alignment.dim_agreement(4, 2, tolerance=2) is True
    assert alignment.dim_agreement(4, 1, tolerance=2) is False


def test_dim_agreement_任一侧None_返None_不计分母():
    """``score=None`` 是不可判定，与 0 分（不在 1-5 档差）语义不同；
    plan §二.1 三态纪律。"""
    assert alignment.dim_agreement(None, 4) is None
    assert alignment.dim_agreement(4, None) is None
    assert alignment.dim_agreement(None, None) is None


def test_dim_agreement_越界值返None_fail_closed():
    """scale 默认 (1, 5)；0 / 99 视为不可判定。"""
    assert alignment.dim_agreement(0, 4) is None
    assert alignment.dim_agreement(6, 4) is None
    assert alignment.dim_agreement(4, 0) is None
    assert alignment.dim_agreement(4, 99) is None


def test_dim_agreement_负tolerance_返None():
    """负 tolerance 无意义；按不可判定处理。"""
    assert alignment.dim_agreement(4, 4, tolerance=-1) is None


def test_dim_agreement_非数值类型返None():
    """int / float 之外走 ``int()`` 失败 → 不可判定。"""
    # ``int("4") == 4`` 是合法的：``int()`` 不要求类型，只要求内容可解析
    assert alignment.dim_agreement("4", "4") is True
    # 真非数值（"x"）→ ``int("x")`` 抛 ValueError → 返回 None
    assert alignment.dim_agreement(4, "x") is None


# --------------------------------------------------------------------------
# ``SOFT_DIM_NAMES`` / ``validate_soft_dim_name`` —— 白名单 + fail-closed
# --------------------------------------------------------------------------


def test_SOFT_DIM_NAMES_是d2到d9子集():
    """白名单是 ``d2..d9`` 共 8 个。d1 不在——是路径占位，不是评分维度。"""
    assert alignment.SOFT_DIM_NAMES == frozenset(
        {f"d{i}" for i in range(2, 10)}
    )
    assert "d1" not in alignment.SOFT_DIM_NAMES
    assert "d10" not in alignment.SOFT_DIM_NAMES


def test_validate_soft_dim_name_白名单内_原样返回():
    for d in ("d2", "d3", "d5", "d9"):
        assert alignment.validate_soft_dim_name(d) == d


def test_validate_soft_dim_name_白名单外_fail_closed():
    """拼错的 dim 静默放过会让等价类的价值归零——plan contract 1。"""
    for bad in ("d1", "d10", "rationale_consistency", "", "D3"):
        with pytest.raises(ValueError, match="不在白名单"):
            alignment.validate_soft_dim_name(bad)
