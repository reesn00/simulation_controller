# Agent 用户模拟端重构实施方案

> 文档状态：已实施，作为代码与验收基线  
> 方案版本：v2.0  
> 确认日期：2026-08-15  
> 当前阶段：Phase 0–6 代码、离线测试与文档已完成；真实远端/浏览器 smoke 按方案作为非阻断外部验证

---

## 1. 背景与目标

本项目用于模拟真实用户与远端执行 Agent 进行多轮交互：

1. 从 Persona、Scenario、Task 和运行 Config 中理解用户身份、背景、任务目标与约束。
2. 生成符合用户角色的首轮提示词，向远端执行 Agent 发起新会话。
3. 等待远端执行 Agent 返回结果。
4. 在模拟端检查结果是否满足任务目标和验收要求。
5. 若任务尚未完成，根据未满足项生成自然的用户追问，复用远端会话继续推进。
6. 达成目标、超过轮次、验证无法完成或执行异常后，保存完整运行结果。

本次重构的核心目标不是简单移动文件，而是形成职责清晰、可测试、可扩展的三层能力结构：

- **交互表达层**：理解当前语境，以 Persona 身份生成自然用户话术。
- **任务运行层**：整理任务信息，维护确定性状态机，编排远端会话和重试。
- **验证取证层**：执行规则校验、工具取证和语义裁决，给出可追踪的任务结论。

---

## 2. 已确认决策

### 2.1 已接受

- 重构为“交互表达层 + 任务运行层 + 验证取证层”。
- 本地模拟端拥有最终任务验收权。
- 远端 Validation Agent 可保留为可选的辅助判断，但不能在本地无法验证时直接判定成功。
- 工具默认只提供给验证取证层，不直接提供给 Persona 交互表达 Agent。
- 工具不可用时必须在启动日志中明确打印状态及原因。
- 某条必选验收标准依赖不可用工具时，该标准为 `INCONCLUSIVE`，任务不得判定成功。
- 主运行编排升级为异步模型，以兼容 CAMEL MCP Toolkit 和浏览器工具生命周期。
- 保留现有 YAML 和输出格式的一版兼容迁移能力。
- 不保存、导出自由文本思维链，改为结构化决策记录。
- 当前远端执行 Agent 未运行，开发验收以单元测试和离线联调功能用例为主。

### 2.2 明确否决/暂缓

- **暂不移除配置文件中的明文模型凭据。**
- 初期不建设复杂的 Secret 管理、自动脱敏和凭据轮换能力。
- 实施期间不得在新增文档、测试快照和错误消息中复制或额外扩散实际凭据内容。

### 2.3 默认策略

- 可选工具不可用：记录警告，系统继续启动。
- 必需工具不可用：启动检查失败，不进入任务执行。
- 验证异常：返回 `ERROR` 或 `INCONCLUSIVE`，不得 fail-open。
- 远端执行异常：终止当前任务并记录 `EXECUTOR_ERROR`，不影响后续任务运行。
- 浏览器工具默认只读，不执行下载、上传、登录、支付或具有外部副作用的操作。

---

## 3. 当前实现与主要问题

### 3.1 当前流程

```text
TaskManager
  -> UserSimulator.generate_first_question
  -> AgentClient.send(new session)
  -> rule_check
  -> remote validation agent
  -> UserSimulator.generate_guidance
  -> AgentClient.send(existing session)
  -> DataSink
```

### 3.2 当前实现中可复用的部分

- Pydantic 配置与任务模型。
- Persona、Scenario、Task 的基础概念。
- 远端执行 Agent 的新建会话/复用会话语义。
- CAMEL `ChatAgent` 和模型工厂。
- JSON/JSONL 运行结果输出。
- 规则预检和语义检查的基本思路。

### 3.3 必须在重构中修复的问题

1. `SessionRunner` 同时负责提示词、会话、状态、校验、重试、异常和持久化，职责过重。
2. 验证调用失败或返回无法解析时会被视为成功。
3. `keywords` 没有本地校验；`list/table/card/text` 格式没有完整实现。
4. `min_length` 当前按字符数解释，但任务配置实际常用于表达最少结果条数。
5. Task YAML 的 `dimension`、`explain` 没有进入 Task 模型，加载时被静默忽略。
6. 当前任务状态只保存在内存字典中，没有合法迁移约束和逐轮事件记录。
7. 保存的 `validation_detail` 不能反映各规则、各轮次和工具证据。
8. 模拟端与远端回复可能包含自由文本推理过程，污染蒸馏数据。
9. 轮询请求没有可靠继承本次提交使用的 Agent ID。
10. HTTP Client 重复创建且未形成统一生命周期。
11. 批量任务中单个未归一化异常可能中断整批执行。
12. 安装后的 CLI 入口与当前空 `simulate_serve/__init__.py` 不匹配。
13. 当前没有自动化测试体系。

---

## 4. 目标架构

```mermaid
flowchart TD
    CLI[CLI / Bootstrap] --> TC[Task Compiler]
    TC --> RT[Task Runtime / State Machine]
    RT --> IA[Interaction Layer]
    RT --> EG[Executor Gateway]
    EG --> RE[Remote Execution Agent]
    RT --> VP[Validation Pipeline]
    VP --> DV[Deterministic Validators]
    VP --> EC[Evidence Collector]
    EC --> TR[Tool Registry]
    TR --> PW[Playwright Adapter]
    TR --> CF[Camoufox Adapter]
    TR --> MCP[Generic MCP Adapter]
    VP --> SJ[Semantic Judge]
    VP --> RT
    RT --> RR[Run Repository]
```

### 4.1 边界原则

- LLM 负责语言理解、自然表达和需要语义推断的裁决。
- 普通 Python 代码负责状态机、重试、超时、聚合规则和工具生命周期。
- 工具负责提供外部事实证据，LLM 不得编造工具结果。
- 明确的工具失败结果优先于语义 Judge 的主观判断。
- 领域层不依赖 QwenPaw、httpx、Playwright、Camoufox 或具体存储实现。
- 三层之间通过结构化对象通信，不传递自由文本思维链。

---

## 5. 目标目录结构

```text
simulate_serve/
├── __init__.py
├── __main__.py
├── bootstrap.py
├── configuration/          # Python 配置加载与 Catalog Schema
│   ├── catalog_loader.py
│   ├── catalog_schema.py
│   └── diagnostics.py
├── config/                 # 仅保留内置 YAML 数据
│   ├── config.yaml
│   ├── scenarios.yaml
│   └── tasks.yaml
├── domain/
│   ├── persona.py
│   ├── task.py
│   ├── run.py
│   ├── validation.py
│   ├── evidence.py
│   └── state_machine.py
├── application/
│   ├── run_batch.py
│   ├── run_task.py
│   ├── task_compiler.py
│   └── ports.py
├── interaction/
│   ├── actor.py
│   ├── prompt_builder.py
│   ├── guidance_policy.py
│   └── response_parser.py
├── validation/
│   ├── pipeline.py
│   ├── aggregation.py
│   ├── deterministic/
│   │   ├── keyword.py
│   │   ├── format.py
│   │   ├── fields.py
│   │   ├── constraints.py
│   │   └── count.py
│   ├── evidence_collector.py
│   └── semantic_judge.py
├── tools/
│   ├── registry.py
│   ├── descriptor.py
│   ├── health.py
│   ├── camel_adapter.py
│   └── browser/
│       ├── protocol.py
│       ├── playwright.py
│       └── camoufox.py
└── infrastructure/
    ├── qwenpaw_client.py
    ├── camel_model_factory.py
    └── json_run_repository.py
tests/                        # 仓库级测试，不打包进 wheel
├── unit/
├── contract/
├── integration/
├── functional/
└── fixtures/
```

目录只按稳定能力边界拆分，不为每个类创建独立文件。迁移期允许旧模块保留为兼容入口，完成切换后再删除。

---

## 6. 领域模型设计

### 6.1 TaskDocument

任务配置的 Raw Document 模型忠实保留“未编译”语义。`schema_version` 属于 Catalog 文件级 envelope，不在每个 Task 重复；可继承字段使用 Optional 区分“未配置”和“显式清空”：

```python
class TaskDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_type: str
    dimension: str | None = None
    explain: str | None = None
    scenario: str | None = None
    task_prompt: str
    user_persona: PersonaOverride | None = None
    acceptance_policy: AcceptancePolicyDocument | None = None
    acceptance_criteria: list[CriterionDocument] | None = None
    constraints: list[str] | None = None
    excluded_platforms: list[str] | None = None
    validation_rules: LegacyValidationRulesDocument
    expected_reference: str | None = None
```

### 6.2 AcceptanceCriterion

每条验收标准必须有稳定 ID，并声明如何验证：

```python
class AcceptanceCriterion(BaseModel):
    criterion_id: str
    description: str
    required: bool = True
    validator: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: set[str] = Field(default_factory=set)
```

示例：

```yaml
acceptance_criteria:
  - criterion_id: playable_url
    description: 至少存在一个可访问、可播放的完整 URL
    required: true
    validator: browser.playable_url
    parameters:
      min_items: 1
    required_capabilities:
      - browser.navigate
      - browser.inspect_page
```

### 6.3 CompiledTask

`TaskCompiler` 合并 Scenario、Persona 默认值和 Task 覆盖项，生成只读的 `CompiledTask`。

必须同时记录配置来源：

```text
persona.role_description <- scenario:video_search
persona.tone             <- task:T001
max_guide_rounds         <- app_config
```

这样可以避免目前通过“字段值看起来是否为默认值”推断继承关系。

### 6.4 ValidationResult

```python
class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"

class CriterionResult(BaseModel):
    criterion_id: str
    verdict: Verdict
    reason_code: str
    message: str
    evidence_ids: list[str] = Field(default_factory=list)

class ValidationReport(BaseModel):
    verdict: Verdict
    criteria: list[CriterionResult]
    missing_items: list[str]
    retryable: bool
```

### 6.5 Evidence

```python
class Evidence(BaseModel):
    evidence_id: str
    source: str
    tool_name: str
    capability: str
    status: str
    summary: str
    artifact_ref: str | None = None
    started_at: datetime
    duration_ms: int
```

工具参数只保存经过筛选的审计信息；截图、页面快照等大对象保存为 artifact，记录引用而不是塞入会话文本。

---

## 7. 交互表达层

### 7.1 职责

- 读取 `CompiledTask` 中的 Persona、目标、背景和当前任务状态。
- 生成首轮用户话术。
- 将结构化验证缺口转换为自然追问。
- 保持 Persona 语气，不暴露“验证器、验收标准、测试”等内部概念。
- 不直接决定任务成功或失败。

### 7.2 接口

```python
class InteractionActor(Protocol):
    async def create_opening(self, context: InteractionContext) -> UserUtterance: ...

    async def create_followup(
        self,
        context: InteractionContext,
        report: ValidationReport,
    ) -> UserUtterance: ...
```

`UserUtterance` 只包含用户对外文本和结构化决策元数据：

```python
class UserUtterance(BaseModel):
    content: str
    action: str
    reason_codes: list[str]
    target_criteria: list[str]
```

不再要求模型输出 `<internal_thought>`。

### 7.3 CAMEL 使用方式

- 使用独立的 `ChatAgent` 作为 Persona Actor。
- 每轮输入显式包含裁剪后的对话历史和 ValidationReport。
- 优先使用结构化响应格式，解析失败时返回表达层错误，不将原始内容当作合法话术。
- Persona Actor 默认不注册浏览器或文件操作工具。

---

## 8. 任务运行层与状态机

### 8.1 状态定义

```python
class RunState(str, Enum):
    PENDING = "pending"
    PREPARING = "preparing"
    GENERATING_OPENING = "generating_opening"
    WAITING_EXECUTOR = "waiting_executor"
    VALIDATING = "validating"
    GENERATING_FOLLOWUP = "generating_followup"
    SUCCESS = "success"
    GUIDE_EXHAUSTED = "guide_exhausted"
    INCONCLUSIVE = "inconclusive"
    VALIDATION_ERROR = "validation_error"
    EXECUTOR_ERROR = "executor_error"
    ACTOR_ERROR = "actor_error"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
```

### 8.2 主要迁移

```text
PENDING -> PREPARING
PREPARING -> GENERATING_OPENING
GENERATING_OPENING -> WAITING_EXECUTOR
WAITING_EXECUTOR -> VALIDATING
VALIDATING + PASS -> SUCCESS
VALIDATING + FAIL/retryable -> GENERATING_FOLLOWUP
GENERATING_FOLLOWUP -> WAITING_EXECUTOR
VALIDATING + FAIL/no rounds -> GUIDE_EXHAUSTED
VALIDATING + INCONCLUSIVE -> INCONCLUSIVE
VALIDATING + ERROR -> VALIDATION_ERROR
WAITING_EXECUTOR + transport/task error -> EXECUTOR_ERROR
表达层生成/解析失败 -> ACTOR_ERROR
任何非终态 + cancellation -> CANCELLED
启动恢复扫描发现非终态持久化记录 -> INTERRUPTED
```

禁止未声明的状态跳转。

### 8.3 运行循环伪代码

```python
async def run_task(task: CompiledTask) -> TaskRun:
    run = state_machine.start(task)
    actor = actor_factory.create(task)

    opening = await actor.create_opening(run.interaction_context())
    response = await executor.open_session(opening.content)

    while True:
        report = await validator.validate(task, run, response)

        if report.verdict is PASS:
            return run.succeed(report)

        if report.verdict in {INCONCLUSIVE, ERROR}:
            return run.stop_from_validation(report)

        if not report.retryable or run.guide_rounds >= task.max_guide_rounds:
            return run.exhaust(report)

        followup = await actor.create_followup(run.interaction_context(), report)
        response = await executor.continue_session(run.remote_session_id, followup.content)
```

