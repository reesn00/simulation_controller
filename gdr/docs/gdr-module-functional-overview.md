# GDR 模块功能梳理

> 本文是对 `gdr/` 子项目各模块的代码级功能梳理，配套 [gdr-mvp-design.md](gdr-mvp-design.md) 与 [README.md](../README.md) 使用。当前实现已经脱离最初设计中的 llama-cpp-python 本地加载，改为通过 HTTP OpenAI 兼容端点（vLLM / llama.cpp server / Ollama）调用外部 LLM，但模块边界与流水线骨架未变。

---

## 0. 模块定位

`gdr/` 是独立子项目（`pyproject.toml` 中 `name = "gdr-agent"`），对 QwenPaw 平台导出的 Agent 会话轨迹做"脏数据入、干净数据出"的自动精修。三级模型：`Session → Message → Block`（thinking / toolcall / toolresult / text）。

入口脚本：

```bash
gdr-pipeline    # 主编排：单文件 / 批量 / 多进程 Pool
gdr-evaluator   # 评估闭环：build-pairs / train-probe / evaluate / feedback-loop / full
```

---

## 1. 数据模型（[domain/schema.py](../domain/schema.py)）

| 层级 | 字段 | 说明 |
|---|---|---|
| `Session` | session_id / messages / reply_context / metadata | 一次完整 Agent 交互 |
| `Message` | role / id / blocks / metadata | 单条 user 或 assistant 消息 |
| `BlockUnion` | ThinkingBlock \| ToolcallBlock \| ToolresultBlock \| TextBlock | 4 种原子块，按 `type` 字段 discriminator 区分 |

辅助模型：`DefectTag`（13 种 StrEnum）、`BlockRefineRecord`（精修记录）、`ValidationResult`（L1/L2/L3 验证结果）、`MessageHealth`（消息级健康分）、`RefineLogEntry`（单次精修尝试日志）。

加载/落盘：`load_session` / `save_session` / `locate_block` 三个工具函数。`_parse_blocks` 在加载时把 dict 统一升格为 Pydantic 模型，减少下游分支。

---

## 2. 13 种缺陷标签与检测方式

| 类别 | 标签 | 检测方式 |
|---|---|---|
| 思考链 | `THOUGHT_TOO_SHORT` | 规则层：`len < thought_min_len`（默认 20） |
| 思考链 | `THOUGHT_TOO_LONG` | 规则层：`len > thought_max_len`（默认 500） |
| 思考链 | `THOUGHT_BROKEN_LOGIC` | LLM 3-票投票（异构上下文窗口） |
| 工具调用 | `TOOL_JSON_INVALID` | 规则：`json.loads(input)` 失败 |
| 工具调用 | `TOOL_HALLUCINATED` | 规则：`name` 不在 `tools.yaml` 白名单 |
| 工具调用 | `API_HALLUCINATION` | 规则：`hallucinated_apis` 黑名单子串匹配 |
| 工具调用 | `TOOL_WRONG_SELECTION` | LLM 3-票投票（异构上下文窗口） |
| 工具调用 | `REPETITIVE_CALL` | 规则：连续 ≥3 次同名 toolcall + SequenceMatcher > 0.9 |
| 工具调用 | `CONTEXT_SWITCH_LOOP` | 规则：browser ↔ execute_shell_command 切换 ≥3 次 |
| 观测 | `OBS_NOISE` | LLM 3-票投票（异构上下文窗口） |
| 观测 | `OBS_DEBUG_LEAK` | 规则：`_NOISE_PATTERN` 关键词（DEBUG/Traceback/FATAL/ModuleNotFoundError…） |
| Text 块 | `TEXT_FACT_HALLUCINATION` | 改进1：text 中数值/平台名未在前序 toolresult 出现 |
| 消息级 | `MESSAGE_UNHEALTHY` | 改进2：消息级健康分 < `message_health_min_ratio`（0.3） |

---

## 3. 目录结构与各包职责

