# trajectory_pipeline · Agent 轨迹合成管线

采集**可归因**的 Agent 浏览器操作轨迹：任务生成 → 代码执行 → 语义感知 → 样本组装 → 评估。
核心主张是**代码管事实层、LLM 管语言层、persona 管输入分布**——LLM 是传感器，不是驾驶员。

仓库里有两代架构：**v2 管线（本目录，当前唯一开发目标）** 与 **v1 存量管线（冻结，只读参考）**。

---

## 为什么换心脏

v1 的做法是：本地扮演用户，把自然语言发给远端 QwenPaw agent，由它做全部浏览器操作，本地只负责验证取证。

这条路的天花板是**观察经 LLM 转述**：代码拿到的是远端模型复述过的「我看到了什么」，不是页面本身。rationale 没有锚，归因也就无从谈起。所以 v2 显式把这条排除（见设计文档 D7 的排除表）：

> **远端 QwenPaw 当执行器** —— 它只有 `chat/task` 入口（吃自然语言、agent 自决策），是 v2 要换掉的那颗心脏；观察经 LLM 转述，rationale 的锚被削弱。

于是换一颗心脏：**本地代码当驾驶员、obscura 当浏览器、Perceptor 当可插拔感知器**。观察是真回放，不是转述。

```
v1:  本地当用户 ──自然语言──▶ 远端 QwenPaw ──▶ 浏览器        观察经 LLM 转述
v2:  代码当控制流 ──MCP──▶ obscura ──▶ 浏览器              观察是原文回放
     └─ Perceptor（可替换：规则 W1 / LLM W3）
```

---

## 快速开始

```powershell
uv sync --group dev

# obscura 可执行文件位置（不硬编码本机路径，缺失即报错不猜）
setx OBSCURA_EXE "C:\Users\klpc\workspace\tool\obscura-x86_64-windows-stealth\obscura.exe"

# 探一次执行层能力（唯一需要真实浏览器的自检）
uv run python -m trajectory_pipeline.executor.cli check

# 采样一批任务实例（不联网）→ 落执行计划
$P = trajectory_pipeline/output/pipeline
uv run python -m trajectory_pipeline.executor.cli gen -n 20 --seed 7 --out $P/plan.json

# 按计划跑，落 P1 观察存档
uv run python -m trajectory_pipeline.executor.cli run --plan $P/plan.json --limit 5 --dry-run

# 导出人工复核队列 → 填 verdict → 回填
uv run python -m trajectory_pipeline.executor.cli review --write $P/review_queue.jsonl --show 3
uv run python -m trajectory_pipeline.executor.cli review --verdicts $P/review_queue.jsonl --apply

# 汇总分支分布
uv run python -m trajectory_pipeline.executor.cli report --out trajectory_pipeline/output/pipeline

# 门禁（存量三棵树一起跑）
uv run python -m pytest -q
```

| 命令 | 联网 | 作用 |
|---|---|---|
| `check` | ✅ | 探 obscura 能力握手与 `observe` 契约 |
| `run` | ✅ | 跑一个 task / 一份计划，落 P1 |
| `gen` | ❌ | 骨架 × persona 采样，产出执行计划 JSON |
| `check-persona` | ❌ | 画像覆盖度 + 判分保真探针（**分开报，不合成一个数**） |
| `review` | ❌ | 导出 / 回填人工复核裁定 |
| `report` | ❌ | 汇总 P1 分支分布 |

---

## 新架构

### 模块地图

```text
trajectory_pipeline/
├─ taskgen/       模块 1  任务生成 + persona 画像库
├─ executor/      模块 2  代码执行循环 —— 控制流在这里，不在任何 LLM 里
├─ perception/    模块 3  语义感知 —— 可替换插件
├─ rationale/     模块 4  rationale 边写边生成 + 一致性闸门三查       [待建]
├─ assembler/     模块 5  组装与切分                                 [待建]
├─ evaluation/    模块 6  评估体系（黄金集 + 三层瀑布 + LS 双向）     [待建]
├─ common/        跨模块地基（无业务逻辑）
├─ llm/           LLM 客户端（全树唯一出口）
├─ storage/       P1/P2/P3 契约读写                                 [待建]
├─ docs/          设计方案 00/01/02 + contracts/
└─ output/pipeline/   全部产物（决策 D8，gitignored）
```

依赖方向**单向、禁止回指**：

```text
common ← llm ← {taskgen, perception, rationale}
              ↑
           executor  ──→ storage
              ↓
          assembler ──→ storage
              ↓
          evaluation ──→ storage
```

两条不可破的约束：`executor` 只依赖 `perception` 的**协议**（`perception/base.py`），不依赖任何实现——这是规则版与 LLM 版能无痛替换的唯一保证；`perception` **不得 import `executor`**，它的输入只有 `Observation` 值对象，不持页面句柄、不回调控制流。

