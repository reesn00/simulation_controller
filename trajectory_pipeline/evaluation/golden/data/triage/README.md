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
| `suggested_bucket` | **可选**：人当时看到的机器建议值（C-06）。留它是为了让「接受 / 改判」算得出来——事后重算会漂。**它不影响 `bucket`** |

## 怎么写

**批量（推荐，C-06）**：先出工作表 → 填三栏 → 导入。

```powershell
# 1. 先有分歧卡（缺卡 = 退 2，不拿"这批全都一致"顶替）
uv run python -m trajectory_pipeline.evaluation.golden.builder agreement --version 7 --emit-disagreements
# 2. 出工作表（机器已把事实、建议**与现场**填好；工作表**已存在不覆盖**）
uv run python -m trajectory_pipeline.evaluation.calibration.worklist build --version 7
# 3. **读现场**：现场就在工作表每一行的 scene 栏里（页面标题 / 每步观察 / 正文预览）
#    尺子（分支语义 D-4 / D-5、判据 D-10）在 v7/clauses.md
#    想一屏看完一条卡（现场 + 尺子 + 两侧）再用 show（只读）
uv run python -m trajectory_pipeline.evaluation.calibration.worklist show --version 7 --golden-id <ID>
uv run python -m trajectory_pipeline.evaluation.calibration.worklist show --version 7 --all
# 4. 打开 output/pipeline/calibration/v7/worksheet.jsonl，逐行填
#    bucket / triage_by / triage_evidence（其余字段是只读上下文、建议与现场）
# 5. 导入：默认 dry-run；**半填整批拒**并逐条列出缺哪几行
uv run python -m trajectory_pipeline.evaluation.calibration.triage import `
  output/pipeline/calibration/v7/worksheet.jsonl
uv run python -m trajectory_pipeline.evaluation.calibration.triage import … --apply
# 6. 看覆盖率：exit 0 全覆盖 / 1 还有未分诊
uv run python -m trajectory_pipeline.evaluation.calibration.worklist status --version 7
```

**第 3 步为什么不能省**：桶的前置事实写在 rubric 的**条款**里（「该给哪个分支
标签」在 D-5 决策树、「什么算播放组件」在 D-4），而现场在**预存档**里。
工作表的 `scene` 栏把现场直接放进**你填的那张表**：

| `scene` 的键 | 读什么 |
|---|---|
| `status` | `located`（读到了）/ `located_no_steps`（读到了存档但那步没观察）/ `not_found`（找了没找到）/ `not_read`（压根没读） |
| `title` / `steps` | 页面标题；每步一行（`3. click → video=6 iframe=10 元素=80/200 正文=6000字`） |
| `body` / `body_from` | 正文预览 + **它来自第几步**（正文常来自更早的一步，不标出来会读成最后一步） |
| `note` / `degraded` / `candidates` | 怎么定位到的；存档自报的降级标记；定位不到时存档里现有的单元与 url |

**只有结论没有现场，「该归哪个桶」就答不了**——那不是「人不肯判」，是表里没有
可判的东西。`scene` 是**只读上下文**，**不进分诊档**（导入时只留裁定字段），
也**不作数**：桶仍然由人填。

定位不到现场时 `scene` 会**明说定位不到**（并存下存档里现有的单元与 url 作候选），
**不返回空读数**：空读数会被读成「页面里没有 video 标签」，那是把「没找到」
当成「看到了没有」。

⚠️ 工作表上的 `suggested_bucket` 是**机器建议，不是裁定**——照抄之前先读
`suggested_reason` 里那句证据原话。机器判不了的（`suggested_bucket: null`）就看
`facts`（`pred_origin` 是谁判的 / `reached_play_page` 到没到播放页 /
`gold_reviewed_by` 金标谁审的）**与 `scene`** 自己判，**不要为了凑满而随手贴**。

**单条**：

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

> 导入时**只保留** `triage.KEPT_FIELDS`（裁定 + 签名 + 证据 + 当时的建议值）
> 这几栏，工作表里的上下文（证据原文 / 冻结行事实）**不会抄进裁定档**：那些会
> 随重算变，抄进来等于让今天的裁定引用下一版的观察。

## 第一轮实况（v7 · 2026-10-23）

`triage.jsonl` 首次入库 = **33 行，全部由人签**（`triage_by=reesn`，`status=assigned`），
`worklist status --version 7` 退 0：

```
分诊进度 33/33 = 100% · 机器建议 12 条（接受 6 / 改判 6）
  evaluator_logic     11  code
  evaluator_prompt     1  prompt
  rubric_ambiguous     0  rubric（只能人填）
  rubric_missing       0  rubric（只能人填）
  gold_defect         14  gold
  evidence_missing     7  collector
```

三条值得记住的实况：

- **机器建议一半被改判**（6/12）——预填是提示不是结论，这个比例就是它该有的样子；
- **5 张「现场定位不到」的卡里有 2 张判成 `evaluator_logic`**：页面压根没变成站点样本，
  代码却照样给了标签 ⇒ **读不到现场 ≠ 采集的错**，同名事实不能直接翻成桶；
- **`rubric_*` = 0 不表示「rubric 没问题」**：33 张卡全在 `d10` 一个维度上，
  d2–d9 九个分数维度**一条卡都没有**（软维度没有金标）。尺子的问题要等软维度金标。

## 谁读它

- `evaluation.calibration.verify --triage <本目录>/triage.jsonl --bucket <本次改动的桶>`
  → 「同桶清零」那一条判据；覆盖不全时该项**判不了**（不是通过）；
- `evaluation.calibration.gate` 透传同样的两个参数；
- 桶与前置事实的源码版本在 [`evaluation/calibration/buckets.py`](../../../calibration/buckets.py)。