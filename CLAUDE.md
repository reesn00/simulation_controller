# CLAUDE.md

## 项目概述

本项目是基于 CAMEL-AI 的 Agent 用户模拟端。它把 Persona、Scenario、Task 编译为 `CompiledTask`，以用户身份驱动远端 QwenPaw 执行 Agent，多轮验证结果并生成追问，最终输出可审计 Run 和清洁蒸馏数据。

## 常用命令

```powershell
uv sync --group dev
uv run python -m simulate_serve --validate-config
uv run python -m simulate_serve --check-tools
uv run python -m simulate_serve --readiness
uv run python -m orchestration start --tasks T001,T003 --parallelism 1
uv run python -m orchestration start --all-tasks --parallelism 4 --dry-run
uv run python -m orchestration status
uv run python -m orchestration replay
uv run python -m pytest -q
.\scripts\label_studio.bat            # C3 + 评分卡 → Label Studio (init → status → upload)
```

> `python -m simulate_serve --tasks / --rerun-task / --limit / --include-offline` 已于 2026-09-22 删除（任务运行入口移交 `orchestration`）；`simulate_serve` 仅保留只读开关。orchestration 默认 `max_parallelism=1` 严格串行，≥2 启用 `multiprocessing.Pool` 并发。

远端执行 Agent 的 LLM 功能已可用（2026-09 确认，此前"未启用"记录已失效）。完整链路验证为可执行项：真实模型输出的端到端批次应当实际运行并记录结果，不再标记为待验证；日常回归仍以单元、合约和离线功能测试为默认门禁。

## 当前架构

```text
CLI / Bootstrap
  -> CatalogLoader -> TaskCompiler -> CompiledTask
  -> BatchRunner -> TaskRuntime / RunStateMachine
       -> InteractionActor
       -> AsyncQwenPawExecutor
       -> ValidationPipeline
            -> deterministic validators
            -> ToolRegistry / BrowserEvidenceProvider
            -> local Semantic Judge
       -> JsonRunRepository
```

边界要求：

- Interaction Actor 只负责自然表达，不拥有验证工具、不决定成功。
- TaskRuntime 使用普通 Python 状态机，不让 LLM 控制状态和重试。
- 本地 ValidationPipeline 拥有最终验收权，聚合为 `FAIL > ERROR > INCONCLUSIVE > PASS`。
- ToolRegistry 是工具创建、健康检查、能力选择和关闭的唯一 owner。
- 所有必选 Criterion 必须 PASS 才能成功；工具缺失不能 fail-open。
- 不保存自由文本思维链、Cookie、Authorization Header 或浏览器 Profile。
  *定义*:「思维链」按**加工状态**分两类 —— **raw CoT**(QwenPaw 原始输出,未经 gdr
  精修)与 **refined CoT**(经 `thought_refactor` 精修后)。本红线约束 **raw CoT 的
  外传**;refined CoT 属训练制品,不受此限(C3 保留 thinking 是 CoT SFT 的必要输入,
  见 MEMORY「ETL drops structured thinking」的修复决策 —— 不要"修复"掉它)。
  *落盘约定*:C1 trajectory 落 raw CoT 供重放;C2/C3 落 refined CoT。
  *外部副本例外*(2026-09-28 已实施):Label Studio 推送 C3(refined CoT)
  与**评分卡 `scorecard.v1`**(L0–L5 指令评分 + 每维依据 + `source` 可信度标注);
  LS 是本项目**终点**,标注结果不回流(不实现 fetch / 不落 `output/labeled/`,
  `orchestration/queue/` 零修改)。推送前跑 R11 凭据扫描,命中 **fail-closed 拒推**。
  设计见 [`docs/设计方案/label-studio-integration.md`](docs/设计方案/label-studio-integration.md),
  契约见 [`docs/contracts/C4-scorecard.md`](docs/contracts/C4-scorecard.md),
  用法见 [`docs/observability-label-studio.md`](docs/observability-label-studio.md)。
- `gdr/reassembly/reassembler.py` 工具配对扫描必须**跨 toolcall 连续扫描**——并行调用（call, call, result, result）下"在下一个 toolcall 处截断"会把成功调用误判为失败删除。

## 数据格式约定

