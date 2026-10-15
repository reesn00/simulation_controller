# 05 · rationale 与闸门域

**职责：** [`rationale/`](../../rationale/) 三件套——`lexicon` / `gates` / `writer`，
执行期生成理由 → P1 落盘 → 组装期**逐条核闸门** → P2/P3 渲染。
对应 [架构总览](../01-架构总览.md) 的**模块 4**。

**核心纪律（§5.5 最贵的一条）：** **fail-closed 管的是「结论」不是「采集」**——
能力不可用时返回 `answer=None`，**绝不猜**；且 `None` 不中断采集，
只有 `False` 才中断。

---

## 当前状态

| 能力 | 状态 | 说明 |
|---|---|---|
| `lexicon` / `gates` / `writer` 三件套全链路接通 | ✅ | 执行期生成 → P1 落盘 → 组装期逐条核闸门 → P2/P3 渲染 |
| 搜索步 rationale 漏生成 | ✅ 已补生成闭环 | `_write_rationales` 泛化后搜索步 settle 成功也走 writer（模型从 `TRAJECTORY_LLM_*` 环境变量取）|
| 真实站点上验证 | ✅ | 第一批 3/3 全部 `written`，内容如实反映「无法确认」而非编故事 |
| 采集侧 mojibake 防护 | ⚠️ **离线成立，从未被真实站点触发** | 见下 |
| 观察充分性两档（hard / capping-only） | ✅ INC42 | judge 有效覆盖 1/14 → **7/14** |
| `truncated` 字典假阳性修复 | ✅ INC42 | 四处同步，各加独立 `_truncated_flag` |

---

## 解码损坏（mojibake）的采集侧防护（2026-10-11 落地）

> 本节放在 rationale 域而不是采集域，因为**它的价值在于保护闸门**——
> 防护本身落在采集侧，但理由是闸门侧的。

清空前有一批存档的观察正文含 U+FFFD（`mojibake` ×30，涉 9 份），
成因当时**没查清**就删了数据——那批数据原文不可恢复（字节丢了），
但成因已查清并落地防护。

**实测成因**：`tv.sohu.com` 响应头**不带 charset**，编码只由 HTML 里唯一的
`<meta charset=GBK">` 声明，而实际字节确实是 GBK——**声明与实际一致**。
损坏发生在**页面内容经 MCP 传出时被按 UTF-8 处理**：按 GBK 严格解码 0 个替换符，
按 UTF-8 解则同一段产出数千个。

⚠️ **损坏是局部的**：同一段里中文与 ASCII 都好，只有中间夹着一小片烂掉。
「整页换个编码写错了」这个直觉判断在这里**是错的**，
而它正是让第一次排查绕远路的原因。

**防护落点**（三个变异已封：阈值函数 + 采集侧两个接线点）：

| 环节 | 位置 |
|---|---|
| 检测 | [`executor/dom.py::is_mojibake`](../../executor/dom.py) ——阈值 `MOJIBAKE_MIN_COUNT = 3`，**不是「出现即降级」** |
| 记降级 | [`executor/browser/obscura_driver.py`](../../executor/browser/obscura_driver.py) → `body_mojibake` / `title_mojibake` |
| 契约 | [`assembler/schema.py`](../../assembler/schema.py) 区分「键缺失」与「键在但内容是烂的」 |
| 门禁信号 | [`executor/integrity.py`](../../executor/integrity.py) 的 `mojibake` / `evidence_mojibake` |

**为什么拦在采集侧而不是闸门里**：坏正文一旦进感知层，rationale 闸门的
实体核查会把「模型引用了页面上真实存在、但已损坏的实体」判成**幻觉**
——**凭空造出一条理由通顺、格式正确、不报任何错的负样本**。
而 [`rationale/gates.py::observation_shortfall`](../../rationale/gates.py) 见到
`degraded` 非空就拒绝判幻觉（rubric D-9 边界规则），所以只要采集侧记一次降级，
闸门侧就自动获得保护——**不需要在闸门里再加第二道门**。

