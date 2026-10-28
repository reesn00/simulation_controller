"""量「**金标冻结时若加 ``--refresh-code``**」会把 D10 读数改成什么（离线，只读）。

背景
----
D-10 的 pred 侧唯一来源是冻结行的 ``code.branch``；``--refresh-code``
是继承式冻结时把 pred 侧 ``code`` 按 ``golden_id`` 从 ``candidates.jsonl``
重挂的出口（code 是 pred 不是金标 ⇒ 重挂**不算**新基线）。

2026-10-28 冻结 v14（**键迁移版**，owner 签）时**刻意不给** ``--refresh-code``。
本脚本量的是「**给了会怎样**」——用来回答两个问题：

1. **读数差多少**：头条（calibration 档 · 口径 C）与旧口径（三档合计）
   在「不刷」与「刷」两态下各是多少；
2. **差在哪些行**：逐行列出 ``code.branch`` 变了的行，并分成两类——
   ① **改好了**（原来不一致、现在一致）；
   ② **改坏了**（原来一致、现在不一致）⇒ 后面按「新分支与它自己的
   evidence 矛盾 / 金标已陈旧」两种成因再分。

⚠️ **本脚本只读**（不写任何产物）⇒ 可以随时重跑。

用法（**必须在仓库根跑**）::

    uv run python trajectory_pipeline/research/probes/gold_refresh_code_impact.py
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path("trajectory_pipeline")
GOLDEN = ROOT / "evaluation" / "golden" / "data"

#: 语义等价类判定走**唯一出处**（不手抄第二份映射）。
from trajectory_pipeline.evaluation.golden.alignment import is_equivalent

SUCCESS = "success"


def _load(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _branch_of(code: object) -> str:
    """冻结行 ``code.branch``；空 ⇒ ``success``（与 builder 同口径）。"""
    if isinstance(code, dict):
        return str(code.get("branch") or SUCCESS)
    return SUCCESS


def _gold_of(row: dict) -> str:
    golden = row.get("golden") or {}
    return str(golden.get("label") or SUCCESS)


def _score(rows: list[dict], preds: dict[str, str]) -> tuple[int, int]:
    ok = 0
    for r in rows:
        gold = _gold_of(r)
        pred = preds.get(str(r.get("golden_id") or ""), _branch_of(r.get("code")))
        if is_equivalent(gold, pred):
            ok += 1
    return ok, len(rows)


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else "14"
    vdir = GOLDEN / f"v{version}"
    rows: list[dict] = []
    splits: dict[str, list[dict]] = {}
    for split in ("calibration", "regression", "holdout"):
        loaded = _load(vdir / f"{split}.jsonl")
        # 冻结行本身不带 ``__split__``（那是 ``agreement`` 读档时打的标）⇒ 这里自己打
        for r in loaded:
            r["__split__"] = split
        splits[split] = loaded
        rows.extend(loaded)
    if not rows:
        print(f"[FAIL] {vdir} 下没有样本")
        return 2
    cands = {
        str(r.get("golden_id") or ""): r for r in _load(GOLDEN / "candidates.jsonl")
    }
    site = [r for r in rows if r.get("source_kind") != "p2_sample"]
    headline = [r for r in site if r.get("__split__") == "calibration"]

    frozen_preds = {str(r.get("golden_id") or ""): _branch_of(r.get("code")) for r in rows}
    refreshed: dict[str, str] = {}
    missing: list[str] = []
    for r in rows:
        gid = str(r.get("golden_id") or "")
        cand = cands.get(gid)
        if cand is None:
            missing.append(gid)
            continue
        refreshed[gid] = _branch_of(cand.get("code"))

    print(f"冻结档 v{version}：行 {len(rows)} · 站点级 {len(site)}"
          f" · 头条(calibration) {len(headline)}")
    if missing:
        print(f"⚠️ candidates 里找不到 {len(missing)} 个 golden_id（不刷这批）")

    ok_f, n_f = _score(headline, frozen_preds)
    ok_r, n_r = _score(headline, refreshed)
    ok_all_f, n_all_f = _score(site, frozen_preds)
    ok_all_r, n_all_r = _score(site, refreshed)
    print("\n=== D10 读数（口径 C = 语义等价类一致） ===")
    print(f"  头条(calibration {n_f} 条)：不刷 {ok_f}/{n_f} = {ok_f/n_f:.1%}"
          f" · 刷 {ok_r}/{n_r} = {ok_r/n_r:.1%}"
          f" ⇒ Δ {(ok_r-ok_f)/n_f:+.1%}")
    print(f"  旧口径(三档合计 {n_all_f} 条)：不刷 {ok_all_f}/{n_all_f} = {ok_all_f/n_all_f:.1%}"
          f" · 刷 {ok_all_r}/{n_all_r} = {ok_all_r/n_all_r:.1%}"
          f" ⇒ Δ {(ok_all_r-ok_all_f)/n_all_f:+.1%}")

    # ── 逐行：哪些行的 pred 变了，变了之后一致率是好了还是坏了 ─────────
    changed = [
        r for r in rows
        if str(r.get("golden_id") or "") in refreshed
        and refreshed[str(r.get("golden_id") or "")] != frozen_preds[str(r.get("golden_id") or "")]
    ]
    fixed: list[dict] = []
    broke: list[dict] = []
    for r in changed:
        gid = str(r.get("golden_id") or "")
        gold = _gold_of(r)
        old = frozen_preds[gid]
        new = refreshed[gid]
        was = is_equivalent(gold, old)
        now = is_equivalent(gold, new)
        item = {
            "gid": gid, "gold": gold, "old": old, "new": new,
            "task": r.get("task_id"), "url": r.get("url"),
            "evidence": str((cands.get(gid) or {}).get("code", {}).get("evidence") or ""),
        }
        if now and not was:
            fixed.append(item)
        elif was and not now:
            broke.append(item)

    print(f"\n=== pred 变了的行：{len(changed)} 条（全部行，含轨迹级） ===")
    print(f"  改好了（旧不一致 → 新一致）：{len(fixed)} 条")
    print(f"  改坏了（旧一致 → 新不一致）：{len(broke)} 条")
    print(f"  两边都不一致（换了说法，仍不一致）："
          f"{len(changed)-len(fixed)-len(broke)} 条")

    if broke:
        annos = {
            str(a.get("golden_id") or ""): a
            for a in _load(GOLDEN / "annotations.jsonl")
        }
        print("\n=== 改坏的那些行（按成因分组） ===")
        groups: dict[str, list[dict]] = collections.defaultdict(list)
        for it in broke:
            anno = annos.get(it["gid"]) or {}
            note = str(anno.get("note") or "")
            ev = it["evidence"]
            # ① **人核已写明「存档分支名走错」**——注记里出现这句话或 INC33
            #    偏差修缮簇 ⇒ 存档里的分支名**当场就被判为错的**
            #    ⇒ 刷进来 = 把已知错的结论再灌回 D10。
            if "分支名走错" in note or "INC33_NOTE" in note:
                groups["A_人核写明存档分支名走错"].append(it)
            # ② **金标陈旧**：owner 2026-10-28 的 Q-23 现场复核（真开浏览器）
            #    已把「真·站点不可达」推翻（站点打得开）⇒ 存档分支改判
            #    ``unresolved``，而**冻结金标仍写 unreachable_hard**。
            elif "Q-23 现场复核" in ev and it["gold"] == "unreachable_hard" and it[
                "new"
            ] == "unresolved":
                groups["B_金标陈旧（现场复核已推翻，标签没跟上）"].append(it)
            else:
                groups["C_其它（口径漂移 / 未分类）"].append(it)
        for g, items in sorted(groups.items()):
            print(f"  {g}：{len(items)} 条")
        for g, items in sorted(groups.items()):
            print(f"  --- {g} ---")
            for it in items:
                print(f"    {it['gid']}  gold={it['gold']}  {it['old']} → {it['new']}")
                print(f"      evidence：{it['evidence'][:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
