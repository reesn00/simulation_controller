# CLAUDE.md

## 项目概述

仓库里有**两代架构并存**：

| | 位置 | 状态 |
|---|---|---|
| **v2 轨迹合成管线** | `trajectory_pipeline/` | **当前唯一开发目标**。全部新功能、新修复写在这里 |
| **v1 存量管线** | `archive/v1/`（原 `simulate_serve/` `orchestration/` `gdr/` `etl/` `label_studio/` `tool_runtime/` `data_refiner/`，2026-10-09 归档） | **冻结**。只读参考：可读代码抄思路，**不可 import、不可修改** |

一句话区别：v1 是「**远端 QwenPaw agent 当驾驶员**，本地只当模拟用户 + 裁判」；
v2 把那颗心脏换掉——「**本地代码当驾驶员、obscura 当浏览器、Perceptor 当可插拔感知器**」。
v2 要采集的是**可归因的证据**，而 v1 的观察经远端 LLM 转述后锚会被削弱。

---

## ⚠️ 开发纪律（先读这一节，违反即视为架构回退）

1. **新代码一律写入 `trajectory_pipeline/`。** 任何新模块、新 CLI、新测试、新文档都落在新树内。
2. **存量仅供功能参考——禁止 import、禁止修改。**（2026-10-09 起存量代码归档至 `archive/v1/`，纪律不变。）需要存量某个能力时的正确姿势是**照着读一遍然后在新树重写**，而不是 `from simulate_serve.x import y`。
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
uv run python -m trajectory_pipeline.executor.cli check              # 探 obscura 能力 + 报两个感知实现的边界（唯一需真实浏览器的自检）
uv run python -m trajectory_pipeline.executor.cli run --task-id T001 --title 功夫
uv run python -m trajectory_pipeline.executor.cli run --plan <plan.json> --limit 20 --dry-run
uv run python -m trajectory_pipeline.executor.cli run --plan <plan.json> --limit 5 --perceptor llm   # 指定实现，覆盖 TRAJECTORY_PERCEPTOR

# ── 新树：任务生成层（不联网）──────────────────────────
uv run python -m trajectory_pipeline.executor.cli gen -n 20 --seed 7 --out <plan.json>
uv run python -m trajectory_pipeline.executor.cli check-persona      # 画像覆盖度 + 判分保真探针，分开报

# ── 新树：人工复核与报表 ───────────────────────────────
uv run python -m trajectory_pipeline.executor.cli review --write <queue.jsonl> --show 3
uv run python -m trajectory_pipeline.executor.cli review --verdicts <queue.jsonl> --apply
uv run python -m trajectory_pipeline.executor.cli report --out trajectory_pipeline/output/pipeline   # 分支分布 + 成功站点域名分布（D7 验收）
uv run python -m trajectory_pipeline.executor.cli check-archives          # 存档完整性门禁（不联网，见下）
uv run python -m trajectory_pipeline.executor.cli replay --perceptor rule   # 感知层回放：真实观察上跑感知实现（不开浏览器）

# ── 新树：整批回滚（默认只列不删）───────────────────────
uv run python -m trajectory_pipeline.executor.cli purge-run --run-id <run_id>
uv run python -m trajectory_pipeline.executor.cli purge-run --run-id <run_id> --apply

# ── 门禁 ───────────────────────────────────────────────
uv run python -m pytest -q
```

> ⚠️ **`run` 的 `--title` 缺省即报错**，不拿 `--task-id` 顶替。
> 实测：少了它就搜成 `T001 在线观看`（**轮胎** T001），产出整批理由通顺的
> 假负样本且**零报错**。见避坑指南 §5.13。

`run` / `check` 需要 `OBSCURA_EXE` 指向 obscura 可执行文件，**缺失即报错，不猜路径**：

```powershell
setx OBSCURA_EXE "C:\Users\klpc\workspace\tool\obscura-x86_64-windows-stealth\obscura.exe"
```

`--perceptor llm` / `TRAJECTORY_PERCEPTOR=llm` 另外需要 LLM 后端，**同样缺失即报错不猜**：

```powershell
setx TRAJECTORY_LLM_BASE_URL "http://<host>/v1"
setx TRAJECTORY_LLM_MODEL "<model>"
setx TRAJECTORY_LLM_API_KEY "<key>"    # 本机 vLLM 不校验时可留空
```

**key 只走环境变量**：代码里没有任何凭据字面量，`LLMConfig.__repr__` 刻意不吐 key，`test_llm.py::TestNoCredentialInSource` 盯着源码里不许出现 `sk-` / `bgw_` 这类前缀。

> 门禁 `testpaths = ["trajectory_pipeline/tests"]`（v1 测试树 2026-10-09 随代码归档退役出默认门禁，手动跑法见 [`archive/v1/README.md`](archive/v1/README.md)）。
> 历史教训仍然有效：gdr/tests 曾漏在门禁外，360+ 条不跑，掩盖了 2 处生产代码从未执行——
> **门禁必须显式枚举每一棵在开发的树，漏一棵 = 静默失效不可见**。

---

# 新架构（`trajectory_pipeline/`）

## 模块地图

```text
trajectory_pipeline/
├─ taskgen/       模块 1  任务生成 + persona 画像库
├─ executor/      模块 2  代码执行循环（控制流在这里，不在任何 LLM 里）
├─ perception/    模块 3  语义感知（可替换插件：rule / llm + 注入工厂）
├─ rationale/     模块 4  rationale 边写边生成 + 一致性闸门三查      [待建]
├─ assembler/     模块 5  组装与切分（schema + 观察视图已建，落盘待建）
├─ evaluation/    模块 6  评估体系（黄金集 + 三层瀑布 + LS 双向）    [D-2 打分器已建，其余待建]
├─ common/        跨模块地基（无业务逻辑）
├─ llm/           LLM 客户端 + 结构化输出解析（全树唯一出口）
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

