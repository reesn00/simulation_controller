# 从 Label Studio 导出标注：用法与实测差异

> 这份文档面向**要把标注结果拿去用的人**。
> 面向维护者的部分在 [`observability-label-studio.md`](observability-label-studio.md)（怎么看、怎么填）、
> [`设计方案/label-studio-integration.md`](设计方案/label-studio-integration.md)（为什么这么设计）、
> [`label-studio-playbook.md`](label-studio-playbook.md)（改配置的方法论）。

本文所有关于导出格式的结论都是 **2026-09-29 在本机 Label Studio 1.23.0 上实测**的，
不是抄文档。网上流传的导出声说有几处与本机不符，已在 §2 逐条标出。

## 1. 这份导出是干什么用的

LS 原始导出是**工程元数据与标注结果的混合体**：一个 task 的输入数据、LS 自己的
管理字段（`id` / `lead_time` / `completed_by` / `was_cancelled` …）、以及标注员
点出来的东西，全挤在同一个 JSON 里。直接丢给训练框架会格式错误。

本项目里这份导出有四个具体去处：

| 用途 | 靠哪个字段 | 消费方 |
|---|---|---|
| **筛训练集** | `overall_decision` = `accept` 的样本进 SFT；`reject` 丢弃；`revise` 进返修队列 | 训练侧数据准备 |
| **取修正稿** | `content_revision` = `corrected` 时，取标注里的 `*_view` 列而不是 `*_text` 列 | 训练侧数据准备 |
| **质量归因** | `failure_mode` 的分布 → 哪类错误最多 | 调 prompt / 调精修策略的人 |
| **校准判据** | `criterion_verdict` = `some_disagree` → 读 `criterion_note` 里点名的 criterion_id，对应 `configuration/` 里的判据本身有问题 | catalog 维护者 |

前两项消费的是**数据**，后两项消费的是**结论**。

> ⚠️ 第四项**拿不到结构化的 `criterion_id` 字段**——LS 1.23 上逐项控件不可用（定性见
> §5.3），归属只能由标注员写在自由文本里。这一项因此是**人工汇总**而不是自动聚合。

> ⚠️ 关于第四项：CLAUDE.md 写的是「LS 是终点，标注结果不回流」。那句话约束的是
> **本项目的代码不消费标注**（不实现 fetch、不落 `output/labeled/`、
> `orchestration/queue/` 零修改），**不是**说人看了结论也不能改配置。判据写得不对
> 就该改配置——那是改 `configuration/` 里的 YAML，不是改数据流水线。两者不冲突。

## 2. 两种导出格式（本机实测，与网络说法有出入）

`GET /api/projects/<id>/export?exportType=<TYPE>`，本机支持 `JSON` / `JSON_MIN` /
`CSV` / `TSV`。

|  | `JSON` | `JSON_MIN` |
|---|---|---|
| 行数 | **每个 task 一行** | **每个 annotation 一行** |
| 标注结果的位置 | `annotations[].result[]`（嵌套，需逐层解析） | **摊平成列**，按控件名直接取值 |
| 同一 task 标了两次 | 挤在同一个 `annotations` 数组里，要自己挑 | **两行**，按 `updated_at` 挑 |
| `was_cancelled` | ✅ 有 | ❌ **没有** |
| `lead_time` | ✅ 有 | ✅ **仍然有** |

实测（project 29，同一 task 连续提交两次）：

```
JSON_MIN: 2 行
   overall_decision = reject  updated_at = 2026-09-29T10:13:39  annotation_id = 3
   overall_decision = accept  updated_at = 2026-09-29T10:13:37  annotation_id = 2

JSON: 1 行, annotations 数组 2 条
   2026-09-29T10:13:37  was_cancelled=False  [{'choices': ['accept']}]
   2026-09-29T10:13:39  was_cancelled=False  [{'choices': ['reject']}]
```

### 三处与网络说法不符的地方

