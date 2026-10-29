"""人工复核队列——W1 正样本的**唯一来源**。

为什么需要它
------------
W1 的 ``RulePerceptor`` 对 ``PLAYER_OK`` **一律不判成功**——媒体计数只是
**必要条件**、不是充分条件（2026-10-23 · M-26，标签数不再许诺成功）；
而真实视频站（优酷 / 爱奇艺 / 西瓜）又多是 **iframe / JS 播放器**，
连标签都不产生。于是判断点 ④ 对它们一律返回 ``answer=None``
→ ``player_unverified``（不是 ``unresolved``：后者说的是「能不能到」，
这里不确定的是「组件能不能用」）。

方案已确认：**W1 不等 W3 的 LLMPerceptor，正样本靠人工复核**。
本模块就是那条路径的实现——把"走到了播放页但代码判不出来"的站点
连同**足够的复核上下文**导出成待办，人工确认后回填。

为什么不自动把 ``unresolved`` 当成功
----------------------------------
``unresolved`` 是"没判出来"，不是"判出来了"（I4）。直接当真会产出两种假数据：
把**判不出来的坏页**记成正样本（教坏模型），把**采集失败**记成正样本
（更糟——基础设施故障被写进业务结论）。所以必须由人给出结论，
而人需要看到的是**判断依据**，不是一行 "unresolved"。

复核员看到什么
--------------
导出项刻意带上 :class:`~trajectory_pipeline.perception.base.Observation` 里
**当时真正看到的东西**：标题、``<video>``/``<iframe>`` 计数、交互元素标签样本、
正文摘要、采集降级项、原始 evidence 文本。缺任何一项，复核就只能靠
"打开网址自己再看一遍"——那等于让人替代码重跑一遍，队列也就没有存在意义。

闭环
----
:func:`apply_verdicts` 把人工结论回填进 P1 存档：确认成功的改 ``branch=None``，
确认不行的落到真负样本分支（``confirm_not_play_site`` → ``not_play_site``；
``confirm_negative`` → ``component_unverified`` 或 ``no_play_control``，
按 ``reached`` 区分；阶段 1 新增 ``confirm_login`` → ``login_required``、
``confirm_blocked`` → ``blocked``、``confirm_broken`` → ``broken``、
``confirm_partial_free`` → ``partial_free``，同样受归属与同页守卫约束）。
**回填保留 ``reviewed_by`` 与原始 branch**，不覆盖——人工判定要能追溯，
且将来能测"人工与代码的分歧率"（D 的下钻）。
"""

from __future__ import annotations

import collections
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Mapping

from trajectory_pipeline.executor.branches import (
    COLLECTION_SIDE_BRANCHES,
    NON_SAMPLE_BRANCHES,
    TERMINAL_SHAPE_BRANCHES,
)
from trajectory_pipeline.perception import questions
from trajectory_pipeline.common.elements import (
    element_json,
    vocabulary_of_archive_elements,
)

# ─────────────────────────────────────────────────────────────────────
# L6 派发常量（2026-10-13 增量三十六落地）
# ─────────────────────────────────────────────────────────────────────
#
# L6 人工触发器的判据由代码层**事实**决定，不由 LLM 决定——
# 与「代码层做护栏、LLM层做探索」纪律一致。两条触发条件各自守一条事实：
#
# 1. :data:`REVIEW_BRANCHES` ——「代码没判出来」（分支体系里的反思档）
# 2. :data:`AUTO_REVIEW_NOTES` ——「采集 / 仲裁层主动 fail-closed」
#
# 漏任一条都会把一类失败样本留在 P1 没人看，造出与实测脱节的负样本池。
#: ``REVIEW_BRANCHES`` 之外、**凭 fail-closed note 也要进复核队列**的 note 集合。
#:
#: ``vlm_error`` — L4 reviewer 抛错时挂的哨兵；人是唯一能定结论的来源
#: ``credential_leak`` — 凭据闸门触发的硬短路；落地的真 DOM 未必被人看过
#: ``timeout`` / ``driver_missing`` / ``driver_error`` — 驱动层掉链；
#:   L4 / L5 自然短路，**事实层**未必抓全；让人看一眼
#:
#: ⚠️ 这条集合**只挂 mediabrowse / run**（生产链），不挂
#: :func:`cli.cmd_proberun` / :func:`cli.cmd_staticprobe` /
#: :func:`cli.cmd_mediaprobe`（验证工具）——后者的失败形态是工具特性，
#: 派给人工等于让人替验证工具背书。
AUTO_REVIEW_NOTES: Final[frozenset[str]] = frozenset({
    "vlm_error", "credential_leak", "timeout",
    "driver_missing", "driver_error",
})

#: L6 默认追加落点。**与人工** ``cli review --write``
#: 派的 ``queue.jsonl`` 物理隔离——auto-派发与人工派发使用同一文件名
#: 时，``write_queue`` 是覆盖写，**auto-追加项会被人工导出整批替换掉**。
#: 把 auto-派发落到独立 jsonl 后，人工 export 仍然覆盖 ``review/queue.jsonl``，
#: 两条线互不污染。
AUTO_QUEUE_SUBDIR: Final = "review_auto"
AUTO_QUEUE_FILENAME: Final = "l6_dispatch.jsonl"


def auto_queue_path(root: Path | str | None = None) -> Path:
    """L6 auto-派发队列的解析路径。

    ``root=None`` 走 **P1 默认根** :data:`~executor.archive.DEFAULT_ROOT`
    ——与 ``cmd_review`` 同源，避免「目录对不上 → 派发项找不到」这种
    静默失效。永远 ``mkdir(parents=True, exist_ok=True)``，副作用轻。
    """
    if root is None:
        from trajectory_pipeline.executor.archive import DEFAULT_ROOT
        base = Path(DEFAULT_ROOT)
    else:
        base = Path(root)
    return base / AUTO_QUEUE_SUBDIR / AUTO_QUEUE_FILENAME

