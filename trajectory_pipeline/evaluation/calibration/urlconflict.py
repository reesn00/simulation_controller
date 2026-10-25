"""URL 级金标读数（M-30）。

背景
----
D-10 的金标粒度按 **访问**（Q-11 裁定，2026-10-23）：同一 URL 的不同访问可以
拿到不同标签，这是**真事实**（站点漂移 / 地区 / 登录态），不是标注缺陷。

但它会污染读数。同一 URL 上的多头标签让「金标 → 分支」的对照失去单值性，
落在同一 URL 上的分歧卡既可能被读成 evaluator 的锅、也可能被读成金标的锅——
两个方向都解释得通时，分歧读数就不能单独拿来判方向。

所以本模块**只摆事实，不下判断**：

- :func:`project_d10_rows` —— 冻结行 → 本模块吃的最小投影（金标取自快照，
  与 D-10 读数同源，不另算一遍）；
- :func:`url_gold_conflicts` —— URL → 金标分布 / 档位 / 逐行明细；
- :func:`split_leakage` —— 切档按**行**不按 URL 造成的档间泄漏；
- :func:`render_conflicts_markdown` —— 人填结论的清单骨架（保留已填内容）。

**不判「金标对不对」**：那是语义判断（同一页面是否可能同时成立），只有人能做。
本模块不投票、不做多数表决——分歧必须暴露给人看。

只读派生
--------
不写冻结档、不改分诊档（``golden/data/triage/`` 是人填资产，往里加字段会让
「33/33」这个读数同时失真——见 ``docs/04-运行手册.md`` 的
「URL 级金标读数（M-30）」一节：「URL 级信息一律走派生读数」）。本模块的产物是**读数**
（命令行打印行 / ``--json-out`` 字段）与**清单骨架**
（``golden/data/url_gold_conflicts.md``：事实块由机器刷新，两列结论由人填）。

fail-closed
-----------
- URL 取不到时归 :data:`URL_UNKNOWN`（``"?"``），**不静默合并**——多个取不到
  URL 的行会聚成一个 ``?`` 桶，但那是显式的「取不到」，不是「同一个 URL」；
- 金标为空（``gold_value`` 假值）的行**不计入**冲突统计，但计入
  :func:`split_leakage` 的档位分布——泄漏是切档的事，与金标无关；
- 「样本是否在盘上」查不到时是 ``None``（**未检查**），不是 ``False``——
  两者混成同一个值，读的人会把「没查」当成「查了，没有」。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from trajectory_pipeline.evaluation.calibration.snapshot import HARD_DIM

#: URL 取不到时的归堆键。**显式的「取不到」**，不是某个具体站点。
URL_UNKNOWN = "?"

#: 清单里人填的两列的字段名（渲染与回读共用一份，不手抄两遍）。
VERDICT_FIELD = "定性"
EVIDENCE_FIELD = "裁定依据"

#: 结论列的占位值。空字符串表示「还没填」，与「填了但没内容」不是一回事。
VERDICT_TODO = "（待填）"

#: 人填的定性取值——**三个，不多不少**（见 ``docs/计划进展/10-TODO台账.md`` §M-30）。
#: 「待查」是合法的结论：证据不够就说证据不够，不许用排除法断言。
VERDICT_DRIFT = "漂移可解释"
VERDICT_IMPOSSIBLE = "不可能共存"
VERDICT_UNKNOWN = "待查"
VERDICT_CHOICES: tuple[str, ...] = (
    VERDICT_DRIFT,
    VERDICT_IMPOSSIBLE,
    VERDICT_UNKNOWN,
)

#: 清单节标题：`## 3 · \`url\``（回读时按这个形状认节）。
_SECTION_RE = re.compile(r"^##\s+\d+\s*·\s*`(?P<url>.+?)`\s*$")
#: 结论行：`- **定性**：xxx`
_VERDICT_RE = re.compile(r"^-\s*\*\*" + VERDICT_FIELD + r"\*\*[：:]\s*(?P<v>.*)$")
_EVIDENCE_RE = re.compile(r"^-\s*\*\*" + EVIDENCE_FIELD + r"\*\*[：:]\s*(?P<v>.*)$")


def _md_cell(value: Any) -> str:
    """表格单元格的转义——竖线会破表，反引号会吃掉整行。"""
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    text = text.replace("\r", " ").replace("\n", " ")
    return text.strip()


def project_d10_rows(
    entries: Sequence[Mapping[str, Any]],
    rows_by_id: Mapping[str, Mapping[str, Any]],
    *,
    on_disk: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """快照条目 + 冻结行 → 本模块吃的最小投影。

    **金标取自快照条目**（``gold_value``），不重新读一遍 ``golden.label``——
    两处各算一遍必然漂，而漂了以后「快照说 19 个 URL 多头、这里说 17 个」没人
    说得清谁对。快照没有的行（无金标）``gold_value`` 留 ``None``。

    ``rows_by_id`` 是冻结行按 ``golden_id`` 的索引，只用来补**人填的**字段
    （审核人 / 审核时间 / 金标备注）与代码侧事实。

    ``on_disk`` 是 ``golden_id → bool``；查不到的行是 ``None``（未检查）。
    """
    projected: list[dict[str, Any]] = []
    for entry in entries:
        if str(entry.get("dimension") or "") != HARD_DIM:
            continue
        gid = str(entry.get("golden_id") or "")
        row = rows_by_id.get(gid) or {}
        golden = row.get("golden") or {}
        code = row.get("code") or {}
        gold_value = entry.get("gold_value")
        projected.append(
            {
                "golden_id": gid,
                "dimension": HARD_DIM,
                "gold_value": str(gold_value) if gold_value else None,
                "split": str(entry.get("split") or ""),
                "reviewed_by": str(golden.get("reviewed_by") or ""),
                "reviewed_at": str(golden.get("reviewed_at") or ""),
                "note": str(golden.get("note") or ""),
                "pred_value": str(entry.get("pred_value") or ""),
                "code_source": str(code.get("source") or ""),
                "reached_play_page": code.get("reached_play_page"),
                "fallback_used": code.get("fallback_used"),
                "on_disk": (on_disk or {}).get(gid),
            }
        )
    return projected


def url_gold_conflicts(
    rows: Sequence[Mapping[str, Any]],
    url_of: Mapping[str, str],
) -> dict[str, dict]:
    """URL → ``{labels, splits, rows, detail}``。

    只统计 ``dimension == d10`` 且 ``gold_value`` 非空的行；多头 =
    ``len(labels) > 1``。纯函数，吃 dict 不吃文件。

    ``rows`` 是 :func:`project_d10_rows` 的产物；``url_of`` 是
    ``golden_id → url`` 的映射，**取不到时归** :data:`URL_UNKNOWN`
    （不静默合并——见模块 docstring 的 fail-closed）。

    每个 URL 的 ``rows`` 是工单约定的五元组
    ``(golden_id, split, label, reviewed_by, reviewed_at)``；``detail``
    是同一批行的全字段版（渲染清单用）。
    """
    conflicts: dict[str, dict] = {}
    for row in rows:
        if str(row.get("dimension") or "") != HARD_DIM:
            continue
        label = row.get("gold_value")
        if not label:
            continue
        gid = str(row.get("golden_id") or "")
        url = url_of.get(gid) or URL_UNKNOWN
        bucket = conflicts.setdefault(
            url,
            {"labels": {}, "splits": [], "rows": [], "detail": []},
        )
        bucket["labels"][str(label)] = bucket["labels"].get(str(label), 0) + 1
        split = str(row.get("split") or "")
        if split and split not in bucket["splits"]:
            bucket["splits"].append(split)
        bucket["rows"].append(
            (
                gid,
                split,
                str(label),
                str(row.get("reviewed_by") or ""),
                str(row.get("reviewed_at") or ""),
            )
        )
        bucket["detail"].append(dict(row))
    for bucket in conflicts.values():
        bucket["splits"] = sorted(bucket["splits"])
        bucket["labels"] = dict(
            sorted(bucket["labels"].items(), key=lambda kv: (-kv[1], kv[0]))
        )
    return conflicts


def summarize_conflicts(
    conflicts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """冲突读数的计数视图（报表 / 断言用；不重算判定）。"""
    n_urls = len(conflicts)
    multi = {u for u, c in conflicts.items() if len(c.get("labels") or {}) > 1}
    return {
        "urls_with_gold": n_urls,
        "urls_multi_label": len(multi),
        "multi_label_urls": sorted(multi),
        "rows_total": sum(len(c.get("rows") or []) for c in conflicts.values()),
    }


def split_leakage(
    rows: Sequence[Mapping[str, Any]],
    url_of: Mapping[str, str],
) -> dict[str, Any]:
    """切档按**行**不按 URL 造成的档间泄漏。

    切档是按行切的（``splits.py`` 的 ``_stratified_split``），而同一 URL 可以
    落进不止一档 ⇒ 持有集里可能有行与调参池/回归集**同 URL** ⇒ 持有集不再
    独立，「持有集 N 条」不能读成「N 条独立证据」（Q-12 裁定：按行切，泄漏
    保留并写进所有持有集读数的附注）。

    ``rows`` 与 :func:`url_gold_conflicts` 同一批投影行——**金标为空也计入**
    （泄漏是切档的事，与金标无关）。
    """
    by_split: dict[str, int] = {}
    url_splits: dict[str, set[str]] = {}
    for row in rows:
        split = str(row.get("split") or "")
        if not split:
            continue
        by_split[split] = by_split.get(split, 0) + 1
        gid = str(row.get("golden_id") or "")
        url = url_of.get(gid) or URL_UNKNOWN
        url_splits.setdefault(url, set()).add(split)

    cross = {u: sorted(s) for u, s in url_splits.items() if len(s) > 1}
    holdout_rows = by_split.get("holdout", 0)
    holdout_leak = 0
    for row in rows:
        if str(row.get("split") or "") != "holdout":
            continue
        gid = str(row.get("golden_id") or "")
        url = url_of.get(gid) or URL_UNKNOWN
        if len(url_splits.get(url, ())) > 1:
            holdout_leak += 1
    return {
        "n_rows": sum(by_split.values()),
        "by_split": dict(sorted(by_split.items())),
        "n_urls": len(url_splits),
        "cross_split_urls": len(cross),
        "cross_split_url_detail": dict(sorted(cross.items())),
        "holdout_rows": holdout_rows,
        "holdout_rows_on_cross_split_urls": holdout_leak,
        "holdout_leak_ratio": (
            holdout_leak / holdout_rows if holdout_rows else None
        ),
    }


def parse_verdicts_markdown(text: str) -> dict[str, dict[str, str]]:
    """回读清单里**人填的两列**（URL → ``{定性, 裁定依据}``）。

    只认本模块自己生成的形状（``## N · `url``` + ``- **定性**：``）。认不出的
    节一律跳过——**不猜、不按行号对齐**（行号会随 URL 增删而漂）。
    """
    out: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in text.splitlines():
        section = _SECTION_RE.match(line)
        if section:
            current = section.group("url")
            out.setdefault(current, {VERDICT_FIELD: "", EVIDENCE_FIELD: ""})
            continue
        if current is None:
            continue
        m = _VERDICT_RE.match(line)
        if m:
            out[current][VERDICT_FIELD] = m.group("v").strip()
            continue
        m = _EVIDENCE_RE.match(line)
        if m:
            out[current][EVIDENCE_FIELD] = m.group("v").strip()
    return out


def _fmt_labels(labels: Mapping[str, int]) -> str:
    return " / ".join(f"{k} {v}" for k, v in labels.items())


def render_conflicts_markdown(
    conflicts: Mapping[str, Mapping[str, Any]],
    *,
    leakage: Mapping[str, Any] | None = None,
    verdicts: Mapping[str, Mapping[str, str]] | None = None,
    generated_by: str = "",
) -> str:
    """渲染人填结论的清单骨架。

    **事实块由机器刷新，两列结论由人填**——重跑只换事实，已填内容原样带过去
    （``verdicts`` 参数；调用方负责先 :func:`parse_verdicts_markdown` 回读旧文件）。
    机器不许抹掉人的结论。

    排序按行数降序、URL 升序——稳定、可 diff。
    """
    verdicts = verdicts or {}
    summary = summarize_conflicts(conflicts)
    lines: list[str] = []
    lines.append("# URL 级金标冲突清单（M-30）")
    lines.append("")
    lines.append(
        "> **机器生成的事实块 + 人填的两列结论**。事实块由 "
        "`agreement --emit-url-conflicts` 刷新；"
        "**「定性」「裁定依据」两列由人填，机器重跑不会抹掉**。"
    )
    lines.append("")
    if generated_by:
        lines.append(f"> 生成命令：`{generated_by}`")
        lines.append("")
    lines.append(
        f"> 带 d10 金标的 URL **{summary['urls_with_gold']}** 个，其中 "
        f"**{summary['urls_multi_label']}** 个带多于一种标签；"
        "**本清单只列这多头的那些**（单头的没有冲突可定性）。"
    )
    if leakage:
        lines.append(
            f"> 切档按**行**不按 URL：跨档 URL **{leakage.get('cross_split_urls')}** 个；"
            f"持有集 **{leakage.get('holdout_rows_on_cross_split_urls')}/"
            f"{leakage.get('holdout_rows')}** 行与调参池/回归集同 URL"
            "（Q-12 裁定按行切 ⇒ 泄漏保留，所有持有集读数须带此附注）。"
        )
    lines.append("")
    lines.append(
        "> **「定性」三选一**："
        f"`{VERDICT_DRIFT}` / `{VERDICT_IMPOSSIBLE}` / `{VERDICT_UNKNOWN}`。"
        "证据不够就填「待查」——**不许用排除法断言**"
        "（同一 URL 两次访问结果不同可能是站点漂移、地区、登录态，那是真事实）。"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    ordered = sorted(
        (
            (u, b)
            for u, b in conflicts.items()
            if len(b.get("labels") or {}) > 1
        ),
        key=lambda kv: (-len(kv[1].get("rows") or []), kv[0]),
    )
    for idx, (url, bucket) in enumerate(ordered, start=1):
        labels = bucket.get("labels") or {}
        splits = bucket.get("splits") or []
        cross = len(splits) > 1
        lines.append(f"## {idx} · `{url}`")
        lines.append("")
        lines.append(f"- **行数**：{len(bucket.get('rows') or [])}")
        lines.append(f"- **金标分布**：{_fmt_labels(labels)}")
        lines.append(
            f"- **档位**：{' / '.join(splits) or '未标注'}"
            f"{'（跨档）' if cross else ''}"
        )
        v = verdicts.get(url) or {}
        lines.append(
            f"- **{VERDICT_FIELD}**：{v.get(VERDICT_FIELD) or VERDICT_TODO}"
        )
        lines.append(
            f"- **{EVIDENCE_FIELD}**：{v.get(EVIDENCE_FIELD) or VERDICT_TODO}"
        )
        lines.append("")
        lines.append(
            "| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | "
            "code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |"
        )
        lines.append(
            "|---|---|---|---|---|---|---|---|---|---|---|"
        )
        for detail in bucket.get("detail") or []:
            on_disk = detail.get("on_disk")
            on_disk_text = (
                "未检查" if on_disk is None else ("是" if on_disk else "否")
            )
            lines.append(
                "| `{gid}` | {split} | {gold} | {pred} | {by} | {at} | {src} | "
                "{rpp} | {fb} | {disk} | {note} |".format(
                    gid=_md_cell(detail.get("golden_id")),
                    split=_md_cell(detail.get("split")),
                    gold=_md_cell(detail.get("gold_value")),
                    pred=_md_cell(detail.get("pred_value")),
                    by=_md_cell(detail.get("reviewed_by")),
                    at=_md_cell(detail.get("reviewed_at")),
                    src=_md_cell(detail.get("code_source")),
                    rpp=_md_cell(detail.get("reached_play_page")),
                    fb=_md_cell(detail.get("fallback_used")),
                    disk=on_disk_text,
                    note=_md_cell(detail.get("note")),
                )
            )
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


__all__ = [
    "EVIDENCE_FIELD",
    "URL_UNKNOWN",
    "VERDICT_CHOICES",
    "VERDICT_DRIFT",
    "VERDICT_FIELD",
    "VERDICT_IMPOSSIBLE",
    "VERDICT_TODO",
    "VERDICT_UNKNOWN",
    "parse_verdicts_markdown",
    "project_d10_rows",
    "render_conflicts_markdown",
    "split_leakage",
    "summarize_conflicts",
    "url_gold_conflicts",
]
