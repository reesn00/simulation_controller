# 13 · A2/A3 实施设计（agent_loop 站内自主导航 · 双模灰度）

**上位文档**：[`12-agent执行器升级方案.md`](12-agent执行器升级方案.md)（阶段路线与五项拍板的单一出处，本文不重复其论证，只做 A2/A3 的细化设计与落地契约）

**性质**：**实施设计文档**。2026-10-16 owner 拍板 A2 四个决策点后成文；前置调研结论（执行侧插座盘点 + 评估判据锚点核对）已并入各节。

**硬边界（继承方案 12，重申）**：22 分支封闭集、P1/P2/P3 产物契约、fail-closed 纪律、负样本纯度三支「没判出来」分立——**一字不动**。唯一例外：P1 `action.tool` 枚举与代码动作空间的存量脱节（§5，契约文字落后于代码，须先收口）。

---

## 0. 拍板记录（2026-10-16 owner）

| # | 决策点 | 拍板 | 落点 |
|---|---|---|---|
| D-B1 | agent_loop ↔ LLM 协议形态 | **JSON 动作指令**（复用 `LLMClient` 文本通道，agent_loop 解析 `{"tool":..., "ref":..., "params":...}`；不用原生 tool_calls） | §3.1 |
| D-B2 | A2 范围切分 | **A2a（agent_loop 基建 + 任务②站内找片）先行；A2b（任务③④ B/C 内化）切后单独一刀** | §3 / §4 |
| D-B3 | P1 动作枚举 | **前置核查先行**——实锤存量脱节（契约三值 vs 代码六值），A2a 开工第一步收口 | §5 |
| D-B4 | scripted 序列 | **`visit_site` 并行保留不删**（A3 fallback 与黄金基线都要用） | §3.7 |

A3 两个开放决策点（幻觉记账口径 / D-9 是否纳入验收）见 §6.6，**开工前待拍**。

---

## 1. 现状插座盘点（2026-10-16 调研事实）

### 1.1 已就位（零改动复用）

| 基建 | 位置 | 状态 |
|---|---|---|
| Gatekeeper 三档处置 | `executor/gatekeeper.py`（389 行） | `InjectGuidance` / `EscalateModel` / `FailClosed` 均 frozen dataclass，**专为 A2 留形、当前零调用方**；`to_event()` 事件形态已定 |
| `Verdict` 封闭七值 | 同上 | NORMAL / BLOCKED_CF / CAPTCHA / LOGIN_WALL / PAYWALL / DEAD_LOOP / DEGRADED |
| `DeadLoopDetector` | 同上 | `record(url, action, body_len)` + `detect() -> "repeat_action"|"empty_body"|""`，尾部连续语义；**等 A2 每步接线** |
| PageDriver 原语 | `executor/browser/page_driver.py` | goto / snapshot / links / interactive / count / extract / evaluate / click / type_text / press_key / new_tab / close_tab / observe / close——**全齐** |
| StepLog 两段式 | `executor/actions.py` | `act(action) -> index` + `settle(index, observation)`——agent 每步动作+回执**天然就是 P1 落盘形态** |
| 工具冻结声明 | `assembler/schema.py` `ToolSpec.visible_to_model()` | 模型可见 = goto / click / fill / press 四个；new_tab / close_tab 留动作流不进声明 |
| `RunRecord.gatekeeper_events[]` | `executor/orchestrator.py` | INC47 已加，A2 直接写入（事件格式 `{stage, verdict, reason, disposition}` 沿用） |
| `_visit_guarded` 超时兜底 | 同上 | `asyncio.wait_for(visit_site, per_site_timeout_s)` 原样套 agent 任务 |
| P3 视图合成 | `assembler/views.py` | `to_openai_messages` 对 step(action)→observation 逐位渲染，**agent 原生轨迹比 scripted 事后合成更契合，核心构建不动** |

### 1.2 缺口（A2 要新建/改动的面）