这与「观察被截断 → 不判幻觉」是同一条纪律：**判断建立在残缺事实上时，
正确做法是记不可判定，而不是拿残缺事实去指控。**

⚠️ **阈值定在 3 的理由**：单个 U+FFFD 可能来自页面真实内容（页面上就印着一个 �），
正文里偶发一两个不该让整份观察降级成不可判定——那会把「观察不充分」这条
降级路径变得过于宽松，最后人人都忽略它。实测坏站一个正文里有**数十个**，
好站是 0，两侧都离得很远。

**尚未验证**：防护只在离线测试里成立，**没被真实站点触发过**。
第一批（2026-10-11）3 个站的正文全是 0 个 U+FFFD，**一次都没触发**——
「已修」目前仍只是离线成立。要证伪需要跑一个已知会坏编码的站
（`tv.sohu.com` 是已知案例）。

---

## INC42 · 观察充分性两档（2026-10-15）

> **修判定面，不修 fail-closed 纪律**：降级观察的闸门一寸未动。

### 缺陷一：`truncated` 字典假阳性（四处同步）

p2 样本 `six.observations` 的 `truncated` 是 **dict**
（`{'source': False, 'view': False}`），四处 `_observation_shortfall`
的 truthy 判断把非空 dict 一律判真 → `E001__57bbf995__site-2` 被误标
「观察被截断」。修复：各加 `_truncated_flag`（**独立实现**，靠 contract 盯守）
——dict 按 `any(values)` 判，任一子观察截断才算。

- 落点：[`rationale/gates.py`](../../rationale/gates.py)、
  [`evaluation/scorers/code_scorer.py`](../../evaluation/scorers/code_scorer.py)、
  [`d3_scorer.py`](../../evaluation/scorers/d3_scorer.py)、
  [`d9_scorer.py`](../../evaluation/scorers/d9_scorer.py)
- contract 新边界：全 False 字典（四处可判）/ 含真值字典（四处不可判）

### 缺陷二：capping-only 观察一律 fail-closed → judge 覆盖 1/14

INC41 实测 13 个 undetermined 里：**8 对是纯元素裁剪（80/N）+ body 完好**
（479~2098 字符、无降级、未截断）、3 对真实降级、1 对 truncated 假阳性、
1 对无数据。原判据把「裁剪」与「降级」同等对待，全部挡在 LLM 门外。

**两档修复（仅 `d9_scorer`；`_observation_shortfall` 本身不动，contract 不受影响）**：

1. **hard**（降级 / 截断 / 全空 / 没有观察）→ 仍 fail-closed
   （§5.5 最贵教训：采集失败与「真没有」在存档里同形）；
2. **capping-only**（`_capping_only`：无降级、未截断、body 与保留元素非空、
   仅 `elements_total` 超出保留条数）→ 放行 LLM，但 judge 结果按
   **方向性规则**兜底（`_CAPPING_SENSITIVE_VERDICTS`）：
   - `anchored` / `vague` / `hindsight` 照常出分——保留区里找到的锚必为真
     （裁剪只减不增），vague / hindsight 是措辞判断；
   - `mild_hallucination` / `systematic` / `misleading` 降为 undetermined——
     「没找到锚」可能是「锚被裁掉」，**不可区分** → fail-closed；
     judge 原始 verdict / evidence 留在字段供审计。

### 验证（15 对工作表 × 两轮真跑，MiniMax-M3.1-Flash-Preview）

| 指标 | 修复前（INC41） | 修复后 |
|---|---|---|
| judge 有效覆盖 | 1/14 | **7/14** |
| A/B 两轮一致 | 14/14 | 13/14（唯一分歧 E014 step0：judge 自身输出翻转 anchored↔mild_hallucination，闸门两轮处理均正确，**翻转方向安全**）|
| 降级观察 fail-closed | 3/3 | 3/3（**不放松**）|
| 方向性降级触发 | — | 4 对（judge 判 mild_hallucination 但观察被裁 → undetermined，证据留档）|

