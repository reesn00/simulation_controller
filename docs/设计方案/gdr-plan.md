gdr 目录：模块功能与时序概括
一句话定位
gdr 是一条轨迹数据精炼流水线：读入原始智能体对话轨迹，逐块检出缺陷、按策略修复或裁剪，最后经一致性校验与模型终审，产出**单 C2 refined Session**（不在 gdr 内部写多视图；多视图拆分由下游 etl 阶段的 `gdr.domain.schema.save_session_v2` 完成）。全程只有一个"编排者"（gdr/pipeline/runner.py）按固定顺序驱动各模块，模型调用被严格限制在三个点位，其余全部是零模型调用的确定性处理。

> 新架构（2026-09-22 起）：`simulation server → gdr → etl`。gdr 不再做 qf_text 渲染与
> usage_prune；runner.py::_apply_usage_prune 整段已删除；gdr 输出单 C2 文件
> `output/refined/<TXXX>__<session_id>.json`，etl 阶段统一做格式整理并拆 4 视图。
> 契约详见 [docs/contracts/](../contracts/)。

主流程时序（单条轨迹的完整旅程）

from_trajectory 加载会话               gdr/parsers/__init__.py
  │       (薄包装 etl.qwenformat.load.load_trajectory + Session.model_validate)
  │
  ├─ ① 硬过滤                            runner._hard_filter_session
  │     整段不合格（缺关键字段等）→ 直接淘汰，不进入后续
  │
  ├─ ② 轻量健康分                        routing/health.py（零模型）
  │
  ├─ ③ 上下文理解·结构层                  core/context_understanding.py（零模型）
  │     建立每个块的前后引用图，供折叠保护与后续决策查询
  │
  ├─ ④ 折叠                              reassembly（零模型）
  │     折叠失败的工具结果、折叠连续重复的思考段
  │
  ├─ ⑤ 状态重追踪                        上下文理解模块（★ 第 1 次模型调用）
  │     折叠后重切块，让模型重新追踪任务状态
  │
  ├─ ⑥ 启发式用户意图                    core/user_intent.py（零模型）
  │     截取首条用户消息，供下一步离题检测做参照
  │
  ├─ ⑦ 缺陷标注                          routing/router.py
  │     规则层（黑名单/白名单/模式匹配）
  │     + 嵌入层（★ 第 2 次模型调用族：向量化算相似度，判工具是否离题）
  │
  ├─ ⑧ 策略决策                          core/policy.py（零模型）
  │     每个缺陷块五选一：原地修复 / 整块裁剪 / 连配对裁剪 /
  │     整条消息裁剪 / 转人工审核
  │
  ├─ ⑨ 精修执行                          refiners/（并发，块间独立）
  │     思考段改写 refiners/thought_refactor.py
  │     工具调用修复 refiners/tool_fixer.py
  │     观察结果去噪 refiners/obs_denoiser.py
  │     每次修复后过三层校验（见下），不过则记为修复失败
  │
  ├─ ⑩ 重组终审                          reassembly/reassembler.py
  │     一致性校验（修复后与原文冲突 → 回滚）
  │     用户意图抽取（★ 第 3 次模型调用，轻量）
  │     模型终审打分 → 意图达成度评分
  │     质量评分挂载 core/quality_scorer.py（零模型，纯组合已有信号）
  │
  ├─ ⑪ 完整性检查                        runner（保存前）
  │     末尾工具调用未闭合/配对缺失/文本不完整 → 旁路，不写训练数据
  │
  └─ ⑫ 保存                              domain/schema.save_refined_session
        单 C2 refined Session：refined blocks + 全部审计 metadata
        路径：output/refined/<TXXX>__<session_id>.json
        下游：etl 阶段读 C2，做 usage_prune + transform + partition + summarizer
        后由 save_session_v2 拆 4 视图落到 output/refine_data/
批处理层把上述单文件流程并发套起来，最后汇总出批次报告：各复杂度档位的数量分布、训练价值分数的最小/最大/平均与分档统计。

