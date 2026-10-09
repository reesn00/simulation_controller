"""LLM 客户端——全树唯一的 LLM 出口。

存在理由：结构化输出解析必须集中处理。已实测本项目 LLM 后端**不强制
json_schema**，模型会漂字段名、套 think、首轮跑偏。``schema_parse.py`` 的
别名容错 + 重试 + fail-soft 是共享资产，不能每个模块各写一遍。

依赖约束：可 import ``common``；**不 import 任何功能模块**。
"""

# 已知的本机后端行为（写在这里以免各处重复踩）：
#   - 不强制 json_schema，字段名会漂
#   - 可能输出 <think> 包裹（送 LLM 前须剥离 raw CoT，见 CLAUDE.md 红线）
#   - 自报 confidence 普遍虚高，须经验校准后才可用于阈值判断
