"""Q-23 出池处置：把负样本池里**存量那 54 条** ``unreachable_hard`` 逐条撤掉。

为什么要有这一步
----------------
M-44（owner，2026-10-27）把 ``unreachable_hard`` 收窄成「**真·站点不可达**
（DNS 解析失败 / 连接被拒）」，机器码判据单点 =
``executor/browser/nav_failure.py::site_unreachable()``；「这次没打开」
（超时 / 打断 / 空响应 / 建页失败 / 会话失效 / 快照失败）⇒ ``unresolved``
（**不进池**）。

但**池里那 54 条是改判之前写进去的**，而池是 append-only、**不回填**
⇒ 改判只对**新行**生效 ⇒ 存量那批必须**逐条复核后出池**（Q-23）。

复核怎么做的
------------
现场开真浏览器重开每条 URL（``research/probes/camofox_q23_uh_recheck.py``，
机器码分歧的两条又补了 ``camofox_q23_uh_settle.py``）。**判据就是
M-44 的机器码口径**，不是「打得开就出池」这种散文判据：

* ``opens`` ⇒ 打得开 ⇒ 原判「站点不可达」**不成立** ⇒ 出池；
* 其余形态（超时 / 空响应 / 连接重置 / SSL 证书错 / 会话故障）⇒
  **证明不了站点不可达** ⇒ 同样是「这次没打开」⇒ 出池；
* 只有「每次尝试都拿到真·不可达机器码」才留在池里。

本脚本**只做处置**，不做判定：判定在两份探针产物里，理由逐条生成
（原样进 ``evidence``，那是半年后唯一的依据）。

处置顺序（**先改存档、后撤池行**）
--------------------------------
1. ``cli downgrade --url <URL> --branch unreachable_hard --reason <why>``
   —— 存档侧落 ``unresolved``。**原档 + 复核档两层都落**（2026-10-28
   改：门禁与报表读的是**复核档**，只改原档会当场报
   ``negative_not_pooled``）；
2. ``cli pool-drop --url <URL> --branch unreachable_hard --reason <why>``
   —— 池行撤掉，原行进 ``pool_dropped.jsonl`` 审计日志。

⚠️ **每条都先 dry-run，且 dry-run 与 apply 的命中数必须 1:1**——
对不上就**中止**，不许「apply 完再看」。这是池这个 append-only 资产的
唯一安全带。

⚠️ **不投票**：机器码分歧的那条（friday.tw/29289，9 次里 2 次拿到
``NS_ERROR_CONNECTION_REFUSED``）按 M-44 口径记「**复现不成立**」，
原始次数**写进理由**里给人看，不做多数决。

用法（**必须在仓库根跑**）::

    uv run python trajectory_pipeline/research/probes/q23_dispose_uh_rows.py            # dry-run
    uv run python trajectory_pipeline/research/probes/q23_dispose_uh_rows.py --apply    # 落盘
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = pathlib.Path.cwd()
OUT_DIR = ROOT / "trajectory_pipeline" / "output" / "pipeline"
RECHECK = HERE / "camofox_q23_uh_recheck.json"
SETTLE = HERE / "camofox_q23_uh_settle.json"
LEDGER = HERE / "q23_dispose_uh_rows.json"

#: 处置里要撤的分支。
TARGET_BRANCH = "unreachable_hard"

#: **真·站点不可达**的机器码——与
#: :mod:`trajectory_pipeline.executor.browser.nav_failure` **同源**
#: （跨面词表不同、语义相同：DNS 解析不出来 / 连接被拒）。
UNREACHABLE_CODES = (
    "NS_ERROR_UNKNOWN_HOST",
    "NS_ERROR_CONNECTION_REFUSED",
    'ConnectError("dns error"',
    "kind: ConnectionRefused",
)

#: 从 CLI 输出里抠出两层的命中条数。
COUNT_RE = re.compile(r"(原档|复核档)\s+(\d+)\s+条")


def _attempts() -> dict[str, list[dict]]:
    """两次探针的尝试记录**并到按 URL 归拢**的一张表。

    补测（settle）只针对机器码分歧的那两条，但表是同一张 ⇒ 理由里
    能说全「一共试了几次、拿到过几次不可达码」。
    """
    out: dict[str, list[dict]] = {}
    for path in (RECHECK, SETTLE):
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        for row in doc.get("results") or ():
            out.setdefault(str(row.get("url") or ""), []).extend(
                row.get("attempts") or ())
    return out


def _verdict(url: str, attempts: list[dict]) -> tuple[str, str]:
    """这条 URL 的裁定 + **写进 evidence 的理由**。

    ⚠️ **判据就是执行器自己那把尺**（:mod:`executor.steps.reachability`）：
    执行器**单次**导航里，机器码证明（DNS 解析不出来 / 连接被拒）⇒
    ``unreachable_hard``；其余（超时 / 打断 / abort / 空响应 / 建页失败 /
    会话失效）⇒ ``unresolved``（不进池）。

    ⇒ 复核问的就是同一个问题：**这条 URL 有没有出现过真·不可达机器码、
    且从未打开**。

    ⚠️ **不投票、不设复现率阈值**：那是本脚本早期的一版做法，已废。
    「出现过一次拒连」就是「服务器拒了连接」这一**站点事实**——
    与执行器单次导航的判据完全一致；而超时只说明「网络静默」，
    **证明不了**（避坑指南 §8.35：``NS_ERROR_NET_TIMEOUT`` 两侧都不算
    「真·站点不可达」）。原始次数**写进理由**里给人看。
    """
    oks = [a for a in attempts if a.get("kind") == "ok"]
    if oks:
        first = oks[0].get("first") or {}
        where = str(first.get("observed_url") or "")
        title = str(first.get("page_title") or "")
        return ("opens",
                f"Q-23 现场复核（2026-10-28，camofox 真开浏览器）：打得开"
                f"——落地 {where}，标题「{title}」⇒ "
                f"「真·站点不可达」不成立，原判是「这次没打开」")
    codes: dict[str, int] = {}
    for a in attempts:
        # ⚠️ **同一次尝试的机器码只算一次**：``site_unreachable`` 那次尝试的码
        # 在 ``code``（异常对象的 ``.code``）与 ``ns_error_codes``（正文里正则
        # 抽出的码）**两个字段里各记了一遍** ⇒ 按字段累加会把「4 次拒连」
        # 写成「×8」⇒ **理由文本是审计记录，不能带这个错**。
        seen: set[str] = {str(c) for c in (a.get("ns_error_codes") or ())}
        code = str(a.get("code") or "")
        if code:
            seen.add(code)
        for c in seen:
            codes[c] = codes.get(c, 0) + 1
    hits = sum(n for c, n in codes.items()
               if any(u in c for u in UNREACHABLE_CODES))
    seen = "、".join(f"{c}×{n}" for c, n in sorted(codes.items())) or "无机器码"
    if hits:
        return ("site_unreachable",
                f"Q-23 现场复核（2026-10-28，camofox 真开浏览器，"
                f"试 {len(attempts)} 次、0 次打开）：拿到过真·不可达机器码"
                f"（{seen}）⇒ 站点性质成立，留池（原始次数照录，不投票）")
    return ("not_proven",
            f"Q-23 现场复核（2026-10-28，camofox 真开浏览器，"
            f"试 {len(attempts)} 次、0 次打开）：机器码**证明不了**"
            f"站点不可达（{seen}）⇒ 「这次没打开」，"
            f"按 M-44 口径落 unresolved（不进池）")


def _run(argv: list[str]) -> tuple[int, str]:
    """跑一条 CLI 命令（**参数数组**，URL 里有 ``&`` 与 ``?``）。"""
    env = {"PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "trajectory_pipeline.executor.cli", *argv],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        env={**env, "PATH": _path(), "SYSTEMROOT": _sysroot()},
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _path() -> str:
    import os
    return os.environ.get("PATH", "")


def _sysroot() -> str:
    import os
    return os.environ.get("SYSTEMROOT", "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Q-23 出池处置")
    ap.add_argument("--apply", action="store_true", help="落盘（默认只报清单）")
    ap.add_argument("--out", default=str(OUT_DIR), help="存档目录")
    args = ap.parse_args(argv)
    out_dir = pathlib.Path(args.out)

    attempts = _attempts()
    pool_path = out_dir / "negative.jsonl"
    pool = [json.loads(l) for l in
            pool_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    targets = sorted({str(r.get("url") or "") for r in pool
                      if str(r.get("branch") or "") == TARGET_BRANCH})
    print(f"池里 {TARGET_BRANCH} 行 "
          f"{sum(1 for r in pool if r.get('branch') == TARGET_BRANCH)} 条 / "
          f"{len(targets)} 个 URL")

    ledger: list[dict] = []
    for i, url in enumerate(targets, 1):
        verdict, why = _verdict(url, attempts.get(url, []))
        rows = [r for r in pool if r.get("url") == url
                and r.get("branch") == TARGET_BRANCH]
        entry: dict = {"url": url, "verdict": verdict, "pool_rows": len(rows),
                       "reason": why}
        print(f"\n[{i}/{len(targets)}] {verdict:18s} {url[:90]}")
        print(f"    池行 {len(rows)} 条 · {why[:120]}")

        if verdict == "site_unreachable":
            print("    ⇒ 站点性质成立，**留池**，不处置")
            entry["action"] = "kept"
            ledger.append(entry)
            continue

        # ① 存档侧：降级（dry-run → apply，命中数必须 1:1）
        dg = ["downgrade", "--out", str(out_dir), "--url", url,
              "--branch", TARGET_BRANCH, "--reason", why]
        rc, text = _run(dg)
        dry = {m.group(1): int(m.group(2))
               for m in COUNT_RE.finditer(text)}
        if rc != 0:
            print(f"    [stop] downgrade dry-run 退出码 {rc}\n{text}")
            return rc
        if args.apply:
            rc, text = _run([*dg, "--apply"])
            got = {m.group(1): int(m.group(2))
                   for m in COUNT_RE.finditer(text)}
            if rc != 0:
                print(f"    [stop] downgrade --apply 退出码 {rc}\n{text}")
                return rc
            if got != dry:
                print(f"    [stop] dry-run 命中 {dry} ≠ apply 命中 {got}"
                      f"——已中止\n{text}")
                return 3
            print(f"    降级：{dry}")
        else:
            print(f"    将降级：{dry}")

        # ② 池侧：撤行（dry-run → apply，命中数必须 1:1）
        pd_ = ["pool-drop", "--out", str(out_dir), "--url", url,
               "--branch", TARGET_BRANCH, "--reason", why]
        rc, text = _run(pd_)
        m = re.search(r"==\s*\**将\**撤掉\s+(\d+)\s+行池记录", text)
        dry_rows = int(m.group(1)) if m else 0
        if rc != 0:
            print(f"    [stop] pool-drop dry-run 退出码 {rc}\n{text}")
            return rc
        if dry_rows != len(rows):
            print(f"    [stop] pool-drop 命中 {dry_rows} ≠ 池里该 URL 的 "
                  f"{len(rows)} 条——判据与池对不上，已中止\n{text}")
            return 3
        if args.apply:
            rc, text = _run([*pd_, "--apply"])
            m = re.search(r"已撤\s+(\d+)\s+行", text)
            got_rows = int(m.group(1)) if m else -1
            if rc != 0 or got_rows != dry_rows:
                print(f"    [stop] pool-drop apply 撤 {got_rows} 行 ≠ "
                      f"dry-run {dry_rows} 行——已中止\n{text}")
                return 3
            print(f"    撤池行：{got_rows}")
        else:
            print(f"    将撤池行：{dry_rows}")
        entry["action"] = "downgraded+pool_dropped"
        ledger.append(entry)

    LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"\n处置清单 → {LEDGER}")
    kept = sum(1 for e in ledger if e["action"] == "kept")
    print(f"处置 {len(ledger) - kept} 个 URL，留池 {kept} 个；"
          f"{'已落盘' if args.apply else '**dry-run，未落盘**'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
