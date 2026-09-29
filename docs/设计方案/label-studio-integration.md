# Label Studio 集成方案

> **状态**: 设计中（架构已定向，2026-09-28 修订）
> **范围**: 人工标注层 — 把 `output/refine_data/*` (C3) 候选样本 + **评分卡**推送至本地
> Label Studio（`http://127.0.0.1:8099`）做 SFT 质量审核
> **本项目终点是 Label Studio**：LS 标注完成后**不回流本项目**，本项目只负责
> 把**明确的指令评分与评分依据**送进去
> **不在范围**: 修改 C1/C2/C3 既有字段语义 / 修改 `simulate_serve / gdr` 业务逻辑
> **模块**: 新增 `label_studio/`（与 `simulate_serve / gdr / etl / orchestration` 平行）

## 0. 设计决策汇总

| # | 决策点 | 选定方案 | 备注 |
|---|---|---|---|
| 1 | 集成定位 | **终点层**：推送评分卡 → 人工核对 → LS 侧终结 | LS 标注结果不回流本项目（§17 架构前提） |
| 2 | 交付物 | **评分卡 `scorecard.v1`**（§4.2） | 指令评分（L0–L5）+ 每维依据 + 来源可信度 |
| 3 | Label Studio 实例 | 本地单实例 `http://127.0.0.1:8099` | 用户提供；非默认 8000 端口 |
| 4 | 项目拓扑 | 单项目 `trajectory-sft-quality` | 按 `complexity_tier` 在 LS 端 Data Manager filter 分桶 |
| 5 | 凭据管理 | `${LABEL_STUDIO_API_KEY}` env | gitignored；禁止硬编码 / 提交 / 测试 / 文档 |
| 6 | label_config XML | `label_studio/label_configs/trajectory_review.xml` | Tabs：评分卡 / 指令核对 / 轨迹 / ChatML + 判定 Choices |
| 7 | 预标注（predictions） | **只做风险提示，不做 accept/reject 预判** | 原阈值规则与 §3 自相矛盾，已反转（§5.2） |
| 8 | C4 契约 | `docs/contracts/C4-scorecard.md` | 取代原 `C4-labeled-annotations.md` |
| 9 | orchestration 集成 | 自动 hook（task done 后推送） | `task_pipeline.py` step 11（在 `mark_phase(done)` 之后）；线程化 + 独立超时；失败不阻塞 |
| 10 | 推送去重 | **本地台账** `output/label_studio/push_index__<project_id>.jsonl` | LS 1.23 根本不去重（§16 R7 已按实测推翻）；台账同时供给预标注要的数字 task id |
| 11 | SQLite | **不改** | 推送状态不回写队列（§4.4 说明理由） |
| 12 | 隐私边界 | 沿用 CLAUDE.md 红线（raw CoT 约束） | LS 推送的 C3 含 **refined CoT**，不受思维链红线约束（CLAUDE.md 定义段）+ R11 凭据扫描 |
| 13 | fork-safe | hook 内 client **用完即弃**（不建单例） | 规避 `multiprocessing.Pool` 跨平台 fork/spawn 差异 |
| 14 | 失败隔离 | LS 不可达 / 凭据错 / 超时全部吞掉 | 仅日志告警；`phase=done` 不变 |

## 1. 背景与动机

### 1.1 当前流水线数据产出

项目核心流水线 `simulate_serve → gdr → etl` 在 `output/refine_data/` 产出 C3 4 视图
（`messages.json` / `openai.json` / `qwenjina.txt` / `meta.json`）。

`meta.json` 的实际结构由 [`gdr/domain/schema.py::save_session_v2`](../../gdr/domain/schema.py) 决定——
是 **`session.metadata` 的平铺**，加注两个字段，**没有 `metadata` 嵌套层**：

```python
meta = dict(session.metadata or {})          # 平铺，不是 {"metadata": {...}}
meta["session_id"] = session.session_id      # session_id 在此注入
meta["meta_tag_contamination"] = ...         # ⟦⟧ 剥离统计
```

因此下列字段的正确取法是 `meta["<key>"]`，**不是** `meta["metadata"]["<key>"]`：

| 字段 | 含义 |
|---|---|
| `training_value_score` | quality_scorer 训练价值分 ∈ [0,1] |
| `complexity_tier` | ∈ {easy, medium, hard}（分桶阈值 0.70 / 0.40） |
| `quality_scorer_components` | **七维分量分解**（评分依据，§4.2 L4） |
| `validation_summary` | 块级三层校验计数（passed/failed L1·L2·L3） |
| `edit_status_summary` | 五种编辑状态计数 |
| `folded_failed_toolresults` | fold 阶段折叠的失败 tool_result |
| `judge_discard` / `judge_relaxed` / `judge_unavailable_at` | LLM Judge 结果 |
| `user_intent_fulfillment_score` | 意图达成 ∈ {0,1,2} |
| `meta_tag_contamination` | ⟦⟧ 剥离统计（F3-D） |

**但缺少"人工决策层"**：自动评分 + 启发式 + LLM Judge 共同决定通过/拒绝，但
人工对修复方向 / 错误归类 / 接受与否是更高质量的反馈源。且现有自动评分**没有
以"指令"为单位展开**——`training_value_score` 是单一标量，标注员无从判断
"哪条指令没达成、为什么扣分"。

### 1.2 为何选 Label Studio

- 业内标准开源标注平台，原生支持 `label_config` XML + `predictions` ML 预标注
- 本地部署，隐私可控
- Python SDK `label-studio-sdk` 提供完整 CRUD；REST API 完备
- 不引入新的外部服务凭据管理负担

## 2. 数据流

```text
              ┌────────────────────────────────────────┐
              │   output/refine_data/*  (C3 4 视图)     │
              │   + output/runs/*  (Criterion 验证结果)  │  ← 前置 F2 注入 criterion_results
              └──────────────┬─────────────────────────┘
                             │ 读 meta.json / messages.json / qwenjina.txt
                             ▼
   ┌─────────────────────────────────────────────────────────────┐
   │   label_studio/  (新模块，与 simulate_serve / gdr / etl 平行) │
   │   client.py ──── SDK 包装：重试 + health check（用完即弃）     │
   │   settings.py ── LabelStudioSettings (frozen dataclass)       │
   │   config_loader.py ── load_label_studio_config()              │
   │   project_manager.py ── create/get_or_create/validate/purge   │
   │   task_exporter.py ── C3 → LS task.data（字段映射 + 凭据扫描）│
   │   scorecard.py ──── C3 meta.json → scorecard.v1（L0–L5 + 依据）│
   │   errors.py ────── LabelStudioError 家族                      │
   │   __main__.py ──── init-project / status / upload / purge     │
   └──────────────────────────┬──────────────────────────────────┘
                              │ POST /api/projects/{id}/import
                              │ POST /api/projects/{id}/import/predictions
                              ▼
              ┌─────────────────────────┐
              │  Label Studio           │
              │  http://127.0.0.1:8099  │
              │  （本项目终点，不回流）   │
              └─────────────────────────┘
                     │ 人工核对（UI）
                     ▼
              ┌─────────────────────────────────────┐
              │  LS 侧终结：经人工校准的评分 + 判定    │
              │  外部训练方按需消费，不回本项目         │
              └─────────────────────────────────────┘
```

## 3. 项目拓扑（单项目）

```
Label Studio @ http://127.0.0.1:8099
└── Project: "trajectory-sft-quality"   (auto-created by init-project)
    ├── label_config: trajectory_review.xml
    ├── tasks = 所有 C3（inner_id = session_id 去重）
    └── Data Manager filter → 按 complexity_tier 分桶:
        ├── filter:tasks:data.complexity_tier = "easy"
        ├── filter:tasks:data.complexity_tier = "medium"
        └── filter:tasks:data.complexity_tier = "hard"   ← 建议优先
```