| # | 缺口 | 现状 | 差距 |
|---|---|---|---|
| G1 | `llm/client.py` `chat`（L200-271）**单轮两消息**：system+user 固定，无 messages 历史、无 usage 回传（`_content_of` 把 `usage` 整体丢弃）、latency 记在感知层 Decision 而非 client | 多步对话与 llm_calls[] 账本两个需求都压在这一个函数上 | §3.5 |
| G2 | `TRAJECTORY_LLM_ESCALATE_*` 零落地（现存 5 个 `TRAJECTORY_LLM_*` 键无 ESCALATE） | 环境变量、config、切换逻辑全缺 | §3.5 |
| G3 | `tests/fakes.py` `FakeLLM` 单问单答无对话状态（按 user 正文 question 字段分发） | agent_loop 需要「按步脚本化动作」的对话后端——**新写**（坏行为开关族可复用） | §3.9 |
| G4 | `RunConfig` 无 `executor_mode`、`RunRecord` 无 `llm_calls[]` | A3 开关与账本字段 | §6.1 / §3.6 |

---

## 2. 总体架构（A2 后一次 visit 的形态）

```text
代码（不变）                     agent_loop（新）                  代码收口（不变）
──────────                      ──────────────                    ─────────────
候选分发（INC48 分类）
  → _visit_guarded 超时套壳
  → probe 可达性预检 ──────────►  任务② 站内找片
  → intercept 进站拦截            loop（步数预算 ≤ N）:              落到播放页后：
      （blocked_cf/captcha         组 prompt（历史+最新观察）         → driver.observe 复观
       → FailClosed 短路）          → LLM JSON 动作指令               → perceptor.decide(PLAYER_OK)
                                   → I6 ref 白名单校验（工具层）        → ledger 记账（22 分支）
                                   → 执行（driver 原语）               → P1/P2/P3（契约不动）
                                   → steps.act + settle
                                   → DeadLoopDetector.record/detect
                                   → intercept(新观察) 三档处置
  → agent 放弃/预算尽 ──────────►  AgentOutcome ──────────────────►  unresolved 收口
```

**职责边界（D-A5 的落地形态）**：agent 包**进站后到点击播放控件的全部导航语义段**；两端不动——进站前 probe+intercept 短路保留（硬不可达与反爬是确定性收口），点击后复观+`PLAYER_OK` 判断题保留（`video_tag_count` 确定性旁路与记账收口仍在代码）。**agent 永远不直接产出分支**——它报「到了/没到/为什么没到」，代码折算分支（§3.3）。

---

## 3. A2a 设计：agent_loop 基建 + 任务②站内找片

### 3.1 JSON 动作指令协议（D-B1）

**为什么不用原生 tool_calls**：①`chat` 单轮两消息形态被十一道判断题共用，原生 tool_calls 要求 client 层大改（payload 加 tools/tool_choice、响应解析改 message.tool_calls），侵入面大；②P3 视图由 `views.py` 合成（一步一 tool_call、id 稠密、arguments 平铺），JSON 动作指令经 Action→ActionView 渲染**与 scripted 完全同形**，P3 契约零风险；③「轨迹教的就是这套动作」靠 `visible_to_model()` 冻结声明保证，与协议形态无关。

**wire format**（agent_loop 向 LLM 请求的输出契约，随 system prompt 注入）：

```json
{"think": "简短一句话：我现在看到什么、打算做什么",
 "tool": "goto | click | fill | press",
 "ref": "e5",
 "params": {"url": "..."} ,
 "done": false,
 "report": null}
```

- `ref`：click/fill 指向的交互元素句柄，**必须来自最近一次观察回执的 interactive_elements**（I6，工具层强制校验，非提示词层）；
- `params`：goto 带 `{"url":...}`；fill 带 `{"text":...}`；press 带 `{"key":...}`；click 空；
- `done=true` 时 `report` 必填：`{"reached": true/false, "why": "..."}`——**只报告事实（到了播放页没有），不报告结论（成不成功）**，成功判定是 PLAYER_OK 判断题的职责；
- `done=false` 时 `report` 必须为 null；
- **全字段缺失/非法 JSON = 没作答**：注入一次格式引导重试，仍失败 → FailClosed（fail-closed 纪律：不猜动作）。

**对话构造**（`chat_messages` 新方法，见 §3.5）：