### 一次运行走什么

`executor/orchestrator.py` 里有 `if`，但**没有一句在判断业务语义**——「这是不是播放站 / 有没有播放控件 / 是不是播放页」全在 `Perceptor` 里，控制流只负责按答案分流：

```text
环节 0    构造查询 + 检索           → 搜索页观察
环节 0.5  反爬拦截判定             ← 必须在取候选【之前】短路
环节 ①    取候选（代码层启发式）     → ▸ 判断点 ① SELECT_PLAY_SITES
          for 每个候选:
            可达性预检（代码）      → ▸ 判断点 ② IS_REACHABLE
            找播放控件             → ▸ 判断点 ③ FIND_PLAY_CONTROL（拿 ref 去点）
            进播放页 + 媒体探测     → ▸ 判断点 ④ PLAYER_OK
```

**LLM 只有四个位置**，决策权全在代码：

| # | 判断题 | 输入 | 输出 | 代码拿它做什么 |
|---|---|---|---|---|
| ① | `SELECT_PLAY_SITES` | 搜索结果观察 + 目标片名 | 链接列表 + 选中/排除理由 | 决定遍历哪些站 |
| ② | `IS_REACHABLE` | 站点页观察 | 是否正常访问 | 跳过登录墙 / 地区限制 / 错误页 |
| ③ | `FIND_PLAY_CONTROL` | 站点页观察 | `{has_control, ref, trailer_only}` | **拿 ref 去点** |
| ④ | `PLAYER_OK` | 播放页观察 + **代码测的 media_count** | 是否正常播放、有无组件 | 成功 or 标记未验证 |

遍历谁、按什么顺序、点哪个、记什么、何时终止、失败样本是否入池——**全部是代码，LLM 无否决权**。

### 失败分支：负样本是强制产出

| 判断点 | 分支 id | 语义 | 需 LLM |
|---|---|---|---|
| ① | `not_play_site` | 搜索结果非该片可观看站 | ✅ |
| ② | `unreachable_hard` | 状态码/超时/空白页 | ❌ 代码判定 |
| ② | `login_wall_or_blocked` | 登录墙 / 地区限制 / 错误页 | ✅ |
| ③ | `no_play_control` | 无剧集也无播放控件 | ✅ |
| ③ | `trailer_only` | 只有预告片，无正片资源 | ❌ 代码判定（词表） |
| ③ | `trailer_suspect` | 疑似预告但词表未覆盖 | ➖ 人工兜底（**不入负样本池**） |
| ④ | `component_unverified` | 有 video 标签但播放器未正常加载 | ✅ |
| ④ | `unresolved` | 置信度不足且重采样耗尽 | ➖ 人工复核（**不入负样本池**） |

**每条分支都必须有对应样本入库**——负样本不是副产品。反过来，`unresolved` / `trailer_suspect` 不是负样本：混进去会污染它，「这里真的看不了」这条训练信号就失真了。

**反爬拦截刻意不进这套分支体系**：它发生在取候选**之前**，根本没有 outcome 可记。它单开运行级字段 `search_blocked`——「被拦」与「没素材」在报表上永远分得开。

### 数据契约

与存量 C1/C2/C3 的目录、命名、schema **全不重叠**，并存不冲突。

| 契约 | 落点 | 内容 | 产出方 |
|---|---|---|---|
| **P1 观察存档** | `output/pipeline/<task_id>__<hash>.json` | 动作 + 工具参数 + **观察原文**（真实回放，非 LLM 转述） | `executor/archive.py` |
| **P2 单条样本** | `output/pipeline/samples/<id>.json` | 六件套 + provenance + gate 标记 | `assembler/` [待建] |
| **P3 训练视图** | `output/pipeline/views/<id>_{messages,openai,meta}.json` | 训练框架可读形态 | `assembler/` [待建] |
| 负样本池 | `output/pipeline/negative.jsonl` | 失败分支样本（带 `branch`） | `executor/branches.py` |
| 复核队列 | `output/pipeline/review_*.jsonl` | 人工裁定（`verdict` + `reviewed_by`） | `executor/review_queue.py` |

字段级契约文档在 `trajectory_pipeline/docs/contracts/`（目录已建，三份待写）；P1 实际形状以 `RunRecord.to_json()` 为准。

> **观察是真实回放，不是 LLM 转述**——这条一旦破，rationale 就没有锚。

### 执行层：obscura

决策 D7 选型：Rust 无头浏览器引擎，内置反检测，**经标准 MCP 协议 stdio 直调**（`obscura mcp --stealth`；协议 2024-11-05，37 个 tool，会话式）。放弃 Playwright MCP + Camoufox。

`executor/browser/` 分三层，`PageDriver` 是全树唯一稳定契约：

