"""用户画像库（决策 D1：重做）。

PersonaProfile 六维度 + 2 标记：genre / popularity / urgency / verbal_style /
persona_presence / task_specificity + has_standard(70-30 配比) / content_tier。

每个维度都对应**可观察的语言特征**（见 schema.py），不是凭空设的标签；
``content_tier`` 须由任务骨架的客观属性推导，**不得由 LLM 自评**，
否则评估的切片轴不可信。
"""
