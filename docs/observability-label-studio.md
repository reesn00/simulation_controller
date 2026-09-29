# Label Studio 集成（用户视角）

> 你好，这份文档面向**用这套系统的人**，不面向维护代码的人。
> 技术细节见 [`设计方案/label-studio-integration.md`](设计方案/label-studio-integration.md)
> 与 [`contracts/C4-scorecard.md`](contracts/C4-scorecard.md)。

## 1. 这是什么

你写的模拟任务跑完之后，除了产出训练用的 4 视图数据，还会把**同一条轨迹
连同一张"评分卡"一起推到本机的 Label Studio**（LS）。

LS 是这条链的**终点**：你在那里逐条核对、给出最终判定。**判定结果不回流到
本项目** —— 本项目不会因为你的标注而改变任何行为，也不维护"哪些已经标过了"
的状态。要查标注结果，去 LS 里查。

## 2. 为什么要推评分卡

自动检查能算出很多东西，但它**不告诉你这个数字有多可信**。

举个真实例子：一条轨迹的"训练价值分"是 0.58，拆开是七个分量的加权和。
其中权重最大的一项（占四分之一）的计算代码里自己写着注释：

> health 来自 router.health_scores，runner 没把它落盘。这里按 message 数和
> 总 toolcall 数做粗估

也就是说，0.58 里有四分之一是**猜的**。如果只把 0.58 推给你，你会以为那是
实测值。评分卡把这一层摊开：

- 每个维度标 `source`：`measured`（实测）/ `partly_estimated`（部分估算）/
  `estimated`（整体推断）/ `missing`（数据缺失，**不可评分**）
- 估算过的分量列出来，并写清为什么是估算的
- **数据缺失时给"不可用"，绝不给 0** —— 0 是一个很具体的数，"没测过"不是 0

## 3. 怎么用

### 3.1 一条命令（推荐）

```powershell
scripts\label_studio.bat            # init-project → status → upload 全跑
scripts\label_studio.bat dry-run    # 同上，但只打印计划不真推
scripts\label_studio.bat status     # 只做连通性 + 配置自检
scripts\label_studio.bat upload --task-id T007   # 过滤条件原样透传
```

脚本自己处理 key、Python 启动方式和控制台编码（`chcp 65001`，否则中文报错
全是 `????`），不用记 uv 命令。完整用法跑 `scripts\label_studio.bat help`。

key 两种给法（脚本按 env → 文件顺序取，都支持）：

```powershell
$env:LABEL_STUDIO_API_KEY = "<你的 key>"     # 换窗口就失效
# 或写进 config\label_studio_api_key.txt    # 已 gitignored, 一次配好长期有效
```

**不要**把 key 当命令行参数传 —— 会留在 shell 历史和进程列表里。

### 3.2 准备

1. 本机跑起 Label Studio（默认 `http://127.0.0.1:8099`）
2. 在 `config/config.yaml` 里确认 `label_studio:` 段的 `base_url` 是 8099
3. 按 3.1 配好 key

> **端口不是 8088。** 8088 上跑的是 QwenPaw 执行后端。指错不报"连不上"，
> 而报**认证失败** —— 会把人引去反复检查一个完全正确的 key。

### 3.3 底层三条命令

```powershell
# 1. 建项目（幂等，重复跑不会建出第二个）
uv run python -m label_studio init-project

# 2. 自检：LS 通不通、配置全不全、凭据在不在（不打印凭据内容）
uv run python -m label_studio status

# 3. 先看看会推什么，不真推
uv run python -m label_studio upload --dry-run

# 4. 真推
uv run python -m label_studio upload
```

`upload` 的常用参数：

| 参数 | 作用 |
|---|---|
| `--dry-run` | 只打印计划（含每条的评分卡摘要），不推 |
| `--task-id T007` | 只推某一个 task（调试） |
| `--complexity-tier hard` | 只推某个难度档 |
| `--min-score 0.5` | 只推训练价值分 ≥ 0.5 的 |
| `--no-scorecard` | 只推轨迹不带评分卡（排障用） |
| `--force` | 样本数超过 `dry_run_skip_threshold` 时强制推 |

### 3.4 标注界面

每个 task 有 5 个页签：

| 页签 | 内容 |
|---|---|
| **评分卡** | 自动评分与依据，逐维度展开。`estimated` / `missing` 的维度有标记 |
| **指令核对** | 逐条列出每条指令的自动判定（PASS/FAIL + 原因）。**同意 / 不认同**必选；不认同要写理由 |
| **轨迹** | 结构化的消息与工具调用 |
| **ChatML** | 渲染成 Qwen3 格式的纯文本，训练框架实际吃的就是这个 |
| **元数据** | 审计用的全量 metadata |

最下面三个控件是必答的：

- **最终判定**：`accept` / `revise` / `reject` —— 留空提交不了
- **失败模式**：多选（`reasoning_error` / `tool_use_error` / `hallucination` / ...）
- **修复建议**：选填

**`最终判定` 没有预填值，这是故意的。** 系统不会替你判"这条该收还是该扔"。

## 4. 为什么"最终判定"不自动填

设计文档里原来有过一条规则：训练价值分 ≥ 0.7 就自动 accept，< 0.4 就自动 reject。
后来删掉了，因为 0.40 正好是"难度 = hard"的阈值 —— 也就是说，**自动 reject
掉的恰恰是最需要人看的样本**。而 easy 样本（0.7 以上）反被人自动放过，最不需要
人看。规则和自己的目标正好反了。

所以现在系统只做一件事：**提示风险**。