W1 的诚实说明：`RulePerceptor` 对 ① `SELECT_PLAY_SITES` / ② `IS_REACHABLE` **只能返回 `None`**（搜索链接筛选与「是否登录墙」都不是规则能判的），所以 W1 批次这两环全是 `unresolved`。这不是缺陷，是 fail-closed 的正常表现；W3 的 `LLMPerceptor` 已接上，同一份控制流不需要改动一行。

**四个判断点现在全部有调用方**（2026-10-09 前 ① 是死的——`orchestrator` 环节 ① 之后直接 `extract_candidates` 全遍历，感知层无人问，`not_play_site` 这条负分支在真实链路上永远触发不了，而报表上分部数字齐全，没人会看得出这支空了）。① 的接线口径与 ② 同款：**None 时不中断采集**，候选仍按代码启发式全跑；判 True 才按 `source_href` 过滤；判 False 则**中断遍历且逐条记 `not_play_site`**（中断若不记账，这批结论就没有任何样本）。

**① 的输入已经补上，但它曾整批产出假负样本**（详见避坑指南 §5.11–§5.12）。`browser_links` 对搜索结果页给的是**面包屑**而非标题（bing 实测 32 条没有一条写着片名，片名在 `body_text` 里但与 URL 无对应关系），① 拿到的输入根本不支持它那道题——模型答「检索摘要未显示作品标题，无法确认」是**对输入的准确描述**，随后 fail-closed 成 `False` 入了负样本池。两条修法**都做了**：

- **修输入**：`RESULT_SELECTORS` + `browser_extract` 取 `li.b_algo h2 a` 的 title 与 href 作为平行数组，写回 `search_obs.links[].text`。实测 bing 从 **0/32 → 9/9**，baidu 9/9。
- **加预检**：`_title_link_sufficiency` —— 链接文本里没有片名时 ① 直接 `None`。**这一层不能省**：选择器会随改版失效，那时 ① 必须退回 fail-closed 而不是拿坏输入硬答。

⚠️ **baidu 的 `browser_links` 本来就带片名**（实测 26/99），只有 bing 不带。所以 §5.11 那条结论**只在 bing 上成立**，别推广成「所有引擎的 links 都是面包屑」。

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
| ④ | `player_unverified` | 有播放组件，但确认不了能不能用 | ➖ 人工复核（**不入负样本池**，2026-10-09 新增） |
| ④ | `unresolved` | 置信度不足且重采样耗尽 | ➖ 人工复核（**不入负样本池**） |

**每条分支都必须有对应样本入库**——负样本不是副产品，是强制产出。反过来，`unresolved` / `trailer_suspect` / `player_unverified` **不是负样本**：把它们混进负样本池会污染它，「这里真的看不了」这条训练信号就此失真。

**三支「没判出来」成因不同，刻意不合并**（避坑指南 §5.17，rubric v1.1 的对照）。`unresolved` 是「连播放页都没到，不确定能不能到」（代码能力不足）；`player_unverified` 是「控件点了、快照拿到了、播放页就在眼前，只是判不了组件能不能用」（页面证据不足）；`trailer_suspect` 是「词表判不准」（需语义判断）。合并之后复核队列就分不出「请补一条判据」与「请看一眼这个页面」这两种待办，而报表也指不出该修代码还是该修采集。

⚠️ **`player_unverified` 不回填历史存档**（项目纪律：并池是写入时的动作，不是读取时的推导）。批次边界之前 `unresolved` 高、之后 `player_unverified` 高，不记批次就会读成「fail-closed 修好了」。**批次元数据要人工记这条。**

