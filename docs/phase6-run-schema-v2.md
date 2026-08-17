# Phase 6 Run Schema v2

每个 Run 使用独立 `run_id`，与远端 session/task/agent ID 分离。目录：

```text
output/
  runs/<run_id>/run.json
  runs/<run_id>/events.jsonl
  runs/<run_id>/validations.jsonl
  runs/<run_id>/evidence.jsonl
  artifacts/<sha256>.<ext>
  datasets/all_runs.v2.jsonl
  datasets/distill_dataset.v2.jsonl
  reports/stats.v2.json
  legacy/
```

Checkpoint 使用临时文件 + fsync + `os.replace` 原子替换；Event/Validation/Evidence 使用 append-only JSONL。Artifact 按 SHA-256 去重并有大小上限。

启动发现非终态 Run 时只标记 `INTERRUPTED`，不自动恢复外部执行。显式重跑创建新 run_id 并记录 rerun_of。