QwenPaw trajectory 形态（2026-09-18 起，单路径事件流）：重放唯一入口 `etl/qwenformat/load.py::parse_trajectory`（被 `gdr/parsers.from_trajectory` 薄包装），每轮一个 assistant message（含全部 thinking / tool_call / tool_result / 最终 text）。

事件约定：
- `model_response.payload.content` 携带模型输出块：`thinking`（独立结构化块）/ `tool_call`（state=pending）/ `text`
- `tool_call_request` 独立事件回归但冗余（与 model_response 重复），重放跳过
- `tool_execution` 是工具结果唯一事件源（state 在 `metadata.end_state`）
- `final_reply.payload.content` 是冗余快照，只取 `metadata.usage`

格式演化历史与早期 AI SDK 内嵌快照路径见 `docs/project-notes.md`。

## Pipeline 流程（2026-09-22 起新架构）

`simulation server → gdr → etl`。三阶段各守一道边界，每段交接面写一份契约文件。

```text
┌──────────────────┐         ┌──────────────┐         ┌──────────────────┐
│ simulation       │  C1     │     gdr      │  C2     │       etl        │
│ server           │ ──────► │ (refine)     │ ──────► │ (format convert) │ ─► 训练
└──────────────────┘         └──────────────┘         └──────────────────┘
output/agent_trajectory/      output/refined/          output/refine_data/
```

| 阶段 | 输入 | 输出 | 职责 |
|---|---|---|---|
| **simulation server** | Persona + Scenario + Task | C1 trajectory 事件流 | 驱动远端 Agent，多轮验证 + 追问；落 run 元数据 + 轨迹 |
| **gdr** | C1 trajectory | C2 refined Session（单文件） | 块级精修：硬过滤 + 健康分 + CU + fold + retry_loop_clip + router + policy + refiners + validators + reassemble + meta_tag_strip |
| **etl** | C2 refined Session | C3 4 视图文件 | 格式整理：usage_prune + transform + system_prompt partition + tool_templates + tool_output_summarizer → save_session_v2 拆 4 视图 |

### 关键契约

| 编号 | 路径 | 入口 | 出口 |
|---|---|---|---|
| C1 | `output/agent_trajectory/<run_id>__<session_id>.json` | simulate_serve archiver | `gdr/parsers.from_trajectory` |
| C2 | `output/refined/<TXXX>__<session_id>.json` | `gdr/pipeline/runner.py::_process_one_file` | `etl/parsers.load_refined_session` |
| C3 | `output/refine_data/<TXXX>__<session_id>_refined.{messages,openai,qwenjina.txt,meta}.json` | `etl/writers/render_to_4_views` → `gdr.domain.schema.save_session_v2` | 训练框架 / audit |

完整契约字段级 schema 见 [docs/contracts/](docs/contracts/)。

## 目录索引

| 目录 | 职责 |
|---|---|
| `configuration/` | 严格 Raw Catalog Schema、加载和诊断 |
| `domain/` | Persona、CompiledTask、Run、Validation、Evidence、状态机 |
| `application/` | TaskCompiler、TaskRuntime、BatchRunner、端口 |
| `interaction/` | Prompt、InteractionActor、GuidancePolicy |
| `validation/` | 确定性校验、Claim、Evidence、Semantic Judge、聚合 |
| `tools/` | Registry、health、CAMEL adapter、Playwright/Camoufox |
| `infrastructure/` | 异步 QwenPaw、CAMEL model、v2 Repository/Exporter |
| `etl/qwenformat/` | trajectory 重放 + transform + system_prompt partition + tool_output_summarizer + usage_prune + chat_template |
| `etl/parsers/` | C2 契约入口：`load_refined_session` |
| `etl/writers/` | C3 4 视图写入：`render_to_4_views` → `gdr.domain.schema.save_session_v2` |
| `gdr/parsers/` | C1 契约入口：`from_trajectory` |
| `gdr/domain/` | Session / Message / Block pydantic 类型 + `save_session_v2` / `save_refined_session` |
| `gdr/{refiners,validators,core,reassembly,routing,config,prompts}/` | gdr 内部模块（详见 [docs/设计方案/gdr-plan.md](docs/设计方案/gdr-plan.md)） |
| `label_studio/` | C3 + 评分卡 → Label Studio 单向推送（终点，不回流）；CLI `init-project`/`status`/`upload`/`purge`；R11 凭据扫描；`push_index.py` 推送台账（LS 1.23 无原生去重，见下） |
| `orchestration/` | 顶层调度（master / pipeline_executor / task_pipeline / producer / workers / queue / failure_handler；2026-09-22 起删 watcher / batch_tracker / qf_worker） |
| `tests/` | unit、contract、functional；默认不访问公网 |