1. **「JSON_MIN 只保留 `from_name` / `to_name` / 结果值」——不准确。** 本机的
   `JSON_MIN` 是**扁平行**：每个 `task.data` 的键和每个**控件名**各自成为一列。
   实测列名（project 27）：

   ```
   annotation_id, annotator, created_at, updated_at, id, lead_time,
   task_id, session_id, complexity_tier, training_value_score, criteria_text,
   messages, messages_text, messages_view, qf_text, qf_text_view,
   metadata, metadata_text, metadata_view, openai,
   scorecard, scorecard_text, scorecard_view,
   audit_text, audit_view, risk_hints_text, risk_hints,
   criterion_verdict, criterion_note, overall_decision, failure_mode,
   revise_notes, content_revision
   ```

   实际效果比文章描述的**更省事**——不用解析嵌套区间，直接按列名取。

2. **「JSON_MIN 剔除 `lead_time`」——不成立，`lead_time` 还在。** 它剔除的是
   `bulk_created` / `ground_truth` / `parent_annotation` / `result_count` /
   `prediction` / `drafts` / 评论区那一套。

3. **「按 `was_cancelled` 跳过取消的标注」——在 `JSON_MIN` 下做不到**，那一列
   不存在。只有 `JSON` 能筛。

### 选哪个

**推荐 `JSON_MIN`**，理由是本项目的标注控件都是**扁平**的（没有 NER 那种带
起止偏移的区间），摊平后直接按列名取值，省掉整层 `result[]` 解析。

代价是**放弃按 `was_cancelled` 过滤**。本项目不使用取消功能，所以可以接受；
如果将来引入「标注员标错了作废重标」，就得改用 `JSON` 自行过滤。

## 3. 取哪条 annotation

`JSON_MIN` 已经是每个 annotation 一行，按 `updated_at` 取最新即可：

```python
rows.sort(key=lambda r: r["updated_at"])
latest = rows[-1]
```

同一 task 标了多次时，**旧的那几条不代表任何人的最终意见**——`lead_time` 之类
的管理字段更不能用它们算工时。

## 4. 字段映射（本项目控件 → 下游字段）

| LS 控件 | 导出列 | 类型 | 缺省行为 | 含义 |
|---|---|---|---|---|
| `overall_decision` | `overall_decision` | 字符串 | 无值则该行不可用 | `accept` / `revise` / `reject`，**必选** |
| `content_revision` | `content_revision` | 字符串 | 同上，**必选** | `unchanged` / `corrected` / `unusable` |
| `failure_mode` | `failure_mode` | 字符串或列表 | 列可能整个不存在 | 多选 |
| `revise_notes` | `revise_notes` | 字符串 | 列可能整个不存在 | `revise` 时事实上必填，但 LS 无条件必填机制 |
| `criterion_verdict` | `criterion_verdict` | 字符串 | 无值则该行不可用 | `all_agree` / `some_disagree` / `none_agree`，**必选**。⚠️ 整块判定，**不含 criterion_id**（见 §5.3） |
| `criterion_note` | `criterion_note` | 字符串 | 列可能整个不存在 | `some_disagree` 时点名 `criterion_id: 理由` |
| `risk_hints` | `risk_hints` | **dict** `{"text": [...]}` | 恒有（预标注） | 机器提示，见 §5.2 |
| 展示块 | `messages_view` / `qf_text_view` / `metadata_view` / `scorecard_view` / `audit_view` / `criteria_view` | dict `{"text": [str]}` | 恒有 | 标注员**修正稿** |

### 4.0 选项的显示文本与导出键是**两套**

标注员在页面上点的是**中文**，导出里落的是**英文键**。2026-09-30 起用
`<Choice value="..." html="..."/>` 把两者分开（之前页面上直接显示机器键加角标，
`accept[2]`）。**写脚本一律按右列匹配**：

| 控件 | 页面显示 | 导出值 |
|---|---|---|
| `overall_decision` | 通过 / 需修改 / 拒绝 | `accept` / `revise` / `reject` |
| `content_revision` | 原样未改 / 已修正 / 内容不可用 | `unchanged` / `corrected` / `unusable` |
| `criterion_verdict` | 每一条都认同 / 有哪几条不认同 / 自动判定整体不可信 | `all_agree` / `some_disagree` / `none_agree` |
| `failure_mode` | 推理错误 / 工具调用错误 / 格式错误 / 幻觉 / 违规 / 回复不完整 / 其他 | `reasoning_error` / `tool_use_error` / `formatting_error` / `hallucination` / `policy_violation` / `incomplete_response` / `other` |