```text
system  = 任务指令 + 工具声明（visible_to_model 四个）+ 输出契约（上文 JSON）
user_0  = 任务上下文：目标片名 / 候选 URL / 站点标题 / 步数预算
assistant_i = 模型第 i 步输出的动作指令原文（think+tool+ref+params）
user_i  = 第 i 步观察回执：{"observation": {snapshot 摘要, links[], interactive[] 带 ref, video/audio/iframe counts}, "error": ""}
（拦截时 user_i 改为引导指令：「该站需付费已被拦截，请换站或报告放弃」——InjectGuidance 落进来）
```

### 3.2 agent_loop.py 循环结构

新建 `executor/agent_loop.py`，目标规模 ~300-400 行（方案 12 D-A1 预算 300）：

```python
async def agent_loop(
    driver: PageDriver,
    client: LLMClient,               # 主后端
    escalate: LLMClient | None,      # 升配后端（EscalateModel 处置时切换，§3.5）
    task: AgentTask,                 # 目标片名 / 候选 URL / 站点标题
    steps: StepLog,                  # 站点级动作流（挂 VisitResult.steps）
    budget: AgentBudget,             # max_steps / max_llm_calls
    gatekeeper_events: list[dict],   # 透传 RunRecord.gatekeeper_events
    llm_calls: list[dict],           # 透传 RunRecord.llm_calls（§3.6）
) -> AgentOutcome
```

每步流水线（顺序即拦截优先级）：

1. **observe**（首步或动作后）：`driver.observe(max_chars)` → `steps.settle`
2. **gatekeeper.intercept(obs, dead_loop)**：
   - `BLOCKED_CF / CAPTCHA` → `FailClosed`（一次性，agent 无登录态不可能过）→ 收口（§3.3 映射表）
   - `LOGIN_WALL / PAYWALL` → `InjectGuidance`（引导文案灌入对话，agent 重规划；**同一站点第二次同类拦截 → FailClosed**，防引导循环）
   - `DEAD_LOOP` → `InjectGuidance`（重规划指令）；**死循环第二次触发 → FailClosed**
   - `DEGRADED` → 不拦，观察降级照进对话（agent 自己看到降级事实）
   - `NORMAL` → 继续
   - 所有处置 `to_event()` 落 `gatekeeper_events`（**完全可见**，L1 已删无静默路径）
3. **组 prompt + LLM 决策**（`chat_messages`）；llm_calls[] 记账（§3.6）；解析失败 → 格式引导重试一次 → FailClosed
4. **I6 ref 白名单校验**（工具层强制）：`done=false` 且 tool∈{click, fill} 时 ref 必须 ∈ 最近观察 interactive_elements 的 ref 集；不过 → 注入引导重试一次 → 仍不过 FailClosed（**编 ref 是不可逆错误，不执行**）
5. **预算检查**：步数 / llm_calls 任一耗尽 → FailClosed（`_BudgetExhausted` 口径）
6. **执行**：`done=true` → 返回 AgentOutcome；否则按 tool 分发 driver 原语（goto→`driver.goto`、click→`driver.click(ref=...)`、fill→`driver.type_text(ref=..., text=...)`、press→`driver.press_key(...)`）→ `steps.act(Action(...))`
7. **DeadLoopDetector.record(url, action, body_len)**（每步一次）

**确定性纪律（方案 12 §5.3 第 1 条的落地）**：步数预算、终止判定、处置映射、事件落盘全部在代码——agent 的非确定性被限制在「页面内选哪个元素、goto 哪个链接」这一段语义决策。

### 3.3 分支收敛映射（agent 结论 → 22 分支）

**核心规则：agent 永不直接产出分支。** `AgentOutcome` 只有三种形态，代码折算：

| AgentOutcome | 折算 | 落分支 | 理由 |
|---|---|---|---|
| `reached=True`（agent 报到了播放页） | 复观 + `PLAYER_OK` 判断题照旧 | 成功 / `player_unverified` / PLAYER_OK 负分支 | 成功判定与记账收口留在代码 |
| `reached=False, reason=...`（agent 自报放弃） | **一律 `unresolved`** | `unresolved` | **无证据不记负样本**——agent 说不清的不入负样本池（三支「没判出来」纪律沿用） |
| `FailClosed(verdict, reason)`（gatekeeper/预算/I6 收口） | 按 Verdict 查映射表 | 见下 | 拦截事实是代码判的确定性证据，可记负分支 |