模块分组职责
目录	职责	时点
gdr/domain/	数据契约：会话、块、缺陷标签、处置策略、修改状态、四视图输出定义	全程被引用
gdr/config/	严格配置加载；tools.yaml 提供工具白名单、幻觉接口名单、工具描述、离题黑名单	启动时一次
gdr/core/	无状态决策件：上下文理解、策略决策表、用户意图、质量评分	流程中段与末段
gdr/routing/	健康分（快速）+ 缺陷标注器（规则与嵌入双路）	流程前段
gdr/validators/	三层校验：确定性规则校验 / 语义校验 / 模型评审校验	每次修复后 + 终审
gdr/refiners/	三个精修器，只改内容不判生死	流程中段
gdr/reassembly/	折叠 + 重组终审 + 元数据挂载	流程末段
gdr/infrastructure/	模型客户端、向量化服务、日志	被按需调用
gdr/data/	程序化构造训练样本、反馈回路补样	离线独立
gdr/evaluator/	修复质量的双评测、探针、报告与反馈	离线独立
gdr/prompts/	各模型调用点用的提示词模板	模型调用时
gdr/pipeline/	编排入口与批处理	全程驱动
模块间交互的四个关键约定
上下文理解贯穿全程：结构层在流程早期建好“谁引用了谁”的视图，折叠靠它保护被引用的块不被误删，策略决策靠它判断“删这个块会不会断链”（被引用的离题工具块降级为转人工，而不是硬删）。
模型调用只有三类点位：状态重追踪（一次）、缺陷标注的向量化（按会话缓存去重）、终审前的意图抽取与评审打分。其余环节全部确定性执行，保证可复现、可审计。
修复器没有验收权：精修器只产出修改建议，每次修改必须过三层校验，终审与回滚权在重组模块；修改状态分五种（未动 / 已修 / 保留原文 / 已回滚 / 需人工），全部记录在案。
元数据是统一审计通道：用户意图（启发式与模型抽取两个版本）、意图达成度、训练价值分数、复杂度档位、质量分量表、每块的处置与修复历史，全部挂在会话元数据上随输出落盘，训练数据则只取完整且干净的会话。
输入与输出
输入：转换后的轨迹目录（每文件一个会话）；配置统一从仓库根 config/config.yaml 读，GDR_CONFIG_FILE 可重定向。
输出：每条轨迹一个输出目录（四视图文件），干净对话进训练数据目录，批次级汇总报告含复杂度分布与分数摘要；未闭合会话旁路到不完整目录，绝不混入训练正例。

---

## 精修执行阶段详细说明

本节展开流程时序第 ⑧ 步「策略决策」与第 ⑨ 步「精修执行」的真实执行细节，对应 `gdr/pipeline/runner.py::process_one` 第 3.5–4 段、`gdr/refiners/` 三个精修器、`gdr/validators/` 三层校验、`gdr/reassembly/reassembler.py::reassemble` 的终审与元数据落盘，并标注与 `gdr/quality-fixes-2026-09-19` 六件套的一致关系。

### 一、决策层（policy）在精修前的分流

精修不是无差别执行的。在每个 block 进入 `_prepare_repair_item` 之前，`core/policy.py::decide_policy` 已经按缺陷标签 + ContextUnderstanding 视图把它分流到五类处置：

| 策略 | 含义 | 是否调 refiner |
|---|---|---|
| `REPAIR_IN_PLACE` | 进入精修流程 | 是 |
| `PRUNE_BLOCK` | 单 block 删除 | 否（仅记入 `policy_decisions`） |
| `PRUNE_WITH_PAIR` | block + 上一轮 user turn 一起删 | 否 |
| `PRUNE_MESSAGE` | 整条 assistant 消息删除（由 reassembler 借 health_scores 处理） | 否 |
| `DEFER_TO_HUMAN` | 标记转人工审核队列 | 否（仅记入 `deferred_block_ids`） |

关键分流点：

