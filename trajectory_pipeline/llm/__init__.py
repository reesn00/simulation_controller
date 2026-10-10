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
  - **同产品族里两种行为都有**——实测（2026-10-10，同一端点、同一提示词、
    同一 ``max_tokens``）：``MiniMax-M3`` / ``MiniMax-M2.7`` **返**内联
    ``<think>``（正文 2286 / 2320 字符），而 ``MiniMax-M3.1-Flash-Preview`` /
    ``deepseek-v4.1-flash`` / ``qwen3.8-flash`` / ``qwen3.8-27b`` /
    ``GLM-5.3-Flash`` **不返**（209–490 字符）。
    **换一次配置就可能换到一个会返的**，所以 ``strip_think`` 是必要的
    而不是多余的，且**逐次都可能不同**。
  - **会写未转义的 ASCII 双引号**（第三种漂移，实测 ``deepseek-v4.1-flash``
    4 次里坏 2 次、``MiniMax-M3`` 4 次里坏 1 次）：形如
    ``{"thinking": "标题只是笼统的"腾讯视频"，正文…"}``。它的可怕之处是
    **大括号配平**——``_balanced`` 取出来那段看起来完全正常，只有
    ``json.loads`` 知道它坏了（报 ``Expecting ',' delimiter``）。共享解析层
    已做**报错位置驱动**的转义修复
    （:func:`~trajectory_pipeline.llm.schema_parse.extract_json_reporting`），
    修复记录随结果返回，由调用方写进 ``degraded_from``——**修可以，账要记**。
  - **模型名大小写敏感，且 403 与 503 是两种失败**：实测
    ``deepseek-V4.1-Flash`` → **403**（名字未注册）、
    ``DeepSeek-V4.1-Flash`` → **503**（已注册但服务过载）、
    ``deepseek-v4.1-flash`` → OK。
  - 自报 confidence 普遍虚高，须经验校准后才可用于阈值判断
  - 大输入会超时——实测正文从 1200 降到 600 字符后，原本直接 ``None``
    的请求恢复正常（见 ``llm_perceptor.LLM_BODY_CHARS``）
  - **是推理模型：思考与正文共用 ``max_tokens`` 一个预算。** 预算小了正文
    一个字都吐不出来（``content: null`` + ``finish_reason: length``），而
    客户端按红线不读 ``reasoning_content``，于是表面上只是「拿不到内容」。
    详见 ``client.DEFAULT_MAX_TOKENS`` 的实测表。

环境变量（**刻意带 ``TRAJECTORY_`` 前缀**，不与存量 v1 的 ``LLM_*`` 撞名）::

    TRAJECTORY_LLM_BASE_URL      必填，缺失即报错不猜
    TRAJECTORY_LLM_MODEL         必填，同上
    TRAJECTORY_LLM_API_KEY       可选，本机 vLLM 不校验时留空
    TRAJECTORY_LLM_TIMEOUT_S     可选，默认 30
    TRAJECTORY_LLM_MAX_TOKENS    可选，默认 8192（照推理模型定的，见上）

端点与模型名可用 :func:`~trajectory_pipeline.llm.client.probe` 一次性探测——
**模型名写错时的症状是每道题都返回 None**，看起来像「LLM 判不了」，
实际是 404。
"""