#: 人工可给的裁定。**枚举值定死**是因为下游要按它分派，
#: 自由文本裁定会退化成"复核员写什么算什么"，无法统计也无法回填。
VERDICTS: Final[dict[str, str]] = {
    "confirm_success": "人工确认：这里确实能看（记为成功样本）",
    "confirm_negative": "人工确认：这里确实看不了（记为负样本）",
    "confirm_not_play_site": (
        "人工确认：这压根不是该片的可观看站（记 not_play_site 负样本）"
    ),
    "confirm_paywall": (
        "人工确认：有播放页但需订阅/付费（记 subscription_wall 负样本）"
    ),
    "confirm_partial_free": (
        "人工确认：有免费部分但完整内容需付费（记 partial_free 负样本）"
    ),
    "confirm_login": (
        "人工确认：登录后才能免费看（记 login_required 负样本）"
    ),
    "confirm_blocked": (
        "人工确认：被验证码/地区限制/反爬等环境阻断挡住"
        "（记 blocked；**采集侧账，不进负样本池**）"
    ),
    "confirm_collection_blocked": (
        "人工确认：这次确实是被拦挡住的（记 collection_status，"
        "**不改 branch、不进负样本池**）——人进得去而 agent 进不去时用它"
    ),
    "confirm_broken": (
        "人工确认：内容损坏，404 或媒体不可解码（记 broken 负样本）"
    ),
    "trailer": "人工确认：只有预告片（记为 trailer_only 负样本）",
    "need_browser": "存档证据不足，需人工开浏览器再看（暂不裁定）",
    "skip": "跳过（不可复核，如已失效页面）",
}

#: ``confirm_negative`` 与 ``confirm_not_play_site`` 为什么分开（2026-10-10）
#:
#: 队列里混着**压根不是播放站**的候选——实测队首就是 ``hao123.com`` 首页
#: （导航站）与 ``trtag.com``，它们压根没有那部片子。而
#: ``confirm_negative`` 只有两个出口，按 ``reached`` 分岔成
#: ``component_unverified`` / ``no_play_control``：导航站首页往往
#: ``reached_play_page=True``（点了个导航项，页面没换），于是被贴成
#: 「到了播放页但没有可验证的播放组件」——**一个语义完全相反的标签**，
#: 而且它是**真负样本**，会直接进池。
#:
#: 两支的结论说的不是一件事，训练出来的东西也不同：``not_play_site``
#: 学「这个结果根本不该点」，``no_play_control`` 学「点了但这里没控件」。
#: 合成一支就把这个区别训没了。
#:
#: ⚠️ 这一支**没有同页守卫**：``BRANCH_PRECONDITION`` 里的前提是给**代码判定**
#: 用的，而这里是人已经看过页面下的结论——人说不是播放站就是不是，
#: 再拿「点击前后是否同页」去否决它只会把正确裁定退回 unresolved。
#:
#: ``confirm_paywall`` 同理补上（2026-10-10）——用户已裁定「**有播放页但要付费
#: 不算成功**」，而 ``confirm_negative`` 的两个出口都装不下它：优酷《功夫》
#: 页有控件、有播放器，只卡在「立即购买 / 最低6元/月升SVIP」，
#: 贴 ``component_unverified`` 会说成「控件没渲染」，贴
#: ``not_play_site`` 更是反的——优酷**就是**《功夫》的可观看站。
#: 没有这一档时复核员只剩 ``need_browser`` 可选，一个已经判明的结论
#: 被白丢，而 ``subscription_wall`` 这条分支永远攒不到样本。
#:
#: ⚠️ 与 :data:`branches.BRANCH_PRECONDITION` 里那条同页守卫**不冲突**：
#: 那张表只作用于 ``decision_branch``（代码判定路径），回填不经过它。

#: 进入复核队列的分支。只有这几类需要人看。
#:
#: - ``unresolved``：代码判不出来（环节①–④，还没到播放页）
#: - ``player_unverified``：判断点④ 判不出来，**但控件点了、播放页在眼前**
#:   ——这一支是 W1 正样本的**主要来源**：规则版对真实视频站一律返 ``None``，
#:   而这些站点全都走到了播放页，人看一眼就能定
#: - ``trailer_suspect``：词表判不准，需要语义判断（方案里本来就设计了人工兜底）
#: - 真负样本**不进队列**：代码已给出确定性结论，复核它们属于抽检（另一件事）。
#:
#: ⚠️ ``player_unverified`` 必须在队列里：它拆出来是为了让复核员知道
#: 「不确定的是组件能不能用」而不是「代码什么都判不出」，
#: 而拆出去却不进队列 = 这批待办凭空消失，而它正是 W1 唯一的正样本来源。
#:
#: 2026-10-26（R1）加 ``blocked``：它是**采集侧账**
#: （:data:`~executor.branches.COLLECTION_SIDE_BRANCHES`）——出负样本池，
#: 但不是「没人需要看」。恰恰相反：**只有人能回答那个问题**——「我这边
#: 进得去，agent 进不去」到底是站点性质（地区/DRM）还是这一次的运气。
#: 代码给不出这个答案（同指纹同日实测 6/10 可达），所以它必须进队列；
#: 而它进队列**不**等于它算负样本，对应裁定是
#: ``confirm_collection_blocked``（写 ``collection_status``，不写 branch）。
REVIEW_BRANCHES: Final = frozenset({
    "unresolved", "trailer_suspect", "player_unverified",
    "blocked",
})

#: **裁定 → 想贴的分支**。``None`` 的键表示「不动分支」。
#:
#: ⚠️ ``confirm_success`` 的 ``None`` 是**「成功」**（成功样本的 branch 恒为
#: None），不是「不处理」——真不处理的是 ``need_browser`` / ``skip``，
#: 它们不在这张表里。
_WANTED_BRANCH: Final[dict[str, str | None]] = {
    "confirm_success": None,                 # 成功 = branch 为 None
    "confirm_negative": None,                # 按 reached 分岔，见 _wanted
    "confirm_not_play_site": "not_play_site",      # ①
    "confirm_paywall": questions.PAY_WALL,         # ④
    "confirm_partial_free": "partial_free",        # ④ 有免费部分（preview/trial）
    "confirm_login": "login_required",             # ② 登录后可免费看
    "confirm_blocked": "blocked",                  # ② 验证码/地区/反爬
    "confirm_broken": "broken",                    # ② 404/媒体不可解码
    "trailer": "trailer_only",                     # ③
}


