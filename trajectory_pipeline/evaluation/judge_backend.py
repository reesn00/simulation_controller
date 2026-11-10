"""评估链的 LLM 裁判后端装配——**唯一**从环境变量建客户端的地方。

为什么单开一层
--------------
``evaluation.cli`` / ``scout`` / ``overlay-scout`` 三条批处理都会用到
D-2（rubric v1.4 起「代码筛 + LLM 判语义」），CLI 还可能在
``--include-llm-judge`` 下跑 D-3 / D-9。三处各写一遍
``LLMConfig.from_env()`` 就会漂：一处把「没配」当错误、另一处把
「配了一半」当没配——两种都表现为「报表上全是 undetermined」，
从输出上分不出来。

三态语义（与 ``OBSCURA_EXE`` / agent 模式的 ``TRAJECTORY_LLM_*`` 同纪律）
------------------------------------------------------------------------
- **一个键都没配** → 返回 ``None``：调用方按 fail-closed 继续跑，相关
  维度落 ``undetermined``，并在 stderr 说清楚是「没配」而不是「判不了」；
- **配了任一键但缺 BASE_URL / MODEL，或值非法** → 抛
  :class:`~trajectory_pipeline.llm.client.LLMUnavailable`。想配却没配齐
  是配置错误，**不静默当没配**——那会让半份配置变成没人读的尸体，
  而「为什么 LLM 没生效」永远查不到它头上；
- **配齐** → 返回 :class:`~trajectory_pipeline.llm.client.LLMClient`。

红线：凭据只从环境变量读，本模块不记录、不打印 key。要往 stderr 报
后端身份，用 :func:`~trajectory_pipeline.llm.client.dump_for_debug`
（只含 model / base_url / timeout）。
"""

from __future__ import annotations

import os
from typing import Mapping

from trajectory_pipeline.llm.client import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_MAX_TOKENS,
    ENV_MODEL,
    ENV_TIMEOUT_S,
    LLMClient,
    LLMConfig,
)

#: 参与「有没有配过」判断的键。任一非空即视为「用户想配 LLM」——
#: 此时缺 BASE_URL / MODEL 是配置错误，不是「没配」。
LLM_ENV_KEYS: tuple[str, ...] = (
    ENV_BASE_URL,
    ENV_MODEL,
    ENV_API_KEY,
    ENV_TIMEOUT_S,
    ENV_MAX_TOKENS,
)


def judge_client_from_env(
    env: Mapping[str, str] | None = None,
) -> LLMClient | None:
    """按 ``TRAJECTORY_LLM_*`` 建裁判客户端；**一个键都没配** → ``None``。

    Args:
        env: 环境变量映射；``None`` = 读进程环境（测试可注入字典）。

    Raises:
        LLMUnavailable: 配了任一键但 BASE_URL / MODEL 缺失，或
            ``TIMEOUT_S`` / ``MAX_TOKENS`` 非法。
    """
    e = os.environ if env is None else env
    if not any((e.get(key) or "").strip() for key in LLM_ENV_KEYS):
        return None
    return LLMClient(LLMConfig.from_env(e))


__all__ = ["LLM_ENV_KEYS", "judge_client_from_env"]