# C4 — 评分卡 `scorecard.v1`

> `C3 → Label Studio` 交接面。**本项目终点**：只推送，不 fetch，不回流。
> 标注结果不回到本项目，本项目也不维护任何"已标注"状态。

设计依据：[`docs/设计方案/label-studio-integration.md`](../设计方案/label-studio-integration.md) §4。

## 1. 生产者 / 消费者

| 角色 | 实现 | 说明 |
|---|---|---|
| 生产者 | `label_studio/scorecard.py::build_scorecard` | C3 `*.meta.json` → 评分卡 |
| 消费 | Label Studio UI（人工核对） | `label_configs/trajectory_review.xml`「评分卡」页 |
| 消费 | `label_studio/task_exporter.py::build_prediction` | 转成 ML 预标注的 `risk_hints` |

## 2. 为什么是"分层评分 + 来源可信度"，而不是一个标量

`quality_scorer` 的 `health` 分量权重 0.25（四分之一），而
`gdr/core/quality_scorer.py::_avg_health_score` 的 docstring 自承：

> health 来自 router.health_scores，runner 没把它落盘。这里按 message 数和
> 总 toolcall 数做粗估

不标出来，标注员会拿 `0.58`（或分量 `health=0.83`）这样的数字做 accept/reject
判定，建立在假精度上。所以：

- 每个维度强制带 `source`
- `partly_estimated` 的维度必须列出 `estimated_components` + `estimated_because`
- 权重 ≥ 0.20 的估算分量进 `estimated_alert`，UI 必须提示

## 3. Schema

```jsonc
{
  "schema_version": "scorecard.v1",
  "task_id": "T001",
  "session_id": "useramulation-20260928-abc",
  "enabled": true,              // scorecard.enabled=false 时为 false，且无 dimensions

  "overall": {
    "suggested_decision": "revise",   // "accept"|"revise"|"reject"|null(数据不足)
    "confidence": "medium",           // "high"|"medium"|"low"
    "basis_dimensions": ["criterion_coverage", "instruction_adherence", ...],
    "derivation": "建议 revise：指令项 1/2 未通过",
    "note": "本判定为自动建议, LS 侧 overall_decision 必须由人工选择"
  },

  "dimensions": [ /* L0–L5, 见 §4 */ ],

  "dimension_summary": {"total": 6, "measured": 5, "estimated": 1, "missing": 0}
}
```

### 3.1 `source` 取值（必填，不是装饰）

| 值 | 含义 | UI 要求 |
|---|---|---|
| `measured` | 有真实落盘数据支撑 | 正常展示 |
| `partly_estimated` | 部分分量为粗估 | 必须展示 `estimated_components` |
| `estimated` | 整体为推断 | 必须标注为推断 |
| `missing` | 数据缺失，**不可评分** | 显示"不可用"，**不得显示 0** |

### 3.2 `overall.suggested_decision = null` 的情形

6 维中可评维度少于一半时**不给建议**。那种情况下"未见异常"只意味着"没看到
要看的地方"，写成 accept 就是卖弄确定性。`derivation` 会写明可评了哪几维。

## 4. 分层（L0–L5）

| 层 | id | 数据源（`*.meta.json` 的键） | `score` | `score_kind` |
|---|---|---|---|---|
| **L0** | `criterion_coverage` | `criterion_results` | PASS 比例 | `ratio` |
| **L1** | `instruction_adherence` | `trajectory_compare.instruction_adherence` | `pass`/`review`/`fail` | `enum` |
| **L2** | `redline` | `trajectory_free.redline` | `bool` | `bool` |
| **L3** | `block_validation` | `validation_summary` | passed/(passed+failed) | `ratio` |
| **L4** | `training_value` | `training_value_score` + `quality_scorer_components` | ∈[0,1] | `ratio` |
| **L5** | `edit_status` | `edit_status_summary` | EDIT 占比 | `ratio` |

**L0 是"指令评分"的主载体**。`criterion_results` 由前置 F2
（[`orchestration/criterion_source.py`](../../orchestration/criterion_source.py)）
从 `output/runs/<run_id>/` 注入 —— `gdr/parsers/` 对 validation 零引用，验证
结果原本只存在于 simulate 端产物里。

## 5. 评分依据（`evidence`）

每个维度的依据是**结构化的**，不是字符串：

| 维度 | `evidence` 项形状 |
|---|---|
| L0 | `{criterion_id, verdict, reason_code, message, evidence_ids, retryable}` |
| L1 | `{loc: "step 12-14", type, note}` + 顶层 `lost_elements` / `preserved_core` |
| L2 | `{type, loc: "step 7", note}` |
| L3 | `{loc: "L1", note: "passed=9 failed=0"}` |
| L4 | `{loc: <分量名>, note: "score=0.83 weight=0.25"}` + `estimated_because` |
| L5 | `{loc: <状态>, note: "count=2"}` |

`overall.derivation` 必须是**人可读的推导链**，不得只给结论 —— 标注员要能判断
这个建议是否合理。

## 6. 与 LS task 的关系

评分卡经 `label_studio/task_exporter.py::build_task_data` 挂在
`task.data["scorecard"]`。相关的另外两个字段：

- `task.data["criteria"]` —— `criterion_results.criteria` 摊平的列表，绑
  label_config 的 perItem 控件（用扁平列表而非 LS 的 data path 过滤语法，
  后者各版本行为不一致）
- `task.data["inner_id"]` = `session_id` —— LS 原生去重键

## 7. 预标注：只做风险提示

`build_prediction` 产出的 ML 预标注**不含 `overall_decision`**（方案 §5.2）：

| 触发条件 | 提示文案 |
|---|---|
| L0 有 `FAIL` | 「指令未完全达成，请核对 C{n}」 |
| L2 `violation = true` | 「红线违规 {n} 项，必须复核」 |
| L4 估算分量权重 ≥0.20 | 「该分量为估算值，非实测: …（合计权重 …）」 |
| 以上皆无 | 「自动检查未见异常，仍需人工确认」 |

自动 accept/reject 预判会把最该看的 hard 样本自动 reject 掉（`0.40` 正是
`complexity_tier=hard` 的阈值），与"优先标注 hard 样本"自相矛盾。

## 8. 边界

- 本项目**不消费**标注结果，无 `fetch` / `sync` 端点
- `orchestration/queue/` **零修改**（不记 `ls_task_id` —— 没有回流就没有消费者，
  且 step 10 已 `mark_phase(done)`，`done ∈ TERMINAL_PHASES` 会撞
  `TaskAlreadyTerminal`）
- 评分卡**不含** raw trajectory / cookies / authorization；C3 已是脱敏产物
- 推送前跑 R11 凭据扫描，命中 **fail-closed 拒推**（`label_studio/task_exporter.py`）