**贴标签前核前置事实**（`branches.BRANCH_PRECONDITION`，避坑指南 §5.14）。分支名是**结论的标签**，而每条负分支的定义里都带一个前提：`component_unverified` 的定义是「**有 video 标签**但播放器未正常加载」。映射只看「模型说了 False」，于是 2026-10-09 真实跑出一条 `component_unverified`——点击前后两次观察完全相同（390 字 / 37 元素 / `video=0`），说明点击没把页面带进播放页，而那个站有《功夫》也有播放按钮。**把可看的站写成看不了的**，这类样本进了池，人工也未必再看得出它是错的。前置事实不成立就降级为 `unresolved`，并把模型原话保留在证据里。当前只有 `component_unverified` 一条需要核——**这张表要短**，长得跟分支表一样长就没人维护了。

**反爬拦截刻意不进这套分支体系**：拦截发生在**取候选之前**，根本没有 outcome 可记。它单开运行级字段 `RunRecord.search_blocked`——代价是 `report` 要多统计一次，换来的是「被拦」与「没素材」在报表上永远分得开。

## 数据契约（P1/P2/P3，与存量 C1/C2/C3 目录、命名、schema 全不重叠）

| 契约 | 落点 | 内容 | 产出方 |
|---|---|---|---|
| **P1 观察存档** | `output/pipeline/<task_id>__<hash>.json` | 动作 + 工具参数 + **观察原文全文**（真实回放，非 LLM 转述） | `executor/archive.py` |
| **P2 单条样本** | `output/pipeline/samples/<id>.json` | 六件套 + provenance + gate 标记 | `assembler/builder.py` [待建]（形状与切条已在 `assembler/schema.py`） |
| **P3 训练视图** | `output/pipeline/views/<id>_{messages,openai,meta}.json` | 训练框架可读形态 | `assembler/views.py` [待建] |
| 负样本池 | `output/pipeline/negative.jsonl` | 失败分支样本（带 `branch`） | `executor/branches.py` |
| 复核队列 | `output/pipeline/review_*.jsonl` | 人工裁定（`verdict` + `reviewed_by`） | `executor/review_queue.py` |

字段级契约文档在 `trajectory_pipeline/docs/contracts/`（**目录已建，P1/P2/P3 三份待写**）。P1 的实际形状以 `RunRecord.to_json()` 为准，路径形状以 `archive.DEFAULT_ROOT` 为准（**平铺单文件，不是 `observations/<run_id>/` 目录**——那是设计方案早期为动作流分片预留的形状，实现从未采用，文档已对齐）。

**P1 里三处「不给就不可判定」的字段**（2026-10-09 按 rubric v1.1 补齐，实施记录见 [`03-rubric修订与实施方案.md` §9](trajectory_pipeline/docs/设计方案/03-rubric修订与实施方案.md)）。它们都不是「多存点信息」，每一处缺了都会让评分器把两件相反的事当成同一件：

| 字段 | 缺了会怎样 |
|---|---|
| `run_config`（整个 `RunConfig` 快照） | `--max-candidates 5` 与默认 20 跑出的两批**在存档里一模一样**，而覆盖完整性差 4 倍。`per_site_timeout_s` 更隐蔽——它连 `--help` 都查不到，却是 D-8 唯一的时限判据。缺它判 **DEGRADED**（`run_config_missing`） |
| 观察的 `elements_total` / `links_total` | 存档把交互元素裁到 80 条。「目标真不在这一页」与「目标恰好落在裁剪区外」同形——**两种相反的错判产出同一个分数**，而打分器看起来完全正常 |
| outcome 的 `site_url` / `play_page_url` | `outcome.url` 是**一字段三语义**（导航失败=候选 URL、找控件失败=`landed_url`、到播放页=`player_obs.url`）。读档的人得回查控制流才知道它指哪一层；丢了其中一个，另一个再也恢复不了（实测存档里有 http→https 跳转过的站） |

**正文 `body_text` 落盘全文，不落摘要。** 早期只留前 400 字符，理由「体积失控、需要时重抓」——站不住：站点会下线改版（重抓拿到的是另一个页面）、被反爬时根本重抓不回来、rationale 的实体核查会把落在摘要外的实体判成幻觉（那不是幻觉，是**没存**）。P1 是批次唯一真值源，证据链断裂不可逆，体积只是磁盘（D8 已隔离产物路径）。

**动作 + 工具参数落在 `steps[]`**（顶层是搜索阶段，`visits[i]` 里是站点阶段），语义见 [executor/actions.py](trajectory_pipeline/executor/actions.py)。三条口径：

- **`observe` 不是动作**，是上一个动作的观察回执。一次动作 + 紧随的 observe = 一个 step。记成动作会让模型学出「调用 observe」这种它不该发出的动作。
- **动作里没有 `ref`**——它是 obscura 的会话内句柄（导航前有效），进训练数据等于让模型学随机数。点击目标一律语义化为 `{tag, label}`，这是**结构保证**（`Action` 没有可放 ref 的字段），不是序列化时过滤。实测 ref 从来没进过 P1（成功路径只记 `PLAYER_OK` 的 evidence，带 ref 的 `FIND_PLAY_CONTROL` 结论不记账），所以 `steps[]` 比 P1 原来有的**更多**。
- **`origin` 区分「模型会选」与「执行器自己做的」**——会话隔离用的 `new_tab` 标 `infrastructure`：模型永不输出它，但它是真发生过的动作，删掉 P1 就无法回放。