def _wanted(verdict: str, rec: Mapping[str, Any]) -> str | None:
    """这个裁定想贴的分支。``confirm_negative`` 按 ``reached`` 分岔。"""
    if verdict != "confirm_negative":
        return _WANTED_BRANCH[verdict]
    return ("component_unverified" if rec.get("reached_play_page")
            else "no_play_control")


def _fits(want: str | None, question: str, belongs: list[str]) -> bool:
    """这个分支能不能贴在这条 outcome 上。

    ⚠️ **成功（``want is None``）也要核归属**，且只能落在④ ``player.ok`` 上。
    这条一开始写的是「成功一律可以」——**错的**，实测被 D-5 当场否掉 15 条：
    ``branch=None`` 的含义是「**这次访问成功了**」，而一次成功的访问必然
    走完 ①→②→③→④。outcome 记的却是②或③，意味着控制流在③就停了
    （原判是 ``unresolved``：找不到控件、没走到④）。

    那种情况下人说「这站确实能看」，给出的不是「这次访问成功了」，
    而是「**代码漏判了一个正样本**」——两件事。写进 ``branch=None``
    会把一条「代码判不出」记成「模型判对了」，
    而训练信号正好反了。所以它同样降级为 ``site_verdict``。

    ⚠️ **``question`` 为空时一律拒绝**：老存档压根没记是哪个判断点，
    「无法核对」不等于「可以贴」。

    ⚠️ **但终局形态那两支是例外**（2026-10-29 · Q-25 甲护栏落地后暴露）：
    ``broken`` / ``blocked`` **压根不属于任何判断点**——它们是**代码层对
    页面正文的事实核**（404 / 地区限制 / 验证码），**不回答任何一道题**
    ⇒ 执行层自己落这种行时 **question 恒为空**（实测本批 12 行
    ``broken`` 全是空 question）。于是上面那条「空 question 一律拒绝」
    会把**执行层自己能产出的形状**挡在人工回填之外 ⇒ **人裁不了执行层
    已经判出来的东西**（实测 28 行 kmslxsh 的 404 页就卡在这儿：
    代码判得出，人一裁就变成「只落 site_verdict、分支不动」）。

    而 D-5 早就接受 ``("", "broken")`` / ``("", "blocked")``
    （``d5_scorer.QUESTIONLESS_PAIRS`` 从
    ``executor.branches.TERMINAL_SHAPE_BRANCHES`` 推导）⇒ **两处口径
    必须一致**，否则就是同一个仓库里两套规矩（H-16 记的那类）。
    ⚠️ 例外**只给终局形态这两支**：判断点分支（``not_play_site`` /
    ``component_unverified`` / …）仍然拒收空 question——它们**真的要归属
    到某道题**，贴错了会造出决策树里不存在的组合。

    ``belongs`` 是 :func:`~trajectory_pipeline.perception.questions.questions_of_branch`
    的**全部产地**（2026-10-16 方案 12 阶段 A1 起为复数）：多产地分支
    （``not_play_site`` 由 ① ``select.play_sites`` 与 ①.5
    ``classify.search_links`` 双注册）只要**任一产地**命中当前 outcome
    的 question 就合规，不强制唯一——老档与新档的「这不是播放站」
    是同一支。未知分支（空列表）任何 question 都不合规。
    """
    if want == "unresolved":
        return True                          # 通用 fail-closed 出口
    if want is None:
        return question == questions.Q.PLAYER_OK
    if not question and want in TERMINAL_SHAPE_BRANCHES:
        return True                          # 终局形态：没有归属判断点
    return any(b == question for b in belongs)

#: 交互元素样本条数。复核员判断"有没有播放按钮"主要看这个，
#: 太多会淹没人眼（实测单页常有 80+ 元素），太少会漏掉唯一那个。
ELEMENT_SAMPLE: Final = 25


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """一条待复核项。字段刻意多——见模块 docstring「复核员看到什么」。"""

    task_id: str
    url: str
    landed_url: str
    branch: str
    evidence: str
    reached_play_page: bool
    page_title: str
    video_tag_count: int
    iframe_count: int
    #: 正文的**展示摘要**（前 200 字符）——P1 存档存的是全文，
    #: 这里只给人眼看，所以刻意截断以免复核页被长正文淹没。
    #: 字段名保持 ``body_preview``：它描述的是**这个队列项**的形态，
    #: 不是 P1 存档的字段名。存档侧那个字段叫 ``body_text``（全文）。
    body_preview: str
    degraded: tuple[str, ...]
    #: 媒体探针结论（``mcp_probe`` 登记；如 "duration=0" / "媒体不可解码"）。
    #: 阶段 1 起暴露给复核员判断「媒体损坏形态的 broken」——仅靠页面正文
    #: 看不出来「媒体元素存在但解不了码」，必须由探针把信号带进队列。
    media_signal: str = ""
    #: 代码判定时的付费墙细分形态（``preview`` / ``trial`` → ``partial_free``）。
    #: 带进队列是让复核员核「有免费部分」这个判断本身有没有依据。
    paywall_type: str = ""
    #: 复核可看的截图路径/URL 清单（探针/采集环节留存，空 = 没有截图）。
    screenshots: tuple[str, ...] = ()
    elements: tuple[Mapping[str, str], ...] = ()
    verdict: str = ""            # 人工填写；空 = 待复核
    reviewed_by: str = ""
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "url": self.url,
            "landed_url": self.landed_url,
            "branch": self.branch,
            "evidence": self.evidence,
            "reached_play_page": self.reached_play_page,
            "page_title": self.page_title,
            "video_tag_count": self.video_tag_count,
            "iframe_count": self.iframe_count,
            "body_preview": self.body_preview,
            "degraded": list(self.degraded),
            "media_signal": self.media_signal,
            "paywall_type": self.paywall_type,
            "screenshots": list(self.screenshots),
            "elements": [dict(e) for e in self.elements],
            "verdict": self.verdict,
            "verdict_options": dict(VERDICTS),
            "reviewed_by": self.reviewed_by,
            "note": self.note,
        }