分桶说明：`complexity_tier` 由 `training_value_score` 分桶而来（`≥0.70` → easy /
`≥0.40` → medium / 否则 hard）。**hard = 质量分低 = 自动判定最不可靠 = 最需要人工**，
故标注员应优先标注 hard 样本。原 §5 的预标注规则把 `score<0.4` 直接判 reject，
与本节自相矛盾，已在 §5.2 反转。

后续若要拆项目（评估集 / 错误库 / 生产）：**P3 增强阶段**新增
`label_studio/label_configs/error_catalog.xml` + `task_exporter.py` 支持
`project_override` 参数，不影响 P1/P2 主链路。

## 4. 契约设计

### 4.1 Label Studio task 契约

由 `task_exporter.py` 完成 C3 → LS `task.data` 的映射。**「来源」列是实施要点**——
`task_id` 并不在 meta.json 里，只能从文件名解析：

| LS `task.data` 字段 | 类型 | **来源** | 用途 |
|---|---|---|---|
| `messages` | obj | `*.messages.json` **原生 dict** | 块视图（LS 自动 pretty-print） |
| `qf_text` | str | `*.qwenjina.txt` 全文 | Qwen3 ChatML 渲染 |
| `metadata` | obj | `*.meta.json` **原生 dict** | audit 元数据；`label_config` 绑 `metadata_text` 孪生字段（§5 R5） |
| `scorecard` | obj | `scorecard.py` 生成 | **评分卡**（§4.2），含依据；`label_config` 绑 `scorecard_text` |
| `messages_text` / `metadata_text` / `scorecard_text` | str | 上述结构化值的 JSON 序列化 | **LS 1.23 只吃字符串**（§5 R5），label_config 一律绑这些 |
| `task_id` | str | **文件名 stem 前缀** `^(T\d{3}\|E\d{3})__` | 反向追溯 |
| `session_id` | str | `meta.json:session_id`（或 stem 后缀） | 反向追溯 + **去重键**（§16 R7） |
| `criteria` | list[str] | `criterion_results.criteria` 摊平成 `[PASS] id (REASON) — message` | LS perItem 文本控件**只接受字符串列表**（§5 R5） |
| `training_value_score` | float | `meta["training_value_score"]` | LS 端排序/筛选 |
| `complexity_tier` | str | `meta["complexity_tier"]` | LS 端 filter |
| `inner_id` | str | `session_id` | 仍随 import 发出，但 **LS 1.23 会丢弃**（§16 R7） |

`task_exporter` 实现约束：

```python
# stem 形如 "T001__useramulation-xxx_refined"
m = re.match(r"^(?P<task_id>[TE]\d{3})__(?P<session_id>.+)_refined$", stem)
if not m:
    raise LabelStudioError(f"无法从 C3 文件名解析 task_id/session_id: {stem}")
```

LS task 不持有 raw trajectory / cookies / authorization；**C3 已是脱敏产物**。
未知字段容错：不认识的 `meta` 键原样并入 `metadata`（原生 dict，LS 会忽略
未在 label_config 中引用的键），无需 R5 的 `extra_metadata` 字符串转义层。

### 4.2 C4 契约：评分卡 `docs/contracts/C4-scorecard.md`

> 生产者 `label_studio/scorecard.py`，消费者 = Label Studio UI（人工核对）。

**设计要点**：不推单一标量，推**分层的指令评分 + 每层依据 + 来源可信度**。

```json
{
  "schema_version": "scorecard.v1",
  "task_id": "T001",
  "session_id": "useramulation-...",
  "overall": {
    "suggested_decision": "revise",
    "confidence": "medium",
    "basis_dimensions": ["criterion_coverage", "redline", "training_value"],
    "derivation": "指令项 4/6 PASS（含 1 项 FAIL）；无红线违规；质量分 0.58（3 维为估算）→ 建议 revise"
  },
  "dimensions": [
    {
      "id": "criterion_coverage",
      "label": "指令项达成",
      "score": 0.667,
      "score_kind": "ratio",
      "source": "measured",
      "evidence": [
        {"criterion_id": "C1", "verdict": "PASS", "reason_code": "found_in_reply",
         "message": "已提供推荐列表", "evidence_ids": ["ev_3f2a"]},
        {"criterion_id": "C4", "verdict": "FAIL", "reason_code": "missing_item",
         "message": "未给出具体库存数字", "evidence_ids": []}
      ]
    },
    {
      "id": "instruction_adherence",
      "label": "指令遵循",
      "score": "review",
      "score_kind": "enum",
      "source": "measured",
      "evidence": [
        {"loc": "step 12-14", "type": "required_change", "note": "删掉了要求的对比表"},
        {"loc": "step 20", "type": "regression", "note": "结论与前文矛盾"}
      ],
      "lost_elements": ["对比表"],
      "preserved_core": ["主推商品"]
    },
    {
      "id": "redline",
      "label": "红线合规",
      "score": false,
      "score_kind": "bool",
      "source": "measured",
      "evidence": [
        {"type": "prompt_injection", "step_location": 7, "evidence": "工具返回内容含指令注入"}
      ]
    },
    {
      "id": "training_value",
      "label": "轨迹训练价值",
      "score": 0.58,
      "score_kind": "ratio",
      "source": "partly_estimated",
      "components": {"health": 0.83, "judge": 0.6, "intent": 1.0,
                     "modified": 0.75, "diversity": 0.5, "noise": 0.9, "depth": 0.9},
      "weights":       {"health": 0.25, "judge": 0.25, "intent": 0.20, "modified": 0.10,
                        "diversity": 0.10, "noise": 0.05, "depth": 0.05},
      "estimated_components": ["health"],
      "estimated_because": {
        "health": "router.health_scores 未落盘，由 toolcall 成功率粗估（quality_scorer._avg_health_score）"
      },
      "evidence": [{"loc": "step 5, 11", "note": "2/12 toolcall 失败（Tavily 429）"}]
    }
  ]
}
```

**`source` 字段是必填，不是装饰**：

| 值 | 含义 |
|---|---|
| `measured` | 有真实落盘数据支撑 |
| `partly_estimated` | 部分分量为粗估（见 `estimated_components`） |
| `estimated` | 整体为推断 |
| `missing` | 数据缺失，不可评分（UI 须显示"不可用"而非 0） |

依据：[`gdr/core/quality_scorer.py`](../../gdr/core/quality_scorer.py) 的
`_avg_health_score` docstring 明写「health 来自 router.health_scores，runner
没把它落盘，这里按 message 数和总 toolcall 数做粗估」。**health 权重 0.25
（四分之一）是猜的**。不标 `estimated`，标注员会据 0.83 这样的数字做判定，
建立在假精度上。

### 4.3 评分卡分层（L0–L5）

| 层 | id | 数据源 | 评分 | 依据 | 现状 |
|---|---|---|---|---|---|
| **L0** | `criterion_coverage` | `criterion_results` | ratio of PASS | `criterion_id` / `verdict` / `reason_code` / `message` / `evidence_ids` | **需前置 F2 注入 C3** |
| **L1** | `instruction_adherence` | `TrajectoryCompareResult.instruction_adherence` | `pass/review/fail` | `diff_summary[{step_range,type,note}]` + `lost_elements` + `preserved_core` | 已有（`gdr/domain/scoring_schema.py`） |
| **L2** | `redline` | `RedlineResult` | `bool` | `labels[{type,step_location,evidence}]` | 已有 |
| **L3** | `block_validation` | `validation_summary` | passed/failed L1·L2·L3 | 计数 + `refine_history` | 已有 |
| **L4** | `training_value` | `training_value_score` + `quality_scorer_components` | ∈[0,1] | 七维分量 + 权重 + `estimated_components` | 已有 |
| **L5** | `edit_status` | `edit_status_summary` | 五态计数 | 计数 | 已有 |

