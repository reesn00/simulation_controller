"""golden_ls CLI: 冻结黄金集 → Label Studio 的**上传与复核回路**（方案 22）。

用法::

    python -m trajectory_pipeline.evaluation.golden_ls projects
    python -m trajectory_pipeline.evaluation.golden_ls pending
    python -m trajectory_pipeline.evaluation.golden_ls init-project --version 14
    python -m trajectory_pipeline.evaluation.golden_ls upload --version 14 --dry-run
    python -m trajectory_pipeline.evaluation.golden_ls upload --version 14
    python -m trajectory_pipeline.evaluation.golden_ls export-annotations --version 14

**为什么是独立的 ``python -m`` 入口，而不是 ``ls_bridge golden`` 子命令**
------------------------------------------------------------------------
``tests/contract/test_ls_bridge_contract.py`` 把 ``ls_bridge`` 的依赖锁死在
``{ls_bridge, scout.aggregate, scout.types, storage, storage.read_p3,
storage.task_views}`` —— **不含** ``executor`` / ``evaluation.golden.*`` /
``evaluation.calibration.*``。而黄金集这一路**必须**读这三样：

* ``executor.branches.BRANCH_LABELS`` —— 分支下拉的 22 个选项；
* ``evaluation.golden.*`` —— 冻结档与等价类；
* ``evaluation.calibration.evidence`` —— 现场读数。

塞进 ``ls_bridge`` 就得把白名单撑开，白名单一撑，scout 那一路的依赖边界
（evaluation → storage，不碰 executor）就没了。⇒ **另立一个包**
``evaluation/golden_ls/``：它 import ``ls_bridge``（用现成的 client / 推送 /
判重台账），方向是**单向**的，``ls_bridge`` 那边一个字都不动。

三档全传（裁定③）、一致行也传（裁定②）、两套署名分列（裁定④）——口径见
方案 22。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Sequence

from trajectory_pipeline.evaluation.golden.builder import PIPELINE_ROOT
from trajectory_pipeline.evaluation.golden.splits import SPLITS
from trajectory_pipeline.evaluation.golden_ls import dataset
from trajectory_pipeline.evaluation.golden_ls import diff as diff_mod
from trajectory_pipeline.evaluation.golden_ls import exporter, report as report_mod
from trajectory_pipeline.evaluation.golden_ls.label_config import (
    label_config_sha,
    render_label_config,
)
from trajectory_pipeline.evaluation.golden_ls.projects import (
    UPLOAD_BASELINE_VERSION,
    GoldenProjectIndex,
    pending_versions,
    project_title,
)

#: 本包的产物目录（**包内相对**，不跟 cwd 走——与 ``ls_bridge.push_index``
#: 同一口径：在仓库根跑与在 ``trajectory_pipeline/`` 里跑读到的同一个目录）。
DEFAULT_CALIBRATION_ROOT = PIPELINE_ROOT / "calibration"


class GoldenCliError(RuntimeError):
    """命令行用法/状态错误 ⇒ **退 2**（不猜、不静默回落）。"""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _version_dir(version: int, *, root: pathlib.Path | None = None) -> pathlib.Path:
    """本版的产物目录（推送计划 / label_config 留底 / 标注导出）。"""
    return (root or DEFAULT_CALIBRATION_ROOT) / f"v{int(version)}"


def _write_text_bytes(path: pathlib.Path, text: str) -> None:
    """**按字节**写文本（LF, UTF-8, 不带 BOM）。

    ⚠️ **不许用 ``Path.write_text``**：它在 Windows 上开的是文本模式
    （``newline=None`` ⇒ 写入时把 ``\\n`` 翻成 ``os.linesep``）⇒ 整份文件
    变 CRLF。这对**产物**不是「无所谓」：

    * ``label_config.xml`` 的留底字节必须**等于**台账里记的那个 sha——
      sha 是对内存里的 XML 算的，文本模式一改行尾，留底与台账就对不上，
      而「留底对不上」这件事**没有任何地方会报错**；
    * ``ls_upload_plan.jsonl`` / ``ls_annotations.jsonl`` 同理——换行符
      混着会让下游按字节比对时判成「内容不同」。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _select_splits(args: argparse.Namespace) -> list[str]:
    """本次要传的档（**默认三档全传**——裁定③）。

    ``--splits`` 缺省 = ``SPLITS`` 全集；``--exclude-holdout`` 从里面摘掉
    holdout。两个开关都给了的话，``--exclude-holdout`` 优先（它是**减法**，
    减法不会把人选的档弄丢）。

    ⚠️ 档名**只从 ``golden/splits.py`` 取**（不手抄字面量）——手抄会写出
    ``calib`` 这种拼错的名字，而拼错**不报错**，只是那一档悄悄变空。
    """
    raw = getattr(args, "splits", None)
    if raw:
        wanted = [s.strip() for s in str(raw).split(",") if s.strip()]
        unknown = [s for s in wanted if s not in SPLITS]
        if unknown:
            raise GoldenCliError(
                f"未知档名 {unknown}；可选 {list(SPLITS)}（档名取自 golden/splits.py）"
            )
        selected = [s for s in SPLITS if s in wanted]
    else:
        selected = list(SPLITS)
    if getattr(args, "exclude_holdout", False):
        selected = [s for s in selected if s != "holdout"]
    if not selected:
        raise GoldenCliError("一档都不剩 —— 没有可推的内容")
    return selected