### 8.4 异常隔离

- `run_batch` 捕获每个 Task 的终态异常，保存记录后继续下一任务。
- `run_task` 只处理单任务编排。
- Executor、Validator、Tool 和 Repository 异常必须转换为领域错误对象。
- 不使用裸 `except Exception` 直接判成功或吞掉原因。

---

## 9. 验证取证层

### 9.1 验证流水线

```text
RemoteAgentResponse
  -> Response Contract Check
  -> Deterministic Validators
  -> Claim Extraction
  -> Tool Evidence Collection
  -> Semantic Judge
  -> Criterion Aggregation
  -> ValidationReport
```

### 9.2 确定性校验器

首批实现：

- `KeywordValidator`：必含、任一、禁含，支持大小写和规范化。
- `FormatValidator`：JSON、列表、Markdown 表格、文本、卡片。
- `FieldValidator`：对 JSON/表格结构检查真实字段。
- `ItemCountValidator`：检查列表项或表格数据行，而不是字符长度。
- `ConstraintValidator`：检查 Must-not 约束和排除平台。
- `UrlSyntaxValidator`：只检查 URL 结构，不宣称可访问。

兼容现有 `min_length` 时：

- Schema v1 保持字符长度语义并给出弃用警告。
- 新任务使用 `min_chars` 或 `min_items`，不得混用。

### 9.3 工具取证

工具取证用于验证外部事实，例如：

- URL 是否能访问。
- 是否发生登录、会员或付费拦截。
- 页面标题是否与目标内容匹配。
- 页面是否存在视频元素或播放控件。
- 页面宣称的集数、画质、字幕等信息。

工具返回的是证据，不直接输出总体任务 verdict。

### 9.4 Semantic Judge

Judge 输入：

- Task 目标和约束。
- Acceptance Criteria。
- 远端 Agent 的最终回复文本。
- 已完成的确定性校验结果。
- 工具证据摘要。

Judge 输出必须符合 `ValidationReport`/中间 Pydantic Schema。

Judge 不得：

- 把工具无法访问解释为“应该可用”。
- 在没有证据时断言链接已验证。
- 输出自由格式成功文本。
- 因解析错误触发默认通过。

### 9.5 聚合规则

```text
任一 required criterion == FAIL         => FAIL
任一 required criterion == ERROR        => ERROR
任一 required criterion == INCONCLUSIVE => INCONCLUSIVE
全部 required criterion == PASS         => PASS
```

可选标准只进入质量评分，不阻止成功。

---

## 10. 工具系统

### 10.1 CAMEL 集成基线

当前 `camel-ai 0.2.90` 已提供：

- `ChatAgent(tools=...)`
- `FunctionTool`
- `BaseToolkit`
- `MCPToolkit`
- `PlaywrightMCPToolkit`

采用方式：

- 自定义 Python 工具使用 `BaseToolkit`/`FunctionTool`。
- MCP 工具通过 `MCPToolkit` 管理连接与工具发现。
- Verification Agent 只接收 Task 所需 capability 对应的工具，不加载全部工具。

参考：

- <https://docs.camel-ai.org/key_modules/tools>
- <https://docs.camel-ai.org/reference/camel.toolkits.base>
- <https://docs.camel-ai.org/reference/camel.toolkits.mcp_toolkit>

### 10.2 ToolDescriptor

```python
class ToolDescriptor(BaseModel):
    name: str
    provider: str
    enabled: bool = True
    required: bool = False
    capabilities: set[str]
    allowed_task_types: set[str] = Field(default_factory=set)
    startup_timeout_seconds: float = 15
    call_timeout_seconds: float = 30
    config: dict[str, Any] = Field(default_factory=dict)
```

### 10.3 健康状态

```python
class ToolStatus(str, Enum):
    DISABLED = "disabled"
    READY = "ready"
    DEPENDENCY_MISSING = "dependency_missing"
    INIT_FAILED = "init_failed"
    CONNECT_FAILED = "connect_failed"
    SCHEMA_INVALID = "schema_invalid"
    PROBE_FAILED = "probe_failed"
    SHUTDOWN_FAILED = "shutdown_failed"
```

启动检查顺序：

1. 检查 Python 模块、外部命令和浏览器二进制。
2. 构建 Toolkit/Adapter。
3. 对 MCP 工具执行 connect。
4. list tools，并验证工具 Schema 和重名。
5. 执行无外部副作用的可选 smoke probe。
6. 生成 `ToolReadinessReport`。

日志示例：

```text
Tool readiness
  playwright  READY               tools=18 duration=1.8s
  camoufox    DEPENDENCY_MISSING  required=false reason=python package unavailable
  search      DISABLED
```

### 10.4 工具配置草案

```yaml
tools:
  startup_policy: warn
  providers:
    - name: playwright
      type: playwright_mcp
      enabled: true
      required: false
      priority: 100
      capabilities:
        - browser.navigate
        - browser.inspect_page
        - browser.snapshot
      startup_timeout_seconds: 20

    - name: camoufox
      type: camoufox
      enabled: false
      required: false
      priority: 50
      capabilities:
        - browser.navigate
        - browser.inspect_page
```

不直接使用 `PlaywrightMCPToolkit` 内置的 `@latest` 作为生产固定配置，改用通用 MCP 配置锁定实际版本。YAML 只能引用已注册 `provider_type`，不接受任意 Python dotted import。

### 10.5 Playwright Adapter

- 使用 MCP 异步 connect/disconnect 生命周期。
- Task 只依赖 capability，不依赖 MCP 具体工具名。
- 将 MCP 返回值转换为统一 `Evidence`。
- 每个运行使用独立浏览器 Context，任务结束后关闭。

### 10.6 Camoufox Adapter

- 基于 Camoufox 的 Playwright 兼容 Python API。
- 自定义继承 CAMEL `BaseToolkit`。
- 对上提供与 Playwright 相同的核心 capability。
- 浏览器差异封装在 Adapter 内部，不进入 Validator 和 State Machine。

### 10.7 浏览器安全边界

初期即使不建设复杂保密功能，浏览器工具仍需具备运行安全边界：

- 默认拒绝 localhost、链路本地地址和内网网段。
- 禁止下载和上传。
- 禁止登录、支付、发帖等外部写操作。
- 限制页面数量、重定向次数、单次结果大小和执行时间。
- Task 间隔离 Cookie、缓存和浏览器状态。
- 所有 Context 在 finally 阶段关闭。

---

## 11. 远端执行 Agent 接口

### 11.1 Port

```python
class ExecutorGateway(Protocol):
    async def open_session(self, message: str) -> ExecutorResponse: ...

    async def continue_session(
        self,
        session_id: str,
        message: str,
    ) -> ExecutorResponse: ...
```

```python
class ExecutorResponse(BaseModel):
    text: str
    session_id: str
    remote_task_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 11.2 HTTP 实现要求

- 使用一个有生命周期的 `httpx.AsyncClient`。
- submit 和 poll 使用相同的 effective agent ID。
- 只对可重试状态和网络异常重试。
- 使用带抖动的指数退避。
- submit、poll、整体 Task 分别设置超时。
- 所有异常转换为 `ExecutorError`，携带阶段和可重试属性。
- 单个 Task 错误不得中断 Batch。

### 11.3 当前离线约束

当前远端执行 Agent 未启动，因此本阶段不将真实 HTTP 成功作为验收前置条件。真实联调在远端恢复后单独执行，不阻塞本地重构。

---

## 12. 持久化与输出

### 12.1 TaskRun 输出

```json
{
  "run_id": "...",
  "task_id": "T001",
  "state": "success",
  "remote_session_id": "...",
  "guide_rounds": 2,
  "conversation": [],
  "state_events": [],
  "validation_rounds": [],
  "evidence": [],
  "failure": null
}
```

### 12.2 数据规则

- `guide_rounds` 表示实际发送成功的追问数量。
- 每轮 ValidationReport 独立保存。
- 保存 CriterionResult 与关联 Evidence ID。
- 不保存 Persona Actor 或 Judge 的自由文本思维链。
- 远端响应只保存对用户可见的最终文本和白名单元数据；包含自由文本思维链、Cookie、Authorization 或内部 Header 的原始 envelope 不得落盘。
- 蒸馏数据只提取经过清洗的 user/assistant 对话。

### 12.3 兼容策略

- 保留现有 `SessionRecord` 导出适配器一个迁移周期。
- 新格式使用 `schema_version`。
- 旧消费者通过 legacy exporter 获取原字段。
- 新统计区分 `failed`、`inconclusive`、`executor_error` 和 `validation_error`。

---

## 13. 测试与离线联调方案

由于当前远端执行 Agent 未运行，测试分为五层。

### 13.1 单元测试

必须覆盖：

- Persona/Scenario/Task 合并和字段来源。
- 未知 Task 字段拒绝。
- 每种确定性 Validator。
- Verdict 聚合规则。
- 所有状态迁移和非法迁移。
- 最大引导轮次，无 off-by-one。
- Validation ERROR/INCONCLUSIVE 不得成功。
- GuidancePolicy 根据缺失项生成正确目标。
- ToolRegistry 的注册、筛选、重复名和状态汇总。
- ToolHealthChecker 的每种错误状态。

### 13.2 Executor 合约测试

使用 `httpx.MockTransport` 或 Fake Transport 模拟：

- 新建会话成功。
- 复用会话成功。
- submit 在确认未发出时可重试；结果不明且无幂等支持时不重试。
- poll 超时。
- 远端 Task failed/cancelled。
- 返回空文本或异常结构。
- Validation Agent 和 Execution Agent 使用各自正确的 Header。

不需要真实远端服务。

### 13.3 工具合约测试

使用 `FakeToolkit`/`FakeBrowserDriver` 验证：

- 工具 READY 并返回证据。
- 可选工具缺失时系统继续启动。
- 必需工具缺失时启动失败。
- 工具执行超时产生 `ERROR`。
- Criterion 依赖工具缺失时产生 `INCONCLUSIVE`。
- 工具明确发现付费墙时 Criterion 为 `FAIL`。

### 13.4 离线功能用例

实现一个 `ScriptedExecutor`，按脚本返回多轮内容：

```yaml
steps:
  - response: "我找到了几个平台，但没有给链接。"
  - response: "这里有链接，但需要会员。"
  - response: "这里是满足要求并有证据的最终结果。"
```

功能用例覆盖：

1. 首轮成功。
2. 首轮失败，第二轮成功。
3. 多轮失败后耗尽。
4. 验证工具不可用。
5. Executor 中途取消。
6. 远端返回推理内容，最终文本提取失败。
7. 异常任务：资源不存在时如实说明即成功。
8. 合规任务：拒绝非法来源并提供合法替代方案。

### 13.5 可选真实工具 Smoke Test

仅在本机依赖可用时运行，使用 pytest marker，例如：

```text
pytest -m "browser_smoke"
```

- Playwright：打开本地静态测试页，检查标题、列表、视频占位元素。
- Camoufox：打开相同本地页面，验证统一 Browser Protocol。
- 默认 CI 和普通单元测试不依赖下载浏览器或公网访问。

### 13.6 远端恢复后的真实联调

远端 Agent 恢复后再执行：

1. `/api/console/chat/task` 基础连通。
2. Agent ID 与 session ID 隔离。
3. 多轮会话复用。
4. cancel/timeout/error 返回。
5. 选取 3 至 5 个代表 Task 做端到端运行。

真实联调结果补充到独立测试记录，不改变已经通过的本地验收定义。

---

## 14. 分阶段实施计划

### Phase 0：测试基线与入口修复

目标：在不改变核心业务行为的前提下建立离线可运行、可重复、能暴露已知缺陷的回归基线，并修复 CLI/打包入口。

#### Phase 0 已确认边界

Phase 0 只允许三类生产侧修改：

1. 测试基础设施和离线测试替身。
2. CLI entry point 与包资源路径修复。
3. 为测试提供的最小接缝，但不得改变任务、校验和会话的业务结果。

本阶段明确不实施：

- Task/Scenario 新领域模型。
- `dimension`、`explain` 字段修复。
- 任务状态机和三层目录迁移。
- AgentClient 异步化和正式依赖注入接口。
- 验证 fail-open 修复。
- `keywords/list/table/card` 等新验证语义。
- ToolRegistry、Playwright、Camoufox。
- 自由文本思维链输出结构调整。
- 明文凭据及当前凭据日志方式调整。
- 旧模块删除。

#### Phase 0 重要决策

| 决策点 | 已选方案 | 原因 |
|---|---|---|
| 业务缺陷处理 | 记录并延后，不在 Phase 0 顺手修复 | 防止测试基线与业务重写混杂 |
| 已知错误行为 | 使用 `xfail(strict=True)` 或已知缺陷清单 | 不把错误行为固化为正确契约，修复后 XPASS 会提醒转为回归测试 |
| CLI 修复深度 | 最小修改为 `simulate_serve.__main__:main` | 先恢复安装命令，CLI/Bootstrap 正式分层留到 Phase 2 |
| AgentClient 测试 | `httpx.MockTransport` + monkeypatch | 不为即将在 Phase 2 异步重写的旧 Client 提前引入较大结构 |
| Phase 0 测试替身 | `ScriptedAgentClient`、`StubUserSimulator`、`FakeClock` | 与当前接口直接对应，可立即覆盖现有 SessionRunner |
| Fake Executor | Phase 2 定义 Executor Port 后建立 | 避免提前猜测接口 |
| Fake Judge | Phase 3 定义 Judge 接口后建立 | 避免形成无生产消费者的测试对象 |
| Fake Toolkit/Browser | Phase 4/5 定义工具协议后建立 | 工具 capability 和生命周期尚未确定 |
| 异步测试依赖 | Phase 0 不添加 `pytest-asyncio` | 当前生产代码为同步，异步化时再引入 |
| 覆盖率门槛 | Phase 0 记录覆盖率，不设置硬百分比 | 先获得真实基线，避免为 Prompt 常量追逐数字 |
| 网络访问 | pytest 默认禁止 socket | 保证远端 Agent、模型和公网不可用时仍可验收 |
| Python 版本 | 保留 `>=3.10` 声明，Phase 0 报告只确认本机 3.13 | 当前没有 3.10-3.12 测试环境 |
| 明文配置 | 保持现状 | 遵循已确认的初期便捷使用决策 |
| `config copy.yaml` | Phase 0–5 不删除、始终不打包；Phase 6 确认无引用后删除 | 保留基线可追溯性，在迁移收口时清理重复配置 |

#### Phase 0.1：测试工具链

在 `pyproject.toml` 中使用 uv 开发依赖组，不把测试库加入生产依赖：

```toml
[dependency-groups]
dev = [
    "pytest",
    "pytest-cov",
    "pytest-socket",
]
```

Phase 0 暂不加入 `pytest-asyncio`。

pytest 配置基线：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers --disable-socket"
markers = [
    "characterization: 当前行为特征测试",
    "functional: 离线功能流程测试",
    "known_gap: 已确认但留待后续 Phase 修复的问题",
    "package: 包构建和安装测试",
]
```