| 文件 | 角色 |
|---|---|
| `page_driver.py` | **协议**。只暴露原子原语（`goto`/`snapshot`/`click`/`type`/`evaluate`/`interactive_elements`/`links`/`media_probe`），**不封装任何业务语义** |
| `mcp_client.py` | 传输层。JSON-RPC 进、文本出，**不认识任何 tool 名** |
| `obscura_driver.py` | obscura 接入 + `FORBIDDEN_TOOLS` 闸门（cookie / storage state 类 5 个 tool 在此被硬拦） |
| `mcp_probe.py` / `probe_returns.py` / `probe_observe.py` | 可重跑探针：tool 清单、返回格式取证、端到端 `observe` 契约 |

业务语义一律在 `steps/` 里，所以驱动层没有可被 LLM 触及的决策面，也能不改控制流地换浏览器。

### 感知层：可替换插件

| | W1 `RulePerceptor`（当前） | W3 `LLMPerceptor` | 未来 `ModelPerceptor` |
|---|---|---|---|
| 实现 | URL 特征、video 标签计数、按钮文本正则 | LLM + 结构化 schema | 训练好的小分类器 |
| 置信度 | 固定 1.0 | 模型自报 | 模型概率 |
| 置信度不足 | N/A | 重采样 → 人工兜底 | 概率阈值 |

**七不变式由契约测试强制**（`tests/contract/test_perception.py`）：

| | 不变式 |
|---|---|
| I1 | evidence 可溯源到 `Observation` |
| I2 | `confidence ∈ [0,1]` |
| I3 | 幂等——同一 `(question, obs)` 重复调用返回相同答案 |
| I4 | fail-closed——能力不可用返 `None`，**绝不猜** |
| I5 | 无副作用——只读 `obs`，不触网、不持页面句柄 |
| I6 | decision 型题 payload 完整，`ref` 可溯源到 `obs.interactive_elements` |
| I7 | `answer=True` 时 payload 不得为空 |

I4 的边界值得单独记住：**`None` 不中断采集，只有 `False` 才中断**。fail-closed 管的是「结论」不是「采集」——早期版本理解成「拿不到结论就停」，结果整批 5 个站点全落 `unresolved`，负样本池一条都产不出来。

W1 的诚实说明：`RulePerceptor` 对 ① `SELECT_PLAY_SITES` / ② `IS_REACHABLE` **只能返回 `None`**（链接筛选与「是否登录墙」都不是规则能判的），所以 W1 批次这两环全是 `unresolved`。这是 fail-closed 的正常表现，不是缺陷——W3 接上 LLM 版后同一份控制流一行都不用改。

### 任务生成层：persona 六维

`PersonaProfile` 六维度 + 2 标记，每一维都对应一个**可观察的语言特征**，不是凭空设的标签：

| 维度 | 影响的语言现象 |
|---|---|
| `genre` | 领域词汇 |
| `popularity` | 是否用站点名 / 别名 / 译名 |
| `urgency` | 催促语、首轮长度 |
| `verbal_style` | 句长、语气词、网络用语、错别字 |
| `persona_presence` | 身份线索句（"我是给娃找的"） |
| `task_specificity` | 指名 vs 指代（"那个科幻的"） |
| + `has_standard` | 70% True / 30% False（防"等标准才行动"） |
| + `content_tier` | 切片用来源档位，**不得由 LLM 自评**，必须来自骨架客观属性 |

**铁律：persona 与改写只改表述，不改判分标准。** 判分维度从固定标准库枚举，`normalizer.py` 必须能把任意改写归一回去；归一失败即丢弃该改写，退回骨架原文。

存量 98 个 task **不是同一种任务**：能提片名的 81 个（`single_title`）、集合型 15 个（`aggregate`）、片名未知型 2 个（`unknown_title`）。W1 只跑 `single_title`——**判分标准与判据不匹配比跑不了更糟**。

### 人工复核

规则版对真实视频站（优酷/爱奇艺/西瓜全是 iframe / JS 播放器）判不出正样本，所以 W1 的正样本来源是人工复核：

```text
P1 存档 ──▶ review --write queue.jsonl（带 video/iframe 计数、元素样本、原始 evidence）
                     │  人工填 verdict + reviewed_by + note
                     ▼
        review --verdicts queue.jsonl --apply
                     ▼
  <name>.reviewed.json（branch=None、source=human、branch_before_review 保留代码原判）
```

队列必须带够上下文——只给一行 `unresolved` 等于把判断责任转嫁给复核员却不给材料，那种队列会退化成"自己开浏览器再看一遍"。回填**只对复核分支生效**，真负样本原样保留（一次误填就能把 `no_play_control` 改成成功，负样本池的完整性就此静默毁掉）。

---

## 当前状态（2026-10-09）