**L0 是"指令评分"的主载体**——`criterion` 是用户指令的可验证条目，
[`simulate_serve/domain/validation.py::CriterionResult`](../../simulate_serve/domain/validation.py)
已有 `criterion_id` / `verdict(PASS|FAIL|INCONCLUSIVE|ERROR)` / `reason_code` /
`message` / `evidence_ids`，正是"明确的指令评分与依据"。

**当前缺口**：`criterion_results` **不在 C3 里**。`trajectory_archiver` 是纯字节
拷贝（[`_copy_with_retry`](../../simulate_serve/infrastructure/trajectory_archiver.py)），
[`gdr/parsers/`](../../gdr/parsers/) 对 `validation` / `criterion` 零引用，
验证结果只存在于 `output/runs/`。这是前置 F2（§12）要解决的。

`overall.derivation` 必须是人可读的推导链字符串，不得只给结论——标注员要能
判断这个建议是否合理。

### 4.4 SQLite：不改（理由）

原方案在 `orchestration/queue/schema.sql` 加 `ls_task_id` / `ls_pushed_at` 两列。
**本方案取消该设计**，三条理由：

1. **无消费者**——LS 标注结果不回流，队列无需知道推送状态（唯一用途是 fetch 去重）
2. **会撞 terminal phase**——step 9 已 `mark_phase(done)`，`done ∈ TERMINAL_PHASES`；
   若 `update_ls_task_id` 复用 `upsert_task` 会抛 `TaskAlreadyTerminal`，
   若走 `_select_task_by_task_id(for_update=True)` 则被
   `AND phase NOT IN ('done','dead')` 滤掉返回 `None`
3. **单列无法表达多 session**——C4/评分卡以 `<TXXX>__<session_id>` 为粒度，
   同一 `task_id` 可有多个 C3，单个 INTEGER 列无处安放

去重改用本地台账 `output/label_studio/push_index__<project_id>.jsonl`（§16 R7，
2026-09-29 按 LS 1.23 实测推翻原方案）。`orchestration/queue/` 仍保持零修改，
§13.3 的状态机不变量得以完整保留 —— 台账是 append-only 的纯文件，不进状态机。

## 5. Label Config 设计（标注界面）

### 5.1 XML 模板

```xml
<View>
  <Header value="Task: $task_id | Session: $session_id"/>
  <Header value="质量分 $training_value_score（$complexity_tier）| 建议 $scorecard.overall.suggested_decision | 置信 $scorecard.overall.confidence"/>

  <Tabs>
    <Tab value="评分卡">
      <Header value="自动评分与依据（逐维度展开，标注员核对后填下方指令核对）"/>
      <Table name="scorecard_view" value="$scorecard" editable="false"/>
    </Tab>

    <Tab value="指令核对">
      <Header value="逐条核对自动判定；不同意必填理由"/>
      <Filter toName="criterion" minlength="1"/>
      <TextArea name="criterion_text" toName="criterion" perItem="true" editable="false"/>
      <Choices name="criterion_verdict" toName="criterion" choice="single"
               perItem="true" required="true">
        <Choice value="agree"    hint="认同自动判定"/>
        <Choice value="disagree" hint="不认同，右侧写明理由"/>
      </Choices>
      <TextArea name="criterion_note" toName="criterion" perItem="true"
                placeholder="不同意时写明：应达成什么 / 实际达成了什么"/>
    </Tab>

    <Tab value="轨迹">
      <TextArea name="messages_view" value="$messages" editable="false"/>
    </Tab>
    <Tab value="ChatML">
      <TextEditor name="qf_text_view" value="$qf_text" editable="false"/>
    </Tab>
  </Tabs>

  <!-- toName 锚点：必须存在，否则 LS 加载 label_config 报
       "toName references missing tag"，三条控件全部无法工作 -->
  <TextArea name="task" value="$messages" maxSubmissions="1" visible="false"/>

  <Header value="最终判定（必选）"/>
  <Choices name="overall_decision" toName="task" required="true">
    <Choice value="accept"  hint="该轨迹质量达标"/>
    <Choice value="revise"  hint="需修改，备注修复方向"/>
    <Choice value="reject"  hint="拒绝，废弃该样本"/>
  </Choices>

  <Choices name="failure_mode" toName="task" required="false">
    <Choice value="reasoning_error"/>
    <Choice value="tool_use_error"/>
    <Choice value="formatting_error"/>
    <Choice value="hallucination"/>
    <Choice value="policy_violation"/>
    <Choice value="incomplete_response"/>
    <Choice value="other"/>
  </Choices>

  <TextArea name="revise_notes" maxSubmissions="1"
            placeholder="修复方向 / 修改建议（选填）"/>
</View>
```

两处对原模板的**硬修**：

1. **补 `<TextArea name="task" ... visible="false"/>` 锚点**——原模板三条控件
   （`overall_decision` / `failure_mode` / `revise_notes`）全部 `toName="task"`，
   但整个 XML 无任何 `name="task"` 的控件，LS 加载即报错、项目建不起来
2. **`revise_notes` 去掉 `toName`**——TextArea 是直接输入控件（`name` +
   `maxSubmissions`），`toName` 是 Choices/Text 的参数，用在这里是错误用法

### 5.2 预标注：只做风险提示，不做 accept/reject 预判

**删除**原规则：

```text
training_value_score ≥ 0.7 → accept          ← 已删
training_value_score < 0.4 → reject          ← 已删
```

理由：`0.40` 正是 `complexity_tier=hard` 的阈值，hard 样本是**最需要人工**的
（§3 自己就这么写），自动 reject 掉等于把最该看的样本排除。`0.7` 阈值则让
最不需要人看的 easy 样本自动通过。

**改为**（`POST /api/projects/{id}/import/predictions`）：

| 触发条件 | 提示文案 |
|---|---|
| L0 `criterion_coverage` 有 `FAIL` | 「指令未完全达成，请核对 C{n}」 |
| L2 `redline.violation = true` | 「红线违规，必须复核」 |
| L4 `estimated_components` 含权重 ≥0.20 的分量 | 「该分量为估算值，非实测」 |
| 以上皆无 | 「自动检查未见异常，仍需人工确认」 |

`overall_decision` **一律留空，强制人工选择**。这同时消解了"0.7/0.4 阈值
从哪来"的模糊——不再需要阈值。

> ⚠️ **落地约束（2026-09-29 实测，R14）**：LS 的 `result` 必须是
> `[{from_name, to_name, type, value}]` 列表，且 `from_name` 要命中 label_config
> 里的真实控件，**否则整条预测被静默丢弃**（`201 {"created": 0}`）。所以风险提示
> 文本不能当自由键塞进去，必须有控件承接：label_config 里的
> `<TextArea name="risk_hints" toName="task_anchor" editable="false"
> value="$risk_hints_text"/>`，预标注以 `type: "textarea"` 往这个块里填
> `value.text`。该块 `editable="false"` —— 风险提示是机器的判断，标注员改它等于
> 抹掉审计痕迹，要表达异议走 `criterion_verdict` / `revise_notes`。

## 6. 配置层

根 `config/config.yaml` 新增顶层 `label_studio:` 段：