另有一个容易漏的字段：**`user_prompt` = 用户开口说的那句**（persona 渲染后的原文），与 `query`（拿去搜的检索串）是两件事。六件套第 ③ 件要的是前者——它曾只活在计划文件里，于是 P2 拿不到用户轮次，而存档里看不出任何异常。空串 = 手工路径（与 `provenance` 空 dict 同款约定）。

**观察是真实回放，不是 LLM 转述**——这条一旦破，rationale 就没有锚。

## 模块 5 · 组装（`assembler/`，形状已建，落盘待建）

[assembler/schema.py](trajectory_pipeline/assembler/schema.py) 定义六件套值对象并**按文件格式**读 P1 切成样本；[assembler/observation_view.py](trajectory_pipeline/assembler/observation_view.py) 渲染训练态观察（纯代码，不用 LLM）。**`builder.py` / `views.py` 尚未写**，所以现在还不产出 P2/P3 文件。

四条口径：

- **一条 = 一个决策单元**（搜索阶段一条 / 每个站点一条）。设计方案 §3.5 早期同时写了「各自独立成条」与「前几步走摘要」，两者矛盾；按决策单元切唯一确定，且不把「选哪个站」与「这个站行不行」压进同一次预测。
- **读 P1 不 import `executor`**——与 `executor/plan.py` 不 import taskgen 同一条纪律。理由是 **P1 贵、P2 便宜**：重建一批 P2 必须独立于 executor 的当前版本，否则老 P1 切不出样本。由 `tests/contract/test_p2_contract.py::TestNoExecutorImport` 用 AST 强制。
- **训练动作空间在 assembler 侧冻结**，既不从 P1 反推（反推会让每条样本各带各的动作空间），也不从 `executor.actions.TOOLS` import。漂移由契约测试 AST 读那个字面量来查。
- **缺件必须看得见**：闸门三值 `passed` / `failed` / **`not_run`**——模块 4 未落地，一律 `not_run`；「没跑」渲染成「通过」会让批次上线时看起来像过了闸。老存档缺的键（`steps` / `user_prompt` / 全文正文）逐条记进 `degraded_from`，不与新样本混用。

## 执行层：obscura（决策 D7）

Rust 无头浏览器引擎，内置反检测，**经标准 MCP 协议 stdio 直调**（`obscura mcp --stealth`，协议 2024-11-05，37 个 tool，会话式）。放弃 Playwright MCP + Camoufox。

`executor/browser/` 分三层：

| 文件 | 角色 |
|---|---|
| `page_driver.py` | **协议，全树唯一稳定契约**。只暴露原子原语（`goto`/`snapshot`/`click`/`type`/`evaluate`/`interactive_elements`/`links`/`media_probe`），**不封装任何业务语义** |
| `mcp_client.py` | 传输层。JSON-RPC 进、文本出，**不认识任何 tool 名** |
| `obscura_driver.py` | obscura 接入（`PageDriver` 实现）+ `FORBIDDEN_TOOLS` 闸门 |

`mcp_probe.py` / `probe_returns.py` / `probe_observe.py` / `probe_result_titles.py` 是**可重跑探针**，分别取 tool 清单、返回格式、端到端 `observe` 契约、**结果标题选择器**取证——上游版本漂移时先重跑它们。

**obscura 不给 `inputSchema`**：实测四个采集 tool 的 `inputSchema` 全是 `null`，它把 schema 放在自定义的 `input_schema` 键里，而 `McpClient.list_tools` 读的是前者。所以**入参名只能靠探针取证**，看文档或类型标注都发现不了。已踩的两个：`browser_extract` 的入参键叫 `schema`（不叫 `fields`），且 `[]` 只是**入参**约定、**返回的键不带 `[]`**（`titles[]` → `titles`）。同一个症状（抽不到标题）两层不同的错，见避坑指南 §5.12。

## 感知层（模块 3，可替换插件）

`perception/base.py` 定协议，`questions.py` 定判断题注册表，`rule_perceptor.py` 是 W1 实现，`llm_perceptor.py` 是 W3 实现，`factory.py` 做注入与灰度。

输入是 DOM 预处理后的结构化观察（`Observation`），不是原始 HTML；输出必须是 schema。**七不变式由契约测试强制**（`tests/contract/test_perception.py`，`IMPLEMENTATIONS` 一行一个实现，两个实现跑同一套）：