FailClosed Verdict → 分支映射表（**封闭枚举，双向钉死**）：

| Verdict / 来源 | 分支 | 记账层 |
|---|---|---|
| LOGIN_WALL | `login_required` | 负样本（有拦截证据） |
| PAYWALL | `subscription_wall`（经付费墙三判据复核，弱信号不拦） | 负样本 |
| BLOCKED_CF / CAPTCHA | `blocked` | 负样本 |
| DEAD_LOOP（二次） | `unresolved` | 不入负样本（行为失败 ≠ 页面负样本） |
| 预算耗尽 / I6 违规 / 格式连续失败 | `unresolved` | 不入负样本 |
| `video_tag_count` 旁路、probe 硬不可达 | 照旧 scripted 口径（`unreachable_hard` 等代码分支） | CODE_ONLY_BRANCHES 不变 |

> ⚠️ 映射表放 `agent_loop.py` 模块级常量（如 `_FALCLOSED_BRANCH`），**不进 `branches.py` 也不进 `questions.py`**——它是 agent 模式的执行层知识，与感知层 REGISTRY 无关；d5_scorer 侧 CODE_ONLY_BRANCHES 若需放行新组合走 INC 逐条核（A2a 验收时跑全量门禁盯 `Test同源`）。

### 3.4 gatekeeper 接线细则

- **进站第一步**：`probe + intercept` 在 agent_loop **之前**由 `_visit_guarded` 侧完成（复用现状）——反爬/硬不可达短路不掉进 agent 对话，省 llm_calls；
- **站内每步**：agent_loop 内每 observe 一次 intercept 一次（§3.2 第 2 条）；
- **InjectGuidance 的对话注入**：引导文案作为 user 消息进入对话历史（不是 system 重写）——**轨迹里可见「被拦→引导→重规划」完整链**，这是 D-A2 删除 L1 的本意：被拦的正确行为本身就是要教的训练信号；
- **EscalateModel 触发条件（A2a 最小版）**：连续 2 次格式解析失败或 I6 违规——「模型能力不够」的信号；触发后**当前候选剩余步骤**切 escalate client（下一候选回到主后端，不全局切换）。

### 3.5 llm/client.py 扩展（G1+G2）

**不动 `chat`**（十一道判断题 + FakeLLM + 全部感知层测试零改动），新增：

```python
@dataclass
class ChatResult:
    content: str | None
    usage: dict | None        # {"prompt_tokens": int, "completion_tokens": int}，后端没给就 None
    latency_ms: int
    error: str = ""           # 与 last_error 同格式：transport:XXX / http:XXX / shape:XXX

def chat_messages(self, messages: list[dict], *, max_tokens=None) -> ChatResult
```

- `messages` 透传 OpenAI 形态（role/content），**不加 tools/tool_choice**（JSON 动作指令不需要）；
- `_usage_of(data)`：解析 `data["usage"]` 的 prompt/completion tokens（client 当前把整个 data 只取 content，usage 顺手救回来）；
- FakeLLM 侧补同签名方法（§3.9）。

**ESCALATE 第二后端（G2）**：

- 环境变量：`TRAJECTORY_LLM_ESCALATE_BASE_URL / _MODEL / _API_KEY`（全部可选；**未配 = 无升配**，EscalateModel 处置降级为记事件不切后端）；
- `LLMConfig.from_env` 加 `escalate: LLMConfig | None` 类方法或 factory 层构造两个 client；
- agent_loop 拿 `escalate: LLMClient | None`（§3.2 签名），None 时 EscalateModel 处置只落事件——**升配是能力不是依赖**，未配置不 fail。

### 3.6 llm_calls[] 账本

`RunRecord` 新字段 `llm_calls: list[dict]`（**不升 schema_version**——可选字段纪律，gatekeeper_events 同款先例）：

```json
{"ts": "...", "caller": "agent_loop", "candidate": "https://...",
 "model": "...", "latency_ms": 123, "prompt_tokens": 456, "completion_tokens": 78,
 "error": ""}
```