```yaml
label_studio:
  base_url: "http://127.0.0.1:8099"            # 本地 LS
  api_key: "${LABEL_STUDIO_API_KEY}"           # 走 env；api_key_path 二者互斥
  api_key_path: null                           # 可选：从文件读，优先级高于 api_key

  # 项目配置
  project_title: "trajectory-sft-quality"      # 自动创建/复用
  label_config_path: "label_studio/label_configs/trajectory_review.xml"
  project_id: null                             # null 时按 title 查找

  # 上传策略
  upload:
    enabled: false                              # 默认关闭
    batch_size: 50                              # 保守设 50；LS 上限 250K/200MB
    include_predictions: true                   # 风险提示预标注（§5.2）
    filter_min_training_value_score: 0.0        # 0.0 = 全量
    filter_complexity_tiers: []                 # [] = 全部
    skip_task_ids: []                           # 排除特定 task
    dry_run_skip_threshold: 5000                # 超过此数需显式 --force 才真推

  # 评分卡
  scorecard:
    enabled: true
    require_estimated_flag: true                # source=estimated 必须显式标注（§4.2）
    drop_missing_dimensions: true               # source=missing 的维度不写入，
                                                # UI 显示"不可用"而非 0

  # 凭据扫描（§16 R11，P1 必做）
  # 注意: 正则必须用**单引号**。YAML 双引号标量里 `\s` / `\.` 会被当转义序列,
  # 整个 config.yaml 直接 ScannerError 加载失败; 单引号下反斜杠是字面量。
  credential_scan:
    enabled: true
    patterns: ['Bearer\s', 'password\s*=', '-----BEGIN .* PRIVATE KEY-----',
               'sk-[A-Za-z0-9]{16,}', 'ghp_[A-Za-z0-9]{20,}',
               'AKIA[0-9A-Z]{16}', 'eyJ[A-Za-z0-9_-]{10,}\.']
    on_hit: reject_task                         # fail-closed：拒推该 task

  # 健康检查
  health_check:
    timeout_seconds: 5
    retry_attempts: 3
    retry_backoff_seconds: 2.0

  # orchestration 自动 hook（task_pipeline step 11 引用）
  hook:
    enabled: false                              # 默认关闭
    hook_timeout_seconds: 30                    # 线程内 future 超时即放弃
```

**开关语义明确**：`upload.enabled` 只管 CLI 全量推送；`hook.enabled` 只管
orchestration step 11 自动推送。hook 的判定**只看 `hook.enabled`**，
`push_single_c3` 内部不再查 `upload.enabled`——避免两开关交叉导致 hook 静默空转。

**2026-09-29 修订（启用自动推送前必须先修的四个坑）**：

| 坑 | 原状 | 修法 |
|---|---|---|
| 超时预算塞不下 | `5s` 要串完 PAT refresh + 建连 + 查项目 + PATCH label_config + 建 task + 预标注 | 提到 `30s`。超时即丢样本（台账来不及写，下次 `upload` 又补推一遍） |
| 每 task 都 PATCH label_config | `task_pipeline` 传 `project_id=None`，每个 task 触发一次「查项目 + PATCH」。PATCH 是**覆盖**不是同步，会把标注员在 LS 上做的调整冲掉 | `ls_hook` 加进程级缓存 `(base_url, project_title) → project_id`，同批次只解析一次 |
| 台账多进程并发写 | `--parallelism ≥2` 时 N 个 Pool worker 写同一 jsonl，无锁。**实测无锁丢 59% 的行**（4 进程 × 40 条 → 只落 66/161 行） | `push_index.append` 加跨平台排他锁，锁加在 `<台账>.lock` 上，不污染 jsonl |
| `on_failure` 是死配置 | 定义、解析、校验、示例配置里都有，但 `ls_hook` **从未读取**，两个值行为完全相同 | 删除。推送是旁路，失败既不重试也不抛，本来就没有可配置的分叉；留着一个不生效的开关比没有更糟 |

凭据解析：`api_key_path` 优先于 `api_key`；两者都为空时 `status` / `init-project`
报清晰错误（不打印任何凭据内容）。yaml 中填真实 key 违反 §10 红线。

`config/config.example.yaml` 同步落 commit-safe 版本（占位符 + 注释，无真凭据）。

## 7. 模块结构

```
label_studio/
├── __init__.py
├── __main__.py                                # CLI: init-project / status / upload / purge
├── settings.py                                # LabelStudioSettings (frozen dataclass)
├── config_loader.py                           # load_label_studio_config()（同 orchestration.config_loader 风格）
├── client.py                                  # SDK 包装: create_project / list_projects / import_tasks
│                                              #   / import_predictions / health_check
│                                              #   约定: client 用完即弃，不做模块级单例（§9.3）
├── project_manager.py                         # init_project / get_or_create_project
│                                              #   / validate_label_config / list_existing_projects
│                                              #   / purge_tasks
├── task_exporter.py                           # C3 files → LS task.data + predictions
│                                              #   push_single_c3(...) / export_batch(...)
│                                              #   内含 §16 R11 凭据扫描
├── scorecard.py                               # C3 meta.json + run validation → scorecard.v1
│                                              #   build_scorecard(...) / derive_overall(...)
├── errors.py                                  # LabelStudioError / Unavailable / AuthFailed
│                                              #   / CredentialLeak
├── label_configs/
│   ├── trajectory_review.xml                  # 主模板（§5.1）
│   └── error_catalog.xml                      # 错误样本模板（P3 增强，可选）
└── tests/
    ├── __init__.py
    ├── test_client.py                         # SDK 重试 / auth / 网络 / 超时
    ├── test_project_manager.py                # create / get_or_create / validate / purge
    ├── test_task_exporter.py                  # 文件名解析 / 字段映射 / 凭据扫描 / batch_size
    ├── test_scorecard.py                      # L0–L5 构建 / source 标注 / 缺失维度 / derivation
    ├── test_config_loader.py                  # YAML 解析 + ${ENV} 占位 + 缺字段报错
    ├── test_label_config_xml.py               # XML 合法（POST /api/projects/{id}/validate/）
    ├── test_e2e_with_real_ls.py               # 需本地 8099；@pytest.mark.integration 默认 skip
    └── fixtures/
        ├── sample_c3_messages.json
        ├── sample_c3_meta.json
        ├── sample_c3_qwenjina.txt
        └── sample_run_validation.json         # CriterionResult 样本（前置 F2）
```

CLI 归属明确：`init-project` → `project_manager.init_project()`；
`status` → `client.health_check()`；`purge` → `project_manager.purge_tasks()`。

不修改 `simulate_serve / gdr / etl / configuration` 任何文件（前置 F1/F2 除外，
见 §12）。

## 8. CLI 设计

```powershell
# 1. 初始化项目（创建 LS project + 校验 label_config；幂等）
python -m label_studio init-project [--label-config PATH]

# 2. 健康检查（不创建 Run 日志；类似 simulate_serve --check-tools）
python -m label_studio status

# 3. 推送 C3 + 评分卡 → LS tasks
python -m label_studio upload
    [--dry-run]                                # 仅打印计划（含将推送的评分卡摘要）
    [--batch-size N]
    [--task-id TXXX]                           # 仅推送单个 task（调试）
    [--complexity-tier easy|medium|hard]
    [--min-score 0.5]
    [--no-scorecard]                           # 只推轨迹不带评分卡（排障用）
    [--force]                                  # 跳过 dry_run_skip_threshold 校验

# 4. 删除 LS 端 task（回滚用，危险）
python -m label_studio purge [--project-id N] [--confirm]
```

**已删除的子命令**：`fetch`（无回流，无 C4 落盘需求）、`sync`（等价于顺序跑
`upload`，不需要独立模块与 `sync_once`/`sync_loop`）。
相应删除 `sync.py` 与 `annotation_importer.py` 两个模块。

`status` 只读取 LS 服务并校验配置完整性（含凭据是否可解析），不创建 Run 日志。

## 9. orchestration 自动 hook

### 9.1 接入点

修改 [`orchestration/task_pipeline.py::_run_one_task_pipeline`](../../orchestration/task_pipeline.py)，
在 step 10 `mark_phase(done)` **之后**追加 step 11。

### 9.2 接入示意（伪代码）