新出分对：`E001 site-2`（mild_hallucination，字典修复解锁）、`E005__3bd50dfb`（vague）、
`E021__2ab12498 step0`（anchored）、`E014 step0`（A 轮 anchored）、
`TPIN02__3dcdbc14 step0`（anchored）；另 4 对 judge 判 mild_hallucination
被方向性降级——judge 证据显示这些 rationale 确有存疑断言（如 E014「免费专区」无锚），
但按纪律不排除「被裁掉」，不出分。

---

## 四处 `_observation_shortfall` 的决策边界一致性契约

四处实现刻意分开（`evaluation → rationale` 单向依赖方向），但「能不能判」
必须口径一致，否则 D-9 闸门过 + d9_scorer undetermined（或反之）。

| 实现 | 位置 | `obs=None` 守卫 | 已知差异 |
|---|---|---|---|
| `gates.observation_shortfall` | [`rationale/gates.py`](../../rationale/gates.py) 闸门三查 | 返回「没有观察」 | — |
| `code_scorer._observation_shortfall` | [`evaluation/scorers/code_scorer.py`](../../evaluation/scorers/code_scorer.py) | **不**做 None 守卫 | callers 永传非 None，本契约不背锅 |
| `d3_scorer._observation_shortfall` | [`evaluation/scorers/d3_scorer.py`](../../evaluation/scorers/d3_scorer.py) | 返回「没有观察」 | — |
| `d9_scorer._observation_shortfall` | [`evaluation/scorers/d9_scorer.py`](../../evaluation/scorers/d9_scorer.py) | 返回「没有观察」 | — |

**契约测试**：[`tests/contract/test_observation_shortfall_consistency.py`](../../tests/contract/test_observation_shortfall_consistency.py)
——10 条盯**决策边界**（`can_judge` 向量两两一致），**不**盯字面措辞。

**措辞漂移巡检工具**（reviewer 视角，**不是** contract 视角）：
[`tests/contract/tools/observation_shortfall_wording_diff.py`](../../tests/contract/tools/observation_shortfall_wording_diff.py)
——4 处实现 × 5 case，3 种输出格式（默认 markdown / table / unified diff）。

```powershell
uv run python trajectory_pipeline/tests/contract/tools/observation_shortfall_wording_diff.py
uv run python trajectory_pipeline/tests/contract/tools/observation_shortfall_wording_diff.py --format table
uv run python trajectory_pipeline/tests/contract/tools/observation_shortfall_wording_diff.py --format unified
```

**关键约束**：**不进 CI 门禁**——工具是 reviewer 视角的人工 drift 巡检，
不是机器判等（决策边界对齐 ≠ 措辞一致）。

---

## ⚠️ 本域已知未闭环项

| 项 | 现状 |
|---|---|
| **3 对 degraded 观察的解锁只能靠采集层** | body 采集降级根因，与「感知层主瓶颈」议题同源 |
| **judge 输出非确定性** | INC42 录到 E014 step0 翻转 `anchored`↔`mild_hallucination`；如需消除，judge 调用加 `temperature=0` 或多数投票，属 `llm/client` 层**独立批** |
| **mojibake 防护从未被真实站点触发** | 要证伪需跑 `tv.sohu.com` |
| **降级正文可能只是部分加载的导航栏** | INC43 明确**降级观察不做付费墙救济**——这是本条纪律的延伸 |

---

## 相关章节

- 编年史：[`90-增量编年史.md`](90-增量编年史.md) §增量四十二（观察充分性两档）· §增量四十三（降级观察不救济）
- rubric：[`../设计方案/在线视频场景rubric.md`](../设计方案/在线视频场景rubric.md) D-9 边界规则
- 早期编年史：[`../07-增量档案.md`](../07-增量档案.md)（mojibake 追溯）
