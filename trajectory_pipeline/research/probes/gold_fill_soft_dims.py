"""**软维度金标（D-2/D-4/D-5/D-6）v8 的填表脚本**（2026-10-24）。

**它是什么**：把 91 条校准样本 × 4 个软维度 = 364 格的 `gold_score` /
`gold_note` / `reviewed_by` 写回 `gold_worksheet.jsonl`。**判分依据是
rubric v1.2 的规格卡原文 + 每条样本的 P1 存档**（正文原话、媒体信号、
分支与证据），**不是**机器给的 `pred_value`。

**为什么脚本化**：工作表落在 `trajectory_pipeline/output/`（gitignored），
填完就看不见改了什么 ⇒ 判读必须有一份**可复算的凭据**留在仓库里。
本脚本就是那份凭据：判读表写在下面，事实底稿由
[`gold_facts.py`](gold_facts.py) 从存档重算，证据逐条引自存档。

---

## 判读口径（同步写进 `docs/07-人工复核与判读手册.md` §7.2）

### 0. 底料以存档为准

D-5 的判读对象是**存档里记的分支**（`outcomes[].branch`，按
`url == record.unit_url` 取；同一 url 有多条时再按 `site_url` 对一次）。
工作表里的 `gold_label`（D-10 金标）**只作交叉核对**，不当答案——v8 的
D-10 金标多数是 auto-validator「保持自动分支」抄回去的
（`evaluation/golden/data/url_gold_conflicts.md` 的「审核人」列），拿它当
判据等于自己判自己。快照 `pred_branch` 与存档不一致的 **7 行**
**不重跑底料**，按存档判并在 note 里写「底料已 relabel」。

### 1. 不适用 / 不可判定 ⇒ `null`，不记 5

① 分支不对页面作任何断言（`unresolved` / `unreachable_hard`）⇒ D-4 无适用项；
② 片名与观察的一致性不可核（片名在观察里检索不到，且观察没有正面指认
另一部作品）⇒ D-6 判不了。机器把「不适用」也出 5 分（`d4_scorer`：非 ④
的结论一律 `skipped` ⇒ 不扣分 ⇒ 5 分；`d6_scorer`：「值不符」与「没提」
同形 ⇒ 判不了），人判不这么算。

### 2. D-4 的「给了确定结论」不分方向

判「有」与判「无」各算 1 处 ⇒ **都按 4 档**；3 档要 2 处（锚点原文：
「2 处该给未验证却给了确定结论」）。⚠️ rubric 边界行写「直接判无排除 =
3 档」，与锚点自相矛盾 ⇒ 以锚点为准（手册 §7.2 边界③ 已写死；本条记
进台账 M-34）。

**D-4 的判据集合**（rubric D-4 卡：判据列「video 标签、player 类名、
iframe、按钮文本」）：

- `<video>` 标签：**有效**（唯一无条件有效的硬信号）。
- `player` 类名：**当前无效**（变更 E 未落地 ⇒ rubric 明写「未落地时只用
  `<video>` 一条」）。
- `iframe`：**是判据**（判据列明列）。反例只针对**广告** iframe，且 rubric
  明写「已知并已修复…**不作活跃扣分项**」⇒ 拿 iframe 判「有组件」不扣分。
  ⇒ **有 iframe ⇒ 组件是「候选」，不是「已确认」** ⇒ 该判未验证时判了
  确���（有/无）都算 1 处。
- 播放按钮 / 集数列表（按钮文本）：**有效**。

⇒ 三种典型形态：`<video>` 在 ⇒ 有组件；只有 iframe ⇒ **未验证**；页面写着
「正在播放《X》第N集」＋有分集列表 ⇒ 组件可确认。

### 3. D-5 的档位阶梯（rubric 锚点逐条落地，**按序判**）

| 档 | 条件 | 锚点原文 |
|---|---|---|
| 5 | **落点对**：分支语义与证据一致、该分支的前置事实成立、没跳步 | 5 = 决策树每步分支全对 |
| 4 | 结论方向对（该排除排除了），但 1 处**应标「未验证」却直接给了确定结论**（判有/判无/记成功/排除），或该分支的前置事实不成立 | 4 = 决策正确，1 处应「未验证」却直接记录/排除 |
| 3 | 1 处**分支名/理由走错**（落错分支、或理由与正文不符），**但池归属没变**（排除↔排除、记录↔记录、未决↔未决） | 3 = 1 处分支走错，未影响最终结论 |
| 2 | **该分支的语义被正文否定**（正文明说的是另一件事）⇒ 结论错，过程部分对 | 2 = 最终结论错误，过程部分正确 |
| 1 | 结论错**且**由错误判据链导致（把无关站/广告 video 当播放组件、把正片页当预告页、把导航栏文案当付费墙信号）；或跳步结论 | 1 = 结论错误且由错误判据链导致；或跳步结论 |

**「未决类」= `unresolved` / `player_unverified` / `trailer_suspect`**
（`executor/review_queue.py::REVIEW_BRANCHES`）：它们**不进负样本池**，
与「排除类」不是一回事。

⚠️ **本维有两个 rubric 没写死的地方，本表是本轮的判读口径，主人可否决**：
① 3 档与 2 档的分界（「分支名错」还是「语义被正文否定」）——本轮取
「**语义被正文否定 ⇒ 2 档**」，因为 v.qq 那 3 条 `subscription_wall` 正是
这种情况，机器的证据核也判 2 分（两边一致 ⇒ 这条口径站得住）；
② 机器**从不给 4 档**（`d5_scorer.score_d5` 把「前提不成立」并进 3 档），
rubric 的 4 档锚点在机器侧没有落点 ⇒ 记 M-34。

---

## 外部依据（判读时用到的既有裁定，不是本脚本的发明）

- `rubric v1.2`：`docs/设计方案/在线视频场景rubric.md` 的 D-2/D-4/D-5/D-6 规格卡。
- `docs/07-人工复核与判读手册.md` §7.1–§7.3：四维判据表、三栏取值。
- `executor/branches.py::BRANCH_PRECONDITION`：分支前置事实。
- **H-15（owner 2026-10-21）**：没观测到拦截特征 ⇒ 兜底落 `unresolved`，
  **不该进负样本池**；`component_unverified` 是 ④ 的结论。
- **H-16（owner 2026-10-21）**：池里 12 条 `subscription_wall` **0 条是真付费墙**，
  8 条 v.qq 的真因是**地区限制**（70017001.1）⇒ 该落 `blocked`；
  `subscription_wall` 的前置是「页面**明说**不给钱/不订阅/不换端就看不了正片」，
  导航栏「开通会员 / 续费会员」**不算**。
- **H-17（owner 2026-10-21）**：「点击失败 / 点击后取不到快照」记
  `component_unverified` ⇒ **假负样本**（出池）；同页守卫把「同一页两次快照」
  判成不同页（元素数 ±1 / 正文差几字）⇒ 守卫放行。
- **M-30（owner 2026-10-23，URL 级裁定）**：`golden/data/url_gold_conflicts.md`
  ——19 个多头 URL 全部定性（18 漂移可解释 / 1 不可能共存）；§1 m.ixigua、
  §2 优酷 v_show、§3 bilibili、§6 v.qq cover（DRM）、§7 v.qq error.html、
  §8 西瓜视频、§11/§12 v.qq cover（地区码优先）。

---

## 用法**（必须在仓库根跑）**

```bash
uv run python trajectory_pipeline/research/probes/gold_fill_soft_dims.py            # 干跑，只报
uv run python trajectory_pipeline/research/probes/gold_fill_soft_dims.py --write    # 写回工作表
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

WORKSHEET = os.path.join(
    REPO, "trajectory_pipeline", "output", "pipeline", "calibration", "v8", "gold_worksheet.jsonl"
)

#: 署名人。⚠️ 本轮是**AI 判读**（人只做提交与终裁）——金标的独立性因此打折，
#: 台账 M-34 里记着这条限制，请主人在提交前过一遍。
REVIEWER = "claude-ai"

#: D-2 的判档映射（rubric D-2 锚点 + 「存档裁剪 ⇒ 不可判定」）。
D2_SCORE = {
    "found": 5,
    "ambiguous": 4,
    "undetermined": None,
    "missing": 2,
    "no_target": None,
}

D2_NOTE = {
    "found": "动作引用「%s」，在观察存档（正文＋页面标题＋元素清单＋链接文字）里检索得到 ⇒ 5 档（%s）",
    "ambiguous": "动作引用「%s」，命中多个同名元素 ⇒ 1 处含糊引用 ⇒ 4 档（%s）",
    "undetermined": "动作引用「%s」检索不到，且观察有短板 ⇒ 不可判定（存档裁剪）⇒ null，不扣分也不给 5（%s）",
    "missing": "动作引用「%s」在完整观察里查不到 ⇒ 1 处引用不存在的实体 ⇒ 2 档（%s）",
    "no_target": "执行器没解出目标（target=None）⇒ 存档完整性问题，本维不扣分也不给 5 ⇒ null",
}


def _d2_note_for(item: dict) -> str:
    kind = item.get("kind") or ""
    tpl = D2_NOTE.get(kind, "动作引用「%s」⇒ %s 档（%s）")
    label = (item.get("label") or "(空)")[:44]
    note = item.get("note") or ""
    if tpl.count("%s") == 2:
        return tpl % (label, note)
    return tpl % label


# ---------------------------------------------------------------------------
# 判读表：按「族」给档位与凭据。
#   d4 / d5 ∈ 1..5 或 None（判不了）；d6 = None 表示按「片名可检索性」自动判。
#   d4_ev / d5_ev 里引的句子都是存档 outcome.evidence 或正文的**原话**。
# ---------------------------------------------------------------------------

FAMILIES: list[dict] = [
    # ── cctv《武林外传》正片页：8 条同站不同集 ──────────────────────────
    dict(
        name="cctv 正片",
        gids=[
            "T001__ab5b6296__1487aa47", "T001__ab5b6296__15a7e42d",
            "T001__ab5b6296__70d1cfec", "T001__ab5b6296__90f399f1",
            "T001__ab5b6296__a74c9670", "T001__ab5b6296__c8a5431c",
            "T001__ab5b6296__eed8ef5e", "T001__ab5b6296__f8c110ff",
        ],
        d4=5, d5=5,
        d4_ev="存档判「有组件且可确认」，判据在存档里查得到：正文写「正在播放《武林外传》 第N集」"
              "（播放状态）＋「提供分集选择 / 1-36集选集 / 分集列表」（集数列表＝rubric D-4 判据列的"
              "「按钮文本」），页面标题即《武林外传》 第N集_央视网 ⇒ 组件判断与观察一致 ⇒ 5 档",
        d5_ev="决策树第六节点（有组件且可确认）⇒ 落 branch=null 记录，含站点 URL、播放页 URL 与"
              "引用页面原话的验证证据，每步分支全对 ⇒ 5 档。⚠️ 机器在这 8 条里的 5 条给了 4 档，"
              "理由是「点击播放控件前后是同一个页面 ⇒ 组件懒加载未触发」；人判不认：这一页**开局就写着"
              "「正在播放《武林外传》 第N集」并带分集列表**，没有「待触发」的懒加载可言，点击点是播放器"
              "标题元素本身（点了也不换页）⇒ 机器的「同页⇒无新证据」是**假阳性**（该代理判据把「点了也不换页」"
              "与「没触发」混为一谈）",
    ),
    # ── 第三方站（策驰影院）────────────────────────────────────────────
    dict(
        name="策驰影院",
        gids=["T025__0d6d185e__1efaa919"],
        d4=5, d5=5,
        d4_ev="存档判「有组件」在存档里查得到：正文写「正在播放」＋提供「换线路」（按钮文本＝rubric D-4 "
              "判据列），页面标题是「播放动作片《功夫HD中字》- 在线免费观看 - 未删减 - 策驰影院」"
              "⇒ 组件判断与观察一致 ⇒ 5 档",
        d5_ev="第六节点 ⇒ 记录，节点全对 ⇒ 5 档。⚠️ 第三方影视站（策驰影院），rubric 决策树没有这种站点形态；"
              "按 G-2「禁止引入内容合规/版权考量」档位不因此变动，**是否入池请主人裁**",
    ),
    # ── 优酷《功夫》v_show：同 URL 四种落点 ────────────────────────────
    dict(
        name="优酷 v_show 记录成功",
        gids=["T040__c9982a1c__5c2c73cf"],
        d4=5, d5=5,
        d4_ev="存档判「有组件且可确认」，判据在存档里查得到：证据原话「检测到 1 个嵌入式播放器iframe」"
              "（iframe 在 rubric D-4 的判据列里；反例只针对**广告** iframe 且明写「不作活跃扣分项」）"
              "＋「未见要求开通或订阅会员后才能观看的明确提示」⇒ 组件判断与观察一致 ⇒ 5 档",
        d5_ev="第六节点（有组件且可确认、无付费墙）⇒ 落 branch=null 记录，节点全对 ⇒ 5 档。⚠️ M-30 §2 对"
              "这个 URL 的定性是「漂移可解释」——同一份正文（body_len 1432–1448、iframe=1、video=0）被读出"
              "success / no_play_control / subscription_wall 三种落点，**owner 明写「这条判据边界上有两种读法」"
              "尚未收敛**。本行按 rubric 的判据读（iframe 是判据、导航栏「续费会员」不是付费墙）判 5 档；"
              "**若主人裁定「组件未确认 ⇒ 应落未验证」，本行应改 4 档**",
    ),
    dict(
        name="优酷 v_show 无播放控件",
        gids=["T040__7b37c195__5c2c73cf", "E021__94a1392f__2b9c7618"],
        d4=4, d5=4,
        d4_ev="存档的否定断言「未提供原文为『播放』的按钮、剧集列表项或**带 ref 的播放器控件**」对外层 DOM "
              "逐字成立，但同一份观察里**有 1 个 iframe**（按判据列 iframe 是组件候选）⇒ 组件状态应是"
              "「有组件但无法确认可用」＝**未验证**，存档却直接判「无播放控件」⇒ 1 处「应判未验证却直接给"
              "确定结论」⇒ 4 档（判「有」与判「无」都算 1 处，rubric 边界行「直接判无排除＝3 档」与锚点矛盾，"
              "按锚点/手册 §7.2 边界③ 取 4 档）",
        d5_ev="证据只支持第五节点（有组件但无法确认可用 ⇒ 标未验证、不入正式记录），存档却直接落 "
              "no_play_control 排除 ⇒ rubric 4 档锚点「应未验证却直接排除」⇒ 4 档。⚠️ M-30 §2：owner 未"
              "收敛本页读法（success / no_play_control / subscription_wall 三读）⇒ **若主人裁定本页就是"
              "「有播放入口」，本行应改 2 档**（本该记成排除）",
    ),
    dict(
        name="优酷 v_show 导航栏续费会员",
        gids=["T014__acaa3ef1__5c2c73cf"],
        d4=4, d5=4,
        d4_ev="同一份 v_show 页：vid=0、外层 DOM 无播放控件、**有 1 个 iframe** ⇒ 组件是「候选」不是「已确认」；"
              "存档给的却是确定结论（判「需续费会员才能观看」＝不可播放）⇒ 应判未验证却给了确定结论 ⇒ 4 档",
        d5_ev="两处错：①「续费会员」在**顶部导航**——正文原话「首页 / 繁星 / 电视剧 / 动漫 / 分类 / **续费会员** / "
              "历史 / 客户端 / 登录」，BRANCH_PRECONDITION 明写「纯续费营销」「导航栏文案」不算付费墙，H-16 实测 "
              "12 条 subscription_wall 0 条站得住，rubric v1.2 边界也写「站点页导航栏里的『开通会员』是导航文案，"
              "不是付费墙」⇒ 分支名错；②组件只到「候选」却给了确定结论 ⇒ 诚实落点是未验证（node 5）⇒ "
              "rubric 4 档锚点「应未验证却直接排除」⇒ **4 档**。⚠️ M-30 §2 明写这条判据边界「有两种读法」、"
              "owner 未收敛 ⇒ **若主人裁定导航栏「续费会员」算付费墙信号，本行应改 5 档**",
    ),
    # ── 网易号文章页：组件信号真实、站点类型错 ───────────────────────────
    dict(
        name="网易号文章页",
        gids=["E006__2729220b__e9b9542b"],
        d4=5, d5=1, d6=3,
        d4_ev="存档说的「存在 <video>/<audio> 标签 ×6（iframe=10）」与末步观察一致（vid=6、ifr=10）⇒ "
              "组件判断与 DOM 一致；rubric 反例「仅含广告 iframe ⇒ 判有组件」明写不作活跃扣分项 ⇒ 5 档",
        d5_ev="页面是网易号文章「济州岛美食攻略：从东门市场砍价到西归浦家常菜」（页面标题即该文章），"
              "**根本不是目标片《戛纳最佳短片2025》的播放页**；决策树第一节点「非播放站/无关站 → 排除」"
              "就成立，存档却凭文章里的 6 个 <video> 标签直接跳到第六节点记成功 ⇒ 结论错、且判据链建在错事实上"
              "（把文章页当播放页、把文中的视频当播放器）⇒ **1 档**",
        d6_ev="站点 URL ✓、播放页 URL 有（＝该文章页）、验证证据有引用（引用了 DOM 事实）；**片名"
              "「戛纳最佳短片2025」与观察不符**（页面标题/正文都是「济州岛美食攻略」文章）⇒ 1 字段值与观察不符 "
              "⇒ 3 档（机器此行判 undetermined——它把「值不符」与「观察没提」当成同形，这正是要人判的一格）",
    ),
    # ── 爱奇艺频道页：是播放站，但这一页不是播放页 ─────────────────────
    dict(
        name="爱奇艺频道页",
        gids=["E005__e35b7455__feb5257a", "T001__d063ddf4__921485c8"],
        d4=5, d5=3,
        d4_ev="存档判「媒体元素数量为 0，并非实际播放页」与观察一致（vid=0；页面是综艺/电视剧频道列表页，"
              "正文是节目卡片与筛选栏）⇒ 组件判断与观察一致 ⇒ 5 档",
        d5_ev="证据支持的落点是第二节点（是播放站、这一页没有播放入口 → no_play_control），存档落的是 "
              "component_unverified ⇒ 该分支的前置「到了播放页」不成立（存档自己的证据都写着「并非实际播放页」）"
              "⇒ 分支名走错、**池归属（排除）没变** ⇒ 3 档",
    ),
    # ── 芒果详情页：有「立即播放」入口但播放器未加载 ────────────────────
    dict(
        name="芒果详情页",
        gids=["E017__a2878b23__9a9dc9bf", "E021__94a1392f__2ab12498"],
        d4=4, d5=4,
        d4_ev="页面只有「立即播放」入口、vid=0/ifr=0（播放器未加载）⇒ 这正是 rubric D-4 边界「组件懒加载需触发 → "
              "标记待验证/触发后再判」那一档；存档直接判「未发现实际播放器 / 未呈现可播放的媒体播放器」⇒ "
              "1 处「应判未验证却直接给确定结论」⇒ **4 档**（rubric 边界行写「直接判无排除 = 3 档」与锚点「2 处」"
              "自相矛盾，此处按锚点/手册 §7.2 边界③ 取 4 档）",
        d5_ev="证据只支持第五节点（标未验证），存档却直接落 component_unverified 排除 ⇒ rubric 4 档锚点"
              "「应未验证却直接排除」⇒ 4 档",
    ),
    # ── 乐视详情页：落 player_unverified（第五节点）──────────────────────
    dict(
        name="乐视 player_unverified",
        gids=["E017__a2878b23__1e5490ea"],
        d4=5, d5=5,
        d4_ev="存档判「未验证」与观察一致：证据原话「页面虽显示『立即播放』，但代码测得 video 标签为 0，仅有 2 个 "
              "iframe，且无明确可播放媒体或付费拦截证据」⇒ 组件确实无法确认可用 ⇒ 5 档",
        d5_ev="第五节点（有组件但无法确认可用）⇒ 标记未验证、不入正式记录（is_negative_sample=False），"
              "节点全对 ⇒ 5 档",
    ),
    # ── v.qq cover 页：播放器 UI 在、状态未确认 ──────────────────────────
    dict(
        name="v.qq cover 未确认",
        gids=["T018__ad4a6065__f9895792", "T037__e33453ed__f9895792", "T031__de0c1df5__f9895792"],
        d4=4, d5=4,
        d4_ev="证据原话「页面仅显示『00:00/00:00』和『倍速』等播放器文字，但未提供播放器或播放按钮的 ref」"
              "（T031 那条是「两个播放器时间均停留在 00:00，且无可操作控件」）⇒ 播放器容器在（ifr=1/2）、"
              "**可用性未确认** ⇒ 正确行为是标待验证，存档却直接给了确定结论（判无 / 判无法正常播放）⇒ 4 档",
        d5_ev="证据只支持第五节点（有组件但无法确认可用 ⇒ 标未验证、不入正式记录），存档却直接落排除"
              "（no_play_control / component_unverified）⇒ rubric 4 档锚点「应未验证却直接排除」⇒ 4 档。"
              "⚠️ T031__de0c1df5 这条尤其要注意：它的元素是 0/0（一个交互元素都没有）——落 component_unverified "
              "（排除）而不是未验证，等于把**采集不到**写成了业务负样本",
    ),
    # ── 腾讯《中国好声音》专题页：4 个 iframe 未核 ⇒ 否定断言撑不住 ────
    dict(
        name="腾讯专题页",
        gids=["E005__e35b7455__3bd50dfb"],
        d4=4, d5=4,
        d4_ev="页面标题「《中国好声音》高清在线观看_腾讯视频」、正文有《中国好声音》与各国好声音入口，"
              "**有 4 个 iframe**（性质没核），元素被裁剪（留 80 / 采到 200）、链接被裁剪（50 / 197）⇒ 存档的"
              "**否定断言**「现有可见控件中未出现剧集列表项、播放按钮或播放器控件」在存档里撑不住；"
              "正确行为是标待验证，存档却直接给确定结论 ⇒ 4 档",
        d5_ev="「该站无播放入口」不可核（4 个 iframe 是不是播放器没查、元素被裁）⇒ 证据只支持未决，存档却直接落 "
              "no_play_control 排除 ⇒ rubric 4 档锚点「应未验证却直接排除」⇒ 4 档。⚠️ **这一行需要现场重采才能"
              "定论**（4 个 iframe 是不是播放器）；站点现状也可能已漂移 ⇒ 记进台账 M-34 的待办",
    ),
    # ── 搜狗电影：页面自己写「本片播放源（暂无）」──────────────────────
    dict(
        name="搜狗电影",
        gids=["T024__b54092ee__6ff76654"],
        d4=5, d5=5,
        d4_ev="页面自己写着「本片播放源（暂无）」，vid=0/ifr=0、元素里也没有播放控件 ⇒ 存档判「无可播放控件」"
              "与观察一致 ⇒ 5 档",
        d5_ev="第二节点（是播放站，无播放入口）⇒ no_play_control，前置成立、每步分支全对 ⇒ 5 档",
    ),
    # ── 西瓜视频：App 端独占 + 付费导流 ────────────────────────────────
    dict(
        name="西瓜视频",
        gids=["TPIN02__1a5e526d__417199e7", "TPIN02__1cf5deca__417199e7"],
        d4=5, d5=3,
        d4_ev="观察完整（元素 1/1＝只有「去抢沙发」评论按钮，vid=0、ifr=0、链接 0/0）⇒ 存档判「没有可开始播放的"
              "控件 / 没有视频播放器或 iframe」与观察一致 ⇒ 5 档",
        d5_ev="**存档自己的正文里**写着「打开西瓜视频，看完整高清内容」「付费」「打开App观看」⇒ 第四节点 "
              "subscription_wall（排除；BRANCH_PRECONDITION 的「不换端就看不了正片」成立，且是正文不是导航栏）；"
              "M-30 §8 的现场重采也是同一裁定（页面事实＝App 端独占＋付费导流 ⇒ 语义落点 subscription_wall，"
              "只是机器词表认不出「打开App看完整内容」）⇒ 存档落 no_play_control / login_wall_or_blocked ⇒ "
              "分支名走错、**池归属（排除）没变** ⇒ 3 档。⚠️ M-30 §8 留了一个开放项：若不认「打开App观看」与"
              "「打开App看完整内容」等价，两行都该落 unresolved ⇒ 那时本行结论就错了（应改 2 档）",
    ),
    # ── cctv 少儿台：正片页被判成「只有预告」──────────────────────────
    dict(
        name="cctv 少儿台 trailer_only",
        gids=["T001__d063ddf4__106df089"],
        d4=4, d5=1,
        d4_ev="页面标题「《武林外传》 第1集_青少台_央视网(cctv.com)」，正文写着「《武林外传》 第1集 / 往期 / 片段 / "
              "视频简介 / 节目信息 / **本集主要内容：郭女侠怒砸同福店，佟掌柜妙点迷路人…（《武林外传》 第1集）**」"
              "⇒ 这是**正片页**（1 个 iframe）；而存档只凭词表抓到的 1 个「片段」控件就判「只有预告」⇒ "
              "组件状态未确认，正确行为是标待验证 ⇒ 4 档",
        d5_ev="决策树第一节点「非播放站」不成立、正片页也不是「只有预告」：页面自己写着第 1 集的「本集主要内容」，"
              "存档只凭词表的 1 个「片段」控件就落 trailer_only 排除 ⇒ **结论错、且判据链建在错事实上**"
              "（把正片页的「片段」页签当成预告、把整页当成只有预告）⇒ 1 档",
    ),
    # ── 优酷：可见控件全是预告/花絮（判不准）──────────────────────────
    dict(
        name="优酷 trailer_suspect",
        gids=["T024__b54092ee__cac9ea9f"],
        d4=5, d5=3,
        d4_ev="元素 48/48 全留，页面可见控件就是「预告 即便生活跌入谷底…」「预告 天使爱美丽 预告片」「天使爱美丽花絮」"
              "等周边 ⇒ 存档「疑似预告、词表判不准」与观察一致 ⇒ 5 档",
        d5_ev="rubric 决策树没有 trailer_suspect 节点；「判不准」的正确落点是第五节点（标未验证）——"
              "trailer_suspect 与 unresolved/player_unverified 一样**不是负样本**（REVIEW_BRANCHES，"
              "is_negative_sample=False）⇒ 结论（不入正式记录）没变、只是分支名不在 rubric 树上 ⇒ 3 档",
    ),
    # ── bilibili 正片页（会员版）：分支名错，结论不变 ───────────────────
    dict(
        name="bilibili 会员正片页",
        gids=["T028__d76e1085__22e0fb36"],
        d4=5, d5=3,
        d4_ev="存档说的「未检测到 video 媒体元素」与观察一致（vid=0、ifr=1、元素 47/47 全留）⇒ 5 档",
        d5_ev="正片入口明写「会员 粤语版」⇒ 第四节点 subscription_wall（排除）；M-30 §3 说得很直白："
              "「『要钱』与『播放器组件没验出来』是同一页面的两层事实，不是互斥」，而该 URL 上唯一的**人工**"
              "金标（4 行 manual-review）全是 subscription_wall ⇒ 存档落 component_unverified ⇒ 分支名走错、"
              "**池归属（排除）没变** ⇒ 3 档",
    ),
    # ── bilibili 正片页：落 login_wall_or_blocked（前置不成立）─────────
    dict(
        name="bilibili login_wall",
        gids=[
            "E014__f50809fb__cf884eaa", "T014__acaa3ef1__22e0fb36",
            "T029__0a800a3f__22e0fb36", "T031__de0c1df5__22e0fb36",
            "T040__c9982a1c__22e0fb36",
        ],
        d4=5, d5=3,
        d4_ev="存档引用的「播放器区域显示『发生了错误，请稍后再试』且未检测到 video 标签」与观察逐字对得上"
              "（vid=0、ifr=1、元素 47/47 全留）⇒ 5 档",
        d5_ev="页面 evidence 里**没有任何登录/验证码/反爬字样**（写的是「未加载出目标内容」「播放器区域提示错误」"
              "「当前国语会员版视频未加载」）⇒ login_wall_or_blocked 的分支语义（登录墙/被打不开）站不住；"
              "M-30 §3 的逐行金标备注也写着「页面为播放器错误/兼容性问题（播放组件不可用），**非登录墙**」"
              "⇒ 该落 component_unverified（排除）；E014 那条还带「国语会员版」信号 ⇒ subscription_wall 也是排除 ⇒ "
              "两种读法都是排除 ⇒ 分支名走错、**池归属没变** ⇒ 3 档",
    ),
    # ── v.qq 正片页：错误码 70017001.1 = 地区限制，不是付费墙 ───────────
    dict(
        name="v.qq subscription_wall",
        gids=["E014__f50809fb__30062587", "T024__b54092ee__10990df1", "TPIN02__1cf5deca__3dcdbc14"],
        d4=5, d5=2,
        d4_ev="存档引用的「未检测到 video 媒体元素」「当前设备不支持播放」与正文逐字一致（vid=0、ifr=5）⇒ "
              "组件判断与观察一致 ⇒ 5 档",
        d5_ev="**分支的语义被正文否定**：证据写「当前设备不支持播放」＋错误码 **70017001.1**（＝地区限制），"
              "落点却写「页面含付费墙信号『开通会员』」——「开通会员」是页面上的推广/导航文案，不是付费墙；"
              "M-30 §11/§12 明写「本仓库既有口径判**地区码优先**」，H-16 的裁定同口径（12 条 subscription_wall "
              "0 条真付费墙）⇒ 该分支声称的「要钱」在正文里被否定 ⇒ rubric 2 档「最终结论错误，过程部分正确」⇒ 2 档"
              "（真因是 blocked）",
    ),
    # ── v.qq cover 页：落 blocked（地区限制，前置成立）──────────────────
    dict(
        name="v.qq blocked",
        gids=[
            "E021__129c3fae__13837008", "E023__a6a2ad3e__41d4e5a5",
            "P033__88d78b22__41d4e5a5", "T024__6240da8e__10990df1",
            "T042__df2ee261__a7274efb",
        ],
        d4=5, d5=5,
        d4_ev="存档逐字引用播放器报错「当前设备不支持播放 / 错误码：70017001.1」⇒ 组件判断与观察一致 ⇒ 5 档",
        d5_ev="70017001.1 = **地区限制** ⇒ 真因是 blocked，该分支的前置事实成立、排除决策站得住"
              "（M-30 §11/§12 的「地区码优先」口径、H-16 的裁定都是这一条）⇒ 5 档。⚠️ P033 那条的目标片名是"
              "探针串（p033_4forms_kpi），D-6 判不了（见 D-6 格）",
    ),
    # ── v.qq cover 页：错落 login_wall_or_blocked（真因不是登录墙）──────
    dict(
        name="v.qq login_wall",
        gids=["T031__3af4e21b__f9895792"],
        d4=5, d5=3,
        d4_ev="存档引用的「当前设备不支持播放 / 错误码 70017001.1」与正文逐字一致 ⇒ 组件判断与观察一致 ⇒ 5 档",
        d5_ev="证据里**没有登录墙的任何特征**（无登录框/验证码/未登录字样）⇒ login_wall_or_blocked 的语义"
              "不成立；真因是地区限制（blocked）或 DRM（M-30 §6 的现场重采读到的是 **70013080.14**＝DRM 加密播放"
              "不可用 ⇒ 应落 component_unverified）——两种读法都是**排除** ⇒ 分支名走错、**池归属没变** ⇒ 3 档。"
              "⚠️ M-30 §6 的裁定是 2026-10-23 的现场重采，比存档的 70017001.1 读数新 ⇒ 本行按「非登录墙」记",
    ),
    # ── 腾讯「那条视频不见了」：真因 broken ───────────────────────────
    dict(
        name="那条视频不见了",
        gids=["T014__acaa3ef1__965be8ec", "T028__d76e1085__965be8ec", "T029__0a800a3f__965be8ec"],
        d4=5, d5=3,
        d4_ev="页面标题「那条视频不见了-腾讯视频」＋正文「视频已不存在」⇒ 存档判「非正常内容页」与观察一致 ⇒ 5 档",
        d5_ev="视频不存在 ⇒ broken（排除）；M-30 §7 裁的是同一个错误页（error.html），「同 URL 不同次落不同内容"
              "是站点行为」，这几行的 D-10 金标就是 broken ⇒ 存档落 login_wall_or_blocked ⇒ 分支名走错、"
              "**池归属（排除）没变** ⇒ 3 档",
    ),
    # ── 404 / 500：真因 broken ─────────────────────────────────────────
    dict(
        name="404/500",
        gids=["T025__0d6d185e__2d99fba8", "T033__53ce4b49__db330971"],
        d4=5, d5=3,
        d4_ev="页面标题「404 Not Found」/ 正文只有「Internal Server Error」⇒ 存档判「不可达页面」与观察一致 ⇒ 5 档",
        d5_ev="404 / 服务错误 ⇒ broken（排除）；M-30 §1 裁的是同一个 m.ixigua URL：T033__53ce4b49 那条"
              "「正文 21 字符只有 Internal Server Error」归 broken ⇒ 存档落 login_wall_or_blocked ⇒ 分支名走错、"
              "**池归属（排除）没变** ⇒ 3 档",
    ),
]

#: 「unresolved」族（落 unresolved = 未决、不入池）：按「落点对不对」分档。
#: ①/② 层（site.find_play_control / site.is_reachable）＝ 代码**没走到**页面级
#: 判断，fail-closed 是正确姿势（rubric D-5 备注：unresolved 的责任方是代码能力
#: 不足；「该站本可判却未判」是 **D-3** 的扣分项，不是 D-5）。
UNRESOLVED: dict[str, tuple[int, str]] = {
    # —— ①/② 层：采集/模型能力不足，fail-closed 落点对（5 档）——
    "E001__57bbf995__6e8d5d31": (5, "模型未回传 ref（证据原话「模型未回传 ref，但词表在本页有非预告播放控件」），"
                                   "问答点是 site.find_play_control ⇒ 代码**没走到**页面级判断，落 unresolved 是 "
                                   "fail-closed 的正确姿势；rubric 备注：unresolved 的责任方是**代码能力不足**，"
                                   "与 player_unverified（页面证据不足）不可混同。（本行页面其实是 v.qq《让子弹飞》"
                                   "封面页＋地区码 70017001.1，「该站本可判却未判」是 **D-3** 的扣分项，不在 D-5 记）"),
    "E005__e35b7455__de84d997": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "E006__2729220b__55573b12": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "E014__f50809fb__7493f4e0": (5, "采到 0 个交互元素（正文 110 字符）⇒ 证据原话「哨兵返回无法区分『页面没渲染完』与"
                                   "『页面确实没控件』，不判（正文里写着『立即播放』而元素 0 个，判 False 就是把采集失败"
                                   "写成业务负样本）」⇒ 落点对"),
    "E014__f50809fb__dec0b09f": (5, "[记账降级] 本该记 component_unverified，但点击播放控件前后是同一个页面"
                                   "（le.com/movie/10075153.html 详情页）⇒ 「到了播放页」这个前置事实不成立 ⇒ "
                                   "降级为 unresolved 正是纪律要求的（贴标签前核前置事实）⇒ 落点对"),
    "E021__94a1392f__d41fe4ac": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "E023__4d175424__9d49aca1": (5, "[记账降级] 本该记 component_unverified，但点击播放控件前后是同一个页面"
                                   "（luketv.tv/zh/title/breaking-bad/ 详情页）⇒ 前置事实不成立 ⇒ 降级为 unresolved 正确"),
    "E023__4d175424__a7f0eaaf": (5, "站点遍历超时（>90s，source=rule）⇒ 没拿到可用观察，落 unresolved 正确"),
    "T011__443c5ec0__60bef0c3": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "T014__acaa3ef1__60bef0c3": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "T014__acaa3ef1__db330971": (5, "采到 0 个交互元素（正文 100 字符）⇒ 同 E014__7493f4e0：落 unresolved 正确"),
    "T018__ad4a6065__60bef0c3": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "T025__0d6d185e__bf4b6ae1": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "T025__7141c8de__60bef0c3": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "T028__d76e1085__60bef0c3": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "T028__d76e1085__db330971": (5, "采到 0 个交互元素（正文 100 字符）⇒ 落 unresolved 正确"),
    "T029__0a800a3f__60bef0c3": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "T031__3af4e21b__60bef0c3": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "T040__c9982a1c__60bef0c3": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "TPIN02__1a5e526d__ad5971d1": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "TPIN02__1a5e526d__fab8371e": (5, "[记账降级] 本该记 component_unverified，但点击播放控件前后是同一个页面"
                                    "（ljy200471.com/y/35918.html 详情页）⇒ 前置事实不成立 ⇒ 降级为 unresolved 正确"),
    "TPIN02__1cf5deca__87597866": (5, "页面撞 Cloudflare「Just a moment...」安全验证、正文被拦截 ⇒ 判不出来，"
                                    "落 unresolved 正确（问答点 site.is_reachable）。⚠️ 存档自带说明：同一 URL 在 camoufox "
                                    "生产批读成可播放页（6 份存档，3 个播放器 iframe）⇒ 这条是**旧执行层 obscura** 的读数，"
                                    "对当前执行层不成立；按「按访问」口径，各判各那次"),
    "TPIN02__1cf5deca__ad5971d1": (5, "采集降级（body_text 缺失）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    "TPIN02__1cf5deca__fb7b373d": (5, "采集降级（body_mojibake）⇒ 证据不完整，落 unresolved 是 fail-closed 的正确姿势"),
    # —— ④ 层：代码做了判断、但落点错（2 档）——
    "T012__8c050356__22e0fb36": (2, "URL 就是播放页（bangumi/play/ep313572）、正文明写「会员 粤语版」＋「发生了错误，"
                                   "请稍后再试」⇒ 证据支持的落点是第四节点 subscription_wall（**排除**，进负样本池）；"
                                   "M-30 §3 里这一行的 D-10 金标正是 manual-review（2026-10-12）人工写的 "
                                   "subscription_wall ⇒ 存档却落 unresolved（未决、不入池）⇒ **池归属错、结论错**。"
                                   "⚠️ 页面事实读对了、节点也选对了（component_unverified），错在**同页守卫误伤**："
                                   "这次点击前就已经在播放页上（reached_play_page=True、play_page_url 就是本页），"
                                   "守卫把「点击没换页」当成「没到播放页」⇒ 2 档（过程部分对，结论错）"),
}

#: 「unreachable_hard」族（传输层失败）：D-4 判不了、D-5 落点对、D-6 片名不可核。
UNREACHABLE = [
    "E017__a2878b23__95f0a368", "T001__ab5b6296__9d610347", "T001__d063ddf4__3c02c4df",
    "T011__443c5ec0__131419cf", "T018__ad4a6065__131419cf", "T018__ad4a6065__2ebb68ad",
    "T024__b54092ee__f68fdcc9", "T025__7141c8de__131419cf", "T029__0a800a3f__db330971",
    "T031__3af4e21b__2ebb68ad", "T031__de0c1df5__131419cf", "T033__53ce4b49__131419cf",
    "T033__53ce4b49__2ebb68ad", "T037__e33453ed__131419cf", "T037__e33453ed__2ebb68ad",
    "T040__7b37c195__131419cf", "T040__c9982a1c__2ebb68ad", "TPIN02__1a5e526d__56ae30f8",
]

UNREACHABLE_D4 = (
    "导航失败（传输层）⇒ 没有页面观察 ⇒ 分支不对页面作任何断言 ⇒ 本维无适用项 ⇒ 判不了（不记 5）"
)
UNREACHABLE_D5 = (
    "传输层失败不是站点性质，证据支持的落点就是 unreachable_hard（导航失败＝这一次没打开，不等于这个站不可用）"
    "⇒ 决策树没走错 ⇒ 5 档。⚠️ **池归属另议**：H-15/H-17 的口径是「采集失败不该进负样本池」，"
    "unreachable_hard 不在 REVIEW_BRANCHES 里（本行按「分支事实正确」记 5 档，要不要出池请主人裁）"
)
UNRESOLVED_D4 = (
    "落 unresolved ⇒ 不对页面作任何断言（代码能力不足，不是页面证据不足）"
    "⇒ 本维无适用项 ⇒ 判不了（不记 5）"
)
UNRESOLVED_D6 = None  # 未决行**也有记录**（outcome 存在）⇒ 按「片名能否检索到」自动判
UNREACHABLE_D6 = (
    "导航失败（传输层）⇒ 没有页面观察 ⇒ 站点 URL 有、播放页 URL 空（未到达播放页 ⇒ 不适用、不扣分），"
    "但「片名与观察是否一致」**无观察可核** ⇒ 判不了 ⇒ null（不记 5）"
)

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
    for gid, (score, note) in UNRESOLVED.items():
        if gid in out:
            raise SystemExit("gid 重复：%s" % gid)
        out[gid] = {
            "d4": (None, UNRESOLVED_D4),
            "d5": (score, note),
            "d6": (None, UNRESOLVED_D6),
            "fam": "unresolved",
        }
    for gid in UNREACHABLE:
        if gid in out:
            raise SystemExit("gid 重复：%s" % gid)
        out[gid] = {
            "d4": (None, UNREACHABLE_D4),
            "d5": (5, UNREACHABLE_D5),
            "d6": (None, UNREACHABLE_D6),
            "fam": "unreachable",
        }
    return out
    for gid in UNREACHABLE:
        if gid in out:
            raise SystemExit("gid 重复：%s" % gid)
        out[gid] = {
            "d4": (None, UNREACHABLE_D4),
            "d5": (5, UNREACHABLE_D5),
            "d6": (None, UNREACHABLE_D6),
            "fam": "unreachable",
        }
    return out


def _read_rows() -> list[dict]:
    with open(WORKSHEET, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _signal_line(fact: dict) -> str:
    """末步观察的媒体信号（从存档重算，不许手抄）。"""
    m = fact["media_last"]
    return (
        "存档信号：vid=%s、iframe=%s、元素 %s/%s、链接 %s/%s、截断=%s、降级=%s；页面标题「%s」" % (
            m["video_tag_count"], m["iframe_count"],
            m["elements_kept"], m["elements_total"],
            m["links_kept"], m["links_total"],
            m["truncated"], m["degraded"], m["page_title"] or "(空)",
        )
    )


def _d6_note(fact: dict) -> str:
    rec = fact["record"]
    out = fact["outcome"]
    site = "✓" if str(rec.get("site_url") or "") else "缺"
    pp = str(out.get("play_page_url") or rec.get("play_page_url") or "")
    pp_txt = "有" if pp else "空（未到达播放页 ⇒ 不适用、不扣分）"
    head = "站点 URL %s、播放页 URL %s、验证证据有引用（引用了页面原话）" % (site, pp_txt)
    title = fact["title"]
    if fact["title_in_obs"]:
        return "%s；片名「%s」在观察里检索得到 ⇒ 四项齐全正确 ⇒ 5 档" % (head, title)
    return (
        "%s；片名「%s」在观察里检索不到 ⇒ 与观察的一致性不可核"
        "（机器同此判 undetermined：「值不符」与「观察压根没提」在存档里同形，要人判）"
        "⇒ 判不了 ⇒ null（不记 5）" % (head, title)
    )


def _cell(value):
    return "na" if value is None else int(value)


def _d4_machine_note(row: dict, human, note: str) -> str:
    """机器 D-4 的读数要带理由露出来（它有白送 5 分的问题）。"""
    pred = row.get("pred_value")
    if human is None:
        return note
    if pred is None:
        return note + (
            "。⚠️ 机器本维不出分（理由见本行 pred_reasons）——人判给 %d 档" % human
        )
    if int(pred) == int(human):
        return note + "。（人机一致）"
    return note + (
        "。⚠️ 机器本维给 %s 分、人判 %d 分：机器的 D-4 **只管判断点 ④（player.ok）**——"
        "非 ④ 的结论一律「skipped ⇒ 不扣分 ⇒ 5 分」（`evaluation/scorers/d4_scorer.py`）"
        "⇒ 机器这个 5 分是**白送**的，不构成对本行的判断 ⇒ 拿它算一致率会虚高（记 M-34）"
        % (pred, human)
    )


def _d5_machine_note(row: dict, human, note: str) -> str:
    """机器 D-5 从不产出 4 档——人判 4 档时要说清这个口径差。"""
    pred = row.get("pred_value")
    if human is None or pred is None:
        return note
    if int(pred) == int(human):
        return note + "。（人机一致）"
    if human == 4:
        return note + (
            "。⚠️ 机器本维给 %s 分、人判 4 档：机器的 `d5_scorer.score_d5` **从不产出 4 档**"
            "（它把「前提不成立」并进 3 档、把「证据冲突」给 2 档）⇒ rubric 的 4 档锚点"
            "在机器侧没有落点（记 M-34）" % pred
        )
    if human == 2 and int(pred) == 2:
        return note + "。（人机一致）"
    return note + "。⚠️ 机器本维给 %s 分、人判 %d 分（机器理由见本行 pred_reasons）" % (pred, human)


def build() -> list[dict]:
    rows = _read_rows()
    recs: dict[str, dict] = {}
    for r in rows:
        recs.setdefault(r["golden_id"], {})[r["dimension"]] = r

    facts = build_facts()
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
            d2_note = (
                "本单元没有模型可见的 click 动作 ⇒ 无实体引用可校验 ⇒ 本维无适用项 ⇒ 判不了（不记 5）。"
                "（机器同此判 undetermined：「没有可校验项」不等于「全部可找到」——"
                "见 code_scorer.score_d2 的注释）"
            )
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
            if d2 is not None:
                # 机器给不出分时，把「检索面」这条口径差写清楚（否则读报告的人
                # 会以为人判与机器是两种能力差异，其实只是查的面不同）。
                if by_dim["d2"].get("pred_value") is None:
                    d2_note += (
                        "。⚠️ 机器本维判「不可判定」不出分：它的检索面只有 interactive_elements"
                        "（元素列表），本条的元素因裁剪（80/200）没进列表、于是被判查不到；"
                        "但 rubric 说的是「在**观察存档**中检索」，存档里还有 body_text / page_title /"
                        " links[].text——元素在正文里出现**同样算「真实存在」** ⇒ 人判按正文面给分。"
                        "（机器 D-2 全库 73/91 出不了分，一大半是这条检索面差异 ⇒ 记 M-34）"
                    )

        # —— D-6 ——
        # 族里给了专用 note 的（网易号的 3 档、unresolved/unreachable 的 null）直接用；
        # 其余按「片名能否在观察里检索到」自动判。
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
            row["gold_score"] = _cell(scores[dim])
            row["gold_note"] = notes[dim]
            row["reviewed_by"] = REVIEWER
            row["reviewed_at"] = NOW
            row["state"] = "filled" if scores[dim] is not None else "judged_na"
            out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 v8 软维度金标写回工作表")
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

    print("═══ 人判分布 ═══")
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
