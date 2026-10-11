"""新契约读写的**收拢点**（P1/P2/P3），与存量 C1/C2/C3 完全隔离。

当前分工（2026-10-11）
---------------------

三层契约**各有归处，只有 P3 收拢在本模块**——这不是偷懒，是三条纪律卡出来的：

- **P1 的路径权威在** :mod:`trajectory_pipeline.executor.archive`
  （:data:`~trajectory_pipeline.executor.archive.DEFAULT_ROOT` 的长注释明令
  不在别处另写路径字面量，它列了 4 处会被牵动的消费方）。executor 是执行
  端点，P1 归它。
- **P2 的落盘在** :mod:`trajectory_pipeline.assembler.builder`
  （``samples_dir`` 自定字面量而不 import executor——assembler 不 import
  executor 是契约，``tests/contract/test_p2_contract.py::TestNoExecutorImport``
  用 AST 强制）。P2 便宜、P1 贵，重建必须独立于执行端当前版本。
- **P3 的读写收拢在这里**：P3 的输入是 **P2 的 JSON dict**，交接面是格式
  （与 schema 读 P1、views 读 P2 同一条纪律），views.py 本身零内部 import，
  所以**只有 P3 放进来不破坏任何方向**。落盘逻辑的唯一实现
  :func:`trajectory_pipeline.storage.write_p3.write_views` 在这里，
  assembler 侧的渲染只调它、不自己 ``write_text``；读取侧
  :func:`trajectory_pipeline.storage.read_p3.read_views` 是训练侧入口。

落盘形状
--------

::

    output/pipeline/views/<sample_id>_{messages,openai,meta}.json

与 P1（``output/pipeline/<task_id>__<run_id>.json``）、P2
（``output/pipeline/samples/<sample_id>.json``）**同根**
（``trajectory_pipeline/output/pipeline/``，D8 产物隔离），路径权威在
:func:`views_dir`——改存储布局只动这里，消费方不许另写字面量。

P1 里最容易踩的几个点（写在对的地方，这里只提醒）：

- **动作里没有 ``ref``，因为 ``ref`` 是会话内句柄。** 点击目标一律是
  语义化的 ``{tag, label}``。它是结构保证，不是序列化时滤掉的。
- **``user_prompt`` 是用户开口说的那句**（persona 渲染后的原文），
  与 ``query``（拿去搜的检索串）是两件事。六件套第 ③ 件要的是前者。
- P3 视图渲染的**六条判断属于 assembler.views 的渲染语义**，本模块
  只负责「把渲染好的 dict 原子写进磁盘、按 id 读回来」——怎么写是
  assembler 的事，怎么存是本模块的事。

原子写
------

先写 ``.json.tmp``、``os.replace`` 覆盖目标（与 ``assembler/builder.py``
同款）。批次中断不会留下半截视图文件——半截视图比缺视图更难查。

隔离约束：**禁止读取仓库根 output/**。一读就继承存量那套隐性耦合。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: 三份视图的后缀名。与 :data:`trajectory_pipeline.assembler.views.VIEW_NAMES`
#: **必须一致**——views 是渲染语义的所有者，这里只照抄用于拼文件名。
#: 由 ``tests/contract/test_p3_contract.py::Test视图名与渲染侧一致`` 盯住。
VIEW_NAMES: tuple[str, ...] = ("messages", "openai", "meta")

#: 视图目录名。落在 ``output/pipeline/`` 下。
DEFAULT_VIEWS_DIRNAME = "views"

#: P1/P2/P3 的公共产物根（相对包根）。与
#: :data:`trajectory_pipeline.executor.archive.DEFAULT_ROOT`、
#: :data:`trajectory_pipeline.assembler.builder.DEFAULT_SAMPLES_PARENT`
#: **同根**——改了这处记得同步那两处（靠人盯，见各处的注释）。
DEFAULT_SAMPLES_PARENT = "pipeline"


def views_dir(root: str | Path | None = None) -> Path:
    """``views/`` 目录的绝对路径。

    ``root=None`` 时取 ``trajectory_pipeline/output/pipeline/views/``——
    与 P1/P2 同根，但**不走 executor 的 import 边界**（本模块不 import
    executor 是契约，见下）。

    ⚠️ **``parents[1]`` 不是 ``parents[2]``**：``__file__`` 在
    ``trajectory_pipeline/storage/__init__.py``，``parents[1]`` 才是
    ``trajectory_pipeline/`` 包根。写成 ``parents[2]`` 会落到**仓库根**的
    ``output/pipeline/``——CLAUDE.md 明令禁止碰的存量产物目录，
    而且**不报错**：只是读不到任何视图，看起来像「这批没样本」。
    由 ``tests/contract/test_p3_contract.py::Test产物根`` 盯住。
    """
    if root is None:
        root = Path(__file__).resolve().parents[1] / "output" / DEFAULT_SAMPLES_PARENT
    return Path(root) / DEFAULT_VIEWS_DIRNAME


__all__ = [
    "VIEW_NAMES",
    "DEFAULT_VIEWS_DIRNAME",
    "DEFAULT_SAMPLES_PARENT",
    "views_dir",
]
