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
| 采集侧 mojibake 防护 | ✅ **已在真实站点上触发（2026-10-25）——但触发点在浏览器端，采集层修不了** | 见下 · 编年史 §增量九一 · [避坑 §6.5](../02-避坑指南.md) |
| 观察充分性两档（hard / capping-only） | ✅ INC42 | judge 有效覆盖 1/14 → **7/14** |
| `truncated` 字典假阳性修复 | ✅ INC42 | 四处同步，各加独立 `_truncated_flag` |
| **rationale「动作前动机」口径** | ✅ INC46；**分代护栏 2026-10-25 落地** | 生成时不喂本步观察；闸门窗口 `[:step_index]`；终局反思落 unit 级 `reflection`。**产物侧按代标记 + 时代感知闸门**（见下节）；**存量 63 档 / 458 步仍待重生成**（≈767 次调用） |
| D-9 few-shot 示例池 | ⚠️ **INC46 已清空** | 旧六例是事后反思口径，与新口径冲突；零样本出分并标注 `fewshot=none`，待重生成后从 manual-review 桶人工重选 |

---

## 解码损坏（mojibake）的采集侧防护（2026-10-11 落地）

> 本节放在 rationale 域而不是采集域，因为**它的价值在于保护闸门**——
> 防护本身落在采集侧，但理由是闸门侧的。

清空前有一批存档的观察正文含 U+FFFD（`mojibake` ×30，涉 9 份），
成因当时**没查清**就删了数据——那批数据原文不可恢复（字节丢了），
但成因已查清并落地防护。

**实测成因（两代读数，2026-10-25 更正）**：`tv.sohu.com` 响应头**不带 charset**，
编码只由 HTML 里唯一的 `<meta charset=GBK">` 声明。

- **2026-10-10（HTTP 直取字节）**：按 GBK 严格解码 **0 个替换符**、按 UTF-8 解则同一段
  产出数千个 ⇒ 当时判「字节是好的，坏在**经 MCP 传出时被按 UTF-8 处理**」。
- **2026-10-25（真采，[`research/probes/probe_obscura_gbk2.py`](../../research/probes/probe_obscura_gbk2.py)，
  commit `d01ee0d`）**：浏览器 DOM 里**已经是坏的** ⇒ 那段 HTML **字节本身就是混合损坏的**
  （声明 GBK、夹无效字节），浏览器按 GBK 解码即产出 U+FFFD。

⇒ **结论：换任何执行层都救不回**（坏在浏览器拿到字节之前）。⇒ **防护不撤**——
它仍是唯一挡住「坏正文进感知层 → 凭空造出理由通顺的假负样本」的那道闸。
⚠️ **两代读数对不上、未区分**：要对外告站点，得**先重取一次字节**（同一 URL 两次取到的是不是
同一批字节、有没有夹无效字节）再比对；「经 MCP 传出」那条假说**至今没有独立证据**。

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

## 增量四十六 · rationale「动作前动机」口径改造（INC46）

**日期**：2026-10-15 · **owner 拍板**：改采集层 writer 口径，P3 渲染顺序不动。

### 问题

P1 `steps[i].rationale` 原本是**事后反思**：writer 生成时喂了
`action[i] + observation[i]`，文本描述的是刚看到的页面。而 P3 把
`rationale[i]` 渲染在 `tool_call(action[i])` **之前**——模型还没导航就先
评价页面内容，训练在教**未卜先知**。

实测证据 `E005__e35b7455.json` site-4：首步是 `goto`，rationale 已经写下
「当前页是…越剧特别版…虽列出 8 期链接」。

### 口径

第 `i` 步的 rationale = **执行第 `i` 个动作之前的动机与预期**。
生成时只喂历史（前 `i` 步的动作与观察）与当前动作，**不喂本步观察**。

- 历史观察**只最近 5 步带**，且用瘦身形状（url / page_title /
  body_text(500) / body_truncated / elements_total）——不带 elements/links
  明细，防 prompt 线性膨胀拖成 `unresolved`；
- `write_one` 的 `error` 参数**删除**：动作没执行谈不上失败，
  而失败原因本就在 P1 `steps[i].error` 落盘；
- 终局反思走**另一个入口** `write_reflection`，落 **unit 级 `reflection` 键**
  （与 `steps` 平级，不进 `outcome`——那是采集事实层）。