约束：

- 普通测试禁止真实 socket。
- HTTP 合约测试只使用 `httpx.MockTransport`。
- 所有文件输出使用 pytest `tmp_path`。
- 测试不得覆盖当前 `output/` 内容。
- 测试不得调用本地模型、QwenPaw、Playwright、Camoufox 或公网。

#### Phase 0.2：CLI 与打包修复

将项目命令入口改为：

```toml
[project.scripts]
useramulation = "simulate_serve.__main__:main"
```

Phase 0 不新增完整 `cli.py`，保持修改最小。CLI 正式拆分到 Bootstrap/CLI 模块可随 Phase 2 进行。

修复 Hatch wheel 内置配置路径：

```toml
[tool.hatch.build.targets.wheel.force-include]
"simulate_serve/config/config.yaml" = "simulate_serve/config/config.yaml"
"simulate_serve/config/tasks.yaml" = "simulate_serve/config/tasks.yaml"
"simulate_serve/config/scenarios.yaml" = "simulate_serve/config/scenarios.yaml"
```

CLI/包测试必须验证：

- `python -m simulate_serve --help` 退出码为 0。
- 安装后的 `useramulation --help` 退出码为 0。
- `--help` 不初始化 SessionRunner、不创建日志文件、不连接网络。
- Wheel 中存在三个正式 YAML。
- Wheel 中不包含 `config copy.yaml`。
- 临时环境安装 Wheel 后默认配置可定位。
- `run.bat` 现有调用方式不受影响。

#### Phase 0.3：配置与 TaskManager 特征测试

建议文件：

```text
tests/characterization/test_config.py
tests/characterization/test_task_manager.py
tests/fixtures/tasks_minimal.yaml
tests/fixtures/scenarios_minimal.yaml
```

覆盖：

- 配置文件不存在时返回默认 `AppConfig`。
- 指定绝对配置文件正常加载。
- Tasks 文件不存在时报错。
- Scenario Persona 被 Task 继承。
- Task Persona 优先于 Scenario。
- Acceptance Criteria 合并并按 item 去重。
- Constraints 和 Excluded Platforms 保持顺序并去重。
- 找不到 Scenario 时记录 warning，但 Task 仍可加载。
- Task 初始状态为 `PENDING`。
- `next_pending()`、`mark()` 和 `summary()` 当前行为。

行为测试使用最小 fixture，不依赖正式配置中的具体模型地址和凭据值。可增加一次默认配置读取 smoke test，但不得快照或输出实际凭据。

#### Phase 0.4：规则校验特征测试

建议文件：

```text
tests/characterization/test_validator.py
```

普通测试固定仍需保留的当前行为：

- 空规则通过。
- 字符数低于 `min_length` 失败。
- 等于边界时通过。
- 合法 JSON 通过，非法 JSON 失败。
- `required_fields` 当前大小写不敏感。
- 多个失败原因顺序稳定。
- `ValidationResult` 字段组合一致。

已知缺陷：

| ID | 缺陷 | 处理阶段 |
|---|---|---|
| KG-001 | `keywords` 没有本地校验 | Phase 3 |
| KG-002 | `list/table/card/text` 格式没有完整实现 | Phase 3 |
| KG-003 | `required_fields` 只做字符串包含，不检查真实结构 | Phase 3 |

适合直接表达期望结果的缺陷使用严格 xfail：

```python
@pytest.mark.known_gap
@pytest.mark.xfail(strict=True, reason="Phase 3: implement keyword validator")
def test_missing_required_keyword_fails():
    ...
```

#### Phase 0.5：AgentClient 合约测试

建议文件：

```text
tests/contract/test_agent_client.py
```

使用 `httpx.MockTransport` 或返回预设 Response 的测试 Client，覆盖：

- 新建会话时 body 不含 `session_id`。
- 复用会话时 body 包含原 session ID。
- `user_id`、`timeout` 和输入消息结构正确。
- Execution/Validation Agent ID 正确写入 submit Header。
- Bearer Token 配置时正确携带。
- Submit 结果缺少 task ID 时抛出 `AgentHTTPError`。
- 重试次数为 `max_retries + 1`。
- Poll 的 `running -> finished` 流程。
- 远端结果为 `failed/cancelled`。
- Poll 超时。
- 返回 session ID 的优先级。
- 顶层 text、嵌套 message/content、字符串 output 的解析。
- `<think>...</think>` 清理。
- 空回复 warning。

测试中 monkeypatch `time.sleep` 和时钟，禁止真实等待。

已知缺陷：

| ID | 缺陷 | Phase 0 处理 | 正式修复阶段 |
|---|---|---|---|
| KG-004 | Validation Agent submit 后，poll 可能回退为 Execution Agent Header | 严格 xfail | Phase 2 |
| KG-005 | Poll 中部分 httpx 异常未统一为 `AgentHTTPError` | 记录缺陷 | Phase 2 |
| KG-006 | 每次请求新建 Client，缺少统一生命周期 | 记录缺陷 | Phase 2 |

Phase 0 不为 KG-005/KG-006 引入旧版同步 Client 的正式依赖注入架构；Phase 2 使用 `httpx.AsyncClient` 一次解决。

#### Phase 0.6：UserSimulator 特征测试

建议文件：

```text
tests/characterization/test_user_simulator.py
```

通过 monkeypatch `_build_model` 或 `ChatAgent.step`，禁止调用真实模型。覆盖：

- CAMEL 初始化失败时首轮使用 fallback。
- 引导轮次选择对应 fallback guidance。
- 自定义 Persona 优先。
- 默认 Persona 按 index 轮换。
- Scenario 合并后的 Interaction Protocol 进入 system prompt。
- `<internal_thought>` 和 `<think>` 当前解析行为。
- 模型返回空内容时使用 fallback。

这里只固定当前输入输出兼容行为，不认可长期保存自由文本思维链；相关数据调整仍在 Phase 6。

#### Phase 0.7：SessionRunner 离线功能测试

建议文件：

```text
tests/functional/test_session_runner.py
tests/doubles.py
```

Phase 0 测试替身：

- `ScriptedAgentClient`：按 agent ID、session ID 和调用次序返回预设结果或异常。
- `StubUserSimulator`：返回确定的首轮话术和引导话术。
- `FakeClock`：控制 poll 时间与 sleep。
- `DataSink + tmp_path`：测试真实输出逻辑，不写正式目录。

最低功能用例：

1. 首轮规则和语义均通过，任务成功。
2. 首轮失败，发送一次引导后成功。
3. 多轮失败，最终 `GUIDE_FAILED`。
4. 首次执行 API 错误，保存 `API_ERROR`。
5. 引导阶段 API 错误，回退末尾未回答的 user turn。
6. 验证结果包含 Markdown JSON 代码块。
7. 验证结果包含 `<think>` 后仍能提取 JSON。
8. 批量运行中单任务出现已归一化错误后继续下一任务。
9. 输出统计与实际任务状态一致。

已知缺陷：

| ID | 缺陷 | 正式修复阶段 |
|---|---|---|
| KG-007 | Validation Agent 调用失败被视为通过 | Phase 3 |
| KG-008 | Validation JSON 无法解析被视为通过 | Phase 3 |
| KG-009 | 引导耗尽时 `guide_count` 可能记录为 `max + 1` | Phase 2 |
| KG-010 | `validation_detail` 只是复制总体 success | Phase 3/6 |

KG-007、KG-008、KG-009 使用严格 xfail；KG-010 先记录现状，等待新版 ValidationReport 和输出模型确定后补正式断言。

#### Phase 0.8：DataSink 测试

建议文件：

```text
tests/characterization/test_data_sink.py
```

覆盖：

- 单 Session JSON 可解析。
- JSONL 每行可独立解析。
- 中文内容不转义。
- success/failed 数量。
- success rate。
- average guide rounds。
- 空数据集不除零。
- 所有文件只写入 `tmp_path`。

#### Phase 0.9：测试目录与用例规模

```text
tests/
├── conftest.py
├── doubles.py
├── fixtures/
│   ├── tasks_minimal.yaml
│   ├── scenarios_minimal.yaml
│   └── agent_responses.py
├── characterization/
│   ├── test_config.py
│   ├── test_task_manager.py
│   ├── test_validator.py
│   ├── test_user_simulator.py
│   └── test_data_sink.py
├── contract/
│   ├── test_agent_client.py
│   └── test_package.py
└── functional/
    └── test_session_runner.py
```

预计 45 至 55 个测试，其中约 5 至 8 个为严格 xfail。用例数量不是验收目标，以关键分支和失败路径覆盖为准。

#### Phase 0.10：基线报告

Phase 0 完成时新增：

```text
docs/phase0-test-baseline.md
```

记录：

- 实际运行命令。
- passed、xfail、skipped 数量。
- 当前覆盖率，不设置硬性 fail-under。
- KG-001 至 KG-010 状态和后续 Phase。
- 未连接远端 Agent 的说明。
- 本机 Python、uv、camel-ai 版本。
- Wheel 构建、内容检查和临时安装结果。

#### Phase 0 预期变更文件

```text
pyproject.toml
uv.lock
tests/conftest.py
tests/doubles.py
tests/fixtures/*
tests/characterization/*
tests/contract/*
tests/functional/*
docs/phase0-test-baseline.md
```

`simulate_serve` 业务模块原则上不修改；如 CLI `--help` 测试证明必须移动导入副作用，应先记录原因，并只做不改变运行语义的最小调整。

#### Phase 0 验收命令

```powershell
uv sync --group dev
uv run pytest
uv run pytest --cov=simulate_serve --cov-report=term-missing
uv run python -m simulate_serve --help
uv run useramulation --help
uv build
```

包安装验证在临时虚拟环境中执行，不复用开发环境的 editable install。

#### Phase 0 完成标准

- 所有非 xfail 测试通过。
- pytest 默认禁止 socket，测试期间没有远端 Agent、模型或公网请求。
- 所有测试输出进入临时目录，现有 `output/` 不发生变化。
- `python -m simulate_serve --help` 和安装后的 `useramulation --help` 均成功。
- Wheel 包含 `config.yaml`、`tasks.yaml`、`scenarios.yaml`，不包含 `config copy.yaml`。
- 至少覆盖首轮成功、引导后成功、引导耗尽和 API 错误四类离线流程。
- 已知错误行为没有被写成普通正确契约。
- `run.bat` 现有使用方式不受影响。
- 明文配置方式保持不变。
- 形成 `phase0-test-baseline.md`，列出测试结果和已知缺陷。
- 除 CLI/打包外，不改变生产业务行为。

### Phase 1：领域模型与 TaskCompiler

目标：建立 Raw Document 与 CompiledTask 双模型，使 58 个现有任务都能经过严格校验、确定性合并和来源追踪后进入运行层，同时保留一阶段旧 SessionRunner 兼容。

#### Phase 1 已确认边界

本阶段实现：

- Raw Task/Scenario Catalog Schema。
- `CompiledTask`、Persona、Criterion、ValidationPolicy 和 Provenance 领域模型。
- 严格 YAML 加载、重复 ID 检查和错误聚合。
- Persona、Criteria、Constraints、Excluded Platforms 和交互策略的确定性合并。
- Legacy Validation Rules 到兼容 Criteria 的编译。
- 旧 YAML 顶层列表兼容。
- `LegacyTaskAdapter`，保持当前 SessionRunner 可运行。

本阶段不实现状态机、异步运行、Validator 执行、工具系统和新持久化。

#### Phase 1 数据基线

当前共 58 个任务：54 个包含 `dimension/explain`，T055、T056、T057、F001 缺少这两个字段；11 个引用 Scenario；6 个任务显式定义 12 条 Acceptance Criteria；全部任务都有 Legacy Validation Rules；没有任务使用 `validation_prompt`。

当前 Scenario YAML 中的 `name`、`description` 以及 Task YAML 中的 `dimension`、`explain` 会被旧模型静默忽略，Phase 1 必须完整保留。

#### Phase 1.1：目录决策

仓库同时存在 `simulate_serve/config.py` 和 YAML 数据目录 `simulate_serve/config/`，因此不把 Python loader/schema 放入同名 `config` 包。采用：

