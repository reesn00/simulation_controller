# Label Studio 开发手册：怎么改、怎么验、怎么回滚

> 这份文档讲**方法**：改 label_config 时该怎么判断对错、该怎么验证、该在什么时候
> 停手。
>
> 不讲「这个设计为什么长这样」——那是 [`设计方案/label-studio-integration.md`](设计方案/label-studio-integration.md)
> 的事；不讲「标注页怎么用」——那是 [`observability-label-studio.md`](observability-label-studio.md)；
> 不讲「导出怎么读」——那是 [`label-studio-annotation-export.md`](label-studio-annotation-export.md)。
>
> **全部内容来自 2026-09-29/30 在本机 Label Studio 1.23.0 上实测**，不是抄文档。
> 网上（含 ctx7 拉到的官方文档）与本机不符的地方都标出来了。

---

## 1. 三种信号，可信度递增

这是整份手册最重要的一条。改 label_config 时你会拿到三种「通过了」的反馈，
它们的价值差着数量级：

| 信号 | 怎么拿到 | 可信度 | 实证 |
|---|---|---|---|
| `validate/` 返回 200 | `POST /api/projects/{id}/validate/` | **0** | `<List>` 什么都不渲染、`perItem` 只出 1 组、`alias` 静默改掉契约键——**全部报绿** |
| `parsed_label_config` | `create_project` / `update_project` 返回的 dict | **能排除，不能验收** | 一眼看出 `alias` 把 `labels` 从 `KEY_C` 换成了 `显示C`；但**答不了** `html=` 渲不渲染 |
| **标注页实际渲染** | 让人打开看 | **1（唯一证据）** | 本手册所有结论的最终来源 |

**为什么 `validate/` 这么不可信**：它校验的是「这份 XML 能不能配得进去」，浏览器
跑的是**另一套标签解析器**。两者不是一回事，`validate/` 对未注册标签和「能配但
渲染不出来」的标签一视同仁地报绿。

**`parsed_label_config` 的正确用法**：它是服务端的解析模型，所以

- ✅ 拿它**排除**明显错误的写法（`alias` 那类会改变数据契约的）
- ❌ **不要**拿它**验收**任何跟「显示」有关的东西

> 教训：2026-09-30 改 `<Choice html="中文">` 时，我靠 `parsed_label_config` 确认了
> 「标识仍是英文键」，逻辑上很完整——但 `html=` 到底渲不渲染完全是另一回事，
> 最后还是得让人看一眼页面。**服务端说得通 ≠ 页面对。**

---

## 2. 属性名会骗人

Label Studio 的属性名基本不能按字面猜语义。这是本项目被同一个模式骗了**五次**。

| 属性 | 按字面以为 | 本机实测 |
|---|---|---|
| `editable="false"` | 只读 | **不是**。只管「加完之后**再**显示一个编辑图标」；**能不能提交看 Add 按钮**（`rows > 1` 时默认可见） |
| `alias` | 显示别名 | **不是**。它**顶掉 `value` 成为提交上去的值**，等于静默改掉数据契约 |
| `html` | —— | ✅ 唯一纯显示的属性：`labels_attrs` 里带 `html`，`labels` 和导出仍是 `value` |
| `transcription` | 转写开关 | 是配 `perRegion` 用的「转写某个 region / 媒体」语义标记。**没有 region 就是空转** |
| `perItem` / `perRegion` | 渲染 N 份控件 | `perRegion="true"` = 「**当前选中的那个 region** 适用此控件」，前提是锚点先产生 N 个 region |
| `inner_id` | 自定义主键 | **整数**字段；批量 import 静默丢弃，重复导入照样新建（LS 1.23 无原生去重） |
| `List` | 官方推荐的逐项渲染 | **本机什么都不渲染**——不报 not-registered 也不出内容，比报错更隐蔽 |
| `visible` / `style` / `className`（写在 `<Text>` 上） | 隐藏 / 改样式 | **全部被静默忽略**。`visible="false"` 写了一个月，页面上两个 `T001` 原样露着。服务端 `parsed_label_config` 里 `<Text>` 只有 `type` / `valueType` / `value` 三个字段——**属性根本不存在**。见 §3.5 |

