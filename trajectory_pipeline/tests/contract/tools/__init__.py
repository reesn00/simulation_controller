# reviewer-only tools 包 —— 非 pytest 收集。
#
# 这里的脚本**不是**测试。它们是供 reviewer 在审稿时跑的工具，例如：
#  - 措辞漂移报告
#  - 一致性矩阵
#
# 由于文件名不含 ``test_*.py``，pytest 不会自动收集；调用方式见各文件
# docstring。