def _previous_version(
    version: int, *, golden_root: pathlib.Path | str | None = None
) -> int | None:
    """上一版（**版本号最大的那个更小的**）。没有 ⇒ ``None``。

    ⚠️ 用**版本号**找上一版，不按「目录名排序的最后一个」——目录顺序是
    文件系统的事（``v9`` 会排在 ``v10`` 前面）。
    """
    versions = dataset.frozen_versions(root=golden_root)
    earlier = [v for v in versions if v < int(version)]
    return max(earlier) if earlier else None


def _load_version(
    version: int, *, golden_root: pathlib.Path | str | None = None
) -> list[dict[str, Any]]:
    try:
        return dataset.load_rows(version, root=golden_root)
    except Exception as exc:  # noqa: BLE001 —— 读不出冻结档要说得清
        raise GoldenCliError(f"读不到 v{version} 的冻结档: {exc}") from exc


def _ledger_path(args: argparse.Namespace) -> pathlib.Path:
    override = getattr(args, "ledger", None)
    if override:
        return pathlib.Path(override)
    return GoldenProjectIndex.default_path()


def _client(args: argparse.Namespace) -> tuple[Any, Any]:
    """建 LS client（**没凭据就报错**——不猜、不静默跳过）。"""
    from trajectory_pipeline.evaluation.ls_bridge.client import build_client
    from trajectory_pipeline.evaluation.ls_bridge.settings import load_settings

    settings = load_settings(base_url=args.url, api_key=args.api_key)
    if not settings.has_credentials():
        raise GoldenCliError(
            "未设置 TRAJECTORY_LS_API_KEY；本命令要连 LS，先给凭据"
        )
    return build_client(settings), settings


def _project_description(version: int, meta: dict[str, Any]) -> str:
    """项目 description（**版本元信息**——``create_project`` 原先硬编码了一段
    scout 的话术，这一版要写黄金集自己的）。"""
    counts = meta.get("counts") or {}
    total = counts.get("total") if isinstance(counts, dict) else None
    return (
        f"trajectory_pipeline 冻结黄金集 v{version} 复核"
        f"（冻结于 {meta.get('frozen_at') or '未知'}，"
        f"标签口径 {meta.get('label_schema_version') or '未知'}，"
        f"rubric {meta.get('rubric_version') or '未知'}，"
        f"评估器 {meta.get('evaluator_version') or '未知'}，"
        f"条数 {total if total is not None else len(counts) or '未知'}）"
    )


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------


def _cmd_projects(args: argparse.Namespace) -> int:
    """台账视图（版本 → 项目 id → 条数）。**只读本地文件**，不联网。"""
    index = GoldenProjectIndex.load(_ledger_path(args))
    if not len(index):
        print(f"台账为空（{index.path}）；冻结了但还没建项目的版本见 `pending`")
        return 0
    print(f"台账: {index.path}")
    for version in index.versions():
        record = index.get(version)
        assert record is not None
        print(
            f"  v{record.version}: project_id={record.project_id} "
            f"title={record.title} frozen_at={record.frozen_at or '-'} "
            f"counts={record.counts or '-'} splits={list(record.splits) or '-'}"
        )
    return 0