```text
simulate_serve/
├── configuration/
│   ├── __init__.py
│   ├── catalog_schema.py
│   ├── catalog_loader.py
│   └── diagnostics.py
├── domain/
│   ├── __init__.py
│   ├── persona.py
│   ├── task.py
│   └── provenance.py
├── application/
│   ├── __init__.py
│   └── task_compiler.py
├── config.py
└── config/
    ├── config.yaml
    ├── tasks.yaml
    └── scenarios.yaml
```

YAML 目录暂不迁移；目标目录中的 Python `config/` 统一修订为 `configuration/`。

#### Phase 1.2：Raw Document Schema

Raw 模型忠实表达“配置前”的数据，继承字段必须使用 Optional，从而区分未配置和明确覆盖：

```python
class TaskDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_type: str
    dimension: str | None = None
    explain: str | None = None
    scenario: str | None = None
    task_prompt: str
    user_persona: PersonaOverride | None = None
    acceptance_policy: AcceptancePolicyDocument | None = None
    acceptance_criteria: list[CriterionDocument] | None = None
    constraints: list[str] | None = None
    excluded_platforms: list[str] | None = None
    interaction_protocol: str | None = None
    fallback_guidance: list[str] | None = None
    validation_rules: LegacyValidationRulesDocument
    expected_reference: str | None = None
    validation_prompt: str | None = None
```

统一空值语义：

- `None`：未配置，继续继承。
- `[]`：明确清空列表。
- 空字符串：非法，不隐式表示继承或清空。
- 字符串在校验前 `strip()`。

ScenarioDocument 必须补齐 `scenario_id/name/description`，并设置 `extra="forbid"`。

#### Phase 1.3：Catalog 版本

版本放在文件级，不在 58 个 Task 中重复：

```yaml
schema_version: "1"
tasks:
  - task_id: T001
```

Scenario 文件对应使用 `schema_version + scenarios`。Loader 同时接受当前顶层列表并识别为 legacy v0；CompiledTask 记录 `source_schema_version`。初次实现不同时机械重排整个 YAML 文件，待编译测试稳定后再迁移 envelope。

#### Phase 1.4：CompiledTask

CompiledTask 是后续运行层唯一应消费的任务真源：

```python
class CompiledTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    task_type: str
    dimension: str
    explain: str
    goal: str
    persona: PersonaSpec
    criteria: tuple[AcceptanceCriterion, ...]
    constraints: tuple[TaskConstraint, ...]
    excluded_platforms: tuple[str, ...]
    interaction_policy: InteractionPolicy
    validation_policy: ValidationPolicy
    reference_text: str | None
    provenance: TaskProvenance
```

Raw 模型使用 list，Compiled 模型使用 tuple。Task Type 和 Dimension 保持可扩展非空字符串，不使用 Enum。ID 约束：

```text
task_id      ^[A-Za-z][A-Za-z0-9_-]{0,63}$
scenario_id  ^[a-z][a-z0-9_]{0,63}$
criterion_id ^[a-z][a-z0-9_.-]{0,127}$
```

#### Phase 1.5：合并规范

统一优先级：`Global Default < Scenario < Task`。

- Persona：按 `role_description/background/tone/verbosity` 逐字段合并，不整体替换。
- Interaction Protocol：Task 显式配置时整体替换；否则继承 Scenario；都没有时使用系统默认。
- Fallback Guidance：`None` 继承；非空列表整体替换；`[]` 表示明确清空并由运行层使用系统 fallback。
- Constraints、Excluded Platforms：Scenario 在前、Task 追加、规范化去重、保留首次顺序。
- Phase 1 不增加 constraints/excluded 的单项删除语法。

#### Phase 1.6：Acceptance Criteria 合并

引入：

```yaml
acceptance_policy:
  mode: extend  # extend | replace
```

- `extend`：Scenario 标准作为基础，Task 追加；同 `criterion_id` 时 Task 覆盖 Scenario。
- `replace`：只使用 Task 标准，但仍继承 Scenario Persona 和交互策略。

T034 和 T046 的目标分别是按配置识别不存在、拒绝盗版，不能继续继承 `video_search` 的“免费、可播放”标准，因此二者固定使用 `replace`。T011、T015、T026、F001 使用 `extend`。

新 Criterion 必须有显式 `criterion_id`。Phase 1 给现有 Scenario/Task 的 18 条显式标准补 ID。外部 legacy 标准若无 ID，Compiler 生成稳定的 `legacy-<8位哈希>` 并产生 warning；不引入拼音依赖。

#### Phase 1.7：Legacy Validation Rules 编译

52 个任务没有显式 Criteria。Compiler 从旧规则派生兼容标准：

```text
legacy:<task_id>:keywords
legacy:<task_id>:format
legacy:<task_id>:min_length
legacy:<task_id>:semantic
```

Phase 1 只形成 Criteria，不改变执行语义。`min_length` 保持旧“字符数”语义并标记 deprecated，Phase 3 才迁移为明确 `min_chars`/`min_items`。

`expected_reference` 编译为 `reference_text`，明确仅供 Judge/人工参考，不作为自动成功标准，也不对编号自由文本做正则拆分。`validation_prompt` 保留在 Raw 兼容模型，若出现则产生 deprecated warning，不进入核心 CompiledTask。

#### Phase 1.8：来源追踪与诊断

使用独立 Provenance Map，不把领域字段包装成 ValueWithSource：

```python
class SourceRef(BaseModel):
    source_type: str  # default | scenario | task | derived
    source_id: str
    path: str

class TaskProvenance(BaseModel):
    fields: dict[str, SourceRef]
```

Criteria 自身携带 source。CatalogLoader 聚合全部错误后一次报告，错误包含文件、列表索引和字段路径。

严格策略：未知字段、重复 Task/Scenario ID、未解析 Scenario、格式错误全部失败；malformed Scenario 不再静默跳过。显式配置的 Scenario 文件缺失为错误；未配置且没有 Task 引用 Scenario 时允许。

#### Phase 1.9：缺失字段和路径兼容

- T055/T056/T057 的 dimension 补为“内容载体类型”。
- F001 的 dimension 补为“文件操作”。
- explain 允许为空，但产生 warning，等待人工补充。
- 不根据 task_type 自动给 T055-T057 添加 `video_search` Scenario，避免隐式改变验收标准。

新路径规则：相对 `tasks_file/scenarios_file` 首先基于 config.yaml 所在目录解析；不存在时回退旧 `PACKAGE_DIR` 规则并打印 deprecated warning。内置 config 更新为 `tasks.yaml`/`scenarios.yaml`。

#### Phase 1.10：兼容接入

不建立未被运行流程消费的影子 Compiler。TaskManager 改为：

```text
CatalogLoader -> TaskCompiler -> CompiledTask[]
                              -> LegacyTaskAdapter -> Task[]
```

TaskManager 暂时同时暴露 `compiled_tasks` 和兼容 `tasks`；CompiledTask 是唯一真源，Legacy Task 不允许反向写回。状态字典保留到 Phase 2；LegacyTaskAdapter 保留到 Phase 6，待新 TaskRuntime 完全消费 CompiledTask 且兼容输出已收口后删除。

#### Phase 1 测试矩阵

新增 Raw Schema、CatalogLoader、TaskCompiler、Criteria Merge、CompiledTask、LegacyTaskAdapter 和内置 Catalog 功能测试。必须覆盖 legacy/v1 Catalog、未知字段、重复 ID、未知 Scenario、Persona 逐字段覆盖、None/空列表语义、Criteria extend/replace/override、来源追踪、不可变模型、相对路径和全部 58 个任务编译。

代表任务至少包含 T001、T034、T046、F001，并断言 T034/T046 不再继承冲突 Criteria。

#### Phase 1 输出文档

```text
docs/phase1-task-schema.md
docs/phase1-migration-report.md
```

迁移报告记录 58 个任务编译结果、warning/error 数量、Criteria 来源、Scenario 继承和弃用字段。

#### Phase 1 完成标准

- 58 个任务全部进入严格编译流程。
- `dimension/explain/name/description` 不再静默丢失。
- Raw Document 与 CompiledTask 明确分离。
- 每个 CompiledTask 至少有显式或 legacy 派生 Criteria，且每条有稳定 ID 和来源。
- T034/T046 不再继承冲突 Criteria。
- CompiledTask 不可变。
- 当前 SessionRunner 可通过 LegacyTaskAdapter 继续运行。
- Phase 0 非 xfail 测试继续通过；新测试完全离线。

### Phase 2：任务状态机与应用端口

目标：用异步、确定性状态机替换 SessionRunner 的隐式 while 循环，建立交互表达层、远端执行端口、验证端口和运行记录端口，并保持远端离线可测。

#### Phase 2 核心决策

- async 运行内核和 `httpx.AsyncClient` 在本阶段一次完成，不等到 Phase 4，避免二次重写。
- 状态机使用普通 Python，不由 LLM/CAMEL Workforce 控制。
- Batch 默认仍串行执行；先稳定单任务语义，再考虑并发。
- `guide_rounds` 只统计成功发送到远端的 follow-up，不统计验证失败次数或发送失败。
- POST 提交在缺少服务端幂等键时不对“请求已发送但响应未知”的超时自动重提，避免重复任务。
- Legacy Validator 通过 Adapter 接入本阶段端口，fail-open 在 Phase 3 正式移除。
- Phase 2 记录完整内存事件；持久化原子日志和新版输出在 Phase 6 实现。

#### Phase 2.1：目录与应用端口

```text
simulate_serve/
├── application/
│   ├── run_task.py
│   ├── run_batch.py
│   ├── ports.py
│   └── errors.py
├── domain/
│   ├── run.py
│   ├── run_event.py
│   └── state_machine.py
├── interaction/
│   ├── actor.py
│   ├── prompt_builder.py
│   ├── guidance_policy.py
│   └── response_parser.py
└── infrastructure/
    ├── qwenpaw_executor.py
    └── legacy_validation_adapter.py
```

端口全部使用领域对象：

```python
class InteractionActor(Protocol):
    async def create_opening(self, context: InteractionContext) -> UserUtterance: ...
    async def create_followup(
        self,
        context: InteractionContext,
        report: ValidationReport,
    ) -> UserUtterance: ...

class ExecutorGateway(Protocol):
    async def open_session(self, request: ExecutorRequest) -> ExecutorResponse: ...
    async def continue_session(
        self,
        session_id: str,
        request: ExecutorRequest,
    ) -> ExecutorResponse: ...

class ValidationService(Protocol):
    async def validate(self, request: ValidationRequest) -> ValidationReport: ...

class RunRepository(Protocol):
    async def save_checkpoint(self, run: TaskRun) -> None: ...
```

Phase 2 的 RunRepository 使用内存/旧 DataSink Adapter；Phase 6 替换为正式实现。

#### Phase 2.2：状态模型

```python
class RunState(str, Enum):
    PENDING = "pending"
    PREPARING = "preparing"
    GENERATING_OPENING = "generating_opening"
    WAITING_EXECUTOR = "waiting_executor"
    VALIDATING = "validating"
    GENERATING_FOLLOWUP = "generating_followup"
    SUCCESS = "success"
    GUIDE_EXHAUSTED = "guide_exhausted"
    INCONCLUSIVE = "inconclusive"
    VALIDATION_ERROR = "validation_error"
    EXECUTOR_ERROR = "executor_error"
    ACTOR_ERROR = "actor_error"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
```

终态集合固定为 SUCCESS、GUIDE_EXHAUSTED、INCONCLUSIVE、VALIDATION_ERROR、EXECUTOR_ERROR、ACTOR_ERROR、CANCELLED、INTERRUPTED。

合法迁移：

```text
PENDING -> PREPARING
PREPARING -> GENERATING_OPENING
GENERATING_OPENING -> WAITING_EXECUTOR
WAITING_EXECUTOR -> VALIDATING
VALIDATING + PASS -> SUCCESS
VALIDATING + FAIL/retryable/round available -> GENERATING_FOLLOWUP
GENERATING_FOLLOWUP -> WAITING_EXECUTOR
VALIDATING + FAIL/no round -> GUIDE_EXHAUSTED
VALIDATING + INCONCLUSIVE -> INCONCLUSIVE
VALIDATING + ERROR -> VALIDATION_ERROR
Actor failure -> ACTOR_ERROR
Executor failure -> EXECUTOR_ERROR
非终态 + cancellation -> CANCELLED
进程恢复时发现非终态旧记录 -> INTERRUPTED
```

非法迁移抛 `InvalidTransitionError`，不得仅记录 warning。

#### Phase 2.3：事件与计数语义

每次迁移生成不可变 RunEvent：

```python
class RunEvent(BaseModel):
    event_id: str
    run_id: str
    event_type: str
    from_state: RunState
    to_state: RunState
    occurred_at: datetime
    reason_code: str
    metadata: dict[str, JsonValue]
```

TaskRun 分开记录：

- `executor_turns`：收到的远端有效回复数。
- `guide_rounds`：成功提交的 follow-up 数。
- `validation_rounds`：完成的 ValidationReport 数。
- `submission_attempts`：HTTP submit 尝试数，仅作传输指标。

`guide_rounds` 在 follow-up submit 成功并获得 remote task ID 后增加；发送前、生成话术后或发送失败时都不增加。

#### Phase 2.4：标识模型

严格区分：

- `run_id`：模拟端一次任务运行 ID。
- `task_id`：配置任务 ID。
- `remote_session_id`：远端对话会话 ID。
- `remote_task_id`：远端一次异步提交 ID。
- `agent_id`：处理本次提交的远端 Agent ID。

日志和输出不得再用一个 `session_id` 同时表达模拟端运行和远端会话。

#### Phase 2.5：异步 QwenPaw Executor

使用单个有生命周期的 `httpx.AsyncClient(http2=False)`：

