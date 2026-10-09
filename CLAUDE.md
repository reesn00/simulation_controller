# CLAUDE.md

## 项目概述

仓库里有**两代架构并存**：

| | 位置 | 状态 |
|---|---|---|
| **v2 轨迹合成管线** | `trajectory_pipeline/` | **当前唯一开发目标**。全部新功能、新修复写在这里 |
| **v1 存量管线** | `simulate_serve/` `orchestration/` `gdr/` `etl/` `label_studio/` `tool_runtime/` `data_refiner/` | **冻结**。只读参考：可读代码抄思路，**不可 import、不可修改** |

一句话区别：v1 是「**远端 QwenPaw agent 当驾驶员**，本地只当模拟用户 + 裁判」；
v2 把那颗心脏换掉——「**本地代码当驾驶员、obscura 当浏览器、Perceptor 当可插拔感知器**」。
v2 要采集的是**可归因的证据**，而 v1 的观察经远端 LLM 转述后锚会被削弱。

---

## ⚠️ 开发纪律（先读这一节，违反即视为架构回退）

1. **新代码一律写入 `trajectory_pipeline/`。** 任何新模块、新 CLI、新测试、新文档都落在新树内。
2. **存量仅供功能参考——禁止 import、禁止修改。** 需要存量某个能力时的正确姿势是**照着读一遍然后在新树重写**，而不是 `from simulate_serve.x import y`。
3. **两处例外（不是破例）**：仓库根 `pyproject.toml`（`testpaths` 必须含 `trajectory_pipeline/tests`）与 `.gitignore`（`trajectory_pipeline/output/`）。这两处是构建/门禁接线，改它们是为了让新树能被门禁覆盖、产物不入库。
4. **跨树/外部能力走进程边界**：MCP 协议 / HTTP，与 Python 导入边无关。这条让「不 import 存量」与「能复用外部能力」同时成立。
5. **产物隔离**：新树**禁止读取仓库根 `output/`**，产物全部落 `trajectory_pipeline/output/pipeline/`（决策 D8）。读了根 `output/` 就继承了存量那套隐性耦合。
6. **导入姿势唯一**：新树一律 `trajectory_pipeline.<模块>` 绝对导入，禁止顶层裸导入；`trajectory_pipeline` **刻意不进** `[tool.uv.workspace]`。理由是存量事故——gdr 成为 workspace 成员后同时以顶层风格和 `gdr.` 前缀风格被加载，同一份源码产生两套类对象，跨边界 `isinstance` 静默判 `False`，`usage_prune` 从未真正执行。

### 跨两棵树的红线

- **凭据不得提交、打包、复制到测试、文档或日志。**
- **不保存 raw CoT / Cookie / Authorization Header / 浏览器 Profile。**「思维链」按加工状态分两类：**raw CoT**（未经 gdr 精修的原始输出，受此红线约束）与 **refined CoT**（经 `thought_refactor` 精修的训练制品，**不受限**——CoT SFT 需要它，不要「修复」掉）。新树的落地方式是在 `executor/browser/obscura_driver.py` 里写死 `FORBIDDEN_TOOLS` + `_assert_tool_allowed` 闸门，所有 tool 调用统一走 `_call`，这是唯一入口。
- **fail-closed（I4）**：能力不可用时返回 `answer=None`，**绝不猜**；且 **`None` 不中断采集，只有 `False` 才中断**——fail-closed 管的是「结论」不是「采集」。
- **禁止投票/集成**（生产链上），分歧必须暴露给人看。

---

## 常用命令

```powershell
uv sync --group dev

# ── 新树：执行层 ────────────────────────────────────────
uv run python -m trajectory_pipeline.executor.cli check              # 探 obscura 能力（唯一需真实浏览器的自检）
uv run python -m trajectory_pipeline.executor.cli run --task-id T001 --title 功夫
uv run python -m trajectory_pipeline.executor.cli run --plan <plan.json> --limit 20 --dry-run

# ── 新树：任务生成层（不联网）──────────────────────────
uv run python -m trajectory_pipeline.executor.cli gen -n 20 --seed 7 --out <plan.json>
uv run python -m trajectory_pipeline.executor.cli check-persona      # 画像覆盖度 + 判分保真探针，分开报

# ── 新树：人工复核与报表 ───────────────────────────────
uv run python -m trajectory_pipeline.executor.cli review --write <queue.jsonl> --show 3
uv run python -m trajectory_pipeline.executor.cli review --verdicts <queue.jsonl> --apply
uv run python -m trajectory_pipeline.executor.cli report --out trajectory_pipeline/output/pipeline   # 分支分布 + 成功站点域名分布（D7 验收）

# ── 门禁 ───────────────────────────────────────────────
uv run python -m pytest -q
```

