# Pipeline 重构第 2 轮 — 执行清单(2026-09-22)

> 第 1 轮交付已完成(`round-1-summary.md`),本轮做 A + B + C 三合一:文档 / smoke / 集成验证。
> 不改 orchestration / simulate_serve / gdr / etl 业务代码。

## 第 1 轮交付基线

- `pytest` 496 passed
- `python -m simulate_serve --validate-config` 工作
- `python -m orchestration start --dry-run --parallelism 1` 工作
- 真实 catalog smoke 未跑(本轮做)

## 三个并行子 agent

### agent-docs — 文档改写

**任务包**:A 文档
**问题记录文件**:`docs/设计方案/agent-docs_design_adjustments.md`

**改动文件**:

| 文件 | 改动方向 |
|---|---|
| `README.md` | 删 `--batch-size` 例子;改"orchestration 三阶段" / "orchestration 边界" 章节;加 `--parallelism N` 例子 |
| `orchestration/README.md`(若存在) | 同步子命令 + 字段 |
| `docs/orchestration-design.md` | §6 主循环章节改写(从 batch + worker → multiprocessing.Pool + task_pipeline);§8 子命令章节删 `--batch-size`;§10 smoke 章节改 |
| `CLAUDE.md` | 同步:Pipeline 流转图、`orchestration` 描述、命令行示例 |

**关键约束**:
1. 严格按 `pipeline-contracts.md` / `pipeline-serial-parallel-refactor.md` / `round-1-summary.md` 落地。
2. 不改业务代码(orchestration / simulate_serve / gdr / etl 源码不动)。
3. 不改契约文件(`pipeline-contracts.md` / `pipeline-serial-parallel-refactor.md`)。
4. 文档措辞与现有 `CLAUDE.md` 风格一致(中文 + 表格 + file:line)。

**输出**:
- 改动文件清单
- `agent-docs_design_adjustments.md`(如有问题)

---

### agent-smoke — 真实 smoke 跑通

**任务包**:B smoke 端到端
**问题记录文件**:`docs/设计方案/agent-smoke_design_adjustments.md`

**任务**(基于 `round-1-summary.md` §七未完成项):

1. **单 task smoke**:`python -m orchestration start --tasks T001 --parallelism 1`
   - 验证:tasks/<run_id>/run.json 落盘 + trajectory .json 落盘 + C2 refined .json 落盘 + C3 4 视图落盘
   - SQLite `phase=done` 的 task 数 = 1

2. **3 task 串行 smoke**:`python -m orchestration start --tasks T001,T002,T003 --parallelism 1`
   - 验证:3 份 C2 + 3×4 视图 = 12 份 etl 产物
   - SQLite 3 个 task 全 `phase=done`

3. **3 task 并行 smoke**:`python -m orchestration start --tasks T001,T002,T003 --parallelism 4`
   - 验证:同样 3 task 跑通
   - 性能对比(可选):并行 vs 串行总耗时

4. **小批量串行 smoke**(≤5 task):`python -m orchestration start --tasks T001,T002,T003,T004,T005 --parallelism 1`
   - 验证:失败率 ≤ 5%(若有失败,记录 task_id + stage + error_msg)

5. **全 catalog 并行 smoke**:`python -m orchestration start --all-tasks --parallelism 4`
   - **警告**:98 task × 4 并行 = 大量产物;需要磁盘空间 + LLM 调用配额
   - 如果实际跑不动(磁盘/配额),记录到 `agent-smoke_design_adjustments.md`,改跑 catalog 子集

**产物**:
- `output/runs/<run_id>/run.json`
- `output/agent_trajectory/<run_id>__<session_id>.json`
- `output/refined/<task_id>__<session_id>.json`
- `output/refine_data/<task_id>__<session_id>.{messages,openai,qwenjina.txt,meta}.json`
- `output/orchestration/orchestration.db`(SQLite phase=done 计数)
- `output/orchestration/logs/health.json`

**关键约束**:
1. 不改 orchestration / simulate_serve / gdr / etl 业务代码。
2. 不改契约文件。
3. 任务跑通后,把每次跑的耗时 / 失败率 / SQLite phase 计数写到 `agent-smoke_design_adjustments.md`。
4. 如果失败(LLM rate limit / 远端 5xx / simulate_serve 业务失败),记录但不阻塞下一项。
5. 不改 `config/config.yaml`(用现网配置)。
6. 不引入新依赖。

**输出**:
- 改动文件清单(可能:只新增产物路径记录文档)
- `agent-smoke_design_adjustments.md`(跑通情况 + 失败记录 + 性能数据)

---