```python
# orchestration/task_pipeline.py::_run_one_task_pipeline
# step 9: 标记 done + 写 etl 输出路径（已有）
messages_path, openai_path, qwenjina_path, meta_path = etl_outputs
queue.mark_phase(task_id, new_phase=PHASE_DONE,
                 etl_messages_path=messages_path,
                 etl_openai_path=openai_path,
                 etl_qwenjina_path=qwenjina_path,
                 etl_meta_path=meta_path)

# step 11: NEW — Label Studio 自动 hook（非阻塞 + 独立超时）
if label_studio_settings is not None and label_studio_settings.hook.enabled:
    try:
        from label_studio.task_exporter import push_single_c3   # 延迟 import
        ls_task_id = _push_with_timeout(
            fn=lambda: push_single_c3(
                meta_path=meta_path,
                task_id=task_id,
                session_id=session_id,
                settings=label_studio_settings,
            ),
            timeout_seconds=label_studio_settings.hook.hook_timeout_seconds,
        )
        log.info("ls push ok: task_id=%s session=%s -> ls_task_id=%s",
                 task_id, session_id, ls_task_id)
    except Exception as exc:  # noqa: BLE001 — 必须吞掉，绝不阻塞主链路
        log.warning("ls push failed (non-blocking): task_id=%s err=%s",
                    task_id, type(exc).__name__)
        # 不 mark_failed，不上 dead；queue 推进不变
        # 失败记入 output/orchestration/logs/ls_push_failures.log
```

**注意 step 11 不写 SQLite**（§4.4），因此没有跨线程写库问题。

### 9.3 超时实现（必须线程化）

§16 R3 / §15 要求"LS 慢响应时单 task 耗时增加 ≤ 5s"。同步 `try/except`
无法中断一个阻塞的 HTTP 调用，**必须**包一层线程：

```python
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

_PUSH_EXECUTOR = ThreadPoolExecutor(max_workers=1,
                                   thread_name_prefix="ls-push")

def _push_with_timeout(fn, timeout_seconds: float):
    fut = _PUSH_EXECUTOR.submit(fn)
    try:
        return fut.result(timeout=timeout_seconds)
    except FutureTimeout:
        fut.cancel()          # 可能无法真正取消，但主流程立即返回
        raise
```

配套约束（两条都必须遵守，否则会引入新 bug）：

1. **线程内不碰 SQLite**——`sqlite3.Connection` 默认 `check_same_thread=True`，
   跨线程使用会抛异常。step 11 已不写库（§4.4），天然满足
2. **client 用完即弃，不做模块级单例**——LS SDK client 非线程安全；
   每次 `push_single_c3` 内部新建 client、`finally` 里关闭。顺带规避
   `multiprocessing.Pool` 在 Windows(spawn) / Linux(fork) 下的行为差异
   （父进程若持有 client，spawn 下 pickle 会失败）

超时后线程仍在后台跑完，最长占用一个 `max_workers=1` 的槽位，不影响主流程。

### 9.4 关键约束

| 约束 | 理由 |
|---|---|
| 非阻塞 `try/except` | LS 不可达 / 凭据错误 / XML schema 错都不能让 task 进 `dead` |
| 独立超时（`hook_timeout_seconds=5`） | 同步调用无法中断，需线程化（§9.3） |
| 延迟 import `label_studio.*` | 与 `producer_simulate` / `workers.gdr_worker` 同模式 |
| **不改 `_worker_init`** | 不做任何 fork 重置：Windows `multiprocessing.Pool()` 走 spawn（`initializer` 会执行但父进程不持有 client 状态），Linux 走 fork。**client 用完即弃的约定下，fork 前后都无残留状态，无需任何 reset** |
| 不改 orchestration 状态机 | `phase=done` 不依赖 LS push 结果；队列零修改（§4.4） |

### 9.5 `PipelineSettings` 增量

```python
# orchestration/settings.py::PipelineSettings
max_parallelism: int = 1
max_retry_gdr: int = 3
max_retry_etl: int = 3
retry_poll_seconds: float = 2.0
# 新增：
label_studio_settings: LabelStudioSettings | None = None  # None = 模块整体关闭
```

`OrchestrationConfig` 在 `load_config()` 时多读一个顶层 `label_studio:` 段
（复用 `label_studio/config_loader.py::load_label_studio_config()`）。

`Master → PipelineExecutor → _run_one_task_pipeline` 三跳均需透传
`label_studio_settings`（原方案 §13.2 漏列 `orchestration/master.py`）。

## 10. 隐私与安全边界（CLAUDE.md 红线对齐）

CLAUDE.md 红线：不保存自由文本思维链、Cookie、Authorization Header 或浏览器 Profile。

| 边界 | 处理 |
|---|---|
| 思维链 | 按 CLAUDE.md 定义段：红线约束 **raw CoT** 外传。C3 含 **refined CoT**（经 `thought_refactor` 精修），属训练制品不受此限；已剥离 ⟦⟧ meta tag（F3-D） |
| Cookie / Authorization | C1/C2/C3 均不存（协议级保证）；但 `tool_call.input` 是 Agent 自主生成的自由文本，**可能**含用户传入的凭据字符串 —— 见 §16 R11 |
| 浏览器 Profile | 不存；无需处理 |
| LS API key | `${LABEL_STUDIO_API_KEY}` env，**禁止**落 yaml / 测试 / 文档 / 异常信息 |
| `refine_history` / `judge_*` / LLM 投票细节 | 随 `metadata` 推 LS 端（标注员需看）；不下载到外部服务 |
| LS 推送方向 | **只写不读**：本项目不消费 LS 侧任何数据，无落盘的标注副本 |

> **重要**：Label Studio 是本项目的**终点**。LS 侧的标注结果不回流本项目，
> 本项目不写 `output/labeled/`，不实现 fetch / loader。LS 实例为本地自托管。

## 11. 测试设计

| 测试文件 | 覆盖点 |
|---|---|
| `test_client.py` | SDK 重试 / auth 失败 / 网络错误 / 超时 |
| `test_project_manager.py` | create_project / get_or_create / validate label_config（无效 XML 抛错）/ purge |
| `test_task_exporter.py` | **文件名 task_id/session_id 解析**（含不匹配 stem 报错）/ 字段映射 / 原生 dict 透传 / **凭据扫描命中即拒推** / batch_size 切分 / 过滤规则 |
| `test_scorecard.py` | L0–L5 构建 / `source` 标注（measured vs estimated vs missing）/ `estimated_components` 收集 / derivation 可读性 / 缺字段不崩 |
| `test_config_loader.py` | YAML 解析 + `${ENV}` 占位 + 缺字段报错 + `api_key`/`api_key_path` 优先级 |
| `test_label_config_xml.py` | **XML 合法且 `toName` 全部有对应控件**（`POST /api/projects/{id}/validate/`） |
| `test_e2e_with_real_ls.py` | 可选；需本地 8099；`@pytest.mark.integration` 默认 skip |
| `orchestration/tests/test_ls_hook.py` | 成功 / 失败不阻塞 / 超时生效 / 并行不干扰 |

默认不访问 8099（类似 `simulate_serve --check-tools`，离线可跑）。

## 12. 前置依赖与实施分阶段

### 12.1 前置依赖（不属于本模块，但阻塞 P1）

| 编号 | 前置项 | 为什么阻塞 |
|---|---|---|
| **F1** | **etl transform 接线** — [`etl/writers/__init__.py`](../../etl/writers/__init__.py) 的 `render_to_4_views` 只实现 step 1，step 2-7 是 `raise NotImplementedError` 死代码；生产链路 [`orchestration/workers/etl_worker.py`](../../orchestration/workers/etl_worker.py) 的 `run_etl_once` 绕过 transform 直接 `save_session_v2` | `qf_text` 无源 → `*.qwenjina.txt` 不生成 → §4.1 的 `qf_text` 字段空、§5.1 的 ChatML Tab 空白。修法二选一：(a) 补齐 `render_to_4_views` step 2-7；(b) 在 `run_etl_once` 里 `load_refined_session` 与 `save_session_v2` 之间插入 `collect_usage → prune → transform → render_cleaned_system` |
| **F2** | **Criterion 注入 C3** — `CriterionResult` 目前只在 `output/runs/`，`trajectory_archiver` 是纯字节拷贝，`gdr/parsers/` 对 validation/criterion 零引用 | L0（指令评分）无数据源，评分卡只剩 L1–L5。**只增不改**：向 `session.metadata` 追加 `criterion_results` 键，不改 C1/C2/C3 既有字段 |