- `QwenPawExecutor.start()` 创建 Client。
- `close()` 或 async context manager 关闭。
- Submit 与 Poll 始终携带同一个 effective agent ID。
- 每个请求设置 connect/read/write/pool timeout。
- 整体远端任务另设 deadline。
- Poll GET 可对连接错误、429、502/503/504 做有限退避重试。
- 4xx 配置/鉴权错误不重试。
- 远端 `failed/cancelled` 转换为结构化 ExecutorError。

POST 重试规则：

- DNS/连接建立前失败：允许重试。
- 收到明确 429/502/503/504 且服务端尚未创建任务：允许重试。
- Read timeout 或连接中断导致是否已创建任务不明确：返回 `SUBMISSION_AMBIGUOUS`，不自动重提。
- 若后端未来支持 idempotency key，再开启安全重提。

这比当前对所有异常统一重试更安全。

#### Phase 2.6：Interaction Actor

现有 UserSimulator 通过 `CamelInteractionActor` 迁移到 async：

- 使用 CAMEL `ChatAgent.astep()`。
- PromptBuilder 与 Actor 分离，PromptBuilder 是纯函数。
- Actor 返回 `UserUtterance(content/action/reason_codes/target_criteria)`。
- Phase 2 暂时兼容旧自由文本响应解析；Phase 3 后改用严格结构化输出。
- Actor 初始化/调用失败转为 `ActorError`；是否使用 fallback 由 InteractionPolicy 显式配置，不再静默决定。

默认保留现有 fallback 行为以减少 Phase 2 业务变化，但记录 `ACTOR_FALLBACK_USED` 事件。

#### Phase 2.7：TaskRuntime 用例

`run_task` 只编排一个 CompiledTask，不负责加载 Catalog 或导出 Batch 统计：

```python
async def run_task(deps: RuntimeDependencies, task: CompiledTask) -> TaskRun:
    run = TaskRun.start(task)
    opening = await deps.actor.create_opening(run.interaction_context())
    response = await deps.executor.open_session(ExecutorRequest.from_utterance(opening))

    while True:
        report = await deps.validator.validate(ValidationRequest.from_run(run, response))
        if report.verdict is PASS:
            return run.succeed(report)
        if report.verdict is INCONCLUSIVE:
            return run.finish_inconclusive(report)
        if report.verdict is ERROR:
            return run.fail_validation(report)
        if not report.retryable or run.guide_rounds >= task.validation_policy.max_guide_rounds:
            return run.exhaust(report)
        followup = await deps.actor.create_followup(run.interaction_context(), report)
        response = await deps.executor.continue_session(run.remote_session_id, ExecutorRequest.from_utterance(followup))
```

每个外部调用前后都记录事件和 checkpoint。Phase 2 checkpoint 可为内存实现，但调用位置固定，为 Phase 6 原子持久化预留。

#### Phase 2.8：BatchRunner

- 默认严格串行，保持现有调用行为和可调试性。
- 单个 Task 所有异常在边界转换为终态并保存，之后继续下一 Task。
- `--limit` 在编译后的稳定顺序上生效。
- Ctrl+C/取消：停止接收新 Task；当前 Task 转 CANCELLED；已完成任务保留。
- Phase 2 不引入并发、队列或分布式调度。

未来并发只能添加在 `run_task` 外层，并通过 `max_concurrency` 限制，不改变状态机。

#### Phase 2.9：Legacy 兼容与切换

- Legacy Validation Adapter 将旧 rule/remote validation 结果转换为四态 ValidationReport；暂时保留其错误语义并维持 Phase 0 xfail。
- Legacy DataSink Adapter 接收 TaskRun，导出当前 SessionRecord。
- 旧 SessionRunner 保留一个版本但 CLI 默认切到新 BatchRunner；可使用临时 `runtime_engine: legacy|state_machine` 回退。
- Phase 2 验收后默认设为 `state_machine`，Phase 6 删除 legacy 选项。

#### Phase 2.10：测试替身与测试矩阵

本阶段正式建立：

- `ScriptedExecutor`：脚本化新建/续会话/错误。
- `StubInteractionActor`。
- `StubValidationService`。
- `InMemoryRunRepository`。
- `FakeClock`、`FixedIdGenerator`。

状态机表驱动测试覆盖所有合法与非法迁移。功能测试覆盖首轮成功、一次/多次引导成功、耗尽、Actor 错误、Executor 错误、Validation ERROR/INCONCLUSIVE、取消、Batch 错误隔离。

Async Executor 合约测试使用 `httpx.MockTransport`，验证 Agent Header 贯穿 submit/poll、timeout 分类、POST ambiguous 不重提、Poll 重试和 Client 关闭。

Phase 2 开始加入 `pytest-asyncio`，pytest 使用严格 asyncio mode，测试结束不得残留 task、client 或后台轮询。

#### Phase 2 输出文档

```text
docs/phase2-runtime-state-machine.md
docs/phase2-executor-contract.md
```

#### Phase 2 完成标准

- CLI 默认使用 async BatchRunner/TaskRuntime。
- 所有任务运行由合法状态迁移控制。
- ScriptedExecutor 覆盖全部终态。
- guide/executor/validation 三类计数语义准确。
- Submit/Poll 使用同一 Agent ID。
- AsyncClient 生命周期无泄漏。
- 单任务异常不终止 Batch。
- 默认仍串行，无远端 Agent 时全部测试通过。
- Legacy SessionRunner 可临时回退，Phase 0/1 非 xfail 测试继续通过。

### Phase 3：验证流水线

目标：建立模拟端本地、结构化、fail-closed 的最终裁决能力；每次结论都能追踪到 Criterion、Validator、原因码和证据。

#### Phase 3 核心决策

- 四态 Verdict 固定为 PASS、FAIL、INCONCLUSIVE、ERROR，不再使用单一 bool 表达验证。
- 本地 ValidationPipeline 是最终裁判；远端 Validation Agent 仅可作为 advisory provider。
- 确定性结果优先于 LLM 判断，Judge 不得覆盖明确失败。
- 单一 CAMEL Semantic Judge 足够完成初期语义判断，不引入多 Judge 投票和 Workforce。
- Judge 使用独立低温模型实例，不与 Persona Actor 共享记忆。
- Phase 3 建立 EvidenceProvider Port，但默认使用 NoopEvidenceProvider；真实工具在 Phase 4 接入。
- 最新回复是规则校验主体，完整对话只供语义上下文和进度判断。
- 解析失败、模型异常、输出缺字段一律 ERROR，不得通过。

#### Phase 3.1：目录

```text
simulate_serve/validation/
├── __init__.py
├── pipeline.py
├── request.py
├── aggregation.py
├── claims.py
├── reason_codes.py
├── deterministic/
│   ├── base.py
│   ├── keyword.py
│   ├── format.py
│   ├── fields.py
│   ├── item_count.py
│   ├── constraints.py
│   └── url_syntax.py
├── semantic/
│   ├── judge.py
│   ├── prompt_builder.py
│   └── response_schema.py
└── evidence/
    ├── port.py
    └── noop.py
```

#### Phase 3.2：请求和结果契约

```python
class ValidationRequest(BaseModel):
    task: CompiledTask
    run_id: str
    round_index: int
    conversation: tuple[ConversationTurn, ...]
    latest_response: ExecutorResponse

class CriterionResult(BaseModel):
    criterion_id: str
    verdict: Verdict
    validator: str
    reason_code: str
    message: str
    retryable: bool
    evidence_ids: tuple[str, ...] = ()
    observed: JsonValue | None = None

class ValidationReport(BaseModel):
    report_id: str
    verdict: Verdict
    criteria: tuple[CriterionResult, ...]
    missing_items: tuple[str, ...]
    retryable: bool
    diagnostics: tuple[ValidationDiagnostic, ...]
```

`message` 用于人工审计，`reason_code` 用于程序、统计和 GuidancePolicy；不能让运行层解析自然语言错误原因。

#### Phase 3.3：流水线顺序

```text
Response Contract Check
  -> Deterministic Validators
  -> Deterministic Claim Extraction
  -> EvidenceProvider（Phase 3 为 Noop）
  -> Semantic Judge（仅处理仍需语义判断的 Criteria）
  -> Criterion Aggregation
  -> ValidationReport
```

- Contract Check 先拒绝空回复、非法类型、无法确定 final text 的响应。
- 确定性校验全部执行，不在第一个失败处短路，便于一次生成完整追问。
- 已由确定性校验 PASS/FAIL 的 Criterion 不交给 Judge 重判。
- 需要工具但当前无 EvidenceProvider 的 Criterion 为 INCONCLUSIVE，而不是交给 Judge 猜测。

#### Phase 3.4：确定性 Validator 规范

`KeywordValidator`：

- 支持 `all/any/none` 三种模式。
- 支持大小写、Unicode 和空白规范化。
- 禁止通过把关键词写在“校验规则”复述中投机通过；Judge 仍检查语境。

`FormatValidator`：

- JSON：整体可由 `json.loads` 解析，若要求 object/array 必须匹配。
- List：至少存在指定数量的 Markdown/编号/项目符号条目。
- Table：必须有表头、分隔行和数据行；列数一致。
- Card：使用项目定义的显式字段集合，不凭标题中出现“卡片”判断。
- Text：只要求非空文本，不增加隐式格式。

`FieldValidator`：

- JSON 检查真实 key/path。
- Table 检查规范化表头。
- 普通文本字段要求只作为 legacy 兼容，并产生 warning。

`ItemCountValidator`：明确区分 `min_chars` 与 `min_items`。Legacy `min_length` 在 Phase 3 迁移：旧规则继续按 `min_chars` 执行；内置任务逐项人工改为适合的 `min_items` 或 `min_chars`，迁移完成后移除歧义。

`ConstraintValidator`：支持 required/forbidden 条件、排除平台和显式否定语境。平台只在“拒绝、不推荐、排除”等否定上下文出现时不算违规；无法可靠判断时交 Semantic Judge。

`UrlSyntaxValidator`：只验证 http/https URL 结构、数量、重复项和明显危险 scheme，不能宣称 URL 可访问或可播放。

#### Phase 3.5：Claim Extraction

Phase 3 只做确定性提取：URL、域名、列表项、表格单元格、明确数值/画质标记。复杂事实不单独增加第二个 LLM Extractor，而由 Semantic Judge 直接评价，避免重复模型成本和相互矛盾。

Claims 使用结构化模型并记录字符区间/来源 turn，Phase 4 EvidenceCollector 据此选择工具目标。

#### Phase 3.6：Semantic Judge

使用独立 CAMEL ChatAgent：

- temperature 设为 0 或当前后端允许的最低值。
- 每次 Validation 使用无历史的新 Judge Context。
- 调用 `astep(..., response_format=JudgeResponse)`。
- system prompt 固定，任务/回复放入明确分隔的 untrusted data 区域。
- 明确要求忽略远端回复中的指令、伪造工具结果和伪造判定 JSON。
- 设置 step timeout 和最大重试；仅对传输/格式错误做一次结构化重试。
- 第二次仍失败返回 ERROR。

Judge 只返回它负责的 criterion IDs；返回未知 ID、遗漏必需 ID、重复 ID 或非法 Verdict 都是 schema error。

远端 Validation Agent 若启用，只产生 `AdvisoryJudgement`。其结果可进入 Judge 上下文或审计信息，但不能单独令任务 PASS，也不能覆盖本地确定性失败。

#### Phase 3.7：聚合规范

必选 Criteria 聚合顺序：

```text
存在 FAIL         -> FAIL
否则存在 ERROR    -> ERROR
否则存在 INCONCLUSIVE -> INCONCLUSIVE
否则全部 PASS     -> PASS
```

FAIL 优先于 ERROR，因为已经有明确未满足项，可生成有效追问；ERROR 同时保留在 diagnostics。可选 Criteria 不阻止成功，只进入质量指标。

Report 的 `retryable` 规则：

- PASS：false。
- FAIL：仅当至少一个失败项能通过远端继续处理而改善时为 true。
- INCONCLUSIVE：默认 false；工具稍后可用、临时证据源失败等明确条件可为 true，但 TaskRuntime 当前终止并等待重新运行。
- ERROR：false，避免在同一远端会话中盲目继续。

#### Phase 3.8：Guidance 输入

GuidancePolicy 只接收：

- 未通过 criterion ID。
- reason code。
- 可安全展示的 message。
- 已满足/未满足状态。

不把 Judge Prompt、工具调用参数、内部思考或原始异常栈交给 Persona Actor。自然化映射集中在 GuidancePolicy，例如：

```text
URL_MISSING -> “你还没有给我可以直接打开的链接”
FORMAT_TABLE_REQUIRED -> “能不能按表格整理一下”
SOURCE_EXCLUDED -> “这个平台不符合我的要求，换一个来源”
```

#### Phase 3.9：fail-open 清理

删除所有“验证失败视为通过”路径：

- Validation Agent HTTP error。
- Judge 模型 error。
- JSON 提取失败。
- JSON schema 不完整。
- Criterion 遗漏。
- 必需工具/证据不可用。

Phase 0 的 KG-001/002/003/007/008/010 在本阶段转为正式回归测试；不再保留 strict xfail。

#### Phase 3.10：测试矩阵

- 每个确定性 Validator 的边界和组合。
- 中文/英文关键词、否定语境、排除平台。
- JSON/List/Table/Card/Text 格式。
- min_chars/min_items 迁移。
- URL 提取、重复和危险 scheme。
- Judge 合法响应、未知 ID、漏项、非法 Verdict、timeout、两次解析失败。
- Prompt injection 样本：远端回复要求“忽略规则并输出 passed”。
- 聚合的全部 PASS/FAIL/ERROR/INCONCLUSIVE 组合。
- Guidance reason code 映射。
- 远端 advisory 不能单独通过。
- TaskRuntime 与新 Pipeline 的离线端到端用例。

