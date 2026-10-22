"""**人判两条的工作表生成器**——负样本成因 / 动作可移植性。

**为什么要有这个脚本**（2026-10-21，批次第 5 项）

§6.1 的判据⑤（负样本成因）与判据⑥（动作可移植性）**机器判不了**：
`compare-drivers` 只能报 `needs_manual`。人判要人看，而「人要看的东西」
必须**先摊开成一张能逐行填的表**——否则人判会退化成「看一眼结论就点头」。

本脚本**只读、不联网、不改任何数据**，产出两张表：

**表一 · 负样本成因**
    每行 = B 面一条 `is_negative_sample=True` 的 outcome。
    列 = 该条的**判断依据**（分支 / question / source / evidence /
    page_quotes / 观察事实）+ **A 面同落地 URL 的对照读数** +
    **给人工填的空格**（`成因` / `是否负样本` / `人工依据`）。

    ⚠️ **成因词表是封闭集**（见 :data:`CAUSES`）：**站点侧填分支名**
    （``not_play_site`` / ``login_required`` / ``blocked`` /
    ``subscription_wall`` …），**我们侧填动作码**（``not_collected`` /
    ``nav_interrupted`` / ``player_unverified``），
    **「不是负样本」填 ``success``**（资源存在，页面在剧集/资源页），
    「说不清是哪一类」要写成 `?`，**不许自由发挥**——
    自由发挥的成因没法汇总，汇总不了就没法定口径
    （口径正是判据⑤ 要产出的东西）。

    ⚠️ **旧口径 W1–W4 已作废**（2026-10-21）：W1 把登录墙与反爬合在一处，
    而付费墙/无播放控件/组件不可验证/只有预告/导航失败**全都没有位置**。
    新词表见 :data:`SITE_SIDE_CAUSES` / :data:`OUR_SIDE_CAUSES` /
    :data:`NOT_NEGATIVE_CAUSES`。

    ⚠️ **「是否仍留池」那一列是派生的**（见 :data:`CAUSE_KEEP`）：
    人只填 ``cause`` 与 ``human_note``。留给人手填那一列就是两份真源——
    实测首轮就漂了 4 行（同一个 URL 在两批里给出相反的留池值），
    而汇总读的是映射 ⇒ 汇总出来的是错的分布。

    ⚠️ **派生列仍然允许人手动改**（改了就以人的为准）——但**改了什么
    要看得见**：人填的原始值留在 ``keep_as_negative_human``，
    与生效值不一致的行在 ``keep_conflicts`` 里**点名**。
    重生成工作表时**必须把这份审计值带过去**（见 :func:`_carry_over`）——
    丢它就等于「静默抹掉人的判断」，正是本脚本要防的那件事。

**表二 · 动作可移植性**
    每行 = B 面一条**元素定位动作**（`click` / `fill` / `press`，
    带 `target: {role, name}`）。列 = 动作本体 + **重放要用的前置条件**
    （动作发生时的观察 URL——存档里**只有它**，动作记录本身不带 URL）
    + **name 的机械体检**（私用区字符 / 控制字符 / 长度 / 非 ASCII）
    + **给人工填的空格**。

    ⚠️ 机械体检**不是结论**：它只标出「这一条重放时最可能出问题」。
    真正的判定要等**重放探针**（台账 M-25）跑出定位读数。

**用法**（必须在仓库根跑）：

    uv run --offline python -m trajectory_pipeline.research.probes.inc73_human_review \\
        --a trajectory_pipeline/output/pipeline/dualrun2/obscura \\
        --b trajectory_pipeline/output/pipeline/dualrun2/camoufox \\
        --b trajectory_pipeline/output/pipeline/dualrun/camoufox \\
        --json trajectory_pipeline/research/probes/inc73_human_review.json

**只读**：不写 P1、不改代码、不联网。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import unicodedata
from typing import Any, Mapping

# ⚠️ **同源纪律**：落地 URL 的归一化**只有一份实现**，在配对比较脚本里。
#    抄第二份必然漂——而漂了的后果是「配不上对」，
#    症状是「A 面那条对照不见了」，看起来像「A 面没有这条」。
from trajectory_pipeline.research.probes.inc72_paired_compare import (  # noqa: E402
    load_runs,
    norm_url,
)

#: **分支标签的注册表与「非样本」清单**——**同源纪律**：成因词表不许
#: 手抄第二份分支名，站点侧成因**必须是注册表里的分支**，
#: 且**不能**是「永远不进池」的那几个（真因是它们的行必须出池）。
from trajectory_pipeline.executor.branches import (  # noqa: E402
    BRANCH_LABELS,
    NON_SAMPLE_BRANCHES,
)

#: **已弃用分支集**——唯一所有者在感知层（分支表在 executor，弃用集在
#: questions，两处各有其职）。汇总要把它单列：``login_wall_or_blocked``
#: 那 10 条的标签错是**已知**的（H-15），混进「真·标签错」会把
#: 这一轮新发现的错盖掉。
from trajectory_pipeline.perception.questions import (  # noqa: E402
    DEPRECATED_BRANCHES,
)

#: **站点侧成因**——「站点真的说看不了」。
#:
#: ⚠️ **取值就是分支名**（``executor.branches.BRANCH_LABELS`` 的键），
#: 所以「人判的真因」与「机器贴的标签」能**逐条对账**：
#: ``cause != branch`` ⇒ **那条的标签错了**。
#:
#: ⚠️ **2026-10-21 改口径：旧的 W1–W4 作废**。旧词表只有 5 项，而
#: **负样本池里实际有 9 种分支**——付费墙、无播放控件、组件不可验证、
#: 只有预告、导航失败**全都没有位置**。硬塞进 W1 的后果是**把 404 页、
#: Cloudflare 验证页、地区限制页统统记成「登录墙/反爬」**
#: （实测：主档池 ``login_wall_or_blocked`` 的 10 条抽样里，
#: 2 条是 404、2 条是 Cloudflare 挑战、1 条是 App 限定，**0 条能确证是登录墙**）。
#:
#: 而**同一个合并坑判据① 已经踩过**（``executor/dual_run.py`` 的
#: 「反爬拦截率」一节）：「反爬 + 登录墙」的合并口径给出**相反的结论**
#: （B 34.09% > A 19.15%，但真·反爬是 B 9.09% < A 14.89%）——
#: **差全在登录墙桶里，而登录墙不是反爬**。成因词表**不许**再合。
#:
#: 分支体系**已经把墙拆开了**（2026-10-12 增量三十二：
#: ``login_wall_or_blocked`` → ``login_required`` / ``blocked``，
#: 另有 ``subscription_wall`` / ``partial_free``）——
#: cause 再把它们合回去是**逆向拆解**。
#:
#: 这 10 项 = **池里实际出现过的分支**（主档 181 行 + 双跑 45 行的
#: 全部分支值）∪ ``partial_free``（试看/预告形态的付费墙，池里还没出现，
#: 但分支体系里有——**不能等它出现了再补词表**）。
SITE_SIDE_CAUSES: tuple[str, ...] = (
    "not_play_site",         # 压根不是播放页
    "no_play_control",       # 是播放页，但连一个播放控件都没有
    "login_required",        # 登录墙：登录后**免费**看（agent 无登录态）
    "blocked",               # 反爬：地区限制 / 验证码 / 人机验证
    "subscription_wall",     # 付费墙：要订阅/付钱才能看
    "partial_free",          # 部分免费：试看/预告能看，完整内容要钱
    "trailer_only",          # 只有预告片，播放资源不存在
    "broken",                # 内容损坏：404 / 白屏 / 媒体不可解码
    "unreachable_hard",      # 压根没打开页面（DNS/超时/网络错误）
    "component_unverified",  # 点了播放入口，但页面上没有可验证的组件
)

#: **我们侧成因**——「不是站点拦，是我们没拿到 / 没验出来 / 没判出来」。
#:
#: 这五条**标签不成立**（池子的价值是「这里真的看不了」），
#: 处置是降级 ``unresolved`` 出池。
#:
#: ⚠️ 前两个**不是分支名**——它们是「采集层出了什么事」的动作码，
#: 分支体系里没有对应分支（对应的是 ``unresolved``）。
#: 后三个**是**分支，但都在 ``NON_SAMPLE_BRANCHES`` 里——**永远不进池**。
#:
#: ⚠️ ``unresolved`` 与 ``?`` **不是一回事**：
#: ``unresolved`` = **判过了**，结论就是「没判出来」（fail-closed 落点，
#: 台账 H-15 的裁定）；``?`` = **还没判**，要人再去看一眼。
OUR_SIDE_CAUSES: tuple[str, ...] = (
    "not_collected",      # 页面有内容，我们没读到（客户端渲染 / 抽正文失败）
    "nav_interrupted",    # 我们把上一次导航打断了（evidence: interrupted by another navigation）
    "player_unverified",  # 有播放组件，但确认不了能不能用
    "trailer_suspect",    # 词表判不准是不是预告
    "unresolved",         # 没判出来：站点返回占位/不可用页，但没有任何拦截特征
)

#: **「不是负样本」一类**——**假负样本**：机器把它记成了负样本，
#: 人工确认它**不是**。
#:
#: ``success`` 的含义（2026-10-21，人判首轮实测）：**该站确实有正片资源**，
#: 只是**当时停在剧集/资源页**——点剧集按钮没点进去、或被拦在列表页、
#: 或播放器在下一层页面。播放组件没出现，机器就记了
#: ``component_unverified`` 之类的负分支，**行进了负样本池**。
#:
#: 实测（首轮 39 行人判）：**7 行**落这一类，其中 **5 行的机器标签是
#: ``component_unverified``**——而 A 面（obscura）对**同一批 URL** 记的是
#: ``unresolved``（进复核队列、**不进池**）。**同一页面两面归属不同**：
#: B 面的分支来自「**点击失败（超时）**」路径——同页守卫要「点击前后两个
#: 观察」才拦得住，点击直接失败时**没有新观察**，守卫不触发，负标签就留下了。
#:
#: ⚠️ **与分支体系的 ``success``（``branch=None``）不是同一件事**：
#: 那边是「**视频播出来了**」（判定为可播放资源），这边是
#: 「**资源存在**（页面在剧集/资源页），我们没走到播放页」。
#: 共同点只有一条：**这行不是负样本**。两套账本各自闭合，**不互相查询**。
#:
#: 处置与「我们侧」同（出池），但**理由不同**：
#: 我们侧是「数据坏了，降级 ``unresolved``」；
#: 这里是「**它其实是个好样本**（可播放资源），只是标签错了」。
#: ⇒ 处置路径不一样：这一类**要转正**，不能只降级——
#: 而「改分支/转池」的路径**目前不存在**（台账已记）。
NOT_NEGATIVE_CAUSES: tuple[str, ...] = (
    "success",  # 资源存在（页面在剧集/资源页），我们没走到播放页 ⇒ 假负样本
)

#: **说不清**——**合法答案**，但必须写进 ``human_note`` 说明卡在哪。
UNKNOWN_CAUSE: str = "?"

#: **人判词表（封闭集）**——「负样本成因」的候选类别。
#:
#: ⚠️ **封闭集是纪律，不是分类学**。加类要改这张表并改判据⑤ 的读法；
#: 自由发挥的类别**汇总不了**，而判据⑤ 要产出的恰恰是汇总。
CAUSES: tuple[str, ...] = (
    SITE_SIDE_CAUSES + OUR_SIDE_CAUSES + NOT_NEGATIVE_CAUSES + (UNKNOWN_CAUSE,)
)

#: 每个成因的**判定提示**（给人看的一句话）——**只是提示，不是判据**；
#: 完整判据以《人工复核与判读手册》§2.3 为准。
CAUSE_HOWTO: dict[str, str] = {
    "not_play_site": "页面压根没有那部片子 / 是门户首页 / 是别的栏目",
    "no_play_control": "是播放页，但**连一个播放控件都没有**（试看也没有）",
    "login_required": "要登录才能看，**登录后免费**",
    "blocked": "地区限制 / 弹验证码 / 人机验证 / 反爬",
    "subscription_wall": "有播放页，**要订阅/付钱**才能看",
    "partial_free": "有免费部分（试看/预告），**完整内容要钱**",
    "trailer_only": "播放资源不存在，只有预告",
    "broken": "404 / 白屏 / 乱码 / 媒体解不了",
    "unreachable_hard": "压根没打开页面（DNS/超时/网络错误）",
    "component_unverified": "点了播放入口，但页面上**没有可验证的组件**",
    "not_collected": "页面**有**内容，我们没读到（客户端渲染/抽正文失败）",
    "nav_interrupted": "**我们**把上一次导航打断了",
    "player_unverified": "有播放组件，但**确认不了能不能用**",
    "trailer_suspect": "词表判不准是不是预告",
    "unresolved": "没判出来：站点返回占位/不可用页，但**没有任何拦截特征**",
    "success": "**资源存在**（页面在剧集/资源页，人工确认可播）——"
               "我们没走到播放页 ⇒ 这行**不是负样本**",
    "?": "说不清——写进 human_note，别硬填",
}

#: ``keep_as_negative`` 的取值——**封闭集**。空串 = **没判**（待查）。
#:
#: ⚠️ **它问的不是「这页是不是负样本」**（那由 ``branch`` 说了），
#: 而是「**这条还该不该留在负样本池里**」——即**它的 ``branch`` 标签
#: 站不站得住**。理由：池子的全部价值是「**这里真的看不了**」，
#: 一条理由不成立的行混进去，会**静默毁掉**它。
KEEP_VALUES: tuple[str, ...] = ("yes", "no")

#: **成因 → 留池与否**的**默认映射**（唯一真源）。
#:
#: 判据只有一条：**这条是不是「站点明确说了看不了」**。
#:
#: - 是（站点侧的 10 项）⇒ 标签成立 ⇒ ``yes``；
#: - 不是（我们侧的 5 项：没采到 / 导航被我们打断 / 验不出来 / 没判出来）
#:   ⇒ 标签不成立 ⇒ ``no``（应降级 ``unresolved``，出池）；
#: - **资源存在**（``success``）⇒ 站点没说看不了，反而给了资源
#:   ⇒ ``no``（**假负样本**，出池——但处置是「转正」不是「降级」）。
#:
#: ⚠️ **``?`` 映射成空串而不是 ``no``**——「还没判」**不是**「不是负样本」。
#: 它是**待查**，要进待查清单；填 ``no`` 等于把一条没查清的行
#: 当成已认定的坏数据处理掉。
#:
#: ⚠️ 「责任在谁」是同一句话的另一种说法，但**不够用**：
#: ``unresolved`` / ``trailer_suspect`` 既不责任在站点、也不责任在我们——
#: 是**没判出来**。按「站点明确说了看不了」这条判据，它们同样出池。
#:
#: ⚠️ **留池列是派生的，不手填**（2026-10-21 首轮人判后改）：
#: 人填 ``cause``、机器按本表派生 ``keep_as_negative``。
#: 留给人手填那一列，就是**两份真源**——实测首轮就漂了 4 行
#: （**同一个 URL 在两批里给出相反的 keep**），
#: 而「人按词表填、汇总按映射读」正是**错的分布**的来源。
#: 人填的那一版保留在 ``keep_as_negative_human``（审计用），
#: 与派生值不一致的行在 ``keep_conflicts`` 里**点名**。
CAUSE_KEEP: dict[str, str] = {
    **{c: "yes" for c in SITE_SIDE_CAUSES},   # 站点说「看不了」⇒ 标签成立
    **{c: "no" for c in OUR_SIDE_CAUSES},     # 我们没读到 / 没验出来 ⇒ 出池
    **{c: "no" for c in NOT_NEGATIVE_CAUSES},  # 资源存在 ⇒ 本来就不该在池里
    UNKNOWN_CAUSE: "",                        # 还没判 ⇒ 留空
}

#: 元素定位动作——**表二只收这三种**（它们才有 ``target``）。
#: ``goto`` / ``new_tab`` / ``close_tab`` 不带元素定位，不属可移植性范畴。
TARGET_ACTIONS: frozenset[str] = frozenset({"click", "fill", "press"})

#: **私用区（Private Use Area）**——图标字体把字形码放在这里。
#:
#: ⚠️ 这些码位**没有可读含义**：它们的语义由页面加载的字体决定。
#: 同一个码位换个站点/换套字体就是另一个字——所以拿它当可访问名，
#: 判据⑥ 的「重放还能不能定位到」就有**额外的脆弱面**。
_PUA_RANGES: tuple[tuple[int, int], ...] = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD))

#: 控制字符（不含 \t \n \r）——出现在可访问名里多半是脏数据。
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _is_pua(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _PUA_RANGES)


def _name_check(name: str) -> dict[str, Any]:
    """可访问名的机械体检——**只标风险，不下结论**。"""
    pua = [c for c in name if _is_pua(c)]
    ctrl = _CTRL.findall(name)
    return {
        "len": len(name),
        "pua_count": len(pua),
        "pua_chars": "".join(pua),
        "control_count": len(ctrl),
        "non_ascii": any(ord(c) > 127 for c in name),
        "has_space": any(c.isspace() for c in name),
        "empty": not name.strip(),
    }


def _outcome_rows(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """一个存档的 outcome 行（带页面事实）。"""
    out: list[dict[str, Any]] = []
    task = str(record.get("task_id") or "")
    run_id = str(record.get("run_id") or "")
    # 落地 URL → 观察事实
    obs_by_key: dict[str, Mapping[str, Any]] = {}
    for v in record.get("visits") or ():
        if not isinstance(v, Mapping):
            continue
        key = norm_url(str(v.get("landed_url") or ""))
        obs = v.get("site_obs")
        if key and isinstance(obs, Mapping):
            obs_by_key.setdefault(key, obs)
    for oc in record.get("outcomes") or ():
        if not isinstance(oc, Mapping):
            continue
        key = norm_url(str(oc.get("url") or oc.get("site_url") or ""))
        obs = obs_by_key.get(key, {})
        out.append({
            "task_id": task,
            "run_id": run_id,
            "url": str(oc.get("url") or oc.get("site_url") or ""),
            "key": key,
            "branch": str(oc.get("branch") or ""),
            "branch_label": str(oc.get("branch_label") or ""),
            "question": str(oc.get("question") or ""),
            "source": str(oc.get("source") or ""),
            "fallback_used": bool(oc.get("fallback_used")),
            "reached_play_page": bool(oc.get("reached_play_page")),
            "is_negative_sample": bool(oc.get("is_negative_sample")),
            "evidence": str(oc.get("evidence") or ""),
            "page_quotes": [str(q) for q in (oc.get("page_quotes") or ())][:4],
            "page_title": str(obs.get("page_title") or ""),
            "body_len": obs.get("body_len"),
            "body_text_head": str(obs.get("body_text") or "")[:120],
            "elements_total": obs.get("elements_total"),
            "video_tag_count": obs.get("video_tag_count"),
            "iframe_count": obs.get("iframe_count"),
            "degraded": list(obs.get("degraded") or ()),
        })
    return out


def _in_ladder(obs: Mapping[str, Any], role: str, name: str) -> bool:
    """动作发生时，这个 ``(role, name)`` **在不在**可交互阶梯的产物里。

    ⚠️ 阶梯会把**真同名整组**丢掉（设计方案 04 §4.5.3）——所以
    「页面上有 3 个同名」与「页面上没有这个目标」在阶梯产物里同形。
    判据⑥ 的归因靠这一项分开：

    - ``True``  → 目标当时**是**被采到的，重放定位不到 = **名字脆 / 页面漂**
    - ``False`` → 目标当时**就不在**采集面给出的集合里
      （真同名被丢 / 采集面与动作面对不上），重放定位不到 = **本来就没这条**

    阶梯产物在 P1 里是 ``observation.interactive_elements``
    （``[{"ref","role","name"}, ...]``）——**只有 ref 面的存档有**，
    obscura 面走的是另一套编码，这里读不到就返回 ``False``。
    """
    elems = obs.get("interactive_elements")
    if not isinstance(elems, (list, tuple)):
        return False
    for el in elems:
        if not isinstance(el, Mapping):
            continue
        if str(el.get("role") or "") == role and str(el.get("name") or "") == name:
            return True
    return False


def _action_rows(record: Mapping[str, Any], src: str) -> list[dict[str, Any]]:
    """一个存档的元素定位动作行。

    ⚠️ **动作记录本身不带 URL**——「在哪个页面做的」只存在于
    该步的**观察**里。所以重放探针必须先取 ``observation.url``，
    否则会拿着动作去找一个「不在任何页面上的元素」。
    """
    out: list[dict[str, Any]] = []
    task = str(record.get("task_id") or "")
    run_id = str(record.get("run_id") or "")
    for vi, v in enumerate(record.get("visits") or ()):
        if not isinstance(v, Mapping):
            continue
        landed = str(v.get("landed_url") or "")
        for si, st in enumerate(v.get("steps") or ()):
            if not isinstance(st, Mapping):
                continue
            a = st.get("action")
            if not isinstance(a, Mapping):
                continue
            tool = str(a.get("tool") or "")
            if tool not in TARGET_ACTIONS:
                continue
            target = a.get("target")
            target = target if isinstance(target, Mapping) else {}
            name = str(target.get("name") or "")
            obs = st.get("observation")
            obs = obs if isinstance(obs, Mapping) else {}
            out.append({
                "src": src,
                "task_id": task,
                "run_id": run_id,
                "visit_index": vi,
                "step_index": si,
                "tool": tool,
                "params": a.get("params") if isinstance(a.get("params"), Mapping) else {},
                "origin": str(a.get("origin") or ""),
                "role": str(target.get("role") or ""),
                "name": name,
                "visit_landed_url": landed,
                "action_url": str(obs.get("url") or ""),
                "error": str(st.get("error") or ""),
                "duration_ms": st.get("duration_ms"),
                "name_check": _name_check(name),
                # ── 动作发生时的页面事实（重放探针要并排比的那一半）──
                # ⚠️ 同一份观察的三个读数 + **当时阶梯里有没有这个目标**。
                #    第四项是判据⑥ 的分水岭：目标当时**不在**阶梯里 ⇒
                #    动作本来就是蒙的（不是「重放定位不到」）；
                #    在阶梯里却重放不到 ⇒ 页面漂了或名字脆（要分开归因）。
                "archived_page_title": str(obs.get("page_title") or ""),
                "archived_body_len": obs.get("body_len"),
                "archived_elements_total": obs.get("elements_total"),
                "archived_in_ladder": _in_ladder(obs, str(target.get("role") or ""), name),
            })
    return out


def _md_cell(text: str, limit: int = 90) -> str:
    """表格单元格里不能有裸的 ``|`` 与换行。"""
    s = " ".join(str(text).split()).replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _safe(text: Any) -> str:
    """**按控制台编码转义**——Windows 控制台是 GBK，而素材里有 GBK 编不了的字符。

    ⚠️ 私用区字符（图标字体字形码）就是这种：U+F01D 在 GBK 里**没有映射**，
    直接 print 会 `UnicodeEncodeError` **把整张工作表打断**——
    而工作表恰恰是为「看私用区字符」才打印它的。
    转义成 ``\\uf01d`` 后**信息还在**，且任何终端都打得出来。
    """
    s = str(text)
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return s.encode(enc, "backslashreplace").decode(enc)
    except (LookupError, UnicodeError):
        return s.encode("ascii", "backslashreplace").decode("ascii")


def _p(*parts: Any) -> None:
    """打印前逐段转义。"""
    print(" ".join(_safe(x) for x in parts))


#: **人填的字段**——重生成工作表时**必须沿用**。
#:
#: ⚠️ **2026-10-21 起只有两项**：``cause`` 与 ``human_note``。
#: 留池列 ``keep_as_negative`` **不再手填**——它由 ``cause`` 派生
#: （:data:`CAUSE_KEEP`）。人手填那一列等于**两份真源**：实测首轮就漂了
#: 4 行（同一个 URL 在两批里给出相反的 keep），而汇总读的是映射——
#: 于是「汇总出来的是错的分布」。
#: 人填的那一版留在 :data:`KEEP_AUDIT_FIELD`（审计用）。
HUMAN_FIELDS: tuple[str, ...] = ("cause", "human_note")

#: 留池列的**审计副本**——人填的原始值（与派生值不一致时点名）。
#:
#: ⚠️ **这个字段在不在，就是两代表的分界**：
#: 旧表（首轮那张）里没有它 ⇒ 留池列**就是人填的**；
#: 新表里有它 ⇒ 留池列是**派生的**，人填的原始值在这里。
#: 沿用时不分清 ⇒ 把派生值当人的值 ⇒ 4 条冲突静默消失。
KEEP_AUDIT_FIELD: str = "keep_as_negative_human"

#: 留池列的名字（首轮是「人填的」，之后是「派生的」；可被人手改）。
LEGACY_KEEP_FIELD: str = "keep_as_negative"


def _row_key(r: Mapping[str, Any]) -> tuple[str, str, str]:
    """一行的人判定位键：``(task_id, run_id, url)``。

    ⚠️ **不用 ``key``**：那是 A 面配对用的键，人填的时候看不见，
    拿它做沿用键等于让人对不上号。
    """
    return (str(r.get("task_id", "")), str(r.get("run_id", "")), str(r.get("url", "")))


def _carry_over(prev_sheet: list[Mapping[str, Any]],
               new_sheet: list[dict[str, Any]]) -> int:
    """把**旧工作表里已填的格子**沿用到新表上，返回沿用了几条。

    ⚠️ **为什么必须沿用**：工作表是**从存档重新生成**的，
    而人的判断只存在于这张 JSON 里——重生成一次（加个列、改个词表）
    就会把人半小时的判断**静默抹掉**，而且工作表看起来还是「正常」的。

    沿用只认「非空值」：旧表里空的格子不覆盖新表（新表可能带了
    更好的自动预填）。匹配键是 :func:`_row_key`。

    ⚠️ **留池列要分清两代表**（2026-10-21 实测踩到）：

    - **旧表**（首轮那张）：留池列**就是人填的**，没有审计列；
    - **新表**：留池列是**派生的**，人填的原始值在
      :data:`KEEP_AUDIT_FIELD` 里。

    分不清就会出事：把**派生值**当成人的值写进审计列 ⇒
    4 条留池冲突在重生成时**静默消失**（实测：``keep_conflicts`` 4 → 0），
    而「分歧必须暴露给人看」这条纪律就破了。
    判据是**审计列在不在**（``KEEP_AUDIT_FIELD in old``）——
    不用另加标记字段。

    ⚠️ **人手动改过派生列** ⇒ 以手改的为准（那是人的最新表达）。
    认得出来靠「旧值 ≠ 按旧成因算出的映射值」——成因没变时才算手改。
    """
    if not prev_sheet:
        return 0
    prev = {_row_key(r): r for r in prev_sheet}
    carried = 0
    for row in new_sheet:
        old = prev.get(_row_key(row))
        if not old:
            continue
        for f in HUMAN_FIELDS:
            v = old.get(f)
            if isinstance(v, str) and v.strip():
                row[f] = v
                carried += 1
        # ── 留池列：审计值与生效值**分开取** ──
        old_cause = str(old.get("cause") or "")
        old_effective = old.get(LEGACY_KEEP_FIELD)
        old_effective = old_effective if isinstance(old_effective, str) else ""
        if KEEP_AUDIT_FIELD in old:
            # 新表：``keep_as_negative`` 是**生效值**（可被人手改过），
            # ``keep_as_negative_human`` 是**审计值**（人填的原始值）。
            human = old.get(KEEP_AUDIT_FIELD)
            if isinstance(human, str) and human.strip():
                row[KEEP_AUDIT_FIELD] = human
                carried += 1
            derived_old = CAUSE_KEEP.get(old_cause, "")
            if (old_effective.strip()
                    and old_cause == str(row.get("cause") or "")
                    and old_effective != derived_old):
                # 人手改过派生列 ⇒ 那是人的最新表达，沿用
                row[LEGACY_KEEP_FIELD] = old_effective
                carried += 1
        else:
            # 旧表：留池列**就是人填的** ⇒ 存进审计列（生效列按映射重算）
            if old_effective.strip():
                row[KEEP_AUDIT_FIELD] = old_effective
                carried += 1
    return carried


def _derive_keep(sheet: list[dict[str, Any]]) -> None:
    """按 :data:`CAUSE_KEEP` **就地重算**留池列（原地改，不返回新表）。

    ⚠️ **留池列是派生的**：人填 ``cause``，留池与否由映射决定。
    人填的那一版在 :data:`KEEP_AUDIT_FIELD` 里，两边不一致的行由
    :func:`_keep_conflicts` 点名——**不静默取一个**。

    ⚠️ **人手动改过这一列 ⇒ 以手改的为准**（那是人的最新表达），
    而且它与映射的差由 :func:`_keep_conflicts` 点名。
    """
    for row in sheet:
        derived = CAUSE_KEEP.get(str(row.get("cause") or ""), "")
        cur = str(row.get(LEGACY_KEEP_FIELD) or "")
        if cur and cur != derived:
            row[LEGACY_KEEP_FIELD] = cur      # 人手改过 ⇒ 保留
        else:
            row[LEGACY_KEEP_FIELD] = derived


def _keep_conflicts(sheet: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """人填的留池值与**派生值**打架的行——**要点名，不静默取一个**。

    实测（2026-10-21 首轮人判）：**4 行**漂了，而漂的方式是
    **同一个 URL 在两批里给出相反的留池值**（``vip.1905.com`` 在
    DR01/cc2a2b13 批是 ``no``、在 DR01/eaabd02a 批是 ``yes``）——
    这类矛盾**不可能两个都对**。所以按规则派生，并把人填的那一版
    留在 :data:`KEEP_AUDIT_FIELD` 供回看。

    另有一类：**``cause`` 不在词表里** ⇒ 派生不出留池值——
    同样点名（否则这条会静默当成「未判」混进分布）。
    **这一类不要求有人的留池值**：词表外的成因本身就是问题。

    ⚠️ **但「还没判」（``cause`` 空）不算冲突**——那是「待查」，
    报成冲突会让**一张还没判的表报出满屏冲突**（39 行全中）。

    第三类：**人手动改过派生列**（生效值 ≠ 映射值）——
    改动是人的表达，但与规则不一致 ⇒ 也要点名。
    """
    out: list[dict[str, Any]] = []
    for r in sheet:
        human = str(r.get(KEEP_AUDIT_FIELD) or "")
        effective = str(r.get(LEGACY_KEEP_FIELD) or "")
        cause = str(r.get("cause") or "")
        mapping = CAUSE_KEEP.get(cause, "")
        if not cause:
            # **还没判**（``cause`` 空）——那是「待查」，不是「填错了」。
            # 报成冲突 ⇒ 一张还没判的表会报出**满屏**冲突（39 行全中）。
            continue
        if cause not in CAUSE_KEEP:
            reason = "cause 不在词表里，派生不出留池值"
        elif effective != mapping:
            # 生效值与映射不一致 ⇒ **人手动改过派生列**——
            # 这是更具体的原因，先判它（否则会被下面那条盖掉）。
            reason = "人手改过派生列（与映射不一致）"
        elif human and human != effective:
            reason = "人填值与规则派生值不一致"
        else:
            continue
        out.append({
            "task_id": r.get("task_id", ""),
            "run_id": r.get("run_id", ""),
            "url": r.get("url", ""),
            "branch": r.get("branch", ""),
            "cause": cause,
            "keep_human": human,
            "keep_derived": mapping,
            "keep_effective": effective,
            "reason": reason,
        })
    return out


def _summary(sheet: list[Mapping[str, Any]]) -> dict[str, Any]:
    """判据⑤ 的**汇总**——分布 + 对账 + 冲突，全部从表里现算。

    ⚠️ **留池数按派生值算**（:data:`CAUSE_KEEP`），不按人填的那一列：
    人填的那一列与规则打架时以规则为准，并把人填的值列进
    ``keep_conflicts``——「汇总按映射读、人按词表填」正是错的分布的来源。

    ``cause_differs_branch_excluding_deprecated`` 是**真·标签错**的行数：
    已弃用的合并态 ``login_wall_or_blocked`` 单列——那 10 条的标签错是
    **已知**的（H-15），混进来会把「这轮新发现的错」盖掉。
    """
    from collections import Counter

    cause_dist = Counter(str(r.get("cause") or "") for r in sheet)
    keep_dist = Counter(str(r.get("keep_as_negative") or "") for r in sheet)
    same = sum(1 for r in sheet if r.get("cause") == r.get("branch"))
    merged = sum(1 for r in sheet
                 if r.get("branch") in DEPRECATED_BRANCHES
                 and r.get("cause") != r.get("branch"))
    return {
        "rows": len(sheet),
        "judged": sum(1 for r in sheet if str(r.get("cause") or "").strip()),
        "cause_dist": dict(cause_dist),
        "keep_dist": dict(keep_dist),
        "keep_conflicts": len(_keep_conflicts(sheet)),
        "cause_equals_branch": same,
        "cause_differs_branch": len(sheet) - same,
        "cause_differs_branch_excluding_deprecated": len(sheet) - same - merged,
        "unknown_causes": {c: n for c, n in cause_dist.items()
                           if c and c not in CAUSE_KEEP},
    }


def _check_vocabulary() -> None:
    """**词表与分支注册表对账**——认不出的成因直接抛，**不静默放过**。

    站点侧成因**必须**是注册分支，且**不能**是「永远不进池」的那几个
    （真因是它们的行必须出池，不能被当成「站点侧真因」留在池里）。

    这条检查放在**生成工作表时**跑，而不是只在测试里——探针是手工跑的，
    测试不一定跟着跑；词表写错时工作表必须是**出不来**的，
    而不是「生成了一份口径已经错了的表」。
    """
    unknown = [c for c in SITE_SIDE_CAUSES if c not in BRANCH_LABELS]
    if unknown:
        raise ValueError(f"站点侧成因不是注册分支：{unknown}")
    non_sample = [c for c in SITE_SIDE_CAUSES if c in NON_SAMPLE_BRANCHES]
    if non_sample:
        raise ValueError(
            f"站点侧成因不能是非样本分支（永远不进池）：{non_sample}")
    # 「不是负样本」一类的成因**不许是分支**——它们记的是「标签错了」，
    # 不是「站点说了什么」。``success`` 尤其要盯住：分支体系里已经有一个
    # ``success``（``branch=None`` = 视频播出来了），两边同名不同义——
    # 哪天 ``success`` 进了分支注册表，本词表的「资源存在」就会与
    # 「视频播出来了」混成**一个码**，那就回到「登录墙与反爬共用一码」
    # 那条老路上了。
    bad = [c for c in NOT_NEGATIVE_CAUSES if c in BRANCH_LABELS]
    if bad:
        raise ValueError(
            f"「不是负样本」成因不能是注册分支（同名会与分支语义混淆）：{bad}")


def build(a_dirs: list[pathlib.Path], b_dirs: list[pathlib.Path],
          prev_sheet: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    _check_vocabulary()
    a_rows: list[dict[str, Any]] = []
    for d in a_dirs:
        a_rows += _outcome_rows_all(d)
    b_rows: list[dict[str, Any]] = []
    for d in b_dirs:
        b_rows += _outcome_rows_all(d)
    a_by_key = {(r["task_id"], r["key"]): r for r in a_rows if r["key"]}

    sheet1: list[dict[str, Any]] = []
    for r in b_rows:
        if not r["is_negative_sample"]:
            continue
        peer = a_by_key.get((r["task_id"], r["key"]))
        sheet1.append({
            **r,
            "a_peer": {
                "url": peer["url"] if peer else "",
                "branch": peer["branch"] if peer else "",
                "evidence": peer["evidence"] if peer else "",
                "page_title": peer["page_title"] if peer else "",
                "body_len": peer["body_len"] if peer else None,
                "elements_total": peer["elements_total"] if peer else None,
                "degraded": peer["degraded"] if peer else [],
            } if peer else None,
            "cause": "",          # ← 人工填：CAUSES 里的一个（站点侧填分支名）
            # 留池列**是派生的**（见 CAUSE_KEEP）——人只填 cause。
            "keep_as_negative": "",
            KEEP_AUDIT_FIELD: "",  # ← 人填过的留池值（审计副本）
            "human_note": "",     # ← 人工填：依据
        })

    sheet2: list[dict[str, Any]] = []
    for d in b_dirs:
        for rec in load_runs(d):
            sheet2 += _action_rows(rec, str(d))
    carried = _carry_over(prev_sheet or [], sheet1)
    _derive_keep(sheet1)
    conflicts = _keep_conflicts(sheet1)
    return {
        "a_dirs": [str(d) for d in a_dirs],
        "b_dirs": [str(d) for d in b_dirs],
        "causes": list(CAUSES),
        "carried_over_cells": carried,
        "summary": _summary(sheet1),
        "keep_conflicts": conflicts,
        "sheet1_negative_cause": sheet1,
        "sheet2_action_portability": sheet2,
    }


def _outcome_rows_all(d: pathlib.Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in load_runs(d):
        out += _outcome_rows(rec)
    return out


def _print_sheet1(sheet: list[dict[str, Any]]) -> None:
    _p("\n## 表一 · 负样本成因（人判）")
    _p(f"共 {len(sheet)} 条 `is_negative_sample=True`。")
    from collections import Counter
    hist = Counter(r["branch"] for r in sheet)
    _p("**分支分布**：")
    for b, n in hist.most_common():
        _p(f"  {b:26s} x{n}")
    _p(f"**成因词表（封闭集，{len(CAUSES)} 项）**——站点侧填**分支名**，"
       "我们侧填动作码，「不是负样本」填 `success`，待定填 `?`：")
    for c in CAUSES:
        keep = CAUSE_KEEP.get(c, "")
        tag = {"yes": "留池", "no": "出池"}.get(keep, "待查")
        _p(f"  {c:22s} {CAUSE_HOWTO.get(c, '')}  => keep={keep or '(留空)'} [{tag}]")
    _p("判据只有一条：**是不是「站点明确说了看不了」**。"
       "是 => yes（标签成立，留池）；不是 => no（标签不成立，出池）；"
       "还没判 => 留空。\n")
    _p("⚠️ **留池列是派生的**（不手填）——手填过的那一版留在 "
       "`keep_as_negative_human`，与派生值不一致的行在 `keep_conflicts` 里点名。\n")
    _p("**cause 与 branch 对账**：cause = 人判的真因（分支名），"
       "branch = 机器贴的标签。**两者不同 ⇒ 那条的标签错了**。\n")
    for i, r in enumerate(sheet, 1):
        peer = r.get("a_peer") or {}
        _p(f"### [{i}] {r['url']}")
        _p(f"  task={r['task_id']} run={r['run_id'][:8]} "
              f"branch=`{r['branch']}` question=`{r['question']}` "
              f"source={r['source']} fallback={r['fallback_used']} "
              f"reached_play={r['reached_play_page']}")
        _p(f"  标题    : {_md_cell(r['page_title'], 70)}")
        _p(f"  观察    : body_len={r['body_len']} elements={r['elements_total']} "
              f"video={r['video_tag_count']} iframe={r['iframe_count']} "
              f"degraded={r['degraded']}")
        _p(f"  正文头  : {_md_cell(r['body_text_head'], 110)}")
        _p(f"  page_quotes: {' / '.join(_md_cell(q, 60) for q in r['page_quotes'])}")
        _p(f"  evidence: {_md_cell(r['evidence'], 200)}")
        _p(f"  A 面同页: branch=`{peer.get('branch', '(配不上)')}` "
              f"body_len={peer.get('body_len')} degraded={peer.get('degraded')}")
        _p(f"  A 面证据: {_md_cell(peer.get('evidence', ''), 160)}")
        keep = r.get("keep_as_negative") or "____"
        audit = r.get(KEEP_AUDIT_FIELD) or ""
        mapping = CAUSE_KEEP.get(str(r.get("cause") or ""), "")
        flags = []
        if audit and audit != keep:
            flags.append(f"冲突：人填={audit}，此处按规则派生")
        if mapping and keep != mapping:
            flags.append(f"人手改过：映射={mapping}")
        flag = ("  [" + "；".join(flags) + "]") if flags else ""
        _p(f"  => 成因 [{r['cause'] or '____'}] "
              f"是否仍是负样本 [{keep}]（派生）{flag} "
              f"依据 [{r['human_note'] or '____'}]")


def _print_summary(report: Mapping[str, Any]) -> None:
    """判据⑤ 的**汇总**——先看这个，再逐行看表。

    ⚠️ **留池数按派生值算**（:data:`CAUSE_KEEP`）：人填的那一列与规则
    打架时以规则为准，冲突在下面单独点名——不静默取一个。
    """
    s = report.get("summary") or {}
    _p("\n## 汇总（判据⑤）")
    _p(f"共 {s.get('rows')} 行，已判 {s.get('judged')} 行。")
    _p("**成因分布**：")
    for c, n in sorted((s.get("cause_dist") or {}).items(), key=lambda kv: -kv[1]):
        _p(f"  {c or '(未判)':22s} x{n}")
    _p("**留池分布（派生）**：")
    for k, n in sorted((s.get("keep_dist") or {}).items(), key=lambda kv: -kv[1]):
        _p(f"  {k or '(未判)':22s} x{n}")
    _p(f"**标签对账**：cause == branch {s.get('cause_equals_branch')} 行 / "
       f"不同 {s.get('cause_differs_branch')} 行"
       f"（其中已弃用合并态 {s.get('cause_differs_branch_excluding_deprecated')} "
       f"行是**真·标签错**）")
    unknown = s.get("unknown_causes") or {}
    if unknown:
        _p(f"⚠️ **词表外的 cause**：{unknown}——汇总不了，"
           f"要么改词表要么改这批填法")
    conflicts = report.get("keep_conflicts") or []
    if conflicts:
        _p(f"⚠️ **留池冲突 {len(conflicts)} 行**"
           f"（人填值 vs 规则派生值；派生值为准，原始值在 "
           f"`{KEEP_AUDIT_FIELD}`）：")
        for c in conflicts:
            _p(f"  {c['task_id']}/{str(c['run_id'])[:8]} "
               f"branch=`{c['branch']}` cause=`{c['cause']}` "
               f"人填={c['keep_human'] or '(空)'} "
               f"映射={c['keep_derived'] or '(空)'} "
               f"生效={c.get('keep_effective') or '(空)'} "
               f"—— {c['reason']}")
            _p(f"      {c['url']}")
    _p("")


def _print_sheet2(sheet: list[dict[str, Any]]) -> None:
    _p("\n## 表二 · 动作可移植性（人判）")
    _p(f"共 {len(sheet)} 条元素定位动作。")
    _p("**name 体检**只标风险（私用区字符 = 图标字体字形码，无可读含义）。\n")
    for i, r in enumerate(sheet, 1):
        c = r["name_check"]
        flags = []
        if c["pua_count"]:
            flags.append(f"私用区×{c['pua_count']}(U+{ord(c['pua_chars'][0]):04X})")
        if c["control_count"]:
            flags.append(f"控制字符×{c['control_count']}")
        if c["empty"]:
            flags.append("**空名**")
        if c["has_space"]:
            flags.append("含空白")
        if not c["non_ascii"]:
            flags.append("纯ASCII")
        _p(f"### [{i}] {r['tool']} role=`{r['role']}` name=`{r['name']}`")
        _p(f"  来源={pathlib.Path(r['src']).name}/{r['task_id']} run={r['run_id'][:8]} "
              f"visit#{r['visit_index']}.step#{r['step_index']} origin={r['origin']}")
        _p(f"  动作页  : {_md_cell(r['action_url'], 90)}")
        _p(f"  落地页  : {_md_cell(r['visit_landed_url'], 90)}")
        _p(f"  体检    : len={c['len']} {' / '.join(flags) if flags else '—'} "
              f"duration_ms={r['duration_ms']}")
        if r["error"]:
            _p(f"  错误    : {_md_cell(r['error'], 140)}")
        _p(f"  => 重放能否定位到 [{'____'}] "
              f"人工依据 [{'____'}]")


def _prev_sheet(json_path: str | None) -> list[Mapping[str, Any]]:
    """读旧工作表（若存在）——只取表一，**只给人填的格子用**。

    读不到 / 文件坏了都**不报错**：工作表是草稿，坏了就当没有，
    但**不能**因为读不成就拒绝重生成（那是把人卡在旧表上）。
    """
    if not json_path:
        return []
    p = pathlib.Path(json_path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sheet = data.get("sheet1_negative_cause")
    return list(sheet) if isinstance(sheet, list) else []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="人判两条的工作表（负样本成因 / 动作可移植性）")
    ap.add_argument("--a", action="append", default=[], help="A 面存档目录（可给多次）")
    ap.add_argument("--b", action="append", default=[], help="B 面存档目录（可给多次）")
    ap.add_argument("--json", default=None, help="工作表落盘路径")
    ap.add_argument("--only-branch", action="append", default=[],
                    help="表一只留这些分支（可给多次；不给 = 全部负样本）")
    args = ap.parse_args(argv)

    a_dirs = [pathlib.Path(d) for d in args.a]
    b_dirs = [pathlib.Path(d) for d in args.b]
    for d in a_dirs + b_dirs:
        if not d.is_dir():
            print(f"目录不存在：{d}", file=sys.stderr)
            return 2
    if not b_dirs:
        print("至少要给一个 --b", file=sys.stderr)
        return 2

    report = build(a_dirs, b_dirs, prev_sheet=_prev_sheet(args.json))
    if args.only_branch:
        keep = set(args.only_branch)
        report["sheet1_negative_cause"] = [
            r for r in report["sheet1_negative_cause"] if r["branch"] in keep
        ]
    print(f"A = {', '.join(str(d) for d in a_dirs) or '(无)'}")
    print(f"B = {', '.join(str(d) for d in b_dirs)}")
    if report["carried_over_cells"]:
        print(f"沿用旧表已填格子：{report['carried_over_cells']} 个"
              f"（旧表 = {args.json}）")
    _print_summary(report)
    _print_sheet1(report["sheet1_negative_cause"])
    _print_sheet2(report["sheet2_action_portability"])

    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n落盘: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
