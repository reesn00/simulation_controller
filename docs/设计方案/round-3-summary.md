# Pipeline 重构第 3 轮 — P1 `replay` 自动归档(2026-09-22)

> 本轮交付 `replay` 子命令自动归档 dead 产物到 `output/orchestration/dead/`。

## 一、目标达成

| 目标 | 状态 |
|---|---|
| replay 默认先归档 dead 产物,再 requeue_dead | ✅ |
| `--no-archive` 选项保留灵活(只 requeue) | ✅ |
| dead.log / dead_index.jsonl 落盘 | ✅ |
| 测试覆盖归档/不归档/空 DB 三种 | ✅ |
| 全量 pytest 仍 535 passed | ✅ |

## 二、改造点

### orchestration/__main__.py

| 位置 | 改动 |
|---|---|
| `_cmd_replay` | 默认先 `reap_dead(...)` 写 dead.log / dead_index.jsonl / 移产物到 `cfg.paths.dead_dir`,再 `requeue_dead()`。顺序固定(归档先,requeue 会清空 src_path) |
| `p_replay` argparse | 新增 `--no-archive` 选项,跳过 reap,直接 requeue |

### 设计选择

- **契约 §7.5 不动**:契约只说 requeue 流程;自动归档是行为改进,新增步骤
- **归档默认开启**:向后兼容(原有调用方不需要写额外步骤),但产物被 move 后源路径失效
- **`--no-archive` 显式逃逸**:给"想保留产物位置"的高级用户用
- **dead.log / dead_index.jsonl 写 cfg.paths.log_dir**:与 health.json 同目录,统一运维入口

### 现有 reap_dead 行为

`reap_dead` 用 SQLite `id` 列(整数 PK)做归档文件 prefix — 文件名为 `<int_id>__<src_basename>`,**不是** `<task_id>__<src_basename>`。这与契约 §6.4 一致;测试用 `*__T_BAD__sess_bad.json` 通配。

## 三、新增测试(`TestReplayAutoArchive`,3 个)

| 测试 | 覆盖 |
|---|---|
| `test_replay_cli_archives_before_requeue` | 默认 replay: src_path 被 move 到 dead_dir、dead.log 落盘、CLI 顺序 (archived 在 requeued 前)、requeue 后 phase=dead → pending 且 src_path 清空 |
| `test_replay_cli_no_archive_skips_reap` | `--no-archive`: 跳过 reap、源文件留原位、dead.log 不写、requeue 仍发生 |
| `test_replay_cli_no_dead_archive_requeue_zero` | 空 dead DB: 返 0、CLI 输出 "archived 0 / requeued 0"、dead.log 不写 |

## 四、关键修复

`test_integration_smoke.py` 在灌 dead task 时直接用 `SQLiteQueue.upsert_task` + `mark_phase(gdr, src_path=...)` + `mark_failed`,**绕开 Master.run**。原因:`_FakeAsyncResult` 在 `gdr_fail=True` 时直接 `mark_failed`,**没有先 `mark_phase(gdr)` 写 src_path**(与生产 `task_pipeline._run_one_task_pipeline` 行为差异)。

测试聚焦 CLI 行为(reap + requeue 衔接),不测 Master.run 的 fake;灌 SQLite 直接构造 dead 状态更准确。

## 五、验证

```
pytest tests/orchestration/test_integration_smoke.py::TestReplayAutoArchive
  → 3 passed in 0.55s

pytest -q
  → 535 passed in 44.80s (Round 2: 532 → +3 new = 535)
```

## 六、未完成项 / 留给后续

| 优先级 | 建议 |
|---|---|
| P2 | reap_dead 归档文件名用 `task_id` 字符串(可读性)而非 `id` 整数 — 需更新契约 §6.4 + 现有 35 测试 |
| P2 | QwenPaw 真实端点打通后跑全 catalog smoke (98 task) |
| P3 | producer_simulate.run_one_task 加 deprecation 警告 |
| P3 | 并行 N=4 真子进程烟测(需 QwenPaw 在线) |

## 七、总结

P1 改进落地:`replay` 现在是完整的"先归档再复活"工作流,与契约 §6.4 reap_dead 行为对齐,535 测试全绿。