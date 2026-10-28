"""Q-23 留池行的**证据补注**：把复核读数（机器码）写进 evidence。

为什么留池的那几行也要补
------------------------
M-44 之后，``unreachable_hard`` 这一支的**唯一**凭据是**机器码**
（避坑指南 §8.35：「机器码才算证据，错误文本不算」）。而存量行的
evidence 是 **obscura 面的旧文案**，还被驱动**截断到 200/245 字**
⇒ 机器码**根本没落进去**（实测：那 54 条 evidence 里 ``site_unreachable()``
**命中 0 条**）。

⇒ 留池的那几行若不补注，就与**刚撤掉的 53 条长得一模一样**
（同样是一句截断的「导航失败（传输层）」）⇒ 半年后没人分得出
「这一条凭什么留」⇒ **审计链断在池文件里**。

⚠️ **只追加，不覆盖**（与 :mod:`executor.relabel` 同款形状）：原判正文
留着（那是「代码当时写下的那句」，也是「人工与代码的分歧率」的原料），
补注另起一段。

⚠️ **判据不另写**：本脚本 import :mod:`q23_dispose_uh_rows` 的
:func:`_verdict`——**一份判据两处用**，分家就会漂。

用法（**必须在仓库根跑**）::

    uv run python trajectory_pipeline/research/probes/q23_annotate_kept_rows.py
    uv run python trajectory_pipeline/research/probes/q23_annotate_kept_rows.py --apply
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))

from q23_dispose_uh_rows import _attempts, _verdict  # noqa: E402

from trajectory_pipeline.common.atomic import write_text_atomic  # noqa: E402
from trajectory_pipeline.executor.archive import P1Archive  # noqa: E402

ROOT = pathlib.Path.cwd()
OUT_DIR = ROOT / "trajectory_pipeline" / "output" / "pipeline"
LEDGER = HERE / "q23_annotate_kept_rows.json"

#: 补注段的前缀——一眼看得出是哪一次复核写的。
MARK = "[Q-23 复核 2026-10-28]"


def _note(verdict_reason: str) -> str:
    return f"{MARK} {verdict_reason}"


def _append_evidence(rec: dict[str, Any], note: str, *,
                     replace: bool = False) -> bool:
    """往一条 outcome 的 evidence **追加**一段。已补过就跳过（幂等）。

    ⚠️ ``replace=True``：**把尾部的旧补注段换掉**（原判正文留着）。
    补注文本里若有**计数错误**（2026-10-28 实测：机器码在两个字段里各记了
    一遍 ⇒ 「4 次拒连」被写成「×8」）⇒ 改判据之后要能把旧的那段换掉，
    而不是「已补过就跳过」⇒ 那样错误会永远留在审计链里。
    """
    old = str(rec.get("evidence") or "")
    if replace:
        parts = old.split("\n\n")
        while parts and parts[-1].startswith(MARK):
            parts.pop()
        old = "\n\n".join(parts)
    if MARK in old:
        return False
    rec["evidence"] = f"{old}\n\n{note}" if old else note
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Q-23 留池行的证据补注")
    ap.add_argument("--apply", action="store_true", help="落盘（默认只报清单）")
    ap.add_argument("--replace", action="store_true",
                    help="把尾部已写过的补注段**换掉**（计数修正后用）")
    ap.add_argument("--out", default=str(OUT_DIR), help="存档目录")
    args = ap.parse_args(argv)
    base = pathlib.Path(args.out)

    attempts = _attempts()
    kept: list[tuple[str, str]] = []
    for url, atts in attempts.items():
        verdict, why = _verdict(url, atts)
        if verdict == "site_unreachable":
            kept.append((url, why))
    print(f"复核裁定为「真·站点不可达」⇒ 留池的 URL：{len(kept)} 个")
    for url, why in kept:
        print(f"  {url}\n    {why[:130]}")

    # ① 存档侧：原档 + 复核档（两层都补——门禁与报表读复核档）
    archive_hits = 0
    for path in sorted(base.glob("*.json")):
        if "__" not in path.stem:
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or "outcomes" not in doc:
            continue
        notes = {url: why for url, why in kept}
        n = 0
        for rec in doc.get("outcomes") or ():
            if not isinstance(rec, dict):
                continue
            url = str(rec.get("url") or "")
            if url not in notes:
                continue
            if str(rec.get("branch") or "") != "unreachable_hard":
                continue
            if _append_evidence(rec, _note(notes[url]), replace=args.replace):
                n += 1
        if not n:
            continue
        archive_hits += n
        print(f"  存档 {path.name}：补注 {n} 条")
        if args.apply:
            write_text_atomic(path, json.dumps(doc, ensure_ascii=False,
                                               indent=2))

    # ② 池侧：把同一段补注写进池行（**重写整份池文件**，坏行原文保留）
    store = P1Archive(base)
    pool_path = base / "negative.jsonl"
    lines = store._load_pool_lines()          # noqa: SLF001 —— 池的读法只有一份
    notes = {url: why for url, why in kept}
    pool_hits = 0
    out_lines: list[str] = []
    for raw, row in lines:
        if row is not None:
            url = str(row.get("url") or "")
            if (url in notes and str(row.get("branch") or "")
                    == "unreachable_hard"):
                if _append_evidence(row, _note(notes[url]),
                                    replace=args.replace):
                    pool_hits += 1
                    raw = json.dumps(row, ensure_ascii=False)
        out_lines.append(raw)
    print(f"  池行补注：{pool_hits} 条（坏行原文保留）")
    if args.apply and pool_hits:
        write_text_atomic(pool_path, "".join(r + "\n" for r in out_lines))

    LEDGER.write_text(json.dumps(
        {"kept": [{"url": u, "reason": w} for u, w in kept],
         "archive_evidence_appended": archive_hits,
         "pool_evidence_appended": pool_hits,
         "applied": bool(args.apply)},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"清单 → {LEDGER}"
          f"{'（已落盘）' if args.apply else '（**dry-run，未落盘**）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