## 配置和工具

- Python 配置代码位于 `simulate_serve/config.py` 和 `configuration/`。
- 内置 YAML 只位于 `simulate_serve/config/`，采用文件级 `schema_version: "2"`；v1/v0 仅作为兼容输入。
- 98 个内置 Task（T001–T068 训练集 + E001–E030 分布外泛化评估集）全部关联 10 个对话策略 Scenario；公开 `initial_request` 与本地 `test_fixture` 严格隔离。
- `initial_request` 原样作为首轮远端消息；本地模型不得改写或削弱请求。
- Criterion 的 `remediation` 决定失败责任、自然反馈和是否允许继续引导；只有可重试的 executor-owned FAIL 可以触发追问。
- 追问必须要求远端保留已满足内容并返回包含全部要求的完整修订结果，避免只验最新回复时发生准则振荡。
- Runtime 会识别“此前 PASS、本轮非 PASS”的回退准则，并在追问和 `FOLLOWUP_CREATED` 事件中明确记录。
- Playwright/Camoufox 默认 disabled，启动不自动安装。使用 `--check-tools` 查看完整状态。
- 使用 `--readiness` 在不连接 QwenPaw 的情况下汇总 Judge/Provider 缺口及受影响 Task；该命令不创建 Run 日志。
- 全项目统一配置入口：仓库根 `config/config.yaml`（gitignored，含真实凭据；提交版模板 `config/config.example.yaml`）。四个模块（simulate_serve / orchestration / gdr / etl.qwenformat）的配置收纳于对应 section，`llm:` 共享段提供端点/密钥/模型缺省，支持 `${VAR}` 环境变量占位符。模块级配置文件已删除，根配置缺失直接报错、无兜底。定位可用 `SIMCTL_CONFIG`（gdr 用 `GDR_CONFIG_FILE`）重定向。凭据不得提交、打包、复制到测试、文档或日志。
- Label Studio 段（`label_studio:`，2026-09-28 已实施，两个开关默认关闭）：本项目**终点**是 Label Studio —— 推送 C3 与评分卡，不做回流；凭据走 `${LABEL_STUDIO_API_KEY}` env 或 `api_key_path`。`upload.enabled` 只管 `python -m label_studio upload`（全量），`hook.enabled` 只管 orchestration step 11 自动推送（单条），两者互不串。`credential_scan` 默认开且 **fail-closed 拒推**。**LS 1.23 没有原生去重**（`Task.inner_id` 是整数字段、批量 import 静默丢弃、重复导入照样新建），所以判重与预标注要的数字 task id 都靠本地台账 `output/label_studio/push_index__<project_id>.jsonl`（`session_id → LS task id`，append-only）—— 删掉它等于每次 upload 都推重复样本。`init-project` / `upload` 每次都会把本地 label_config `PATCH` 进项目：LS 端存的是建项目那刻的 XML，不同步就会出现「校验报绿、import 却 400 `data['xxx']`」。
  - **推送只有 etl 之后一个时点**（C3）。simulate 后的 C1 含 raw CoT，外推撞 CLAUDE.md 思维链红线；gdr 后的 C2 会被后续改写，标注等于标中间态。设计依据见 `docs/设计方案/label-studio-integration.md`。
  - **hook 超时 30s 而非 5s**：一次推送串完 PAT 刷新 + 查项目 + PATCH label_config + 建 task + 预标注。超时不丢样本 —— `push_single_c3` 先拿 LS task id 再写台账，「台账没有」严格等价于「LS 上没建成」，批次后 `scripts/label_studio.bat upload` 补推幂等。project 解析在 `ls_hook` 里进程级缓存，**每个 task 都 PATCH label_config 会覆盖标注员的改动**。
  - **低分样本（`judge_discard`）也推**：走完 etl 出 C3，终态仍是 `PHASE_AUDITED`（不洗成 done，否则 status 统计会骗人）。C3 meta 顶层 `audit_reason` → 评分卡 `audit` 标记 → label_config 最上方「低分标记」展示块 + 风险提示第一条。`scoring_reject` 推不了（C2 刻意不写），是设计硬墙。
  - **标注页的展示块是「可改的审查工作区」，不是只读回显**（2026-09-29 实测）。`editable` **只管「加完之后能不能再改」，不管能不能提交**——能否提交取决于 **Add 按钮**，而 Add 按钮在 `rows > 1` 时默认可见（本项目展示块 rows 全 > 1）。实测标注员删改 `messages_view` 后提交，修改原样进了 annotation。**后果**：annotation 里的 `messages_view` / `openai_view` / `metadata_view` 是**标注员的修正稿，不是训练稿**，训练制品永远以 `output/refine_data/` 的 C3 为准。2026-09-30 起展示块改 `editable="true"`（探针实测：提交后**能就地改**，不必为修错字再点一次 Add 多存一条重复 submission；`rows > 1` 因此成为必须保留的约束）。机器判定（低分标记/评分卡/风险提示）与人工判定分开放，异议走 `criterion_verdict` / `revise_notes`——**能改 ≠ 该改**。结构上做成只读也可行（`<Text>` 非控件挂不上 submission），但 `<Text>` 会 trim 掉缩进，而保住缩进的配方 `<Style>.htx-text{white-space:pre-wrap}</Style>` **2026-09-30 已被实测证伪**（`.htx-text` 这个类名在标注页上根本选不中）；「可改」是设计决定而非能力限制，故不做；唯一例外是 `risk_hints` 不能换，它是预标注的唯一落点。见 `docs/observability-label-studio.md` §3.5 与设计文档 R16。
  - **qwenjina.txt 不上传（2026-09-30 起）**：ChatML 全文曾走两条路上 LS —— `task.data["qf_text"]`（qf_text_view 展示块）+ meta.json 内嵌 `qf_text` 留底（经 metadata/metadata_text 二次上传）。现展示块换成 `openai_view`（openai.json 的人读渲染 `render_openai_text`，非 JSON 孪生），推送前从 meta 副本剥掉视图载荷键 `openai_messages/tools/qf_text/qf_stats/qf_rendered_at`（task.data 只留审计元数据）。**C3 磁盘文件不变**（契约不动，只剥 LS 副本）；展示块仍用 TextArea 而非 `<Chat>`（官方文档：导入消息不可选，editable 只管标注员新增消息，与「可改审查工作区」冲突）。