`run` / `check` 需要 `OBSCURA_EXE` 指向 obscura 可执行文件，**缺失即报错，不猜路径**：

```powershell
setx OBSCURA_EXE "C:\Users\klpc\workspace\tool\obscura-x86_64-windows-stealth\obscura.exe"
```

> 门禁 `testpaths = ["tests", "gdr/tests", "trajectory_pipeline/tests"]`，**三棵树都跑**。
> 漏掉任何一棵树 = 静默失效不可见（存量 gdr 那次漏掉 360+ 条，掩盖了 2 处生产代码从未执行）。
> 改新树代码时**别只跑 `trajectory_pipeline/tests`**。

---

# 新架构（`trajectory_pipeline/`）

## 模块地图

```text
trajectory_pipeline/
├─ taskgen/       模块 1  任务生成 + persona 画像库
├─ executor/      模块 2  代码执行循环（控制流在这里，不在任何 LLM 里）
├─ perception/    模块 3  语义感知（可替换插件）
├─ rationale/     模块 4  rationale 边写边生成 + 一致性闸门三查      [待建]
├─ assembler/     模块 5  组装与切分                                [待建]
├─ evaluation/    模块 6  评估体系（黄金集 + 三层瀑布 + LS 双向）    [待建]
├─ common/        跨模块地基（无业务逻辑）
├─ llm/           LLM 客户端（全树唯一出口）
├─ storage/       P1/P2/P3 契约读写                                [待建]
├─ docs/          设计方案 00/01/02 + contracts/
└─ output/pipeline/   全部产物（D8，gitignored）
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

两条不可破的依赖约束：
1. `executor` 只依赖 `perception` 的**协议**（`perception/base.py`），不依赖任何实现——这是 W1 规则版与 W3 LLM 版能无痛替换的唯一保证。
2. `perception` **不得 import `executor`**。感知层的输入只有 `Observation` 值对象，不持页面句柄、不回调控制流。

## 三层分工铁律

**代码管事实层 / LLM 管语言层 / persona 管输入分布。**

> **LLM 是传感器不是驾驶员**：决策权在控制流（代码），感知内容由 `Perceptor.decide` 给。
> 遍历谁、按什么顺序、点哪个、记什么、何时终止、失败样本是否入池——**全部是代码，LLM 无否决权**。

## 控制流与四个判断点

`executor/orchestrator.py` 里有 `if`，但**没有一句在判断业务语义**——「这是不是播放站 / 有没有播放控件 / 是不是播放页」全在 `Perceptor` 里，控制流只负责按答案分流：

```text
环节 0    构造查询 + 检索          → 搜索页观察
环节 0.5  反爬拦截判定            ← 必须在取候选【之前】短路
环节 ①    取候选（代码层启发式）  → ▸ 判断点 ① SELECT_PLAY_SITES
          for 每个候选:
            可达性预检（代码）    → ▸ 判断点 ② IS_REACHABLE
            找播放控件            → ▸ 判断点 ③ FIND_PLAY_CONTROL（拿 ref 去点）
            进播放页 + 媒体探测    → ▸ 判断点 ④ PLAYER_OK
