"""人工复核队列——W1 正样本的**唯一来源**。

为什么需要它
------------
W1 的 ``RulePerceptor`` 对 ``PLAYER_OK`` 只认 ``<video>`` / ``<audio>`` 标签，
而真实视频站（优酷 / 爱奇艺 / 西瓜）全是 **iframe / JS 播放器**，不产生这些标签。
于是判断点 ④ 对它们一律返回 ``answer=None`` → ``unresolved``。

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
确认不行的落到真负样本分支（``component_unverified`` 或 ``no_play_control``，
按 ``reached`` 区分）。**回填保留 ``reviewed_by`` 与原始 branch**，
不覆盖——人工判定要能追溯，且将来能测"人工与代码的分歧率"（D 的下钻）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Mapping

from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES

#: 人工可给的裁定。**枚举值定死**是因为下游要按它分派，
#: 自由文本裁定会退化成"复核员写什么算什么"，无法统计也无法回填。
VERDICTS: Final[dict[str, str]] = {
    "confirm_success": "人工确认：这里确实能看（记为成功样本）",
    "confirm_negative": "人工确认：这里确实看不了（记为负样本）",
    "trailer": "人工确认：只有预告片（记为 trailer_only 负样本）",
    "need_browser": "存档证据不足，需人工开浏览器再看（暂不裁定）",
    "skip": "跳过（不可复核，如已失效页面）",
}

#: 进入复核队列的分支。只有这三类需要人看。
#:
#: - ``unresolved``：代码判不出来（含判断点 ④ 的 ``None``）
#: - ``trailer_suspect``：词表判不准，需要语义判断（方案里本来就设计了人工兜底）
#: - 真负样本**不进队列**：代码已给出确定性结论，复核它们属于抽检（另一件事）。
REVIEW_BRANCHES: Final = frozenset({"unresolved", "trailer_suspect"})

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
    body_preview: str
    degraded: tuple[str, ...]
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
        elements = tuple(
            {"ref": str(e.get("ref", "")), "tag": str(e.get("tag", "")),
             "label": str(e.get("label", ""))[:60]}
            for e in list(obs.get("interactive_elements") or [])[:ELEMENT_SAMPLE]
            if isinstance(e, Mapping)
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
            body_preview=str(obs.get("body_preview") or "")[:200],
            degraded=tuple(str(d) for d in (obs.get("degraded") or [])),
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

    未裁定的项**保持原样**并计入 ``unreviewed``，不默认成成功也不默认丢弃。
    """
    out = dict(archive)
    stats = {"confirm_success": 0, "confirm_negative": 0,
             "trailer": 0, "skipped": 0, "unreviewed": 0, "ignored": 0}
    new_outcomes: list[Mapping[str, Any]] = []

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
        rec["branch_before_review"] = outcome.get("branch")

        if verdict == "confirm_success":
            rec["branch"] = None
            rec["is_negative_sample"] = False
            rec["source"] = "human"
            stats["confirm_success"] += 1
        elif verdict == "confirm_negative":
            rec["branch"] = (
                "component_unverified" if rec.get("reached_play_page")
                else "no_play_control"
            )
            rec["is_negative_sample"] = True
            rec["source"] = "human"
            stats["confirm_negative"] += 1
        elif verdict == "trailer":
            rec["branch"] = "trailer_only"
            rec["is_negative_sample"] = True
            rec["source"] = "human"
            stats["trailer"] += 1
        else:                     # need_browser / skip：不动分支，只记状态
            rec["review_pending"] = True
            stats["skipped"] += 1
        new_outcomes.append(rec)

    out["outcomes"] = new_outcomes
    out["review_stats"] = stats
    out["review_note"] = (
        "人工裁定已回填。branch_before_review 保留代码原始判定，"
        "source=human 标记人工来源——两者都留着才能测人工与代码的分歧率。"
    )
    return out, stats