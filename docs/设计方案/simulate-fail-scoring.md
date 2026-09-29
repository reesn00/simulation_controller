# 验证不通过数据的保留与归因评分(2026-09-29)

## 问题

`orchestration/task_pipeline.py` 的 `_SIMULATE_FAIL_STATES` 原本把 simulate 端
**所有**非 SUCCESS 终态判为死信,包括 `guide_exhausted` 与 `inconclusive`。

T001(找《武林外传》全集在线免费观看的可播放网址)是第一个撞上的实例: 远端
Agent 出于版权理由拒答, run 以 `guide_exhausted` 收场, orchestration 直接
`mark_dead`, 轨迹不进 gdr / etl。

后果与 CLAUDE.md「数据保留原则」直接冲突 —— 该原则只允许三类**结构严重不可用**
的数据进死信(轨迹不完整 / 仅有用户无 assistant / 无明确总结回复), 而这条轨迹
结构完整、含 assistant 实质回复。**验证不通过 ≠ 数据不可用**: 拒答样本恰好是
训练「模型该在什么情况下拒绝」的高价值素材, 丢弃它等于丢失信号。

## 判据修正

判据从「验证是否通过」改成「**数据结构是否可用**」:

| RunState | 旧 | 新 | 理由 |
|---|---|---|---|
| `guide_exhausted` | dead | **继续** | 引导耗尽/拒答, 轨迹通常完整 |
| `inconclusive` | dead | **继续** | 语义待定, 数据可用质量存疑 |
| `validation_error` | dead | dead | 基础设施故障 |
| `executor_error` | dead | dead | 远端执行失败, 可能无轨迹 |
| `actor_error` | dead | dead | 本地 actor 故障 |
| `cancelled` | dead | dead | 人为中断 |
| `interrupted` | dead | dead | 非终态恢复 |
| `completion_incomplete` | dead | dead | 完成度截断(死信三判据之一) |

改后 `guide_exhausted` / `inconclusive` 的轨迹正常流到 gdr → etl, 终态由 gdr
自身判定: 评分低 → `audited`(旁路 jsonl), 正常 → `done`(C3 训练制品)。
gdr 侧对 `validations.jsonl` **零引用**, 不会因 simulate 验证失败而二次丢弃。

## 失败归因与 0 分

「验证不通过」这个信号不能随死信一起消失, 但也**不需要靠丢数据表达**。
`orchestration/fail_evaluator.py` 在 etl 阶段(与 F2 的 `criterion_results` 注入
相邻)把两样东西发给 LLM 做一次归因:

* **验证不通过原因** —— `criterion_source.load_criterion_evaluation` 已读出的
  `final_verdict` / 每条 criterion 的 `reason_code` + `message` / `missing_items`
* **agent 轨迹结果内容** —— C1 trajectory 最后一条 `model_response` 的 text block

### 分层: 分数不交给 LLM

评价结果的 `score` **恒为 `0.0`**, `score_source="simulate_validation"`。
LLM 只负责**定性归因**, 不参与打分:

| 字段 | 来源 |
|---|---|
| `score` | simulate 端确定性校验 (恒 0) |
| `failure_category` | LLM, 闭集 7 类 |
| `root_cause` / `agent_intent` | LLM |
| `should_revise_task` | LLM —— 该改任务定义还是改模型 |
| `review_priority` / `review_note` | LLM |

把打分交给 LLM 会让同一批数据每次评价分数漂移, 且与 `ValidationReport` 的
fail-closed 判定脱钩。定性归因则稳定可复现, 且能回答「该改任务还是改模型」
这类比例分回答不了的问题。

### raw CoT 红线

C1 trajectory 的 text block **内嵌 QwenPaw 的原始 `<think>` 推理链**。本模块
的产物就是「发给 LLM 的 prompt」, 所以 `extract_final_reply()` 在**任何 LLM
调用之前**剥离 `<think>...</think>`; 未闭合的 `<think>`(trajectory 截断时)
连同其后内容一并丢弃 —— 半截推理链比没有推理链更危险。

这正是 CLAUDE.md 隐私红线约束的 **raw CoT 外传**(refined CoT 是训练制品, 不受此限)。

### 鲁棒性

后端不一定支持 `response_format.json_schema` —— `llm_client` 会退化成「把
schema 拼进 prompt」, 模型仍会改写键名(实测 `root_cause` → `root__agent`)。
因此:

1. `_FIELD_ALIASES` 按候选顺序取第一个非空值, 字段名漂移能收敛回 schema
2. 解析不完整则带**明确字段名**重试一次(`_MAX_ATTEMPTS=2`)
3. 全程 fail-soft —— LLM 挂掉/超时/输出垃圾, 返回归因为空但 `score=0.0` 的
   结果, **绝不阻断 etl**。分数信息比归因重要, 不能一起丢。

## 落点

`session.metadata` 是 C3 meta.json 的**全量平铺**(`gdr/domain/schema.py` 的
`save_session_v2`), 所以注入即落盘:

```
output/refine_data/<task>__<session>.meta.json
├── criterion_results          # F2: simulate 端验证结果(已有)
└── fail_evaluation            # 本次新增
    ├── score: 0.0
    ├── failure_category: "refusal"
    └── ...
```

`label_studio/scorecard.py` 的 L0 `criterion_coverage` 消费它:
`final_verdict != "pass"` 时**记 0 分**(而非 PASS 比例), 并把归因透到
`build_risk_hints`, 让验证未通过的样本在人工复核界面显式可见。

比例分会把「3/5 条通过但任务整体失败」显示成 0.6, 掩盖「这条数据不可用」的
事实。同时 `build_risk_hints` 原先只筛 `verdict == "fail"`, `inconclusive`
的 criterion 完全隐身 —— 已一并修正。

## 边界

- 本次**不补人工审查 CLI 入口**, 审查仍靠 `orchestration status` + 直接翻
  `output/` 与旁路 jsonl。
- 恢复历史死信数据用 `python -m orchestration replay`(会重跑 simulate, 重新
  调一次远端 Agent); 若只想复位 DB 不想重跑, 见 `requeue_dead()`。

## 相关

- 契约: [pipeline-contracts.md](pipeline-contracts.md) §4.5 第 5 步
- 评分卡: [../contracts/C4-scorecard.md](../contracts/C4-scorecard.md)
- 代码: [orchestration/fail_evaluator.py](../../orchestration/fail_evaluator.py)、
  [orchestration/criterion_source.py](../../orchestration/criterion_source.py)、
  [label_studio/scorecard.py](../../label_studio/scorecard.py)
