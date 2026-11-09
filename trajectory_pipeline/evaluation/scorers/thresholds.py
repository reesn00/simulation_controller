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

#: 阈值表——**唯一真源**。每条五栏：
#:
#: - ``value``：阈值本体；
#: - ``status``：``已定线`` / ``只出读数``（D-C9：新线第一批只出读数，
#:   线还没定 ⇒ 只报读数，不报合格 / 不合格）；
#: - ``scope``：**这条线绑在什么对象上**（哪一档 × 哪个口径 × 哪一侧）。
#:   **不是装饰**：「82.8% 过线」这句话若不说清对象，就是**换了分母还
#:   读成同一个数**——2026-10-30 的教训：上岗线从「口径 C × 头条档」改绑
#:   「调参池 × 人工署名子集 × 三态口径」，**数值没动**，读数从 82.8%
#:   变成 88.9%（换的是**绑定对象 + 记账口径**两件事）。
#:   形态：可以是**散文**，也可以是**能直接判的记号组合**
#:   （``headline_split+human_signed+three_state``）——组合形态的好处是
#:   **能被契约测试机械判定**（散文改一个字没人发现，组合改一位测试就红）。
#:   记号的读法见 :data:`SCOPE_LEGEND`。
#: - ``basis``：这条线哪来的（哪个裁定 / 哪份文档）；
#: - ``where``：使用它的模块（相对仓库根，**可多个**）——契约测试按它核
#:   「表里这条确实被用到」。
#:
#: ⚠️ 改这个表**不改** rubric 版本，也**不进** rubric 变更记录（D-C6 裁定）。
THRESHOLDS: dict[str, dict[str, Any]] = {
    "d10_min_agreement": {
        "value": 0.80,
        "status": "已定线",
        # ⚠️ **value 写成能直接判的组合**（下划线记号，`+` 连接）——
        # 「这条线读的是哪一档 × 哪个口径 × 哪一侧」要能被**机械判定**，
        # 不能只是一句散文（散文改一个字没人发现，组合改一位测试就红）。
        # 记号读法见 :data:`SCOPE_LEGEND`，展开用 :func:`scope_text`。
        #
        # ⚠️ **只绑调参池**：上岗线是**调参依据**（它决定这条管线能不能
        # 上岗）⇒ 只能读调参池。持有集（D-C5「每个 rubric 版本只跑一次，
        # 平时只报条数」）与回归集（D-C8「冻结、永不作调参依据」）是
        # **封存面**——读它们 = 拿封存面调参 = 看一眼就回不去。
        "scope": "headline_split+human_signed+three_state",
        "basis": (
            "D-C9 口径 C（语义等价类一致）；数值由 0.90 下调至 0.80"
            "（owner 2026-10-24 裁定）。**判定对象**于 owner 2026-10-30 裁定改为"
            "「调参池 × 人工署名子集 × 三态口径」，**数值仍是 0.80 未动**——"
            "理由一（为什么绑人工子集）：自动署名行（auto-validator-*）的标签由"
            "验证器从同一条 code.branch 派生，其一致率接近自证、不是独立裁定；"
            "人署名的行才是独立裁定。"
            "理由二（为什么只读调参池）：上岗线是**调参依据**（它决定这条管线"
            "能不能上岗），而 C-04 已定「头条 = 调参池」；持有集（D-C5「每个 "
            "rubric 版本只跑一次，平时只报条数」）与回归集（D-C8「冻结、永不作"
            "调参依据」）是封存面，读它们 = 拿封存面调参 ⇒ 这条线只读调参池。"
        ),
        "where": ("evaluation/golden/builder.py",),
    },
    "class_regression_tolerance": {
        "value": 0.02,
        "status": "已定线",
        "scope": "相邻两冻结档之间的**逐等价类 per-class F1** 下降量",
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
        "scope": "judge 侧**单条款**的等价类分歧率（全量比对集）",
        "basis": "T-1 候选线（单条款等价类分歧率），暂定未正式定线",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "t1_min_n": {
        "value": 20,
        "status": "只出读数",
        "scope": "上一条的**样本量门槛**（n < 门槛不判越线）",
        "basis": "T-1 候选线的样本量门槛（n < 门槛不判越线）",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "t2_hard_kappa": {
        "value": 0.70,
        "status": "已定线",
        "scope": "judge ↔ 人工标注在**硬维度 D-10** 上的 Cohen's κ",
        "basis": "D-C9 沿用上岗线（judge ↔ 人工一致率，硬维度 Cohen's κ）",
        "where": (
            "evaluation/calibration/triggers.py",
            "evaluation/golden/builder.py",
        ),
    },
    "t2_soft_kappa": {
        "value": 0.50,
        "status": "已定线",
        "scope": "judge ↔ 人工标注在**软维度**上的线性加权 κ",
        "basis": "D-C9 沿用上岗线（软维度线性加权 κ）",
        "where": (
            "evaluation/calibration/triggers.py",
            "evaluation/golden/builder.py",
        ),
    },
    "t2_drop_delta": {
        "value": -0.05,
        "status": "已定线",
        "scope": "**相邻两版之间**的 D-10 一致率降幅",
        "basis": "D-C9：相邻两版一致率下降超过这个幅度 = 触发回归检查",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "t3_vague_rate": {
        "value": 0.30,
        "status": "只出读数",
        "scope": "判读理由文本的**含糊率**",
        "basis": "T-3 候选线的理由含糊率，暂定未正式定线",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "t4_mode_share": {
        "value": 0.80,
        "status": "只出读数",
        "scope": "软维度取值的**众数占比**",
        "basis": "T-4 候选线：众数占比过高 = 取值分布退化",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "t4_distinct_values": {
        "value": 2,
        "status": "只出读数",
        "scope": "软维度取值的**基数**（取值数 ≤ N = 条款区分不开）",
        "basis": "T-4 候选线：取值数 ≤ N = 条款区分不开",
        "where": ("evaluation/calibration/triggers.py",),
    },
    "soft_dim_tolerance": {
        "value": 1,
        "status": "已定线",
        "scope": "软维度一致率的**档差容忍**（|t-p| ≤ N 算一致）",
        "basis": "软维度一致率的档差容忍（|t-p| ≤ N 算一致）",
        "where": (
            "evaluation/golden/builder.py",
            "evaluation/calibration/gate.py",
        ),
    },
    "d3_batch_agreement_min": {
        "value": 0.85,
        "status": "已定线",
        "scope": "批次级 B-3 的**站点类型判定一致率**（每 run 一份，分母 = "
                 "本批进入站点类型判定的样本数）",
        "basis": (
            "rubric v1.3 B-3 卡：「与黄金集人工类别标注的一致率，阈值 ≥85%"
            "（LLM 裁判资质领证线）」——线是 rubric 写定的，本表只是它的"
            "机器可读副本（D-C6：改线只改表，不升 rubric 版本、不进变更记录）"
        ),
        "where": ("evaluation/batch/asserts.py",),
    },
    "batch_asserts_max_failed_runs": {
        "value": 0,
        "status": "只出读数",
        "scope": "批次级断言（rubric v1.3 B-1..B-5，一批 = 一个 run）判"
                 "「不通过」的 run 数（每版一份 batch_asserts.json）",
        "basis": (
            "rubric G-7「批次级断言不过 → 暂停该批发放」的机器线。"
            "方案 24 §2.1：第五条闸门判据 batch_asserts_clear **第一批只报"
            "不判**，等 owner 看一批读数再定线（D-C9）；定线只改本表，"
            "不算 rubric 级变更（D-C6）"
        ),
        "where": ("evaluation/calibration/verify.py",),
    },
}


#: 定线状态的两个取值。
STATUS_SET = ("已定线", "只出读数")


#: ``scope`` 里**记号形态**的中文读法。
#:
#: ``scope`` 的 value 允许写成散文（读起来顺），也允许写成**能直接判的
#: 记号组合**（判得准）。记号形态的那几条，它的每个记号都得在这里有
#: 读法——**没登记就读不出来**（fail-closed）：组合改了而读法没跟上，
#: 报出来的话就是错的，而错的读法比没有读法更坏。
SCOPE_LEGEND: dict[str, str] = {
    "site_rows": "站点级已标注行（source_kind != p2_sample 且有 golden）",
    "human_signed": "人工署名子集（classify_signature = human）",
    "automated_signed": "自动署名子集（classify_signature = automated）",
    "three_state": "三态口径（agree + disagree 进分母；未判定行进弃权率）",
    "all_splits": "跨三档（调参池 + 持有集 + 回归集）",
    "headline_split": "头条档（调参池）",
}


def _is_token_scope(text: str) -> bool:
    """``scope`` 是不是**记号形态**（纯小写记号，`+` 连接）。"""
    if not text or "+" not in text:
        return False
    return all(
        part and part.replace("_", "").isalnum() and part.islower()
        for part in text.split("+")
    )


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


def scope(name: str) -> str:
    """这条线**绑在什么对象上**（哪一档 × 哪个口径）。

    「线读的是哪一档、哪个口径」必须有答案——没答案时，「82.8% 过线」
    这句话就**没有对象**：换个分母还会读成同一个数（2026-10-30 的教训）。

    未登记 scope 的线 → ``KeyError``（fail-closed，与 :func:`value` 同向）。

    ⚠️ **两件不同的事给两句不同的话**：① 阈值名压根没登记；② 阈值登记了
    但**没写绑定对象**（线必须有绑定对象）。报障时一眼分清是查错了名字，
    还是表里漏填了一栏。
    """
    if name not in THRESHOLDS:
        raise KeyError(
            f"阈值 {name!r} 没登记在 THRESHOLDS（已登记：{sorted(THRESHOLDS)}）"
        )
    text = str(THRESHOLDS[name].get("scope") or "").strip()
    if not text:
        raise KeyError(f"阈值 {name!r} 没登记 scope（线必须有绑定对象）")
    return text


def scope_text(name: str) -> str:
    """``scope`` 的**人类可读读法**。

    散文形态 → 原样返回；记号形态 → 按 :data:`SCOPE_LEGEND` 展开成一句
    能读的话。记号没登记 → ``KeyError``（fail-closed，与 :func:`scope`
    同向）：组合改了而读法没跟上，报出来的话就是错的。
    """
    raw = scope(name)
    if not _is_token_scope(raw):
        return raw
    parts = [p for p in raw.split("+") if p]
    unknown = [p for p in parts if p not in SCOPE_LEGEND]
    if unknown:
        raise KeyError(
            f"阈值 {name!r} 的 scope 记号不认识：{unknown}——"
            "先在 SCOPE_LEGEND 里补读法"
        )
    return " × ".join(SCOPE_LEGEND[p] for p in parts)


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
    "SCOPE_LEGEND",
    "STATUS_SET",
    "THRESHOLDS",
    "basis",
    "scope",
    "scope_text",
    "status",
    "value",
    "where",
]