- **重复调用（`REPETITIVE_CALL`）+ 窗口内冗余**：`CLEAN` 模式直接 `PRUNE_BLOCK`；`ROBUST` 模式保留错误样本走修复；`DROP` 模式升级为 `PRUNE_MESSAGE`（`failure_handling_mode` 配置）。
- **离题工具（`TOOL_OFF_TOPIC`）**：决策恒为 `PRUNE_BLOCK`——改写工具名/参数都不能消除"无关"事实，删是最干净的训练信号；只有当后续 thinking/text 引用了它（`referenced_by` 非空）才降级为 `DEFER_TO_HUMAN` 避免断链。
- **过长 thinking（`THOUGHT_TOO_LONG`）**：根据 `context_view.key_decisions` 是否非空二选一——含决策句走 REPAIR（压缩），纯填充走 PRUNE。
- **decision / meta-reasoning 类 thinking**（`thought_refactor` 内部 `_is_decision_or_meta_reasoning`）：命中 ≥2 个决策标记（`let me decide` / `now compose` / `compose the answer` / `policy decision` 等）即跳过 LLM 改写保留原文，避免 9B/32B 输出"如何改"的解释而非 JSON（P1.4）。
- **空白名单降级态**：`tool_fixer.refine` 显式放过 `tool_names=[]` 的名称校验——任何候选名都触发的"not in allowed list"会让重试耗尽 + 32B 升级全部白费，反而误杀真实数据。

决策层不调任何 LLM，仅依赖 ContextUnderstanding 视图。`enable_policy_layer=False` 时全量回退到 `REPAIR_IN_PLACE`（兼容旧调用方式）。

### 二、精修器接口与失败升级路径

三个精修器共享统一形态 `refine(block, context, defects, cfg) -> refined_content | None`：

- `refiners/thought_refactor.py::refine(ThinkingBlock, …)` — 处理 `thought_too_short / thought_too_long / thought_broken_logic`
- `refiners/tool_fixer.py::refine(ToolcallBlock, …)` — 处理 `tool_json_invalid / tool_hallucinated / api_hallucination / tool_wrong_selection / repetitive_call`
- `refiners/obs_denoiser.py::refine(ToolresultBlock, …)` — 处理 `obs_noise / obs_debug_leak`

无缺陷的块直接返回原内容，不调 LLM。

**重试 + 升级策略**：每个精修器最多 `max_retries_9b` 次（默认 2）使用 `main_model`（9B）改写；空输出 / 校验失败 → 下轮把对话附 "上轮你只给了分析说明，请立即输出 JSON 代码块" 强约束再试；耗尽后升级到 `tool_model`（32B）做单次最后一搏（`escalation to 32B` 日志）。仍失败则记 `discard block <id>, reason=<module>_exhausted`，返回 `None`。

#### 1. thought_refactor 的实体硬约束（P1.1 / P1.2 / P1.3 / P1.4 / P1.7 联动）

- **实体抽取白名单**：仅保留高置信度硬约束实体——URL（`_URL_PATTERN`）、文件路径（`_FILE_PATH_PATTERN`，URL 范围屏蔽，避免 `/example.com/path` 被误识别为 Unix 路径）、短引号标识符（结构特征）、CamelCase 专名、内置工具名（白名单）、≥2 位数字 ID。引号长句片段不再视为实体。
- **实体保持校验** `_entities_preserved`：严格匹配；CamelCase / 工具名走大小写不敏感兜底；URL 仅 prefix 兜底（refiner 在 URL 末尾加 query / 去 query 算保留），**host 必须一致**——LLM 改写时被 prompt 硬约束禁止 host 标准化（`www.iqiyi.com ≠ m.iqiyi.com`），此处 P1.7 修复撤销了早期 host 弹性豁免。
- **长度余量**：在 `thought_max_len`（默认 500）基础上加 `thought_max_len_grace_pct`%（默认 10%）的 grace 区间，避免 501 vs 500 这类单字符临界误杀。
- **decision / meta-reasoning 跳过**：`_is_decision_or_meta_reasoning` 命中 ≥2 个 `_DECISION_MARKERS` 即跳过 LLM 改写（policy 已在更上层分流一部分，此处是补漏）。

#### 2. tool_fixer 的"空白名单 + 强约束重试"

- 调用 `LlamaCppClient.chat(..., grammar_json_schema=OUTPUT_SCHEMA, max_tokens=1536)` 强制 JSON 输出 schema（`name` + `input`，`input` 必须是 JSON 字符串）。
- `tool_names` 非空时强制 `name in tool_names`；`hallu_apis` 非空时强制 `api.lower() not in inp.lower()`。
- 重试附 "上轮你只给了分析说明，没有输出 JSON" 提示。

#### 3. obs_denoiser 的压缩比与禁用格式

- 评估压缩比时**先剥离 `<think>...</think>` 块**（`_THINK_RE.sub`），否则 reasoning 也会被计入，导致 ratio > 1.0。
- 压缩比阈值 `cfg.max_compression_ratio`（默认 1.50）。
- 输出禁止 markdown（`### | ** | --- | ` 关键字）与 JSON 前缀（`^\s*[\{\[]`）——这些是模型把"摘要"写成新 markdown 章节或 JSON 块的常见污染。