| 触发条件 | 提示 |
|---|---|
| 有指令项没达成 | 「指令未完全达成，请核对 C4」 |
| 触发红线 | 「红线违规 2 项，必须复核」 |
| 有高权重的估算分量 | 「该分量为估算值，非实测: health（合计权重 0.25）」 |

## 5. 自动推送

`orchestration` 跑完一个 task、etl 写完 C3 之后，会**自动**推一条到 LS。

三条保证：

1. **推不推得动都不影响 task 的结果**。task 的终态照常落库，LS 挂了只是日志里一句告警
2. **不会拖慢流水线**。推送有独立超时（默认 **30 秒**），LS 卡死就放弃
3. **默认关闭**。要开得在配置里显式写 `label_studio.hook.enabled: true`

自动推送用 `hook.enabled` 这个开关，命令行 `upload` 用 `upload.enabled`，
两个互不串。配了其中一个不会让另一个跟着开。

### 5.1 超时 / 进程被杀不会丢样本

一次推送要串完 PAT 刷新 → 建连 → 查项目 → 同步标注配置 → 建 task → 写预标注，
所以超时给的是 30 秒而不是 5 秒。

万一还是超时了、或者批次结束时进程被回收，**那条样本不会丢**：`push_single_c3`
是**先拿到 LS task id 再写台账**，所以「台账里没有」严格等价于「LS 上没建成」。
批次跑完之后补一次全量对账，缺的那些会被补上：

```powershell
.\scripts\label_studio.bat upload      # init-project → status → upload
```

这是幂等的 —— 已经推过的 session 靠台账跳过。**别删台账文件**，删了等于每次
upload 都推一份重复样本（LS 1.23 没有任何原生去重）。

### 5.2 低分样本也会推上来（2026-09-29）

gdr 判 `judge_discard`（质量评分没过的 session）**同样会推**。这类样本结构
合格、只是评分低，按项目的「数据保留原则」属于有用素材，不是废数据。

标注界面最上方有一个**「低分标记」**区块，写明它为什么被拒收。**看到这个
标记时请重点复核「精修质量」维度** —— 判别是"gdr 拒得对"还是"gdr 判错了"。
其余维度分数低不代表样本本身不可用。

没有这个标记只有两种可能：样本本来就没被拒收，或者它来自 `scoring_reject`
（那一支 gdr 刻意不写 C2，所以走不到推送）。

## 6. 有一类样本会被**拒绝推送**

Agent 的工具调用参数是**模型自己写的自由文本**，理论上可能把用户传进去的
凭据（`Bearer xxx`、`sk-xxx`、私钥块等）原样写进轨迹。协议上我们不主动存
Cookie / Authorization，但**挡不住 Agent 自己往里写**。

所以推送前会扫一遍，命中就**整条拒推**，并在输出里告诉你哪条、哪个视图、
第几个模式命中。

**为什么不自动脱敏后照推？** 因为那样你在 LS 里看到的样本就与训练时用的样本
不一致了 —— 你标的是"改过的东西"，评出来的结论没法用。宁可少一条，让你决定
怎么处理。

如果 `upload` 的输出里有 `rejected` 字段，去看一眼对应的 C3 是怎么生成的。

## 7. 常见问题

**Q：`status` 说"未找到凭据"**
设 `LABEL_STUDIO_API_KEY` 环境变量，或在配置里写 `label_studio.api_key_path`
指向一个存了 key 的文件。两者都配时 `api_key_path` 优先。

**Q：`upload` 说"未找到 LS 项目"**
先跑一次 `init-project`。

**Q：同一条轨迹推了两次，LS 里出现两条？**
本项目会跳过。判重靠本地台账 `output/label_studio/push_index__<project_id>.jsonl`
（`session_id → LS task id` 映射）。

> 早先的方案写的是"靠 LS 原生 `inner_id` 去重"，**这是错的** —— 2026-09-29 对
> LS 1.23.0 实测：`Task.inner_id` 是整数字段（发字符串直接 400），批量 import
> **静默丢弃**，整数 inner_id 重复导入照样每次新建一条（42 推三次 → id 7/8/9）。
> 所以台账不是"多一份状态"，是唯一挡板。

台账丢了或想重推：`scripts\label_studio.bat purge --confirm`（**会连标注一起
删**）后重新 `upload`，或者直接删掉那个 jsonl —— 删掉只会导致重推重复。

**Q：`upload` 报 `data['messages']=...` 400 / `data['criteria']=...` 400**
多半是 LS 项目里的 `label_config` 与本地 XML 不一致。LS 端存的是**建项目那一刻**
的 XML，而校验接口查的是你递过去的文本，两者不同步时"`init-project` 报绿、
upload 却 400"。

正常路径下 `init-project` 和 `upload` 都会把本地 XML 同步进项目
（`PATCH /api/projects/{id}`），所以直接重跑 `scripts\label_studio.bat` 即可。
若项目在 LS 界面里被人手改过导致同步失败（脚本会打 warning），删掉项目重建：
`purge --confirm` 然后 `init`。

**Q：某条轨迹的评分卡大部分是"不可用"？**
说明这条数据缺前置产物（比如 simulate 端没跑验证，或验证结果没注入 C3）。
系统不会拿 0 糊弄你 —— 但也别把这条当"没问题"看，它就是没测过。

**Q：能把标注结果导回本项目吗？**
不能，这是设计决定。LS 是终点。导出给你自己的分析流程用，不回本项目。

**Q：`purge` 会删什么？**
删掉整个 LS 项目，**包括里面已完成的标注**，不可逆。加 `--confirm` 才真删。