| | 不变式 |
|---|---|
| I1 | evidence 可溯源到 `Observation` |
| I2 | `confidence ∈ [0,1]` |
| I3 | 幂等——同一 `(question, obs)` 重复调用返回相同答案 |
| I4 | fail-closed——能力不可用返 `None`，**绝不猜** |
| I5 | 无副作用——只读 `obs`，不触网、不持页面句柄 |
| I6 | decision 型题 payload 完整，`ref` 可溯源到 `obs.interactive_elements` |
| I7 | `answer=True` 时 payload 不得为空 |

### W3 的 `LLMPerceptor` 是混合体，不是纯 LLM

「LLM 是传感器不是驾驶员」在实现层是三条可检查的落法，缺一条就会把采集故障写成业务结论：

1. **确定性事实优先，LLM 只做语义补位。** `<video>` 标签存在性是代码事实（`video_tag_count`），**不问模型**；预告片词表是**本项目的业务规则**，模型不知道也不该猜——它只在模型选中某个 ref 时用来**复核**。
2. **采集充分性在代码层预检，在问模型之前**（`_sufficiency`）。这条是实测逼出来的，也是 W3 最贵的一条：**模型不会替你 fail-closed。** 实测 iqiyi 播放页 `video=1` 被判「否」，模型的分析是「`body_text` 为空、`interactive_elements` 为空，说明未渲染或采集失败」——它把**采集失败**讲成了一条通顺的错结论。规则版早就防住这条（哨兵返回分不清「没渲染完」与「确实为空」），换成 LLM 就丢了那层保护，而它恰恰是最需要的地方。
3. **ref 白名单校验。** 模型回传的 ref 必须**真的存在于** `obs.interactive_elements`，否则 fail-closed。模型会编 ref（会话内句柄看着就像可生成的字符串），而代码拿它去 click——**点错不可逆**。

第 2 条的**最窄形态**（`FIND_PLAY_CONTROL` 撞上 `interactive_elements == []`）现在两版口径一致，并由 `test_perception.py::TestZeroElementsBothImplementations` 锁住：实测 m.ixigua.com/video/6582085495839261192 正文 100 字符、元素 0 个，模型仍判了 False。**别拿「有正文」当「渲染完了」的证据**——正文与元素来自两个独立的 tool，一个有值不代表另一个有值，而只看着正文那个就会把「没渲染完」写成「这个站没控件」。

另外：**不采信模型自报的 confidence**（`llm/__init__.py` 记着「自报普遍虚高」）。给的是未校准先验 `CONFIDENCE_JUDGED = 0.8`，evidence 里写明「非模型自报」，拿到标注集后按题校准。

**灰度只在构造期**（`factory.build_perceptor`），不做运行期切换：运行期切换会让 `Decision.source` 分不清「LLM 判的」与「LLM 挂了所以规则版顶上来的」，而后者不是 fail-closed，是拿不确定的输入驱动确定的输出。要对比两条链就跑两批——`RunRecord.perceptor` 字段已经把两者分开。

配置：`TRAJECTORY_PERCEPTOR=rule|llm|auto`（默认 `auto`，配了 LLM 后端就用 LLM 版，否则退回规则版**且不报错**，W1 批次必须在没有后端的机器上能跑通；`run --perceptor` 可临时覆盖）。显式 `llm` 而未配置则**报错**——静默退回会让人以为跑的是 LLM 版。后端配置见 [`llm/__init__.py`](trajectory_pipeline/llm/__init__.py)：`TRAJECTORY_LLM_BASE_URL` / `_MODEL` / `_API_KEY` / `_TIMEOUT_S`，**缺失即报错不猜**（与 `OBSCURA_EXE` 同纪律）。

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

## 模块 6 · 评估（`evaluation/`，评分器代码断言层已建）

[`evaluation/scorers/code_scorer.py`](trajectory_pipeline/evaluation/scorers/code_scorer.py) 目前只实现 **D-2 观察-动作一致性**，按 rubric v1.1（[`在线视频场景rubric.md`](trajectory_pipeline/docs/设计方案/在线视频场景rubric.md)）。它是**第一个落地的评分维度**，因为 D-2 是唯一一条「不补数据就必然产生反向错判」的维度——存档把元素裁到 80 条时，「目标真不在这一页」与「目标落在裁剪区外」同形。

**三态而非两态**：`score=None` 表示**不可判定**，与 5 分严格分开。两个方向都不能含糊——「没查」渲染成「查过了」等于凭空白发通行证；「不可判定」并进「引用不存在」等于把采集器的取舍记成轨迹的缺陷。不可判定的五个入口：元素/链接裁剪、采集降级、正文截断、观察三项全空、`target=None`。

**`goto` 的 URL 刻意不校验**：它要核的「这个 URL 在搜索页链接里吗」需要**另一条样本**的观察（决策单元切分的结果）。在本样本内查它必然查不到，于是每条 `goto` 都记一条不存在、全批恒 1 分。恒定分数没有区分力——与 D-1 被划到批次级 B-1 同源。

`evaluation/golden/` 与批次级断言 B-1..B-4 **未建**。

## 现状（2026-10-09 午后快照）