```python
# ❌ 标注员看到的是「拒绝」，导出里没有这个字符串
if row["overall_decision"] == "拒绝": ...

# ✅
if row["overall_decision"] == "reject": ...
```

> ⚠️ **`alias=` 不是替代方案。** 实测服务端 `parsed_label_config`：写成
> `<Choice value="KEY_C" alias="显示C"/>` 之后，LS 认的选项标识会从 `KEY_C`
> **变成** `显示C`（`control_weights` 里也是），前端点一下提交上去的就是中文。
> 这个错没有任何信号——validate 报绿、import 201、标注能提交，只有读导出时才发现
> 对不上。`html=` 只进 `labels_attrs`，标识仍是 `value`。

### 4.1 未填的列是**不存在**，不是 null

实测：没填 `criterion_note` 和 `revise_notes` 时，这两列在 `JSON_MIN` 里**根本
不存在**（不是 `null`、不是空串）。

```python
notes = row.get("revise_notes", "")   # ← 必须用 .get()
```

写成 `row["revise_notes"]` 会直接 KeyError，而且**只在"标注员没填"的那批样本上
崩**——跑通十条之后才崩一次，最难查。

### 4.2 `JSON_MIN` 里原值与修正稿是**两列并存**

这是本项目最重要的一个结构特性。实测（project 27，标注员删改过 `messages_view`）：

```
      messages_text (135119) vs messages_view (134951)   不同 ← 标注员改过
            qf_text (115927) vs qf_text_view (115927)    完全相同
      metadata_text (262216) vs metadata_view (262216)    完全相同
   scorecard_text (   6161) vs scorecard_view (   6161)   完全相同
        audit_text (     3) vs audit_view (     3)        完全相同
    risk_hints_text (    36) vs risk_hints (        73)   不同 ← 多了一条 submission
```

`*_text` 列来自 `task.data`（机器原值），`*_view` 列来自标注结果（标注员提交
的值）。**所以下游不必只信 `content_revision` 那个开关，两列直接比对也是独立
的第二重证据。** 保留开关是因为比对 13 万字符不现实，而且"提交了但没改"与
"压根没提交"在只有 `_view` 列时长得一样。

⚠️ 但这只在 `JSON_MIN` 下成立。`JSON` 导出里展示块的修正稿和真正的决策混在同一个
`annotations[].result[]` 列表中，**不靠列名区分**——要按 `from_name` 自己分。
这就是推荐 `JSON_MIN` 的第二个理由。

> ⚠️ **`scorecard_text` 是唯一不是 JSON 孪生的 `*_text` 列。** `messages_text` /
> `metadata_text` 是原对象的 JSON 序列化（`json.loads` 能还原），而
> `scorecard_text` 是评分卡**渲染成的人读文本**（结论先行 + 维度分行 +
> 依据逐条，2026-09-30 改，见 [observability §3.4](observability-label-studio.md)），
> `json.loads` 会失败。需要结构化评分卡就取 `scorecard` 列，它始终是完整的
> `scorecard.v1` dict。
>
> 这个改动对下游是**改善**：要判断"机器给了什么分"，读渲染后的文本比 diff
> 13 万字符的 JSON 现实得多；真要程序化处理，`scorecard` 列没动过。

## 5. 本项目的清洗点

对应业界常见的三类。对照实测结果：

### 5.1 空值过滤

`overall_decision` 与 `content_revision` 都是 `required="true"`，所以正常路径下
不会有空判定。但**防御性过滤仍然要做**，因为 LS 的 `required` 只在 UI 层拦，
API 直接写入的标注不受约束：

```python
if row.get("overall_decision") not in ("accept", "revise", "reject"):
    continue          # 没有明确判定的样本不进任何下游
```

### 5.2 去重：一条样本可能带多条 submission

实测 `risk_hints` 存了**两条一模一样的**内容（预标注一条 + 标注员点了一次 Add）。
所有 TextArea 型控件的 `value.text` 都是**列表**，长度不保证为 1：

```python
texts = row["risk_hints"]["text"]
hint = texts[0] if texts else ""
```

不要假设 `value.text` 是字符串——本项目所有展示块和预标注都是列表。

