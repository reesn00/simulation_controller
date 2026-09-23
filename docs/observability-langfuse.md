# Langfuse 可观测性 — 用户视角总览

> 状态:已落地(2026-09-23,PR 6)
> 配套基线:[docs/observability-langfuse-plan.md](observability-langfuse-plan.md)(设计意图 + 配置 schema + 工厂代码)
> 模块级实施参考:[docs/langfuse-simulate-server.md](langfuse-simulate-server.md) · [docs/langfuse-gdr.md](langfuse-gdr.md) · [docs/langfuse-etl.md](langfuse-etl.md)

本文档面向**最终使用者**(数据科学家 / 运维 / 调试者),回答以下问题:

1. 这个集成是为了什么?
2. 怎么启用 / 关闭?
3. 启用后能看到哪些 trace / span?
4. 出问题怎么排查?
5. 怎么控制开销?

如果你是**开发者**(需要改 Langfuse 接入点),请直接看配套基线和模块级实施参考,本文档不重复实现细节。

---

## 1. 为什么需要 Langfuse

`simulation_controller` 三阶段流水线(`simulate_serve → gdr → etl`)对每条 Session 产生大量中间制品:

- `simulate_serve` 写 `output/agent_trajectory/<run>__<session>.json` (C1 trajectory)
- `gdr` 写 `output/refined/<TXXX>__<session>.json` (C2 refined Session)
- `etl` 写 `output/refine_data/<TXXX>__<session>_refined.{messages,openai,qwenjina.txt,meta}.json` (C3 4 视图)

Langfuse 用于**对比观测**这三次形态演化的每一个步骤:

| Langfuse 体现 | 实际价值 |
|---|---|
| 同一 `session_id` 串联 3 个独立 trace | 在 UI 里点开一个 session,沿时间轴看到"原始 trajectory → 每步精修 → 最终训练视图"的完整演化 |
| 每个步骤 `span.input` / `span.output` 是该步骤进入 / 退出时的 session 快照 | 直接在 UI 里 diff 两个相邻 span,无需打开 6 个 jsonl |
| trace metadata 含 `task_id` / `run_id` / `attempt` / `terminal_reached` | 按 task 聚合,排查某条 task 退化到哪个步骤 |
| 子 span 含 token usage / latency / 模型名(LLM 步骤) | 监控 LLM 步骤耗时分布 + 失败率 |

### 1.1 隐私边界(必读)

CLAUDE.md "不保存自由文本思维链、Cookie、Authorization Header 或浏览器 Profile" 是**落盘产物**(`output/`)的约束。

Langfuse 观测副本是**独立项目**,按设计上传**完整** trajectory / refined Session / 4 视图内容用于对比观察。**它不入训练集**,也不替代 `output/` 制品的脱敏策略。如果 Langfuse 后端账户被攻破,泄露的是观测副本,**不是训练数据**;反过来训练数据流跑的是 `output/` 制品,遵守 CLAUDE.md 既有规则继续脱敏。

简而言之:**两条数据流独立,共享一份 `runtime_id`(用于 Langfuse 端 session 聚合)**。

---

## 2. 启用

### 2.1 安装 SDK(可选)

按 CLAUDE.md "默认不访问公网" 约束,`langfuse` 是 **optional dependency**:

```bash
# 在根 pyproject.toml 已声明 [project.optional-dependencies] observability
uv sync --extra observability
```

不启用观测时不必安装;启用前再装即可。

### 2.2 配置开关

打开仓库根 `config/config.yaml`(本地,gitignored),新增 / 修改 `langfuse:` 段:

```yaml
langfuse:
  enabled: true                       # 关闭 -> false(默认)
  public_key: "${LANGFUSE_PUBLIC_KEY}"
  secret_key: "${LANGFUSE_SECRET_KEY}"
  base_url: "https://cloud.langfuse.com"  # 自部署改你的 host
  environment: "dev"                  # 生产改 "prod"
  release: "${LANGFUSE_RELEASE:-local}"
  sample_rate: 1.0                    # 0.1 = 10% 采样,大规模任务用
  flush_at: 512                       # SDK 批传阈值
  flush_interval: 5.0                 # SDK 定时 flush 间隔
  timeout: 10                         # HTTP timeout

  # Payload 三种模式 (默认 full)
  upload_payload: full                # full | summary | none
  max_payload_bytes: 0                # 0 = 不限制;>0 触发降级到 summary

  # 阶段级开关
  stages:
    simulate_serve:
      enabled: true                   # 该阶段是否上传
    gdr:
      enabled: true
      per_step_span: true             # 21 步骤逐个 span
      per_llm_span: false             # LLM 调用级(默认 true,100+ spans/session)
    etl:
      enabled: true
      per_step_span: true
```