**已跑通**：执行层端到端（真实 bing/baidu 站点）、模块 1 采样与 `run --plan` 接线、反爬拦截识别与单独记账、候选域名过滤、persona 渲染、复核队列与 `review --apply`、**成功站点域名分布报告（D7 验收）**、**P1 动作流（`steps[]`）与 `user_prompt` 落盘**、**P2 六件套形状 + 按决策单元切条（不落盘）**、**W3 `LLMPerceptor` + `factory` + 判断点 ① 接线**、**P1 存档完整性门禁 `check-archives`**、**感知层回放 `cli replay`**、**W3 真实端点批次（`steps[]`/`user_prompt`/正文全文齐备）**、**负样本池落盘**、**判断点 ① 的输入修复 + 充分性预检**（`RESULT_SELECTORS` + `browser_extract`，bing 从 0/32 条带片名变成 9/9）、**整批回滚 `cli purge-run`**、**负样本池跨存档矛盾检查**、**rubric v1.1 对齐：第九条分支 `player_unverified` + P1 补 `run_config` / 元素总量 / 三层 URL + D-2 打分器**（门禁 **841 passed**）。

**待办**：

- **`negative.jsonl` 里那条整批污染的数据已清**：`run` 少给 `--title` 曾拿 `T001` 当片名，搜成「轮胎 T001」，产出 6 条理由通顺的 `not_play_site`（`autohome` / `bridgestone` / `et001` / `yoojia` / `smzdm` / `targetmol`）。守卫已三处补上（§5.13），并于 2026-10-09 经确认后 `cli purge-run --run-id b3a3548a --apply` 清掉存档 1 个 + 池行 6 条（38 → 32）。**回滚命令的 `--apply` 路径本身当时是第一次执行**，当场炸出一个 bug（见 §5.16）——所以「命令写完了」不等于「命令能跑」。**未经确认不要动池**。
- **`negative.jsonl` 另有 3 条 URL 与成功记录自相矛盾**（hao123 / tv.sohu / **v-wb.youku**，池记负样本、存档判成功），**已冻结待人工定夺**。youku 那条现在是 **3 份**存档一致判成功（`T001__cebf5f18` 判「优酷《功夫》视频播放页，检测到 1 个 iframe」）——**输入修复后重跑直接把它推翻了**。处置是移出来重判，不是看着办；按 URL 逐条定，不要整批 `purge-run`（同 run 里可能有完全正确的结论）。
- **判断点 ② 让错误页通过了**（2026-10-09 实测）：bilibili 播放页正文写着「发生了错误，请稍后再试」，② `IS_REACHABLE` 判 True 放行，③ 才落成 `no_play_control` 负样本——而错误页按分支定义属 `login_wall_or_blocked`。这是**提示词质量问题不是机械缺口**（没有可靠的代码层事实能区分「错误页」与「正常页」），要么改 ② 的输出契约强调错误页，要么接受 ③ 兜住它。**未修**。
- **W3 真实批次已跑，剩「rule vs llm 分歧率比对」未做。** `replay --perceptor both` 需要正文全文的存档——老存档是 400 字符预览，新批次已具备条件、对比尚未跑。跑新批次前仍建议先 `check`：模型名写错时每道题都返回 `None`，症状像「LLM 判不了」实则 404。**注意 `TRAJECTORY_LLM_TIMEOUT_S` 默认 30s 对判断点 ① 不够**（实测 5 条里超时 3 条），跑真实批次先把它调到 90。
- **`--engine bing` 是目前唯一验证过判断点 ① 的引擎**：baidu 的 `browser_links` 本来就带片名（实测 26/99），bing 只给面包屑（0/32）。§5.11 那条结论**只在 bing 上成立**，别推广成「所有引擎的 links 都是面包屑」。
- **规则版 `PLAYER_OK` 对真实视频站的正样本召回仍是 0**——只认 `<video>`/`<audio>`，真实视频站全是 iframe/JS 播放器，判断点 ④ 对它们一律 `None`。**W1 正样本靠人工复核**（`review --write` → 人工填 `verdict` → `--apply`，报表会按 `source` 把人工确认的成功与规则判定分开列）。这是 W3 `LLMPerceptor` 的直接输入，不是 W1 缺口。⚠️ 2026-10-09 起这类 `None` 落在 **`player_unverified`** 而不再是 `unresolved`（§5.17），所以报表上 `unresolved` 骤降**不是质量变好**。
- **`CONFIDENCE_JUDGED = 0.8` 是未校准先验，不是实测值。** 拿到标注集后要按题校准，否则阈值判断全是在拿一个常数当概率用。
- **`trailer_only` 词表覆盖率与误杀边界**（真实站点上只跑到 `trailer_suspect`，`trailer_only` 一次都没触发）。
- **老存档与新存档双轨**：2026-10-09 之前的存档（4 份 T001 + `live/`/`live_bing`/`fix/` 各若干）跑在 `steps[]` 落地之前，切出来的 P2 全带 `degraded_from: [steps, user_prompt, body_preview_only]`；**新批次（llm 存档）已带 `steps[]`/`user_prompt`/正文全文**，契约测试 `TestToolSpaceDrift` 已从 skip 转为生效。老存档不与新样本混用，`degraded_from` 逐条记账。**现在又多两项**——老存档还没有 `run_config` 与元素总量，D-2 打分器对它们一律出「不可判定」（正确：这批数据确实判不了）。
- **5 份老存档含 GBK mojibake**（`T001__004b6be9` / `0778f4ab` / `65208dfb` / `b9d104e7` / `T013__cb96e512`，共 62 个 U+FFFD）。诊断：站点发的是 **GBK 字节，被当 UTF-8 解码**——合法的双字节序列偶然解成 `ǳ`「长」这类字符，非法字节变成 U+FFFD。**从存档已不可恢复**（字节丢了），要修得在采集层按 `charset` 重新解码。新跑的批次没有这个问题。
- `assembler/builder.py`（落 P2）+ `views.py`（P3）+ `splitter.py`（切分已并入 `schema.split_archive`，是否仍单列待定）；`rationale/` / `storage/` 两个模块 + `evaluation/` 的黄金集与批次级断言 B-1..B-4。
- **变更 E（`Observation.player_signals`，D-4 正例的判据数据源）按计划延后**——等 `LLMPerceptor` 在真实端点跑通一批再做。D-4 的**错误**档（判有组件而存档无播放元素）在元素总量落盘之后已可判。
- `docs/contracts/` 下 P1/P2/P3 三份字段级契约。**P1 这轮又加了 5 个字段**（`run_config` / `elements_total` / `links_total` / `site_url` / `play_page_url`），契约文档还没写，`TestDegradedMarkersMatchSplit` 只盯缺件标记、盯不住新增字段。

