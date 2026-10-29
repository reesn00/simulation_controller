"""golden_ls: 冻结黄金集 → Label Studio 的**上传与复核回路**（方案 22）。

为什么单独一个包（而不是塞进 ``ls_bridge``）
--------------------------------------------
``tests/contract/test_ls_bridge_contract.py`` 把 ``ls_bridge`` 的依赖锁死在
``{ls_bridge, scout.aggregate, scout.types, storage, storage.read_p3,
storage.task_views}``——**不含** ``executor`` / ``evaluation.golden.*`` /
``evaluation.calibration.*``。而黄金集这一路必须读这三样：分支表（22 类
封闭集）、冻结档、等价类与现场读数。

⇒ 另立一个包。依赖方向**单向**::

    evaluation.golden_ls  →  evaluation.ls_bridge   （用现成的 client / 推送 / 判重台账）
    evaluation.golden_ls  →  evaluation.golden       （冻结档 / 等价类 / 档名）
    evaluation.golden_ls  →  evaluation.calibration  （现场读数）
    evaluation.golden_ls  →  executor.branches       （分支表真源，只读常量）

``ls_bridge`` 那边一个字都不动——scout 轨迹那一路的依赖边界不因黄金集而松。

这个包做什么
------------
===========================  ==================================================
:mod:`~.dataset`             读冻结档（manifest / 三档行 / 版本列表）
:mod:`~.label_config`        **渲染**复核用 label_config（分支选项现读真源）
:mod:`~.exporter`            冻结行 → ``task.data``（九个区块，纯函数、不联网）
:mod:`~.diff`                相对上一版的变更标记
:mod:`~.report`              推送计划的读数（分档 / 一致率 / 可得率 / 变更分布）
:mod:`~.projects`            版本 → LS 项目的本地台账（append-only）
:mod:`~.verdicts`            LS 侧裁定的导出（**两套署名分列**，裁定④）
:mod:`~.cli`                 命令行入口
===========================  ==================================================

⚠️ **模块不叫 ``annotations``**：本文件第 43 行的
``from __future__ import annotations`` 会把 ``annotations`` 这个名字绑成
``__future__._Feature``（CPython 真的执行这条 import），**把同名子模块
顶掉**——于是 ``from ...golden_ls import annotations`` 拿到的是那个
``_Feature``，不是模块。这不是 pytest 的锅，是包命名与 ``__future__``
撞名。⇒ 模块叫 ``verdicts``。

三条贯穿全包的纪律
------------------
1. **读不到就说读不到。** 现场 / T 视图读不到时写「⚠ 没读到」+ 原因 +
   候选，**不给空串充数**——空读数会被读成「页面里没有播放控件」。
2. **缺字段与没测是两回事。** 维度分数三种状态分开写（M-29 的病）。
3. **不回流。** 本包只把数据推上去、把裁定导出来；**不改金标**——
   回流要走 ``golden.builder annotate`` 整行模式，且「金标变动 = 新基线」
   ⇒ 要重冻结出新版本。
"""

from __future__ import annotations

__all__ = [
    "cli",
    "dataset",
    "diff",
    "exporter",
    "label_config",
    "projects",
    "report",
    "verdicts",
]