## 输出

按阶段分目录（新架构，2026-09-22 起）：

- `output/runs|artifacts|reports` —— simulate_serve 自洽（run 元数据 / content-addressed 制品 / 聚合统计）
- `output/agent_trajectory/` —— C1 trajectory 事件流（simulate_serve → gdr 交接面）
- `output/refined/` —— C2 单 refined Session（gdr → etl 交接面）
- `output/refine_data/` —— C3 4 视图文件（etl → 训练 / audit）；旁路 jsonl（incomplete / judge_low / deferred / routing_low）也在此

审计保存所有 Run；非终态启动恢复时标记 `INTERRUPTED`，绝不自动重复远端任务。
`simulate_serve` 不再导出 `output/datasets/all_runs.v2.jsonl` / `distill_dataset.v2.jsonl`
（已被 C3 取代）。

## 数据保留原则

本项目的核心场景目标是**生成 agent 轨迹数据**,不仅用于 SFT 训练,还需为
任务调优、修改与质量问题分析提供输入。**结构合格但评分低**的轨迹与高质量
轨迹同等重要,不能因为评分低就丢入死信。

### 死信判定的边界

仅当数据**结构严重不可用**时才进死信（`output/orchestration/dead/`）:

1. 轨迹不完整 —— 尾部 toolcall 缺 toolresult / 配对缺失 / 末段截断
2. 仅有用户内容无 assistant 内容 —— 全程 agent 未产生任何回复
3. 没有明确的模型总结回复 —— 末尾 text 被启发式判截断,或末段仅 thinking
   无 final text

凡**结构合格 + 含 assistant 回复**的轨迹一律**不**进死信:

- 评分低（judge / free_quality 子分未达）但结构完整 → 走旁路审计
  `refine_data/judge_low.jsonl` / `audit/scoring_reject.jsonl`,供后期人工复核
  与质量问题归因
- 评审层弃权（LLM 投票解析失败）→ `refine_data/routing_abstain.jsonl`,仅
  丢单 block 不丢整 session

### 双轨归档

- **死信** = `output/orchestration/dead/<task_id>__<filename>`(src_path 物理
  move)
- **旁路** = `refine_data/{incomplete,judge_low,deferred,routing_low}.jsonl`
  / `audit/scoring_reject.jsonl`(完整 session dump 或仅 metadata)

死信与旁路互为兜底:所有原始 trajectory 在当前架构下均可经 `requeue_dead()`
复活,或经旁路 jsonl 直接读取完整 session。**无永久丢失路径**。改动丢弃
逻辑前必须先 grep `phase='dead'` / `_append_*_queue` / `GdrNonRetryableError`
/ `GdrAuditedError` 确认无遗漏。

**实施状态 (2026-09-24)**: 评分低 (judge_discard / scoring_reject) 已走
`PHASE_AUDITED` 终态 + `mark_audited` 队列方法, 不再进 dead。详见
[docs/设计方案/pipeline-contracts.md](docs/设计方案/pipeline-contracts.md) §2
phase 表 + `mark_audited` 定义。

**实施状态 (2026-09-29)**: simulate 端判据已从「验证是否通过」改为「**数据
结构是否可用**」。`guide_exhausted` / `inconclusive` (远端拒答、引导耗尽、
语义待定) 的轨迹**不再进死信**, 继续走 gdr → etl, 终态由 gdr 判定 (`audited`
旁路 / `done` C3 制品)。真正的死信只剩 `validation_error` / `executor_error` /
`actor_error` / `cancelled` / `interrupted` / `completion_incomplete` 六类。

「验证不通过」这个信号改由 `orchestration/fail_evaluator.py` 承载: etl 阶段把
**验证失败原因** + **agent 轨迹结果内容**发给 LLM 做一次定性归因, 写入 C3
meta.json 顶层 `fail_evaluation`, **分数恒为 0**(`score_source:
simulate_validation`, LLM 不参与打分)。Label Studio 评分卡 L0
`criterion_coverage` 在 `final_verdict != "pass"` 时记 0 分并透出归因。
**raw CoT 红线**: 该模块在调 LLM 前剥离 C1 text block 内嵌的 `<think>` 链
(含未闭合的情形)。设计见
[docs/设计方案/simulate-fail-scoring.md](docs/设计方案/simulate-fail-scoring.md)。

## 文档

- `docs/orchestration-design.md` — orchestration 三阶段流水线设计基线（2026-09-22 重写；§6.8 Label Studio 旁路推送）
- `docs/observability-label-studio.md` — Label Studio 推送的用户视角（评分卡为什么标 `source` / 哪类样本会被拒推）
- `docs/label-studio-playbook.md` — **改 label_config 的方法论**：三种信号可信度递增（`validate/` 绿灯**不算数**，只有标注页算数）、属性名会骗人对照表、探针法（一次问完 / 必须带对照组 / 机器能验的自己验）、动生产配置前的停手条件与重置顺序。踩坑细节在设计文档 R13–R17
- `docs/label-studio-annotation-export.md` — 标注**导出**怎么读：`JSON` vs `JSON_MIN` 本机实测差异、控件→下游字段映射、原值与修正稿分列、清洗点（⚠️ 逐条归属是自由文本不是结构化字段）
- `docs/todolist.md` — Label Studio 集成的**未完成项**（标注界面待验证项、存储规模、待清理的 probe 项目）；每项含「为什么没动 / 怎么验」
- `docs/refactor-development-progress.md` — gdr SFT 数据质量修复迭代日志（含 2026-09-19 六件套 F1/F2/F3-C + Fix A/B/C + F3-D/E）
- `docs/执行agent资料/`、`docs/任务合集/`、`docs/设计方案/` — 项目历史档案
- 框架与策略长文：`gdr/docs/gdr-context-understanding-and-policy.md`、`gdr/docs/gdr-module-functional-overview.md`、`gdr/docs/gdr-mvp-design.md`、`gdr/docs/incremental-state-tracking-plan.md`