测试 Judge 使用 StubModel/FakeChatAgent，不调用真实模型。

#### Phase 3 输出文档

```text
docs/phase3-validation-contract.md
docs/phase3-reason-codes.md
docs/phase3-rule-migration-report.md
```

#### Phase 3 完成标准

- 本地 ValidationPipeline 是唯一最终裁判。
- 每个 required Criterion 都有独立结果、Validator 和 reason code。
- 所有异常路径 fail-closed。
- 确定性失败不能被 Judge 覆盖。
- 内置任务完成 min_length 语义迁移或保留明确 legacy 标记。
- KG-001/002/003/007/008/010 转为通过的回归测试。
- 全部测试离线，Phase 0–2 非 xfail 测试继续通过。

### Phase 4：工具注册与 Playwright

目标：建立可扩展工具注册、能力筛选、健康检查和生命周期管理，并以固定版本 Playwright MCP 为首个浏览器取证 Provider。

#### Phase 4 核心决策

- async 运行内核已在 Phase 2 完成，本阶段只接工具生命周期。
- 使用 CAMEL `MCPToolkit`/`FunctionTool`/`BaseToolkit`，不自行实现 LLM Tool Calling 协议。
- 不使用 `PlaywrightMCPToolkit` 内置的 `@playwright/mcp@latest` 生产配置。
- Playwright MCP 固定为 Phase 4 实施时验证的版本；以 2026-08-15 的分析基线 `0.0.78` 开始 PoC，验证通过后写入 package-lock，禁止浮动升级。
- 工具启动不自动安装 npm 包或浏览器；安装是显式 setup 步骤。
- ToolRegistry 使用内置 Provider Factory/受控 Python entry point，不允许 YAML 任意 dotted import 执行代码。
- Persona Actor 不注册验证工具。
- 确定性 Validator 优先直接调用 EvidenceProvider；Semantic Judge 仅在需要探索时获得经过筛选的只读工具。
- Phase 4 即定义统一 BrowserEvidenceProvider，高层验证不绑定 Playwright；Phase 5 只增加 Camoufox Provider。

#### Phase 4.1：目录

```text
simulate_serve/tools/
├── __init__.py
├── descriptor.py
├── registry.py
├── factories.py
├── capabilities.py
├── health.py
├── lifecycle.py
├── errors.py
├── camel_adapter.py
├── evidence_adapter.py
└── browser/
    ├── protocol.py
    ├── models.py
    ├── policy.py
    └── playwright_mcp.py

tool_runtime/playwright/
├── package.json
└── package-lock.json
```

#### Phase 4.2：ToolDescriptor 与状态

```python
class ToolDescriptor(BaseModel):
    name: str
    provider_type: str
    enabled: bool
    required: bool
    capabilities: frozenset[str]
    allowed_task_types: frozenset[str] = frozenset()
    startup_timeout_seconds: float = 20
    call_timeout_seconds: float = 30
    max_concurrency: int = 1
    config: dict[str, JsonValue] = Field(default_factory=dict)

class ToolStatus(str, Enum):
    DISABLED = "disabled"
    READY = "ready"
    DEPENDENCY_MISSING = "dependency_missing"
    INIT_FAILED = "init_failed"
    CONNECT_FAILED = "connect_failed"
    SCHEMA_INVALID = "schema_invalid"
    PROBE_FAILED = "probe_failed"
    SHUTDOWN_FAILED = "shutdown_failed"
```

ToolHandle 持有 descriptor、status、CAMEL tools、provider instance、health detail 和 semaphore。Registry 是唯一生命周期 owner。

#### Phase 4.3：Provider 注册

内置 Provider 由代码中的显式 Factory Map 注册：

```text
playwright_mcp -> PlaywrightMCPProviderFactory
```

外部扩展后续使用 Python entry point group `useramulation.tool_providers`，需在启动时验证元数据和接口。YAML 只能引用已注册 provider type，不接受任意模块路径。

工具名强制 namespace，例如：

```text
browser.navigate
browser.snapshot
browser.inspect_links
browser.inspect_media
browser.detect_barrier
```

重复 capability 可有多个 Provider；Registry 根据 Task Policy、配置优先级和 READY 状态选择，不允许静默覆盖同名工具。

#### Phase 4.4：启动健康检查

所有 enabled Provider 并发但有总并发上限地执行：

1. dependency：模块、Node、命令、本地包、浏览器二进制。
2. construct：创建 Provider/Toolkit。
3. connect：MCP handshake。
4. discover：列出工具。
5. schema：检查 FunctionTool schema、命名和重复。
6. probe：执行无外部副作用的本地 smoke probe。
7. report：生成 ToolReadinessReport。

必须先检查完所有工具并打印完整汇总，再根据 required 状态决定是否终止，不能遇到第一个错误就退出。

```text
Tool readiness
  playwright  READY               required=false tools=18 duration=1.8s
  camoufox    DISABLED
  search      DEPENDENCY_MISSING  required=false reason=package unavailable
```

- required + 非 READY：打印报告后抛 `RequiredToolUnavailableError`。
- optional + 非 READY：继续启动。
- Task Criterion 依赖当前非 READY capability：Criterion 为 INCONCLUSIVE。

#### Phase 4.5：显式安装与固定版本

在 `tool_runtime/playwright/package.json` 固定：

```json
{
  "private": true,
  "dependencies": {
    "@playwright/mcp": "0.0.78"
  }
}
```

以 package-lock 为最终锁定依据。Setup 命令显式运行 `npm ci` 和对应 browser install；普通程序启动和健康检查不得触发下载。运行使用项目本地包，例如在固定 cwd 中执行 `npx --no-install @playwright/mcp@0.0.78`。

版本升级必须单独提交：更新 package.json/lock、运行工具合约和 browser smoke tests、记录升级结果。不得用 `latest`。

实施复核基线：

- CAMEL Tools 和 MCPToolkit：<https://docs.camel-ai.org/key_modules/tools>、<https://docs.camel-ai.org/reference/camel.toolkits.mcp_toolkit>
- Playwright MCP 官方仓库及发布包：<https://github.com/microsoft/playwright-mcp>、<https://www.npmjs.com/package/@playwright/mcp>
- 实施时必须重新确认锁定版本的 CLI 参数和 Node 支持范围；如官方契约已变更，通过独立依赖升级记录更新本基线，不在安装时隐式漂移。

#### Phase 4.6：Playwright MCP 配置

通过 CAMEL `MCPToolkit(config_dict=..., skip_failed=False)` 管理单一 Playwright server：

- stdio transport。
- `--headless`。
- `--isolated`，禁止持久 Profile。
- `--block-service-workers`。
- `--image-responses=omit`，除非 Criterion 明确需要截图。
- `--output-mode=file`，输出到当前 run artifact 临时目录。
- `--output-max-size` 设置上限。
- `--codegen=none`。
- 不启用 unrestricted file access。
- 不授予 clipboard/geolocation 等额外权限。
- 不启用 shared browser context。

一个 Provider 只管理自己的 MCPToolkit，失败状态不会被其他 server 的 `skip_failed` 掩盖。Shutdown 逆序 disconnect，并设置超时。

#### Phase 4.7：统一 Browser Evidence 协议

高层验证只依赖：

```python
class BrowserEvidenceProvider(Protocol):
    async def inspect_url(
        self,
        request: BrowserInspectionRequest,
    ) -> BrowserInspectionResult: ...
```

请求声明 URL、目标 Criterion、允许动作、超时和证据深度。结果规范化为：

- PageObservation：final URL、status、title、主要文本摘要。
- LinkObservation：链接、锚文本、域名。
- MediaObservation：元素/控件、readyState、可选播放进度。
- BarrierObservation：登录、会员、付费、验证码、区域限制。
- EvidenceConfidence：CONFIRMED、SUPPORTED、WEAK、NONE。

不能用“页面存在 video 标签”直接宣称完整内容可播放。播放验证应分层记录：页面可访问、内容匹配、存在播放器、无明显门槛、可选静音播放进度。每个结论只表达实际观察范围。

#### Phase 4.8：工具调用模式

两种模式共用 Registry 和 Evidence：

1. Deterministic Evidence：Criterion 指定 `browser.inspect_url`，Pipeline 直接调用 Provider，结果最可控。
2. Agent Tool Calling：Semantic Judge 仅获得本 Task 所需 capability 对应的只读 FunctionTools，用于决定检查哪个候选 URL 或补充观察。

Judge Tool Calling 设置：

- `max_iteration` 有界。
- `tool_execution_timeout` 有界。
- 单轮/单 Task 最大工具调用数。
- Tool 输出先限流和规范化，再进入模型上下文。
- 禁止把原始 Page、Cookie、Header 或大型 accessibility tree 直接写入 ValidationReport。

#### Phase 4.9：URL 与浏览器安全策略

初期浏览器验证仍需最小安全边界：

- 只允许 http/https。
- 拒绝 URL credentials、localhost、单标签主机名、private/loopback/link-local/multicast/reserved IP。
- 导航前解析 DNS 并检查所有地址。
- final URL/redirect 后再次校验。
- MCP 使用 allowed/blocked origins、文件访问限制和 service-worker block 作为纵深防护。
- 只暴露只读工具；不暴露 click 任意操作、表单填写、文件上传/下载、任意 JavaScript evaluate。
- 禁止登录、验证码处理、支付和外部写操作。
- 每 Task 使用独立 Context 和 artifact 目录。

Playwright MCP 官方说明 allowed/blocked origins 不构成完整安全边界且不覆盖全部重定向，因此本项目不把 Phase 4 浏览器宣称为对恶意 URL 的强沙箱。默认只验证通过 URLPolicy 的公开 URL；需要对敌意网络目标提供强隔离时，必须增加独立 egress sandbox/proxy，属于后续扩展。

#### Phase 4.10：工具配置

```yaml
tools:
  startup_policy: warn
  providers:
    - name: playwright
      type: playwright_mcp
      enabled: true
      required: false
      priority: 100
      capabilities:
        - browser.navigate
        - browser.snapshot
        - browser.inspect_links
        - browser.inspect_media
        - browser.detect_barrier
      startup_timeout_seconds: 20
      call_timeout_seconds: 30
      max_concurrency: 1
```

`auto_install` 不作为配置项，避免误开。缺少本地 npm 包或浏览器时状态为 DEPENDENCY_MISSING，并在启动汇总中说明 setup 命令。

#### Phase 4.11：Evidence 转换与审计

每次调用生成 tool_call_id 和 Evidence：

- provider/tool/capability/version。
- 开始、结束、耗时、状态。
- 规范化且有限的输入摘要。
- 输出摘要、confidence、artifact refs。
- 错误类型和 retryable。

不记录 Cookie、Authorization、完整请求 Header、表单内容和浏览器 Profile。URL 若含用户信息直接拒绝；query 是否保存由字段白名单控制。

#### Phase 4.12：测试矩阵

- Registry 注册、禁用、重复名、capability 选择和优先级。
- 全部 ToolStatus。
- required/optional 启动策略。
- 健康检查完整汇总后失败。
- MCP connect/discover/schema/probe/shutdown。
- 工具 timeout、并发限制和取消。
- Judge 只收到 Task 所需工具。
- 工具缺失导致 required Criterion INCONCLUSIVE。
- URLPolicy 的 localhost/private/IP/credentials/redirect。
- BrowserInspectionResult 到 Evidence 的转换。
- FakeMCP 完成全部默认测试，无 Node/浏览器依赖。
- `browser_smoke` marker 可选打开本地静态测试页。

真实 browser smoke 默认不访问公网，只验证本地测试页；安全 URLPolicy 对本地测试使用显式 test-only exemption，生产配置不存在该 exemption。

#### Phase 4 输出文档

```text
docs/phase4-tool-provider-contract.md
docs/phase4-tool-readiness.md
docs/phase4-playwright-setup.md
```

#### Phase 4 完成标准

- 工具注册、筛选、健康检查和关闭由 Registry 统一管理。
- 启动后完整打印 enabled 工具状态和不可用原因。
- required 工具失败阻止运行，optional 工具可降级。
- Playwright MCP 使用本地固定版本和 lock，不使用 `latest`，启动不下载。
- Persona Actor 无验证工具。
- 工具缺失/调用失败绝不令 Criterion PASS。
- FakeMCP 测试完全离线；可选本地 browser smoke 通过。
- 新增 Provider 不修改状态机和 Validation 聚合。

### Phase 5：Camoufox 与统一浏览器协议

目标：在不修改状态机、ValidationPipeline 和 Criterion 聚合的情况下接入 Camoufox，并验证统一 BrowserEvidenceProvider 可以支持多浏览器实现和受控回退。

#### Phase 5 核心决策

- 高层 BrowserEvidenceProvider 已在 Phase 4 定义，Phase 5 不再改其领域契约。
- Camoufox 使用 Python Async API 直接驱动，不依赖实验性远程 server 或第三方非官方 MCP。
- 通过自定义 CAMEL `BaseToolkit`/`FunctionTool` 暴露与 Playwright 相同的只读领域工具。
- 初始依赖基线固定 `cloverlabs-camoufox==0.6.0`，import 名仍为 `camoufox`；最终以 uv.lock 为准，不浮动升级。
- Camoufox 为可选 Provider，默认不自动安装、不自动 fetch 浏览器。
- Playwright 为默认 primary；Camoufox 只在明确的 Provider Policy 下回退，不对所有失败盲目重试。
- Camoufox 的反检测能力不用于绕过登录、付费、验证码或访问控制。

#### Phase 5.1：目录

```text
simulate_serve/tools/browser/
├── protocol.py
├── models.py
├── policy.py
├── playwright_mcp.py
├── camoufox_driver.py
├── camoufox_toolkit.py
└── provider_selector.py
```