**F2 归属**：为守住 §13.3 的"零修改"承诺，注入点选在 **orchestration 的 etl_worker
侧**（或 producer 阶段把 run metadata 合并进 C1 头部），**不改 gdr 业务逻辑**。

### 12.2 分阶段

| 阶段 | 范围 | 工期 | 关键交付 |
|---|---|---|---|
| **F1** | etl transform 接线 | 1 天 | C3 产出 4 视图（`qwenjina.txt` / `openai.json` 真实内容） |
| **F2** | Criterion 注入 C3 | 0.5 天 | `meta.json:criterion_results` 可用 |
| **P1 核心推送** | client + settings + config_loader + project_manager + **scorecard** + task_exporter（含 R11 凭据扫描）+ 1 label_config + `init-project`/`status`/`upload` CLI + 单测 | 1.5 天 | `python -m label_studio upload` 端到端跑通，LS UI 可见评分卡 + 轨迹 |
| **P2 orchestration hook** | `orchestration/ls_hook.py`（线程化超时 + 失败隔离 + 进程级指标）+ `task_pipeline.py` step 11 + 单测 | 1 天 | orchestration 自动推送，队列零修改 |
| **P3 增强（可选）** | `error_catalog.xml` + `project_override` + 失败任务（`phase=dead`）补推 + 标注员一致性校验 | 1 天 | 多模板支持 |

**核心交付：F1 + F2 + P1 + P2 ≈ 4 天**（原方案 3.5 天，但原方案的 P2 反馈回流
已随架构转向删除，换来的是前置的评分数据接线）。

## 13. 文件清单

### 13.1 新增（实施后实际清单，2026-09-28）

```
label_studio/                                         ← 新模块根
├── __init__.py
├── __main__.py                                       ← CLI: init-project / status / upload / purge
├── settings.py
├── config_loader.py
├── client.py                                         ← httpx 直打 REST，不引 label-studio-sdk
├── project_manager.py
├── task_exporter.py                                  ← 含 R11 凭据扫描
├── scorecard.py                                      ← 评分卡生成
├── errors.py
└── label_configs/
    └── trajectory_review.xml

orchestration/
└── ls_hook.py                                        ← step 11 旁路推送（线程化超时 + 失败隔离）

tests/label_studio/                                   ← 跟随全仓 tests/ 约定（不放在包内）
├── c3_fixtures.py                                    ← 样本数据
├── conftest.py                                       ← C3 4 视图工厂
├── test_scorecard.py
├── test_task_exporter.py
├── test_client.py
├── test_project_manager.py
├── test_ls_config_loader.py
├── test_label_config_xml.py
├── test_ls_cli.py
└── test_e2e_with_real_ls.py                          ← @pytest.mark.integration, 默认 skip

tests/orchestration/
└── test_ls_hook.py

docs/contracts/
└── C4-scorecard.md                                  ← 评分卡契约

docs/
└── observability-label-studio.md                    ← 用户视角
```

**与原设计的偏差（3 处，均为实施中发现）**：

| 偏差 | 原设计 | 实施 | 原因 |
|---|---|---|---|
| 测试位置 | `label_studio/tests/` | `tests/label_studio/` | 全仓 `tests/` 单一入口（CLAUDE.md 目录索引）；且 `test_cli.py` / `test_config_loader.py` 与 `tests/unit/`、`tests/orchestration/` 同名，包内放置会触发 pytest rootdir 模式的模块冲突 |
| 样本数据 | 4 个 `fixtures/*.json` | `c3_fixtures.py` + `conftest.write_c3()` | 4 视图必须**同一 stem 成组**产出，拆成 4 个静态 JSON 反而要手工对齐文件名 |
| P2 接线 | 沿 `PipelineSettings` → `master` → `pipeline_executor` 逐层透传 | worker 内 `ls_hook.load_hook_settings()` 直读根配置 | 少 4 个文件的透传改动，且子进程 `spawn`/`fork` 两模式下行为一致 |

### 13.2 修改（实施后实际清单，2026-09-28）

| 文件 | 改动 |
|---|---|
| `orchestration/task_pipeline.py` | 新增 `_push_to_label_studio()`；step 11 在 `mark_phase(done)` **之后**调用 |
| `shared_config.py` | `ROOT_SECTION_KEYS` 增 `"label_studio"`（否则只含该段的配置不被 `is_root_config()` 识别） |
| `pyproject.toml` | 新增 `integration` marker（`--strict-markers` 下未登记会直接报错） |
| `config/config.example.yaml` | 顶层 `label_studio:` 段（commit-safe）。⚠ 正则**必须用单引号** —— YAML 双引号里 `\s` / `\.` 是转义序列，整个文件会 `ScannerError` |
| `CLAUDE.md` | 「配置和工具」追加 Label Studio 段；「外部副本例外 2」改为终点 + 评分卡措辞；「输出」段**不**加 `output/labeled/`（无回流） |
| `docs/contracts/README.md` | 契约一览加 C4 行 |
| `docs/orchestration-design.md` | §3 数据流图加 step 11；新增 §6.8 Label Studio 接入 |

### 13.3 零修改（关键不变量）

| 模块 | 原因 |
|---|---|
| `simulate_serve/*` | 完全独立（Criterion 数据只读，不改） |
| `gdr/*` | 完全独立（F2 注入点选在 orchestration 侧，守住此条） |
| `etl/*` | 完全独立（只被 `label_studio.task_exporter` 读 C3） |
| `docs/contracts/C1/C2/C3` | 字段语义不变（F2 只向 meta 追加新键） |
| `orchestration/queue/` | **完全零修改**（§4.4），7 个 phase 状态机与 Task dataclass 全部不动 |

## 14. 关键边界与 CLAUDE.md 红线对齐

| CLAUDE.md 红线 | 方案处理 |
|---|---|
| 不保存自由文本思维链 | C3 含 **refined CoT**（CLAUDE.md 定义段：红线只约束 raw CoT 外传）；已 F3-D ⟦⟧ 剥离；LS 本地自托管。登记为 CLAUDE.md「外部副本例外 2」 |
| 不保存 Authorization Header / Cookie | C1/C2/C3 均不存；但 `tool_call.input` 是 Agent 自由文本，**可能**含用户传入的凭据 —— 见 §16 R11（P1 起 fail-closed 拒推） |
| 凭据不得提交 / 打包 / 复制到日志 | `${LABEL_STUDIO_API_KEY}` env 注入；`errors.py` 统一脱敏；`api_key`/`api_key_path` 互斥且报错时不回显 |
| 多阶段流水线各守边界 | `label_studio/` 与 `simulate_serve/gdr/etl/orchestration` 平行；只读 C3；orchestration hook 只调 `task_exporter.push_single_c3()`，无循环依赖 |
| 数据保留原则（结构合格但评分低也保留） | 不影响。LS 推送不修改 `output/refine_data/*` / `output/refined/*` / `output/agent_trajectory/*`；低分样本正是人工复核的目标 |
| F3-D ⟦⟧ meta tag strip | 已落；LS 端读到的 C3 是剥离后版本 |
| 评分不得伪造精度 | 评分卡每维强制标 `source`（measured/estimated/missing），`health` 等粗估分量必须在 UI 可见（§4.2） |

## 15. 验收清单（F1 + F2 + P1 + P2，2026-09-28 实施状态）

