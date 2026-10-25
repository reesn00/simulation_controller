"""阈值唯一真源（方案 16 C-14）——owner 2026-10-23 裁定 D-C6。

裁定内容：**阈值调整不算 rubric 级变更**。

这与 C-14 原本的修法相反（原本是「按 rubric 级变更处理」＝人裁 + 版本 +
记录）。owner 裁完之后，阈值的三条边界是：

- **不**升 :data:`trajectory_pipeline.evaluation.scorers.rubric_version.RUBRIC_VERSION`
  ——改阈值与改 rubric 是两件事，版本号不该被阈值搅浑；
- **不**进 rubric 变更记录（那是 rubric 文档里的九列表，阈值不是条款）；
- **不**设 owner 签名门。

但「不算 rubric 级变更」**不等于「可以无声改」**——C-14 当初要防的就是
「差一点点就把线调低」。所以阈值有了自己的面：

- 唯一真源在 :data:`THRESHOLDS`（每条带值 / 定线状态 / 依据 / 使用处）；
- 使用处一律 ``thresholds.value("<name>")`` 取值，**不许各写一份字面量**；
- 契约测试盯两件机械的事：表里每条都真的被引用（表不腐）、比率型阈值
  的字面量不再出现在使用模块里。

⚠️ 与 D-C9 互相咬的地方（owner 裁定把 D-C9 的后半句推翻了）
----------------------------------------------------------
D-C9 原话：「新线第一批只出读数，**定线本身按 rubric 级变更（D-C6）走**」。
D-C6 既已裁定「阈值调整不算 rubric 级变更」，**「定线走 rubric 变更流程」
这条随之作废**——定线不进 rubric 变更记录，只进本表（``status`` 从
``只出读数`` 改成 ``已定线``）。两条裁定咬在这里，写出来免得后人在
D-C9 里读到一条已经作废的路径。

范围
----
本表只收**判定线**（一致率上岗线 / 类回归容差 / 触发线 / 软维度档差容忍）。
**不收**：切分比例容差（``SPLIT_PP_TOLERANCE``——那是数据形状不是判定）、
LLM 输入体量上限（``D3_BODY_CHARS`` 之类——那是成本不是判据）。

fail-closed
-----------
取未登记的阈值名 → ``KeyError``，不返回默认值。**猜一个默认值就是放水**
——「差一点点就把线调低」正是从一个默认 0.0 开始的。
"""

from __future__ import annotations

from typing import Any

#: 阈值表——**唯一真源**。每条四栏：
#:
#: - ``value``：阈值本体；
#: - ``status``：``已定线`` / ``只出读数``（D-C9：新线第一批只出读数，
#:   线还没定 ⇒ 只报读数，不报合格 / 不合格）；
#: - ``basis``：这条线哪来的（哪个裁定 / 哪份文档）；
#: - ``where``：使用它的模块（相对仓库根，**可多个**）——契约测试按它核
#:   「表里这条确实被用到」。
#:
#: ⚠️ 改这个表**不改** rubric 版本，也**不进** rubric 变更记录（D-C6 裁定）。
THRESHOLDS: dict[str, dict[str, Any]] = {
    "d10_min_agreement": {
        "value": 0.80,
        "status": "已定线",
        "basis": "D-C9 口径 C（语义等价类一致）；上岗线由 0.90 下调至 0.80（owner 2026-10-24 裁定）",
        "where": ("evaluation/golden/builder.py",),
    },
    "class_regression_tolerance": {
        "value": 0.02,
        "status": "已定线",
        "basis": (
            "类 F1 下降容差（等价类 per-class F1）。原 0.0（任何下降都驳回）；"
            "放宽至 0.02（owner 2026-10-25 裁定，D-C6 阈值变更）——认可 v13 "
            "两处类回归均在容差内：NO_PLAY_RESOURCE -0.011、UNKNOWN -0.010"
        ),
        "where": (
            "evaluation/calibration/verify.py",
            "evaluation/calibration/gate.py",
        ),
    },
    "t1_eq_class_rate": {
        "value": 0.10,
        "status": "只出读数",
        "basis": "T-1 候选线（单条款等价类分歧率），暂定未正式定线",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "t1_min_n": {
        "value": 20,
        "status": "只出读数",
        "basis": "T-1 候选线的样本量门槛（n < 门槛不判越线）",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "t2_hard_kappa": {
        "value": 0.70,
        "status": "已定线",
        "basis": "D-C9 沿用上岗线（judge ↔ 人工一致率，硬维度 Cohen's κ）",
        "where": (
            "evaluation/calibration/triggers.py",
            "evaluation/golden/builder.py",
        ),
    },
    "t2_soft_kappa": {
        "value": 0.50,
        "status": "已定线",
        "basis": "D-C9 沿用上岗线（软维度线性加权 κ）",
        "where": (
            "evaluation/calibration/triggers.py",
            "evaluation/golden/builder.py",
        ),
    },
    "t2_drop_delta": {
        "value": -0.05,
        "status": "已定线",
        "basis": "D-C9：相邻两版一致率下降超过这个幅度 = 触发回归检查",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "t3_vague_rate": {
        "value": 0.30,
        "status": "只出读数",
        "basis": "T-3 候选线（理由含糊率），暂定未正式定线",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "t4_mode_share": {
        "value": 0.80,
        "status": "只出读数",
        "basis": "T-4 候选线：众数占比过高 = 取值分布退化",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "t4_distinct_values": {
        "value": 2,
        "status": "只出读数",
        "basis": "T-4 候选线：取值数 ≤ N = 条款区分不开",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "soft_dim_tolerance": {
        "value": 1,
        "status": "已定线",
        "basis": "软维度一致率的档差容忍（|t-p| ≤ N 算一致）",
        "where": (
            "evaluation/golden/builder.py",
            "evaluation/calibration/gate.py",
        ),
    },
}

#: 定线状态的两个取值。
STATUS_SET = ("已定线", "只出读数")


def value(name: str) -> Any:
    """取阈值——**唯一取法**。

    未登记的名字 → ``KeyError``（fail-closed）：不返回默认值，也不猜。
    """
    try:
        return THRESHOLDS[name]["value"]
    except KeyError:
        raise KeyError(
            f"阈值 {name!r} 没登记在 THRESHOLDS；先补表再改代码"
            f"（已登记：{sorted(THRESHOLDS)}）"
        ) from None


def status(name: str) -> str:
    """这条线的定线状态（``已定线`` / ``只出读数``）。"""
    try:
        return str(THRESHOLDS[name]["status"])
    except KeyError:
        raise KeyError(f"阈值 {name!r} 没登记在 THRESHOLDS") from None


def basis(name: str) -> str:
    """这条线的依据（哪个裁定 / 哪份文档）。"""
    try:
        return str(THRESHOLDS[name]["basis"])
    except KeyError:
        raise KeyError(f"阈值 {name!r} 没登记在 THRESHOLDS") from None


def where(name: str) -> tuple[str, ...]:
    """使用这条线的模块（相对仓库根）。

    「使用处」= **真的调了** :func:`value`，不是「提到过这个数」。契约测试
    按它核「表里这条确实被引用」——表不腐靠的就是这条。
    """
    try:
        raw = THRESHOLDS[name]["where"]
    except KeyError:
        raise KeyError(f"阈值 {name!r} 没登记在 THRESHOLDS") from None
    return (raw,) if isinstance(raw, str) else tuple(str(x) for x in raw)


__all__ = [
    "STATUS_SET",
    "THRESHOLDS",
    "basis",
    "status",
    "value",
    "where",
]