#### Phase 5.2：依赖和安装

使用可选运行依赖：

```toml
[project.optional-dependencies]
browser-camoufox = [
    "cloverlabs-camoufox==0.6.0",
]
```

浏览器二进制通过显式 setup 命令获取，例如 `python -m camoufox fetch`。程序启动只检查：

- Python distribution/import 是否存在。
- Camoufox CLI/API 版本。
- 浏览器 path 是否存在且可执行。
- AsyncCamoufox 能否启动本地空页面。

缺失时标记 DEPENDENCY_MISSING/PROBE_FAILED 并打印安装提示，不触发下载。

实施复核基线：

- Camoufox Python 官方用法：<https://camoufox.com/python/usage/>
- `cloverlabs-camoufox` 发布包：<https://pypi.org/project/cloverlabs-camoufox/>
- 版本升级与 Playwright 使用同样的显式升级流程，包括 API 契约、本地 smoke test 和回退用例，不在 setup 中默认放宽版本。

#### Phase 5.3：CamoufoxDriver

使用：

```python
from camoufox.async_api import AsyncCamoufox
```

每个 BrowserInspectionRequest 建立独立 async context：

- 默认 headless。
- 不使用 persistent context/user_data_dir。
- 默认禁用 geoip、proxy 和自动定位。
- 固定可配置 OS/locale，默认 Windows + zh-CN，减少验证结果随机性。
- 不启用 humanize，验证任务不需要模拟长时用户行为。
- 不保存 Cookie、缓存和 Profile。
- 最多一个 Page，除非请求明确允许新标签。
- finally 关闭 page/context/browser。

Phase 5 不使用 Camoufox experimental remote websocket server，避免额外进程协议和单浏览器指纹共享问题。

#### Phase 5.4：统一只读工具

`CamoufoxValidationToolkit(BaseToolkit)` 对模型暴露与 Playwright 一致的函数 schema：

```text
browser.navigate
browser.snapshot
browser.inspect_links
browser.inspect_media
browser.detect_barrier
```

Toolkit 只委托 BrowserEvidenceProvider，不向 Agent 暴露 Page/Browser 对象、任意 selector click、任意 JavaScript、文件系统路径或原始 Playwright API。

同一个 Criterion 在两个 Provider 上生成相同领域模型；Provider 差异只记录在 Evidence source/version 中。

#### Phase 5.5：媒体与访问门槛检查规范

统一检查层级：

1. URL 语法与网络策略通过。
2. 导航成功并得到最终公开 URL。
3. 标题/主要文本与目标内容具有支持关系。
4. 存在视频/音频元素或可识别播放控件。
5. 未发现明显登录、会员、付费、区域限制、验证码。
6. Criterion 明确要求时，尝试静音播放并观察 currentTime 在有限时间内推进。

Evidence 只能声明观察到的层级。即使 currentTime 推进，也不能仅凭单页面证明“全集、完整影片、所有集数可播放”；集数覆盖必须逐项或抽样并明确 confidence。

#### Phase 5.6：Provider 选择和回退

配置顺序和 priority 共同决定 primary：

```yaml
tools:
  browser_policy:
    primary: playwright
    fallbacks:
      - camoufox
    fallback_on:
      - bot_blocked
      - renderer_crashed
      - browser_incompatible
```

不在以下情况回退：

- 404/410。
- 明确付费墙、登录墙。
- 内容不匹配。
- URLPolicy 拒绝。
- Criterion 已明确 FAIL。
- 用户/系统取消。

发生允许回退时创建新的隔离 Context，两个 Provider 的 Evidence 均保留。最终聚合不得隐藏 primary 失败。

#### Phase 5.7：一致性和冲突处理

如果两个 Provider 结果冲突：

- 一个 ERROR、一个有明确观察：使用有效观察，但保留 ERROR diagnostic。
- 一个发现付费墙、另一个未发现：整体 INCONCLUSIVE，除非有更强可重复证据。
- final URL/标题不一致：INCONCLUSIVE，并记录 provider conflict。
- 两者均确认同一事实：提升 confidence，但不自动扩大事实范围。

ProviderSelector 不产生任务最终 Verdict，只返回 EvidenceSet。

#### Phase 5.8：资源与并发

- 每个 Camoufox Provider 默认 `max_concurrency=1`。
- 启动 probe 与任务调用共享 semaphore。
- 单次导航、观察播放、总检查分别设置 timeout。
- 取消信号必须关闭 AsyncCamoufox context。
- 不跨 Task 复用 Browser Context；若后续为性能复用 browser process，仍必须新建 context 并通过独立设计评审。

#### Phase 5.9：安全边界

复用 Phase 4 URLPolicy。额外规定：

- 不配置代理/geoip 自动发现作为默认行为。
- 不处理 CAPTCHA，不点击 Turnstile/验证码。
- 不利用 anti-detect 绕过服务访问控制。
- 不允许登录态导入和持久存储。
- 页面要求下载/App 安装/扩展时记录 Barrier，不执行。
- Camoufox 配置不得开放任意 `config` 字典给 Task/LLM；只允许应用白名单字段。

#### Phase 5.10：测试矩阵

- Camoufox dependency/module/binary/launch health 状态。
- Async context 正常关闭、异常关闭、取消关闭。
- 统一工具 schema 与 Playwright 完全一致。
- Provider priority 和 capability 选择。
- 允许/禁止 fallback reason。
- 冲突 Evidence 聚合。
- Cookie/Profile 隔离。
- timeout/concurrency。
- FakeBrowserDriver 完成默认测试，不需要真实 Camoufox。
- 可选 `camoufox_smoke` 打开本地静态测试页，并与 Playwright 结果运行同一 contract suite。

本地 smoke 只检查统一协议，不测试或宣称绕过任何第三方反机器人机制。

#### Phase 5 输出文档

```text
docs/phase5-browser-provider-contract.md
docs/phase5-camoufox-setup.md
docs/phase5-browser-evidence-levels.md
```

#### Phase 5 完成标准

- Camoufox 可由 optional dependency/config 独立启停。
- 启动不自动安装 Python 包或下载浏览器。
- Camoufox 与 Playwright 通过同一 BrowserEvidenceProvider contract suite。
- 切换/回退 Provider 不修改状态机、TaskRuntime、ValidationPipeline 或 Criterion 聚合。
- 回退条件明确且不会掩盖付费墙、404、策略拒绝等真实失败。
- 所有浏览器资源在成功、异常和取消路径都被关闭。
- 默认测试完全离线，可选 smoke 只访问本地 fixture。

### Phase 6：输出迁移与清理

目标：将 TaskRun、状态事件、ValidationReport 和 Evidence 可靠持久化，生成干净的蒸馏数据集，提供一个版本周期的旧格式兼容，并删除旧运行链路。

#### Phase 6 核心决策

- 运行审计数据与蒸馏数据分离：所有 Run 都保存，但默认蒸馏数据集只包含 SUCCESS。
- 新输出 Schema 使用独立版本号 `run_schema_version: "2"`，不与 Task Catalog 版本混用。
- `run_id` 与 `remote_session_id` 永久分离。
- 不保存 Persona Actor、Semantic Judge 或远端模型的自由文本思维链。
- 结构化 reason code、CriterionResult、RunEvent 和 Evidence 足够用于审计。
- Legacy Export 默认开启一个项目版本周期，之后移除；兼容输出不伪造新语义。
- 所有单文件写入使用同目录临时文件 + flush/fsync + `os.replace` 原子替换。
- Phase 6 不自动恢复未完成远端任务；启动发现非终态记录时标记 INTERRUPTED，显式重跑，避免重复外部执行。

#### Phase 6.1：目录与输出布局

```text
output/
├── runs/
│   └── <run_id>/
│       ├── run.json
│       ├── events.jsonl
│       ├── validations.jsonl
│       └── artifact_refs.json
├── artifacts/
│   └── <sha256-prefix>/<sha256>.<ext>
├── datasets/
│   ├── distill_dataset.v2.jsonl
│   └── all_runs.v2.jsonl
├── reports/
│   └── stats.v2.json
├── legacy/
│   ├── <legacy-session-id>.json
│   ├── distill_dataset.jsonl
│   └── stats.json
└── run.log
```

运行目录按 run_id 隔离；Artifact 内容寻址去重。日志不作为领域记录真源。

#### Phase 6.2：Run Schema v2

```python
class RunRecordV2(BaseModel):
    run_schema_version: Literal["2"]
    run_id: str
    task_id: str
    catalog_schema_version: str
    state: RunState
    started_at: datetime
    ended_at: datetime | None
    remote_session_id: str | None
    execution_agent_id: str | None
    executor_turns: int
    guide_rounds: int
    validation_rounds: int
    conversation: tuple[ConversationTurn, ...]
    event_ids: tuple[str, ...]
    validation_report_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    failure: FailureRecord | None
```

ConversationTurn 使用稳定 `turn_id/role/content/created_at/remote_task_id`。只保存明确 final text；无法可靠区分推理和最终回答的远端响应作为 raw artifact/diagnostic，不进入蒸馏 conversation。

#### Phase 6.3：RunRepository

```python
class RunRepository(Protocol):
    async def create(self, run: TaskRun) -> None: ...
    async def append_event(self, event: RunEvent) -> None: ...
    async def append_validation(self, report: ValidationReport) -> None: ...
    async def store_evidence(self, evidence: Evidence) -> EvidenceRef: ...
    async def save_checkpoint(self, run: TaskRun) -> None: ...
    async def finalize(self, run: TaskRun) -> None: ...
```

JSON 文件写入可放入 `asyncio.to_thread`，避免阻塞 event loop。每次状态迁移先追加 Event，再原子更新 run checkpoint。Artifact 先写临时文件、计算 hash、原子移动。

Repository 异常不改变已确定的业务 Verdict，但 TaskRun 无法完成可靠记录时进程返回非零并打印明确错误；不得悄悄继续生成不完整数据集。

#### Phase 6.4：中断和恢复策略

启动时扫描 run.json：

- 终态：保持不变。
- 非终态且所属进程已不存在：追加 `RUN_INTERRUPTED`，状态转 INTERRUPTED。
- 不自动重新 submit 远端任务。
- 不自动续用 remote_session_id。
- 用户可通过后续 `--rerun-task <task_id>` 创建新的 run_id；旧 Run 保留审计关系 `rerun_of`。

安全自动恢复需远端幂等查询、稳定 task ownership 和 checkpoint 协议，当前不具备，明确不在本轮范围。

#### Phase 6.5：Evidence 和 Artifact

Evidence JSON 保存结构化摘要和 artifact ref。禁止持久化：

- Cookie、Authorization、完整请求 Header。
- 浏览器 Profile/storage state。
- LLM 自由文本思维链。
- 工具进程完整环境变量。
- 任意表单敏感内容。

URL credentials 由 URLPolicy 拒绝；query 只在 Validator 声明的白名单字段中保存，否则保存规范化 origin/path 和 hash。该限制属于基本数据边界，不建设复杂 Secret Manager。

Artifact 设置单项和总目录大小上限；超限时保存截断摘要和 `ARTIFACT_DROPPED_SIZE_LIMIT` diagnostic。

#### Phase 6.6：蒸馏数据集

`distill_dataset.v2.jsonl` 默认只包含：

- RunState.SUCCESS。
- 所有 required Criteria PASS。
- conversation turn 成对完整。
- 没有 unresolved raw response contamination。

每行包含 task metadata、persona 摘要、clean conversation、最终 validation summary 和数据 lineage；不包含内部 prompt、Judge 原始回复、思维链和完整工具输出。

失败、错误和 INCONCLUSIVE Run 进入 `all_runs.v2.jsonl` 或 runs 审计目录，不进入默认训练集。可配置专门失败分析导出，但不得复用 distill 文件名。

#### Phase 6.7：统计规范

`stats.v2.json` 至少包含：

- total、各 RunState 数量。
- success_rate，分母定义为全部已终止 Run。
- executor_error/validation_error/inconclusive/cancelled 分开统计。
- avg/p50/p95 guide rounds。
- avg/p50/p95 run duration。
- Criterion pass/fail/inconclusive/error 分布。
- Tool READY/调用/失败/timeout 分布。
- 按 task_type/dimension/scenario 的结果分组。

空数据时比率为 0，不产生 NaN/Infinity。所有统计定义写入 Schema 文档。

#### Phase 6.8：Legacy Export

配置：

```yaml
output:
  schema: v2
  legacy_export: true
```

Legacy Exporter 将 RunRecordV2 投影为旧 SessionRecord：

- `session_id` 使用兼容生成值，不冒充 remote_session_id。
- `success` 仅在 RunState.SUCCESS 时为 true。
- `guide_rounds` 使用修正后的实际发送数。
- `validation_detail` 提供可表达的汇总。
- `internal_thoughts` 固定为空列表并标记 deprecated，不生成或回填思维链。
- 无法表达的新状态通过 fail_reason/validation_detail 显式标注，不能压成假成功。

Legacy `distill_dataset.jsonl` 也默认只导出成功 Run。兼容期结束条件写入变更日志，删除前需要消费者确认。

#### Phase 6.9：CLI 和退出码

CLI 新增只涉及稳定运行管理的选项：

- `--output-format v2|both|legacy`，迁移期默认 both。
- `--rerun-task <task_id>` 创建新 Run。
- `--list-interrupted` 只读展示中断 Run。
- `--check-tools` 只运行工具 readiness。
- `--validate-config` 只编译 Catalog。

退出码规范：

```text
0  命令成功；Batch 中业务任务失败但运行过程完整时仍由统计表达
2  CLI/配置错误
3  必需工具不可用
4  Repository/输出完整性错误
5  启动或运行基础设施错误
130 用户取消
```