class ReviewQueueError(ValueError):
    """复核回填出错。显式抛。"""


def extract_items(archive: Mapping[str, Any]) -> list[ReviewItem]:
    """从一份 P1 存档里抽复核项。

    需要把 ``outcomes`` 与 ``visits`` **按 url 关联**才能拿到观察细节——
    只给 outcome 的话复核员拿不到 ``<video>`` 计数和元素标签，
    队列就退化成一张"请自行判断"的清单。
    """
    task_id = str(archive.get("task_id") or "")
    by_landed: dict[str, Mapping[str, Any]] = {}
    by_url: dict[str, Mapping[str, Any]] = {}
    for visit in archive.get("visits") or []:
        if not isinstance(visit, Mapping):
            continue
        for obs_key in ("player_obs", "site_obs"):
            obs = visit.get(obs_key)
            if not isinstance(obs, Mapping):
                continue
            landed = str(visit.get("landed_url") or visit.get("url") or "")
            by_landed.setdefault(landed, obs)
            by_url.setdefault(str(visit.get("url") or ""), obs)
            if obs.get("url"):
                by_landed.setdefault(str(obs["url"]), obs)

    items: list[ReviewItem] = []
    for outcome in archive.get("outcomes") or []:
        if not isinstance(outcome, Mapping):
            continue
        branch = str(outcome.get("branch") or "")
        if branch not in REVIEW_BRANCHES:
            continue
        url = str(outcome.get("url") or "")
        obs = by_landed.get(url) or by_url.get(url) or {}
        # 键名跟着**存档的方言**走（P4）：这里写死 ``tag``/``label`` 会把
        # camoufox 批次的元素抽成三列空串，而复核员看到的是「有元素，但
        # 没有标签」——队列本身没报错，只是白抽了一批。
        raw_elements = [
            e for e in list(obs.get("interactive_elements") or [])[:ELEMENT_SAMPLE]
            if isinstance(e, Mapping)
        ]
        vocabulary = vocabulary_of_archive_elements(raw_elements)
        elements = tuple(
            element_json(e, vocabulary, name_limit=60) for e in raw_elements
        )
        items.append(ReviewItem(
            task_id=task_id,
            url=url,
            landed_url=str(obs.get("url") or url),
            branch=branch,
            evidence=str(outcome.get("evidence") or ""),
            reached_play_page=bool(outcome.get("reached_play_page")),
            page_title=str(obs.get("page_title") or ""),
            video_tag_count=int(obs.get("video_tag_count") or 0),
            iframe_count=int(obs.get("iframe_count") or 0),
            body_preview=str(obs.get("body_text") or obs.get("body_preview") or "")[:200],
            degraded=tuple(str(d) for d in (obs.get("degraded") or [])),
            media_signal=str(
                outcome.get("media_signal") or obs.get("media_signal") or ""
            ),
            paywall_type=str(
                outcome.get("paywall_type") or obs.get("paywall_type") or ""
            ),
            screenshots=tuple(
                str(s) for s in (
                    outcome.get("screenshots") or obs.get("screenshots") or []
                )
            ),
            elements=elements,
        ))
    return items