```
gdr/
├── config/        # Settings (pydantic-settings, GDR_ 前缀) + tools.yaml 加载
├── domain/        # Pydantic 数据契约：Session/Message/Block*/DefectTag
├── infrastructure/# HTTP LLM 客户端 (LlamaCppClient) + 双通道日志
├── routing/       # Router：规则层 + LLM 3-票异构上下文投票 + 健康分
├── pipeline/      # 主编排：单文件 / 批量 / 多进程 Pool
├── context_understanding.py  # 上下文理解：active window + 分级 archive + 引用图 (P0)
├── policy.py      # 决策层：defect × context → REPAIR/PRUNE/DEFER 策略
├── refiners/      # 3 大精修器：thought_refactor / tool_fixer / obs_denoiser
├── validators/    # L1 规则 / L2 嵌入语义 / L3 LLM 裁判 三级验证
├── reassembly/    # 一致性终检 + block 剪枝 + 重写 + 落 metadata
├── data/          # 程序化构造 SFT 训练对 (broken→correct / broken→broken)
├── evaluator/     # 闭环：探针训练 + 双维评测 + 反馈回路
├── prompts/       # 5 个 YAML 模板：thought/tool/obs/judge/reassembler
└── docs/          # 设计文档（本目录）
```

各包通过 `__init__.py` 重新导出顶层符号，保持向后兼容（旧 `from config import Settings`、`from refiners.base import LlamaCppClient` 等仍可用）。

> **新增顶层模块**：`context_understanding.py` 与 `policy.py` 直接放在 `gdr/` 根目录（与 `pipeline/cli.py` 等入口脚本同级），不放在子包里——它们是流水线横向能力，被多个步骤共享使用。

---

## 4. 流水线主链路（[pipeline/runner.py](../pipeline/runner.py)）

```
load_session → Router.tag → ContextUnderstanding → Policy.decide → 精修/PRUNE/DEFER → reassemble → save_session
                                                  ↓
                                       validate_block (L1/L2/L3) → success / failed
```

### `process_one(session, cfg, tool_names, hallu_apis)`

1. **Router.tag**：产出 `defects_index: {block_id → [DefectTag...]}` 和 `health_scores: [MessageHealth]`。
2. **ContextUnderstanding.build**（P0 新增）：构建双层上下文（active window + 分级 archive + 引用图），为后续决策提供上下文感知。
3. **跳过不健康消息**：`health_score < message_health_min_ratio` 的整条 assistant 消息短路，避免无意义精修。
4. **Policy.decide**（P0 新增）：对每条有 defect 的 block，调用 `decide_policy(block, defects, view, retry_exhausted, cfg)` 选策略：
   - `REPAIR_IN_PLACE` → 调用对应精修器
   - `PRUNE_BLOCK` / `PRUNE_WITH_PAIR` → 加入 `prune_block_ids` 集合，跳过 refiner
   - `PRUNE_MESSAGE` → 标记，由 reassembler 处理
   - `DEFER_TO_HUMAN` → 加入 `deferred_block_ids` 集合，跳过 refiner（保留 block 原文 + 标记待复核）
5. **精修 → validate_block**：对走 REPAIR 的 block 做 L1/L2/L3 验证，写入 `BlockRefineRecord`。
6. **reassemble**：剪枝（REPETITIVE / SWITCH_LOOP / policy-driven prune）+ 一致性终检 + 落 metadata（`policy_decisions` / `deferred_blocks` / `context_stats`）。

### 无缺陷快速通道

如果 router 完全没标任何缺陷，跑一次 `_l1_sanity_check`（toolcall JSON 合法性 + 工具白名单 + thinking 长度）防止漏检；通过则原样返回 session。

### 多进程模式

- `workers > 1` 时用 `multiprocessing.get_context("spawn")` 起 Pool（Windows/Linux 通用，模型不跨进程共享）。
- `_worker_init` 在子进程内独立 `setup_logger` + 模型缓存。
- 批量输出 `<batch_output_dir>/_batch_report.json` 汇总统计（success / discard / error / kept_ratio）。

---

## 5. Routing 层（[routing/router.py](../routing/router.py)）

### 规则层

| 方法 | 输入 | 输出 DefectTag |
|---|---|---|
| `_rule_layer_think` | ThinkingBlock | TOO_SHORT / TOO_LONG |
| `_rule_layer_toolcall` | ToolcallBlock + 工具白名单 + 黑名单 API | JSON_INVALID / HALLUCINATED / API_HALLUCINATION |
| `_rule_layer_toolresult` | ToolresultBlock | OBS_DEBUG_LEAK |
| `_rule_layer_text` | TextBlock + 前序 toolresult 列表 | TEXT_FACT_HALLUCINATION（改进1） |
| `_rule_layer_message` | 全消息 blocks | REPETITIVE_CALL / CONTEXT_SWITCH_LOOP |
| `_message_health_score` | 全消息 blocks | `MessageHealth`（改进2） |