### 5.3 逐条归属是**自由文本**，不是结构化字段 ⚠️ 消费时注意

`criterion_verdict` 现在是**整块三选一**（`all_agree` / `some_disagree` /
`none_agree`），选了 `some_disagree` 时，具体是哪几条不认同**由标注员写在
`criterion_note` 里**（形如 `media.playable: 实际给了可播放链接`）。

这意味着 §1 第四种用途（**校准判据**）拿不到可直接聚合的 `criterion_id` 字段：

```python
# ❌ 不要指望有这个字段
disputed = row["criterion_ids"]          # KeyError

# ✅ 只能解析自由文本，或者干脆把 note 交给人看
note = row.get("criterion_note", "")     # 未填时这一列**不存在**
if row.get("criterion_verdict") == "some_disagree" and not note.strip():
    ...                                   # 选了却不点名 = 无效标注，退回人工
```

**为什么是这样**：LS 1.23 上「列表文本 + 逐项控件」这条路走不通——
`perItem` / `perRegion` 都只出 1 组单选，`<Chat>` 的 import 消息不可选，
`<List>` 在本机不渲染。定性过程见 [`todolist.md`](todolist.md) 已完成的 P0 第 2 项。

> 换句话说：**`criterion_verdict` 的作用从「记录哪几条不认同」退化成「记录整体
> 认不认同」**。第四种用途还在，但要从「自动聚合哪些 criterion_id 有问题」改成
> 「人工读 `criterion_note` 汇总」。

### 5.4 `criteria_text` 换行分隔，逐条要自己 split

清单在 `task.data["criteria_text"]` 里是**换行分隔的单串**（每行一条
`[VERDICT] criterion_id (REASON) — message`），**不是列表**——LS 把列表绑给
文本标签会 400，绑给 `<Text>` 会用 `,` 连成一整段（实测 6 条挤成一行，逐条核对
连读都读不下去）。

```python
rows = (row.get("criteria_text") or "").splitlines()   # 结构化原值在 metadata 里
```

结构化的 criterion 判定始终在 `metadata.criterion_results.criteria`
（dict 列表，含 `criterion_id` / `verdict` / `reason_code` / `message`），
要程序化处理用那个，不要解析 `criteria_text`。

## 6. 样例：从 JSON_MIN 到可训练样本

```python
import json, pathlib

def load(path):
    """JSON_MIN 导出 → 每个 session 一条最终判定。"""
    rows = [json.loads(l) for l in pathlib.Path(path).read_text("utf-8").splitlines() if l.strip()]
    by_session = {}
    for r in rows:
        sid = r.get("session_id")
        if not sid:
            continue
        # 同一 session 多次标注取最新（§3）
        prev = by_session.get(sid)
        if prev is None or r.get("updated_at", "") > prev.get("updated_at", ""):
            by_session[sid] = r
    return by_session

def to_training_sample(row):
    decision = row.get("overall_decision")
    if decision != "accept":            # §5.1 防御性过滤
        return None
    if row.get("content_revision") == "unusable":
        return None

    # §4.2 修正稿与原值分列 —— 按 content_revision 选边
    use_corrected = row.get("content_revision") == "corrected"
    def col(name):
        v = row.get(name)
        if v is None:
            return ""
        t = v.get("text") if isinstance(v, dict) else None
        if isinstance(t, list):
            return "\n".join(t)          # §5.2 列表，不是字符串
        return t if isinstance(t, str) else str(v)

    return {
        "session_id": row["session_id"],
        "messages": col("messages_view" if use_corrected else "messages_text"),
        "decision": decision,
        "failure_modes": row.get("failure_mode"),
    }
```

## 相关文档

| 关注点 | 文档 |
|---|---|
| 未完成项（标注界面待验证项、存储规模、待清理的 probe 项目） | [`todolist.md`](todolist.md) |
| 标注页怎么看、每个区填什么 | [`observability-label-studio.md`](observability-label-studio.md) §3.4–3.5 |
| 评分卡 `scorecard.v1` 字段级 schema（C4 契约） | [`contracts/C4-scorecard.md`](contracts/C4-scorecard.md) |
| 推送侧设计、label_config 踩坑记录（R13–R16） | [`设计方案/label-studio-integration.md`](设计方案/label-studio-integration.md) |