### 连带迁移

| 位置 | 改动 |
|---|---|
| `rationale/gates.py` | 可见窗口 `[:step_index]`（**不含本步**），后见窗口 `[step_index:]`（**含本步**）；step 0 空窗口**显式早退**为 undeterminable，不判幻觉 |
| `evaluation/scorers/d9_scorer.py` | `_render_user` 逐条给「该步可见观察」（`obs[0..i-1]`），step 0 渲染「（无前置观察，只有任务与动作）」；**few-shot 六例整体移除**，零样本出分并在 reason 标注 `fewshot=none` |
| `assembler/schema.py` | `Sample.reflection` 新键；`gate_of` 改用 `rationale.step_index`（**不是**列表下标——`align_rationales` 产出是稀疏序列，按下标取会让窗口错一格） |
| `assembler/views.py` | `_closing_text` 结论改读 `reflection.rationale.conclusion`；无 reflection 时退回纯 disposition 话术 |
| `executor/cli.py` | 新增 `regenerate-rationales` 子命令（备份到 `output/pipeline/_backup_pre_motivation/`、原子写回、单步失败不中断、支持 `--only` / `--dry-run`） |

### 规模与验收

全量 **480 条 rationale + 316 条 reflection ≈ 796 次 LLM 调用**（需
`TRAJECTORY_LLM_*`）。门禁 **0 failed / 0 skipped**（2026-10-21 xlsx 测试
清退后；当时总条数在 3398～3439 之间浮动，因工作区并发编辑，见
[09 §当前状态](09-门禁与纪律域.md)；此前那一格 1 failed 是 `test_xlsx_backfill`
的 xlsx 环境缺失，已随工作单删除清退）；变异检查 **47/47 全红**。

### 风险与未做

