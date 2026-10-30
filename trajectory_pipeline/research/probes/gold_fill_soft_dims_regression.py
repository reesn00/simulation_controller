"""**软维度金标（D-2/D-4/D-5/D-6）回归集的填表脚本**（2026-10-30）。

它是什么
--------
给**回归集**（`regression`，60 条）首次补上四维软维度分数：46 条有样本的
逐条判读（184 格），另 14 条没有样本的**不进表**（由
[`gold_dim_restore.py`](gold_dim_restore.py) 以显式 `null` 记「判不了」）。

它与 v8 那份的关系
-----------------
**口径完全沿用** [`gold_fill_soft_dims.py`](gold_fill_soft_dims.py)（2026-10-24
那一轮）：判读表按「族」给档位与凭据，D-2 与 D-6 走机械判定，D-4/D-5 逐族
按 rubric 锚点判。**先例也沿用**——同一批 URL（优酷 v_show、西瓜、bilibili、
v.qq cover、cctv 正片页、网易号/攻略页）在 v8 已判过的族，判法一致；
v8 没覆盖的新形态（新记法、新分支）在下面单独说明。

事实底稿由 [`gold_facts.py`](gold_facts.py) 从 P1 存档重算（`--worksheet`
参数化），看现场用 [`gold_peek.py`](gold_peek.py)。

**为什么脚本化**：工作表落在 `trajectory_pipeline/output/`（gitignored），
填完就看不见改了什么 ⇒ 判读必须有一份**可复算的凭据**留在仓库里。

---

## 本轮与 v8 不同的四处判读（先读这四条）

1. **同页守卫误伤 ⇒ 落点错、池归属错 ⇒ 2 档**（4 条：bilibili 正片页 ×3、
   优酷 v_show ×1）。这 4 条的存档 evidence 写着「[记账降级] 本该记
   component_unverified / subscription_wall，但点击播放控件前后是同一个页面
   ……这次点击没有把页面带进播放页」——**而同一行的 `reached_play_page=True`、
   `play_page_url` 就是本页** ⇒ 降级理由（「没到播放页」）与记录自相矛盾。
   H-17（2026-10-21 owner）明写「同一页两次快照 ⇒ 守卫放行」；同页守卫
   2026-10-19 也已收窄到只覆盖 `component_unverified` 一支。
   ⇒ 证据支持的落点是**排除类**，存档落 `unresolved`（未决、不入池）⇒
   **池归属变了** ⇒ rubric 2 档「结论错，过程部分对」。
   判法同 v8 的 `T012__8c050356__22e0fb36`（同一处守卫误伤，v8 判 2 档）。

2. **Q-23 现场复核后 relabel 的 7 条**：存档 evidence 记着
   「[原判 unreachable_hard] 导航失败（传输层）…… Q-23 现场复核（2026-10-28，
   camofox 真开浏览器）：**打得开**——落地 …，标题「…」⇒「真·站点不可达」
   不成立，原判是『这次没打开』」⇒ 存档分支已 relabel 成 `unresolved`
   （未决、不入池）。本轮按**存档**判（底料以存档为准）⇒ 落点对 ⇒ 5 档；
   池归属跟着 relabel 走（这类传输层失败不进负样本池，与 H-15/H-17 同口径）。
   ⚠️ **O-c 仍待主人裁**（`unreachable_hard` 该不该进负样本池）。

3. **D-6 的「验证证据引用观察」在 Q-23 行上成立**：Q-23 的现场复核是**另一次
   访问**，但它**引用了页面标题**（一次真实观察）⇒ 目标内容标识可以核。
   trtag 4 条（标题含《功夫(普通话版)》）与 yn-mg 1 条（标题含《功夫…》）⇒
   5 档；cmgo 2 条（标题只有「CMGO」）⇒ 检索不到且观察压根没提 ⇒ null。

4. **`login_wall_or_blocked` 是已弃用的合并态分支名**
   （`perception/questions.py::DEPRECATED_BRANCHES`）⇒ 按现行 22 支集，
   CF/地区码/反爬应落 `blocked`；按方案 20 R3（2026-10-26）`blocked` 已
   **出负样本池**、改进人工复核队列 ⇒ 本轮对这类行**按「分支事实正确」
   记 5 档，池归属口径按方案 20 重算**（在 note 里留痕，不在分数里藏）。

---

## 口径出处（与 v8 同一套，主人可否决）

- rubric v1.2 规格卡（D-2/D-4/D-5/D-6）＝ `docs/设计方案/在线视频场景rubric.md`
- 手册 §7.2/§7.2.1 的四维判据与七条判读口径
- `executor/branches.py::BRANCH_PRECONDITION`（分支前置事实）
- H-15 / H-16 / H-17（owner 2026-10-21）、M-30（URL 级裁定）、
  方案 20（反爬拦截与金标观测面，R3）

## 署名

`REVIEWER = "claude-ai"`：**本轮是 AI 判读**（owner 指示执行，人只做提交与
终裁）——金标的独立性因此打折，台账 M-34 里记着这条限制，**主人提交前要
过一遍**。

---

## 用法**（必须在仓库根跑）**

```bash
uv run python trajectory_pipeline/research/probes/gold_fill_soft_dims_regression.py            # 干跑，只报
uv run python trajectory_pipeline/research/probes/gold_fill_soft_dims_regression.py --write    # 写回工作表
```
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gold_facts import build as build_facts  # noqa: E402
from gold_fill_soft_dims import (  # noqa: E402
    D2_SCORE,
    _d2_note_for,
    _d4_machine_note,
    _d5_machine_note,
    _d6_note,
    _signal_line,
)

VERSION = 15
SPLIT = "regression"
WORKSHEET = os.path.join(
    REPO, "trajectory_pipeline", "output", "pipeline", "calibration", f"v{VERSION}", "gold_worksheet.jsonl"
)

#: 署名人（见模块 docstring「署名」一节）。
REVIEWER = "claude-ai"

#: D-2 判不了时的口径（与 v8 同句）。
NO_CLICK_D2 = (
    "本单元没有模型可见的 click 动作 ⇒ 无实体引用可校验 ⇒ 本维无适用项 ⇒ 判不了（不记 5）。"
    "（机器同此判 undetermined：「没有可校验项」不等于「全部可找到」——见 code_scorer.score_d2 的注释）"
)

#: 落 `unresolved` ⇒ 不对页面作任何断言 ⇒ D-4 无适用项。
UNRESOLVED_D4 = (
    "落 unresolved ⇒ 不对页面作任何断言（代码能力不足，不是页面证据不足）"
    "⇒ 本维无适用项 ⇒ 判不了（不记 5）"
)

#: 传输层失败 ⇒ 没有观察 ⇒ D-4 无适用项、D-6 无观察可核。
UNREACHABLE_D4 = (
    "导航失败（传输层）⇒ 没有页面观察 ⇒ 分支不对页面作任何断言 ⇒ 本维无适用项 ⇒ 判不了（不记 5）"
)

#: 「这一格没有可判的内容」时的统一理由（14 条无样本行 + 各族共用）。
NA_REASON = "无适用项 ⇒ 判不了（不记 5）"


# ---------------------------------------------------------------------------
# 判读表：按「族」给档位与凭据。
#   d4 / d5 ∈ 1..5 或 None（判不了）；d6 = None 表示按「片名可检索性」自动判。
# ---------------------------------------------------------------------------

FAMILIES: list[dict] = [
    # ── bilibili 正片页：同页守卫误伤 → 落点错、池归属错 ────────────────
    dict(
        name="bilibili 正片页（记账降级）",
        gids=[
            "E005__e35b7455__b8513a71",
            "T033__53ce4b49__22e0fb36",
            "T036__10313caf__22e0fb36",
        ],
        d4=5, d5=2,
        d4_ev="存档引用的「播放器区域明确显示『发生了错误，请稍后再试』」（另一条写「页面显示『发生了错误』」）"
              "＋「未检测到 video 媒体元素」与观察逐字对得上（vid=0、iframe=1、元素 42/42 与 47/47 全留、"
              "无裁剪）⇒ 组件判断与观察一致 ⇒ 5 档。⚠️ 正文里另写着「正片 | (1/8) | 第一期 …」等分集列表，"
              "组件是**候选**不是已确认——这正是下一格要记的账",
        d5_ev="存档的**意图落点**是 component_unverified（排除），但被同页守卫降级成 unresolved"
              "（未决、不入池）：降级理由写「这次点击没有把页面带进播放页，结论没有新证据支撑」，"
              "**而同一行的 record.reached_play_page=True、play_page_url 就是本页** ⇒ 降级理由与记录自相矛盾；"
              "H-17（2026-10-21 owner）明写「同一页两次快照 ⇒ 守卫放行」，同页守卫 2026-10-19 也已收窄到"
              "只覆盖 component_unverified 一支（方案 X）⇒ 前置事实其实成立 ⇒ 证据支持的落点是**排除类**，"
              "存档落 unresolved ⇒ **池归属变了**（排除↔未决）⇒ rubric 2 档「结论错，过程部分对」⇒ 2 档"
              "（判法同 v8 的 T012__8c050356__22e0fb36：同一处守卫误伤，v8 判 2 档）",
    ),
    # ── 优酷 v_show《功夫》：记录成功 ─────────────────────────────────
    dict(
        name="优酷 v_show 记录成功",
        gids=[
            "T011__443c5ec0__5c2c73cf",
            "T018__ad4a6065__5c2c73cf",
            "T025__7141c8de__5c2c73cf",
            "T028__d76e1085__5c2c73cf",
            "T029__0a800a3f__5c2c73cf",
        ],
        d4=5, d5=5,
        d4_ev="存档结论「检测到 1 个 iframe 播放器，且未出现订阅或付费后才能观看的提示，『续费会员』仅为导航栏入口」"
              "与观察一致（vid=0、iframe=1、元素 76/76 全留、无裁剪）⇒ iframe 在 rubric D-4 的判据列里"
              "（反例只针对**广告** iframe，且 rubric 明写「不作活跃扣分项」）⇒ 组件判断与观察一致 ⇒ 5 档。"
              "⚠️ **O-a 未收敛**（M-30 §2：同一份正文被读成 success / no_play_control / subscription_wall 三种落点，"
              "owner 明写「这条判据边界上有两种读法」）——本行按 rubric 读（iframe 是判据、导航栏「续费会员」不是付费墙）"
              "记 5 档；若主人裁定「组件未确认 ⇒ 应落未验证」，本行改 4 档",
        d5_ev="决策树第六节点（有组件且可确认、无付费墙）⇒ 落 branch=null 记录成功，节点全对 ⇒ 5 档。"
              "⚠️ 同 O-a：若主人裁定「组件未确认 ⇒ 应落未验证」，本行改 4 档（v8 同一 URL 的"
              " T040__c9982a1c__5c2c73cf 就是这么判的：5/5）",
    ),
    # ── 优酷 v_show《功夫》：记账降级（subscription_wall 意图）─────────
    dict(
        name="优酷 v_show 记账降级",
        gids=["TPIN02__1cf5deca__42ce4312"],
        d4=4, d5=2,
        d4_ev="页面正文明写「正片 | VIP | 功夫」＋「首3月9.9元/月 | 立即开通」⇒ 付费墙判据（门禁语境"
              "「开通会员观看」）成立；但 DOM 是 vid=0、iframe=1 ⇒ 按 D-4 判据集合，iframe 只是**组件候选**"
              "不是已确认 ⇒ 存档给的是确定结论（「需订阅会员才能观看」＝不可播放）⇒ 1 处「应判未验证却直接给了"
              "确定结论」⇒ 4 档（判「有」与判「无」都算 1 处 ⇒ 按锚点取 4 档；同 O-a 里两个「4 档」读法）",
        d5_ev="存档的**意图落点**是 subscription_wall（排除），但被同页守卫降级成 unresolved（未决、不入池）："
              "降级理由写「这次点击没把页面带进播放页」，**而 record.reached_play_page=True、play_page_url "
              "就是本页** ⇒ 前置事实成立 ⇒ 证据支持的落点是排除类、存档落未决 ⇒ **池归属变了** ⇒ rubric 2 档"
              "「结论错，过程部分对」⇒ 2 档（同 v8 的 T012__8c050356__22e0fb36 判法）。"
              "⚠️ BRANCH_PRECONDITION 记着「页面挂着『开通会员』字样**不算**，那是导航栏广告」——"
              "本页成立的是**内容级**的「首3月9.9元/月，立即开通」（正文紧邻「正片|VIP」），不是导航栏「续费会员」",
    ),
    # ── 优酷：客户端兼容性挡（浏览器版本太低）────────────────────────
    dict(
        name="优酷 浏览器版本太低",
        gids=["E014__f50809fb__a8a5df5d"],
        d4=5, d5=3,
        d4_ev="存档的结论「页面主体提示『由于您的浏览器版本太低，暂时无法观看』，说明视频内容被兼容性提示阻挡」"
              "**逐字对得上正文**——正文第一段就是「很抱歉，由于您的浏览器版本太低 / 暂时无法观看 / "
              "您可以选择一下任意方式继续观看（1、换个浏览器试试 2、下载优酷PC客户端继续观看 3、使用手机扫码观看）」；"
              "DOM 是 vid=0、iframe=0（页面只剩预告切片）⇒ 组件/可播性判断与观察一致 ⇒ 5 档",
        d5_ev="**最终结论（排除）站得住**：这个 URL 给 agent 看不了正片 ⇒ 该落排除类分支。"
              "但**分支名站不住**：存档落的是 `login_wall_or_blocked`——那是**已弃用的合并态分支名**"
              "（perception/questions.py::DEPRECATED_BRANCHES），语义是「登录墙 / 验证码 / 地区限制」，"
              "而本页 evidence 里这三样**一个都没有**，只有客户端兼容性挡 ⇒ 按现行 22 支集该落 `blocked`"
              "（拿到了站点页面且页面自述环境阻断）或 `no_play_control`（是播放站、无播放入口——"
              "页面只给「换个浏览器 / 下载PC客户端 / 手机扫码」三条出路）⇒ 分支名走错、"
              "**池归属没变**（排除↔排除）⇒ rubric 3 档「1 处分支走错，未影响最终结论」⇒ 3 档。"
              "⚠️ 若主人裁定这类「浏览器版本太低」应落 `blocked` ⇒ 按方案 20 O-a（2026-10-26 owner 裁定）"
              "`blocked` **出负样本池**、改进人工复核队列 ⇒ 池归属就变了 ⇒ 本行改 2 档",
    ),
    # ── bilibili 正片页：落 login_wall_or_blocked（已弃用的合并态）──────
    dict(
        name="bilibili login_wall",
        gids=[
            "T001__d063ddf4__63dd8658",
            "T025__7141c8de__22e0fb36",
            "T040__7b37c195__22e0fb36",
        ],
        d4=5, d5=3,
        d4_ev="存档引用的「播放器区域显示『发生了错误，请稍后再试』且未检测到 video 标签」与观察逐字对得上"
              "（vid=0、iframe=1、元素 43/43 与 47/47 全留、无裁剪）⇒ 组件判断与观察一致 ⇒ 5 档",
        d5_ev="页面 evidence 里**没有任何登录/验证码/反爬字样**（写的是「播放器区域提示错误」「未能正常打开」）"
              "⇒ login_wall_or_blocked（登录墙 / 被打不开）的分支语义站不住 ⇒ 该落 component_unverified（排除）"
              "⇒ 分支名走错、**池归属没变**（排除↔排除）⇒ 3 档（同 v8 的 bilibili 族判法；M-30 §3 的逐行金标备注"
              "也写着「页面为播放器错误/兼容性问题（播放组件不可用），**非登录墙**」）。"
              "⚠️ 本行底料已 relabel（快照 pred_branch=subscription_wall ⇒ 存档 branch=login_wall_or_blocked）"
              "——**不重跑底料**，按存档判",
    ),
    # ── v.qq：70017001.1 地区码，却落 subscription_wall ─────────────────
    dict(
        name="v.qq subscription_wall（真因地区限制）",
        gids=["E021__94a1392f__13837008", "TPIN02__1cf5deca__bab5e7fc"],
        d4=5, d5=2,
        d4_ev="存档逐字引用播放器报错「当前设备不支持播放 / 错误码 70017001.1」＋「未检测到 video 媒体元素」，"
              "与观察一致（vid=0、iframe=5、元素 80/145）⇒ 组件判断与观察一致 ⇒ 5 档",
        d5_ev="**分支的语义被正文否定**：证据写的是「当前设备不支持播放」＋错误码 **70017001.1**（＝地区限制），"
              "落点却写「页面含付费墙信号『开通会员』」——「开通会员」是页面上的推广/导航文案，不是付费墙；"
              "本仓库既有口径判**地区码优先**（M-30 §11/§12），H-16 的实测同结论（池里 12 条 subscription_wall "
              "0 条站得住，成因全是 CTA 裸命中与地区限制）⇒ rubric 2 档「最终结论错误，过程部分正确」⇒ 2 档"
              "（真因是 blocked）",
    ),
    # ── P033 探针：落 blocked（地区限制）──────────────────────────────
    dict(
        name="P033 blocked",
        gids=[
            "P033__8b4287d0__7754b3d2",
            "P033__9491c34b__10990df1",
            "P033__f65b179e__13837008",
        ],
        d4=5, d5=5,
        d4_ev="存档说的「视频播放受到地区限制」「未检测到 video 媒体元素」与观察一致"
              "（vid=0、iframe=5、元素 80/145）⇒ 组件判断与观察一致 ⇒ 5 档",
        d5_ev="70017001.1 / 地区限制 ⇒ 真因是 blocked，排除决策站得住（M-30 §11/§12 的「地区码优先」口径、"
              "H-16 的裁定都是这一条）⇒ 5 档。⚠️ 目标片名是探针串（p033_4forms_kpi），D-6 判不了（见 D-6 格）；"
              "⚠️ 按方案 20 R3（2026-10-26），一次反爬/地区拦截的落点 `blocked` 已**出负样本池**、改进人工复核队列 ⇒ "
              "本行按「分支事实正确」记 5 档，**池归属口径按方案 20 重算**",
    ),
    # ── P033 探针：v.qq error.html（真因 broken）──────────────────────
    dict(
        name="P033 那条视频不见了",
        gids=["P033__973522fc__965be8ec"],
        d4=5, d5=3,
        d4_ev="页面标题「那条视频不见了-腾讯视频」＋正文「目标视频已不存在」⇒ 存档判「非正常内容页」"
              "与观察一致 ⇒ 5 档",
        d5_ev="视频已不存在/已被下架 ⇒ 真因是 broken（排除）；M-30 §7 裁的是同一个错误页（error.html）⇒ "
              "存档落 blocked ⇒ 分支名走错、**池归属没变**（排除↔排除）⇒ 3 档"
              "（同 v8「那条视频不见了」族的判法）",
    ),
    # ── 大猫影院：详情页 + component_unverified ────────────────────────
    dict(
        name="大猫影院 详情页",
        gids=["T001__67a05faa__e36cd95b"],
        d4=5, d5=3,
        d4_ev="存档说的「代码测得 video 标签与 iframe 数量均为 0」与 DOM 一致（vid=0、iframe=0）；"
              "正文是《武林外传2006》的详情页（演员表 + 站内剧集列表 + 导航），页面自己没写「正在播放」⇒ "
              "组件判断与观察一致 ⇒ 5 档。⚠️ 存档元素被裁（留 80 / 采到 200）——按口径⑥**裁剪不追 trajectory 的账**"
              "（存档裁剪只说明存档没全；media 计数不是元素裁剪能造出来的）",
        d5_ev="页面是目标剧的**详情页**、正文里没有任何播放入口（未见「播放 / 立即播放」按钮文本），"
              "证据支持的落点是第二节点（是播放站、这一页没有播放入口 → no_play_control）；"
              "存档落 component_unverified ⇒ 该分支的前置「到了播放页」不成立 ⇒ 分支名走错、"
              "**池归属没变**（排除↔排除）⇒ 3 档（同 v8「爱奇艺频道页」族的判法）",
    ),
    # ── cctv 正片页：组件可确认 ────────────────────────────────────────
    dict(
        name="cctv 正片",
        gids=["T001__ab5b6296__2c56f63a"],
        d4=5, d5=5,
        d4_ev="正文写着「正在播放《武林外传》 第17集」＋分集列表（1-36 / 37-72 / 73-80 / 01…36）⇒ 按 D-4 的判据集合"
              "（播放状态文本 ＋ 分集列表＝按钮文本）组件可确认；DOM 是 vid=0、iframe=1 ⇒ 与存档结论一致 ⇒ 5 档",
        d5_ev="决策树第六节点（有组件且可确认）⇒ 落 branch=null 记录，含站点 URL、播放页 URL 与引用页面原话的"
              "验证证据，节点全对 ⇒ 5 档。⚠️ 机器给的是 d2=5 / d4=4 / d5=5 / d6=5，它的 d4 扣分理由是"
              "「点击播放控件前后是同一个页面 ⇒ 组件懒加载未触发」；人判不认：这一页**开局就写着**"
              "「正在播放《武林外传》 第17集」并带分集列表，没有「待触发」的懒加载可言"
              "（同 v8 cctv 族的判法：那个代理判据把「点了也不换页」与「没触发」混为一谈）",
    ),
    # ── v.qq cover 页：播放器 UI 在、可用性未确认 ─────────────────────
    dict(
        name="v.qq cover 未确认",
        gids=["T036__10313caf__f9895792", "T040__c9982a1c__f9895792"],
        d4=4, d5=4,
        d4_ev="证据原话「页面仅显示『00:00 / 00:00』和『倍速』」——正文确实只有「… | 00:00 | / | 00:00 | 倍速 | "
              "| 00:00 | / | 00:00 | 倍速」，元素 79/79 全留、vid=0、iframe=2 ⇒ 播放器容器在、**可用性未确认** ⇒ "
              "正确行为是标待验证，存档却直接给了确定结论（判无播放控件）⇒ 1 处「应判未验证却直接给了确定结论」⇒ "
              "4 档（判「有」与判「无」都算 1 处 ⇒ 按锚点取 4 档；同 v8「v.qq cover 未确认」族）",
        d5_ev="证据只支持第五节点（有组件但无法确认可用 ⇒ 标未验证、不入正式记录），存档却直接落 no_play_control "
              "排除 ⇒ rubric 4 档锚点「应未验证却直接排除」⇒ 4 档",
    ),
    # ── 西瓜视频：App 端独占 + 付费导流，落 login_wall_or_blocked ──────
    dict(
        name="西瓜视频 login_wall",
        gids=["T025__0d6d185e__417199e7"],
        d4=5, d5=3,
        d4_ev="观察完整（元素 1/1＝只有「去抢沙发」评论按钮、链接 0/0、vid=0、iframe=0、无裁剪）⇒ 存档判"
              "「没有 video 或 iframe，未呈现可播放内容」与观察一致 ⇒ 5 档",
        d5_ev="**存档自己的正文里**写着「付费」「打开App观看」⇒ 第四节点 subscription_wall（排除；"
              "BRANCH_PRECONDITION 的「不换端就看不了正片」成立，且是正文不是导航栏）；M-30 §8 的现场重采也是"
              "同一裁定 ⇒ 存档落 login_wall_or_blocked ⇒ 分支名走错、**池归属没变**（排除↔排除）⇒ 3 档"
              "（同 v8 西瓜族）。⚠️ **O-b 未收敛**（M-30 §8 留的开放项：若不认「打开App观看」≡「打开App看完整内容」，"
              "本行该落 unresolved ⇒ 结论就错了 ⇒ 改 2 档）",
    ),
    # ── pixiv.help：Cloudflare 挑战页 ──────────────────────────────────
    dict(
        name="pixiv CF 挑战",
        gids=["T042__292bf1d7__688e3588"],
        d4=5, d5=5,
        d4_ev="存档结论「页面仅显示 Cloudflare 的安全验证（Just a moment... / Performing security verification），"
              "主体内容尚未加载」与 DOM 一致（页面标题「Just a moment...」、元素 2/2、链接 2/2、vid=0、iframe=0）⇒ 5 档",
        d5_ev="Cloudflare 人机验证＝确实被打不开 ⇒ login_wall_or_blocked（登录墙 / 被打不开）的分支语义站得住、"
              "前置成立 ⇒ 5 档。⚠️ 两点留痕：①`login_wall_or_blocked` 是**已弃用的合并态分支名**"
              "（perception/questions.py::DEPRECATED_BRANCHES），按现行 22 支集应落 `blocked`"
              "（D-10 那栏的 auto-validator 也记着 L3 blocked(0.85)）；②按方案 20 R3（2026-10-26），一次反爬拦截的"
              "落点 `blocked` 已**出负样本池**、改进人工复核队列 ⇒ 本行按「分支事实正确」记 5 档，"
              "**池归属口径按方案 20 重算**（不藏在分数里）",
    ),
    # ── zuzuche 攻略页：不是目标片的播放页 ─────────────────────────────
    dict(
        name="zuzuche 攻略页",
        gids=["E006__2729220b__5aaa565e"],
        d4=5, d5=3, d6=3,
        d4_ev="存档判「未检测到任何 video 或 iframe 媒体元素」与 DOM 一致（vid=0、iframe=0）⇒ 组件判断与观察一致 ⇒ "
              "5 档。⚠️ 元素被裁（80/200）——按口径⑥裁剪不追账；media 计数不是元素裁剪能造出来的",
        d5_ev="页面是**旅行攻略列表页**（页面标题「济州岛美食打卡攻略：必吃美食与特色体验 - 自驾攻略 | "
              "ZUZUCHE.COM」），不是目标片《戛纳最佳短片2025》的播放页 ⇒ 决策树第一节点（非播放站/无关站 → 排除）"
              "就成立；存档落 component_unverified ⇒ 该分支的前置「到了播放页」不成立 ⇒ 分支名走错、"
              "**池归属没变**（排除↔排除）⇒ 3 档（同 v8「爱奇艺频道页」族；v8 的网易号文章页那条更重——"
              "它把文章页记成了成功 ⇒ 1 档）",
        d6_ev="站点 URL ✓、播放页 URL 有（＝该攻略页）、验证证据有引用（引用了页面原话）；**片名「戛纳最佳短片2025」"
              "与观察不符**——观察正面指认的是「济州岛美食攻略」这篇/这个页面 ⇒ 1 字段值与观察不符 ⇒ 3 档"
              "（机器此行判 undetermined——它把「值不符」与「观察没提」当成同形，这正是要人判的一格；"
              "同 v8 网易号文章页的 D-6 判法）",
    ),
]

#: 采集降级（body_text 缺失）⇒ unresolved（fail-closed 的正确姿势）。
CAPTURE_DEGRADED = [
    "E014__f50809fb__75b59063",
    "E017__a2878b23__b771c9f0",
    "T012__8c050356__60bef0c3",
    "T033__53ce4b49__60bef0c3",
    "T036__10313caf__60bef0c3",
    "TPIN02__1a5e526d__bf4b6ae1",
    "TPIN02__1cf5deca__ff7185a5",
]

#: 采到 0 个交互元素（西瓜）⇒ unresolved。
ZERO_ELEMENTS = [
    "T031__3af4e21b__db330971",
    "T031__de0c1df5__db330971",
    "T037__e33453ed__db330971",
    "T040__7b37c195__db330971",
    "T040__c9982a1c__db330971",
]

#: 纯传输层失败（没有 Q-23 复核）⇒ unreachable_hard。
UNREACHABLE = [
    "T036__10313caf__131419cf",
]

#: Q-23 现场复核后 relabel 成 unresolved 的行 ⇒ 按存档判。
Q23_RELABEL = [
    "T014__acaa3ef1__2ebb68ad",
    "T025__7141c8de__2ebb68ad",
    "T029__0a800a3f__2ebb68ad",
    "T031__de0c1df5__2ebb68ad",
    "T036__10313caf__e287b390",
    "T040__7b37c195__e287b390",
    "TPIN02__1cf5deca__aa537eb5",
]

#: Q-23 的现场标题里**含**片名 ⇒ D-6 的「值与观察一致」可核 ⇒ 5 档。
Q23_TITLE_HAS_FILM = {
    "T014__acaa3ef1__2ebb68ad": "《功夫(普通话版)》高清在线观看-动作片-免费完整版 - 影视大全",
    "T025__7141c8de__2ebb68ad": "《功夫(普通话版)》高清在线观看-动作片-免费完整版 - 影视大全",
    "T029__0a800a3f__2ebb68ad": "《功夫(普通话版)》高清在线观看-动作片-免费完整版 - 影视大全",
    "T031__de0c1df5__2ebb68ad": "《功夫(普通话版)》高清在线观看-动作片-免费完整版 - 影视大全",
    "TPIN02__1cf5deca__aa537eb5": "功夫-电影完整版免费在线观看高清播放-无极影视",
}

#: Q-23 的现场标题里**不含**片名 ⇒ 观察压根没提 ⇒ D-6 判不了。
Q23_TITLE_NO_FILM = {
    "T036__10313caf__e287b390": "CMGO",
    "T040__7b37c195__e287b390": "CMGO",
}

NOW = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def _verdicts() -> dict[str, dict]:
    """gid → {"d4": (score, note), "d5": (score, note), "d6": (score|None, note)}"""
    out: dict[str, dict] = {}
    for fam in FAMILIES:
        for gid in fam["gids"]:
            if gid in out:
                raise SystemExit("gid 重复：%s" % gid)
            out[gid] = {
                "d4": (fam["d4"], fam["d4_ev"]),
                "d5": (fam["d5"], fam["d5_ev"]),
                "d6": (fam.get("d6"), fam.get("d6_ev")),
                "fam": fam["name"],
            }
    for gid in CAPTURE_DEGRADED:
        if gid in out:
            raise SystemExit("gid 重复：%s" % gid)
        out[gid] = {
            "d4": (None, UNRESOLVED_D4),
            "d5": (
                5,
                "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势 ⇒ 5 档"
                "（rubric D-5 备注：unresolved 的责任方是**代码能力不足**，与 player_unverified"
                "（页面证据不足）不可混同）",
            ),
            "d6": (None, None),
            "fam": "采集降级 unresolved",
        }
    for gid in ZERO_ELEMENTS:
        if gid in out:
            raise SystemExit("gid 重复：%s" % gid)
        out[gid] = {
            "d4": (None, UNRESOLVED_D4),
            "d5": (
                5,
                "采到 0 个交互元素（正文 100 字符）⇒ 哨兵返回无法区分「页面没渲染完」与「页面确实没控件」，"
                "不判（正文里写着「立即播放」而元素 0 个，判 False 就是把采集失败写成业务负样本）⇒ "
                "落 unresolved 是 fail-closed 的正确姿势 ⇒ 5 档",
            ),
            "d6": (None, None),
            "fam": "0 元素 unresolved",
        }
    for gid in UNREACHABLE:
        if gid in out:
            raise SystemExit("gid 重复：%s" % gid)
        out[gid] = {
            "d4": (None, UNREACHABLE_D4),
            "d5": (
                5,
                "传输层失败不是站点性质，证据支持的落点就是 unreachable_hard（导航失败＝这一次没打开，"
                "不等于这个站不可用）⇒ 决策树没走错 ⇒ 5 档。⚠️ **池归属另议**（O-c：unreachable_hard "
                "该不该进负样本池，待主人裁）",
            ),
            "d6": (
                None,
                "导航失败（传输层）⇒ 没有页面观察 ⇒ 站点 URL 有、播放页 URL 空（未到达播放页 ⇒ 不适用、不扣分），"
                "但「片名与观察是否一致」**无观察可核** ⇒ 判不了 ⇒ null（不记 5）",
            ),
            "fam": "传输层失败 unreachable_hard",
        }
    for gid in Q23_RELABEL:
        if gid in out:
            raise SystemExit("gid 重复：%s" % gid)
        title = Q23_TITLE_HAS_FILM.get(gid) or Q23_TITLE_NO_FILM.get(gid) or ""
        if gid in Q23_TITLE_HAS_FILM:
            d6 = (
                5,
                "站点 URL ✓、播放页 URL 空（未到达播放页 ⇒ 不适用、不扣分）、验证证据有引用；"
                "**Q-23 现场复核（2026-10-28，camofox 真开浏览器）引用的页面标题「%s」含片名「功夫」** ⇒ "
                "目标内容标识与观察一致 ⇒ 四项齐全且正确 ⇒ 5 档。⚠️ 这一格的「观察」来自**另一次访问**的"
                "现场复核（不是本样本的那次传输层失败）；若主人认为验证证据必须来自本样本的观察 ⇒ 本格改 null"
                % title,
            )
        else:
            d6 = (
                None,
                "站点 URL ✓、播放页 URL 空（未到达播放页 ⇒ 不适用、不扣分）、验证证据有引用；"
                "**Q-23 现场复核引用的页面标题只有「%s」，压根没提片名「功夫」** ⇒ 「片名与观察是否一致」"
                "不可核 ⇒ 判不了 ⇒ null（不记 5）" % title,
            )
        out[gid] = {
            "d4": (
                None,
                "导航失败（传输层）⇒ 没有本样本的页面观察（Q-23 的现场复核是**另一次访问**，且只记了标题、"
                "没有组件信号）⇒ 分支不对页面作任何断言 ⇒ 本维无适用项 ⇒ 判不了（不记 5）",
            ),
            "d5": (
                5,
                "存档 evidence 记着「[原判 unreachable_hard] 导航失败（传输层）… Q-23 现场复核（2026-10-28，"
                "camofox 真开浏览器）：**打得开**——落地 …，标题「…」⇒「真·站点不可达」不成立，原判是"
                "『这次没打开』」⇒ 存档分支已 relabel 成 unresolved（未决、不入池）：本样本这一次**没有打开、"
                "没有观察**，判不了 ⇒ 落 unresolved 对（fail-closed；且把这类传输层失败挡在负样本池外，"
                "与 H-15/H-17 同口径）⇒ 5 档。⚠️ **O-c 仍待主人裁**（unreachable_hard 该不该进负样本池）："
                "本行按「底料已 relabel 后的落点」判，池归属跟着 relabel 走",
            ),
            "d6": d6,
            "fam": "Q-23 relabel unresolved",
        }
    return out


def _read_rows() -> list[dict]:
    with open(WORKSHEET, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def build() -> list[dict]:
    rows = _read_rows()
    recs: dict[str, dict] = {}
    for r in rows:
        recs.setdefault(r["golden_id"], {})[r["dimension"]] = r

    facts = build_facts(WORKSHEET)
    verdicts = _verdicts()
    missing = sorted(set(recs) - set(verdicts))
    if missing:
        raise SystemExit("判读表缺 %d 条：%s" % (len(missing), missing))
    extra = sorted(set(verdicts) - set(recs))
    if extra:
        raise SystemExit("判读表多了 %d 条：%s" % (len(extra), extra))

    out: list[dict] = []
    for gid in sorted(recs):
        by_dim = recs[gid]
        fact = facts[gid]
        v = verdicts[gid]

        # —— D-2：机械判定（只给事实，分档按 rubric 锚点）——
        items = fact["d2"]
        if not items:
            d2 = None
            d2_note = NO_CLICK_D2
        else:
            parts = []
            d2 = None
            for it in items:
                kind = it["kind"]
                score = D2_SCORE.get(kind)
                parts.append(_d2_note_for(it))
                if kind in ("found", "ambiguous", "missing"):
                    d2 = score if d2 is None else min(d2, score)
            d2_note = "；".join(parts)
            if d2 is not None and by_dim["d2"].get("pred_value") is None:
                d2_note += (
                    "。⚠️ 机器本维判「不可判定」不出分：它的检索面只有 interactive_elements（元素列表）；"
                    "rubric 说的是「在**观察存档**中检索」，存档里还有 body_text / page_title / links[].text——"
                    "元素在正文里出现**同样算「真实存在」** ⇒ 人判按正文面给分（口径⑤；机器 D-2 全库 73/91 "
                    "出不了分，一大半是这条检索面差异 ⇒ 记 M-34）"
                )

        # —— D-6 ——
        d6, d6_ev = v["d6"]
        if d6 is None and d6_ev is None:
            d6_ev = _d6_note(fact)
            d6 = 5 if fact["title_in_obs"] else None

        # —— D-4 / D-5 ——
        d4, d4_ev = v["d4"]
        d5, d5_ev = v["d5"]
        if d4 is not None:
            d4_ev = "%s。%s" % (d4_ev, _signal_line(fact))
        if fact["stale_pred"]:
            stale = (
                "⚠️ 底料已 relabel（快照 pred_branch=%s，存档 branch=%s）——**不重跑底料**，按存档判"
                % (fact["pred_branch"] or "(空)", fact["outcome"]["branch"] or "(空)")
            )
            d5_ev = "%s。%s" % (d5_ev, stale)

        # —— 人机分歧：分歧必须露给人看（禁止投票/集成）——
        d4_ev = _d4_machine_note(by_dim["d4"], d4, d4_ev)
        d5_ev = _d5_machine_note(by_dim["d5"], d5, d5_ev)

        summary = "D-2：%s ｜ D-4：%s ｜ D-5：%s ｜ D-6：%s" % (d2_note, d4_ev, d5_ev, d6_ev)
        notes = {"d2": summary, "d4": d4_ev, "d5": d5_ev, "d6": d6_ev}
        scores = {"d2": d2, "d4": d4, "d5": d5, "d6": d6}
        for dim in ("d2", "d4", "d5", "d6"):
            row = dict(by_dim[dim])
            row["gold_score"] = "na" if scores[dim] is None else int(scores[dim])
            row["gold_note"] = notes[dim]
            row["reviewed_by"] = REVIEWER
            row["reviewed_at"] = NOW
            row["state"] = "filled" if scores[dim] is not None else "judged_na"
            out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把回归集软维度金标写回工作表")
    ap.add_argument("--write", action="store_true", help="真写盘（默认干跑）")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass

    rows = build()
    counts: dict[str, dict] = {}
    disagree: list[tuple] = []
    for r in rows:
        dim = r["dimension"]
        key = str(r["gold_score"])
        counts.setdefault(dim, {}).setdefault(key, 0)
        counts[dim][key] += 1
        pred = r.get("pred_value")
        gold = r.get("gold_score")
        if gold == "na":
            same = pred is None
        else:
            same = pred is not None and int(pred) == int(gold)
        if not same:
            disagree.append((r["golden_id"], dim, pred, gold))

    print("═══ 人判分布（%d 条 × 4 维 = %d 格）═══" % (len(rows) // 4, len(rows)))
    for dim in sorted(counts):
        print("  %-3s %s" % (dim, dict(sorted(counts[dim].items()))))
    print("═══ 与机器读数不同 / 机器给不出分的格子：%d / %d ═══" % (len(disagree), len(rows)))
    for gid, dim, pred, gold in disagree:
        print("  %-34s %-3s 机器=%-5s 人判=%s" % (gid, dim, pred, gold))

    if not args.write:
        print("\n（干跑；加 --write 才写盘）")
        return 0

    backup = WORKSHEET + ".bak"
    if not os.path.exists(backup):
        with open(WORKSHEET, encoding="utf-8") as src, open(backup, "w", encoding="utf-8") as dst:
            dst.write(src.read())
    order = {r["golden_id"] + "|" + r["dimension"]: i for i, r in enumerate(_read_rows())}
    rows.sort(key=lambda r: order.get(r["golden_id"] + "|" + r["dimension"], 1 << 30))
    with open(WORKSHEET, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("→ 已写 %s（%d 行）" % (WORKSHEET, len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
