"""缺件负样本线——fail-closed 的**第三类**训练信号（H-41 阶段 3 · 方案 26 §6.4）。

为什么要有这条线
----------------

语义渲染（H-41 阶段 2）把 ``truncated.view`` 变成了**恒假**：本层一个字
都没截，正文是**模型自己挑过**的行。于是 ``[内容不完整: 本次裁剪]`` 这个
标记在语义模式下**不再出现**。

而那个标记是**有功能的**：它是教模型「看到缺件就说判不了」的载体
（见 :func:`trajectory_pipeline.assembler.views.render_observation`
的 docstring）。标记没了 ⇒ 模型被教成「永远敢下结论」。

方案 26 §6.4 定了三条迁移去处：

1. **采集降级**（``[采集降级: …]``）——真实数据里本来就有（``degraded`` 非空）；
2. **``observation_missing`` 占位**——真实数据里本来就有（没采到观察那一步）；
3. **故意给不完整观察的负样本线**——**真实数据里没有**，得**造**。

前两类是「本来就在」，第三类是「**要造**」——这就是本模块的活。

造什么
------

一条**负样本**：给模型看一条**故意不完整**的观察，期望它说「判不了」。

真实数据里的缺件样本太少（存量 P1 只有 **4.4%** 的观察带上游截断，
见方案 26 §6.5b）⇒ 靠真实数据教不出来 ⇒ 必须**专门造一条线**。

⚠️ **造出来的样本绝不能混进主训练池**：

- 主池的样本是**真实轨迹**（真站点、真结论）⇒ 混进「人为阉割过的观察」
  就是在教模型「这个站点没有播放控件」——那是**假事实**；
- 所以这条线**单独一个目录**（``failclosed_samples/``），与 ``samples/``
  平级但**不同名** ⇒ 主池的读侧（``storage.read_p3.read_views`` 之类）
  **扫不到它**。

那它长什么样
------------

一条 P2 形状的样本，与主池的样本**同构**（同一个 ``Sample.to_json``
形状、同一套渲染、同一套收尾话术），差别只有三处：

1. **最后一步的观察被故意弄成不完整的**（三种方式，见 :data:`CUT_MODES`）；
   ⇒ 回执里**如实**出现 ``[内容不完整: 上游截断]`` 或 ``[采集降级: …]``；
2. **轨迹在缺件那一步截断**（后面的动作与观察都拿掉）⇒ 一条
   fail-closed 的 agent 是「看到缺件就停并如实说」，不是「看到缺件
   还继续点下去」；
3. **结论改判成 fail-closed 分支**（``unresolved``）⇒ 收尾话术由
   :mod:`trajectory_pipeline.assembler.closing` **派生**（不手写），
   于是那句「无法确认能否播放，不予记录」与主池里其他 fail-closed
   样本**逐字同款**。

⚠️ **期望答案不是手写的**：它由 ``outcome.branch = "unresolved"`` 经
:func:`trajectory_pipeline.assembler.closing.closing_for_run` 派生。
手写一份话术 ⇒ 两份话术会漂 ⇒ 训练语料里出现「两种说法」⇒ 模型学到
「看情况换说法」。

不造的东西
----------

- **不造新的事实**：期望答案里没有任何「这个站点是什么」的信息——
  只有「我判不了」。这与「让模型选、不让模型写」同一条纪律
  （方案 26 §6.2）：**只可能漏，不可能造**。
- **不改正文措辞**：被砍掉的那半截正文是真的少了一截 ⇒ 标记说
  「上游截断」；元素表没了 ⇒ 标记说「采集降级」。**标记必须是真的**，
  否则这条线教的是「标记可以乱写」。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: **fail-closed 分支**——感知层「没判出来」的那一支。
#:
#: ⚠️ **必须与 :data:`trajectory_pipeline.assembler.closing.CLOSING_DISPOSITION`
#: 里那一支同名**：收尾话术按分支名查词表 ⇒ 名字对不上 ⇒ 收尾落到
#: ``UNKNOWN_REASON``（「原因未能确认，排除」）⇒ 这条线教出来的
#: fail-closed 说法与主池里的**不一样** ⇒ 模型学到两种说法。
#:
#: ⚠️ **它不是「真负样本分支」**：:data:`trajectory_pipeline.assembler.
#: builder.NEGATIVE_SAMPLE_BRANCHES` 那一套是**站点性质**的分类
#: （「这个站不是播放站」），而 ``unresolved`` 是**判不出来**——
#: 「这里看不了」与「没判出来」是两件事（避坑指南 §5.1）。
FAIL_CLOSED_BRANCH = "unresolved"

#: **故意缺件的三种方式**。都对应**真实采集里会发生**的成因——
#: 造的样本必须像真的缺件，否则教出来的行为在真场景里对不上。
#:
#: - ``body_truncated``：**上游**把正文砍了（采集预算用尽）⇒
#:   回执里是 ``[内容不完整: 上游截断]``；
#: - ``elements_missing``：**交互元素表没采到** ⇒ 回执里是
#:   ``[采集降级: interactive_elements]``；
#: - ``body_missing``：**正文没采到** ⇒ 回执里是
#:   ``[采集降级: body_text]``。
#:
#: ⚠️ 三种都**必须留下标记**（:func:`degrade_observation` 负责）——
#: 缺标记的缺件观察教出来的是「模型看到的就是全部」⇒ 那是幻觉的训练。
CUT_MODES: tuple[str, ...] = (
    "body_truncated",
    "elements_missing",
    "body_missing",
)

#: 缺件方式 → 人读标签（报表用）。
CUT_MODE_LABELS: Mapping[str, str] = {
    "body_truncated": "正文被上游截断",
    "elements_missing": "交互元素表没采到",
    "body_missing": "正文没采到",
}

#: 缺件样本落盘时写进 ``degraded_from`` 的标记。
#:
#: ⚠️ **它是「这条是造出来的」的唯一留痕**——读样本的人看到一条
#: 「被判不了」的样本，必须能一眼看出它是**故意缺件**的，不是真实跑出来的。
FAIL_CLOSED_MARKER = "failclosed_line"

#: 缺件样本的 ``sample_id`` 后缀。
#:
#: ⚠️ **必须与原样本 id 不同**（``<原 id>__fc-<方式>``）——同 id 会覆盖
#: 主池里那条真实样本（同一个目录里）⇒ **真语料被假语料顶掉**，
#: 而文件系统的行为是「静默覆盖」：读的人只会看到少了一条。
LINE_ID_SUFFIX = "__fc"


def line_sample_id(sample_id: str, cut_mode: str) -> str:
    """缺件样本的 id：``<原样本 id>__fc-<缺件方式>``。"""
    if cut_mode not in CUT_MODES:
        raise ValueError(
            f"认不出的缺件方式 {cut_mode!r}（已知：{list(CUT_MODES)}）"
            "——不猜、不静默当成默认方式"
        )
    return f"{sample_id}{LINE_ID_SUFFIX}-{cut_mode}"


def _limits_of(obs: Mapping[str, Any]) -> dict[str, Any]:
    """观察的 ``limits``（**7 键形状**不许变——见 P2 契约）。"""
    limits = obs.get("limits")
    if isinstance(limits, Mapping):
        return dict(limits)
    return {}


def _relimit(limits: Mapping[str, Any], *, changed: bool) -> dict[str, Any]:
    """按改动后的内容**重算** ``limits.cropped``。

    ⚠️ ``cropped`` 是「本层动没动过」的信号——砍掉一半正文之后它必须
    仍为真，否则读的人以为「这条没被处理过」。其余键**原样带过**
    （``limits`` 的 7 键形状是契约，见 ``docs/contracts/P2-sample.md``）。

    ⚠️ **不能只看那六个计数**：``body_chars=0``（正文被砍到空）是
    ``falsy`` ⇒ 光靠它算不出「动过」⇒ 必须**显式**传 ``changed``。
    （这正是「长度归 limits、信号归 truncated」那条纪律的例外处：被砍成
    空的那一次，**长度就是 0**，而 0 不携带「动过」这个信息。）
    """
    out = dict(limits)
    out["cropped"] = bool(
        changed
        or out.get("body_chars") or out.get("elements") or out.get("links")
        or out.get("body_lines_dropped") or out.get("elements_dropped")
        or out.get("links_dropped")
    )
    return out


def degrade_observation(
    obs: Mapping[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    """把一条**训练态**观察**故意**弄成不完整的，并**如实标记**。

    Args:
        obs: 训练态观察（:func:`trajectory_pipeline.assembler.
            observation_view.render` 的产物）。
        mode: 缺件方式，见 :data:`CUT_MODES`。

    Returns:
        **新的**观察 dict（原 dict 不动——P2 样本是证据，派生物不改原件）。

    ⚠️ **标记必须是真的**：缺件之后 ``truncated.source`` / ``degraded``
    必须**如实**写上，否则回执里不会出现 ``[内容不完整: …]`` /
    ``[采集降级: …]`` ⇒ 这条线教出来的就是「模型看到的就是全部」。
    """
    if mode not in CUT_MODES:
        raise ValueError(
            f"认不出的缺件方式 {mode!r}（已知：{list(CUT_MODES)}）"
            "——不猜、不静默当成默认方式"
        )
    out = dict(obs)
    degraded = [str(d) for d in (obs.get("degraded") or ())]
    trunc_raw = obs.get("truncated")
    #: ⚠️ **永远保持 2 键形状**（``source`` / ``view``——见 P2 契约）。
    #: P1 存档里的 ``truncated`` 是 **bool**，直接搬过来会变成 1 键 ⇒
    #: 下游 ``_truncated_branch`` 只认这两个键 ⇒ 少一个就落进
    #: 「不认识的老形状」分支。
    trunc: dict[str, Any] = {
        "source": (
            bool(trunc_raw.get("source"))
            if isinstance(trunc_raw, Mapping) else bool(trunc_raw)
        ),
        "view": (
            bool(trunc_raw.get("view"))
            if isinstance(trunc_raw, Mapping) else False
        ),
    }
    limits = _limits_of(obs)
    changed = False
    #: **正文有没有被本模块动过**（动过 ⇒ 精炼留痕要重算）。
    body_changed = False

    if mode == "body_truncated":
        body = str(obs.get("body_text") or "")
        # ⚠️ **按字符砍前缀，不按行砍**——真实的「上游截断」是
        # **采集预算用尽**（``dom.DEFAULT_MAX_CHARS``）⇒ 切在**字符**上
        # ⇒ 尾部那半截没了、最后一行还可能是**半行**。按行砍会造出
        # 「整行整行地少」这种真实采集里不存在的形态。
        #
        # ⚠️ **砍前缀不是砍后缀**：砍后缀会**删掉动作目标的行**（keep 命中
        # 的行在尾部）⇒ 观察里出现「点了但观察里没有的东西」⇒ 教模型
        # 编目标。砍前缀保住了前部所有整行 ⇒ 「只可能漏，不可能造」。
        keep = len(body) // 2
        new_body = body[:keep]
        changed = keep < len(body)
        body_changed = True
        out["body_text"] = new_body
        out["body_chars"] = len(new_body)
        # ⚠️ **只置 ``source``，不动 ``view``**：``view`` 说的是「本层
        # 训练态渲染裁掉了」——那是规则层的事，与本模块无关。改了它
        # 就是**谎报**（这条线自己的纪律就是「标记必须是真的」）。
        trunc["source"] = True
        limits["body_chars"] = len(new_body)
    elif mode == "elements_missing":
        removed = list(obs.get("interactive_elements") or ())
        out["interactive_elements"] = []
        if "interactive_elements" not in degraded:
            degraded.append("interactive_elements")
        # ⚠️ ``elements``（那个上限）**不再成立**：一个元素都没剩 ⇒
        # 触没触顶无从说起 ⇒ 置空；被拿掉的条数记进 ``elements_dropped``。
        limits["elements"] = None
        limits["elements_dropped"] = len(removed)
        changed = bool(removed)
    else:                                    # body_missing
        body = str(obs.get("body_text") or "")
        out["body_text"] = ""
        out["body_chars"] = 0
        if "body_text" not in degraded:
            degraded.append("body_text")
        # ⚠️ **记 0 而不是 ``None``**：``None`` 的意思是「**本层没动过
        # 正文**」，而这里**动了**（动到空）⇒ 记 ``None`` 就是谎报。
        # 「动过」由 ``cropped`` 表达（0 是 falsy ⇒ 靠 ``changed`` 兜）。
        limits["body_chars"] = 0
        changed = bool(body.strip())
        body_changed = True

    if body_changed:
        # ⚠️ **精炼留痕要跟着重算**——否则回执里会出现
        # 「正文：（空）」与「[内容已精简: 保留 6 行]」**并排**⇒
        # 自相矛盾 ⇒ 模型学到「标记可以乱写」。
        #
        # - ``body_truncated``：正文还在（只是被砍短了）⇒ **行数按实际
        #   留下的算**（模型选的那几行现在只剩这么多了）；
        # - ``body_missing``：**压根没有正文** ⇒ 与真实的
        #   ``observation_missing`` 占位**同款**（``body_source`` 记
        #   ``missing``、精炼留痕清空）——那才是「正文没采到」的真实形状。
        if out["body_text"]:
            out["body_refined_lines"] = len([
                ln for ln in str(out["body_text"]).splitlines() if ln.strip()
            ])
        else:
            out["body_source"] = "missing"
            out["body_refined"] = False
            out["body_refined_lines"] = 0
            out["body_refined_dropped"] = 0

    out["degraded"] = degraded
    out["truncated"] = trunc
    out["limits"] = _relimit(limits, changed=changed)
    return out


def _has_body(obs: Mapping[str, Any]) -> bool:
    return bool(str(obs.get("body_text") or "").strip())


def _has_elements(obs: Mapping[str, Any]) -> bool:
    return bool(list(obs.get("interactive_elements") or ()))


def pick_cut_index(observations: Sequence[Mapping[str, Any]]) -> int:
    """该在哪一步**故意缺件**？返回下标；``-1`` = 这条样本切不出来。

    ⚠️ **取最后一条**「有内容」的观察——那是**决定性**那一步（模型最后
    看到的那个页面）。往前取 ⇒ 缺件发生在一个中间步骤 ⇒ 后面还有
    完整的观察 ⇒ 模型会以为「再看一次就好了」⇒ 学不到
    「缺件了就停」。

    ⚠️ **只取有内容的**：``observation_missing`` 占位（没采到观察）
    那一步**本来就不完整** ⇒ 不需要再故意弄一次（而且那样造出来的
    样本与真实数据里的那类重复）。

    ⚠️ **优先取「正文与元素都有」的那一步**（2026-11-18 补）：三种缺件
    方式里,``body_truncated`` / ``body_missing`` 要**正文**、
    ``elements_missing`` 要**元素** ⇒ 挑一条两样都有的 ⇒ **三种方式
    各自都真的少东西**。
    ⇒ 挑不到两样都有的就退而取「有正文的」（两种正文方式成立，
    ``elements_missing`` 只留标记）⇒ 再退「有元素的」。

    ⚠️ **为什么「少东西」这么要紧**：标记说「上游截断」而正文其实
    完整 ⇒ 模型学到「标记可以乱写」⇒ **前面两条真实的缺件信号
    （采集降级 / 上游截断）也一起废掉**。⇒ 宁可少造一条样本，
    不可造一条「标记与观察自相矛盾」的样本。
    """
    both = -1
    body = -1
    elements = -1
    for i in range(len(observations) - 1, -1, -1):
        obs = observations[i]
        if not isinstance(obs, Mapping):
            continue
        has_b = _has_body(obs)
        has_e = _has_elements(obs)
        if has_b and has_e:
            both = i
            break
        if has_b and body < 0:
            body = i
        if has_e and elements < 0:
            elements = i
    if both >= 0:
        return both
    if body >= 0:
        return body
    return elements


def _as_fail_closed(outcome: Any) -> Any:
    """一条 outcome → **fail-closed 判据**的版本。

    ⚠️ ``branch`` 改成 :data:`FAIL_CLOSED_BRANCH`；``succeeded`` 改成
    ``False``——两者必须一起改：「分支说判不了、却记着成功」是一条
    **自相矛盾**的结论，而收尾话术（:func:`trajectory_pipeline.assembler.
    closing.closing_for_run`）按 ``branch is None`` 判成功 ⇒ 只改分支
    就够，但 ``succeeded`` 会留在下游的判读里说「成功」⇒ 报表上
    「成功 1 条、判不了 1 条」。

    ⚠️ **不改** ``url`` / ``site_url`` / ``play_page_url``——它们是
    **真的**（确实访问过那个地址）。改了就成编事实。
    """
    if not isinstance(outcome, Mapping):
        return outcome
    out = dict(outcome)
    out["branch"] = FAIL_CLOSED_BRANCH
    out["succeeded"] = False
    return out


def _site_row(row: Mapping[str, Any], *, cut_index: int) -> dict[str, Any]:
    """一个站点级结论 → 缺件变体。

    ⚠️ **只有落在缺件那一步上的站点**才改成 fail-closed——**别的站点
    保留它们**真实的结论**。

    理由：那些站点**确实**被访问过、确实被排除过（原因是真的）。
    一并改成「无法确认」＝**改写真实事实** ⇒ 训练语料里出现「这个站
    判不出来」而真相是「这个站确实不是播放站」⇒ 学到**假事实**。
    ⇒ 只改**决定性那一个**：它正是缺件那一步对应的站点。
    """
    out = dict(row)
    if not isinstance(row.get("outcome"), Mapping):
        return out
    try:
        start = int(row.get("step_start") or 0)
        end = int(row.get("step_end") or 0)
    except (TypeError, ValueError):
        return out
    # ⚠️ 区间是**动作步**的 ``[step_start, step_end)``；观察与动作**按
    # 下标对齐**（P2 契约）⇒ 同一个下标就是同一步。
    if start <= cut_index < end:
        out["outcome"] = _as_fail_closed(row["outcome"])
    return out


def build_line_payload(
    payload: Mapping[str, Any],
    *,
    cut_mode: str,
    cut_index: int,
) -> dict[str, Any]:
    """一份**已渲染**的 P2 样本 → 它的**缺件负样本**版本。

    Args:
        payload: :func:`trajectory_pipeline.assembler.builder._sample_to_json`
            的产物（**已渲染**的训练态观察）。
        cut_mode: 缺件方式，见 :data:`CUT_MODES`。
        cut_index: 在第几步缺件（见 :func:`pick_cut_index`）。

    Returns:
        **新的** P2 形状 dict（原 payload 不动）。
    """
    if cut_mode not in CUT_MODES:
        raise ValueError(
            f"认不出的缺件方式 {cut_mode!r}（已知：{list(CUT_MODES)}）"
            "——不猜、不静默当成默认方式"
        )
    out = dict(payload)
    six = dict(payload.get("six") or {})

    observations = [dict(o) for o in (six.get("observations") or ())
                    if isinstance(o, Mapping)]
    actions = list(six.get("actions") or ())
    rationales = list(six.get("rationales") or ())

    # ⚠️ **轨迹在缺件那一步截断**：fail-closed 的 agent 是「看到缺件
    # 就停并如实说」，不是「看到缺件还继续点下去」。留着后面的动作
    # ⇒ 这一条教出来的是「缺件了也能一路点到底」。
    observations = observations[: cut_index + 1]
    actions = actions[: cut_index + 1]
    rationales = [
        r for r in rationales
        if isinstance(r, Mapping)
        and int(r.get("step_index") or 0) <= cut_index
    ]
    if observations:
        observations[cut_index] = degrade_observation(
            observations[cut_index], mode=cut_mode,
        )

    six["observations"] = observations
    six["actions"] = actions
    six["rationales"] = rationales
    out["six"] = six

    # ⚠️ **结论改判成 fail-closed**：期望答案（收尾话术）由它**派生**，
    # 不手写——手写会出现两份话术 ⇒ 模型学到两种说法。
    out["outcome"] = _as_fail_closed(payload.get("outcome"))
    site_outcomes = []
    for s in payload.get("site_outcomes") or ():
        if not isinstance(s, Mapping):
            continue
        site_outcomes.append(_site_row(s, cut_index=cut_index))
    out["site_outcomes"] = site_outcomes

    # ⚠️ **留痕**（「这条是造出来的」）：`degraded_from` 是 P2 契约里的
    # 既有键（读样本的人本来就会看它）⇒ 复用它，不新开顶层键。
    degraded_from = [str(d) for d in (payload.get("degraded_from") or ())]
    if FAIL_CLOSED_MARKER not in degraded_from:
        degraded_from.append(FAIL_CLOSED_MARKER)
    out["degraded_from"] = degraded_from
    # ⚠️ **缺件方式与位置**单独记：读的人要能一眼看出「缺在哪、缺的是
    # 什么」——否则「造出来的」只说明它是造的，不说明它造得对不对。
    out["failclosed_cut_mode"] = cut_mode
    out["failclosed_cut_step"] = cut_index
    out["sample_id"] = line_sample_id(
        str(payload.get("sample_id") or ""), cut_mode,
    )
    return out


__all__ = [
    "CUT_MODES",
    "CUT_MODE_LABELS",
    "FAIL_CLOSED_BRANCH",
    "FAIL_CLOSED_MARKER",
    "LINE_ID_SUFFIX",
    "build_line_payload",
    "degrade_observation",
    "line_sample_id",
    "pick_cut_index",
]
