"""执行器模式（``scripted`` / ``agent``）——A2a 的灰度开关。

与 :mod:`trajectory_pipeline.perception.factory` 的 ``TRAJECTORY_PERCEPTOR``
同一条纪律：**灰度只在构造期**。一批存档要么整批 scripted、要么整批
agent——运行期按候选切换会让「这条轨迹是哪种执行器产的」变成逐条才能
回答的问题，而 :class:`~.orchestrator.RunRecord` 的 ``executor_mode``
是**批次级**字段，它回答不了逐条的问题（方案 13 §6.1）。

A3-1（2026-10-17）已转正：模式是
:class:`~trajectory_pipeline.executor.orchestrator.RunConfig` 的
``executor_mode`` 字段，run 子命令开了参数门（``--executor-mode``，
proberun 不加，沿用 ``discovery_path`` 先例）。优先级：
``--executor-mode`` > 本环境变量 > 默认 ``scripted``——本键降级为
「参数缺省时的 fallback」，保留到 A3 结束后再撤（老机器上的脚本不会
一夜之间改完）。封闭集 :data:`EXECUTOR_MODES` 留在本模块：
``RunConfig`` 只存值，取值集只有一个产地。

环境变量
--------
``TRAJECTORY_EXECUTOR_MODE=scripted|agent``（默认 ``scripted``）
    未知值**报错**不猜。拼错成 ``agentt`` 会静默跑成 scripted 批，
    而那一批的存档会**诚实地**写着 ``executor_mode="scripted"``——
    读档的人只会以为「这批没灰度」，看不出那是打错了字，
    也看不出有人以为自己跑的是 agent 批。

    默认值取 ``scripted`` 而不是 ``auto``：这里没有
    ``TRAJECTORY_PERCEPTOR`` 那种「有后端就用」的语义——后端存在
    **不等于**该让它开方向盘。LLM 后端是为了判断题配的，A1 批次
    全靠它，把它的存在当成灰度信号会让所有 A1 批次一夜之间变成
    agent 批。
"""

from __future__ import annotations

import os
from typing import Final, Mapping

#: 封闭取值集。**两个值各代表一整条执行链**，没有中间态——
#: §6.1 定死：``agent_visit`` 内部不 fallback，一个候选一种模式，
#: 混合轨迹不可归因。
EXECUTOR_MODES: Final = ("scripted", "agent")

#: A2a 的私有关口名。A3-1 转正后由 ``--executor-mode`` /
#: ``RunConfig.executor_mode`` 承担，本键降级为参数缺省时的 fallback，
#: 保留到 A3 结束后再撤（老机器上的脚本不会一夜之间改完）。
ENV_EXECUTOR_MODE: Final = "TRAJECTORY_EXECUTOR_MODE"

#: 默认档。**刻意叫它「默认」而不是「兜底」**：没设这个变量时跑
#: scripted 是主动选择，不是「agent 没能开起来」——后者必须报错。
DEFAULT_EXECUTOR_MODE: Final = "scripted"

AGENT: Final = "agent"
SCRIPTED: Final = "scripted"


def configured_mode(env: Mapping[str, str] | None = None) -> str:
    """当前请求的执行器模式。**未知值抛错，不猜。**

    Args:
        env: 注入的环境变量表（测试用）。``None`` = 读 :data:`os.environ`。

    Raises:
        ValueError: 值不在 :data:`EXECUTOR_MODES` 里。空串 = 未设，
            按默认处理——与「设了但打错」是两回事，前者是常态。
    """
    e = os.environ if env is None else env
    raw = (e.get(ENV_EXECUTOR_MODE) or "").strip().lower()
    if not raw:
        return DEFAULT_EXECUTOR_MODE
    if raw not in EXECUTOR_MODES:
        raise ValueError(
            f"未知 {ENV_EXECUTOR_MODE}={raw!r}；可用：{EXECUTOR_MODES}。"
            f"拼错会静默跑成 {DEFAULT_EXECUTOR_MODE} 批，而存档里 "
            f"executor_mode 会诚实写着 {DEFAULT_EXECUTOR_MODE!r}——"
            f"到了 A/B 对比那天，没人分得清那是没灰度还是打错了字。"
        )
    return raw


__all__ = [
    "AGENT",
    "DEFAULT_EXECUTOR_MODE",
    "ENV_EXECUTOR_MODE",
    "EXECUTOR_MODES",
    "SCRIPTED",
    "configured_mode",
]