**A2a 记账范围决策**：只记 agent_loop 调用（新面顺手）。感知层十一题的调用**不汇入**——Decision 已有 latency_ms，把账本实例传进 perceptor 是对感知层的侵入，收益（token 数）要改 client 返回形态才能拿到，**列为 O 阶段（Langfuse 前置）扩展项**。账本的意义在「agent 批次成本可比」而不是「全管线审计」，先够用。

### 3.7 visit_site 替换点与 scripted 保留（D-B4）

- `visit_site`（`executor/steps/visit.py` L65-268，七段固定序列）**不删不改**——A3 fallback 与黄金基线；
- 接线方式：`_visit_guarded` 内按 `RunConfig.executor_mode`（§6.1，A2a 先埋常量 `"scripted"` 默认 + 私有环境变量开关，A3 转正为 RunConfig 字段）分派到 `agent_visit(...)` 或 `visit_site(...)`；
- `agent_visit`：薄壳（~80 行）——probe 预检 + agent_loop + 落播放页后复用 visit.py 尾部的复观/PLAYER_OK/记账段（**该段抽函数共用，不复制**）；
- `VisitResult` 字段完全复用（`steps` 就是 agent_loop 写满的 StepLog）——P1 落盘对下游零差异。

### 3.8 任务② prompt 设计要点

- system：任务目标（「在这个站点找到《片名》的播放页，点击播放控件」）+ 工具四件套说明 + 输出契约（§3.1 JSON）+ 纪律三句（ref 只能用观察回执里出现过的；找不到就如实报告放弃；不猜 URL）；
- 观察回执裁剪：snapshot 正文截断（复用 `max_chars` 预算）、links/interactive 上限沿用 `PERCEPTION_LINK_WINDOW` 口径——**agent 与判断题看到的页面同一份投影**，观察口径不分叉；
- 站内搜索场景（首页无片名直接入口）：agent 自主走 fill+press 搜索框路径——这正是 agent 化相对 scripted 固定序列的增量收益（scripted 的 site_search 是专用代码路径，agent 把它变成涌现行为）。

### 3.9 测试策略（G3）

新建 `FakeAgentLLM`（放 `tests/fakes.py`，复用坏行为开关族形态）：

- `scripted_actions: list[dict]`——按步返回动作指令（**测试不靠碰巧的 URL 措辞**，与 FakeLLM 的 classify 表同哲学）；
- 坏行为开关：`bad_json`（输出散文）、`invent_ref`（编 e99）、`never_done`（死循环到预算耗尽）、`wrong_then_recover`（错一步后引导重试成功——**「被拦→修复→重试」是训练信号本身，必须有测试钉住这条链完整落轨迹**）；
- 接口与 `chat_messages` 同签名。

单元测试面（估 40-60 例）：

| 类 | 覆盖 |
|---|---|
| 协议解析 | JSON 各字段缺失 / bad_json 重试 / 重试仍败 FailClosed / done+report 配对约束 |
| I6 | 编 ref 拒绝执行 + 引导重试 + 仍编 FailClosed / 合法 ref 放行 |
| gatekeeper 接线 | LOGIN_WALL 首次 InjectGuidance（对话可见）+ 二次 FailClosed / DEAD_LOOP 同款 / BLOCKED_CF 一步收口 / 事件全落 gatekeeper_events |
| 预算 | 步数耗尽 / llm_calls 耗尽 → unresolved 收口 |
| 分支收敛 | §3.3 映射表逐行（reached→PLAYER_OK 链 / 放弃→unresolved / 各 FailClosed→对应分支） |
| 升配 | EscalateModel 触发切换 / escalate=None 降级记事件 |
| 账本 | llm_calls[] 记账字段 / usage 缺失容错 |
| 端到端 | agent_visit 全链：FakeAgent 导航到播放页 → PLAYER_OK → P1/P2/P3 重建一致（含 P3 tool_calls 形态与 scripted 同形断言） |
| 契约漂移 | `TestToolSpaceDrift` 扩展：agent_loop 实际发出的 tool 集 ⊆ TOOL_SPECS.visible_to_model() |

---

## 4. A2b 设计：任务③④（B/C 路径内化）

**切分理由（D-B2）**：③④ 牵动 `link_check.py` / `site_search.py` 与 B/C 六道感知题内化，与 agent_loop 基建正交，单独一刀风险可控；A2a 落地后任务③④是「同一循环换任务指令」的增量。

