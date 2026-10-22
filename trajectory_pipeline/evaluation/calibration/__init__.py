"""校准回路（方案 16）：把「不一致」从报表数字变成可指认的对象。

本包只做**只读派生**——读冻结档（``evaluation/golden/data/v{N}/*.jsonl``）
与 candidates，派生出：

- :mod:`~trajectory_pipeline.evaluation.calibration.snapshot` —— 全量逐行
  预测快照（方案 16 §9.2 C-15）。**不回写冻结档**；
- :mod:`~trajectory_pipeline.evaluation.calibration.disagreement` —— 分歧卡
  （§3.1 契约 / C-02），从快照里筛出「有差异」的行。

两件事为什么分成两个模块
------------------------
回归判定问的是「**上一轮对、这一轮错**」，而上一轮的那一行**根本不在这一轮
的分歧卡里**——只存分歧卡，回归就判不出来。所以快照必须是全量，卡是快照的
视图；同一次 agreement 跑出两份，口径不会分叉。

落点与归属（D-C1）
------------------
派生数据落 ``trajectory_pipeline/output/pipeline/calibration/v{N}/``
（gitignore 内，可再生）；**人填的分诊档**另存
``evaluation/golden/data/triage/``（入版本控制，丢了不可重建）。本包只写前者。
"""