### 三、三层校验（validators）

每次精修后**必须**走三层校验 `gdr/validators/__init__.py::validate_block`，失败即记 `result="failed"`（即便 refiner 返回了文本）。

```
L1 规则 → L2 语义（嵌入相似度）→ L3 模型评审（仅 L1/L2 失败时启用）
```

#### L1 规则校验 `validators/l1_rules.py::check`

- `thinking`：非空、长度 ≤ `thought_max_len_l1`（默认 2000，**注意这是 L1 专属上限，与 LLM 重写用的 thought_max_len=500 不同**）、原文实体集合 ⊆ 新实体集合。
- `toolcall`：`name ∈ tool_names` 且 `json.loads(input)` 不抛异常。
- `toolresult`：非空、不含 `_NOISE_PATTERN`（`DEBUG | Traceback | [API_MISUSE] | FATAL | ModuleNotFoundError | IndentationError | SyntaxError`）。
- **fail-close**：任何 L1 失败直接终止校验，不进 L2/L3。

L1 是无 LLM 的确定性校验，单 block 微秒级，能挡住最常见的格式破坏。

#### L2 语义校验 `validators/l2_semantic.py::check`

- 调 `HttpEmbedder`（OpenAI 兼容 `/v1/embeddings` 端点，缓存单例）算原文 vs 新文的 cosine 相似度。
- 阈值按块类型：`thinking ≥ 0.85 / toolcall ≥ 0.90 / toolresult ≥ 0.80`。
- `_FAIL_MARGIN = 0.03`：处于阈值下沿 0.03 范围内判 soft fail，仍把决定权交给 L3。
- **fail-close**：嵌入服务异常 / 空响应 → 判 fail，让 L3 接手而不是静默放过（避免"embed 崩了 → 所有块自动通过"）。

#### L3 模型评审 `validators/l3_judge.py::check`

- 仅 L1/L2 失败时触发（`_run_l3` 追加到 results 末尾，决定 block 最终 passed）。
- 调 `judge_model`（默认 32B，`max_tokens=cfg.judge_max_tokens` 默认 36000，`temperature=0.0`），返回 `{verdict, score, reason}`。
- L3 异常时按 fail 处理，**绝不**让一次 LLM 卡顿吞掉所有 valid_block。

`passed = (L1.passed) AND (if L2 enabled then L2.passed else True) AND (if L3 触发了 then L3.passed else True)`——任一层 fail 即视为 `result="failed"`，refined_content 仍写回 record 但 block 在下游一致性校验中走回滚/人工审路径。

### 四、并发执行与早退分支

`_run_repairs(repair_items, cfg, tool_names, hallu_apis)`：

- 用 `ThreadPoolExecutor(max_workers=min(llm_concurrency, len(repair_items)), thread_name_prefix="gdr-refine")` 并发调 `_execute_repair_item`；`workers=1` 时退化为串行。
- 每个 item 顺序走 `_execute_repair_item` → refiner → `validate_block` → 产出 `(refined, val_results, result)` 三元组。
- 线程安全：精修器只依赖 item 内数据 + 无状态模块函数 + 共享 `LlamaCppClient.get(model)` 单例。

**早退分支**（`_run_repairs` 返回空 + `policy_decisions` 也为空）：

- 通过 `_l1_sanity_check`（toolcall.input 是合法 JSON、toolcall.name 在白名单、thinking 非空且长度 ≤ `thought_max_len_l1`）→ 挂 metadata 返回 session。
- 失败 → 仍挂 metadata 返回 session，**不丢弃**——保留 audit trail，下次 router 规则更新后可再跑。

有 `policy_decisions` 但无 `refine_records`（如全部 PRUNE / DEFER）时**不能早退**，否则剪枝决策会被静默丢弃——这条注释明确写在 `_run_repairs` 之后那段代码。

### 五、重组终审（reassembler）

`gdr/reassembly/reassembler.py::reassemble` 拿到 `(session, refine_records, health_scores, policy_decisions, prune_block_ids, deferred_block_ids, cu)`，依次做：

