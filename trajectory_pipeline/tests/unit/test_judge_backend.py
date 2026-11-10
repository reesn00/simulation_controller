"""``evaluation.judge_backend`` 的三态语义。

三态不是风格问题，是**报表可读性**问题：都走一条「没配就返回 None」的
路径时，「一个键都没配」与「配了一半」在输出上同形——都是全维
undetermined，没人能从报表反推出是哪种，而两者要采取的动作完全不同
（前者去配环境，后者去补缺的键）。参见 ``llm.client.LLMConfig.from_env``
的「缺件即报错，不猜」纪律。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.evaluation.judge_backend import (
    LLM_ENV_KEYS,
    judge_client_from_env,
)
from trajectory_pipeline.llm.client import LLMClient, LLMUnavailable


def test_一个键都没配返回None():
    assert judge_client_from_env({}) is None


def test_空白值也算没配():
    """``set VAR=`` 在 Windows 上是「清空」的常见写法，不能当成配了。"""
    assert judge_client_from_env({k: "  " for k in LLM_ENV_KEYS}) is None


def test_配齐返回客户端():
    client = judge_client_from_env({
        "TRAJECTORY_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "TRAJECTORY_LLM_MODEL": "test-model",
    })
    assert isinstance(client, LLMClient)
    assert client.cfg.model == "test-model"


def test_只配一半是配置错误_不静默当没配():
    """配了 KEY 却缺 BASE_URL/MODEL：用户显然**想**配，静默当没配
    会让那一半配置成为没人读的尸体，且「LLM 为什么没生效」查不到它。"""
    with pytest.raises(LLMUnavailable, match="TRAJECTORY_LLM_BASE_URL"):
        judge_client_from_env({"TRAJECTORY_LLM_MODEL": "m"})
    with pytest.raises(LLMUnavailable, match="TRAJECTORY_LLM_MODEL"):
        judge_client_from_env({"TRAJECTORY_LLM_BASE_URL": "http://x"})


def test_只配api_key也算配了一半():
    with pytest.raises(LLMUnavailable):
        judge_client_from_env({"TRAJECTORY_LLM_API_KEY": "k"})


def test_非法timeout是配置错误():
    with pytest.raises(LLMUnavailable, match="TRAJECTORY_LLM_TIMEOUT_S"):
        judge_client_from_env({
            "TRAJECTORY_LLM_BASE_URL": "http://x",
            "TRAJECTORY_LLM_MODEL": "m",
            "TRAJECTORY_LLM_TIMEOUT_S": "很快",
        })


def test_客户端repr与调试摘要都不含key():
    """凭据红线：key 只从环境变量读，绝不进 repr / 日志 / 报错。"""
    secret = "test-secret-should-never-appear"
    client = judge_client_from_env({
        "TRAJECTORY_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "TRAJECTORY_LLM_MODEL": "test-model",
        "TRAJECTORY_LLM_API_KEY": secret,
    })
    assert client is not None
    assert secret not in repr(client)
    assert secret not in repr(client.cfg)
    assert client.cfg.api_key == secret  # 真的读到了，只是不打印