def _cmd_pending(args: argparse.Namespace) -> int:
    """**冻结了但没建项目**的版本（欠账清单）。

    这是 owner 裁定「增量版本每个都上传」的**可执行性抓手**：纪律靠人记
    会漏，列出来才看得见。

    ⚠️ **只算 v14 及以上**（裁定 O-1：存量只上传 v14，v1..v13 永久留作
    只读历史，**不上 LS**）——把 v1..v13 列成「欠账」等于发一条会违反
    owner 裁定的指令。

    ⚠️ **``--strict`` 才有退出码**（M-53）：默认**退 0**（这份清单是给人
    看的，缺省退非零会让它没法当参考跑）；``--strict`` 时欠账非空 ⇒
    **退 1**（不是退 2：欠账是**状态**，不是用法错误）⇒ 这样「冻结了但
    没人推」能进 CI/门禁，而不是靠人记得看。
    """
    index = GoldenProjectIndex.load(_ledger_path(args))
    pending = pending_versions(index, golden_root=args.golden_root)
    all_versions = dataset.frozen_versions(root=args.golden_root)
    exempt = [v for v in all_versions if v < UPLOAD_BASELINE_VERSION]
    print(f"已冻结版本: {all_versions or '（无）'}")
    print(f"已建项目:   {index.versions() or '（无）'}")
    print(
        f"欠账(≥v{UPLOAD_BASELINE_VERSION}): "
        f"{pending or '（无）—— 该传的版本都建过项目了'}"
    )
    if exempt:
        print(
            f"不上 LS（裁定 O-1，存量只上传 v{UPLOAD_BASELINE_VERSION}）: {exempt}"
        )
    if pending and getattr(args, "strict", False):
        print(
            f"错误:还有 {len(pending)} 个版本冻结了但没建 LS 项目（欠账）: {pending}",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_init_project(args: argparse.Namespace) -> int:
    """建 / 复用这一版的 LS 项目（**联网**）。

    幂等：**标题精确匹配**（``golden-v{N}``），找到就复用，**不 PATCH
    label_config**——项目里存的是建项目那一刻的快照，老项目配老口径，
    覆盖它会把标注员正在用的配置换掉。
    """
    from trajectory_pipeline.evaluation.ls_bridge.project_manager import (
        find_project_by_title,
        validate_label_config,
    )

    version = int(args.version)
    client, _settings = _client(args)
    xml = render_label_config()
    sha = label_config_sha(xml)
    title = project_title(version)

    existing = find_project_by_title(client, title)
    if existing is not None:
        project_id = int(existing["id"])
        created = False
    else:
        meta = dataset.version_meta(version, root=args.golden_root)
        new_project = client.create_project(
            title=title,
            label_config=xml,
            description=_project_description(version, meta),
        )
        project_id = int(new_project["id"])
        created = True
        errors = validate_label_config(client, project_id, xml)
        if errors:
            print(
                f"⚠ 项目 {project_id} 的 label_config 校验未通过（{len(errors)} 处）:"
                f" {errors[:3]}",
                file=sys.stderr,
            )

    # 渲染结果留底（可核对、可复现）。
    out_dir = _version_dir(version)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_text_bytes(out_dir / "label_config.xml", xml)

    index = GoldenProjectIndex.load(_ledger_path(args))
    if not index.has(version):
        meta = dataset.version_meta(version, root=args.golden_root)
        index.record(
            index.new_record(
                version,
                project_id,
                title=title,
                label_config_sha=sha,
                frozen_at=str(meta.get("frozen_at") or ""),
                evaluator_version=str(meta.get("evaluator_version") or ""),
                counts=dict(meta.get("counts") or {}),
                splits=tuple(SPLITS),
            )
        )
    print(f"project_id={project_id} title={title} created={created} sha={sha}")
    print(f"label_config 留底: {out_dir / 'label_config.xml'}")
    return 0


def _select_by_ids(
    rows: Sequence[dict[str, Any]],
    wanted: Sequence[str],
    *,
    selected: Sequence[str],
    version: int,
) -> list[dict[str, Any]]:
    """按 ``golden_id`` 点名挑行（**试点用**）。

    ⚠️ **点错的 id 必须是错，不能是「少推几条」**：拼错一个字符若被静默
    忽略，人会以为试点覆盖了那条路径、其实没有——而「覆盖到了」正是试点
    的全部意义。所以：**不存在的 id ⇒ 退 2**；**不在本次要传的档里的 id ⇒ 退 2**
    （两件事都是「输入不成立」，不是「少推一点」）。

    重复点名的 id **只推一次**（保持第一次出现的顺序）。
    """
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_id.setdefault(str(row.get("golden_id") or ""), row)
    unknown = [gid for gid in wanted if gid not in by_id]
    if unknown:
        raise GoldenCliError(
            f"v{version} 里没有这些 golden_id: {unknown}；"
            "点错了 id 就不推（试点要覆盖到哪条就得是确的那条）"
        )
    outside = [
        gid
        for gid in wanted
        if str(by_id[gid].get("__split__") or "") not in selected
    ]
    if outside:
        raise GoldenCliError(
            f"这些 golden_id 不在本次要传的档 {list(selected)} 里: {outside}；"
            "要么把那一档也选上，要么去掉它"
        )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for gid in wanted:
        if gid in seen:
            continue
        seen.add(gid)
        out.append(by_id[gid])
    return out


def _cmd_upload(args: argparse.Namespace) -> int:
    """推送某一版的冻结行到它的 LS 项目。"""
    version = int(args.version)
    rows = _load_version(version, golden_root=args.golden_root)
    selected = _select_splits(args)

    prev_version = _previous_version(version, golden_root=args.golden_root)
    prev_rows = (
        dataset.load_rows(prev_version, root=args.golden_root)
        if prev_version is not None
        else []
    )
    markers = diff_mod.mark_changes(rows, prev_rows)

    export_rows = [r for r in rows if str(r.get("__split__") or "") in selected]
    wanted_ids = [
        s.strip()
        for s in str(getattr(args, "golden_ids", "") or "").split(",")
        if s.strip()
    ]
    if wanted_ids:
        export_rows = _select_by_ids(
            rows, wanted_ids, selected=selected, version=version
        )
    if args.limit is not None:
        export_rows = export_rows[: max(0, int(args.limit))]

    from trajectory_pipeline.evaluation.calibration import evidence as evidence_mod

    clauses = evidence_mod.load_clauses()
    meta = dataset.version_meta(version, root=args.golden_root)
    result = exporter.export_golden_rows(
        export_rows,
        version_meta=meta,
        clauses=clauses,
        markers=markers,
        only_changed=bool(args.only_changed),
        p2_root=args.p2_root,
        task_views_root=args.task_views_root,
    )

    out_dir = _version_dir(version)
    out_dir.mkdir(parents=True, exist_ok=True)
    xml = render_label_config()
    _write_text_bytes(out_dir / "label_config.xml", xml)
    _write_plan(
        out_dir, version, result, markers, selected, prev_version, label_config_sha(xml)
    )

    pushed = 0
    if args.dry_run:
        print("[dry-run] 未推送。去掉 --dry-run 执行实际推送。")
    else:
        pushed = _push(args, version, result)

    plan = report_mod.plan_report(
        version=version,
        rows=rows,
        markers=markers,
        stats=result.rows,
        selected_splits=selected,
        previous_version=prev_version,
        planned=result.count,
        pushed=pushed,
    )
    print(report_mod.render_report(plan))
    for gid, hits in result.plan.rejected[:10]:
        print(f"  rejected: {gid} — {'; '.join(h.describe() for h in hits)}")
    print(f"推送计划留底: {out_dir / 'ls_upload_plan.jsonl'}")
    return 0


def _write_plan(
    out_dir: pathlib.Path,
    version: int,
    result: exporter.GoldenExportResult,
    markers: dict[str, str],
    selected: Sequence[str],
    prev_version: int | None,
    sha: str,
) -> None:
    """推送计划落盘（**每条一行**：键 + 读数 + 变更标记）。

    计划里的字段与 ``diff.content_key`` 的判据同源——「变了没有」只有一把
    尺，标记不会与变更标记打架。
    """
    path = out_dir / "ls_upload_plan.jsonl"
    with path.open("wb") as handle:
        header = {
            "_kind": "header",
            "version": version,
            "previous_version": prev_version,
            "selected_splits": list(selected),
            "tasks": result.count,
            "rejected": len(result.plan.rejected),
            "label_config_sha": sha,
        }
        handle.write((json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8"))
        by_id = {
            str((t.get("data") or {}).get("golden_id") or ""): t
            for t in result.plan.tasks
        }
        for stat in result.rows:
            gid = str(stat.get("golden_id") or "")
            data = (by_id.get(gid) or {}).get("data") or {}
            handle.write(
                (
                    json.dumps(
                        {
                            "golden_id": gid,
                            "split": stat.get("split"),
                            "marker": stat.get("marker") or markers.get(gid, ""),
                            "scene_found": stat.get("scene_found"),
                            "trajectory_found": stat.get("trajectory_found"),
                            "signature": stat.get("signature"),
                            "signature_kind": stat.get("signature_kind"),
                            "rejected": stat.get("rejected"),
                            "generated_at": data.get("generated_at", ""),
                            "uploaded_at": data.get("uploaded_at", ""),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
            )


def _with_batch_size(settings: Any, batch_size: int) -> Any:
    """把 ``upload.batch_size`` 改掉（``--batch-size`` 生效的地方）。

    ⚠️ **不许挂一个不生效的开关**：``--batch-size`` 要是只声明不接上，
    人调它却毫无变化——「以为按 10 条一批推了」与「实际按 50 条推了」在
    台账上长得一模一样。⇒ 接上它（``push_batch`` 读的就是这个值）。
    """
    import dataclasses

    if batch_size < 1:
        raise GoldenCliError("--batch-size 必须 ≥ 1")
    upload = dataclasses.replace(settings.upload, batch_size=batch_size)
    return dataclasses.replace(settings, upload=upload)


def _push(
    args: argparse.Namespace, version: int, result: exporter.GoldenExportResult
) -> int:
    """真正推（**联网**）。

    复用 ``ls_bridge.exporter.push_batch``：判重台账按 **project_id** 分
    文件（``push_index__<pid>.jsonl``），``sample_id = golden_id`` ⇒ 同版
    重跑不重复推。
    """
    from trajectory_pipeline.evaluation.ls_bridge.exporter import push_batch
    from trajectory_pipeline.evaluation.ls_bridge.push_index import PushIndex
    from trajectory_pipeline.evaluation.ls_bridge.settings import load_settings

    client, settings = _client(args)
    index = GoldenProjectIndex.load(_ledger_path(args))
    record = index.get(version)
    if record is None:
        raise GoldenCliError(
            f"v{version} 还没有 LS 项目；先跑 `init-project --version {version}`"
        )
    project_id = record.project_id

    if getattr(args, "batch_size", None) is not None:
        settings = _with_batch_size(settings, int(args.batch_size))

    threshold = settings.upload.dry_run_skip_threshold
    if result.count > threshold and not args.force:
        raise GoldenCliError(
            f"待推送 {result.count} 条超过安全闸 {threshold}; "
            "先跑 --dry-run 核对, 确认无误后加 --force"
        )

    push_index = PushIndex.load(PushIndex.default_path(project_id))
    pushed = push_batch(
        result.plan,
        settings=settings,
        project_id=project_id,
        client=client,
        index=push_index,
    )
    print(
        f"[upload] project={project_id} tasks_pushed={pushed['tasks_pushed']} "
        f"batches={pushed['batches']} "
        f"skipped_duplicate={pushed['skipped_duplicate']}"
    )
    return int(pushed["tasks_pushed"])


def _cmd_export_annotations(args: argparse.Namespace) -> int:
    """导出 LS 侧的人工裁定（**联网**读项目导出）。

    落 ``output/pipeline/calibration/v{N}/ls_annotations.jsonl``，每行含
    **两套分列的署名**（``annotator`` = LS 侧复核人；``gold.reviewed_by`` =
    金标侧原署名）——方案 22 §4.4 / 裁定④。
    """
    version = int(args.version)
    client, _settings = _client(args)
    index = GoldenProjectIndex.load(_ledger_path(args))
    record = index.get(version)
    if record is None:
        raise GoldenCliError(
            f"v{version} 还没有 LS 项目；先跑 `init-project --version {version}`"
        )
    payload = client.export_project_json(record.project_id)

    from trajectory_pipeline.evaluation.golden_ls import verdicts as verdicts_mod

    rows = _load_version(version, golden_root=args.golden_root)
    gold_rows = {str(r.get("golden_id") or ""): r for r in rows}
    records = verdicts_mod.parse_export(payload, gold_rows=gold_rows)

    out_dir = _version_dir(version)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "ls_annotations.jsonl"
    _write_text_bytes(path, verdicts_mod.dump_jsonl(records))

    summary = verdicts_mod.summarize(records)
    print(f"导出 {summary['total']} 条 → {path}")
    print(f"  已复核 {summary['reviewed']} / 未复核 {summary['unreviewed']}")
    print(f"  按档: {summary['by_split']}")
    print(f"  按裁决: {summary['by_verdict']}")
    print(f"  按复核人: {summary['by_annotator']}")
    return 0


# --------------------------------------------------------------------------
# reflow（LS 裁定 → 金标回流）
# --------------------------------------------------------------------------


def _load_annotations(golden_root: pathlib.Path | str | None) -> dict[str, dict[str, Any]]:
    """读 ``annotations.jsonl``（round=1）→ ``golden_id → 行``。

    ⚠️ 只取 round=1：round=2 是 retest（D9 的一致性复测），**不是**金标
    本体，回流挂错轮次等于改了一条重测记录。
    """
    from trajectory_pipeline.evaluation.golden.builder import ANNOTATIONS_FILE

    path = dataset.golden_root(golden_root) / ANNOTATIONS_FILE
    if not path.exists():
        raise GoldenCliError(f"{path} 不存在；先跑 `golden.builder annotate`")
    out: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise GoldenCliError(f"{path} 里有坏行：{exc}") from exc
        if int(row.get("round", 1)) != 1:
            continue
        out[str(row.get("golden_id") or "")] = row
    return out


def _read_ls_verdicts(path: pathlib.Path) -> list[dict[str, Any]]:
    """读 LS 裁定导出（``ls_annotations.jsonl``）⇒ 裁定记录列表。"""
    if not path.exists():
        raise GoldenCliError(
            f"{path} 不存在；先跑 `golden_ls export-annotations --version N`"
            "（把 LS 上的人工裁定导出来）"
        )
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError as exc:
            raise GoldenCliError(f"{path} 里有坏行：{exc}") from exc
    return out


def _cmd_reflow_worksheet(args: argparse.Namespace) -> int:
    """LS 裁定 → **回流工作表**（离线，不写金标）。

    工作表里有**三栏必须由人填**：``access_surface`` / ``observed_at``
    （确认型与改判型都要）、``reviewed_by``（**仅改判型**——「谁重新定了
    这个标签」是人的事实，推不出来）。

    ⚠️ **本命令只读不写金标**：填完工作表后要显式跑 `reflow apply`。
    """
    from trajectory_pipeline.evaluation.golden_ls import reflow as reflow_mod

    version = int(args.version)
    rows = _load_version(version, golden_root=args.golden_root)
    gold_rows = {str(r.get("golden_id") or ""): r for r in rows}
    ann_rows = _load_annotations(args.golden_root)

    export_path = pathlib.Path(args.export) if args.export else (
        _version_dir(version) / "ls_annotations.jsonl"
    )
    records = _read_ls_verdicts(export_path)

    worksheet, summary = reflow_mod.build_worksheet(
        records, gold_rows=gold_rows, ann_rows=ann_rows
    )
    out_dir = _version_dir(version)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "reflow_worksheet.jsonl"
    _write_text_bytes(path, reflow_mod.dump_jsonl(worksheet))

    print(f"裁定记录 {summary['total_records']} 条（来源 {export_path}）")
    print(
        f"  可回流：确认型 {summary['confirm']} 条 / 改判型 {summary['relabel']} 条"
    )
    print(f"  跳过 {summary['skipped']} 条（逐条原因见 {path}.skipped 明细）")
    for item in summary["skipped_detail"][:20]:
        print(f"    skipped: {item['golden_id']} — {item['reason']}")
    print(f"工作表: {path}")
    print(
        "  ⚠ 必填栏（**推不出来，只能由人填**）：access_surface / observed_at"
        + ("；改判型还要 reviewed_by（人的署名）" if summary["relabel"] else "")
    )
    print(
        "  填完跑 `reflow apply --worksheet "
        f"{path}` 核对，通过后加 --write 才写进 annotations.jsonl"
    )
    return 0


def _cmd_reflow_apply(args: argparse.Namespace) -> int:
    """回流工作表 → **写进 annotations.jsonl**（默认只核对，不写）。

    ⚠️ **默认不写**：先核对（人填的三栏齐不齐、标签在不在宇宙里、署名
    是不是人的），确认后再加 ``--write``。
    """
    from trajectory_pipeline.evaluation.golden_ls import reflow as reflow_mod

    version = int(args.version)
    rows = _load_version(version, golden_root=args.golden_root)
    gold_rows = {str(r.get("golden_id") or ""): r for r in rows}

    path = pathlib.Path(args.worksheet)
    if not path.exists():
        raise GoldenCliError(f"{path} 不存在；先跑 `reflow worksheet`")
    worksheet = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            worksheet.append(json.loads(line))
        except ValueError as exc:
            raise GoldenCliError(f"{path} 里有坏行：{exc}") from exc

    errors = reflow_mod.validate(
        worksheet, gold_rows=gold_rows, ann_rows=_load_annotations(args.golden_root)
    )
    if errors:
        print(f"工作表有 {len(errors)} 处不合格（**不写**）:", file=sys.stderr)
        for message in errors[:20]:
            print(f"  - {message}", file=sys.stderr)
        if len(errors) > 20:
            print(f"  … 另有 {len(errors) - 20} 处", file=sys.stderr)
        return 2

    kinds = {}
    for row in worksheet:
        kinds[str(row.get("kind") or "")] = kinds.get(str(row.get("kind") or ""), 0) + 1
    todo = reflow_mod.submitted(worksheet)
    held = [r for r in worksheet if not r.get("submit", True)]
    print(f"工作表 {len(worksheet)} 行：{kinds}（本轮提交 {len(todo)} 行）")
    for row in todo:
        print(
            f"  {row.get('kind')}: {row.get('golden_id')} "
            f"label={row.get('label')} face={row.get('access_surface')} "
            f"observed_at={row.get('observed_at')} "
            f"reviewed_by={row.get('reviewed_by')}"
        )
    for row in held:
        # ⚠️ 打**为什么这轮不提交**，不是打 `why`（那是「裁定可不可回流」的
        # 理由，对扣下的行没用——扣下的行往往是裁定没问题、只是三栏没齐）。
        reason = row.get("face_time_status") or row.get("why") or "三栏还没齐"
        print(
            f"  [本轮不提交] {row.get('golden_id')}：{reason}"
            "（留在工作表里，下轮接着办）"
        )
    if not todo:
        print("错误:没有要提交的行（submit=true 的一个都没有）", file=sys.stderr)
        return 2
    if not args.write:
        print("[dry-run] 未写。加 --write 写进 annotations.jsonl。")
        return 0

    result = reflow_mod.write_annotations(
        todo, golden_root=str(dataset.golden_root(args.golden_root))
    )
    print(f"→ 已写 {result['accepted']} 条标注：{result['annotation_file']}")

    versions = dataset.frozen_versions(root=args.golden_root)
    next_version = (max(versions) + 1) if versions else 1
    if kinds.get(reflow_mod.KIND_RELABEL):
        print(
            "  下一步（**改标签 = 新基线**，gate 退 2 是对的）：\n"
            f"    python -m trajectory_pipeline.evaluation.golden.builder freeze "
            f"--version {next_version} --from-version {version} --refresh-label"
        )
    else:
        print(
            "  下一步（**观测面变了不等于金标变了**，本版与上一版仍逐行可比）：\n"
            f"    python -m trajectory_pipeline.evaluation.golden.builder freeze "
            f"--version {next_version} --from-version {version} --refresh-surface"
        )
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m trajectory_pipeline.evaluation.golden_ls",
        description="冻结黄金集 → Label Studio：一版一项目（方案 22）",
    )
    ap.add_argument("--url", default=None, help="LS 地址(默认取 $TRAJECTORY_LS_URL)")
    ap.add_argument(
        "--api-key", default=None, help="LS API key(默认取 $TRAJECTORY_LS_API_KEY)"
    )
    ap.add_argument(
        "--golden-root", default=None, help="黄金集真源根(默认 evaluation/golden/data)"
    )
    ap.add_argument(
        "--ledger",
        default=None,
        help="项目台账路径(默认 output/ls_bridge/golden_projects.jsonl)",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("projects", help="台账视图(版本 → 项目 id → 条数)")

    p_pend = sub.add_parser("pending", help="冻结了但没建项目的版本(欠账清单)")
    p_pend.add_argument(
        "--strict",
        action="store_true",
        help="欠账非空就退 1(默认退 0——它是给人看的清单,不是门禁)",
    )

    p_reflow = sub.add_parser(
        "reflow", help="LS 裁定 → 金标回流(单独一步,不与上传混做)"
    )
    reflow_sub = p_reflow.add_subparsers(dest="reflow_command", required=True)
    p_ws = reflow_sub.add_parser("worksheet", help="生成回流工作表(只读,不写金标)")
    p_ws.add_argument("--version", type=int, required=True)
    p_ws.add_argument(
        "--export",
        default=None,
        help="LS 裁定导出路径(缺省 v{N}/ls_annotations.jsonl)",
    )
    p_ap = reflow_sub.add_parser("apply", help="核对工作表并写回 annotations.jsonl")
    p_ap.add_argument("--worksheet", required=True, help="reflow worksheet 产出的路径")
    p_ap.add_argument("--version", type=int, required=True, help="裁定来自哪一版")
    p_ap.add_argument(
        "--write", action="store_true", help="真写(缺省只核对,不写金标)"
    )

    p_init = sub.add_parser("init-project", help="建/复用这一版的 LS 项目(联网)")
    p_init.add_argument("--version", type=int, required=True)

    p_up = sub.add_parser("upload", help="推送某一版的冻结行")
    p_up.add_argument("--version", type=int, required=True)
    p_up.add_argument(
        "--splits",
        default=None,
        help=f"要传的档, 逗号分隔(默认三档全传: {','.join(SPLITS)})",
    )
    p_up.add_argument(
        "--exclude-holdout",
        action="store_true",
        help="不传 holdout(默认是传的——裁定③)",
    )
    p_up.add_argument(
        "--only-changed",
        action="store_true",
        help="只推相对上一版有变化的行(new/changed/id_migrated)",
    )
    p_up.add_argument(
        "--p2-root", default=None, help=f"P1 存档根(缺省 {PIPELINE_ROOT})"
    )
    p_up.add_argument(
        "--task-views-root",
        default=None,
        help=(
            "**产物根**(其下会自动接 task_views/); 缺省包内 output/pipeline。"
            "⚠️ **别多给一层 task_views/**——那会去找 task_views/task_views/*.json "
            "⇒ T 视图可得率静默变成 0%"
        ),
    )
    p_up.add_argument(
        "--golden-ids",
        default=None,
        help="只推这几条 golden_id(逗号分隔; 试点用)。点错的 id 退 2, 不静默少推",
    )
    p_up.add_argument("--limit", type=int, default=None, help="最多处理 N 条(取样调试)")
    p_up.add_argument("--batch-size", type=int, default=None, help="单批推送条数(默认 50)")
    p_up.add_argument("--dry-run", action="store_true", help="只出计划与读数, 不推")
    p_up.add_argument("--force", action="store_true", help="超安全闸时仍强制推送")

    p_exp = sub.add_parser("export-annotations", help="导出 LS 侧人工裁定(联网)")
    p_exp.add_argument("--version", type=int, required=True)
    return ap


_COMMANDS: dict[str, Any] = {
    "projects": _cmd_projects,
    "pending": _cmd_pending,
    "init-project": _cmd_init_project,
    "upload": _cmd_upload,
    "export-annotations": _cmd_export_annotations,
}

#: ``reflow`` 的二级命令（**嵌套子命令**：回流分「出工作表」与「写回」两步，
#: 中间夹着人填三栏 ⇒ 合成一条命令就没人能插进去了）。
_REFLOW_COMMANDS: dict[str, Any] = {
    "worksheet": _cmd_reflow_worksheet,
    "apply": _cmd_reflow_apply,
}


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    parser = _build_parser()
    args = parser.parse_args(argv)
    if str(args.command) == "reflow":
        handler = _REFLOW_COMMANDS.get(str(getattr(args, "reflow_command", "")))
    else:
        handler = _COMMANDS.get(str(args.command))
    if handler is None:  # pragma: no cover - argparse required=True 已拦
        parser.error(f"未知子命令: {args.command}")
        return 2
    try:
        return handler(args)
    except GoldenCliError as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 —— CLI 边界：报清楚而不是回栈
        print(f"错误:{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
