"""**R1 试点推送的回读核验**（联网；只读 LS，不推不删）。

背景
----
R1 只推 v14 的 5 条试点（方案 22 §9）。推完**不能只看推送命令自己打印的
``tasks_pushed=5``**——那说的是「LS 收下了几条」，不是「LS 里现在长什么样」。
本脚本把项目**导出来**核一遍，核的是六件事：

1. **任务条数** = 推的那几条（不多不少）；
2. **每条 task 的 data 九个区块都在**（少一个 ⇒ 标注页上有一块是空的，
   而**空白的区块和「现场读不到」长得一样**——人分不出来）；
3. **label_config 与台账记的 sha 一致**（不一致 ⇒ 推上去的渲染结果与留底
   不是同一份，事后无法复现）；
4. **一条 prediction 都没有**（§3.3：不推预测。预填裁决等于给答案）；
5. **人工标注条数** == 期望条数（推送后是 0；人复核过之后是 N）；
6. **每条标注都带复核人身份**（裁定④：annotator 单列一栏）。

⚠️ **第 4 条要单独看**：LS 1.23 对 ``from_name`` 不匹配的 prediction 是
**静默丢弃**的（``201 {"created": 0}``）⇒ 「没推」与「推了但被丢了」在推送
返回值上长得一样 ⇒ 只能回读确认。

⚠️ **第 5/6 条踩过的坑（R1 实测，2026-10-29）**：**任务列表里的
``annotations`` 数组恒为空**（``total_annotations=1`` 而 ``annotations=[]``）
⇒ 从任务列表读标注**永远读到 0 条**，而那读起来跟「一条都没复核」**一模一样**
（静默的错）。⇒ 标注要从 ``/api/tasks/{id}/annotations`` 逐条取。

用法（**必须在仓库根跑**）::

    uv run python -m trajectory_pipeline.research.probes.golden_ls_r1_readback \
        --version 14 --expect 5

    # 人复核过之后（含「每条标注都有复核人身份」）：
    uv run python -m trajectory_pipeline.research.probes.golden_ls_r1_readback \
        --version 14 --expect 5 --expect-annotations 1

⚠️ **要用 ``-m`` 起**（或 ``uv run``）：直接 ``python <路径>.py`` 时
``sys.path[0]`` 是**脚本所在目录**，不是仓库根 ⇒ ``import
trajectory_pipeline`` 会失败（``ModuleNotFoundError``）。

产物：同目录 ``golden_ls_r1_readback.json``（**证据**，不是结论）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path("trajectory_pipeline")
LEDGER = ROOT / "output" / "ls_bridge" / "golden_projects.jsonl"
OUT = pathlib.Path(__file__).resolve().parent / "golden_ls_r1_readback.json"

#: task.data 里必须有的九个区块（与 ``label_config.py`` 的渲染同源）。
BLOCKS = (
    "version_block",
    "site_block",
    "gold_block",
    "code_block",
    "scores_block",
    "scene_block",
    "rubric_block",
    "trajectory_block",
    "persona_block",
)

#: 裁决四栏的控件名（与 ``label_config.py`` 同源）。
VERDICT_CONTROL = "verdict"
HUMAN_BRANCH_CONTROL = "human_branch"
REASON_CONTROL = "reason"
NOTE_CONTROL = "note"


def _ledger_record(version: int) -> dict:
    """读台账里那一版（**坏行跳过**——台账是索引，LS 才是真相）。"""
    if not LEDGER.is_file():
        raise SystemExit(f"[FAIL] 台账不存在: {LEDGER}")
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if int(rec.get("version") or -1) == int(version):
            return rec
    raise SystemExit(f"[FAIL] 台账里没有 v{version}（先跑 init-project）")


def _result_map(annotation: dict) -> dict[str, str]:
    """一条标注的 ``result`` → ``{控件名: 文本}``（取值口径同 ``verdicts.py``）。"""
    out: dict[str, str] = {}
    for item in annotation.get("result") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("from_name") or "")
        if name not in (
            VERDICT_CONTROL,
            HUMAN_BRANCH_CONTROL,
            REASON_CONTROL,
            NOTE_CONTROL,
        ):
            continue
        value = item.get("value")
        text = ""
        if isinstance(value, dict):
            if "choices" in value:
                choices = value.get("choices")
                text = (
                    " / ".join(str(c) for c in choices)
                    if isinstance(choices, list)
                    else str(choices or "")
                )
            else:
                raw = value.get("text")
                text = (
                    "\n".join(str(x) for x in raw)
                    if isinstance(raw, list)
                    else str(raw or "")
                )
        elif isinstance(value, str):
            text = value
        if name in out and out[name]:
            out[name] = f"{out[name]}\n{text}".strip()
        else:
            out[name] = text
    return out


def _annotator(annotation: dict) -> str:
    """复核人身份（口径同 ``verdicts._annotator_identity``，但**不 import 它**）。

    ⚠️ 这里**重写一遍**是有意的：探针是**证据**，不 import 业务模块才能
    在业务代码出错时**独立地**说「读出来是空的」。口径一样，但两处各写
    一遍——业务代码把 ``created_username`` 读错时，探针会**先叫**。
    """
    created_by = annotation.get("created_by")
    if isinstance(created_by, dict):
        email = str(created_by.get("email") or "")
        if email:
            return email
    elif isinstance(created_by, str) and created_by.strip():
        return created_by.strip()
    username = str(annotation.get("created_username") or "").strip()
    if username:
        return username.split(",")[0].strip()
    for key in ("completed_by", "updated_by"):
        value = annotation.get(key)
        if isinstance(value, int):
            return str(value)
    return "（不可用）"


def main() -> int:
    ap = argparse.ArgumentParser(description="R1 试点推送回读核验")
    ap.add_argument("--version", type=int, required=True)
    ap.add_argument(
        "--expect",
        type=int,
        default=None,
        help="期望的任务条数（不给则只报读数，不判对错）",
    )
    ap.add_argument(
        "--expect-annotations",
        type=int,
        default=0,
        help="期望的人工标注条数（推送后 0；人复核过之后是复核条数）",
    )
    args = ap.parse_args()

    from trajectory_pipeline.evaluation.ls_bridge.client import build_client
    from trajectory_pipeline.evaluation.ls_bridge.settings import load_settings

    rec = _ledger_record(args.version)
    project_id = int(rec["project_id"])
    want_sha = str(rec.get("label_config_sha") or "")

    settings = load_settings()
    client = build_client(settings)
    payload = client.export_project_json(project_id)

    tasks = payload.get("tasks") or []
    annotations = payload.get("annotations") or []
    mismatches = payload.get("mismatches") or []

    # ── ① 条数 ────────────────────────────────────────────────────────
    n_tasks = len(tasks)
    n_pred = sum(len(t.get("predictions") or []) for t in tasks)
    # ⚠️ **按 annotation id 去重**：任务列表内联的 annotations 与逐条端点
    # 可能是**同一条**（client 已合并，这里再兜一层）。
    ann_ids = {a.get("id") for a in annotations if a.get("id") is not None}
    n_ann = len(ann_ids) if ann_ids else len(annotations)
    # ⚠️ **LS 自称的标注条数**（每个 task 上的 ``total_annotations`` 之和）
    # ——第三个独立读数，用来对账。
    n_ann_self = sum(int(t.get("total_annotations") or 0) for t in tasks)

    # ── ② 每条 task 的区块 ────────────────────────────────────────────
    per_task = []
    missing_blocks_total = 0
    for t in tasks:
        data = t.get("data") or {}
        gid = str(data.get("golden_id") or "")
        missing = [b for b in BLOCKS if not str(data.get(b) or "").strip()]
        missing_blocks_total += len(missing)
        scene_block = str(data.get("scene_block") or "")
        per_task.append(
            {
                "ls_task_id": t.get("id"),
                "golden_id": gid,
                "sample_id": str(data.get("sample_id") or ""),
                "split": str(data.get("split") or ""),
                "missing_blocks": missing,
                # 「现场读到了」vs「没读到现场」——判据是区块里那行状态字样，
                # 不是「区块非空」（区块**总是**非空：读不到也会写明为什么）。
                "scene_status": (
                    "not_found" if "现场状态          not_found" in scene_block else "located"
                ),
                "predictions": len(t.get("predictions") or []),
                "total_annotations": int(t.get("total_annotations") or 0),
            }
        )

    # ── 标注明细 ──────────────────────────────────────────────────────
    per_annotation = []
    for a in annotations:
        values = _result_map(a)
        per_annotation.append(
            {
                "annotation_id": a.get("id"),
                "task": a.get("task"),
                "annotator": _annotator(a),
                "created_username": a.get("created_username"),
                "verdict": values.get(VERDICT_CONTROL) or "",
                "human_branch": values.get(HUMAN_BRANCH_CONTROL) or "",
                "reason_filled": bool(values.get(REASON_CONTROL)),
                "note_filled": bool(values.get(NOTE_CONTROL)),
                "result_controls": [
                    str(item.get("from_name") or "")
                    for item in (a.get("result") or [])
                    if isinstance(item, dict)
                ],
            }
        )
    # ⚠️ **每条标注都要能配回它的 task**（配不上 ⇒ 这条标注在统计里凭空
    # 消失，而那读起来像「没复核」）。
    task_ids = {t.get("id") for t in tasks}
    orphan = [
        a for a in per_annotation if a.get("task") is not None and a["task"] not in task_ids
    ]
    all_have_annotator = all(a["annotator"] != "（不可用）" for a in per_annotation)

    # ── ③ label_config ───────────────────────────────────────────────
    # ⚠️ **两件事分开判**：
    #   (a) LS 端存的**内容** == 渲染结果（尾随空白除外——LS 会去掉末尾换行）；
    #   (b) **留底文件**的 sha == 台账里记的 sha（台账记的是留底字节的指纹）。
    # 混成一个「sha 对比」会两边都误报。
    from trajectory_pipeline.evaluation.golden_ls.label_config import (
        label_config_matches,
    )

    project = client.get_project(project_id)
    server_xml = str(project.get("label_config") or "")
    local_path = (
        ROOT / "output" / "pipeline" / "calibration" / f"v{args.version}" / "label_config.xml"
    )
    local_bytes = local_path.read_bytes() if local_path.is_file() else b""
    local_xml = local_bytes.decode("utf-8", errors="replace")
    local_sha = hashlib.sha256(local_bytes).hexdigest()[:12] if local_bytes else ""
    content_match = label_config_matches(server_xml, local_xml)

    # ── 判据 ──────────────────────────────────────────────────────────
    checks = {
        "任务条数": (n_tasks == args.expect) if args.expect is not None else None,
        "九条区块齐全": missing_blocks_total == 0,
        "LS 端 label_config 内容一致": content_match,
        "留底文件 sha 与台账一致": (not want_sha) or local_sha == want_sha,
        "一条 prediction 都没有": n_pred == 0,
        "人工标注条数与期望一致": n_ann == int(args.expect_annotations),
        "LS 自称的标注条数与读到的一致": n_ann_self == n_ann,
        "每条标注都配得回它的 task": not orphan,
        "每条标注都有复核人身份": all_have_annotator,
        "两个端点没有对不上账": not mismatches,
    }
    hard = {k: v for k, v in checks.items() if v is not None}

    report = {
        "version": args.version,
        "project_id": project_id,
        "title": rec.get("title"),
        "tasks": n_tasks,
        "expected_tasks": args.expect,
        "predictions": n_pred,
        "annotations": n_ann,
        "expected_annotations": args.expect_annotations,
        "annotations_self_reported": n_ann_self,
        "mismatches": mismatches,
        "label_config": {
            "ledger_sha": want_sha,
            "local_file": str(local_path),
            "local_sha": local_sha,
            "server_sha": hashlib.sha256(server_xml.encode("utf-8")).hexdigest()[:12],
            "content_match": content_match,
            "note": "LS 存的那份去掉了末尾换行 ⇒ 比内容不比 sha",
        },
        "per_task": per_task,
        "per_annotation": per_annotation,
        "checks": checks,
    }
    OUT.write_bytes(
        json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
    )

    print(f"项目 {project_id}（{rec.get('title')}）任务 {n_tasks} 条 · "
          f"prediction {n_pred} · 人工标注 {n_ann}")
    for t in per_task:
        miss = (" · 缺区块 " + ",".join(t["missing_blocks"])) if t["missing_blocks"] else ""
        print(f"   {t['golden_id'][:44]:<46} {t['split']:<12} {t['scene_status']}{miss}")
    for a in per_annotation:
        print(f"   ▸ task {a['task']} · {a['annotator']} · {a['verdict']}"
              f" / {a['human_branch']} · 理由{'有' if a['reason_filled'] else '空'}"
              f" · 备注{'有' if a['note_filled'] else '空'}")
    for k, v in hard.items():
        print(f"  {'OK ' if v else 'FAIL'} {k}")
    print(f"证据落盘: {OUT}")
    return 0 if all(hard.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
