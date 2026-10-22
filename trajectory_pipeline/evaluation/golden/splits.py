"""黄金集三档切分的**唯一真源**（方案 16 C-04 / C-17）。

为什么要单独一个模块
--------------------
切分档名会同时出现在四个地方：``freeze`` 写文件、``agreement`` 算头条、
``calibration.snapshot`` / ``disagreement`` 打标、报表与文档。手抄四个字符串
字面量，迟早有一处写成 ``calib`` 而另一处写 ``calibration``——那时头条分母
会悄悄变成 0，而报表不会报错（分母为 0 的比率要么除零、要么打印 "0/0"）。
所以档名、文件名、头条档位只在这里写一次。

三档各是什么
------------
======================  ==========================================
``calibration``          **调参池**。头条 D-10 分母、软维度分母、
                         分歧卡、人工分诊，全部只在这一档上做。
``regression``           **回归集**（黄金校准集，D-C8 定 60 条）。
                         改 rubric / 改评估器后先跑它：原本判对的现在判错
                         = 回归 = 回滚。**永不作调参依据**，因此它的
                         分歧卡不进人工分诊池（`disagreement.cards_from_snapshot`
                         不吃这一档）。
``holdout``              **持有集**（封存，D-C5）。只在最终验收跑一次；
                         平时只报条数，不报一致率。
======================  ==========================================

``calibration`` 与 ``regression`` 都在 v6 及更早的冻结档里叫 ``calibration``
（那时还没有回归集）。**不回填历史存档**：老版本的 ``calibration.jsonl``
一直是调参池，v7 起才从中切出回归集。
"""

from __future__ import annotations

SPLIT_CALIBRATION = "calibration"
SPLIT_REGRESSION = "regression"
SPLIT_HOLDOUT = "holdout"

#: 三档全集（顺序即文件/报表顺序）。新增档位先问：它进不进头条分母？
SPLITS: tuple[str, ...] = (SPLIT_CALIBRATION, SPLIT_REGRESSION, SPLIT_HOLDOUT)

#: 档位 → 冻结版目录下的文件名（stem 同时是 ``__split__`` 标注值）。
SPLIT_FILES: dict[str, str] = {
    SPLIT_CALIBRATION: f"{SPLIT_CALIBRATION}.jsonl",
    SPLIT_REGRESSION: f"{SPLIT_REGRESSION}.jsonl",
    SPLIT_HOLDOUT: f"{SPLIT_HOLDOUT}.jsonl",
}

#: 头条 D-10 的分母档：**只有调参池**（C-04）。旁置档单独报条数，
#: 回归集的判读在 ``verify``（C-08），持有集留给最终验收。
HEADLINE_SPLIT = SPLIT_CALIBRATION

#: 旁置档（报条数与分布，不进头条分母）。
SIDE_SPLITS: frozenset[str] = frozenset({SPLIT_REGRESSION, SPLIT_HOLDOUT})

#: 回归集条数（D-C8 已裁定：从 calibration 切固定 60 条）。
REGRESSION_SIZE = 60

#: 回归集抽样种子。**与 ``freeze --seed``（切校准/持有）分两个种子**：
#: 改一个不该动到另一个；两者混用会让"只是重切了持有集"看起来像"回归集变了"。
REGRESSION_SEED = 20261022


def split_of_file(path_or_stem: str) -> str:
    """``"calibration.jsonl"`` / ``"calibration"`` → 档名；未登记返回空串。

    返回空串而不是抛：读冻结档时要容忍"这个版本没有 regression.jsonl"，
    但**不允许猜**（例如把 ``regression.jsonl`` 当成 ``calibration``）。
    """
    stem = str(path_or_stem).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if stem.endswith(".jsonl"):
        stem = stem[: -len(".jsonl")]
    return stem if stem in SPLITS else ""