凭据经 `${LANGFUSE_*}` 环境变量注入,**不要**把明文 public_key / secret_key 写进 yaml:

```bash
# Linux / macOS
export LANGFUSE_PUBLIC_KEY="pk-lf-..."
export LANGFUSE_SECRET_KEY="sk-lf-..."

# Windows PowerShell
$env:LANGFUSE_PUBLIC_KEY = "pk-lf-..."
$env:LANGFUSE_SECRET_KEY = "sk-lf-..."
```

### 2.3 验证配置

```bash
# CLI 不报错 + 能看到 langfuse 段
uv run python -m simulate_serve --validate-config

# enabled=false 时 get_client 返回 None,业务零影响
uv run python -c "
from simulate_serve.config import LangfuseConfig
from simulate_serve.observability.langfuse_client import get_client
print(get_client(LangfuseConfig(enabled=False)))
# -> None
"
```

### 2.4 一键关闭

```yaml
langfuse:
  enabled: false
```

回退路径:旧批次 / 旧任务零侵入。

---

## 3. 配置字段参考

> **白名单(冻结)**:工厂 `_extract_langfuse_fields` 只接受下列字段名,其他字段被忽略。新增字段必须先改工厂 PR。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 全局开关 |
| `public_key` | str | `""` | Langfuse 公钥,空 = 关闭 |
| `secret_key` | str | `""` | Langfuse 私钥,空 = 关闭 |
| `base_url` | str | `https://cloud.langfuse.com` | 自部署 Langfuse 改这里 |
| `environment` | str | `dev` | Langfuse `environment` 字段 |
| `release` | str | `local` | Langfuse `release` 字段(版本号) |
| `sample_rate` | float `[0.0, 1.0]` | `1.0` | SDK 端抽样 |
| `flush_at` | int ≥ 1 | `512` | SDK 批传阈值 |
| `flush_interval` | float > 0 | `5.0` | SDK 定时 flush 间隔(秒) |
| `timeout` | int > 0 | `10` | HTTP timeout(秒) |
| `upload_payload` | `full` / `summary` / `none` | `full` | 三态 payload 模式 |
| `max_payload_bytes` | int ≥ 0 | `0` | session payload 截断阈值(0 = 不截) |
| `max_block_payload_bytes` | int ≥ 0 | `0` | 单 block payload 截断阈值(0 = 不截) |
| `stages.*.enabled` | bool | `true` | 阶段级开关(stages 嵌套段不传给 simulate_serve LangfuseConfig schema) |
| `stages.gdr.per_step_span` | bool | `true` | 21 步骤逐个 span |
| `stages.gdr.per_llm_span` | bool | `false` | LLM 调用级 generation span |
| `stages.gdr.per_refine_span` | bool | `false` | 每条 repair_item 一个 span |
| `stages.etl.per_step_span` | bool | `true` | etl 端 2 个子 span |

`stages:` 嵌套段由 gdr / etl loader 各自消费;模拟启动后从根 yaml 复制到 `simulate_serve.langfuse` 时**剥除 `stages:`**(`AppConfig.LangfuseConfig` 是 `StrictConfig`,`extra="forbid"`)。

---

## 4. 各阶段 Span 名清单

> **总规则**:三阶段用**同一个** `session_id`(`run.remote_session_id`,即 `useramulation-xxx`),Langfuse 端按 session_id 聚合。每个阶段产生**独立 trace**,**不强制父子跨进程**。

### 4.1 simulate_serve 阶段

| Span 名 | 触发时机 | metadata 关键字段 |
|---|---|---|
| `simulate_serve:<task_id>` | 每个 executor turn 的 `archive()` 调用 → `_emit_trail`(`finally` 块) | `run_id` / `task_id` / `agent_id` / `terminal_reached` / `last_event_type` / `trajectory_path` |

**多轮语义**:同一 session 多个 turn → 多个 trace,**取最后一个 `output` 是 C1 trajectory 终态**(覆盖式累积);`metadata.terminal_reached=true` 是终态,`false` 标 partial。

**Payload**:`span.output` 是完整 C1 trajectory dict(JSONL → list of events);`span.input` 为 None(`_emit_trail` 不传输入)。

