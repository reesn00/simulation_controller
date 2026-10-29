"""golden_ls.exporter: 冻结黄金行 → LS ``task.data``（方案 22 §3.2）。

九个区块
--------
顺序 = 人读的顺序（结论在前，现场在后，长正文最后）::

    ① version_block    版本 / 冻结时刻 / 口径版本 / **金标署名** / 变更标记
    ② site_block       URL / 播放页 / task / run / golden_id / 所属档
    ③ gold_block       金标标签 + 备注
    ④ code_block       代码分支 + 证据原文
    ⑤ scores_block     维度分数
    ⑥ scene_block      现场读数（页面标题 / 每步观察 / 正文预览）
    ⑦ rubric_block     尺子（条款原文 / 分支表 / 前置事实 / 口径代）
    ⑧ trajectory_block T 视图（per-Run 完整任务视图）
    ⑨ persona_block    persona 六维

三条硬纪律（都在下面落地，不是口号）
------------------------------------
1. **读不到就说读不到。** 现场定位不到时写「⚠ 没读到现场（**不是**「页面里没有
   控件」）」+ 定位说明 + 存档里现有的候选——**空读数会被读成「页面里没有
   video 标签」**（``calibration/evidence.py`` 模块 docstring 的原话）。
2. **缺字段与没测是两回事。** 维度分数有三种状态：**字段不存在**（v1–v6 的
   冻结档没有顶层维度分数）/ **本行为空**（没测）/ **本行弃权**（``null``）。
   三者在快照里长得几乎一样，正是 M-29 要修的病——这里分开写。
3. **分支文本取行内的。** ④ 里的标签文本取 ``code.branch_label``（**判读当时
   的文本**）；⑦ 的分支表是**现行**的 22 类封闭集，只作选标签时的参照。
   拿现行表解释历史判定 = 拿新尺子量旧活（``executor/branches.py`` 的 M-44
   注释明说历史行不回填）。

本模块**只读本地文件、不联网**（现场来自 P1 存档、轨迹来自 T 视图）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trajectory_pipeline.evaluation.calibration import evidence
from trajectory_pipeline.evaluation.golden.signatures import classify_signature
from trajectory_pipeline.evaluation.golden_ls import diff as diff_mod
from trajectory_pipeline.evaluation.golden_ls.label_config import BLOCK_CONTROLS
from trajectory_pipeline.evaluation.ls_bridge.exporter import (
    ExportPlan,
    build_task,
    render_messages_text,
    render_meta_text,
    render_openai_text,
)
from trajectory_pipeline.evaluation.ls_bridge.scan import ScanHit, scan_for_credentials
from trajectory_pipeline.evaluation.ls_bridge.settings import CredentialScanSettings
from trajectory_pipeline.storage.task_views import read_task_view, task_views_dir

log = logging.getLogger(__name__)

#: ⑤ 维度分数块的三种状态（**别混**：M-29 的病就是它们长得一样）。
SCORES_ABSENT = "absent"      # 字段不存在（v1–v6）
SCORES_EMPTY = "empty"        # 字段存在但为空（没测）
SCORES_NULL = "null"          # 字段存在且为 null（弃权）
SCORES_FILLED = "filled"      # 有内容

#: 条款号：卡自己的条款（D-10 判「结论与 ground truth 一致」）+ 分支语义的两条。
DEFAULT_CLAUSES: tuple[str, ...] = ("D-10", "D-4", "D-5")

#: 机器署名的金标要**显眼**——v14 里只有 82/294 行是人署的名（方案 22 §1.3）。
#:
#: ⚠️ 分类**不手写规则**：``golden.signatures.classify_signature`` 是唯一
#: 出处（``human`` = 署名在 ``HUMAN_SIGNATURES`` 里；其余非空 = 自动化）。
#: 在这里另写一套「看起来像」的子串匹配，会把 ``ai-assisted-*``、
#: ``agent-*`` 判成人核过的——那正是最贵的错。
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _line(label: str, value: Any, *, width: int = 14) -> str:
    text = "" if value is None else str(value)
    return f"{label.ljust(width)}{text}" if text else f"{label.ljust(width)}（无）"


def _is_machine_signature(signature: str) -> bool:
    """金标署名是不是**机器/自动化**写的（人核 ⇒ ``False``）。"""
    from trajectory_pipeline.evaluation.golden.signatures import (
        KIND_HUMAN,
        classify_signature,
    )

    return classify_signature(signature) != KIND_HUMAN


# --------------------------------------------------------------------------
# 各区块渲染
# --------------------------------------------------------------------------


def render_version_block(
    row: Mapping[str, Any],
    *,
    version_meta: Mapping[str, Any],
    marker: str,
) -> str:
    """① 版本与署名。**金标署名摆在最前**——它是「这条要不要多看两眼」的开关。"""
    golden = row.get("golden") if isinstance(row.get("golden"), Mapping) else {}
    signature = str(golden.get("reviewed_by") or "")
    machine = _is_machine_signature(signature)
    marker_text = {
        diff_mod.MARK_NEW: "new（上一版没有这一行）",
        diff_mod.MARK_CHANGED: "changed（内容相对上一版变了 ⇒ 要重新看）",
        diff_mod.MARK_ID_MIGRATED: "id_migrated（键变了，内容没变）",
        diff_mod.MARK_UNCHANGED: "unchanged（内容与上一版一致）",
    }.get(marker, marker or "（无对照版本）")

    lines = [
        _line("冻结版本", f"v{version_meta.get('version', '?')}"),
        _line("冻结时刻", version_meta.get("frozen_at") or ""),
        _line("标签口径版本", version_meta.get("label_schema_version") or ""),
        _line("rubric 版本", version_meta.get("rubric_version") or ""),
        _line("评估器版本", version_meta.get("evaluator_version") or ""),
        _line("金标署名", signature),
        _line("金标署名时刻", golden.get("reviewed_at") or ""),
        _line("变更标记", marker_text),
    ]
    if machine:
        lines.insert(
            5,
            f"⚠ **这条金标不是人署的名**（署名 = {signature or '（空）'}，"
            "自动化写的）——判读时请格外当心，"
            "裁定「不一致」时优先考虑「金标本身错了」。",
        )
    return "\n".join(lines)


def render_site_block(row: Mapping[str, Any]) -> str:
    """② 站点与来源。"""
    legacy = str(row.get("golden_id_legacy") or "")
    return "\n".join([
        _line("URL", row.get("url") or ""),
        _line("站点 URL", row.get("site_url") or ""),
        _line("播放页 URL", row.get("play_page_url") or ""),
        _line("任务", row.get("task_id") or ""),
        _line("Run", row.get("run_id") or ""),
        _line("golden_id", row.get("golden_id") or ""),
        _line("旧 golden_id", legacy),
        _line("来源类型", row.get("source_kind") or ""),
        _line("样本状态", row.get("status") or ""),
        _line("所属档", row.get("__split__") or ""),
    ])


def render_gold_block(row: Mapping[str, Any]) -> str:
    """③ 金标（人工真值）。"""
    golden = row.get("golden") if isinstance(row.get("golden"), Mapping) else {}
    label = str(golden.get("label") or "")
    label_text = str(golden.get("label_label") or "")
    head = f"{label}（{label_text}）" if label_text else label
    lines = [_line("金标标签", head)]
    note = str(golden.get("note") or "")
    if note:
        lines.append(_line("金标备注", note))
    gold_scores = golden.get("dimension_scores")
    if gold_scores:
        lines.append(_line("金标维度分", json.dumps(gold_scores, ensure_ascii=False)))
    return "\n".join(lines)


def render_code_block(row: Mapping[str, Any]) -> str:
    """④ 代码判定与证据。**标签文本取行内的**（判读当时的文本）。"""
    code = row.get("code") if isinstance(row.get("code"), Mapping) else {}
    branch = code.get("branch")
    branch_text = "成功（branch=null ⇒ 记成功）" if branch is None else str(branch)
    label_text = str(code.get("branch_label") or "")
    head = f"{branch_text}（{label_text}）" if label_text else branch_text
    return "\n".join([
        _line("代码分支", head),
        _line("判断点", code.get("question") or ""),
        _line("判定来源", code.get("source") or ""),
        _line("到达播放页", code.get("reached_play_page")),
        _line("用了兜底", code.get("fallback_used")),
        _line("证据原文", ""),
        str(code.get("evidence") or "（无证据原文）"),
    ])


def render_scores_block(row: Mapping[str, Any], *, version: Any = None) -> str:
    """⑤ 维度分数。**三种「没有」分开写**（M-29 的病）。"""
    top = row.get("dimension_scores", None)
    detail = row.get("scores_detail", None)
    if "dimension_scores" not in row and "scores_detail" not in row:
        return (
            f"⚠ **该版本（v{version}）的冻结档没有顶层维度分数字段**"
            "（顶层维度分数自 v7 起才有）——不是「没测」，是**没这字段**。"
        )
    lines: list[str] = []
    if top is None:
        lines.append("顶层维度分数: **null（本行弃权）**")
    elif not top:
        lines.append("顶层维度分数: **空（本行没测）**")
    else:
        lines.append("顶层维度分数:")
        lines.append(json.dumps(top, ensure_ascii=False, indent=2))
    if detail is None:
        lines.append("scores_detail: **null（弃权）**")
    elif not detail:
        lines.append("scores_detail: **空（本行没测）**")
    else:
        lines.append("scores_detail:")
        lines.append(json.dumps(detail, ensure_ascii=False, indent=2))
    return "\n".join(lines)


def render_scene_block(scene: Mapping[str, Any]) -> str:
    """⑥ 现场读数。**读不到就说读不到**（见模块 docstring 第 1 条）。"""
    lines: list[str] = []
    status = str(scene.get("status") or "")
    found = bool(scene.get("found"))
    if found:
        lines.append(_line("现场状态", f"{status}（读到了）"))
    else:
        lines.append(
            _line("现场状态", f"{status or 'not_found'}"),
        )
        lines.append(
            "⚠ **没读到现场**——这**不是**「页面里没有播放控件」。"
            "定位说明与候选见下面；判不了就在裁决里选「判不了」。"
        )
    lines.append(_line("定位说明", scene.get("note") or ""))
    unit = str(scene.get("unit") or "")
    if unit:
        lines.append(_line("决策单元", unit))
    degraded = list(scene.get("degraded") or [])
    if degraded:
        lines.append(_line("存档降级标记", "/".join(degraded)))
    title = str(scene.get("title") or "")
    if title:
        lines.append(_line("页面标题", title))
    steps = list(scene.get("steps") or [])
    if steps:
        lines.append("")
        lines.append(f"── 每步观察（{len(steps)} 步）──")
        lines.extend(f"  {s}" for s in steps)
    else:
        lines.append("")
        lines.append("  （这一步没有观察读数）")
    candidates = list(scene.get("candidates") or [])
    if candidates:
        lines.append("")
        lines.append("── 存档里现有的单元与 url（判断是「没访问过」还是「url 对不上」）──")
        lines.extend(f"  {c}" for c in candidates)
    body = str(scene.get("body") or "")
    body_from = str(scene.get("body_from") or "")
    if body:
        lines.append("")
        lines.append(f"── 正文预览（来自 {body_from or '未标注来源步'}）──")
        lines.append(body)
    else:
        lines.append("")
        lines.append("── 正文预览 ──")
        lines.append("  （没读到正文）")
    return "\n".join(lines)


def render_rubric_block(
    *,
    clauses: Mapping[str, str],
    branches: Mapping[str, str],
    preconditions: Mapping[str, str],
    era_note: str,
    wanted: Sequence[str] = DEFAULT_CLAUSES,
) -> str:
    """⑦ 尺子：条款原文 + 分支表 + 前置事实 + 口径代。**全部现读渲染**。"""
    lines: list[str] = ["── 条款原文（rubric 现行版本）──"]
    for cid in wanted:
        text = clauses.get(cid, "")
        if not text:
            lines.append(f"⚠ 条款 {cid} 原文不可用（rubric 文档里没有这一条）")
            lines.append("")
            continue
        lines.append(text)
        lines.append("")
    lines.append("── 分支表（22 类封闭集 · **现行**口径）──")
    lines.append("⚠ 本行实际用的标签文本在 ④ 里（**判读当时的文本**）——")
    lines.append("   现行表只作「该给哪个分支」的参照清单，别拿它改写 ④ 的口径。")
    for branch, label in branches.items():
        lines.append(f"  {branch} = {label}")
    if preconditions:
        lines.append("")
        lines.append("── 分支前置事实（贴标签的前提；前提不成立 ⇒ 降 unresolved）──")
        for branch, text in preconditions.items():
            lines.append(f"  [{branch}] {text}")
    if era_note:
        lines.append("")
        lines.append("── 分支口径代 ──")
        lines.append(era_note)
    return "\n".join(lines)


def render_trajectory_block(trajectory: Mapping[str, Any] | None, *, tid: str) -> str:
    """⑧ T 视图（per-Run 完整任务视图）。**读不到就说读不到**。"""
    if trajectory is None:
        return (
            f"⚠ **没读到 T 视图**（{tid}.json 不存在或解析失败）——"
            "**不是**「这个任务什么都没做」。"
        )
    lines = [
        "── messages 视图 ──",
        render_messages_text(trajectory.get("messages")),
        "",
        "── openai 视图（function-calling）──",
        render_openai_text(trajectory.get("openai")),
        "",
        "── meta 视图 ──",
        render_meta_text(trajectory.get("meta")),
    ]
    return "\n".join(lines)


def render_persona_block(row: Mapping[str, Any]) -> str:
    """⑨ persona 六维。"""
    persona = row.get("persona") if isinstance(row.get("persona"), Mapping) else {}
    if not persona:
        return "（本行没有 persona）"
    return "\n".join(
        _line(str(key), value) for key, value in persona.items()
    )


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def build_golden_task_data(
    row: Mapping[str, Any],
    *,
    version_meta: Mapping[str, Any],
    clauses: Mapping[str, str],
    marker: str = "",
    scene: Mapping[str, Any] | None = None,
    trajectory: Mapping[str, Any] | None = None,
    branches: Mapping[str, str] | None = None,
    preconditions: Mapping[str, str] | None = None,
    era_note: str = "",
    generated_at: str = "",
) -> dict[str, Any]:
    """一行冻结行 → ``task.data``（**纯函数**，不联网）。

    ``scene`` / ``trajectory`` 由调用方取好传进来——取数要碰文件系统，
    组装这一步不碰。
    """
    if branches is None:
        from trajectory_pipeline.executor.branches import BRANCH_LABELS

        branches = BRANCH_LABELS
    if preconditions is None:
        from trajectory_pipeline.executor.branches import BRANCH_PRECONDITION

        preconditions = BRANCH_PRECONDITION

    golden_id = str(row.get("golden_id") or "")
    version = version_meta.get("version", "")
    data: dict[str, Any] = {
        # 键：判重与回填都靠它
        "sample_id": golden_id,
        "golden_id": golden_id,
        "split": str(row.get("__split__") or ""),
        "task_id": str(row.get("task_id") or ""),
        "run_id": str(row.get("run_id") or ""),
        "version_label": f"v{version}",
        # 九个展示块（键名与 label_config 的控件名逐字一致）
        "version_block": render_version_block(row, version_meta=version_meta, marker=marker),
        "site_block": render_site_block(row),
        "gold_block": render_gold_block(row),
        "code_block": render_code_block(row),
        "scores_block": render_scores_block(row, version=version),
        "scene_block": render_scene_block(scene or {}),
        "rubric_block": render_rubric_block(
            clauses=clauses,
            branches=branches,
            preconditions=preconditions,
            era_note=era_note,
        ),
        "trajectory_block": render_trajectory_block(
            trajectory, tid=f"{row.get('task_id')}__{row.get('run_id')}"
        ),
        "persona_block": render_persona_block(row),
        # 时间戳（批次边界核对用）
        "generated_at": generated_at,
        "uploaded_at": _now_iso(),
    }
    # 契约：九个块一个都不能少（键名与控件名逐字一致）
    missing = [c for c in BLOCK_CONTROLS if c not in data]
    if missing:  # pragma: no cover - 结构性错误，跑一次就该炸
        raise ValueError(f"task.data 缺展示块: {missing}")
    return data


def read_scene(
    row: Mapping[str, Any],
    *,
    p2_root: str | Path | None = None,
    preview_chars: int = evidence.DEFAULT_PREVIEW_CHARS,
) -> dict[str, Any]:
    """冻结行 → 现场摘要（**只读**；读不到时 ``found=False`` 且带说明）。"""
    digest = evidence.row_digest(row, p2_root=p2_root, preview_chars=preview_chars)
    return evidence.scene_summary(digest)


def read_trajectory(
    row: Mapping[str, Any],
    *,
    task_views_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """冻结行 → T 视图。**读不到返回 ``None``**（fail-closed，不返回半份）。"""
    tid = f"{row.get('task_id')}__{row.get('run_id')}"
    return read_task_view(tid, root=task_views_root)


def generated_at_of(
    row: Mapping[str, Any],
    *,
    task_views_root: str | Path | None = None,
) -> str:
    """T 视图落盘时刻（T 视图没有 ⇒ 空串，不拿上传时刻顶替）。"""
    tid = f"{row.get('task_id')}__{row.get('run_id')}"
    try:
        path = task_views_dir(task_views_root) / f"{tid}.json"
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class GoldenExportResult:
    """一次导出的**全部读数**：LS 任务计划 + **逐行读数**。

    为什么要把逐行读数一起带出来：现场可得率 / T 视图可得率这些数，
    只有在**导出那一次**读现场时才算得出来。事后另跑一遍读数，等于把
    「切存档」这件重活做两遍，还多一次两边读数漂移的机会。
    """

    #: 推给 LS 的任务（``plan.tasks``）+ 拒推/跳过清单。
    plan: ExportPlan = field(default_factory=ExportPlan)
    #: 逐行读数：``golden_id`` / ``split`` / ``marker`` / ``scene_found`` /
    #: ``trajectory_found`` / ``signature`` / ``rejected``。
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return self.plan.count

    def summary(self) -> str:
        return self.plan.summary()


def export_golden_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    version_meta: Mapping[str, Any],
    clauses: Mapping[str, str],
    markers: Mapping[str, str] | None = None,
    only_changed: bool = False,
    p2_root: str | Path | None = None,
    task_views_root: str | Path | None = None,
    scan_settings: CredentialScanSettings | None = None,
) -> GoldenExportResult:
    """一批冻结行 → :class:`GoldenExportResult`（**不联网**）。

    凭据扫描覆盖 **task.data 全文**（不是只扫 P3 三视图）——现场正文预览与
    条款原文也在扫描范围内。命中即 **fail-closed 拒推**（记 ``rejected``）。
    """
    scan = scan_settings if scan_settings is not None else CredentialScanSettings()
    marker_map = markers or {}
    result = GoldenExportResult()

    for row in diff_mod.filter_changed(rows, marker_map, only_changed=only_changed):
        gid = str(row.get("golden_id") or "")
        marker = marker_map.get(gid, "")
        scene = read_scene(row, p2_root=p2_root)
        trajectory = read_trajectory(row, task_views_root=task_views_root)
        data = build_golden_task_data(
            row,
            version_meta=version_meta,
            clauses=clauses,
            marker=marker,
            scene=scene,
            trajectory=trajectory,
            era_note=era_note_for(row),
            generated_at=generated_at_of(row, task_views_root=task_views_root),
        )
        hits: list[ScanHit] = scan_for_credentials(
            data, view="task_data", settings=scan
        )
        golden = row.get("golden") if isinstance(row.get("golden"), Mapping) else {}
        signature = str(golden.get("reviewed_by") or "")
        stat: dict[str, Any] = {
            "golden_id": gid,
            "split": str(row.get("__split__") or ""),
            "marker": marker,
            "scene_found": bool(scene.get("found")),
            "scene_status": str(scene.get("status") or ""),
            "trajectory_found": trajectory is not None,
            "signature": signature,
            "signature_kind": classify_signature(signature),
            "rejected": bool(hits),
        }
        result.rows.append(stat)
        if hits:
            result.plan.rejected.append((gid, hits))
            continue
        result.plan.tasks.append(build_task(data))
    return result


def era_note_for(row: Mapping[str, Any]) -> str:
    """本行的分支口径代说明。

    ⚠️ **代是按金标署名时刻定位的**，不是按采集时刻——P1 存档**不带时间戳**
    （``executor/branch_eras.py`` 的 ``M_TIME_CAVEAT``），采集那一刻的代
    **不可知**（mtime 是代理不是事实）。这一句必须跟着口径代一起给评审看。
    """
    from trajectory_pipeline.executor.branch_eras import (
        BRANCH_ERAS,
        M_TIME_CAVEAT,
        era_for_date,
    )

    golden = row.get("golden") if isinstance(row.get("golden"), Mapping) else {}
    reviewed_at = str(golden.get("reviewed_at") or "")
    day = reviewed_at[:10] if reviewed_at else ""
    era_id = era_for_date(day) if day else BRANCH_ERAS[0].era_id
    era = next((e for e in BRANCH_ERAS if e.era_id == era_id), None)
    summary = era.summary if era else ""
    since = era.since if era else ""
    return (
        f"口径代 {era_id}（自 {since} 起）：{summary}\n"
        f"⚠ 代按**金标署名时刻**（{day or '未知'}）定位；"
        f"采集侧分支判定的时刻不可知——{M_TIME_CAVEAT}"
    )


__all__ = [
    "DEFAULT_CLAUSES",
    "SCORES_ABSENT",
    "SCORES_EMPTY",
    "SCORES_FILLED",
    "SCORES_NULL",
    "GoldenExportResult",
    "build_golden_task_data",
    "era_note_for",
    "export_golden_rows",
    "generated_at_of",
    "read_scene",
    "read_trajectory",
    "render_code_block",
    "render_gold_block",
    "render_persona_block",
    "render_rubric_block",
    "render_scene_block",
    "render_site_block",
    "render_scores_block",
    "render_trajectory_block",
    "render_version_block",
]