```

| # | 判断题 | 输入 | 输出 | 代码拿它做什么 |
|---|---|---|---|---|
| ① | `SELECT_PLAY_SITES` | 搜索结果观察 + 目标片名 | 链接列表 + 选中/排除理由 | 决定遍历哪些站 |
| ② | `IS_REACHABLE` | 站点页观察 | 是否正常访问 | 跳过登录墙 / 地区限制 / 错误页 |
| ③ | `FIND_PLAY_CONTROL` | 站点页观察 | `{has_control, ref, trailer_only}` | **拿 ref 去点** |
| ④ | `PLAYER_OK` | 播放页观察 + **代码测的 media_count** | 是否正常播放、有无组件 | 成功 or 标记未验证 |

W1 的诚实说明：`RulePerceptor` 对 ① `SELECT_PLAY_SITES` / ② `IS_REACHABLE` **只能返回 `None`**（搜索链接筛选与「是否登录墙」都不是规则能判的），所以 W1 批次这两环全是 `unresolved`。这不是缺陷，是 fail-closed 的正常表现；W3 接上 `LLMPerceptor` 后同一份代码不需要改动一行。

### 失败分支全集（`executor/branches.py`）

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

**每条分支都必须有对应样本入库**——负样本不是副产品，是强制产出。反过来，`unresolved` / `trailer_suspect` **不是负样本**：把它们混进负样本池会污染它，「这里真的看不了」这条训练信号就此失真。

**反爬拦截刻意不进这套分支体系**：拦截发生在**取候选之前**，根本没有 outcome 可记。它单开运行级字段 `RunRecord.search_blocked`——代价是 `report` 要多统计一次，换来的是「被拦」与「没素材」在报表上永远分得开。

## 数据契约（P1/P2/P3，与存量 C1/C2/C3 目录、命名、schema 全不重叠）

| 契约 | 落点 | 内容 | 产出方 |
|---|---|---|---|
| **P1 观察存档** | `output/pipeline/<task_id>__<hash>.json` | 动作 + 工具参数 + **观察原文全文**（真实回放，非 LLM 转述） | `executor/archive.py` |
| **P2 单条样本** | `output/pipeline/samples/<id>.json` | 六件套 + provenance + gate 标记 | `assembler/builder.py` [待建] |
| **P3 训练视图** | `output/pipeline/views/<id>_{messages,openai,meta}.json` | 训练框架可读形态 | `assembler/views.py` [待建] |
| 负样本池 | `output/pipeline/negative.jsonl` | 失败分支样本（带 `branch`） | `executor/branches.py` |
| 复核队列 | `output/pipeline/review_*.jsonl` | 人工裁定（`verdict` + `reviewed_by`） | `executor/review_queue.py` |

字段级契约文档在 `trajectory_pipeline/docs/contracts/`（**目录已建，P1/P2/P3 三份待写**）。P1 的实际形状以 `RunRecord.to_json()` 为准，路径形状以 `archive.DEFAULT_ROOT` 为准（**平铺单文件，不是 `observations/<run_id>/` 目录**——那是设计方案早期为动作流分片预留的形状，实现从未采用，文档已对齐）。

**正文 `body_text` 落盘全文，不落摘要。** 早期只留前 400 字符，理由「体积失控、需要时重抓」——站不住：站点会下线改版（重抓拿到的是另一个页面）、被反爬时根本重抓不回来、rationale 的实体核查会把落在摘要外的实体判成幻觉（那不是幻觉，是**没存**）。P1 是批次唯一真值源，证据链断裂不可逆，体积只是磁盘（D8 已隔离产物路径）。

**观察是真实回放，不是 LLM 转述**——这条一旦破，rationale 就没有锚。

## 执行层：obscura（决策 D7）

Rust 无头浏览器引擎，内置反检测，**经标准 MCP 协议 stdio 直调**（`obscura mcp --stealth`，协议 2024-11-05，37 个 tool，会话式）。放弃 Playwright MCP + Camoufox。

`executor/browser/` 分三层：

| 文件 | 角色 |
|---|---|
| `page_driver.py` | **协议，全树唯一稳定契约**。只暴露原子原语（`goto`/`snapshot`/`click`/`type`/`evaluate`/`interactive_elements`/`links`/`media_probe`），**不封装任何业务语义** |
| `mcp_client.py` | 传输层。JSON-RPC 进、文本出，**不认识任何 tool 名** |
| `obscura_driver.py` | obscura 接入（`PageDriver` 实现）+ `FORBIDDEN_TOOLS` 闸门 |

`mcp_probe.py` / `probe_returns.py` / `probe_observe.py` 是**可重跑探针**，取 tool 清单、返回格式取证、端到端 `observe` 契约取证——上游版本漂移时先重跑它们。

## 感知层（模块 3，可替换插件）

`perception/base.py` 定协议，`questions.py` 定判断题注册表，`rule_perceptor.py` 是 W1 实现，`factory.py` 做注入与灰度（**待建**）。

输入是 DOM 预处理后的结构化观察（`Observation`），不是原始 HTML；输出必须是 schema。**七不变式由契约测试强制**（`tests/contract/test_perception.py`）：

| | 不变式 |
|---|---|
| I1 | evidence 可溯源到 `Observation` |
| I2 | `confidence ∈ [0,1]` |
| I3 | 幂等——同一 `(question, obs)` 重复调用返回相同答案 |
| I4 | fail-closed——能力不可用返 `None`，**绝不猜** |
| I5 | 无副作用——只读 `obs`，不触网、不持页面句柄 |
| I6 | decision 型题 payload 完整，`ref` 可溯源到 `obs.interactive_elements` |
| I7 | `answer=True` 时 payload 不得为空 |

## persona 六维（模块 1）

`taskgen/persona/schema.py` 的 `PersonaProfile`：六维度 + 2 标记。每条都对应一个**可观察的语言特征**：

| 维度 | 影响的语言现象 |
|---|---|
| `genre` | 领域词汇 |
| `popularity` | 是否用站点名/别名/译名 |
| `urgency` | 催促语、首轮长度 |
| `verbal_style` | 句长、语气词、网络用语、错别字 |
| `persona_presence` | 身份线索句 |
| `task_specificity` | 指名 vs 指代 |
| + `has_standard` | 70% True / 30% False（防「等标准才行动」） |
| + `content_tier` | 切片用来源档位，**不得由 LLM 自评**，必须来自骨架客观属性 |

**铁律：persona 与改写只改表述，不改判分标准。** 归一失败即丢弃该改写，退回骨架原文。

存量 98 个 task **不是同一种任务**：能提片名的 80 个（`single_title`）、集合型 16 个（`aggregate`）、片名未知型 2 个（`unknown_title`）。W1 只跑 `single_title`——**判分标准与判据不匹配比跑不了更糟**。（T041「那个周星驰的片子」原本被 `extract_title` 提成片名、归进 `single_title`，检索式变成 `那个周星驰的片子 在线观看`——哪个引擎都搜不到，且失败形态与「站上没有播放控件」不可区分。已修：指代前缀**与**类别词同时命中才算指代短语，否则《那个杀手不太冷》这类真片名会被误杀。）

## 现状（2026-10-09）

**已跑通**：执行层端到端（真实 bing/baidu 站点）、模块 1 采样与 `run --plan` 接线、反爬拦截识别与单独记账、候选域名过滤、persona 渲染、复核队列与 `review --apply`、**成功站点域名分布报告（D7 验收）**。

**待办**：

- **规则版 `PLAYER_OK` 对真实视频站的正样本召回仍是 0**——只认 `<video>`/`<audio>`，真实视频站全是 iframe/JS 播放器，判断点 ④ 对它们一律 `None` → `unresolved`。`report` 的实测数据：20 站 / 成功 3 / **3 条全是 `fallback_used` 存在性判定**（其中一条是导航首页 hao123 的假阳性，该批存档早于「iframe 不作判据」的修正）。**W1 正样本靠人工复核**（`review --write` → 人工填 `verdict` → `--apply`，报表会按 `source` 把人工确认的成功与规则判定分开列）。这是 W3 `LLMPerceptor` 的直接输入，不是 W1 缺口。
- `trailer_only` 词表覆盖率与误杀边界（真实站点上只跑到 `trailer_suspect`，`trailer_only` 一次都没触发）。
- `rationale/` / `assembler/` / `evaluation/` / `storage/` 四个模块（包骨架已建，业务代码待写）。
- `docs/contracts/` 下 P1/P2/P3 三份字段级契约。
- `perception/llm_perceptor.py` + `factory.py`（W3）。

**每次真实运行后都要逐条打开存档读 evidence 核对**——这是固定动作不是可选项。设计上五类缺陷全部是离线测试测不出来的（fixture 是我们自己造的，规则和 fixture 一起错，测试照样绿），只有把真实站点喂进去、逐条读存档，才会看见「结论」和「证据」在打架。踩坑全记录在 [`trajectory_pipeline/docs/02-避坑指南.md`](trajectory_pipeline/docs/02-避坑指南.md)。

---

# 存量架构（`simulate_serve` / `orchestration` / `gdr` / `etl` / `label_studio`，冻结只读）

`simulation server → gdr → etl` 三阶段，每段一道边界，交接面写契约（C1/C2/C3/C4）。产物落根 `output/`。

| 目录 | 职责 |
|---|---|
| `simulate_serve/` | v1 模拟采集端。`configuration/` 严格 Schema v2 Catalog；`domain/`+`application/` 编译任务与异步状态机；`interaction/` 生成首轮请求与追问（不拥有验证工具）；`validation/`+`tools/` 确定性规则 + 语义 Judge + 工具取证；`infrastructure/` QwenPaw HTTP + JSON v2 持久化。入口 `python -m simulate_serve` |
| `orchestration/` | 顶层调度。`master` → `PipelineExecutor`（`multiprocessing.Pool` 槽位填充）→ `task_pipeline` 单 task 三阶段串行；`queue/sqlite_queue.py` 状态机 `pending → simulate → gdr → etl → done`，超限入 `dead`；`workers/{gdr,etl}_worker.py` 分别调 gdr / etl 公开入口。入口 `python -m orchestration` |
| `gdr/` | C1→C2 精修。Session/Message/Block 三级模型、13 种缺陷标签（规则 + LLM 三票投票）、obs_denoiser/thought_refactor/tool_fixer、L1/L2/L3 三级验证。⚠️ 双导入姿势（顶层风格 + `gdr.` 前缀）导致同源码产生两套类对象，跨边界**不要用 `isinstance` 认类型** |
| `etl/` | C2→C3 格式转换。`qwenformat/`（trajectory 重放 `load.parse_trajectory` + transform + chat_template）、`parsers/`（C2 入口 `load_refined_session`）、`writers/`（C3 入口 `render_to_4_views`）、`pawsession/`（平行旧路，**不在 orchestration 主链路**） |
| `label_studio/` | **流水线终点**，单向推送不回流。`scorecard.v1` 分层评分卡（L0–L5，每维带 `source` 与依据）；R11 凭据扫描 fail-closed 拒推；`push_index.py` 本地台账（LS 1.23 无原生去重，删台账等于每次推重复样本）。入口 `python -m label_studio` |
| `tool_runtime/` | Node 侧 Playwright MCP 依赖，打包时并入 `simulate_serve/tools/browser/`，默认禁用 |
| `data_refiner/` | 合成数据轻量规则清洗，**只标注不删除**。入口 `python -m data_refiner`（不在主链路） |
| `scripts/` | 迁移脚本 + `model_train/`（unsloth LoRA 独立训练脚本，不在主依赖里） |
| `tests/` `gdr/tests/` | 存量测试两棵树，与 `trajectory_pipeline/tests/` 一起构成全量门禁 |

存量**设计细节**（LS 标注页坑位、C3 meta 瘦身、gdr 双导入、死信/旁路判据等）留在各目录代码注释与 `docs/` 里按需查阅，**不再在本文件展开**。

---

## 输出目录

- **`trajectory_pipeline/output/pipeline/`** —— 新树全部产物（D8，gitignored）：P1 存档、复核队列、`obscura_tools.json` 等探针取证
- `output/agent_trajectory|refined|refine_data/` —— 存量 C1/C2/C3（冻结，不迁移、不双写）
- `output/orchestration/dead/` —— 存量死信；`refine_data/*.jsonl` —— 存量旁路审计通道

## 文档

**新树（先看这里）**：

- [`trajectory_pipeline/docs/设计方案/00-总体方案.md`](trajectory_pipeline/docs/设计方案/00-总体方案.md) —— v2 设计基线：决策 D1–D10、模块设计、四个判断点、失败分支、契约、里程碑、风险
- [`trajectory_pipeline/docs/设计方案/01-模块3-可替换感知层.md`](trajectory_pipeline/docs/设计方案/01-模块3-可替换感知层.md) —— 插件化机制完整规格 + 七不变式
- [`trajectory_pipeline/docs/02-避坑指南.md`](trajectory_pipeline/docs/02-避坑指南.md) —— **8 类 35 条**，症状写成终端里真正会看到的那句话，可全文搜索

**存量**（按需查阅）：`docs/orchestration-design.md`、`docs/observability-label-studio.md`、`docs/label-studio-playbook.md`、`docs/label-studio-annotation-export.md`、`docs/设计方案/`、`docs/contracts/`、`gdr/docs/`。