### 4.2 gdr 阶段

**Outer**: `gdr.process_one`(在 `gdr/pipeline/runner.py::process_one` body 起,`run_gdr_once` 不再起 outer)

**子 spans(21 项 + 3 reassemble generation + 1 retry_loop_clip judge generation,共 25 项)**

| Span 名 | 类型 | 备注 |
|---|---|---|
| `gdr.hard_filter` | span | step -1,Session 级硬过滤 |
| `gdr.light_health` | span | step 0,零 LLM |
| `gdr.context_understanding.build` | span | step 1,零 LLM |
| `gdr.fold.failed_toolresults` | span | step 2,零 LLM |
| `gdr.fold.repeated_thinking` | span | step 2,零 LLM |
| `gdr.retry_loop_clip` | span | step 2.3,外层 |
| `gdr.retry_loop_clip.judge` | **generation** | step 2.3,LLM judge |
| `gdr.cu.retrack_state` | span | step 3,LLM |
| `gdr.user_intent.heuristic` | span | step 4,零 LLM |
| `gdr.router.tag` | span | step 5,LLM fan-out |
| `gdr.policy.decide` | span | step 6,零 LLM |
| `gdr.refine.run_repairs` | span | step 7-12,LLM ThreadPoolExecutor |
| `gdr.early_exit` | span | step 11,无修复退出 |
| `gdr.reassemble` | span | step 13,外层 |
| `gdr.reassemble.user_intent_llm` | **generation** | step 13 |
| `gdr.reassemble.consistency_check` | **generation** | step 13 |
| `gdr.reassemble.l3_judge` | **generation** | step 13 |
| `gdr.timeout_fallback` | span | step 13,降级 |
| `gdr.unhandled_error` | span | step 13,异常 |
| `gdr.audit.routing_abstain` | span | step 16,旁路队列 |
| `gdr.incomplete_check` | span | step 17,旁路队列 |
| `gdr.save_refined_session` | span | step 18,IO |
| `gdr.audit.deferred` | span | step 19,旁路队列 |
| `gdr.audit.judge_low` | span | step 20,旁路队列 |

**Payload**:每个非 LLM 步骤的 `span.input` = 进入时 session 深拷贝快照,`span.output` = 退出时 session(by-ref,延迟到 span 退出时取);LLM 步骤按生成 token 字符数计。

**可选更细**(`stages.gdr.per_llm_span: true` 开启,默认 false):每条 `LlamaCppClient.chat` 调用一个 generation span,span 名 `gdr.llm.<model>`,含 max_tokens / temperature / token usage。**span 数会爆炸到 100+ per session,建议同时降到 `sample_rate=0.1`**。

### 4.3 etl 阶段

| Span 名 | 触发时机 | 备注 |
|---|---|---|
| `etl:<task_id>` | `run_etl_once` body 外层(`stage_trace`) | metadata 含 `attempt:N`(重试序号) |
| `etl.load_refined_session` | `load_refined_session(c2_path)` 入口 | `input` = C2 文件内容 dict,`output` = Session 对象 |
| `etl.save_c3_4views` | `save_session_v2(session, base_path)` 入口 | `input` = Session,`output` = 4 视图文件路径 + 字节数 |

**进程模型**:`run_etl_once` 在 `multiprocessing.Pool` worker 中跑,Langfuse SDK 不 fork-safe,worker 子进程 fork 后:

1. 工厂 `_reset_for_fork()` 自动清旧 `_client`(`task_pipeline._worker_init` 已注册)
2. 子进程下次 `get_client(cfg)` 触发重 init(主进程 / 子进程各持一份 client)
3. `finally` 块 `client.flush()` 把内存里 batch 推完

**重试 trace**:`_safe_run_etl` 重试 N 次成功 N 次 → **N 个独立 trace**(metadata 含 `attempt:N`)。Langfuse 端按 session_id 聚合时 N 次尝试按时间排列,各自独立 input/output。

---

## 5. 故障排查

### 5.1 SDK 异常

工厂 `_open_observation` 在 PR 5 补了 try/except:

```python
try:
    cm = client.start_as_current_observation(...)
    ...
except Exception as exc:
    logger.warning("langfuse start_observation failed: %s", exc)
```

业务异常 / SDK 异常**不影响**原业务路径,只 `logger.warning` 记录。如果日志看到 `langfuse start_observation failed`,说明 SDK 升级或网络阻塞,但**业务流程照常完成**。