- **step 0 空窗口**是全链路最容易误伤的位置，已有显式早退 + 三组测试钉住；
- D-9 空池期间分数与带示例批次**不可直接比较**，靠 `fewshot=none` 标注；
- **旧 rationale 的派生物失效**：INC40 选的 few-shot 六例、INC41 κ=0.176 口径；
- 存量 64 档**尚未重生成**（`regenerate-rationales` 已就绪，待后端配额确认后跑）。
  ⚠️ **2026-10-25 复核**：实际是 **63 档 / 458 步**（≈767 次调用），且产物侧
  已有分代护栏，见上文 §[INC46 收尾](#inc46-收尾产物侧的分代护栏2026-10-25)。

### 单档真跑（E005__e35b7455）连带发现：合成实体误判

真跑 7 单元 / 13 步 + 7 reflection = **20 次调用，13 written / 0 unavailable**。
因果链验证通过——site-4 step0 从「当前页是…越剧特别版…虽列出 8 期链接」
变成「需要打开一个候选链接进行验证…**预期**进入后能判断…」，
但闸门**误判**了其中一条：

- site-4 step1 写「《中国好声音——越剧特别版/越剧特别季》」；
- `obs[0]` 的 `page_title` 与 `body_text` 里这两处**各自都真实存在**，
  模型只是**合并**成一个斜杠串；
- quoted 实体是**整串子串匹配** → 合成串必然落空 → 判 hard fail。

**旧口径下这条被掩盖**：本步观察 `obs[1]` 为空，实体核查直接早退。
新口径下历史观察完整，核查真跑起来才暴露。

修法（`lexicon.split_composite` + `gates._in_haystack`）：整串落空时按
**并列分隔符**（`/ ／ | ｜ 、`）拆开，**每段都命中才算有据**。刻意**不收
逗号**——「发生了错误，请稍后再试」是一句话不是两个并列实体，收了它
短片段极易意外命中、核查被放空。

收紧的方向没变：**少一段命中就仍是幻觉**，「看到 A 就顺手编个 B」照样判死。
已加 3 组测试 + 2 条变异（`all`→`any`、逗号进分隔符表）钉住。

### 实体核查的第二个假阳性形态：全角/半角与空白（2026-10-26 实测）

**现场**：优酷《功夫》页两条重生成后的样本（`T012__8c050356__site-7` /
`T018__ad4a6065__site-7`）被判 `unsupported_entity`。逐条读档：

- 观察 `page_title`：`功夫(周星驰: 电影全集)国语版-电影-高清完整正版视频在线观看-优酷`
- rationale 写的：`功夫（周星驰：电影全集）国语版`
- 差在**全角括号、全角冒号、冒号后有没有空格**——是**同一个实体**

旧代码按字面子串比 ⇒ 必然落空 ⇒ 把**如实描述页面**的 rationale 记成幻觉。
这比分不清更坏：它污染负样本池，而报告上只表现为「闸门拦下两条」。

**修法**（`lexicon.match_key` + `gates._in_haystack` 的 `haystack_key`）：
匹配键 = **NFKC 归一 + 去全部空白 + 小写**，两侧都归一后再比。
**只抹排版、不抹内容**——不拆词、不同义、不做模糊匹配；
实体比页面上多一个字（如「…国语版」→「…国语版 蓝光加长版」）**照样判幻觉**，
反面用例已钉（`tests/unit/test_rationale_gates.py::Test排版归一化`）。

⚠️ **改这一条要动的是四个比较点，不是两处**——少改一处就有一处仍然
按字面比，症状是「有的实体放行了、有的还是判幻觉」：

| 比较点 | 位置 | 作用 |
|---|---|---|
| ① 主循环的字面比 | `rationale/gates.py:297` | 先按字面比（快路径） |
| ② 主循环的排版归一比 | `rationale/gates.py:299-301` | 字面落空后的**第二次**机会——**这条是本修法的主体** |
| ③ `shaky_later` 分支 | `rationale/gates.py:308-310` | 「后续步**不完整**观察里出现过」⇒ 记不可判定、不判幻觉 |
| ④ `check_temporal_at` 的两处 | `rationale/gates.py:405-416`（`visible_key` / `later_key`） | 后见之明核查：当时看得见 / 前后都没有 |

⚠️ ③④ 漏改的后果比①②更隐蔽：②漏了会**多判幻觉**（看得见的报幻觉），
③④漏了会**把「当时就在页面里」的实体判成后见之明**（或反过来）。
⇒ 这四处**同源**（都吃 `lexicon.match_key`），改一处要确认另外三处也吃到了。

⚠️ 这一条与上面的「合成实体」是**同一类错误的两个形态**：实体匹配对
**排版**敏感。下一个形态大概率还在这个方向——写新的匹配逻辑时，
先问「模型会怎么写同一个实体，页面会怎么存它」。

### INC46 收尾：产物侧的分代护栏（2026-10-25）

**问题**：代码换了口径，**盘上的旧文本不会自己变**。实测（2026-10-25）：

| 读数 | 值 |
|---|---|
| P1 存档 | 78 档 = **15 档新口径 / 63 档旧口径** |
| 旧口径规模 | **309 个决策单元 / 458 条已写成 rationale**（≈**767 次 LLM 调用**才能重生成） |
| P2 现有样本 | 833 条 rationale 里 **444 条是事后反思**，分布在 **286/522** 条样本上 |
| 闸门读数 | 8 条 `failed` **全部**出自旧口径样本，成因是 step 0 的**假 `temporal_leak`** |

最后一行是最坏的形态：**旧文本被新窗口核出的「失败」是尺子错配的产物**。
按它去修数据等于按错的尺子改数据；反过来，旧文本也可能因窗口恰好没覆盖
它引用的实体而**假通过**。两头都在污染读数。

**四件护栏**（全部落代码，2026-10-25）：

1. **分代标记 `rationale_era`**（`assembler/schema.py::rationale_era_of`）：
   `pre_action` / `post_hoc` / `""`，随 P2 落盘、透传进 P3 的 `to_meta`。
   判据是**结构**：unit 级 `reflection` 键在不在（INC46 两条写路径都必写
   该键，之前的存档从不写）。**不读内容猜**——两代文本同构，正文里没有时代印记。
2. **时代感知闸门**：`post_hoc` 的样本一律 `not_run`（**不判通过、也不判失败**），
   `reasons` 写明「要出分先跑 `regenerate-rationales`」。`not_run` 的第三值
   本来就是为「没测过」留的，这是它的第二个用法。
3. **「重生成写的档」= 「build 读的档」**：`cmd_build` 改走
   `select_archives`（人工复核档优先），与 report / check-archives /
   evaluation 同一条口径。此前 builder 自己 glob 原始档，于是**跑完重生成，
   P2 上一个字节都不变**——两条命令都绿。
4. **构建期口径普查**：默认**两代都产出并逐条标记**（旧样本仍是真实证据），
   但打印 `pre_action N / post_hoc M` 并在混批时报警；`build --rationale-era
   pre_action` 只收一代（被筛的逐条列进 skipped，**不静默消失**）。

**`regenerate-rationales --only-old`**：整批跑时该加。旧口径单元整段重写
+ 补 reflection；已跨代但仍有步骤没写成的单元**只补那几步**（已有的 `written`
一个不动、总结不重掷）；已完整的单元一个字节都不动、不花调用。没有它时
每个单元都整段重写——**一轮后端抖动就能把上一轮的好数据换成 `unavailable`**。

---

## ⚠️ 本域已知未闭环项

| 项 | 现状 |
|---|---|
| **3 对 degraded 观察的解锁只能靠采集层** | body 采集降级根因，与「感知层主瓶颈」议题同源 |
| **存量 rationale 未重生成** | **63 档 / 309 单元 / 458 步**仍是事后反思口径；`regenerate-rationales --only-old` 已就绪（≈**767 次 LLM 调用**）。这期间的样本**不丢**：逐条标 `rationale_era=post_hoc`、闸门一律 `not_run`，`build --rationale-era pre_action` 可出同口径批次 |
| **judge 输出非确定性** | INC42 录到 E014 step0 翻转 `anchored`↔`mild_hallucination`；如需消除，judge 调用加 `temperature=0` 或多数投票，属 `llm/client` 层**独立批** |
| **mojibake 防护已被真实站点触发（2026-10-25 已闭）** | `tv.sohu.com` 真采：坏在**浏览器端**（字节本身混合损坏）⇒ 采集层修不了；防护**保留**（「坏正文 ⇒ 假负样本」的最后一道闸）。**残留开口**：2026-10-10 那次 HTTP 直取字节是好的，与本次对不上、未区分 ⇒ 对外告站点前**先重取字节** |
| **降级正文可能只是部分加载的导航栏** | INC43 明确**降级观察不做付费墙救济**——这是本条纪律的延伸 |

---

## 相关章节

- 编年史：[`90-增量编年史.md`](90-增量编年史.md) §增量四十二（观察充分性两档）· §增量四十三（降级观察不救济）· **§增量四十六（动作前动机口径）**
- rubric：[`../设计方案/在线视频场景rubric.md`](../设计方案/在线视频场景rubric.md) D-9 边界规则


### D-9 侧连带修掉的两处（真跑探针撞出来的）

**（a）``_rationales`` / ``_actions`` 只认 ``Mapping`` → Sample 对象静默变空。**
``score_d9`` 签名写 ``Sample | Any``，但 ``Sample.rationales`` 是 ``Rationale``
dataclass 元组、``Sample.actions`` 是 ``ActionView`` 元组，都不是 Mapping，
于是被**静默过滤成空**：不抛异常、不出 ``undetermined``，judge 在没有原文
的情况下照常出分。实测探针出 ``vague``，evidence 写着「未提供 rationale
原文」——那句话是 judge 在如实说它没东西可判。
生产路径（``evaluation/cli.py`` 喂 P2 dict）绕开了这个坑，所以此前没暴露。
修法：新增 ``_as_mappings``，dataclass 走它的 ``to_json()``。

**（b）``_render_observation`` 不渲染 ``page_title`` → 可见性口径与闸门不一致。**
闸门 haystack（``lexicon.resettable_text``）是 url / title / body / 元素 label /
链接**五项**全并，judge 侧只有 url + body 两项。同一个「当时能看到什么」
的问题两边拿着两个答案：judge 会把**标题里明写**的东西判成「当时看不到」，
系统性高估后见之明，把好 rationale 判进负样本池。实测那一条：judge 说
step0 里没有「…第一期：」，而它就在 ``obs[0]['page_title']`` 里。
修法：渲染补 title（空则不渲染该字段）。

修完重跑同一条探针：判据从「未提供 rationale 原文 → vague/4」变成
**引用具体文本的 hindsight/3**——judge 抓到 step2 把本步动作的 ``target``
label 说成「正文里出现」，而纯代码闸门漏了它（没加引号，lexicon 抽不出）。
这正是 gate + judge 分层的价值。
