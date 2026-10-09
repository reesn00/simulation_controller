"""LLM 客户端——全树唯一的 LLM 出口。

存在理由：结构化输出解析必须集中处理。已实测本项目 LLM 后端**不强制
json_schema**，模型会漂字段名、套 think、首轮跑偏。``schema_parse.py`` 的
别名容错 + 重试 + fail-soft 是共享资产，不能每个模块各写一遍。

依赖约束：可 import ``common``；**不 import 任何功能模块**。

当前状态
--------
- :mod:`~trajectory_pipeline.llm.client` —— OpenAI 兼容 chat-completions
  客户端 + 配置。**凭据只从环境变量读**，不进代码/测试/文档/日志；
  ``LLMConfig.__repr__`` 刻意不吐 key。
- :mod:`~trajectory_pipeline.llm.schema_parse` —— 共享容错层：剥 think、
  从散文/fence 里抠 JSON、字段别名容错、宽松布尔与置信度解析。

已知的本机后端行为（写在这里以免各处重复踩）：
  - 不强制 json_schema，字段名会漂
  - 可能输出 ``<think>`` 包裹（送 LLM 前须剥离 raw CoT，见 CLAUDE.md 红线）
  - 自报 confidence 普遍虚高，须经验校准后才可用于阈值判断
  - 大输入会超时——实测正文从 1200 降到 600 字符后，原本直接 ``None``
    的请求恢复正常（见 ``llm_perceptor.LLM_BODY_CHARS``）

环境变量（**刻意带 ``TRAJECTORY_`` 前缀**，不与存量 v1 的 ``LLM_*`` 撞名）::

    TRAJECTORY_LLM_BASE_URL    必填，缺失即报错不猜
    TRAJECTORY_LLM_MODEL       必填，同上
    TRAJECTORY_LLM_API_KEY     可选，本机 vLLM 不校验时留空
    TRAJECTORY_LLM_TIMEOUT_S   可选，默认 30

端点与模型名可用 :func:`~trajectory_pipeline.llm.client.probe` 一次性探测——
**模型名写错时的症状是每道题都返回 None**，看起来像「LLM 判不了」，
实际是 404。
"""