- **任务③（聚合站探索）**：聚合站落地页 → agent 自主找片/聚合栏 → 进详情页；`SELECT_SITE_ENTRIES` / `IS_AGGREGATOR` 语义内化进任务指令，`negative_branch` 知识转 §3.3 同款收敛映射（`not_aggregator` 双产地口径不变——INC48 的 `questions_of_branch` 复数归属已兼容）；
- **任务④（文章找引荐链接）**：文章页 → agent 找视频站引荐链接 → goto；`SELECT_REFERRAL_LINKS` 内化；**与 scripted link_check 并存期**：A2b 灰度期间 B/C 路径仍是 scripted 主跑（executor_mode 全局一个开关，不分路径——分路径开关是过度设计，A3 真有需要再切）；
- `max_intermediates` / `max_referral_links` 预算沿用（外层调度不动，agent 在单站点内的步数预算是新增的**第二层**预算，两层互不侵蚀）；
- ROUTED 等价类口径：A2b 验收时跑 golden alignment 同源测试复核。

---

## 5. P1 动作枚举前置核查（D-B3，A2a 第一步）

**实锤存量脱节**（2026-10-16 核查）：

- 契约 [`P1-observation.md` L150](../contracts/P1-observation.md)：`action.tool` 封闭 **`goto / click / new_tab` 三值**；
- 代码 [`actions.py` L70](../../executor/actions.py)：`TOOLS = ("goto", "click", "new_tab", "fill", "press", "close_tab")` **六值**——fill/press 自路径 B/C 批次（site_search）起真实在用，P2/P3 链路已渲染（INC45 的 P3 重建含 fill/press 步），**只有 P1 契约文档文字没跟上**。

**收口动作（A2a 第一个 INC）**：P1-observation.md `action.tool` 行改六值 + `params` 行补 fill/press 形态（fill 是 `{"text":...}`、press 是 `{"key":...}`）+ target 语义补全（fill 可带目标）。**这是契约文字向代码事实对齐，不是契约变更**——P2/P3 契约与下游渲染早已支持，无需提案流程；契约测试若有三值硬断言（核查 `test_p2_contract`）同步更新。

**对 A2 的意义**：agent 的四工具（goto/click/fill/press）全部落在已存档的真实动作空间内——「控制流哪天真的发了新原语，这里跟着加——加的时候那条原语就有真实存档兜底」（actions.py 注释原话），A2 不引入任何 P1 未见过的新动作。

---

## 6. A3 设计：双模灰度与评估收口

### 6.1 executor_mode 双模

- `RunConfig.executor_mode: Literal["scripted", "agent"] = "scripted"`（**灰度只在构造期**——run 子命令加 `--executor-mode`，proberun 不加，沿用 discovery-path 先例）；
- `RunRecord.executor_mode` 分开记账（比对时永远知道这批是 agent 批——方案 12 §5.3 第 4 条）；
- **agent 失败 fallback**：`agent_visit` 内部不 fallback（一个候选一种模式，混合轨迹不可归因）；批次级 fallback 由 A/B 双跑本身承担（agent 批的 unresolved 率就是决策数据）。

### 6.2 goto URL 溯源断言（批次级，A3 最大单项）

**失效机理**：scripted 下「goto 的 URL 与候选对应」是代码编排保证的恒真式，批次断言只需比对；agent 下 goto 是 LLM 决定的，URL 可能来自候选链接、站内链接、**也可能幻觉编造**——断言从「比对两个代码产物」变成「核查 LLM 输出的可溯源性」。

**设计**（新批次级检查器，`evaluation/` 侧新文件或 batch_assertions 扩展）：

- 断言：每个 `origin=model` 的 goto 动作，其 `params.url` ∈ **候选池 ∪ 该轨迹内已观察 links[].href**（锚点集合随轨迹递推——第 N 步的合法 goto 集包含前 N-1 步观察到的全部链接）；
- 违反产出：批次级统计报告（幻觉 goto 计数 / 明细），**不进 22 分支封闭集**（样本级分支记账一字不动——幻觉是批次质量信号，不是站点失败原因）；
- 例外口径：`done=true` 前的最后 goto 若是站内相对 URL 解析结果，以 `landed_url` 归一后核（domain+path 前缀匹配，容忍重定向）。