> **`visible` 这条的教训比"属性名骗人"更重一档**：上面多数是「名字对、语义不对」，
> 这条是**属性压根不存在**。区别在于——名字对但语义不对的，你看到效果不对会怀疑；
> 属性不存在的，连一个可供怀疑的信号都没有，页面上就是原样显示，一眼看过去
> 「好像也没什么问题」。**一个从没生效过的写法，可以靠读代码自我感觉良好地传很久**
> （`trajectory_review.xml` 顶部那句「`visible=false` 不占版面」就是这么来的）。
> 改完配置记得问一句：*这个属性我是什么时候、拿什么证据确认它生效的？*

**通用做法**：拿不准就去翻官方 tag 文档的参数说明（ctx7），但**翻完仍要实测**——
本机行为和文档不一致是常态，`inner_id`、`perItem`、`<List>` 三处都是。

---

## 3. 改 label_config 的标准流程

### 3.1 建一次性探针项目

```python
r = client.create_project(title="zz-probe-<主题>", label_config=PROBE_XML)
pid = r["id"]          # ⚠️ create_project 返回整个 dict，不是 id
client.import_tasks(pid, [{"data": {...}}])
```

标题统一 `zz-probe-` 前缀，结束就删（§5.3）。

### 3.2 三条设计规则

**① 一次浏览器交互，回答尽可能多的问题。**
让用户点一次、截一张图的成本远高于你自己多写十行探针。做法是把所有候选写法
并排放进同一个项目，每个前面挂一个 `<Header>` 写清楚「这块在验什么」。

**② 必须有对照组。**
没有对照，你分不清「改动生效了」和「它本来就长这样」。2026-09-30 的 `perItem`
调查如果只有实验组，会得出「`perItem` 好像有点用」这种废话结论。

**③ 机器能验的别拿去问人。**
用户的时间应该花在只有浏览器能回答的问题上。例：`alias` 会不会改掉导出键，
**用 API 写一条 annotation 读回来就知道**，不用让用户点。

```python
# 导出侧的机器验证：写进去，读回来
# ⚠️ client._headers() 是私有方法, 只适合一次性诊断脚本, 别抄进生产代码
httpx.post(f"{base}/api/tasks/{tid}/annotations/", headers=client._headers(),
           json={"result": [{"from_name": n, "to_name": "t", "type": "choices",
                             "value": {"choices": [k]}} for n, k in pairs]})
```

**④ 用 `<View className>` + `<Style>` 限定作用范围**，这样能在**同一个项目**里
做「套了样式」和「没套样式」的对照：

```xml
<Style>
  .keepfmt * { white-space: pre-wrap; }          /* 通配，先确认 Style 有没有生效 */
  .keephtm .htx-text { white-space: pre-wrap; }  /* 精确，顺带验类名对不对 */
</Style>

<Text name="tA" value="$block"/>                 <!-- 对照组，不在任何容器里 -->
<View className="keepfmt"><Text name="tB" value="$block"/></View>
<View className="keephtm"><Text name="tC" value="$block"/></View>
```

### 3.3 问人要什么

一次说清，别来回：

- **给 URL**（`http://127.0.0.1:8099/projects/<id>/data?tab=labeling&task=<n>`）
- **给观察点**，逐块说明在验什么
- **给判读表**：每种可能的结果各代表什么结论，让对方只需回报「A 和 B 哪个成立」

> 2026-09-30 的失误：第一次只截到局部，看不到 A/B 两块，白跑一轮。
> 截图前先说「请从上到下滚一遍，或分别截关键几块」。

### 3.4 同步回生产

