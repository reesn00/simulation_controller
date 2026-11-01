"""**Q-30 的读数**：那 5 条「占位的 per-unit 行」在冻结档里**留不留**。

**这个脚本只摆读数，不下结论**（结论在
`docs/计划进展/10-TODO台账.md` 的 **Q-30** 条）。

背景
----
v14–v16 的冻结档里有 **5 行** ``source_kind == "p2_sample"`` · ``url == ""``
· 金标 ``unresolved`` · 人话「**占位样本：无URL无evidence，不可人工验证，
建议排除出 D10 分子分母**」。

per-Run 之后它们**在 per-Run 的世界里没有后继**（不是站点、也不是轨迹）⇒
新冻结档里「留」还是「不留」是个**口径题**（要 owner 裁）。

⚠️ **那两个口径不是同一件事**
---------------------------
- **严格一致**（``golden_ls.report.strict_agreement``）分母 = **全部行**
  ⇒ 这 5 行**在里面**，且它们**必然不一致**（占位行没有 code 判定 ⇒
  ``code.branch`` 是空 ⇒ 与 ``unresolved`` 不等）；
- **D-10**（``golden_ls.report.d10_agreement``）分母 = **站点级已标注行**
  ⇒ 判据是 ``source_kind != "p2_sample"`` ⇒ **这 5 行本来就不算它们**
  ⇒ **留不留，D-10 一个数都不动**。

⇒ 所以「剔掉它们」唯一能动的数是**严格一致**，而且是**分母变小**
（分子一个没动）⇒ **这不是「口径变好」，是「把难看的行拿掉」**。

用法（**必须在仓库根跑**）::

    uv run python -m trajectory_pipeline.research.probes.per_run_placeholder_rows
    uv run python -m trajectory_pipeline.research.probes.per_run_placeholder_rows --version 16
"""

from __future__ import annotations

import argparse
import json
import pathlib

from trajectory_pipeline.evaluation.golden_ls import report

#: 冻结档的三个切分文件。
SPLITS = ("calibration", "regression", "holdout")

#: 缺省读哪一版冻结档（**已入库**的那一版 ⇒ 干净 clone 也能复算）。
DEFAULT_VERSION = 16


def default_golden_root() -> pathlib.Path:
    """黄金集 data 根（**包内**，不跟 cwd 走）。"""
    return (
        pathlib.Path(__file__).resolve().parents[2] / "evaluation" / "golden" / "data"
    )


def load_rows(golden_root: pathlib.Path, version: int) -> list[dict]:
    """读某一版冻结档的三份切分文件（每行补上 ``__split__``）。"""
    vdir = pathlib.Path(golden_root) / f"v{int(version)}"
    rows: list[dict] = []
    for split in SPLITS:
        path = vdir / f"{split}.jsonl"
        if not path.is_file():
            raise SystemExit(f"找不到 {path}（用 --version 指一版已冻结的档）")
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            row["__split__"] = split
            rows.append(row)
    return rows


def is_placeholder(row: dict) -> bool:
    """这一行是不是「占位的 per-unit 行」。

    判据与台账 Q-30 一致：**没有 URL** ＋ **金标 unresolved** ＋
    **``source_kind`` 是旧的 per-unit 那个值**。
    """
    golden = row.get("golden") if isinstance(row.get("golden"), dict) else {}
    return (
        str(row.get("source_kind") or "") == "p2_sample"
        and not str(row.get("url") or "")
        and str(golden.get("label") or "") == "unresolved"
    )


def _d10(rows: list[dict], split: str | None = None) -> dict:
    subset = rows if split is None else [r for r in rows if r["__split__"] == split]
    return report.d10_agreement(subset)


def show(tag: str, rows: list[dict]) -> None:
    strict = report.strict_agreement(rows)
    d10 = _d10(rows)
    head = d10.get("headline") or {}
    print(f"── {tag} ──")
    print(f"  行数             = {len(rows)}")
    print(f"  分档分布         = {report.split_counts(rows)}")
    print(f"  严格一致         = {strict['agree']} / {strict['total']}"
          f"（不一致 {strict['disagree']}）= {strict['ratio'] * 100:.2f}%")
    print(f"  D-10 整体        = {d10['agree']} / {d10['total']}"
          f" = {d10['ratio'] * 100:.2f}%（未判定 {d10['unregistered']}）")
    print(f"  D-10 头条(调参池) = {head.get('agree')} / {head.get('total')}"
          f" = {float(head.get('ratio') or 0) * 100:.2f}%")
    for split in SPLITS:
        s = _d10(rows, split)
        print(f"  D-10 {split:<12} = {s['agree']} / {s['total']}"
              f" = {s['ratio'] * 100:.2f}%")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Q-30：占位的 per-unit 行留不留")
    parser.add_argument("--version", type=int, default=DEFAULT_VERSION,
                        help=f"读哪一版冻结档（缺省 {DEFAULT_VERSION}）")
    parser.add_argument("--golden-root", default=None,
                        help="黄金集 data 根（缺省包内 evaluation/golden/data）")
    args = parser.parse_args(argv)

    root = (pathlib.Path(args.golden_root) if args.golden_root
            else default_golden_root())
    rows = load_rows(root, args.version)
    placeholders = [r for r in rows if is_placeholder(r)]

    print(f"冻结档 v{args.version}（{root}）")
    print(f"占位行 {len(placeholders)} 条：")
    for row in placeholders:
        golden = row.get("golden") if isinstance(row.get("golden"), dict) else {}
        note = str(golden.get("note") or "")
        print(f"  [{row['__split__']:<11}] {row.get('golden_id')}  note=…{note[:36]}")

    ids = [str(r.get("golden_id") or "") for r in rows]
    print(f"\n主键唯一？ {len(ids) == len(set(ids))}"
          f"（{len(ids)} 行 / {len(set(ids))} 个不同 id）")

    show(f"留（现状 v{args.version}）", rows)
    show(f"不留（剔掉 {len(placeholders)} 条占位行）",
         [r for r in rows if not is_placeholder(r)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