1. **应用 health-driven 剪枝**：循环消息级，对 `repetitive_loop / context_switch_loop` 调 `_prune_repetitive_blocks` / `_prune_context_switch_blocks`。
2. **应用 policy-driven 剪枝**：`prune_block_ids` 中的 block 直接从 `msg.blocks` 删除（按 `block_id` 定位，避免位置索引因前面剪枝而错位）。
3. **按 `block_id` 回写 refined content**（关键修复：不能按 `record.block_index.block_idx` 位置索引写，否则前面剪枝会让 IndexError 或把内容错写到相邻块）。
4. **编辑一致性校验 `_validate_edit_consistency`**（`enable_edit_consistency_check=True` 时）：
   - 对每 chunk 比对 edit 前后 archive 视图，找关键字段丢失/冲突。
   - 实体丢失先经 LLM 复核（`consistency_semantic_confirm=True` 启用 `_confirm_loss_with_llm`），不确认才回滚。
   - 字段级一致性阈值 `consistency_constraint_similarity`（默认 0.6）。
   - LLM 调用受 `consistency_max_llm_calls` 预算约束（默认 40），超预算时停止语义复核，按已确认回滚推进。
   - **修复 B 短路**：所有成功编辑都被一致性回滚 → blocks 已恢复为原文（语义安全）→ 短路跳过终 judge，避免 judge 把"无编辑痕迹"误判 0 分导致双重丢弃。
5. **标记 edit_status**：成功编辑 `UNTOUCHED → EDITED`；失败 `UNTOUCHED → PRESERVED`（含回滚、需人工、保留原文）。
6. **轻量 user_intent 抽取 `extract_user_intent_llm`**：第 3 次也是最后一次模型调用（轻量），失败/关闭时降级到 `heuristic_user_intent`；写到 `metadata.user_intent`。
7. **Judge 终评**：
   - 用 `reassembler` prompt（`system + user(session_summary, messages_detail, user_intent)`），`judge_model` 32B，`max_tokens=cfg.judge_max_tokens`（默认 36000，留足 reasoning 模型预算），`temperature=0.0`。
   - judge 不可用检测（`empty_text / parse_failed / no_score_field`）→ 写入 `metadata.judge_unavailable_at`，**保留 session 不丢弃**（与"数据完整即处理"主旨一致）。
   - judge 评分阈值由 `_judge_min_score_for(cfg, modified_count)` 计算（Fix B 三段阶梯）：
     - `modified_count ≤ 1` → `passthrough`，默认 `min=2`（`judge_min_modified_passthrough / judge_min_score_passthrough`）
     - `modified_count ≤ 3` → `low_edit`，默认 `min=5`（`judge_min_modified_low_edit / judge_min_score_low_edit`）
     - `modified_count ≤ 5` → `relaxed`，默认 `min=3`（`judge_min_modified_for_relaxation / judge_min_score_relaxed`）
     - 其他 → 严格 `judge_min_score=7`
   - 阶梯设计依据：modified_blocks 越少说明 refiner 编辑越保守，judge 实际评的是原 trajectory 内部自洽度，与 refiner 编辑质量解耦；放宽让合格样本进主输出，`relaxed_kind` 留 audit 通道。
   - 拒收时 `metadata.judge_discard = {score, min_score, reason, relaxed_kind, modified_blocks}`（Fix A 把 L3 reason 串入 metadata）。
8. **挂元数据 `_attach_metadata`**：
   - 先跑 `compute_quality_score`（quality_scorer）：组合 7 个分量（health / judge / intent / modified_ratio / tool_diversity / noise / depth）→ `training_value_score ∈ [0,1]` + `complexity_tier ∈ {easy, medium, hard}`，默认权重见 `quality_scorer_weight_*`。
   - `refine_history`：每条 `[module, attempts, model_used="9B/32B", result, reason]`。
   - `validation_summary`：聚合 `total_blocks / modified_blocks / passed_L{1,2,3} / failed_L{1,2,3}`。
   - `modified_blocks`：成功编辑的 block_id 列表。
   - `edit_status_summary`：5 种状态（UNTOUCHED / EDITED / PRESERVED / ROLLED_BACK / NEEDS_HUMAN）计数。
   - `policy_decisions / deferred_blocks`：决策层输出供审计/复盘。
   - `original_session_id + refined_version="v2"`：血缘追溯。

### 六、保存前完整性检查