探针结论成立后，**不要直接把探针配置搬过去**——把结论翻译成生产配置，
更新测试守护（`tests/label_studio/test_label_config_xml.py`），
再 `PATCH /api/projects/{id}`（见 §5.2 的停手条件）。

### 3.5 案例：怎么把两个隐藏锚点藏起来（2026-09-30）

起因是标注页顶部露出两行 `T001`。一轮探针（project 47/49）测完，结论：

| 写法 | 标注页实测 |
|---|---|
| `<Text visible="false">` | ❌ 照旧显示（服务端 `parsed_label_config` 里没这个属性） |
| `<Text style="display:none">` | ❌ 不渲染 |
| `<Text className="...">` | ❌ 不渲染 |
| `<Style>` 里 `.htx-text { display: none }` | ❌ **一行都没藏住**，类名对不上 |
| `<Style>` 里 `.htx-text[name='tX'] { outline: … }` | ❌ 无红框 → DOM 里没有 `name` 属性 |
| `<View className="x">` + `<Style>` 规则 | ✅ 消失 |
| `<View style="display:none">` | ✅ 消失 |

**有效的只有容器上的属性。** 生产取 `<View style="display:none">`：不引
`<Style>`、不依赖任何全局类名，作用域正好是那两个锚点。

> ⚠️ 这轮**推翻了本项目自己的一条结论**：`trajectory_review.xml` 曾写「探针 37
> 实测 `.htx-text` 类名是对的」。那次探针没有对照组——缩进本来可能就是默认值，
> 于是「CSS 好像生效了」和「CSS 根本没生效」被混为一谈。**探针没有对照组，
> 结论就是猜的**（§3.2②）。

**连带一个机器就能验的坑**：LS 的 `Label config contains non-unique names`
是对**整份 XML 文本**的朴素扫描，连 `<Style>` 的 CSS 正文一起扫。纯 CSS 里的
`.htx-text[name="tX"]` 撞上后面一个 `name="tX"` 的标签 → 建项目直接 400。
要按名字选元素就写单引号 `[name='tX']`（那条校验扫的是 `name="` 这个字面量，
不是真的 AST）。已由 `test_style_block_cannot_duplicate_a_tag_name` 守住。

**验收还差一步机器验**：`display:none` 会不会掐断 `<Choices toName="task">` 的
region source？会的话就是 2026-09-29 那个「点一下整页崩成空白」的重演。所以
全保真探针（生产配置原样 + 真实样本）里让人点一次提交，再用 API 把 annotation
读回来核对 `from_name` / `to_name` / 值——**12 条 result 全部对上才算过**。


---

## 4. 机器可验的部分，自己验掉

这些不需要人看，**每次改完自己跑**：

| 验什么 | 怎么做 |
|---|---|
| 配置 XML 本身合法 | `test_label_config_xml.py`（标签白名单 + 属性规则） |
| XML 引用的 `$xxx` 在 `task.data` 里真的存在 | 同上，`test_referenced_data_keys_exist_in_exporter`（**记得先剥 XML 注释**） |
| 绑给文本标签的值真的是字符串 | 同上，`test_display_bindings_are_plain_strings` |
| 推送侧与配置侧的控件名对得上 | 同上，`test_prediction_control_matches_label_config` |
| 导出键没被改坏 | `parsed_label_config` 的 `labels` 是不是你写的 `value` |
| 全量回归 | `uv run python -m pytest -q` |

> ⚠️ **静态测试扫 XML 时先 `re.sub(r"<!--.*?-->", "", xml, flags=re.DOTALL)` 剥注释。**
> 说明段里会引用已下线的写法（例如 `<Text value="$criteria"/>`），那不是真的
> 数据绑定，不该要求 `task.data` 里有这个键。这条踩过。

---

## 5. 动生产配置前的停手条件

### 5.1 人的工作优先于配置新鲜度