**每次真实运行后都要逐条打开存档读 evidence 核对**——这是固定动作不是可选项。设计上五类缺陷全部是离线测试测不出来的（fixture 是我们自己造的，规则和 fixture 一起错，测试照样绿），只有把真实站点喂进去、逐条读存档，才会看见「结论」和「证据」在打架。踩坑全记录在 [`trajectory_pipeline/docs/02-避坑指南.md`](trajectory_pipeline/docs/02-避坑指南.md)。

**这条现在是命令了**：`cli check-archives`（`executor/integrity.py`）是它的机械版——查证据链完整性、结论与观察是否自相矛盾、真负样本有没有并池。**纯手工的固定动作等于没有**：它靠人记得做，而它恰恰是最容易在赶批次时跳过的一步。选档案走 `archive.select_archives`，与 `report` 同一口径，否则会出现「门禁说全过、报表说有 5 个档有问题」。三档严重度：`FATAL` 不可信只能重跑 / `DEGRADED` 缺件不能直接当训练数据 / `INFO` 看一眼。**判断「结论对不对」仍然不在门禁里**——那要人读证据，它只保证「值得人读的东西没悄悄烂掉」。

## `check-archives` 在现存 12 份真实存档上查出来的三件事（2026-10-09）

门禁第一次上线就抓出三条**报表上看不见**的问题——这正是它存在的理由：

| # | 查到的 | 为什么报表上看不见 |
|---|---|---|
| 1 | **7 条真负样本从没并池**，`negative.jsonl` 根本不存在 | `report` 从存档里的 `ledger` 读分支分布，不看池。「每条分支都必须有对应样本入库」是硬要求，而这批跑在并池逻辑之前 |
| 2 | **`65208dfb` 的 3 条 success 有 2 条是假阳性**（hao123 首页 iframe=15、youku iframe=1） | `report` 只数「成功 N 条」，不看依据。门禁比对的是**结论与存档自己记的观察是否自相矛盾**：判成功但 `video_tag_count=0`，而 rule 版唯一判据就是媒体标签 |
| 3 | **人工复核从未 `--apply` 回填**，全库没有 `.reviewed.json` | `review_live*.jsonl` 队列在，`review --apply` 没跑过。W1 正样本的唯一来源就是人工复核 |

**「终端乱码 ≠ 存档内容坏了」**（避坑指南 §6.4）：现存 12 份存档 37 条 outcome 的 `evidence` **无一损坏**；真正烂掉的是 4 份 T001 存档 `visits[3]`（tv.sohu.com）`body_preview` 中间一段与一个 `<a>` 标签，同段里正常中文是好的——触发条件是**站点声明的 charset 与实际内容不一致**，同一批 11 个站没事、同一个站 4 次全坏，**重跑同一批不会变好**。判断必须在落盘后用 `strict` 重新解码，在终端里 `print` 出来的样子不作数。

