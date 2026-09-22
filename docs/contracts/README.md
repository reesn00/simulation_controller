# Pipeline Contracts — simulation_controller 三模块接口契约

> 本目录定义 `simulation server → gdr → etl` 三模块交接面的数据契约。
> 任何模块越过契约的边界（直接读取对方内部文件、修改对方私有字段、
> 在自己模块内 import 对端的实现细节）都视为破坏接口，必须先改契约再实现。

## 1. 新流程总览

```text
┌───────────────────┐       ┌───────────────────┐       ┌───────────────────┐
│ simulation server │       │       gdr         │       │       etl         │
│  (simulate_serve) │       │  (refine-only)    │       │  (format convert) │
└────────┬──────────┘       └─────────┬─────────┘       └─────────┬─────────┘
         │                            │                            │
         │       Contract C1          │       Contract C2          │       Contract C3
         │     trajectory events      │     refined Session        │    final 4-view files
         │                            │                            │
         ▼                            ▼                            ▼
output/agent_trajectory/*.json   output/refined/*.json       output/refine_data/*
```

### 1.1 状态机

```
pending  ──►  gdr_processing  ──►  pending_etl  ──►  etl_processing  ──►  done
     │              │                  │                  │
     ▼              ▼                  ▼                  ▼
   error          error              error              error
```

队列状态字段对应 `orchestration/queue/schema.sql`，每阶段结束由对应 worker
回写 `mark_gdr_done` / `mark_etl_done` 推进。

### 1.2 与旧流程的差异

| 维度 | 旧（simulation server → etl → gdr） | 新（simulation server → gdr → etl） |
|---|---|---|
| etl 调用时机 | 头部（qf_worker）+ 尾部（gdr._apply_usage_prune） | 单一尾部（etl_worker） |
| gdr 输入格式 | 已渲染 qf_out（含 `metadata.openai_messages / qf_text`） | 原始 trajectory（或轻解析 Session） |
| gdr 操作语义 | refine blocks + 重渲染 qf_text | refine blocks only |
| etl 职责 | 重（partition / template / summarizer / transform / 一次 usage-prune） | 重（partition / template / summarizer / transform / usage-prune 一次） |
| qf_text 渲染时机 | qf_worker 渲染一次 → gdr 重渲染一次 | etl_worker 渲染一次（基于 refined blocks） |
| system prompt 切分 | qf_worker 做 | etl_worker 做 |

旧流程的根本问题是"etl 在头部做格式整理、在尾部又被 gdr 调一次 usage_prune"，
导致 etl 模块同时承担"上游清洗"和"下游格式化"两种语义。新的拆法把 etl 收敛为
单一职责（pure format conversion），把 refine 与 format 解耦。

## 2. 三个契约一览

| 编号 | 名称 | 生产者 | 消费者 | 文件形态 |
|---|---|---|---|---|
| C1 | trajectory_events | simulation server | gdr | `output/agent_trajectory/<run_id>__<session_id>.json`（JSONL 事件流） |
| C2 | refined_session | gdr | etl | `output/refined/<TXXX>__<session_id>.json`（单 Session 对象） |
| C3 | final_sft_views | etl | 训练 / 审计 | `output/refine_data/<TXXX>__<session_id>_refined.{messages,openai,qwenjina.txt,meta}.json` |

| 契约 | 详解 |
|---|---|
| C1 trajectory 事件流 | [C1-trajectory-events.md](C1-trajectory-events.md) |
| C2 精修后 Session | [C2-refined-session.md](C2-refined-session.md) |
| C3 最终 4 视图 | [C3-final-sft-views.md](C3-final-sft-views.md) |
| 调整方案 | [migration-plan.md](migration-plan.md) |

## 3. 跨契约约束

1. **task_id 维稳**：`run_id → task_id` 映射在 orchestration 队列里维护，
   gdr 与 etl 都通过 `task_id` 命名各自输出文件；不再依赖文件名反推 task。
2. **schema_version**：每个 C2 / C3 顶层携带 `schema_version` 字段
   （C2: `"refined_session.v1"` / C3: `"sft_views.v1"`），版本破坏式升级时改号。
3. **未知字段向前兼容**：消费方（gdr / etl / 训练框架）必须忽略对方 metadata
   中的未知字段，不做严格 schema 校验；schema 演进由 Pydantic `extra="allow"` 保障。
4. **失败隔离**：任一阶段失败 → 写 dead，不传染下一阶段；旁路 audit 文件
   （incomplete / judge_low / deferred / routing_low）由 gdr 写，不进 etl 流水线。