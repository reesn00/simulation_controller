"""任务输出契约的**只读**查询（方案 23 R2-3）。

`output_contract` 只存在于任务库 ``taskgen/data/tasks.yaml``，P1 存档里
没有；P2 要判「终局答复有没有兑现它」，只能按 ``task_id`` 回任务库查。

为什么自己读 YAML 而不是 import ``taskgen``：``assembler`` 的模块图纪律是
**不向执行端/生成端要对象**（交接面是格式）。任务库是一份静态数据文件，
按数据读它，重建一批 P2 时不需要 taskgen 的当前版本。

**读不到就是读不到**（fail-closed）：文件缺失、YAML 坏了、``task_id``
不在库里、合同里没有 ``min_urls``——一律返回 ``None``，表示**这条判据
不适用**，调用方不得据此改动任何结论。反过来（读不到就当 0 URLs 要求）
会让一批本来合法的样本被判失败，那是「猜」。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

#: 任务库路径。模块内唯一字面量——改布局只动这一处。
TASKS_PATH = (
    Path(__file__).resolve().parents[1] / "taskgen" / "data" / "tasks.yaml"
)


@lru_cache(maxsize=1)
def _tasks_index(path: str) -> dict[str, Mapping[str, Any]] | None:
    """``task_id -> task dict``。读不到返回 ``None``（不抛、不猜）。"""
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, Mapping):
        return None
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return None
    index: dict[str, Mapping[str, Any]] = {}
    for task in tasks:
        if isinstance(task, Mapping) and task.get("task_id"):
            index[str(task["task_id"])] = task
    return index


def task_entry(task_id: str) -> Mapping[str, Any] | None:
    """``task_id`` 的整条任务记录；未知/缺失 → ``None``（不猜）。

    与 :func:`output_contract` 同一条纪律：任务库是一份静态数据文件，
    按数据读它；读不到就是「这条判据不适用」。
    """
    index = _tasks_index(str(TASKS_PATH))
    if index is None:
        return None
    return index.get(str(task_id or ""))


def output_contract(task_id: str) -> Mapping[str, Any] | None:
    """``task_id`` 的 ``output_contract``；未知/缺失 → ``None``。"""
    task = task_entry(task_id)
    if task is None:
        return None
    contract = task.get("output_contract")
    return contract if isinstance(contract, Mapping) else None


def min_urls_of(task_id: str) -> int | None:
    """合同要求答复里至少出现几个网址；没有这条要求 → ``None``。

    - 合同缺 ``min_urls`` / 值不是整数 → ``None``（判据不适用）；
    - 值为 0 或负数 → 同样视为不适用（没有要求就没什么可判）。
    """
    contract = output_contract(task_id)
    if contract is None:
        return None
    value = contract.get("min_urls")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


__all__ = ["TASKS_PATH", "task_entry", "output_contract", "min_urls_of"]