图例：✅ 已验证 · ⬜ 需真实 LS 实例（离线跑不了）

### 15.1 前置

- ✅ F1：`render_to_4_views` 接通渲染链 → `*.openai.json` 不再空、`*.qwenjina.txt` 生成
  （`tests/etl/test_render_chain.py` 13 项）
- ✅ F2：`meta.json` 含 `criterion_results`，条目含 `criterion_id` / `verdict` /
  `reason_code` / `message`（`tests/orchestration/test_criterion_source.py` 17 项）

### 15.2 基础

- ✅ `status` LS 不可达时优雅报错，无 traceback
- ✅ `init-project` 幂等（同名复用 / 无则创建）+ label_config 校验失败带 LS 逐条消息
- ✅ label_config XML 静态校验：所有 `toName` 有锚点（`task` 与 `criterion` 两个 perItem 锚点）
- ✅ `upload --dry-run` 不建任何 client 连接，只打印计划 + 评分卡摘要
- ⬜ `upload` 真推送后 LS UI 可见 task + 评分卡 + 轨迹 + ChatML **四者皆有内容**
- ✅ 评分卡 `source` 语义：`health` 标 `partly_estimated` + `estimated_components` +
  `estimated_because`；`missing` 维度给 `score=None` 而非 0
- ✅ 评分卡可测维度不足一半时 `suggested_decision=None`（不给假建议）
- ✅ 预标注**不含** `overall_decision`（`test_hints_never_predicate_accept_or_reject`）
- ✅ `${LABEL_STUDIO_API_KEY}` 未设置时 `status` 报清晰错误且不回显凭据
- ✅ 含 `Bearer <token>` / `sk-*` / `ghp_*` / `AKIA*` / 私钥头的样本 → **fail-closed 拒推**，
  该 task 一条都不进 LS（`test_credential_leak_is_never_uploaded`）
- ✅ CLI stdout 是**单个** JSON 对象（拒推报告与推送结果合并，不做二次 print）
- ✅ 一次 `upload` 只建一个 client（项目解析与推送复用同一实例）

### 15.3 orchestration hook

- ✅ hook 关闭时**零副作用**（不建 client，`test_task_pipeline_helper_is_silent_when_hook_off`）
- ✅ `hook.enabled` 与 `upload.enabled` 互不串（配了 upload 不代表 hook 开了）
- ✅ LS 抛任何异常 → 吞掉，`phase=done` 不受影响（4 种异常类型参数化）
- ✅ LS 挂死 → `hook_timeout_seconds` 后放弃等待，**实测 <2s 返回**（设 0.3s 超时）
- ✅ LS 侧自己抛的 `TimeoutError` 不会被误记成"我们等超时"（用 `future.done()` 区分，
  否则 `ls_hook_timed_out` / `ls_hook_failed` 两个指标从此不可信）
- ✅ 线程内不碰 SQLite（step 11 在 `mark_phase(done)` 之后，且不写库）
- ⬜ 多子进程并行 `--parallelism 4` 实跑（client 用完即弃，设计上无共享状态）
- ✅ SQLite `tasks` 表**结构未变**（无 `ls_task_id` / `ls_pushed_at` 列 —— `orchestration/queue/` 零修改）

### 15.4 文档

- ✅ `docs/contracts/C4-scorecard.md` 落地
- ✅ `CLAUDE.md` 同步：「配置和工具」段 + 「外部副本例外 2」措辞
- ✅ `docs/observability-label-studio.md` 用户视角落地
- ⬜ `docs/orchestration-design.md` §3 / §6.2 加 step 11 + 新 §6.8

### 15.5 测试

- ✅ `uv run python -m pytest tests/label_studio -q` → 228 passed, 6 skipped（e2e 默认 skip）
- ✅ `uv run python -m pytest tests/orchestration -q` → 293 passed（含 `test_ls_hook.py`）
- ✅ `uv run python -m pytest -q` → **1052 passed, 9 skipped, 0 failed**
- ⬜ 真实 LS e2e：`LS_E2E=1 LABEL_STUDIO_API_KEY=... uv run python -m pytest
  tests/label_studio/test_e2e_with_real_ls.py -m integration --allow-hosts=127.0.0.1`

