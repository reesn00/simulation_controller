# P2 · 样本契约

> 交接面是**格式**，不是 Python 对象——所以这份文档描述的是
> `samples/<id>.json` 的字段，任何一侧改形状都应在此同步。
> 「读 P1 不 import `executor`」这条纪律由
> `tests/contract/test_p2_contract.py::TestNoExecutorImport` 用 AST 强制。

## 切条粒度

**一条 = 一个决策单元**：搜索阶段一条，每个站点一条。
不把「选哪个站」与「这个站行不行」压进同一次预测。

## 顶层结构

```json
{
  "sample_id": "T001__b98b3e56__site-5",
  "task_id": "T001",
  "unit": "site-5",
  "title": "功夫",
  "source_archive": "T001__b98b3e56.json",
  "six": { ...七件套... },
  "gate": { "status": "passed", "reasons": ["逐条核查 1/1 步通过"] },
  "rationale_missing": false,
  "outcome": { ... },
  "reflection": { ... } | null,
  "degraded_from": []
}
```

`sample_id` = `<task_id>__<run_id>__<unit>`，**排序即时间序**，
且从 id 就能反查源存档，不必开文件。

| 顶层键 | 说明 |
|---|---|
| `sample_id` | `<task_id>__<run_id>__<unit>` |
| `task_id` / `unit` / `title` | 切片轴；`unit` 是 `"search"` 或 `site-N` |
| `source_archive` | 据以生成的 P1 **文件名**（不含目录），批次可回溯 |


## 六件套（`six`）

| 键 | 类型 | 说明 |
|---|---|---|
| `system_prompt` | `str` | 常量，**随样本冻结**——改一次历史样本之间就不可比 |
| `tools` | `list` | **仅 `visible_to_model()` 的那些**。`new_tab` 留在动作流里（删了无法回放）但不进工具声明 |
| `user_prompt` | `str` | persona 渲染后的**用户原话**，不是检索式 |
| `provenance` | `object` | 采样切片轴，**控制流不读它** |
| `actions` | `list` | ④ 动作+参数。**没有 `ref`**——会话句柄不可回放 |
| `observations` | `list` | ⑤ 观察原文（可长于训练态渲染） |
| `rationales` | `list` | ⑥ 思考+结论，**与 `actions` 按 `step_index` 对齐**（见下） |
| `reflection` | `object` \| `null` | INC46：**unit 级终局反思**（与 `rationales` 平级，不属于任何一步）。形状同 `WriteResult.to_json`（`status` / `rationale` / `reason` / `degraded`），于是与 `steps[i].rationale` 同构。`null` = 没生成（writer 未接 / 后端不可用 / 生成失败）——与「生成了但内容为空」不同 |

### ⑥ `rationales`（2026-10-10 由单值 `rationale` 改来）

```json
{"step_index": 0, "text": "...", "conclusion": "...",
 "status": "written", "reason": ""}
```

- **与动作逐步对齐**。执行期逐步生成，压成一条就对不上动作，
  而时序核查本来就需要 `step_index`。
- **缺一条不补空串、不挪位**：按出现顺序猜会在中间缺一条时整体错位，
  而错位的 rationale 会被实体核查当成凭空引入的实体——**凭空造出负样本**。
- `status` 原样带 P1 的三态（`written` / `unavailable` / `empty`）。
  后两者 `text` **留空**，不合成占位：「模型这轮没说话」与「压根没生成」
  必须分得开。

### `gate`（三值）

| 状态 | 含义 |
|---|---|
| `passed` | 逐条核查过，无 hard 发现 |
| `failed` | 有 hard 发现，`reasons` **定位到第几步** |
| `not_run` | **没有可核查的 rationale**——不是「通过」 |

`rationale_missing`（一条都没写成）与 `gate` **分开记**：
`gate=passed` 但 `rationale_missing=true` 意味着样本只有动作没有思考，
报告上看 `gate` 是看不出来的。

### `degraded_from`

P1 里**没有**的键（老存档缺 `steps` / `user_prompt` / `run_config` / 全文正文…）。
非空即表示这条是**降级重建**的，**别当成与新存档同等的样本混着用**。

> 「键不存在」与「字段为空」必须分得开——两者在 `to_json` 之后都渲染成 `""`。

## `outcome`

指回 P1 的那一条。样本里**不复制 evidence 原文**，只留指针——
P1 是真值源，复制就产生第二份需要同步的副本。

`branch is None` 表示**成功**（P1 的 ledger 只记失败分支），
所以「无分支」不是「没结论」。

`p1_missing_fields` 逐条记「P1 里压根没有这个键」——
老存档缺字段时，那一条**不可判定**而不是「字段缺失」。