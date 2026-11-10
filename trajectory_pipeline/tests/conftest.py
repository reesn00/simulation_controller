"""测试全局夹具。

⚠️ **所有测试默认看不见 ``TRAJECTORY_LLM_*``**（含升配后端），autouse
逐条清空进程环境里的这一组键。理由不是洁癖：

- 评估链自 2026-11-10 起**按环境变量自动建裁判客户端**
  （:func:`trajectory_pipeline.evaluation.judge_backend.judge_client_from_env`，
  ``evaluation.cli`` / scout / overlay-scout 三处）。开发机用 ``setx``
  配过真实后端时，任何走 CLI 入口的测试都会**真发网络请求**——既烧钱，
  也把样本片段（URL / 标题 / 正文前 240 字）送出本机，而测试本应离线。
- 更糟的是它**不会红**：后端活着时测试照过，只是慢；后端挂了才出
  莫名其妙的 fail。这类随机器状态漂移的测试比没有测试更坏，
  因为它把「环境没配」伪装成「代码有 bug」。

测试里要验「配了会怎样」，用 ``monkeypatch.setenv`` 显式给值——
fixture 按函数顺序先清空，monkeypatch 之后写入，互不干扰。
"""

from __future__ import annotations

import os

import pytest

#: 评估链 / 执行层读的 LLM 环境变量前缀。按前缀清，新增键自动覆盖。
_LLM_ENV_PREFIX = "TRAJECTORY_LLM_"


@pytest.fixture(autouse=True)
def _no_ambient_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉进程环境里的 ``TRAJECTORY_LLM_*``——测试永不真连后端。"""
    for key in [k for k in os.environ if k.startswith(_LLM_ENV_PREFIX)]:
        monkeypatch.delenv(key, raising=False)