**2026-10-09 午后复跑**（选定 16 份存档 / 73 站点 / 真负样本 44）：FATAL 仍是 `65208dfb` 那两条；新增**池与存档自相矛盾**一类——3 条 URL（hao123 / tv.sohu / v-wb.youku）池里记负样本、存档判成功，标签打架不能进训练数据，**要动的是池**；DEGRADED 全部集中在 4 份老 T001，新批次（llm 存档）干净；复核仍无 `.reviewed.json`（午后快照）。

**2026-10-09 rubric 对齐后再复跑**（16 份 / 65 访问点 / 74 outcome / 真负样本 41）：FATAL 仍是那两条、**没有新增**（P1 形状变化没引出新问题）。DEGRADED 多了一项 **`run_config_missing ×16`——16 份全中，因为它们全跑在 `run_config` 落盘之前**。判 DEGRADED 而不是 FATAL 是刻意的：这批数据本身仍是真证据，丢的只是「覆盖完整性分母」这一个字段；判 FATAL 会让整批真实存档不可用。它们同样也没有 `elements_total`，所以 D-2 打分器对它们一律出「不可判定」——**这是正确输出，不是缺陷**。

## 感知层回放（`cli replay`）

`perception/replay.py` 把 P1 存档里的**真实观察原样**喂给感知实现，**不开浏览器**。P1 存的是 `_obs_json` 落下来的结构化观察——`video_tag_count` / `iframe_count` / `interactive_elements` / 正文 / `degraded` 全在，那正是 `Observation` 的字段。

**验**：感知层在真实数据上的判得出来率、两版答案差在哪。
**不验**：ref 点击、播放器探测、跳转、反爬、加载时序——**所以它不能替代真实批次**。

`--perceptor rule` **不需要任何 LLM 后端**，于是「W1 在真实数据上答得出来几道」完全离线可测。在现存 12 份存档上回放 49 道真实判断题，规则版：

| 判断点 | 答得出来 |
|---|---|
| ② `IS_REACHABLE` | **0 / 20** |
| ① `SELECT_PLAY_SITES` | **0 / 4** |
| ③ `FIND_PLAY_CONTROL` | 8 / 20 |
| ④ `PLAYER_OK` | 1 / 5 |
| **合计** | **9 / 49（18.4%）** |

这就是「W1 正样本召回仍是 0」的定量版本——**②① 两环全灭**，而它们不是被判成 False，是 fail-closed 返 `None`。

**⚠️ 只报分歧，不报准确率**：拿 rule 的答案当真值会得到一个漂亮的假数字（rule 判成功的 3 条里 2 条是假阳性，见上）。真要算准得先有人工标注集——那也是 `CONFIDENCE_JUDGED` 校准的前置条件，两件事一起做。

**⚠️ 2026-10-09 之前的 12 份存档正文全是 400 字符预览**（49/49 降级）。**新批次（llm 存档）正文全文，`--perceptor both` 已具备条件**——rule-vs-llm 分歧率对比尚未跑，做完它 W3 验收项 ② 才闭环。

---

# 存量架构（v1，冻结只读，已归档至 `archive/v1/`）

`simulation server → gdr → etl` 三阶段，每段一道边界，交接面写契约（C1/C2/C3/C4）。产物落根 `output/`（数据未随档）。
下表目录名均指 `archive/v1/` 下的同名目录；归档说明与手动运行方法见 [`archive/v1/README.md`](archive/v1/README.md)。

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
- [`trajectory_pipeline/docs/设计方案/02-rubric与代码对齐.md`](trajectory_pipeline/docs/设计方案/02-rubric与代码对齐.md) —— 评分标准 [`RUB-VID-PLAY-001`](trajectory_pipeline/docs/设计方案/在线视频场景rubric.md) 逐维度 ↔ 新树代码的对齐账：每个维度靠哪份已落盘数据判、哪些判不了、缺什么。**改 rubric 或改控制流之前先读**——它记的是两侧哪里已经错位，而错位不会自己显形
- [`trajectory_pipeline/docs/设计方案/03-rubric修订与实施方案.md`](trajectory_pipeline/docs/设计方案/03-rubric修订与实施方案.md) —— 上两条定下来的决策怎么落地：rubric v1.1 逐条改稿清单 + 五个代码变更（点文件、点函数）+ 执行顺序 + 每项改动配的机械验证。**§9 是实施结果**（哪条做了、哪条没做、每条由哪个测试封着、方案漏掉了什么）。**要动 `branches.py` / `observation_view.py` / `integrity.py` 的先读**
- [`trajectory_pipeline/docs/02-避坑指南.md`](trajectory_pipeline/docs/02-避坑指南.md) —— **9 类 50 条**，症状写成终端里真正会看到的那句话，可全文搜索

**存量**（均在 `archive/v1/` 下，按需查阅）：`docs/orchestration-design.md`、`docs/observability-label-studio.md`、`docs/label-studio-playbook.md`、`docs/label-studio-annotation-export.md`、`docs/设计方案/`、`docs/contracts/`、`gdr/docs/`。