> **待 owner 拍板（A3 开工前）**：幻觉 goto 是否要反映进 D-2 样本级（「动作引用实体在前置观察中可找到」口径天然覆盖 goto URL 溯源——goto 的 params.url 算不算「动作引用实体」）。倾向：D-2 保持只核 click/fill 的 target（现有口径），URL 溯源独立成批次级——两个信号分开放，D-2 分数跨模式可比。

### 6.3 D-2/D-5/D-6 兼容性（调研结论：天然兼容 + 守卫钉住）

| 判据 | 结论 | 守卫动作 |
|---|---|---|
| D-2（code_scorer） | click/fill target 前置观察核查与「谁决定动作」无关；不可判定判据依赖的 `elements_total`/`limits` 留痕在 agent 模式照常产出（观察采集层不动） | 守卫测试：FakeAgent 轨迹跑 D-2 得 found/missing 正确分类 |
| D-5（d5_scorer） | 只锚 `outcome.(branch/question/reached_play_page)`；`QUESTION_TO_BRANCHES` 动态推导自 REGISTRY，agent 化判断点自动同步；§3.3 映射表的 FailClosed 分支组合逐条过 `Test同源` | 全量门禁即守卫（A2a 验收含） |
| D-6 | 只读 outcome 与观察正文，零动作流依赖 | 无需 |
| P3（views.py） | agent 原生轨迹（action=tool_call 形态 + 原生 think）比 scripted 事后合成更契合；`TestClosingDispositionDrift` 盯分支-处置映射漂移照常有效 | 端到端测试断言 P3 形态与 scripted 同形 |

### 6.4 D-9 连带核对（若纳入验收）

原生 CoT 的时点关系与 scripted 相反：scripted rationale 是「看过观察后写」的总结，agent 的 think 是「看观察→思考→决定动作」——d9/gates 的时序核查与 goto 首步早退逻辑要按新首步类型重核。**A2a 的 think 字段就是原生 CoT，P2 rationales 按 step_index 对齐逻辑已兼容**；D-9 判据修订是独立工作量，**建议不进 A3 验收（agent 批次先不产 D-9 分），列为后续 INC**——待 owner 拍板。

### 6.5 mutation_check 口径

功能不受影响（开发期守卫验证工具，不跑业务批次）；受影响的是重跑可比性假设（「结论可从 task_id 纯函数重算」在 agent 批次不成立）。**落成文**（mutation_check 文件头 + 04-运行手册）：mutation_check 只对 scripted 批次承担守卫职责；agent 批次重跑可比性 = executor_mode 分批记账 + 归因锚点存档全保真（方案 12 §5.3 第 3 条），A/B 口径从「单次重跑」改为「同 task N 次重跑取分布」。

### 6.6 黄金集 A/B 双跑（验收主体）

- 基建现成：`evaluation/golden/builder.py` 冻结黄金集版本 → 跑批次 → EQUIV_CLASS 等价类算 D10 一致率——scripted/agent **分别跑批、分别算，无需新建对比工具**；
- 验收四指标：①D10 等价类一致率 agent 批不降（主要口径）；②D-2/D-5/D-6 得分不降；③agent 步数预算内完成率（导航到播放页的比例）；④unresolved 簇占比对比（方案 12 §3 的针对性主张：多步自主导航对 body 反爬降级 + JS-heavy 失败两类 unresolved 的修复效果）；
- **A1 遗留收口**：「①判断准确率 A/B 对比」与 golden alignment ROUTED 口径复核一并在此验收；
- 真实批次迭代轮次（prompt 调优 × 站点时变）是两端最大的不可控项——批次验收与单元门禁分开记账，单元全绿不等于批次达标。

### 6.7 文档收口

- 架构总览第 5 行立场表述按 D-A5 修订（「LLM 是传感器不是驾驶员」→「agent 是驾驶员，代码是护栏…」）——**A2 落地即修**（方案 12 风险清单钉过）；
- 01-架构总览判断点表加 agent 模式行、06-端到端流程加 agent_visit 分支段、90-编年史 INC 入册、本文档标落地注——A2a/A2b/A3 各一个 INC。

