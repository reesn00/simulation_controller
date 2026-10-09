"""感知层工厂——**注入点与灰度开关**。

executor 只 import :mod:`~trajectory_pipeline.perception.base` 的协议，
由本模块在 CLI 边界处挑实现。这条「控制流不知道有几种感知器」的分界
让 W1 → W3 的切换**不需要改 orchestrator 一行**。

灰度只发生在**构造期**，不做运行期切换
------------------------------------------
按比例灰度（每 N 个 task 用 LLM）在这里实现，**不在 ``decide`` 内部
按题或按次切换**。原因有两条，都不是风格问题：

1. **运行期切换会让来源不可辨。** ``Decision.source`` 只有 ``"rule"``
   与 ``"llm"`` 两个取值。一批里若按比例混用，存档上每条都带着
   ``source``，但**分不清某条结论是「LLM 判的」还是「LLM 挂了所以
   规则版顶上来的」**——而这两者的可信度完全不同。
2. **LLM 不可用时的规则版顶上，不是 fail-closed。** I4 要求「判不出来
   就说判不出来」。LLM 说 ``None``（真·不确定）若被规则版的确定结论
   覆盖，就变成了用不确定的输入驱动确定的输出。这正是「禁止投票/集成」
   要防的那类事——两路结论的分歧必须暴露给人看，不能自动择一。

所以：**一个 run 要么整条链走 rule，要么整条链走 LLM。** 想对比两条
链，跑两批，存档的 ``perceptor`` 字段（``RunRecord.perceptor``）已经把
两者分开了。

配置
----
``TRAJECTORY_PERCEPTOR=rule|llm|auto``（默认 ``auto``）
    ``auto`` = 配了 ``TRAJECTORY_LLM_BASE_URL`` 且 ``TRAJECTORY_LLM_MODEL``
    就用 LLM 版，否则退回规则版。**不报错**——W1 批次必须在没有后端的
    机器上能跑通，这是既有能力，不能因为新插件上线就报废。
    显式写 ``llm`` 而未配置后端则**报错**：那是配置错误，
    静默退回规则版会让人以为跑的是 LLM 版，而存档里 perceptor 字段
    会诚实地写 ``rule``——但没人会去读那个字段。
"""

from __future__ import annotations

import os
from typing import Any

from trajectory_pipeline.llm.client import LLMConfig, LLMUnavailable
from trajectory_pipeline.perception.base import Perceptor
from trajectory_pipeline.perception.llm_perceptor import LLMPerceptor
from trajectory_pipeline.perception.rule_perceptor import RulePerceptor

ENV_MODE = "TRAJECTORY_PERCEPTOR"

MODES = ("rule", "llm", "auto")


def configured_mode(env: dict[str, str] | None = None) -> str:
    """当前请求的感知模式。未知值抛错——不猜。"""
    e = os.environ if env is None else env
    mode = (e.get(ENV_MODE) or "auto").strip().lower()
    if mode not in MODES:
        raise ValueError(f"未知 {ENV_MODE}={mode!r}；可用：{MODES}")
    return mode


def build_perceptor(
    *,
    mode: str | None = None,
    target_title: str = "",
    env: dict[str, str] | None = None,
    client: Any = None,
) -> Perceptor:
    """构造感知器。

    ``client`` 供测试注入假后端。**生产路径不传**——传了就等于让
    上层决定用哪个后端，而那是 :mod:`~trajectory_pipeline.llm.client`
    的职责。
    """
    chosen = (mode or configured_mode(env)).strip().lower()
    if chosen not in MODES:
        raise ValueError(f"未知感知模式 {chosen!r}；可用：{MODES}")

    if chosen == "rule":
        return RulePerceptor()

    try:
        cfg = LLMConfig.from_env(env)
    except LLMUnavailable:
        if chosen == "llm":
            raise
        # auto 且未配置 → 退回规则版，理由由 health() 暴露给 CLI。
        return RulePerceptor()

    if client is None:
        from trajectory_pipeline.llm.client import LLMClient

        client = LLMClient(cfg)
    return LLMPerceptor(client, target_title=target_title)


def describe(perceptor: Perceptor) -> str:
    """一行说明，给 CLI 的能力检查用。"""
    name = getattr(perceptor, "name", "?")
    health = getattr(perceptor, "health", None)
    if not callable(health):
        return f"{name}（未实现 health）"
    try:
        ok, why = health()
    except Exception as exc:                       # 自述失败不该打断检查
        return f"{name}（health 抛异常：{type(exc).__name__}）"
    return f"{'OK ' if ok else 'FAIL'} {name}: {why}"