是否因部分 Task 失败返回非零作为后续 CI policy 配置，不改变默认交互式批量行为。

#### Phase 6.10：旧代码清理

满足新链路全量测试后删除：

- 旧 SessionRunner 实现和 runtime_engine legacy 开关。
- 旧 SemanticValidator。
- `_validate_with_agent` 和旧 JSON 提取逻辑。
- 旧 rule_check 单函数入口，由 ValidationPipeline 替代。
- LegacyTaskAdapter（TaskRuntime 已完全消费 CompiledTask 后）。
- 旧 DataSink 主实现；保留命名明确的 LegacyExporter。
- SessionRecord/internal_thoughts 等旧领域模型，仅在 legacy schema 模块保留必要 DTO。
- 重复的 `simulate_serve/config/config copy.yaml`（先使用 `rg` 确认无生产、测试和文档引用）。

删除前使用 `rg` 确认无生产 import，并先移除/转换对应 xfail 和兼容测试。

#### Phase 6.11：文档收口

更新：

- README/CLAUDE 项目入口和命令。
- `flow-architecture.md` 为新三层架构。
- QwenPaw API 文档中的新 Executor 约束。
- Task Schema、Validation、Tool、Browser、Output Schema 索引。
- 配置示例和迁移说明。

旧架构文档必须明确标记 archived 或替换，不能与新入口同时声称为当前实现。

#### Phase 6.12：测试矩阵

- 每种终态的 RunRecordV2。
- Event/Validation JSONL append 和 checkpoint 原子替换。
- 模拟写入中断时原文件保持完整。
- Artifact hash、去重、大小上限和清理。
- 非终态扫描转 INTERRUPTED，不自动远端调用。
- v2 distill 只包含成功且干净对话。
- all_runs 包含失败/错误。
- stats 全状态、空数据、分位数。
- Legacy projection 的所有新状态。
- internal_thoughts 永远为空且原始 CoT 不进入 dataset。
- CLI output format、validate-config、check-tools 和退出码。
- 全量离线端到端：Catalog -> Runtime -> Validation -> Fake Tool -> Repository -> Export。

#### Phase 6 输出文档

```text
docs/phase6-run-schema-v2.md
docs/phase6-output-migration.md
docs/phase6-legacy-deprecation.md
docs/phase6-final-validation-report.md
```

#### Phase 6 完成标准

- Run/Event/Validation/Evidence 可靠持久化并有 Schema 版本。
- 所有写入为原子写或 append-only 事件。
- 默认蒸馏数据只含完全成功 Run。
- 失败、错误、取消和不确定状态可独立统计。
- 自由文本思维链不进入 RunRecord、Legacy Export 和数据集。
- Legacy 输出可用但明确 deprecated，默认兼容一个版本周期。
- 非终态 Run 安全标记 INTERRUPTED，不自动产生重复远端任务。
- 旧 SessionRunner/SemanticValidator/DataSink 主链路删除。
- 新文档与实际入口一致。
- 全量离线端到端测试通过；远端恢复后只需运行独立联调清单。

---

## 15. 总体验收标准

- [x] 58 个现有任务均通过严格配置编译，没有字段静默丢失。
- [x] 任务运行由明确状态机控制，非法迁移会报错。
- [x] 单个任务异常不会中断整个批次。
- [x] 校验调用失败、工具失败、结果无法解析时绝不判成功。
- [x] 每条必选 Acceptance Criterion 都有独立 verdict。
- [x] 成功结论能关联规则结果或工具证据。
- [x] Persona Actor 不直接拥有验证浏览器工具。
- [x] Playwright 和 Camoufox 可由配置启停。
- [x] 新增 Toolkit 不需要修改任务状态机。
- [x] 启动日志打印全部启用工具的可用状态和不可用原因。
- [x] 必需工具不可用时阻止运行；可选工具不可用时可降级。
- [x] 不保存或导出自由文本思维链。
- [x] 单元、合约和离线功能测试不依赖真实远端 Agent。
- [x] 真实远端恢复后可通过少量配置开启端到端联调。
- [x] 暂时保留现有明文凭据配置使用方式。

---

## 16. 实施边界与非目标

本轮重构不包含：

- 复杂 Secret Manager 或自动凭据轮换。
- 分布式任务队列和多机调度。
- Web 管理后台。
- 浏览器自动登录、验证码绕过、支付或写操作。
- 将整个流程迁移为 CAMEL Workforce；当前状态机使用普通 Python 更容易测试和约束。
- 依赖真实远端 Agent 才能完成的验收。

后续如果需要并发执行大量 Task，可在稳定的 `run_task` Use Case 外层增加并发控制，不改变领域状态机。

---

## 17. 后续实施入口

后续代码修改应以本文件为实施基线，按 Phase 0 至 Phase 6 顺序推进。每个 Phase 单独完成测试和验收，避免一次性替换全部代码。

已授权的默认实施方式：

1. 从 Phase 0 开始按顺序连续推进，每个 Phase 作为独立变更集，通过本阶段门禁后才进入下一阶段。
2. Playwright 在 Phase 4 实质接入，使用项目内固定版本和 lockfile；安装 Node 包、下载浏览器或系统依赖是实施动作，如运行环境要求权限审批，再按实际命令请求授权，不再作为架构决策点。
3. Camoufox 在 Phase 5 按已定义的统一 Browser Evidence 协议完整实现，不只保留空 Adapter。
4. 当前远程执行 Agent 不可用时，Phase 0–6 的必达验收均使用单元、合约和离线功能测试；真实远程联调为独立非阻断清单。
5. 实施中若发现与本方案同级的新架构冲突、需要扩大外部副作用或需要新的用户权限，才暂停并单独提出；一般实现细节按本文的默认决策自主处理。

---

## 18. 重要决策总结

以下决策是后续实施的默认基线；除非出现新的事实冲突或用户明确变更范围，不再重复讨论。

| ID | 阶段 | 最终决策 |
|---|---|---|
| D-001 | 全局 | 架构固定为“交互表达层 + 任务运行层 + 验证取证层”，三层使用结构化契约交互。 |
| D-002 | 全局 | 初期保留明文模型凭据的便捷使用，不建设 Secret Manager；禁止在新文档、日志和测试数据中扩散实际凭据。 |
| D-003 | Phase 0 | 远程 Agent 离线不阻塞开发；默认禁止测试网络，使用脚本化替身、MockTransport 和离线功能用例。 |
| D-004 | Phase 0 | 已知缺陷用 `xfail(strict=True)`/缺陷清单表达，不在基线阶段顺手改业务语义。 |
| D-005 | Phase 1 | Python 配置代码使用 `configuration/`，`config/` 只作为内置 YAML 数据目录，避免与现有 `config.py` 冲突。 |
| D-006 | Phase 1 | 配置实行 Raw Document + 不可变 CompiledTask 双模型；CompiledTask 是后续运行的唯一真源。 |
| D-007 | Phase 1 | Catalog 版本放在文件级；兼容当前顶层列表 v0，但新 Schema 对未知字段、重复 ID 和无法解析引用严格失败。 |
| D-008 | Phase 1 | Acceptance Criteria 默认 `extend`，只在任务显式声明时 `replace`；Catalog v2 的 T034/T046 使用专用 Scenario，不再依赖 ID 硬编码替换。v1 Legacy Rules 继续编译为稳定 Criterion ID。 |
| D-009 | Phase 1 | 内置 Catalog v2 的 58 个任务全部显式引用 Scenario；编译器仍不根据 task_type 隐式选择 Scenario。 |
| D-010 | Phase 2 | 主运行链在 Phase 2 即完成 async 迁移；HTTP 使用单一生命周期 `httpx.AsyncClient`，不在 Phase 4 再二次改造。 |
| D-011 | Phase 2 | 状态机用普通 Python 显式迁移表实现，不用 LLM/Workforce 控制状态。批处理默认串行，将来在用例外层增加受控并发。 |
| D-012 | Phase 2 | `run_id` 与远程 session/task/agent ID 分离；`guide_rounds` 只在追问成功提交后增加。 |
| D-013 | Phase 2 | 无幂等键时，POST 提交后出现结果不明不自动重试，避免重复创建远程任务。 |
| D-014 | Phase 3 | 本地 ValidationPipeline 拥有最终验收权，远程 Validation Agent 只能作为可选信号。 |
| D-015 | Phase 3 | 单条准则使用 `PASS/FAIL/INCONCLUSIVE/ERROR`；聚合优先级为 FAIL > ERROR > INCONCLUSIVE > PASS，任何无法验证的必选项都不得成功。 |
| D-016 | Phase 3 | 确定性 Validator 和可复现证据优先于 Semantic Judge；Judge 使用严格结构化输出、低温度且不可推翻确定性失败。 |
| D-017 | Phase 4 | ToolRegistry 是工具创建、capability、health check 和关闭的唯一所有者；通过 CAMEL MCPToolkit/FunctionTool/BaseToolkit 适配扩展。 |
| D-018 | Phase 4 | Playwright MCP 以方案分析时的 `@playwright/mcp 0.0.78` 为首次固定基线，使用 package.json + lockfile；禁止 `latest`、启动时自动安装和自动下载。 |
| D-019 | Phase 4 | Playwright 在启动阶段完成可用性检查并先打印完整报告；必需工具不可用则拒绝运行，可选工具降级。 |
| D-020 | Phase 4 | 高层 Browser Evidence 协议在 Playwright 阶段定义，业务验证器不依赖 MCP 工具名和原始输出。 |
| D-021 | Phase 4 | 浏览器默认无状态、无头、只读、禁止上下载/登录/支付；URL allow/block 只是降低风险的控制，不宣称是强安全边界。 |
| D-022 | Phase 5 | Camoufox 使用可选依赖 `cloverlabs-camoufox==0.6.0` 和 `AsyncCamoufox`直接实现，不使用实验性远程/MCP 封装。 |
| D-023 | Phase 5 | Playwright 为主 Provider；仅对 bot-blocked、渲染器崩溃或兼容性故障回退 Camoufox，不对 404、付费墙、策略拒绝或验证失败回退。 |
| D-024 | Phase 5 | Camoufox 默认固定可复现的 locale/OS，禁用持久 profile、geoip、humanize 和 CAPTCHA 绕过。 |
| D-025 | Phase 6 | 审计输出保存全部 Run；默认蒸馏数据集只导出终态 SUCCESS 且通过清洁性检查的对话。 |
| D-026 | Phase 6 | Run/Event/Validation/Evidence 使用 v2 Schema；checkpoint 原子替换，events 使用 append-only，Artifact 带 hash 和大小上限。 |
| D-027 | Phase 6 | 启动时将未终态 Run 标记为 INTERRUPTED，绝不自动续跑远程任务；显式重跑建立新 run_id 和 rerun_of 关系。 |
| D-028 | Phase 6 | 不保存自由文本思维链、cookie、auth header 或浏览器 profile；legacy `internal_thoughts` 恒为空。 |
| D-029 | Phase 6 | Legacy 输出通过单向 LegacyExporter 保留一个版本周期；兼容层不反向污染 v2 领域模型。 |
| D-030 | Phase 6 | 新链路全量离线验收后删除旧 SessionRunner/SemanticValidator/DataSink 主链路、LegacyTaskAdapter 和无引用的 `config copy.yaml`。 |
| D-031 | Catalog v2 | Scenario 按对话策略和状态模式分类，内置场景从 2 个扩展为 11 个。 |
| D-032 | Catalog v2 | `initial_request` 与 `test_fixture` 严格隔离；fixture 只用于本地用例，永不进入远端请求、Actor Prompt 或 Judge。 |
| D-033 | Catalog v2 | `intent` 表达用户目标和优先级，`output_contract` 负责编译确定性格式、字段、数量和 URL 准则。 |
| D-034 | Catalog v2 | Criterion remediation 是失败责任、自然反馈和重试权的唯一任务级契约。 |
| D-035 | Catalog v2 | 只有 executor-owned FAIL 可继续引导；simulator/environment/user-owned 缺口不得归咎远端。 |
| D-036 | Catalog v2 | 数量单位明确为 list_items/table_rows/urls，修复 card 输出被通用 min_items 误判的问题。 |
| D-037 | Catalog v2 | 时效事实通过 `reference.as_of` 和 forbidden_assumptions 限定，不把平台、权利或版本状态写成永久真值。 |
| D-038 | Catalog v2 | 远端 Agent 未启动期间只使用本地单元、契约、Fake Provider 和 Scripted Executor 功能用例。 |

---

## 19. 实施结果

Phase 0–6 已于 2026-08-15 按本方案完成代码落地。当前生产主链路为：

```text
CLI/Bootstrap
  -> CatalogLoader/TaskCompiler
  -> BatchRunner/TaskRuntime/RunStateMachine
  -> InteractionActor + AsyncQwenPawExecutor
  -> ValidationPipeline + ToolRegistry + SemanticJudge
  -> JsonRunRepository/LegacyExporter
```

实施中的稳定化调整：

- Windows asyncio 需要 loopback socketpair，因此 pytest 网络门禁从“禁止所有 socket”改为“仅允许 `127.0.0.1/::1`”，公网仍被禁止。
- 系统 Temp 目录当前无写权，pytest basetemp 固定在工作区 `.pytest-tmp`。
- 内置 Task Catalog 已进一步迁移至 v2 envelope：58 个 Task、11 个 Scenario、0 diagnostics；旧式 `validation_rules` 已从内置数据移除。
- Playwright 只生成并锁定 npm lock，普通开发/启动没有下载浏览器。Camoufox 作为 optional dependency 进入 uv.lock，未隐式 fetch 二进制。

验证结果和非阻断项见 `docs/phase6-final-validation-report.md`；逐阶段记录见 `docs/refactor-development-progress.md`。