runner 在 `process_one` 末尾、`save_refined_session` 之前执行 `_detect_incomplete_session` 四维检测（F2 / F3-D / F3-E）：

1. 末尾 assistant 无 final text
2. toolcall/toolresult 数量不匹配（**F3-E 豁免**：仅末尾是 text 块 + 含完整收尾信号时跳过，记 INFO "agent-intentional closure"）
3. 末尾文本截断（`last_text_incomplete`）
4. **thinking-only tail**：末尾 assistant 无 final text、无未配对 toolcall、但 `thinking_chars ≥ incomplete_thinking_only_min_chars`（默认 200）→ incomplete（F2）

"完整收尾信号"由 `_has_complete_close_signal` 给出（弱化版，不设最短长度门槛），强信号版 `_has_structural_close` 还覆盖 markdown 表格行、分隔线、code fence、colon-fenced block、方括号配对闭合（⟦⟧、【】、()、[] 等）。

判 incomplete 的 session 走 `_append_incomplete_queue`，由 `failure_handler.reap_dead`（Fix C）保留 refined 单文件 + 写 INDEX.jsonl，可被 `reprocess_dead` 按 score/status 过滤回灌。

### 七、与修复日志的对应关系

| 修复 | 对应执行点 |
|---|---|
| F1（tools 字段透传） | `_attach_metadata` 之外的 `domain/schema.py::save_refined_session`，未在本节展开 |
| F2（thinking-only tail 维度 4） | `_detect_incomplete_session` 第六节 |
| F3-C（system_prompt 追加 reasoning requirement） | 不在 gdr 本体内，详见 `etl/qwenformat/system_prompt.py` |
| Fix A（judge reason 串入 metadata） | 重组终审第 7 步 |
| Fix B（judge 三段阶梯） | 重组终审第 7 步 `_judge_min_score_for` |
| Fix C（failure_handler 保留 refined 单文件 + INDEX.jsonl） | 第六节旁路通道 |
| F3-D（结构闭合信号） | 第六节 `_has_structural_close` |
| F3-E（维度 2 豁免） | 第六节 `_has_complete_close_signal` |
| P1.1（thought_refactor entity_loss 误判） | 精修器 1 的实体抽取 |
| P1.2（thought 长度 grace） | 精修器 1 的 `_max_with_grace` |
| P1.3（path 边界收紧） | 精修器 1 的 `_FILE_PATH_PATTERN` |
| P1.4（decision 跳过） | 精修器 1 的 `_is_decision_or_meta_reasoning` |
| P1.5（gdr worker incomplete 走 NonRetryableError） | 不在本节，体现为 `process_one` 的 try/except 返回 None |
| P1.7（撤销 host 弹性豁免） | 精修器 1 的 `_entities_preserved` |

### 八、关键设计原则

1. **修复器无验收权**：refiner 只产出修改建议，每次修改必须过三层校验；终审与回滚权在 reassembler；修改状态分五种（UNTOUCHED / EDITED / PRESERVED / ROLLED_BACK / NEEDS_HUMAN）。
2. **校验 fail-close**：L1 直接终止；L2 嵌入崩了判 fail 让 L3 接手；L3 异常按 fail 处理——任一层都不能静默放过。
3. **数据完整性优先于判分**：judge 不可用 / 一致性全回滚 / 不健康消息都保留 session 挂 metadata，不因一次 LLM 卡顿丢数据；incomplete 旁路同样保留原始 session。
4. **阶梯阈值让合格样本进主输出**：modified_blocks 越少、refiner 编辑越保守，judge 评的是原 trajectory 自洽度，应放宽门槛；放宽的同时 `relaxed_kind` 留 audit 通道。
5. **决策分层（policy）不是装饰**：先分流再精修——`PRUNE_BLOCK` 类决策零成本跳过 refiner 与三层校验；`DEFER_TO_HUMAN` 类只标记不调用；只有 `REPAIR_IN_PLACE` 才进精修链路，让 LLM 预算花在真正值得改的块上。
6. **可灰度可回退**：所有修复均带 cfg flag（`enable_policy_layer / fold_protect_active_text_only / judge_min_*_passthrough / consistency_semantic_confirm / incomplete_detection_enabled / enable_edit_consistency_check` 等），单点关闭即回到既有行为，便于新旧对比与紧急回滚。