### 5.2 flush 数据丢失

每个 etl worker / 主进程入口会调 `client.flush()`。如果 worker 被 **SIGKILL** 或 **OOM**,进程级内存里的 batch 没推完。缓解:

- 默认 `flush_at=512` 让 SDK 周期性自动推,不等 worker 退出
- 减小 `flush_at` 到 256 / 128,代价是 HTTP 请求频率上升
- 兜底:重要批次跑完调 `client.shutdown()`(已注册在 `bootstrap.close()` 和 `task_pipeline._worker_init` 的 `atexit`)

### 5.3 Pool worker socket

`multiprocessing.Pool` 默认 fork 后子进程**继承父进程 socket**。Langfuse SDK 用 HTTPS + 长连接,socket 在子进程里**不可用**(file descriptor 失效)。

工厂通过 `_reset_for_fork()` 解决:子进程下次 `get_client` 时重新 init 一份 SDK client(用新 socket)。如果看到 `ConnectionError` / `Bad file descriptor`,说明 `_reset_for_fork` 没被调用 → 检查 `task_pipeline._worker_init` 是否被执行(必须用 `Pool(initializer=...)`,不是 `Pool(processes=...)`)。

### 5.4 session_id 不一致

三阶段必须用**同一个** `session_id`(`run.remote_session_id`)。如果 Langfuse UI 看到三个独立 trace 不串起来,检查:

- simulate_serve:`trajectory_archiver.set_run_context` 是否被 `run_task._archive_trajectory` 调用(否则 `_run_ctx={}`,`session_id=""` 兜底失效)
- gdr:`Session.session_id` 字段是否被 C1 → C2 转换保留
- etl:`etl_worker.run_etl_once(session_id=...)` 入参是否与 gdr 一致

### 5.5 enabled=false 但仍看到日志

`get_client` 在 `enabled=False` / 缺凭据 / SDK 未装 三种场景都返回 None,业务零侵入。**不会**触发 `Langfuse()` 实例化。日志里若看到 Langfuse 相关条目,只能是别的代码(如直接调用 `langfuse.Langfuse()` 而不通过工厂)引出。

---

## 6. 性能与采样建议

| 场景 | 建议配置 | 估算 |
|---|---|---|
| 单 task 调试 | `enabled=true` + `sample_rate=1.0` + `per_step_span=true` + `upload_payload=full` | ~25 spans/session, 1-2 MB payload/session |
| 58 task 全量回归 | `enabled=true` + `sample_rate=0.1` + `upload_payload=summary` | ~145 spans total, 0.1 MB/task summary |
| 生产压测 | `enabled=false` 或 `sample_rate=0.01` | ~15 spans total |
| 网络受限(Intranet 部署) | `enabled=true` + `base_url=http://lf.internal` + `flush_at=256` | 视网络带宽 |

**最大瓶颈**:`gdr` 21 step × `copy.deepcopy(session)`。典型 session 50-500 个 block,Python 深拷贝在 ms 量级,可接受;**极端 session(1000+ block)请切 `summary` 模式或 `max_payload_bytes=5242880`(5MB)**。

**Span 数爆炸**:`stages.gdr.per_llm_span: false` 是默认;开了之后 span 数到 100+ per session,Langfuse 配额(按 span 计费)会爆炸。

---

## 7. 链接

| 读者 | 文档 |
|---|---|
| 用户 / 运维 / 数据科学家 | 本文(就这一篇) |
| 设计师 / 架构师 | [docs/observability-langfuse-plan.md](observability-langfuse-plan.md)(设计意图 + 13 字段 schema + 风险与回退) |
| simulate_serve 开发者 | [docs/langfuse-simulate-server.md](langfuse-simulate-server.md)(PR 2 实施参考) |
| gdr 开发者 | [docs/langfuse-gdr.md](langfuse-gdr.md)(PR 3 实施参考) |
| etl 开发者 | [docs/langfuse-etl.md](langfuse-etl.md)(PR 4 实施参考) |

---

## 文档元信息

- **状态**:已落地(2026-09-23,PR 6)
- **总章节**:7 节(概述 / 启用 / 配置 / Span 名清单 / 故障排查 / 性能 / 链接)
- **估算字数**:~3000 字(含表格)
- **覆盖 PR**:1(工厂)+ 2(simulate_serve)+ 3(gdr)+ 4(etl/gdr 残余)+ 5(orchestration plumbing)+ 6(集成测试 + 收尾 + 文档整合)