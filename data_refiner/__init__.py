"""data_refiner: agent 合成数据剪裁工具。

把 input_data/ 下的 agent 合成数据剪裁为清洁训练数据：
- 连续工具调用失败段只保留最后一次失败，删除前几次失败尝试；
- thinking 过长/过短只标注不删除；
- 无效文件（仅 user 发言，或仅一次 assistant 回复且只有 thinking + 全失败调用）跳过并说明原因；
- 全过程写入本地日志与轨迹块状态记录。
"""

