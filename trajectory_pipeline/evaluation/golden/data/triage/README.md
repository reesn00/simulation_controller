# 分诊档（人填，**入版本控制**）

方案 16（评估器与 rubric 双向校准回路）里，分歧卡是**派生数据**（可重建，
落 `output/pipeline/calibration/v{N}/`），而这里的分诊是**人工裁定**——
不可重建，丢了无法还原，所以和 `annotations.jsonl` 一样留在数据根里（D-C1）。

## 一行一条

`triage.jsonl`，JSONL，一条 `(golden_id, dimension)` 一行：

```json
{"golden_id": "E006__2729220b__e9b9542b", "dimension": "d10", "bucket": "evaluator_logic",
 "triage_by": "owner", "triage_evidence": "按 D-3.5 明写条款应给 subscription_wall，金标无歧义",
 "responsibility": "code", "status": "assigned", "triage_at": "2026-10-23T10:00:00+08:00"}
```

| 字段 | 说明 |
|---|---|
| `golden_id` / `dimension` | 配对键，与快照/分歧卡同一个（`d10` 或软维度名 `d2`…`d9`） |
| `bucket` | **六桶封闭集**：`evaluator_logic` / `evaluator_prompt` / `rubric_ambiguous` / `rubric_missing` / `gold_defect` / `evidence_missing`。没有 `other`——不认识就留空（卡停在 `open`） |
| `triage_by` | 分诊人 id。**机器身份一律拒写**（`agent` / `llm:*` / `bot` 等）：`rubric_*` / `gold_defect` 是尺子与真值的裁定，agent 至多写建议值（D-C3） |
| `triage_evidence` | **必填**，写「为什么**不是别的桶**」。只写"判错了"等于没写 |
| `responsibility` | **由桶推导**，不用手填（手填冲突会被拒） |
| `status` | 默认 `assigned`；状态机见方案 16 §3.3（`accepted_exception` 是状态不是桶） |
| `triage_at` | 写入时间，`add` 自动填 |

## 怎么写

```powershell
# 先校验（缺省读本目录的 triage.jsonl；档不存在 = 退出码 2，不是"0 行没事"）
uv run python -m trajectory_pipeline.evaluation.calibration.triage check

# 追加一条：默认 dry-run，看完再加 --apply
uv run python -m trajectory_pipeline.evaluation.calibration.triage add `
  --golden-id E006__2729220b__e9b9542b --dimension d10 `
  --bucket evaluator_logic --by <你的名字> `
  --evidence "按 D-3.5 明写条款应给 subscription_wall，金标无歧义"
```

改判**默认拒绝**，要 `--replace`；旧值留在 git 历史里——这就是分诊的留痕方式。

## 谁读它

- `evaluation.calibration.verify --triage <本目录>/triage.jsonl --bucket <本次改动的桶>`
  → 「同桶清零」那一条判据；覆盖不全时该项**判不了**（不是通过）；
- `evaluation.calibration.gate` 透传同样的两个参数；
- 桶与前置事实的源码版本在 [`evaluation/calibration/buckets.py`](../../../calibration/buckets.py)。