```python
for t in client.list_recent_tasks(PROJECT_ID, limit=20):
    if (t.get("total_annotations") or 0) > 0:
        raise SystemExit("⛔ 上有人工标注, 不动配置")
```

**只要有非零的人工标注就停。** 配置旧一点只是界面难看，标注白做是不可逆的。

### 5.2 展示块的内容在 import 时就定死

改 `task_exporter` 不会回改**已经推上去**的 task。想让改动生效必须重推，而重推
要求先删旧 task——这时 §5.1 的检查就派上用场了。

### 5.3 重置的顺序不能反

```python
# ✅ 正确：LS 端和台账一起删
client.delete_task(...)          # 或 delete_project
# 同步删掉 output/label_studio/push_index__<pid>.jsonl 里那一行

# ❌ 错误：只删一边
#   只删 LS 不删台账 → push_batch 判重跳过 → 这条样本再也推不回来
#   只删台账不删 LS → LS 上出现重复样本（1.23 无原生去重）
```

**台账是唯一挡板**（`inner_id` 去重方案已被实测推翻，见设计文档 R7），
删它等于关掉去重。删之前先确认台账行确实存在，别凭印象。

### 5.4 顺序：先 PATCH 配置，再推数据

LS 端存的是**建项目那一刻**的 XML。`validate/` 校验的是你递过去的文本、不是项目里
存的那份——不同步就会出现「init-project 报绿、upload 却 400 `data['xxx']`」。
`init_project` 与 `resolve_project_id` 都会 `PATCH`，别绕过。

---

## 6. 文档必须跟着 UI 走

**我踩的**：文档里写「判定收成三选一：`每一条都认同` / `有哪几条不认同` /
`自动判定整体不可信`」，而页面上实际显示的是 `all_agree[1]` / `some_disagree[2]`。
文档描述的是**设计意图**，不是**界面现状**——读者按文档去找选项会找不到。

**检查方法**：文档里出现的每一处界面文案，逐条对回 `trajectory_review.xml` 的
`html=` 属性。两者不一致时**先信 UI**（那是用户真正看到的东西），
然后决定是改配置还是改文档。

相关：显示中文用 `html=`，**绝不用 `alias=`**（§2）。这个区分必须写进给下游的
导出文档——标注员点的是中文，脚本读的是英文键，两套。

---

## 7. 踩坑档案索引

具体每个坑的详细记录，不在本文档：

| 想查 | 去哪 |
|---|---|
| 某个设计决策的来龙去脉 | [`设计方案/label-studio-integration.md`](设计方案/label-studio-integration.md) §16 风险登记 R1–R18 |
| 标注页每一块填什么、后果是什么 | [`observability-label-studio.md`](observability-label-studio.md) §3.4–3.5 |
| 导出后怎么按字段取数据 | [`label-studio-annotation-export.md`](label-studio-annotation-export.md) |
| 评分卡字段级 schema | [`contracts/C4-scorecard.md`](contracts/C4-scorecard.md) |
| **还没做完的** | [`todolist.md`](todolist.md) |
| label_config 的静态守护规则 | `tests/label_studio/test_label_config_xml.py` 的模块 docstring（编号规则 1–7，全部有测试守着） |

---

## 8. 速查

- 服务端说 OK **不算数**，只有页面算数
- 拿不准的属性名 → 翻 tag 文档 → **再实测**
- 探针一次问完，**必须带对照组**，机器能验的自己验
- 有人工标注就不动配置
- 重置时 LS 端和台账**一起**删
- 文档写的中文要和 `html=` 对得上

> 最后一条元教训：本次几乎所有返工，根因都是**同一个**——
> 我用一个不能证明命题的信号（`validate/` 绿灯、服务端解析）当成了验收。
> 在这类「别人家的黑盒系统」上，**先想清楚手里那个信号能证明什么、不能证明什么**，
> 比多写十行代码更省时间。