---

## 7. 工作量分解与落地顺序

| 阶段 | 任务 | 规模 | 依赖 |
|---|---|---|---|
| **A2a-0** | P1 动作枚举收口（§5） | 契约文档 3 行 + 契约测试核对 | 无 |
| **A2a-1** | `chat_messages` + usage + FakeLLM 扩展 | client ~80 行 + fake ~60 行 | 无 |
| **A2a-2** | `agent_loop.py` 循环体 + 分支收敛映射 | ~300-400 行 | A2a-1 |
| **A2a-3** | `agent_visit` 薄壳 + visit.py 尾段抽函数共用 + executor_mode 埋点 | ~100 行 + visit.py 重构 | A2a-2 |
| **A2a-4** | llm_calls[] + ESCALATE 环境变量 + factory | ~100 行 | A2a-1 |
| **A2a-5** | 测试：FakeAgentLLM + 单元 40-60 例 + 端到端 | fake ~200 行 + 测试主体 | A2a-1..4 |
| **A2a-验收** | 全量门禁 + 文档（架构总览第 5 行 D-A5 修订） | 门禁全绿 + INC 入册 | 全部 |
| **A2b** | 任务③④ + B/C 感知题内化 + ROUTED 复核 | ~200-300 行 + 测试 | A2a |
| **A3-1** | executor_mode RunConfig 转正 + 记账 | ~80-120 行 | A2a |
| **A3-2** | goto URL 溯源断言（批次级） | ~150-250 行（新逻辑） | A2a（动作流形态定） |
| **A3-3** | D-2/5/6 守卫测试 + D-9/D-2 口径拍板落地 | 15-25 例 | A2a |
| **A3-4** | 黄金集 A/B 双跑 + 四指标验收 | 代码 ~0，批次迭代 | A2b |
| **A3-5** | 文档收口（mutation 口径 / 编年史 / 本文档落地注） | 文书 | A3-4 |

**可并行项**：A3-2（溯源断言）只依赖动作流形态确定，可与 A2b 并行前研；A2a-1 与 A2a-4 无依赖可同刀。

**合计口径**（与 2026-10-16 评估一致）：A2 全程 ~900-1300 行 + 40-60 例；A3 ~300-500 行 + 15-25 例；批次 A/B 迭代轮次为最大不可控项。

---

## 8. 风险清单（继承方案 12 §7 + 本文新增）

| 风险 | 影响 | 缓解 |
|---|---|---|
| 真实 LLM prompt 迭代轮次不可控 | A2a 验收延期 | FakeAgent 全链先钉行为契约；真实批次调试与单元门禁分开记账 |
| agent 编 ref 点错不可逆 | 污染存档、误伤真实站点 | I6 白名单工具层强制（§3.2 第 4 条），编 ref 根本不执行 |
| 引导循环（InjectGuidance 反复触发同一拦截） | llm_calls 烧预算无进展 | 同站第二次同类拦截 → FailClosed（§3.4） |
| 双轨并存期口径打架（词表救济链 vs agent 语义判定） | 同一页面两套结论 | 仲裁规则不变（确定性事实优先）；gatekeeper 唯一拦截出口；A2a 期间 agent 批与 scripted 批分开记账可比 |
| P1 契约三值枚举若被测试硬钉 | A2a-0 收口动作碰红 | 核查 test_p2_contract 实际断言集——代码六值已是存档事实，契约文字对齐是纠偏不是变更 |
| agent 批次 D-9 缺分被误读为质量下降 | 验收口径混乱 | 6.4 拍板后写死：agent 批不产 D-9（或产但单独口径），验收四指标里不含 D-9 |

---

## 相关文档

- [`12-agent执行器升级方案.md`](12-agent执行器升级方案.md) —— 阶段路线与五项拍板（上位文档）
- [P1-observation 契约](../contracts/P1-observation.md) —— §5 收口对象
- [01-架构总览](../01-架构总览.md) —— §6.7 修订对象（第 5 行立场表述）
- [90-增量编年史](../计划进展/90-增量编年史.md) —— A2a/A2b/A3 各入一册
