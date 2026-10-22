"""冻结时闸门（方案 16 C-09）：把 :mod:`verify` 挂进「冻结下一版」这一步。

为什么闸门挂在 freeze 上，而不是每次 push
------------------------------------------
快照的 pred 侧取自**冻结行**（``snapshot.PROVIDER_FROZEN_BRANCH`` /
``PROVIDER_ROW_DIMS``），**不由当前代码重算**。所以同一个版本对同一个版本跑
``verify``，结果恒等于「净改善 0」——按 push 跑的门第一天就永远是红的，
然后会被绕过（而绕过过的门比没有门更糟：它给过绿灯）。

能被判的只有一件事：**这一版比上一版好**。它的触发点自然是「新冻结了一版」：

    freeze --version N+1 --from-version N        # 切分/金标变了
    gate   --current-version N+1                 # 这一版比 N 好才算数

闸门做什么
----------
1. **定基线**：``--current-version`` 缺省 = 最新冻结版；基线缺省 = 它之前
   最近的一版。**没有基线 ⇒ 测不成（退 2）**，不是「首版通过」；
2. **出快照**：两版各跑一遍 ``agreement --emit-disagreements``（与人工跑的是
   同一份实现，不抄第二份）。快照不含时间戳，同输入逐字节相同，所以重算
   不引入噪声，也就没有「盘上的旧快照还算不算数」这个坑；
3. **判**：交回 :func:`verify.analyze`，四条判据一条不省。

退出码（与 ``verify`` 同一套，**1 与 2 语义不同**）
--------------------------------------------------
* ``0`` 通过——这一版可以采纳；
* ``1`` 驳回——判据没过，**这次改动不行**；
* ``2`` **测不成**——没有基线 / 冻结档坏了 / 切分跨档 / 快照读不了。
  工具故障**不许**写成回退：写成 1 会让人去查 rubric，写成 0 更糟。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import pathlib
import re
import sys
from typing import Any, Callable, Mapping, Sequence

from trajectory_pipeline.evaluation.calibration.snapshot import load_snapshot
from trajectory_pipeline.evaluation.calibration.triage import (
    TriageError,
    load_triage,
)
from trajectory_pipeline.evaluation.calibration.verify import (
    DEFAULT_SNAPSHOT_ROOT,
    GATE_PASS,
    VerifyError,
    analyze,
    render,
)
from trajectory_pipeline.evaluation.golden.builder import GOLDEN_ROOT
from trajectory_pipeline.evaluation.golden.splits import SPLIT_FILES

#: 退出码（C-08 / C-09 共用一套；调用方必须分开报）
EXIT_PASS = 0
EXIT_REJECT = 1
EXIT_UNABLE = 2

MANIFEST_FILE = "manifest.json"
_VERSION_DIR = re.compile(r"^v(\d+)$")


class GateError(Exception):
    """测不成（→ 退出码 2）。**不是**驳回：驳回意味着判据跑过且没过。"""


# --------------------------------------------------------------------------
# 选版本
# --------------------------------------------------------------------------


def list_versions(golden_root: pathlib.Path | str) -> list[int]:
    """冻结版本号（升序）。判据 = 目录里有 ``manifest.json`` 或任一档样本文件。"""
    root = pathlib.Path(golden_root)
    if not root.exists():
        return []
    out: list[int] = []
    for child in root.iterdir():
        m = _VERSION_DIR.match(child.name)
        if not m or not child.is_dir():
            continue
        has = (child / MANIFEST_FILE).exists() or any(
            (child / name).exists() for name in SPLIT_FILES.values()
        )
        if has:
            out.append(int(m.group(1)))
    return sorted(out)


def resolve_versions(
    versions: Sequence[int], *, current: int = 0, baseline: int = 0
) -> tuple[int, int]:
    """定「本轮」与「基线」。缺省：本轮 = 最新，基线 = 它之前最近的一版。

    **不给基线不许通过**：首版没有可比的上一版，那是「测不成」。
    自造一个"空基线"（0 条配对）会让净改善恒为 0 并退 1，看起来像驳回，
    实际什么都没判——两种错都不如直接说测不成。
    """
    have = sorted(int(v) for v in versions)
    if not have:
        raise GateError(
            "找不到任何冻结版本：先 freeze（黄金集数据根里没有 v*/ 目录）"
        )
    cur = int(current) if current else have[-1]
    if cur not in have:
        raise GateError(
            f"v{cur} 不是冻结版本（现有：{', '.join(f'v{v}' for v in have)}）"
        )
    if baseline:
        base = int(baseline)
        if base not in have:
            raise GateError(
                f"v{base} 不是冻结版本（现有：{', '.join(f'v{v}' for v in have)}）"
            )
        if base > cur:
            raise GateError(
                f"基线 v{base} 晚于本轮 v{cur}：闸门判的是「这一版比上一版好」，"
                "反着比出来的数字正负号会反"
            )
        if base == cur:
            raise GateError(
                f"基线与本轮都是 v{cur}：同一版本对同一版本恒等于净改善 0，"
                "这不是驳回也不是通过，是没比（预检就该拦）"
            )
        return cur, base
    earlier = [v for v in have if v < cur]
    if not earlier:
        raise GateError(
            f"v{cur} 是首版，没有可比的基线：闸门判的是「这一版比上一版好」，"
            "首版无基线 ⇒ **测不成**（不是通过）。要放行首版请走 owner 裁定，"
            "不要在闸门上开一个默认放行的口子"
        )
    return cur, earlier[-1]


# --------------------------------------------------------------------------
# 出快照
# --------------------------------------------------------------------------


def _emit_via_builder(
    version: int, *, golden_root: pathlib.Path, calibration_root: pathlib.Path
) -> pathlib.Path:
    """跑 ``agreement --version N --emit-disagreements``（进程内，同一条实现）。"""
    from trajectory_pipeline.evaluation.golden.builder import GoldenError, cmd_agreement

    ns = argparse.Namespace(
        golden_root=str(golden_root),
        version=version,
        emit_disagreements=str(calibration_root),
        json_out="",
        soft_dim=[],
        dim_tolerance=1,
    )
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cmd_agreement(ns)
    except GoldenError as exc:
        # 冻结档本身不成立（回归集被改过 / 调参池为空 / 尺子版本不认识）——
        # 这些都不是"这次改动不行"，是"这次的输入不可信"。
        raise GateError(f"v{version} 出不了快照：{exc}") from exc
    snap = calibration_root / f"v{version}" / "snapshot.json"
    if not snap.exists():
        tail = "\n".join(buf.getvalue().splitlines()[-6:])
        raise GateError(
            f"v{version} 的 agreement 跑完了但没有落快照（{snap} 不存在）。\n"
            f"命令输出末尾：\n{tail}"
        )
    return snap


Emitter = Callable[..., pathlib.Path]


def ensure_snapshot(
    version: int,
    *,
    golden_root: pathlib.Path | str,
    calibration_root: pathlib.Path | str,
    emit: bool = True,
    emitter: Emitter | None = None,
) -> pathlib.Path:
    """给出 ``v{N}/snapshot.json``。``emit=True`` 时**总是重算**，不复用盘上的旧文件。"""
    cal = pathlib.Path(calibration_root)
    snap = cal / f"v{version}" / "snapshot.json"
    if not emit:
        if not snap.exists():
            raise GateError(
                f"--no-emit 且 v{version} 的快照不存在（{snap}）："
                "要么去掉 --no-emit 让它重算，要么先人工跑 "
                f"agreement --version {version} --emit-disagreements"
            )
        return snap
    fn = emitter or _emit_via_builder
    return fn(
        version,
        golden_root=pathlib.Path(golden_root),
        calibration_root=cal,
    )


# --------------------------------------------------------------------------
# 判
# --------------------------------------------------------------------------


def run(
    *,
    golden_root: pathlib.Path | str = GOLDEN_ROOT,
    calibration_root: pathlib.Path | str = DEFAULT_SNAPSHOT_ROOT,
    current_version: int = 0,
    baseline_version: int = 0,
    triage: Sequence[Mapping[str, Any]] | None = None,
    buckets: Sequence[str] = (),
    class_tolerance: float = 0.0,
    emit: bool = True,
    emitter: Emitter | None = None,
) -> tuple[dict[str, Any], int]:
    """定基线 → 出快照 → 判。返回 ``(报告, 退出码)``；测不成抛 :class:`GateError`。

    报告在 :func:`verify.analyze` 的字段之外多一个 ``gate_meta``：
    下次有人问「这个绿是哪两版比出来的」，答案在文件里，不在命令行历史里。
    """
    cur, base = resolve_versions(
        list_versions(golden_root), current=current_version, baseline=baseline_version
    )
    paths: dict[str, pathlib.Path] = {}
    for name, version in (("baseline", base), ("current", cur)):
        paths[name] = ensure_snapshot(
            version,
            golden_root=golden_root,
            calibration_root=calibration_root,
            emit=emit,
            emitter=emitter,
        )
    try:
        baseline_snap = load_snapshot(paths["baseline"])
        current_snap = load_snapshot(paths["current"])
        report = analyze(
            baseline_snap,
            current_snap,
            triage=triage,
            buckets=buckets,
            class_tolerance=class_tolerance,
        )
    except (OSError, ValueError) as exc:
        raise GateError(f"读快照失败：{exc}") from exc
    except VerifyError as exc:
        # 加一行「这次选的是哪一对」：跨切档是最常见的「测不成」，
        # 而它的解法不是修门，是等到下一版冻结（v{N} 与它的后继才有可比性）。
        raise GateError(
            f"{exc}\n闸门这次选的是 v{base} → v{cur}；若这两版之间恰有一次"
            "切档（C-17），第一对可比的是本轮与它的**后继**"
        ) from exc

    report["gate_meta"] = {
        "baseline_version": base,
        "current_version": cur,
        "baseline_snapshot": str(paths["baseline"]),
        "current_snapshot": str(paths["current"]),
        "emitted": bool(emit),
        "exit_codes": {
            "pass": EXIT_PASS,
            "reject": EXIT_REJECT,
            "unable": EXIT_UNABLE,
        },
    }
    return report, (EXIT_PASS if report.get("gate") == GATE_PASS else EXIT_REJECT)




# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="gate",
        description=(
            "冻结时闸门（C-09）：本轮冻结版 vs 上一版 → 净改善 / 无类回归 / "
            "回归集 regressed=0 / 同桶清零。退出码 0 通过 / 1 驳回 / 2 测不成"
        ),
    )
    ap.add_argument(
        "--current-version", type=int, default=0,
        help="本轮冻结版（缺省 = 最新冻结版）",
    )
    ap.add_argument(
        "--baseline-version", type=int, default=0,
        help="基线冻结版（缺省 = 本轮之前最近的一版；没有就退 2）",
    )
    ap.add_argument(
        "--golden-root", default=str(GOLDEN_ROOT),
        help=f"黄金集数据根（默认 {GOLDEN_ROOT}）",
    )
    ap.add_argument(
        "--calibration-root", default=str(DEFAULT_SNAPSHOT_ROOT),
        help=f"派生快照根（默认 {DEFAULT_SNAPSHOT_ROOT}）",
    )
    ap.add_argument(
        "--no-emit", dest="emit", action="store_false",
        help="不重算快照（只读盘上的 snapshot.json；缺了退 2）",
    )
    ap.add_argument(
        "--class-tolerance", type=float, default=0.0,
        help="类回归容差（同 verify；缺省 0，放宽属 D-C6 阈值变更）",
    )
    ap.add_argument(
        "--triage", nargs="*", default=None,
        help="分诊表 JSONL；给了才判「同桶清零」",
    )
    ap.add_argument(
        "--bucket", nargs="*", default=[],
        help="本次改动针对的桶（配 --triage）",
    )
    ap.add_argument("--json-out", default="", help="报告落成 JSON")
    args = ap.parse_args(argv)

    try:
        report, code = run(
            golden_root=args.golden_root,
            calibration_root=args.calibration_root,
            current_version=args.current_version,
            baseline_version=args.baseline_version,
            triage=load_triage(args.triage),
            buckets=args.bucket or (),
            class_tolerance=args.class_tolerance,
            emit=args.emit,
        )
    except (GateError, VerifyError, TriageError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print(
            f"退出码 {EXIT_UNABLE}（测不成）：**不是驳回**——判据根本没跑完，"
            "别把这条读成「这次改动不行」",
            file=sys.stderr,
        )
        return EXIT_UNABLE

    meta = report["gate_meta"]
    print(
        f"⚖ 冻结时闸门：v{meta['baseline_version']} → v{meta['current_version']}"
        f"（{'已重算快照' if meta['emitted'] else '只读盘上快照'}）"
    )
    print(render(report))
    if args.json_out:
        out = pathlib.Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"→ 报告已写 {out}")
    if code == EXIT_PASS:
        print(f"退出码 {EXIT_PASS}（通过）：这一版可以采纳")
    else:
        print(
            f"退出码 {EXIT_REJECT}（驳回）：判据没过，**这次改动不行**——"
            "别修门，修改动"
        )
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "EXIT_PASS",
    "EXIT_REJECT",
    "EXIT_UNABLE",
    "GateError",
    "ensure_snapshot",
    "list_versions",
    "main",
    "resolve_versions",
    "run",
]