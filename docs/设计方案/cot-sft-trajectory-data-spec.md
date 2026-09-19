# CoT SFT Agent 轨迹数据要求与规范

> 依据 2026-09-10/11 批次（60 run / 38 条 trajectory / 36 个 qf_out / 41 个
> `_refined.qwenjina.txt`）的质量审查结论制定。适用对象：gdr refine 产出的
> `_refined.qwenjina.txt`（Qwen3 chat_template 渲染纯文本），用于 CoT SFT 训练。
> 后续批次按本规范验收；文中标注"建议值"的阈值可按训练目标调整。

## 1. 数据链路与消费形态

```text
agent_trajectory/*.json (JSONL 事件流)
  -> etl/qwenformat -> output/qf_out/*.json (Session, blocks 视图)
  -> gdr refine     -> *_refined.{messages,openai,meta}.json + .qwenjina.txt
  -> CoT SFT 训练集
```

训练消费形态为 ChatML 纯文本：

- `<|im_start|>system / user / assistant ... <|im_end|>` 消息边界
- assistant turn 内：`<think>...</think>` 推理块 →（可选）`<tool_call>` 工具调用 →（可选）正文
- 工具结果以 user 角色 `<tool_response>[{type:"text",...}]</tool_response>` 回传

## 2. 硬性门槛（一票否决）

| 编号 | 要求 | 判定方法 |
|---|---|---|
| G1 | **每个 assistant turn 必须有非空 `<think>` 块**。无 thinking 内容时省略标签，禁止输出空块（空块会教模型"打开即关闭思考"） | think 非空率 = 100% |
| G2 | **结尾必须是任务终态**：结论、交付物说明或明确拒绝。禁止 mid-task 截断（"Let me ..." 类过渡句收尾、句子中途被切） | 结尾启发式 + 来源 run 状态交叉 |
| G3 | **结构完整**：im_start/im_end 严格配对、文件无截断、tool_response 内 JSON 100% 可解析、无空参数工具调用 | 解析器全量扫描 |
| G4 | **来源 run 状态合规**：只收 SUCCESS（或明确豁免并标注原因）的 run。`executor_error`（超时 / 内容审查拒绝）的 session 不得进入训练集 | run.json state 映射 |

## 3. 内容规范

### 3.1 think 块

- 内容必须来自上游 trajectory 的真实推理（`model_request.messages[].content` 中
  `type=thinking` 块），不得由 refine 阶段合成或改写。
- 渲染端（qwenjina）规则：消息无 thinking 时**省略 think 标签**，不写空块。
- 工具调用前的自然语言铺垫（pre-call text）不能替代 think 块；本批次仅
  70/835 个调用 turn 有 >40 字铺垫，不足以充当弱 CoT。

### 3.2 工具调用与结果

- `tool_call` / `tool_response` 必须配对；参数值非空。
- **工具报错样本保留**（报错后的恢复行为有教学价值），但单文件报错密度
  建议不超过总调用数的 15%（建议值；本批 T016 为 12/39 ≈ 31%，超标示例）。
- tool result 内部元数据字段（`finished_at` 等）训练前剥除；本批次
  849/849 条 `finished_at: null`，属纯噪声。

### 3.3 结尾 headline

- `⟦...⟧` 是 QwenPaw 内部检索标记，不属于任务答复。训练前**统一剥除**；
  禁止混布（本批次 33/41 final 有、8 个无、16 处中途 turn 缺失，即混布状态）。

### 3.4 system prompt

- 含环境特定内容（`C:\Users\...` 工作目录、agent id、headline 指令）。
  是否保留须与训练/部署模板对齐后**全批一致**执行，不得有的留有的删。

### 3.5 去重与配比

- 近重复会话去重：正文相似度 ≥ 0.8（建议值）只留一条（本批 T001 两
  session 相似度 0.84）。
- 样本构成建议：正常完成为主；安全拒绝样本保留（本批 4 个均为对盗版
  请求的规范拒绝，附正版替代渠道）；报错恢复样本保留；mid-task 截断样本
  数量必须为 0。

## 4. 每批次验收清单

- [ ] G1 think 非空率 100%（分母 = assistant turn 总数）
- [ ] G2 无 mid-task 结尾；被剔除文件列出清单及原因
- [ ] G3 标签配对 / JSON 解析 / 参数非空 全过
- [ ] G4 来源 run 状态全部合规，豁免项有书面原因
- [ ] headline 已统一剥除（或统一保留），无混布
- [ ] tool result 元数据噪声已剥除
- [ ] 去重完成
- [ ] 记录规模：对话数 / assistant turn 数 / 工具调用数 / 总字符数
- [ ] 记录工具报错分布（类型 × 次数 × 涉及文件数）

验收工具：本批次审查用的三个脚本暂存于仓库根（`analyze_traj_quality.py`、
`compare_src_refined.py`、`check_thinking_src.py`），可作验收脚本基础，后续
应固化为 `--validate-dataset` 类命令。

## 5. 本批次（2026-09-10/11）执行结果

| 检查项 | 结果 | 判定 |
|---|---|---|
| G1 think 非空 | 1/876（仅 T047 单块 3864 字符） | **不合格（P0）** |
| G2 完整收尾 | 37/41；T003、T007（内容审查中断）、T020、T056（1800s 超时）为 mid-task 截断 | 不合格 |
| G3 结构完整 | 41/41 标签配对、无截断、JSON 100% 可解析 | 通过 |
| G4 run 状态 | 0 SUCCESS / 54 inconclusive / 6 executor_error，41 文件全部导出 | 不合规 |
| headline | 33 有 / 8 无（混布） | 需统一 |
| 工具报错 | 29/41 文件（timeout 33、`failed:` 28、HTTP 4xx 22，多为 Tavily 429） | 保留 |
| 重复 | T001 对相似度 0.84 | 需去重 |
| 规模 | 41 对话 / 876 assistant turn / 834 工具调用 / 3.4MB | 偏小，试点级 |

**结论：本批次不可用于 CoT SFT。** 主因是 think 块 100% 为空——qf_out 源
即无 thinking（38 个中 37 个为 0），而上游 trajectory 实际含约 2050 万字符
结构化 thinking，属 ETL 丢失，需修复后重新导出再验收。

## 6. 已知强制停止模式（验收时重点排查）

| 模式 | 证据 | 本批案例 |
|---|---|---|
| 1800s 墙钟超时 | run.json failure: `Task timed out after 1800s` | T017、T010、T020、T056 |
| 内容审查拒绝 | DashScope `data_inspection_failed` | T003、T007 |
| 轨迹解析失败入 dead | orchestration/dead/（session 记录恢复，无 qf_out） | T020、T056、T055 |

注意：dead 来源 ≠ 未完成。T055 在 dead 但正常完成（有完整结论与 headline）；
判定完成度以结尾内容 + run 状态为准，不以目录来源为准。未发现任何"最大推理
/轮次上限"截断（38 条轨迹 model_request 1–89 无封顶聚集）。