### agent-verify — 集成 verify

**任务包**:C 集成验证
**问题记录文件**:`docs/设计方案/agent-verify_design_adjustments.md`

**任务**:

1. **`status` 子命令**:任务跑通后跑 `python -m orchestration status`
   - 验证:打印 `{phases: {...}, total, last_updated}` 6 个 phase 字段齐全 + 计数正确

2. **死信归档**:构造一个 gdr 失败的 task(可以通过改 `task_id` 故意传不存在的 trajectory 路径,或 monkeypatch gdr 抛 NonRetryable)
   - 跑通后跑 `python -m orchestration replay` 或 reap_dead
   - 验证:`output/orchestration/dead/` 有归档文件 + `log_entry` 写入 + phase=dead 计数正确

3. **replay 复活**:把 `phase=dead` 的 task 改回 `phase=pending`
   - 跑 `python -m orchestration replay`
   - 验证:SQLite 中对应行 phase=dead → pending,error_msg 清空
   - 跑回该 task,验证它能正常走完三阶段

4. **写集成测试**:新增 `tests/orchestration/test_integration_smoke.py`
   - 测 status 打印格式
   - 测 reap_dead 归档文件存在性
   - 测 replay 重置 SQLite 行

5. **检查点对照**:对照 `round-1-summary.md` §六"验证标准":
   - 5 项中除了"真实 catalog smoke"已做,其余都要在 pytest 集成测试中覆盖

**关键约束**:
1. 集成测试不依赖真实 LLM / 远端 QwenPaw,使用 monkeypatch / fake_pool 模式(参考 `test_pipeline_executor.py::_FakePool`)
2. 测 status 时构造一个 `SQLiteQueue` 填充若干 task 行,验证 `collect_tasks` 返 6 个 phase key 全在
3. 测 reap_dead 时 monkeypatch `_run_one_task_pipeline` 让某个 task 走 dead 路径
4. 测 replay 时调用真实 `SQLiteQueue.requeue_dead()`,验证 SQLite 行被改回
5. 不改 orchestration / simulate_serve / gdr / etl 业务代码
6. 不改契约文件
7. 新增测试要 `pytest tests/orchestration/test_integration_smoke.py -q` 全绿

**输出**:
- 改动文件清单(主要:`tests/orchestration/test_integration_smoke.py` 新增)
- `agent-verify_design_adjustments.md`

---

## 主 agent 工作流

### 阶段 0:启动前确认
- 第 1 轮交付完整 ✅(见 `round-1-summary.md`)
- 本轮执行清单已写 ✅(本文件)

### 阶段 1:启动 3 个并行子 agent
- 3 个 Agent tool 调用,**同一条 message 内**并行发出
- 每个 agent 拿到:
  - 本执行清单(本文)
  - `round-1-summary.md`(第 1 轮交付基线)
  - 契约文档 `pipeline-contracts.md` 只读
  - 自己的任务包(上表)
  - 问题记录文件路径
  - 严格约束(不能改契约,只能写问题文件)

### 阶段 2:等待 3 个 agent 完成
- 用 SendMessage 续接在途 agent(如有需要)
- 不读 agent 输出文件

### 阶段 3:收集 + 分析
- 收 3 个 hand-back 报告
- 读 3 个 `_design_adjustments.md` 文件
- 分类:重大决策 / 实现细节微调 / 测试覆盖缺口

### 阶段 4:处理遗留项
- 重大决策 → AskUserQuestion 询问
- 微调 → 主 agent 直接修

### 阶段 5:串联验证
```bash
# 1. 全量 pytest(应保持 496+ green)
pytest -q

# 2. 真实 smoke 入口仍工作
python -m simulate_serve --validate-config
python -m orchestration --help
python -m orchestration start --all-tasks --dry-run --parallelism 1

# 3. 文档渲染检查(markdownlint / GitHub 预览)
# (可选)
```

### 阶段 6:汇总报告
- 主 agent 写最终汇总到 `docs/设计方案/round-2-summary.md`

---

## 关键风险

| 风险 | 缓解 |
|---|---|
| agent-smoke 跑不动真实 catalog(LLM/磁盘) | 跑小子集(1 task / 3 task),catalog 大集合跑不动就降级 |
| agent-verify 集成测试依赖真实远端 | monkeypatch / fake_pool 模式 |
| 3 个 agent 同时改 README.md / orchestration/README.md 同一文件 | agent-docs 单独负责 README 类;其他 2 个不动 README |
| agent-smoke 跑耗时过长 | dry-run 验证在 agent-smoke 任务前已完成,真实跑限时(每 task < 5min) |
