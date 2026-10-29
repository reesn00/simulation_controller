"""golden_ls.report: 推送计划的**读数视图**（方案 22 §1 的口径）。

这里算的五个数是「推之前必须对得上」的：

===========================  ==============================================
分档分布                     三档各多少条（**三档全传**，裁定③）
一致率                       **两个口径都报**（见下）
现场可得率                   切到 P1 存档的比例
T 视图可得率                 切到 per-Run 任务视图的比例
变更分布                     ``new`` / ``changed`` / ``id_migrated`` / ``unchanged``
===========================  ==============================================

**为什么单独一个模块**：这几个数是「人核对推出去的东西对不对」的依据，
得能被单测直接调。塞进 CLI 里就只能靠跑命令看——而跑命令要人读屏，
数字对不对当场看不出来。

一致率的**两个口径**（别混）
---------------------------
=====================  ======================  ==========================
口径                   分母                    v14 读数
=====================  ======================  ==========================
**严格一致**           **全部 294 行**         201 / 93 = 68.4%
（方案 §1.2 的数）      ``golden.label``        ——**逐字相等**
                       ``== code.branch``
**D-10 口径 C**        **站点级 289 行**       230 / 289 = 79.6%
（门禁的数）            **语义等价类相等**      ——``is_equivalent``
=====================  ======================  ==========================

两个数**都对**，但问的不是同一件事：

* 严格一致是**方案 §1 的基线数**（§1.2 那张表就是它算出来的），它是
  「两侧的标签字面一样吗」；
* D-10 口径 C 是**门禁的数**（``builder.cmd_agreement`` 就是它），它把
  ``blocked`` 与 ``login_wall_or_blocked`` 这类**语义相同但字面不同**的
  也算一致——新细分分支上线那天，字面相等会假性暴跌。

⚠️ **只报一个就是误导**：只报 79.6% 会让人以为 §1 的 68.4% 记错了；
只报 68.4% 又会拿一个不是门禁口径的数去对门禁。两个都摆出来，各标各的
分母与定义。

一致率的口径**不重算**
--------------------
D-10 那一半直接用 ``golden/alignment.is_equivalent``（口径 C 的唯一
出处）。另算一份等于两份映射必然漂，而漂了不报错——只是头条数字慢慢不对。

⚠️ **未登记标签要显式报出来**：``equiv_class`` 对未登记标签抛
``KeyError``（fail-closed）。本模块不吞掉它，而是把它计入
``unregistered`` 并**单列**——「标签没登记」和「判成不一致」是不同的事，
混进同一个分母就看不出「这批标签域变了」。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from trajectory_pipeline.evaluation.golden.alignment import is_equivalent
from trajectory_pipeline.evaluation.golden.builder import SUCCESS_LABEL
from trajectory_pipeline.evaluation.golden.splits import HEADLINE_SPLIT, SPLITS

from trajectory_pipeline.evaluation.golden_ls import diff as diff_mod


def _gold_label(row: Mapping[str, Any]) -> str:
    golden = row.get("golden") if isinstance(row.get("golden"), Mapping) else {}
    return str(golden.get("label") or SUCCESS_LABEL)


def _code_branch(row: Mapping[str, Any]) -> str:
    code = row.get("code") if isinstance(row.get("code"), Mapping) else {}
    return str(code.get("branch") or SUCCESS_LABEL)


def is_site_row(row: Mapping[str, Any]) -> bool:
    """站点级已标注行（**D-10 的分母口径**，与 ``builder.cmd_agreement`` 同）。

    轨迹级样本（``source_kind == "p2_sample"``）不计入——它们没有 code 判定。
    """
    return str(row.get("source_kind") or "") != "p2_sample" and bool(row.get("golden"))


def split_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """各档条数（**按 ``SPLITS`` 的固定顺序**，缺的补 0）。"""
    counts = {split: 0 for split in SPLITS}
    for row in rows:
        split = str(row.get("__split__") or "")
        if split in counts:
            counts[split] += 1
    return counts


def strict_agreement(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """**严格一致**（方案 §1.2 的口径）：``golden.label == code.branch``。

    分母是**全部行**（v14 是 294 条），**不是**站点级——§1.2 那张表就是
    这个口径算出来的（201 / 93 = 68.4%）。

    ⚠️ ``code`` 缺失（``code: None``）的行**算不一致**——与 §1.2 的量法
    一致（那边也是直接取 ``code.branch`` 比的）。这 5 行是轨迹级样本
    （``source_kind == "p2_sample"``），本来就没有代码判定。
    """
    total = len(rows)
    same = 0
    for row in rows:
        golden = row.get("golden") if isinstance(row.get("golden"), Mapping) else {}
        code = row.get("code") if isinstance(row.get("code"), Mapping) else {}
        if golden.get("label") == code.get("branch"):
            same += 1
    return {
        "total": total,
        "agree": same,
        "disagree": total - same,
        "ratio": (same / total) if total else 0.0,
    }


def d10_agreement(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """D-10 口径 C 一致率（**语义等价类**）。

    返回::

        {"split": ..., "total": N, "agree": K, "ratio": r, "unregistered": U,
         "headline": {"total": ..., "agree": ..., "ratio": ..., ...}}

    分母是**站点级已标注行**（轨迹级没有代码判定，不进分母），与
    ``builder.cmd_agreement`` 同。头条另算**调参池**（``HEADLINE_SPLIT``）。

    ``unregistered`` 是**单列**的：标签不在 ``EQUIV_CLASS`` 里 ⇒ 判不了
    等价（``equiv_class`` fail-closed 抛 ``KeyError``）。这些行**不进
    ``agree`` 的分子**，但**留在分母里**——分母是该档该判的行数，缩小它
    等于把「判不了」读成「判对了」。
    """
    site = [row for row in rows if is_site_row(row)]
    has_split = any(row.get("__split__") for row in site)
    headline_rows = (
        [row for row in site if row.get("__split__") == HEADLINE_SPLIT]
        if has_split
        else site
    )

    def _score(subset: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        agree = 0
        unregistered = 0
        for row in subset:
            try:
                if is_equivalent(_gold_label(row), _code_branch(row)):
                    agree += 1
            except KeyError:
                unregistered += 1
        total = len(subset)
        return {
            "total": total,
            "agree": agree,
            "disagree": total - agree,
            "ratio": (agree / total) if total else 0.0,
            "unregistered": unregistered,
        }

    overall = _score(site)
    headline = _score(headline_rows)
    return {
        "split": HEADLINE_SPLIT if has_split else "（未切档）",
        "total": overall["total"],
        "agree": overall["agree"],
        "disagree": overall["disagree"],
        "ratio": overall["ratio"],
        "unregistered": overall["unregistered"],
        "headline": headline,
    }


def coverage(stats: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """现场 / T 视图 / 金标署名的可得率（分母 = 本次导出的行数）。

    ⚠️ **「读不到」不是「没有」**：现场读不到的行在区块⑥里明写「没读到
    现场」+ 定位说明（见 exporter 的渲染纪律）。这里的 ``scene_found``
    只是**覆盖率**的读数，不是「页面里没有播放控件」的结论。

    金标署名分三档（``golden.signatures`` 的分类）：``human``（人署的名）
    / ``automated``（自动验证器、AI 辅助——**没人逐条看过**）/ ``unknown``
    （压根没署名）。
    """
    total = len(stats)
    scene = sum(1 for s in stats if s.get("scene_found"))
    trajectory = sum(1 for s in stats if s.get("trajectory_found"))
    human = sum(1 for s in stats if s.get("signature_kind") == "human")
    automated = sum(1 for s in stats if s.get("signature_kind") == "automated")
    unknown = sum(1 for s in stats if s.get("signature_kind") == "unknown")
    rejected = sum(1 for s in stats if s.get("rejected"))

    def _ratio(part: int) -> float:
        return (part / total) if total else 0.0

    return {
        "total": total,
        "scene_found": scene,
        "scene_ratio": _ratio(scene),
        "trajectory_found": trajectory,
        "trajectory_ratio": _ratio(trajectory),
        "signature_human": human,
        "signature_automated": automated,
        "signature_unknown": unknown,
        "rejected": rejected,
    }


def marker_distribution(
    rows: Sequence[Mapping[str, Any]], markers: Mapping[str, str]
) -> dict[str, int]:
    """变更标记分布（按 ``ALL_MARKERS`` 的固定顺序）。"""
    return diff_mod.summarize_markers(rows, markers)


def signature_distribution(stats: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """金标署名的**原始分布**（按署名值计数，降序）。

    ⚠️ **分类之前先看这个**：``human`` / ``automated`` 是**分类**，
    这里是**原始事实**。两者对不上时（比如新冒出一个署名），
    原始分布会先露出来——分类错了还能看出是哪个署名错了。
    """
    counts: dict[str, int] = {}
    for stat in stats:
        name = str(stat.get("signature") or "")
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def plan_report(
    *,
    version: int,
    rows: Sequence[Mapping[str, Any]],
    markers: Mapping[str, str],
    stats: Sequence[Mapping[str, Any]],
    selected_splits: Sequence[str],
    previous_version: int | None,
    planned: int,
    pushed: int,
) -> dict[str, Any]:
    """一次推送的**完整读数**（可打印、可存盘、可被测试断言）。

    两个口径**分开摆**，不混成一张表：

    * **版本级**（``splits`` / ``agreement`` / ``markers``）——这一版**全部**
      行的读数，与 ``builder agreement --version N`` 同源；
    * **推送级**（``coverage`` / ``planned`` / ``pushed``）——**本次实际导出**
      的那几行（``--splits`` / ``--limit`` 收窄过的话，分母就不是全版了）。
    """
    return {
        "version": version,
        "previous_version": previous_version,
        "selected_splits": list(selected_splits),
        "splits": split_counts(rows),
        "strict": strict_agreement(rows),
        "agreement": d10_agreement(rows),
        "coverage": coverage(stats),
        "signatures": signature_distribution(stats),
        "markers": marker_distribution(rows, markers),
        "planned": planned,
        "pushed": pushed,
    }


def render_report(report: Mapping[str, Any]) -> str:
    """读数 → 人读文本（**给人核对用**，不是给机器解析）。"""
    version = report.get("version")
    splits = report.get("splits") or {}
    strict = report.get("strict") or {}
    agreement = report.get("agreement") or {}
    cover = report.get("coverage") or {}
    markers = report.get("markers") or {}
    headline = agreement.get("headline") or {}

    lines = [
        f"── v{version} 推送计划读数 ──",
        (
            "分档分布(全版): "
            + " / ".join(f"{k}={v}" for k, v in splits.items())
            + f"（本次选中: {', '.join(report.get('selected_splits') or [])}）"
        ),
        (
            f"一致率·严格(全版 {strict.get('total')} 行, 逐字相等): "
            f"{strict.get('agree')}/{strict.get('total')} "
            f"= {float(strict.get('ratio') or 0):.1%}"
        ),
        (
            f"一致率·D-10 口径 C(站点级 {agreement.get('total')} 行, 等价类): "
            f"头条 {agreement.get('split')} "
            f"{headline.get('agree')}/{headline.get('total')} "
            f"= {float(headline.get('ratio') or 0):.1%} ｜ "
            f"三档合计 {agreement.get('agree')}/{agreement.get('total')} "
            f"= {float(agreement.get('ratio') or 0):.1%}"
        ),
    ]
    unregistered = int(agreement.get("unregistered") or 0)
    if unregistered:
        lines.append(
            f"⚠ 标签未登记(判不了等价) {unregistered} 条 —— 留在分母里, 单列"
        )
    lines.append(
        f"现场可得率(本次导出 {cover.get('total')} 条): "
        f"{cover.get('scene_found')}/{cover.get('total')} "
        f"= {float(cover.get('scene_ratio') or 0):.1%}"
        f" ｜ T 视图可得率: {cover.get('trajectory_found')}/{cover.get('total')} "
        f"= {float(cover.get('trajectory_ratio') or 0):.1%}"
    )
    lines.append(
        "金标署名: "
        f"人核 {cover.get('signature_human')} / 自动化 {cover.get('signature_automated')} "
        f"/ 无署名 {cover.get('signature_unknown')}"
    )
    signatures = report.get("signatures") or {}
    if signatures:
        lines.append(
            "署名分布: "
            + " / ".join(f"{k or '（空）'}={v}" for k, v in signatures.items())
        )
    lines.append(
        "变更分布(全版): "
        + " / ".join(f"{k}={v}" for k, v in markers.items() if not k.startswith("__"))
    )
    lines.append(
        f"计划 {report.get('planned')} 条 / 已推送 {report.get('pushed')} 条"
    )
    rejected = int(cover.get("rejected") or 0)
    if rejected:
        lines.append(f"⚠ 凭据扫描拒推 {rejected} 条（fail-closed, 不推半份）")
    return "\n".join(lines)


__all__ = [
    "coverage",
    "d10_agreement",
    "is_site_row",
    "marker_distribution",
    "plan_report",
    "render_report",
    "signature_distribution",
    "split_counts",
    "strict_agreement",
]