## 16. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R1 | LS SDK client 跨进程/跨线程状态污染 | **client 用完即弃**，不做模块级单例（§9.3/§9.4）；fork 前后无残留状态，跨平台均正确 |
| R2 | LS 宕机让 task 进 dead | hook 强制 `try/except` 吞所有异常，`phase=done` 不变，日志告警 |
| R3 | hook 阻塞慢 task 跑完 | `ThreadPoolExecutor` + `future.result(timeout=hook_timeout_seconds)`（§9.3）；线程内不碰 SQLite |
| R4 | 批量上传超过 LS 250K / 200MB 上限 | `upload.batch_size=50`；多 C3 文件分批 import |
| R5 | C3 meta.json 字段扩展破坏 label_config | 未知键原样并入 `metadata`（原生 dict），LS 自动忽略未引用键。**但 LS 1.23 的 `Text` / `TextArea` / `TextEditor` 绑定到结构化值（dict / list）时 import 直接 400 `data['xxx']=...`** —— 所以 `task_exporter` 额外产出 `messages_text` / `metadata_text` / `scorecard_text` 三个 JSON 字符串孪生字段，`criteria` 也从 list[dict] 摊成 list[str]（perItem 文本控件只收字符串）。label_config 一律绑孪生字段，结构化原值保留给下游脚本 |
| R6 | 标注员跳过"指令核对"直接给判定 | `criterion_verdict` 设 `required="true"` + `perItem="true"`；Tab 上标未核对数 |
| R7 | 推送重复 task（同一 session 跑多次） | ~~LS 原生 `inner_id = session_id` 去重。**不引入** `pushed_tasks.json` 索引~~ **2026-09-29 按 LS 1.23.0 实测推翻**：`Task.inner_id` 是**整数字段**（发字符串 400 `A valid integer is required.`），批量 `/import` 直接**静默丢弃**；整数 inner_id 重复导入**照样每次新建**（实测 42 推三次 → id 7/8/9）；`?inner_id=` 过滤被忽略；`fields=` 参数被忽略（永远回全量 data）。结论：**LS 侧没有任何可用的原生去重**，必须靠本地台账。台账同时解决第二个问题 —— `import/predictions` 的 `task` 字段只认 LS 侧数字 id，而 `/import` 返回体只有计数。格式见 `label_studio/push_index.py` |
| R8 | 标注员误删 / 误改 label_config | 标题匹配即复用项目，但**每次 `init-project` / `upload` 都把本地 XML 同步进项目**（`PATCH /api/projects/{id}`）。原设计"不主动覆盖"是错的：LS 端存的是**建项目那一刻**的 XML，而 `POST .../validate/` 校验的是递过去的 XML 文本、不是项目里存的那份 —— 不同步就会出现"`init-project` 报绿、`upload` 却 400 `data['xxx']`"这种对不上的现象。`purge` 仍必须 `--confirm` |
| R9 | 凭据泄露到日志 / 异常信息 | `errors.py` 统一脱敏（`client.__repr__` 不打印 api_key）；错误信息只报类型不报参数 |
| R10 | 评分卡暗示不存在的精度 | 每维强制 `source` 字段；`estimated_components` 显式列出；UI 必须可见。**这是本方案最容易做错的地方** |
| R11 | `tool_call.input` 是 Agent 自由文本，可能含用户传入的凭据字符串（协议级"trajectory 不存 Cookie/Auth"只保证框架不**主动**存，挡不住 Agent 自主写入） | P1 起 `task_exporter` 推送前对 `messages` / `qf_text` / `openai` / `metadata` 做敏感模式扫描（`Bearer ` / `password=` / 私钥头 / 常见 token 前缀），命中则**拒绝推送该 task**（fail-closed，不静默脱敏以免污染标注语义）。**实施改为结构化报告**（CLI 输出 `rejected[]` + `log.error`），不另开 `ls_push_failures.log` —— 那个文件本身就是一份要维护的状态，且多进程写同一文件没有好处。命中记录**只存位置**（view / pattern 序号 / 字符偏移），不存原文，避免凭据经由日志二次泄露 |
| R12 | 前置 F1/F2 未完成就开工 P1 | 评分卡只有 L1–L5、ChatML Tab 空白。**F1/F2 是 P1 的准入条件**，不并行开工 |
| R13 | label_config 的标签规则踩坑（2026-09-29 实测补） | ⚠️ **服务端校验不可信**：`POST /api/projects/{id}/validate/` 会为一份浏览器根本解析不了的 label_config 返回 **200**，`/import` 也照样 **201** —— task 推上去了，标注页打开是一屏 `Tag with name X is not registered`。**"服务端校验通过"不是证据。** 唯一可信的静态信号是标签白名单，由 `test_only_registered_tags_used` 守。已知规则：① `<Tabs>` / `<Tab>` / `<TextEditor>` **在本机 LS 1.23 未注册**（`<TextEditor>` 根本不是 LS 的合法标签名），分区改用 `<Header>`、只读展示改用 `<TextArea editable="false">`；② `<Filter>` 必须带 `name`，否则报 `Attribute name is required for FilterModel`；③ `<TextArea>` **恒需 `toName`**（与 `editable` / `visible` 无关），缺了报 `'toName' is a required property` 且**不告诉你是哪个标签**；④ perItem 锚点必须是 `<Text name="x" value="$list"/>` + 控件 `toName="x" perItem="true"`，**不能**自引用（import 必 400） |
| R14 | 预标注**一条都建不成而上报说成功了**（2026-09-29 实测发现，原诊断已推翻） | `POST .../import/predictions` 对格式错的预测照样回 **201**，而计数键是 `created` 不是 `task_count` → 原实现漏读该键、`predictions_pushed` 恒报 0。**当时据此推断"预标注其实建成了，只是报少了"，是错的**：回查 `/api/tasks/{id}` 实为 **0 条**。三格式实测：`result` 是 dict → `{"created": 0}`；`result` 是 region 列表且 `from_name` 命中控件 → `{"created": 1}`；`result` 是 region 列表但 `from_name` 不在 label_config → `{"created": 0}`。根因有二：① `result` 必须是 `[{from_name,to_name,type,value}]` 列表，原实现发的是 dict；② **LS 按 `from_name` 匹配控件，匹配不上整条静默丢弃** —— 所以"自由文本风险提示原样存下来供审计"（原 `build_prediction` docstring）**不成立**，风险提示必须有控件可落。已加 `<TextArea name="risk_hints">` 只读展示块 + 预标注指向它，`test_prediction_control_matches_label_config` 把两侧名字钉在一起。计数修正：`_task_count_from` 读 `created`，且**服务端给了计数就照抄（含 0）**，只有"一个计数键都没给"才按 sent 兜底 —— 把诚实的 0 覆盖成 sent 才是**假成功**，比假失败难查 |
| R15 | 标注页控件渲染出来但**不可交互**（2026-09-29，待定性） | 服务端侧已排除：项目存的 label_config 与本地一致（`init-project` 的 sync 生效）、task.data 字段齐全且类型正确、annotation 数 0（非"已提交"导致的只读）、`project_type` 为 null。**服务端给不出信号，只能靠标注页实际点**。已搭对照实验：一次性项目 `zz-probe-A-canonical`（LS 官方最小模板）vs `zz-probe-B-ourconfig`（本配置原样），A 通 B 不通即定位到配置 |

## 17. 不在本方案范围（明确划清）

| 不做 | 理由 |
|---|---|
| **LS 标注结果回流本项目** | **架构前提，非权衡**。本项目终点是 Label Studio：不实现 `fetch`、不写 `output/labeled/`、不提供 SFT loader、不消费 LS 侧任何数据 |
| 修改 C1/C2/C3 既有字段语义 | F2 只向 meta 追加新键，不改既有字段；契约文档仅需在 C3 增补 `criterion_results` 字段说明 |
| 修改 `gdr/*` 业务逻辑 | F2 注入点选在 orchestration 侧，守住 §13.3 |
| LS ML Backend 自动训练 | 工作量大且偏离"人工标注"目标 |
| 多 Label Studio 实例 / 多项目拆分 | P1–P2 单项目足够；P3 阶段按需扩展 |
| 评分卡回灌 gdr 用于调参 | 终点之后的事，超出本方案 |

## 18. 与现有文档的关系

| 既有文档 | 关系 |
|---|---|
| `CLAUDE.md` | 修改（「配置和工具」段 + 「外部副本例外 2」措辞；**不加** `output/labeled/`） |
| `docs/orchestration-design.md` | 修改（§3 / §6.2 加 step 11 + 新 §6.8 Label Studio 接入） |
| `docs/contracts/C1-trajectory-events.md` | 引用，不变 |
| `docs/contracts/C2-refined-session.md` | 引用（C3 字段来源） |
| `docs/contracts/C3-final-sft-views.md` | 修改（增补 `criterion_results` 字段说明，F2） |
| `docs/contracts/migration-plan.md` | 引用，不变 |
| `docs/设计方案/etl-prune-frontload.md` · `gdr-plan.md` | 参照风格 |
| `config/config.example.yaml` | 修改（新增 `label_studio:` 段 commit-safe 模板） |

## 19. 后续步骤

1. **F1 etl transform 接线**（1 天）— P1 准入条件
   - 修 `etl/writers/__init__.py::render_to_4_views` step 2-7，或在 `run_etl_once` 插入 transform 链路
   - 验证：`output/refine_data/` 出现非空 `*.qwenjina.txt` + `*.openai.json`

2. **F2 Criterion 注入 C3**（0.5 天）— P1 准入条件
   - 在 orchestration 侧把 `output/runs/` 的 `ValidationReport.criteria` 注入 `session.metadata["criterion_results"]`
   - 更新 `docs/contracts/C3-final-sft-views.md`
   - 验证：`meta.json` 含 `criterion_results`

3. **P1 环境准备**（半天）
   - 本地启动 Label Studio（`pip install label-studio` + `label-studio start --port 8099`）
   - 生成 `LABEL_STUDIO_API_KEY`（Settings → API Token），设 `$env:LABEL_STUDIO_API_KEY`
   - `python -m label_studio status` 验证连通性

4. **P1 实施**（1.5 天）
   - 落 `label_studio/{__main__,settings,config_loader,client,project_manager,task_exporter,scorecard,errors}.py`
   - 落 `label_studio/label_configs/trajectory_review.xml`（§5.1 模板）
   - 落 `docs/contracts/C4-scorecard.md`
   - 落 6 份单测 + 4 份 fixtures（含 R11 凭据扫描用例）
   - 端到端：`init-project` → `upload --dry-run` → `upload --task-id T001`

5. **P2 实施**（1 天）
   - 落 `ThreadPoolExecutor` 超时包装（§9.3）
   - 改 `orchestration/{task_pipeline,settings,config_loader,master,pipeline_executor}.py`
   - 落 `orchestration/tests/test_ls_hook.py`（成功 / 失败不阻塞 / 超时 / 并行）
   - 端到端：`python -m orchestration start --tasks T001` → LS 端出现 task，SQLite 结构未变

6. **P3 增强**（可选，1 天）

7. **文档同步**
   - 修改 `CLAUDE.md`（「配置和工具」+ 「外部副本例外 2」）
   - 修改 `docs/orchestration-design.md`（§3 / §6.2 / 新 §6.8）
   - 修改 `orchestration/README.md`
   - 新增 `docs/observability-label-studio.md`（用户视角）
