"""量**金标候选集重切（v14）**要用的四组事实（离线，只读）。

背景
----
M-42：池行候选的 ``golden_id`` 里嵌的是**池文件行号**（``__neg{i:02d}``）
⇒ 池一增删，后面所有行的 id 前移/后移 ⇒ **人核标签静默掉线**。

重切要动的东西是「**人的核数据**」⇒ 动之前先把事实量清楚：

1. **新口径唯一吗**——``(task, run, url, branch)`` 在池里唯一 ⇒ 新 id 唯一；
2. **旧 id → 新 id 的映射是不是 1:1**——重切就是靠这张表换键，
   **有歧义就不能换**（换错 = 标签挂到别的行上）；
3. **重建候选集之后，现行人核标注还挂得上吗**——挂不上的行数就是
   「这次重切会弄丢多少人工核过的东西」；
4. **哪些历史冻结版本含有旧 id**——历史档**不得改写**（golden 不覆盖历史
   版本）⇒ 迁移期必须**双 id 并存**，这决定要建什么映射表。

用法（**必须在仓库根跑**）::

    uv run python trajectory_pipeline/research/probes/gold_candidate_rekey.py
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path("trajectory_pipeline")
GOLDEN = ROOT / "evaluation" / "golden" / "data"
PIPELINE = ROOT / "output" / "pipeline"


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


def _old_id(task: str, run: str, url: str, index: int) -> str:
    """现行口径（行号后缀）——照抄 builder 的写法，别手抄第二份真源。"""
    import hashlib

    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return f"{task}__{run}__{h}__neg{index:02d}"


def _new_id(task: str, run: str, url: str, branch: str) -> str:
    """新口径（分支后缀）——身份口径，不依赖池里的行号。"""
    import hashlib

    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return f"{task}__{run}__{h}__neg_{branch}"


def main() -> int:
    cands = _load(GOLDEN / "candidates.jsonl")
    annos = _load(GOLDEN / "annotations.jsonl")
    pool = _load(PIPELINE / "negative.jsonl")
    print(f"现行候选集 {len(cands)} 行 · 标注 {len(annos)} 行 · 池 {len(pool)} 行")
    if not cands or not pool:
        print("[FAIL] 读不到候选集/池")
        return 2

    # ── 判据①：新口径唯一吗 ────────────────────────────────────────────
    k_new = [
        (r.get("task_id"), r.get("run_id"), r.get("url"), r.get("branch"))
        for r in pool
    ]
    print(f"\n① 池里 (task,run,url,branch) 唯一: {len(set(k_new))}/{len(k_new)}")
    ids_new = [_new_id(t, r, u, str(b or "")) for t, r, u, b in k_new]
    dup = [i for i, n in collections.Counter(ids_new).items() if n > 1]
    print(f"   新 id 唯一: {len(set(ids_new))}/{len(ids_new)}"
          + (f" · 重复 {dup[:3]}" if dup else ""))

    # ── 判据②：旧 id → 新 id 的映射是不是 1:1 ─────────────────────────
    neg_cands = [c for c in cands if "__neg" in str(c.get("golden_id") or "")]
    pool_by_access: dict[tuple[str, str, str], list[dict]] = {}
    for r in pool:
        pool_by_access.setdefault(
            (str(r.get("task_id")), str(r.get("run_id")), str(r.get("url"))), []
        ).append(r)
    mapping: dict[str, str] = {}
    ambiguous: list[str] = []
    for c in neg_cands:
        old = str(c.get("golden_id"))
        key = (
            str(c.get("task_id")),
            str(c.get("run_id")),
            str(c.get("url")),
        )
        hits = pool_by_access.get(key, [])
        if len(hits) != 1:
            ambiguous.append(f"{old} → 池里 {len(hits)} 行同名访问")
            continue
        branch = str(hits[0].get("branch") or "")
        mapping[old] = _new_id(key[0], key[1], key[2], branch)
    print(f"\n② 旧→新映射：{len(mapping)} 条可换"
          + (f" · ⚠️ 歧义 {len(ambiguous)} 条" if ambiguous else " · 无歧义"))
    for a in ambiguous:
        print(f"   ⚠️ {a}")
    for old, new in list(mapping.items())[:3]:
        print(f"   例：{old} → {new}")

    # ── 判据③：重建候选集后，人核标注还挂得上吗 ───────────────────────
    # 重建口径 = 现行 builder 的三段扫描（存档 + 池 + P2 样例）。
    from trajectory_pipeline.evaluation.golden import builder as gb

    archives = gb._scan_archives(PIPELINE)
    keys = {(a["task_id"], a["run_id"], a["url"]): a for a in archives}
    negatives = gb._scan_negative_pool(PIPELINE, keys)
    p2_existing = {c["golden_id"] for c in archives + negatives}
    p2 = gb._scan_p2_samples(PIPELINE, p2_existing)
    rebuilt = archives + negatives + p2
    rebuilt_ids = {str(c.get("golden_id")) for c in rebuilt}
    # 池行 id 用**新口径**（分支后缀）——上面的扫描还是旧口径，这里单独补上
    new_neg_ids = {
        _new_id(
            str(r.get("task_id")),
            str(r.get("run_id")),
            str(r.get("url")),
            str(r.get("branch") or ""),
        )
        for r in pool
        if (str(r.get("task_id")), str(r.get("run_id")), str(r.get("url"))) not in keys
    }
    rebuilt_ids |= new_neg_ids

    old_ids = {str(c.get("golden_id")) for c in cands}
    ann_ids = [str(a.get("golden_id")) for a in annos]
    attach_now = [i for i in ann_ids if i in old_ids]
    attach_after = [i for i in ann_ids if i in rebuilt_ids]
    attach_after_alias = [
        i for i in ann_ids if i in rebuilt_ids or mapping.get(i) in rebuilt_ids
    ]
    print(f"\n③ 重建候选集：{len(rebuilt)} 行"
          f"（存档 {len(archives)} + 池 {len(negatives)} + P2样例 {len(p2)}）")
    print(f"   标注 {len(ann_ids)} 条 → 现行候选集挂得上 {len(attach_now)}"
          f" · 重建后（直挂）{len(attach_after)}"
          f" · 重建后（经映射表）{len(attach_after_alias)}")
    orphan = [i for i in ann_ids if i not in attach_after_alias]
    print(f"   ⚠️ 重建后仍挂不上的标注：{len(orphan)} 条")
    for i in orphan[:10]:
        print(f"      {i}")

    # ── 判据③b：v13 冻结行在新候选集里配不配得上 ──────────────────────
    v13_dir = GOLDEN / "v13"
    v13_rows: list[dict] = []
    for f in sorted(v13_dir.glob("*.jsonl")):
        v13_rows.extend(_load(f))
    v13_ids = {str(r.get("golden_id") or "") for r in v13_rows}
    missing = sorted(i for i in v13_ids if i not in rebuilt_ids
                     and mapping.get(i) not in rebuilt_ids)
    print(f"\n③b v13 冻结行 {len(v13_rows)} 行 / {len(v13_ids)} 键"
          f" → 新候选集配不上的：{len(missing)}")
    for i in missing[:8]:
        print(f"      {i}")
    # code 侧漂移（刷新 code 会改几行）
    new_code = {}
    for c in rebuilt:
        new_code[str(c.get("golden_id"))] = (c.get("code") or {}).get("branch")
    for c in cands:
        new_code.setdefault(str(c.get("golden_id")), (c.get("code") or {}).get("branch"))
    drift = 0
    for r in v13_rows:
        gid = str(r.get("golden_id") or "")
        want = new_code.get(gid, new_code.get(mapping.get(gid, "")))
        if want is not None and want != (r.get("code") or {}).get("branch"):
            drift += 1
    print(f"    code 侧漂移（刷新会改的行）：{drift}")

    # ── 判据④：哪些历史版本含旧 id（历史档不得改写 ⇒ 要双 id 并存） ────
    print("\n④ 历史冻结版本里的旧 id：")
    for v in range(1, 30):
        vdir = GOLDEN / f"v{v}"
        if not vdir.exists():
            continue
        ids: set[str] = set()
        for f in sorted(vdir.glob("*.jsonl")):
            for row in _load(f):
                ids.add(str(row.get("golden_id") or ""))
        hit = sorted(i for i in ids if "__neg" in i)
        if hit:
            print(f"   v{v}: {len(hit)} 条 → {hit[0]} …")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