def collect(root: Path | str, *, pattern: str = "*.json") -> list[ReviewItem]:
    """扫一个目录下的全部 P1 存档，抽全部复核项。"""
    base = Path(root)
    if not base.is_dir():
        raise ReviewQueueError(f"{base} 不是目录")
    out: list[ReviewItem] = []
    for path in sorted(base.glob(pattern)):
        if path.name.endswith("review.jsonl"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not isinstance(data, Mapping):
            continue
        out.extend(extract_items(data))
    return out


def write_queue(path: Path | str, items: Iterable[ReviewItem]) -> int:
    """写 jsonl。append 还是覆盖由调用方决定路径，这里**覆盖**——
    复核队列是**派生产物**，重跑就该重建；人工裁定另存到
    ``<name>.verdicts.jsonl``，两者分开才不会互相覆盖。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item.to_json(), ensure_ascii=False) + "\n")
            n += 1
    return n


def _sync_visit_success(out: dict[str, Any], urls: set[str]) -> int:
    """人工确认成功后，把对应 ``visits[]`` 的 ``success`` 也翻成 True。

    **为什么必须一起翻**：存档里「成功」记在两个地方——
    ``outcome.branch is None`` 与 ``visits[i].success``。控制流一次只写两处，
    而 :func:`apply_verdicts` 只改了 outcome，于是留下
    ``success=False 但 branch=None``。

    后果不是「多一个不一致的字段」：``visits[].success`` 是
    **assembler 切样本时判定该决策单元是否终止的依据**，它没翻，
    切出来的样本就带着一条「失败」的 outcome，而 D-5 会把它读成
    「分支走错」——实测 15 条一票否决全部由此而来，
    ``check-archives`` 的 ``success_branch_mismatch`` 记 16 条。

    按 ``url`` 与 ``landed_url`` 双键匹配：outcome 的 ``url`` 记的是
    **结论形成处**，而 visit 的 ``url`` 是候选地址，两者在重定向后不同。
    """
    n = 0
    visits = out.get("visits")
    if not isinstance(visits, list):
        return 0
    for visit in visits:
        if not isinstance(visit, dict) or visit.get("success"):
            continue
        keys = {str(visit.get("url") or ""), str(visit.get("landed_url") or "")}
        if keys & urls:
            visit["success"] = True
            visit["success_by_review"] = "人工确认（review --apply）"
            n += 1
    return n


def apply_verdicts(
    archive: Mapping[str, Any],
    verdicts: Mapping[str, Mapping[str, Any]],
) -> tuple[Mapping[str, Any], dict[str, int]]:
    """把人工裁定回填进一份存档。

    ``verdicts`` 是 ``{landed_url: {verdict, reviewed_by, note}}``。

    ⚠️ **只对 :data:`REVIEW_BRANCHES` 内的分支生效**，其余一律原样保留
    并计入 ``ignored``。这条不是洁癖：复核队列只导出
    ``unresolved`` / ``trailer_suspect``，而 ``verdicts`` 文件可能来自
    人工手填、跨批次粘贴、或者对着一份**旧**队列在编辑新存档——
    里面完全可能混进一条真负样本的 url。若无差别回填，
    一次误填就会把 ``no_play_control`` 改成成功样本。

    而**负样本池的全部价值就是"这里真的看不了"**：
    混进"其实能看"的条目会直接毁掉它，且这个破坏是**静默**的——
    分支分布只会显示"成功 +1、负样本 -1"，看不出那条是从哪来的。

    **只改分支，不改原始证据**：``evidence`` 保留代码当时写下的那句，
    另加 ``review_*`` 字段记录人工怎么裁的。这样"人工与代码的分歧率"
    仍然可测——覆盖掉之后就永远测不出来了。

    ⚠️ **只写「归属同一个判断点」的分支**（2026-10-10 口径裁定）
    ------------------------------------------------------------
    人工复核给出的其实是**站点级**结论——「这不是播放站」「这站付费墙」，
    而 outcome 是**决策单元级**的：它带着某个判断点的 ``question``。
    跨判断点贴分支会造出决策树里不存在的组合，且**破坏是静默的**：

    * ``(site.find_play_control, not_play_site)``——①的结论贴在③上
    * ``(site.is_reachable, subscription_wall)``——④的结论贴在②上

    实测 14 条，其中 15 条触发 D-5 一票否决（``branch=None`` 与
    ``question`` 非④ 组合，被读成「分支走错」），门禁
    ``test_d5_scorer::Test同源`` 当场红。

    所以**归属不对就不改 ``branch``**，改记 ``site_verdict`` +
    ``site_verdict_mismatch``，人工结论照样留下、照样可统计，
    决策树保持它该有的样子。归属用
    :func:`~trajectory_pipeline.perception.questions.questions_of_branch`
    核（多产地分支任一命中即合规，2026-10-16 方案 12 阶段 A1）。

    未裁定的项**保持原样**并计入 ``unreviewed``，不默认成成功也不默认丢弃。

    ⚠️ **采集侧账写 ``collection_status``，不写 ``branch``**（2026-10-26 · R1）
    -----------------------------------------------------------------------
    ``blocked`` 进复核队列（:data:`REVIEW_BRANCHES`），但它**不是负样本**
    （:data:`~executor.branches.COLLECTION_SIDE_BRANCHES`）。于是两条路：

    * ``confirm_blocked`` —— 改判 ``branch="blocked"``，同时写
      ``collection_status``，``is_negative_sample`` **恒为 False**；
    * ``confirm_collection_blocked`` —— **不动 branch**，只写
      ``collection_status="blocked"``：人确认的是「这次没进去」，
      而分支早就写着 blocked 了。

    两个都进 ``collection_written`` / ``branch_written`` 各自的计数，
    报表上「人确认被拦」与「人确认看不了」永远分得开。
    """
    out = dict(archive)
    # 计数键**从 VERDICTS 派生**，不手写——手写过一次就漏了 `need_browser`
    # 与 `skip`，末尾那句 `stats[verdict] += 1` 对它们直接 KeyError。
    stats: dict[str, int] = {v: 0 for v in VERDICTS}
    stats.update(unreviewed=0, ignored=0, off_question=0, skipped=0,
                 branch_written=0, collection_written=0)
    new_outcomes: list[Mapping[str, Any]] = []
    confirmed: set[str] = set()

    for outcome in out.get("outcomes") or []:
        if not isinstance(outcome, Mapping):
            new_outcomes.append(outcome)
            continue
        url = str(outcome.get("url") or "")
        branch = str(outcome.get("branch") or "")
        v = verdicts.get(url)
        verdict = str((v or {}).get("verdict") or "").strip()

        if verdict and branch not in REVIEW_BRANCHES:
            # 裁定落在一条**不在复核范围内**的记录上 —— 记下来，不改。
            stats["ignored"] += 1
            new_outcomes.append(outcome)
            continue
        if not verdict:
            stats["unreviewed"] += 1
            new_outcomes.append(outcome)
            continue
        if verdict not in VERDICTS:
            raise ReviewQueueError(
                f"未知裁定 {verdict!r}（url={url}）；可选：{sorted(VERDICTS)}"
            )

        rec = dict(outcome)
        rec["review_verdict"] = verdict
        rec["reviewed_by"] = str((v or {}).get("reviewed_by") or "")
        rec["review_note"] = str((v or {}).get("note") or "")
        # ⚠️ **回填可以叠加，但「代码原判」不许被覆盖**（2026-10-29）：
        # 同一份存档被裁过两轮时，第二轮的输入是**上一轮的复核档**⇒
        # ``outcome["branch"]`` 已经是**人改过的**分支 ⇒ 直接拿它当
        # ``branch_before_review`` 会把**代码原判**抹成上一轮的人裁，
        # 「人工与代码的分歧率」从此不可测。已有就保留已有的。
        prior = outcome.get("branch_before_review")
        rec["branch_before_review"] = (
            prior if prior is not None else outcome.get("branch"))

        # 这个裁定想要贴上去的分支。``None`` 表示「成功」。
        if verdict in _WANTED_BRANCH:
            # 只有这九个裁定会碰 branch；need_browser / skip 走下面的 pending 分支
            want = _wanted(verdict, rec)
            question = str(outcome.get("question") or "")
            belongs = questions.questions_of_branch(want)
            if not _fits(want, question, belongs):
                # 跨判断点：**不动 branch**，人工结论降级为站点级记录。
                # 成功写成字面量 "success" 而不是 None——落盘后 JSON null
                # 与「字段不存在」在下游完全同形，报表读到 null 时
                # 分不出「这是成功结论」与「这里什么都没记」。
                rec["site_verdict"] = "success" if want is None else want
                rec["site_verdict_verdict"] = verdict
                rec["site_verdict_mismatch"] = (
                    f"{want!r} 归属判断点 {belongs!r}，"
                    f"而本条记的是 {question!r}")
                stats["off_question"] += 1
                stats[verdict] += 1
                new_outcomes.append(rec)
                continue
            rec["branch"] = want
            # 入池判据与 :attr:`branches.SiteOutcome.is_negative_sample`
            # **同源**，包括第二段排除（采集侧账）。写成 ``want is not None``
            # 的话，人一贴 ``confirm_blocked`` 就把一条拦截写进负样本池——
            # R1 的全部工作被一次人工回填抵消，而分支分布上看不出来
            # （那一行本来就写着 blocked）。
            rec["is_negative_sample"] = (
                want is not None and want not in COLLECTION_SIDE_BRANCHES)
            if want in COLLECTION_SIDE_BRANCHES:
                rec["collection_status"] = want
            rec["source"] = "human"
            stats["branch_written"] += 1
            if want is None:
                confirmed.add(url)
        elif verdict == "confirm_collection_blocked":
            # **只写采集侧账，不动 branch**。人这条裁定说的是
            # 「这次确实被拦了」，而分支本来就是 blocked——把一句否定
            # （不是站点性质）写成一句肯定（branch=blocked 再入池）正是
            # 反的。原判不是 blocked 的（如 unresolved）也保持原样：
            # 人确认的是「没进去」，不是「代码该改判成什么」。
            rec["collection_status"] = "blocked"
            rec["is_negative_sample"] = False
            stats["collection_written"] += 1
        elif verdict in ("need_browser", "skip"):
            # 「没裁」与「裁了但没结论」是两种状态，各记各的：
            # ``skipped`` 是给报表看的合计数（两种加在一起），
            # ``stats[verdict]`` 是分开看的——分不开就会把
            # 「页面已失效」与「证据不足」当成同一件事。
            rec["review_pending"] = True
            stats["skipped"] += 1
        stats[verdict] += 1
        new_outcomes.append(rec)

    out["outcomes"] = new_outcomes
    if confirmed:
        _sync_visit_success(out, confirmed)
    out["review_stats"] = stats
    out["review_note"] = (
        "人工裁定已回填。branch_before_review 保留代码原始判定，"
        "source=human 标记人工来源——两者都留着才能测人工与代码的分歧率。"
        "跨判断点的裁定不写 branch，改记 site_verdict——"
        "见 review_queue.apply_verdicts 的 docstring。"
    )
    return out, stats

# ══════════════════════════════════════════════════════════════════════
# 队列预填与收敛（2026-10-10 从一次性脚本提炼）
# ══════════════════════════════════════════════════════════════════════
#
# 提炼的是**机制**，不是那批数据的判据。判据留在
# :data:`PORTAL_HOSTS` 里——它是「哪些站点压根没有逐片播放页这种页面形态」
# 这一条知识，会随站点变化而更新，不该变成命令行参数。

#: 门户 / 导航 / 追踪站。**它们没有「逐片播放页」这种页面形态**——
#: 域名本身就是结论，与「采没采到元素」无关。
PORTAL_HOSTS: Final[tuple[str, ...]] = (
    "hao123.com",      # 导航站
    "trtag.com",       # 追踪 / 标签站
)

#: 写进 ``reviewed_by`` 的标记。**刻意不写人名**——这些行是按形态
#: 推出的，记成人名等于**伪造「人看过证据」**，而复核档是人工裁定
#: 的正式载体。
AUTO_BY_PORTAL: Final = "auto:portal-domain"
AUTO_BY_INSUFFICIENT: Final = "auto:insufficient-evidence"

#: 「没证据」类裁定。**它不对站点下结论**，所以同 URL 出现真结论时
#: 应当让位（见 :func:`settle_same_site`）。
NO_CLAIM: Final[frozenset[str]] = frozenset({"need_browser", "skip"})

PORTAL_NOTE: Final = (
    "门户 / 导航 / 追踪站首页：不存在「该片播放页」这种页面形态，"
    "站点形态本身即结论，与采集完整性无关。"
)
INSUFFICIENT_NOTE: Final = (
    "零交互元素：存档里没有可看的东西，**判不了**。"
    "这不是「这里看不了」——按 fail-closed（I4）与避坑指南 §5.15，"
    "应开浏览器复看后再裁。"
)


def prefill(rows: list[dict[str, Any]]) -> dict[str, int]:
    """按**证据形态**给空行填裁定，**一个字都不改已填的行**。

    两条硬规矩：

    1. **只填空行。** 人的裁定一律不覆盖——``fill`` 逐行判
       ``verdict`` 非空就跳过。
    2. **零交互元素填 ``need_browser``，不是任何负分支。**

    第 2 条不是洁癖，有硬反证：实测 58 条零元素里 **24 条
    ``video_tag_count=1``**（页面上有 ``<video>`` 标签——这不是「能播」的
    证据（2026-10-23 · M-26），但足以**否掉「它不是播放页」**，
    恰好与要贴的标签相反）、**14 条 ``page_title`` 就是
    「功夫 - 电影 - 高清完整版在线观看 - 西瓜视频」**。

    把它们贴成 ``not_play_site`` 就是把**采集失败**写成业务负样本，
    而那是个**真负样本**，会直接进池污染它——理由通顺、零报错。

    返回各类计数，供报表用。
    """
    stats: dict[str, int] = {"confirm_not_play_site": 0, "need_browser": 0,
                             "（有元素，留给人判）": 0, "（已填，未动）": 0}
    for row in rows:
        if str(row.get("verdict") or "").strip():
            stats["（已填，未动）"] += 1
            continue
        if row.get("elements"):
            stats["（有元素，留给人判）"] += 1
            continue
        host = str(row.get("landed_url") or row.get("url") or "")
        if any(h in host for h in PORTAL_HOSTS):
            row["verdict"] = "confirm_not_play_site"
            row["reviewed_by"] = AUTO_BY_PORTAL
            row["note"] = PORTAL_NOTE
            stats["confirm_not_play_site"] += 1
        else:
            row["verdict"] = "need_browser"
            row["reviewed_by"] = AUTO_BY_INSUFFICIENT
            row["note"] = INSUFFICIENT_NOTE
            stats["need_browser"] += 1
    return stats


def settle_same_site(rows: list[dict[str, Any]]) -> int:
    """同 URL 只保留一个裁定：**结论档压过「没证据」档**。返回改动行数。

    同一站点在多份存档里各出现一次，元素完整性逐次不同——实测某个
    《功夫》页 8 行里 1 行零元素（被填成 ``need_browser``）、7 行有元素
    （证据指向付费墙）。裁定按 URL 应用，两档并存就是
    **后一行静默吃掉前一行**，而 ``apply --apply`` 之后无从分辨。

    判据不是「哪个在后」，而是**哪个在说话**：
    ``need_browser`` / ``skip`` 的含义是「**这一行没取到证据**」，
    不是「这个站点如何」——所以它不能压过一条基于证据的结论。
    """
    by_url: dict[str, set[str]] = collections.defaultdict(set)
    for row in rows:
        v = str(row.get("verdict") or "").strip()
        if v:
            by_url[str(row.get("landed_url") or row.get("url") or "")].add(v)

    winner: dict[str, str] = {}
    for url, seen in by_url.items():
        claims = seen - NO_CLAIM
        if len(claims) == 1:                # 有且只有一条结论档 → 它说了算
            winner[url] = claims.pop()
        elif not claims and len(seen) > 1:  # 两档都是「没证据」，取任一
            winner[url] = next(iter(seen))

    n = 0
    for row in rows:
        url = str(row.get("landed_url") or row.get("url") or "")
        w = winner.get(url)
        if w and row.get("verdict") != w:
            row["note"] = ((row.get("note") or "")
                           + f" ｜ 随同 URL 的其他行同步为 {w}"
                             f"（本行零元素，「没证据」不压过有证据的结论）"
                           ).strip(" ｜")
            row["verdict"] = w
            n += 1
    return n


# ══════════════════════════════════════════════════════════════════════
# L6 自动派发（2026-10-13 增量三十六落地）
# ══════════════════════════════════════════════════════════════════════
#
# 与人工导出（``cli review --write`` → :func:`write_queue`）**两条线**：
# - 人工是**覆盖写**到 ``<root>/review/queue.jsonl`` —— 重跑即重建。
# - L6 是**追加写**到 ``<root>/review_auto/l6_dispatch.jsonl`` —— 同 URL
#   不重复派；老行的 verdict 不动。
#
# 两条线物理隔离的理由（2026-10-13 设计裁定）：写同文件名时
# :func:`write_queue` 是覆盖写，**auto-追加项会被人工导出整批替换掉**；
# 分开存，互不污染，且每条线可独立统计 / 独立清空。

#: 默认从 :class:`BrowserProbeResult` 抽取复核项时使用的元素样本条数。
#: 与人工导出 :data:`ELEMENT_SAMPLE` 同值；mediabrowse 没有完整的
#: ``interactive_elements`` 列表，body / snapshot 文本替代元素样本。
AUTO_BODY_PREVIEW_MAX_CHARS: Final = 200


def extract_from_browser_result(
    result: Any, *, task_id: str
) -> list[ReviewItem]:
    """从 :class:`BrowserProbeResult`（含 L5 注入）抽 L6 复核项。

    触发判据（**两条 OR**）：

    1. ``result.l5_outcome.branch ∈ REVIEW_BRANCHES``
       ——「代码层判定为反思档」（unresolved / trailer_suspect /
       player_unverified）。与 :func:`extract_items` 同源。
    2. ``result.note ∈ AUTO_REVIEW_NOTES``
       ——「采集 / 仲裁层主动 fail-closed」。这条是 mediabrowse 专属，
       run 路径的 P1 archive 没有 ``note`` 字段。

    成功（``branch is None``）与其他 16 活跃负样本分支不进——前者按定义
    不需要复核，后者按 :class:`~executor.branches.NON_SAMPLE_BRANCHES`
    反面是**代码已给出确定性结论**，复核它属于抽检（另一件事）。
    （口径：活跃 20 支 − 非样本 4 支 = 16 支入库负样本；2026-10-26 回填。）
    采集侧分支（``blocked``）不在这 16 支里：它在
    :data:`REVIEW_BRANCHES`，理由见那张表——它**不是**确定性结论。

    ⚠️ **dataclass 鸭子处理**——``final_url`` / ``requested_url`` /
    ``note`` 等字段在探测层不同时是空值；本函数不读隐含语义，只看字段
    实际值。
    """
    items: list[ReviewItem] = []
    l5 = getattr(result, "l5_outcome", None)
    note = str(getattr(result, "note", "") or "")
    body = str(getattr(result, "body_excerpt", "") or "")
    snapshot = str(getattr(result, "snapshot_excerpt", "") or "")
    final_url = str(getattr(result, "final_url", "")
                    or getattr(result, "requested_url", "") or "")

    def _branch_qualifies(branch: Any) -> bool:
        return isinstance(branch, str) and branch in REVIEW_BRANCHES

    if l5 is not None and _branch_qualifies(getattr(l5, "branch", None)):
        # L5 → SiteOutcome 落反思档 → 进 L6。这是主路径。
        items.append(ReviewItem(
            task_id=task_id,
            url=final_url,
            landed_url=final_url,
            branch=str(l5.branch),
            evidence=str(getattr(l5, "evidence", "") or ""),
            reached_play_page=True,    # mediabrowse 已到落地页
            page_title="",             # BrowserProbeResult 不直接给标题
            video_tag_count=0,
            iframe_count=0,
            body_preview=body[:AUTO_BODY_PREVIEW_MAX_CHARS],
            degraded=(note,) if note else (),
            media_signal="",
            paywall_type="",
            screenshots=(),
            elements=(),
        ))
    elif l5 is None and note in AUTO_REVIEW_NOTES:
        # Fail-closed 兜底：**L5 没出**（None）时由 fail-closed 兜底触发；
        # 若 L5 已出真负样本（branch 非反思档也非 None），代码已下结论，
        # 不再二次触发——两个信号并存会让同 URL 派两条，靠 settle 收敛
        # 没必要，且不真：派发原因只有一条（哪个是真判据）。
        items.append(ReviewItem(
            task_id=task_id,
            url=final_url,
            landed_url=final_url,
            branch="unresolved",       # fail-closed 出口（与人工队列同源）
            evidence=f"L6 auto-dispatch: note={note!r}",
            reached_play_page=False,
            page_title="",
            video_tag_count=0,
            iframe_count=0,
            body_preview=body[:AUTO_BODY_PREVIEW_MAX_CHARS],
            degraded=(note,),
            media_signal="",
            paywall_type="",
            screenshots=(),
            elements=(),
        ))
    return items


def extract_from_p1_archive(
    archive: Mapping[str, Any]
) -> list[ReviewItem]:
    """从 P1 archive（``cli run --plan`` / ``run --task-id`` 落盘形态）抽 L6。

    本质是把 :func:`extract_items` 加 task_id 解析与忽略空 task_id 的
    薄包装——archive 里已经有 :data:`REVIEW_BRANCHES` 的 outcome 行，
    复用同一条过滤线，**不再引入第二套判断**。
    """
    task_id = str(archive.get("task_id") or "")
    if not task_id:
        return []
    return extract_items(archive)


def append_to_queue(
    path: Path | str, items: Iterable[ReviewItem]
) -> tuple[int, int]:
    """追加写：同 URL **不重复派**，原行不动。

    去重键：``landed_url or url``。这是**派发去重**，不是**裁定去重**
    ——同一 URL 重复出现时不重复写入队列，但下游 ``apply_verdicts``
    仍按 URL 收一份裁定，不会因为「派发去重」就丢掉任何 URL 的处理。

    ⚠️ **不读也不改 verdict**。本函数仅当判定键（同 URL）作去重判据，
    不会因为新一批里有「已填 verdict 的 URL」而跳过——只有「同 URL
    已在本 jsonl」才跳过。这条避坑：覆写人工裁定属于静默破坏，
    L6 派发要么追加要么跳过、不能改老行。

    返回 ``(written, skipped)``：实写条数与跳过的条数。两者加在一起
    = ``len(items)``；调用方用 ``written`` 做末尾打印的那一行汇总。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    existing: set[str] = set()
    if p.exists():
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                key = str(row.get("landed_url") or row.get("url") or "")
                if key:
                    existing.add(key)

    items_list = list(items)
    if not items_list:
        # 空派发不创建/不留痕——避免「目录摸到了、文件 0 字节」被理解
        # 成「派发通道没接好」。``written=0`` 也表明没派，不会被误读。
        return 0, 0

    written = 0
    skipped = 0
    with p.open("a", encoding="utf-8") as fh:
        for it in items_list:
            key = str(it.landed_url or it.url or "")
            if key and key in existing:
                skipped += 1
                continue
            fh.write(json.dumps(it.to_json(), ensure_ascii=False) + "\n")
            if key:
                existing.add(key)
            written += 1
    return written, skipped


def dispatch_l6(
    result_or_archive: Any,
    *,
    task_id: str,
    queue_path: Path | str | None = None,
) -> tuple[int, int]:
    """CLI 钩子：自动派发复核项到 L6 队列。

    根据输入类型自动选择抽法：

    - 鸭子类型有 :attr:`l5_outcome` 字段 → :func:`extract_from_browser_result`
      （mediabrowse 入口）
    - 鸭子类型有 ``outcomes``（属性或键）→ :func:`extract_from_p1_archive`
      （run 路径——既吃 ``archive_dict`` 也吃 :class:`RunRecord` dataclass）

    入口写在 :mod:`executor.cli` 的两处主循环末尾；这条 hook 把
    「判类型 → 抽 → 追加」三步绑成一个调用，便于单测覆盖与将来拓展
    （例如加第二批产物的入口）。

    ⚠️ **不在验证工具里挂**——proberun / staticprobe / mediaprobe
    的产物体裁与生产链不同，它们是**验证工具**，产物常含「工具特性」
    而非「真实业务分歧」，派发会让复核队列长出一批噪声。
    """
    if hasattr(result_or_archive, "l5_outcome") and not isinstance(
        result_or_archive, Mapping
    ):
        items = extract_from_browser_result(result_or_archive, task_id=task_id)
    else:
        # 鸭子：Mapping（有 ``outcomes`` 键）或 dataclass（有 ``outcomes`` 属性）
        if isinstance(result_or_archive, Mapping) and "outcomes" in result_or_archive:
            archive_dict: Mapping[str, Any] = result_or_archive
        elif hasattr(result_or_archive, "outcomes"):
            archive_dict = {
                "task_id": str(
                    getattr(result_or_archive, "task_id", "") or task_id
                ),
                "outcomes": list(getattr(result_or_archive, "outcomes", []) or []),
                "visits": list(getattr(result_or_archive, "visits", []) or []),
            }
        else:
            return 0, 0  # 未知形态，零派发（fail-closed 不报错）
        items = extract_from_p1_archive(archive_dict)
    path = Path(queue_path) if queue_path is not None else auto_queue_path()
    return append_to_queue(path, items)