**已跑通**：执行层端到端（真实 bing / baidu 站点）、模块 1 采样与 `run --plan` 接线、反爬拦截识别与单独记账、候选域名过滤、persona 渲染、复核队列与 `review --apply`。

**待办**：成功站点域名分布报告（D7 验收，挂起等人工复核出第一批真成功样本）｜`trailer_only` 词表覆盖率与误杀边界｜`rationale/` `assembler/` `evaluation/` `storage/` 四个模块（包骨架已建）｜P1/P2/P3 字段级契约｜`LLMPerceptor` + `factory`（W3）。

> **每次真实运行后都要逐条打开存档读 evidence 核对**，这是固定动作不是可选项。设计上五类严重缺陷全部是离线测试测不出来的——fixture 是我们自己造的，规则和 fixture 一起错，测试照样绿。踩坑全记录在 [`trajectory_pipeline/docs/02-避坑指南.md`](trajectory_pipeline/docs/02-避坑指南.md)。

---

## 开发纪律

1. **新代码一律写入 `trajectory_pipeline/`。**
2. **存量仅供功能参考——禁止 import、禁止修改。** 需要某个存量能力时的正确姿势是**照着读一遍然后在新树重写**。
3. **两处例外（不是破例）**：根 `pyproject.toml`（`testpaths` 必须含 `trajectory_pipeline/tests`）与 `.gitignore`（`trajectory_pipeline/output/`）。
4. **跨树/外部能力走进程边界**：MCP 协议 / HTTP，与 Python 导入边无关。
5. **产物隔离**：新树禁止读取根 `output/`，产物全落 `trajectory_pipeline/output/pipeline/`。
6. **导入姿势唯一**：一律 `trajectory_pipeline.<模块>` 绝对导入；`trajectory_pipeline` 刻意不进 `[tool.uv.workspace]`。

**跨两棵树的红线**：凭据不得提交、打包、复制到测试、文档或日志｜不保存 raw CoT / Cookie / Authorization Header / 浏览器 Profile（refined CoT 是 CoT SFT 的必要输入，不受此限）｜生产链上禁止投票/集成，分歧必须暴露给人看。

---

## 存量架构（v1，冻结只读）

`simulation server → gdr → etl` 三阶段，每段一道边界，交接面写契约（C1 trajectory / C2 refined Session / C3 4 视图 / C4 评分卡）。产物落根 `output/`。**细节按需查阅各目录代码注释与 `docs/`，本文不再展开。**

| 目录 | 职责 |
|---|---|
| `simulate_serve/` | v1 模拟采集端。Catalog 编译 + 异步运行状态机 + 确定性验证 + 语义 Judge + 工具取证 + QwenPaw HTTP。入口 `python -m simulate_serve` |
| `orchestration/` | 顶层调度。`master` → `PipelineExecutor`（`multiprocessing.Pool`）→ 单 task 三阶段串行；SQLite 队列状态机 + 死信/旁路分流。入口 `python -m orchestration` |
| `gdr/` | C1→C2 精修。三级数据模型、13 种缺陷标签、块级精修器、L1/L2/L3 验证 |
| `etl/` | C2→C3 格式转换。`qwenformat/` trajectory 重放、`parsers/` C2 入口、`writers/` C3 入口 |
| `label_studio/` | **流水线终点**，单向推送不回流。分层评分卡 + R11 凭据扫描 fail-closed + 本地推送台账。入口 `python -m label_studio` |
| `tool_runtime/` | Node 侧 Playwright MCP 依赖，默认禁用 |
| `data_refiner/` | 合成数据轻量规则清洗，只标注不删除（不在主链路） |
| `scripts/` | 迁移脚本 + `model_train/` 独立 LoRA 训练脚本 |
| `tests/` `gdr/tests/` | 存量测试两棵树，与 `trajectory_pipeline/tests/` 一起构成全量门禁 |

---

## 文档

**新树**

- [`trajectory_pipeline/docs/设计方案/00-总体方案.md`](trajectory_pipeline/docs/设计方案/00-总体方案.md) —— 设计基线：决策 D1–D10、模块设计、四个判断点、失败分支、契约、里程碑、风险与待决
- [`trajectory_pipeline/docs/设计方案/01-模块3-可替换感知层.md`](trajectory_pipeline/docs/设计方案/01-模块3-可替换感知层.md) —— 插件化机制完整规格 + 七不变式
- [`trajectory_pipeline/docs/02-避坑指南.md`](trajectory_pipeline/docs/02-避坑指南.md) —— **8 类 35 条**，症状写成终端里真正会看到的那句话，可全文搜索

**存量**（按需查阅）：`docs/orchestration-design.md`、`docs/observability-label-studio.md`、`docs/label-studio-playbook.md`、`docs/label-studio-annotation-export.md`、`docs/设计方案/`、`docs/contracts/`、`gdr/docs/`。