**消息级健康分公式**（[router.py:185](../routing/router.py#L185)）：

```
success_ratio = success_toolcalls / total_toolcalls
failure_penalty = min(failures_before_first_success / max_failures_before_success, 1) × 0.4
loop_penalty   = 0.3 if has_repetitive_loop else 0
switch_penalty = 0.3 if has_context_switch_loop else 0

health_score = max(0, success_ratio − failure_penalty − loop_penalty − switch_penalty)
is_healthy   = health_score ≥ message_health_min_ratio (0.3)
              且 failures_before_first_success ≤ max_failures_before_success (8)
```

### LLM 投票层（`_llm_layer`）

`tag()` 在规则层跑完后，对"被规则层命中缺陷的 `thinking`/`toolcall`/`toolresult` block"（不健康消息的 block 跳过）调一次 `_llm_layer`，对每条候选 block 做 **3 次独立 LLM 调用**，得到 `votes: list[bool]`。

**3 次调用各自的输入不同** —— 这是关键设计。`cfg.llm_vote_context_strategies`（默认 `["none", "±1", "pre2_post1"]`）控制每次投票能看到的相邻上下文：

| 策略 | 前置 block | 后置 block | 用途 |
|---|---|---|---|
| `none` | 0 | 0 | 裸看：只看 block 本身 |
| `±1` | 1 | 1 | 局部窗口：判断前后连贯性 |
| `±2` | 2 | 2 | 较大窗口 |
| `pre1_post2` | 1 | 2 | 偏后置：看后续影响 |
| `pre2_post1` | 2 | 1 | 偏前文：看决策来源 |

Surrounding 拼接格式：`[前 N \| type#id] \n content`，按策略名查 `_VOTE_STRATEGY_SPAN` 表得到 (前向数, 后向数) 后取 session 对应索引范围内的 block；超出 `cfg.llm_vote_max_context_chars` 时尾部截断并追加 `...(truncated)`。无效策略值（如拼写错误）降级为 `none`，不抛错。

**投票规则**：

- 解析失败 / 超时 → 弃权（不计入有效票）；
- 有效票 `< 2` → 不标记（信号不足）；
- 有效票中 ≥ 2 票 `has_defect=True` → 按 block_type 标记 `THOUGHT_BROKEN_LOGIC` / `TOOL_WRONG_SELECTION` / `OBS_NOISE`；
- 标记结果合并进 `defects_index`，由 runner 走 refiner / policy。

**为什么 3 次不同上下文**：LLM 在判断"逻辑断裂 / 工具选错 / 观测噪声"这类语义问题时，**单次投票的输入对最终结论影响远大于 temperature**。如果 3 次请求文本相同，9B 模型大概率给出同 token 的相同回答，majority-vote 等价于单次投票，对系统性偏差零修正。换成 3 种上下文窗口后，LLM 在不同证据基础上独立判断，**既保留 majority-vote 对单次解析错误的容错，又获得输入侧多样性对系统性偏差的稀释**。这是"3 票 + 弃权 + 阈值 2 + 输入异构"的组合设计。

---

## 6. 三大精修器（[refiners/](../refiners/)）

通用模式：`max_retries_9b` 次主模型（9B）调用 → 失败升级 `tool_model`（32B）一次 → 还失败返回 `None` 由上层丢弃。

### [thought_refactor.py](../refiners/thought_refactor.py)

- 提示词：`prompts/thought.yaml` (task 模板)。
- 守恒校验：`_extract_entities` 从原文抽取引号字符串 / CamelCase / 工具名 / 字段名，refined 必须满足 `orig_entities ⊆ new_entities`（实体守恒）。
- 长度约束：`[thought_min_len, thought_max_len]`，默认 20–500。
- 失败原因：`length out of range` / `entity loss: {...}` / `empty refined_thought`。

### [tool_fixer.py](../refiners/tool_fixer.py)

- 提示词：`prompts/tool.yaml` (system + user)。
- 使用 `response_format: json_schema` 强制结构化输出 `{name, input}`（`OUTPUT_SCHEMA`）。
- 三道硬约束：① 工具名在白名单 ② `input` 必须可 JSON 解析 ③ 不能含黑名单 API 子串。
- 失败原因：`tool name not in allowed list` / `repaired input is not valid JSON` / `hallucinated API still present`。

### [obs_denoiser.py](../refiners/obs_denoiser.py)

- 提示词：`prompts/obs.yaml` (system + user)。
- 硬约束：
  - 输出非空；
  - 压缩率 `len(text)/len(original) ≤ max_compression_ratio`（默认 0.5），防止 LLM 把有效信息全删；
  - 不含 Markdown（` ``` `、`###`、`**`、`---`）；
  - 不是 JSON（不以 `{`/`[` 开头）。
- 失败原因：`empty output` / `compression ratio ... exceeds` / `contains markdown formatting` / `appears to be JSON`。

---

## 7. 三级验证（[validators/](../validators/)）

### L1 规则（[l1_rules.py](../validators/l1_rules.py)）

- thinking：非空、`len ≤ thought_max_len_l1`、实体守恒；
- toolcall：name 在白名单、`input` 是合法 JSON；
- toolresult：非空、不含 `_NOISE_PATTERN`。
- L1 不通过 → **直接 fail**，不上 L2/L3。

### L2 嵌入语义（[l2_semantic.py](../validators/l2_semantic.py)）

- HTTP 嵌入端点（默认 `http://127.0.0.1:8086/v1`，模型 `v5-nano-retrieval`，768 维；调用方为 `infrastructure.http_embed.HttpEmbedder`，调 `/v1/embeddings`）计算 `cosine_similarity(orig, refined)`。已下线 BGE-M3 本地权重方案。
- 阈值：thinking 0.85 / toolcall 0.90 / toolresult 0.80。
- **fail-close**：sentence-transformers 缺失时不默认通过，而是降级到 L3。

### L3 LLM 裁判（[l3_judge.py](../validators/l3_judge.py)）

- 仅在 L2 失败时被触发，调用 `judge_model`（默认 32B）。
- 解析 JSON `{verdict, score, reason}`，通过条件：`verdict == "pass" && score ≥ 7`。

### 验证流程（[validators/__init__.py](../validators/__init__.py)）

```
L1 fail → return False
L2 pass → return True
L2 fail → 触发 L3 judge → return L3 result
L2 模块缺失 + L3 enabled → 降级跑 L3
L2 模块缺失 + L3 disabled → return False
```

---

## 8. Reassembly（[reassembly/reassembler.py](../reassembly/reassembler.py)）

### Block 剪枝（改进3）

1. **REPETITIVE_CALL**：连续同名 toolcall + 高度相似 input → 保留第一个，删除后续（连带删除对应 toolresult，toolcall/toolresult 共享 id）。
2. **CONTEXT_SWITCH_LOOP**：browser ↔ shell 切换对 ≥ 阈值 → 保留前 `threshold - 1` 对，删除后续。

剪枝后会同步过滤 `refine_records`，确保落盘 metadata 与实际 block 一致。

### Block 重写

按 `BlockRefineRecord.refined_content` 把对应字段写回 block（thinking / name / input / output_text），兼容 dict 与 Pydantic 实例。

### 一致性终检

调用 `judge_model` 对整体 session 打 0–10 分：

- `score ≥ 7` → 通过；
- 失败且 `strict_consistency=True` → **整条 session 丢弃**（设计上的硬底线）；
- 失败且 `strict_consistency=False` → 警告并继续（不建议生产，Settings 启动时打 WARN）。

### 元数据落盘

在 `session.metadata` 中写入：

- `refine_history`：每条 record 的 module / attempts / model_used / result；
- `validation_summary`：total / modified / passed_L1 / passed_L2 / passed_L3 + 各层失败数；
- `modified_blocks`：成功的 block_id 列表；
- `policy_decisions`（P0 新增）：每个 block 的 `{policy, defects, reason, context_relevance}` 决策记录；
- `deferred_blocks`（P0 新增）：所有 `DEFER_TO_HUMAN` 的 block_id 列表，供人工复核导出口。

---

## 8.5 上下文理解（[context_understanding.py](../context_understanding.py)）

**P0 新增模块**。为决策层提供"对话级上下文感知"，取代 `pipeline/runner.py:_build_context` 旧的 ±2 block 局部视图。

### 双层架构

```
┌─────────────────────────────────────────────┐
│  Active Window (近期窗口)                   │  默认 4 条 assistant 消息
│  ─ 完整 blocks + 抽取事实/工具调用/决策     │
│  ─ 实体、toolcall → toolresult 引用关系 │
└─────────────────────────────────────────────┘
              │  滑动窗口
              ▼
┌─────────────────────────────────────────────┐
│  TieredArchive (已读压缩, 分级)             │  默认 80k 字符预算
│  T0 关键  |  T1 详述  |  T2 简述  |  T3 指针  |  T4 丢弃
└─────────────────────────────────────────────┘
```

### 5 个核心组件

| 组件 | 职责 |
|---|---|
| `BlockContextView` | 单个 block 的完整上下文视图（定位、active window、archive 摘要、引用图、相关性评分、重要性评分） |
| `MessageSnapshot` | 单条消息的结构化快照（供 archive / view 共享） |
| `SummaryEntry` | archive 单条条目（含 tier、importance、压缩内容、引用关系） |
| `TieredArchive` | 5 个 tier 分级存储 + `render(max_chars)` + `evict_until_under_budget` |
| `ContextUnderstanding` | 主类：`build(session)` 一次性构建；`get_view(block_id)` O(1) 查询 |

### 重要性评分（5 因子加权）

```
importance(block) =
    0.30 × error_relevance       # 含失败/错误关键词
  + 0.25 × transition_signal     # 位于消息边界（开头/结尾）
  + 0.20 × reference_count       # 被后续 block 引用次数
  + 0.15 × finality_signal       # 该方向唯一/最终成功的尝试
  + 0.10 × entity_novelty        # 引入新实体 vs 重复实体
```

阈值映射到 tier：

| Tier | 阈值 | 处理 |
|---|---|---|
| T0 | ≥ 0.7 | 全文保留 |
| T1 | 0.5~0.7 | 句级摘要（保留参数/数值/错误码） |
| T2 | 0.3~0.5 | 短语级（一句话） |
| T3 | 0.1~0.3 | `block_id + 一句话结果` |
| T4 | < 0.1 | 仅记 id |

### 跨 block 引用图

基于 **实体共指 + 时序相邻** 构建：

1. **时序相邻**：toolcall 后紧邻的 toolresult 自动配对
2. **实体共指**：A 提到 entity X，B 后续也提到 → A → B
3. **CJK 支持**：抽取时包含 1~4 个汉字（停用词过滤）+ 数字实体

### 容量降级保底

超 80k 字符时按 **T2 → T3 → T4 → T1 → T2** 顺序降级，T0 **永不降级**。

### 公开 API

```python
from context_understanding import build_context_for_session

cu = build_context_for_session(session, cfg, unhealthy_msg_indices=set())
view = cu.get_view(block_id)         # O(1) 单 block 查询
stats = cu.stats()                    # archive_chars / tier 分布 / llm 调用次数
archive_text = cu.render_archive()   # 供 prompt 注入
```

详细设计见 [gdr-context-understanding-and-policy.md §2](gdr-context-understanding-and-policy.md)。

---

## 8.6 决策层（[policy.py](../policy.py)）

**P0 新增模块**。基于 `defect × BlockContextView` 选择三级响应策略（替代 refiner 二元的"通过/丢弃"）。

### 策略枚举

```python
class RefinementPolicy(StrEnum):
    REPAIR_IN_PLACE   = "repair_in_place"   # 调整后保留
    PRUNE_BLOCK       = "prune_block"       # 删除单个 block
    PRUNE_WITH_PAIR   = "prune_with_pair"   # 删除 block + 上一轮 user
    PRUNE_MESSAGE     = "prune_message"     # 整条 assistant 消息删除
    DEFER_TO_HUMAN    = "defer_to_human"    # 标记待人工复核
```

### 核心 API

```python
def decide_policy(
    block, defects: list[DefectTag],
    context_view: BlockContextView | None,
    retry_exhausted: bool, cfg,
) -> RefinementPolicy: ...
```

**纯函数**：输入充分决定输出，**便于单测**。`context_view=None` 时退化为 `REPAIR_IN_PLACE`。

### 决策表（rule-based）

| Defect | 上下文特征 | 策略 |
|---|---|---|
| `REPETITIVE_CALL` | 窗口内已有同名等效版本 | `PRUNE_BLOCK` |
| `REPETITIVE_CALL` | 仍被后续引用 | `REPAIR_IN_PLACE` |
| `CONTEXT_SWITCH_LOOP` | archive 显示此前已尝试同样目的 | `PRUNE_BLOCK` |
| `OBS_DEBUG_LEAK` | 被后续 block 引用 | `REPAIR_IN_PLACE` |
| `OBS_DEBUG_LEAK` | 无引用 | `PRUNE_BLOCK` |
| `TOOL_HALLUCINATED` + retry 耗尽 | — | `DEFER_TO_HUMAN` |
| `THOUGHT_TOO_LONG` | 含 key_decisions | `REPAIR_IN_PLACE` |
| `THOUGHT_TOO_LONG` | 信息密度低 | `PRUNE_BLOCK` |
| `THOUGHT_BROKEN_LOGIC` + is_transition_point | — | `DEFER_TO_HUMAN` |
| `TEXT_FACT_HALLUCINATION` | 无来源实体 | `DEFER_TO_HUMAN` |

完整决策表见 [gdr-context-understanding-and-policy.md §3.2](gdr-context-understanding-and-policy.md)。

### 重试耗尽统一降级

`REPAIR` 9B 失败 → 升级 32B → 还失败 → 转为 `DEFER_TO_HUMAN`（而非返回 `None` 静默丢弃）。**无内容无声消失**，所有失败都有痕迹。

### 元数据落盘

每次决策写入 `policy_decisions: [{block_id, msg_idx, defects, policy, reason, context_relevance}]`，可通过 `metadata.deferred_blocks` 单独过滤出待人工复核清单。

---

## 9. Settings 关键开关（[config/settings.py](../config/settings.py)）

`pydantic-settings` + `env_prefix="GDR_"`：

| 配置 | 默认 | 作用 |
|---|---|---|
| `enable_l1 / l2 / l3` | True | 三级验证开关 |
| `enable_llm_layer` | True | Router 的 LLM 投票层总开关 |
| `llm_vote_context_strategies` | `["none", "±1", "pre2_post1"]` | 3 次投票各自的上下文窗口策略；可选值 `none` / `±1` / `±2` / `pre1_post2` / `pre2_post1`，无效值降级为 `none` |
| `llm_vote_max_context_chars` | 4000 | surrounding 字符预算，超出截断并追加 `...(truncated)` |
| `enable_text_fact_check` | True | 改进1：text 块事实性校验 |
| `strict_consistency` | True | 一致性失败是否丢弃 |
| `workers` | 1 | 多进程 Pool 大小 |
| `session_timeout_s` | 180 | 单 session 超时 |
| `max_retries_9b` | 2 | 主模型重试次数 |
| `thought_min_len / thought_max_len` | 20 / 500 | thinking 长度约束 |
| `thought_max_len_l1` | 2000 | L1 兜底上限 |
| `max_compression_ratio` | 0.5 | obs 压缩上限 |
| `context_switch_threshold / repetitive_call_threshold` | 3 / 3 | 循环类规则阈值 |
| `message_health_min_ratio` | 0.3 | 消息级健康阈值 |
| `max_failures_before_success` | 8 | 健康分失败惩罚分母 |
| `retention_threshold / removal_threshold` | 0.97 / 0.50 | 评估器闭环阈值 |
| `max_feedback_iterations` | 3 | 反馈回路最大轮数 |

**P0 新增 —— 上下文理解**：| `enable_context_understanding` | True | 是否启用 context_understanding 模块（False 时退化为旧的 ±2 上下文） |
| `context_active_window_size` | 4 | 近期窗口消息数（3~5 推荐） |
| `context_relevance_threshold` | 0.6 | 相关性阈值 |
| `context_redundancy_threshold` | 0.85 | 判定"窗口内已有等价版本"的语义相似度阈值 |
| `context_max_archive_chars` | 80000 | archive 总字符上限（4 倍余量） |
| `context_max_t0_entries` | 200 | T0 全文条目上限，超出触发合并摘要 |
| `context_compression_strategy` | "hybrid" | rule / llm / hybrid（P0 仅 rule） |
| `context_max_llm_compressions` | 3 | 单 session LLM 压缩调用上限（防爆量） |
| `context_tier0_threshold` ~ `tier3_threshold` | 0.7 / 0.5 / 0.3 / 0.1 | 重要性分数 → Tier 阈值 |
| `context_importance_w_error / transit / refs / finality / novelty` | 0.30 / 0.25 / 0.20 / 0.15 / 0.10 | 5 因子重要性权重 |

**P0 新增 —— 决策层**：

| `enable_policy_layer` | True | 是否启用 policy 决策层（False 则全部 REPAIR_IN_PLACE） |
| `policy_defer_on_exhausted` | True | REPAIR 失败耗尽是否转为 DEFER（而非丢弃） |
| `policy_prune_with_pair_enabled` | False | 是否启用"连带 user 删除"（默认关闭，保守） |
| `policy_min_redundancy_for_prune` | 1 | 窗口内至少 N 个等价版本才允许 PRUNE |

`tools.yaml` 提供工具白名单 + 幻觉 API 黑名单，`load_tools` 在启动时载入。

---

## 10. 评估器闭环（[evaluator/](../evaluator/)）

设计目标：**"精修不损失下游能力，且确实剔除缺陷"**——仅看 L1/L2 通过率无法验证 SFT 数据可用性，因此训练两个探针做差分评测。

### 10.1 SFT 数据构造（[data/sft_pairs.py](../data/sft_pairs.py)）

程序化注入缺陷，覆盖三大精修器：

- **tool_fixer**：json_invalid / hallucinated / api_hallucination / wrong_tool；
- **thought_refactor**：too_short / too_long / broken_logic；
- **obs_denoiser**：noise / debug_leak。

每模块产出两份 jsonl：

- `<module>.jsonl` → 精修探针，broken → correct；
- `<module>_broken.jsonl` → 对照探针，保留缺陷分布。

### 10.2 探针训练（[evaluator/probe.py](../evaluator/probe.py)）

- 在 `cfg.probe_base_model_name`（默认 Qwen3.5-9B-Instruct）上 LoRA SFT；
- LoRA r/α = 16/32，epochs/batch/lr = 3/4/2e-4；
- 训练完成后 `merge_and_unload`，导出 merged 模型目录供下游评测；
- 缺失 `[sft]` extras 时抛明确错误而非静默失败。

### 10.3 双维评测（[evaluator/dual_eval.py](../evaluator/dual_eval.py)）

对 `refined` 与 `original` 两个 runner 各跑 6 个子指标：

**保留性 (Retention)** —— 精修后是否仍具备原能力

| 子指标 | 计算 |
|---|---|
| `task_completion_proxy` | instruction → output 嵌入相似度均值 |
| `tool_selection_accuracy` | gold tool_name 命中率 |
| `thought_fact_consistency` | 实体守恒率 |

权重：`overall = 0.4 × task + 0.4 × tool + 0.2 × thought`

阈值：`refined.overall / original.overall ≥ cfg.retention_threshold`（0.97）

**剔除性 (Removal)** —— 精修后是否真消除了缺陷

| 子指标 | 计算 |
|---|---|
| `broken_to_correct_recovery` | broken → gold 严格匹配率 |
| `noise_robustness` | 输出不含噪声关键词的比例 |
| `debug_leak_suppression` | debug_leak 子集的非噪声率 |

权重：`overall = 0.5 × recovery + 0.3 × noise + 0.2 × debug`

阈值：`refined.overall - original.overall ≥ cfg.removal_threshold`（0.50）

只有两者**同时通过**才算 `threshold_passed`。

### 10.4 反馈回路（[evaluator/feedback.py](../evaluator/feedback.py)）

收敛条件不满足时：

1. `identify_failing_module(report)`：按 `retention.thought_fact_consistency` / `(tool_sel_acc + broken_recovery)/2` / `(noise_robustness + debug_leak)/2` 取最差；任一模块 < 0.5 即视为 failing。
2. `augment_pairs_for_module(failing_module, ...)` 程序化生成 `n_extra`（默认 `max(100, eval_size × 0.5)`）对新训练样本，追加到对应 jsonl。
3. 自动重训 refined 探针，重新跑 dual_eval。
4. 最多 `cfg.max_feedback_iterations`（默认 3）轮。

### 10.5 CLI 入口

```bash
gdr-evaluator build-pairs --n-tool 200 --n-thought 200 --n-obs 200
gdr-evaluator train-probe --train-data ./data/sft_pairs/tool_fixer.jsonl --output-dir ./probe_out --probe-name refined
gdr-evaluator evaluate --runner-refined gguf --probe-refined ./probe.gguf ...
gdr-evaluator feedback-loop
gdr-evaluator full   # 一站式 build → train ×2 → evaluate → feedback
```

支持 `mock_refined / mock_original / gguf / hf` 四种 runner，前两种无需模型做 smoke test。

---

## 11. 端到端流程图

```
QwenPaw JSON
     │
     ▼
┌─────────────────────────┐
│  Router.tag             │  规则层 + LLM 3-票异构上下文投票
│  defects_index          │  (策略见 llm_vote_context_strategies)
│  health_scores          │
└─────────┬───────────────┘
          │
          ▼
┌─────────────────────────┐
│  ContextUnderstanding   │  active window + 分级 archive + 引用图
│  BlockContextView       │  (P0 新增, 零 LLM 调用)
└─────────┬───────────────┘
          │
          ▼
┌─────────────────────────┐
│  Policy.decide          │  defect × context → REPAIR/PRUNE/DEFER
│  (P0 新增, 纯函数)        │
└─────────┬───────────────┘
          │
   ┌──────┼──────┬─────────────┐
   │      │      │             │
   ▼      ▼      ▼             ▼
┌─────┐ ┌─────┐ ┌────────┐ ┌────────────┐
│REPAIR│ │PRUNE│ │ DEFER   │ │ 跳过 refiner│
│ → refiner → validate L1/L2/L3 │
└─────┘ └─────┘ └────────┘ └────────────┘
   │      │      │             │
   │      │      │             ▼
   │      │      │     metadata.deferred_blocks
   │      │      ▼
   │      │   prune_block_ids → reassembler 删除
   │      ▼
   │   精修结果 → validate → refine_records
   ▼
┌─────────────────────────┐
│  Reassembler            │  剪枝 REPETITIVE / SWITCH_LOOP / policy-driven
│                         │  一致性终检 (judge ≥ 7)
│                         │  落 metadata: refine_history / validation_summary
│                         │                / modified_blocks / policy_decisions
│                         │                / deferred_blocks
└─────────┬───────────────┘
          │
          ▼  output.json
          │
          ▼  ───►  evaluator 闭环（可选）
                       │
                       ▼ retention / removal
                       │
                       ▼ feedback loop
```

---

## 12. 关键设计要点

1. **LLM 调用通过 HTTP**：[infrastructure/llm_client.py](../infrastructure/llm_client.py) 用 `httpx` + 单例池 + `Semaphore(4)` 限制并发；类名 `LlamaCppClient` 仅为向后兼容保留，实际是 OpenAI 兼容 chat/completions，支持 `response_format: json_schema` 结构化输出。

2. **三层验证是 fail-close 的**：L2 模块缺失会降级到 L3 而非默认放行；L1 不通过直接 fail；避免"工具缺失就过"的常见陷阱。

3. **三级模型 + 单向串联**：Router 只标 defect，Refiner 只修，Validator 只验，Reassembler 才落盘。每层职责清晰、可独立测试。

4. **改进 1+2+3 是回路核心增量**：
   - **改进 1** (TEXT_FACT_HALLUCINATION)：text 块跨 toolresult 的事实守恒校验；
   - **改进 2** (MESSAGE_UNHEALTHY)：宏观轨迹健康分 → 不健康消息整条短路；
   - **改进 3** (block 剪枝)：在 reassembler 中按 REPETITIVE / CONTEXT_SWITCH_LOOP 物理删除多余 block。

5. **评估器闭环解决"指标好看但下游掉点"痛点**：仅 L1/L2 通过率无法证明 SFT 数据可用，必须用差分探针证明"既保留能力又剔除缺陷"，并通过反馈回路自动增广最弱模块的样本。

6. **strict_consistency 是底线开关**：一致性终检失败即整 session 丢弃，避免把不一致的轨迹混入下游训练数据；关闭时会显式 WARN。

7. **多进程注意**：每个 worker 独立 `setup_logger` + 独立 LLM 客户端缓存（spawn 上下文，模型不跨进程共享），日志文件路径按 worker pid 区分。

8. **上下文理解 + 决策层（P0 增强）**：在 Router 与 Refiner 之间插入 `ContextUnderstanding`（双层上下文：active window + 分级 archive + 引用图）和 `Policy.decide`（REPAIR/PRUNE/DEFER 三级响应）。`decide_policy` 是纯函数，便于单测；ContextUnderstanding P0 阶段零 LLM 调用，纯 Python + 规则；任何重试耗尽的内容都会以 `DEFER_TO_HUMAN` 形式落到 `metadata.deferred_blocks`，**无内容无声消失**。详细设计见 [gdr-context-understanding-and-policy.md](gdr-context-understanding-and-policy.md)。

9. **分级 archive 而非均匀压缩**：80k 字符预算 + 5 个 Tier（T0~T4）取代"统一概括"模式。重要内容（决策、错误、关键 toolcall）保留全文或句级摘要；无关内容（debug 噪声、重复失败）可丢弃或仅留指针；T0 永不降级。P1 阶段会接入 LLM 合并摘要处理 T0 超限场景。