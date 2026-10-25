# -*- coding: utf-8 -*-
"""把重采 outcomes.jsonl 的新判定写回 candidates.jsonl 的 code 字段（v12 → v13）。

从仓库根跑（与同目录 measure.py / visit.py 同约定）：

    uv run python trajectory_pipeline/research/recollect/apply_code_writeback.py

口径（最小改动）：
  * 只动「冻结集内（v12）的 golden_id」且 (task_id, url) 能在同目录
    outcomes.jsonl 命中、且重采 branch 与冻结行 branch **不同**的行 —— 本次实测 26 行。
  * 这些行的 code 子字典**整份替换**为重采那次的 code（去掉 task_id / url 两个连接键）。
    重采 code 的键集**不固定**（站点级行只有 5 键），按原样搬，不补齐、不补默认。
  * 其余字段（golden / dimension_scores / scores_detail / status / …）一字不动；
    未命中的行 re-serialize 后必须与原行**逐字节一致**（下面有断言）。

写回之后，新 code 由
``builder.py freeze --version <N> --from-version <M> --refresh-code`` 挂到冻结链，
再用 ``agreement --version <N>`` 出正式读数——**这一步不在本脚本里**。

⚠️ 一次性：candidates.jsonl 只该被写一次。应用后再跑会因 changed != 预期而
   fail-closed（SystemExit），不会二次污染。
⚠️ 回滚：``git checkout -- trajectory_pipeline/evaluation/golden/data/candidates.jsonl``
   （该文件已跟踪，pre-image 也可从 git 取）；本脚本另存一份 pre-image 到 .tmp/。
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: 仓库根相对路径（**在仓库根跑**；与 measure.py / visit.py 同约定）。
G = Path("trajectory_pipeline/evaluation/golden/data")
CAND = G / "candidates.jsonl"
OUT = HERE / "outcomes.jsonl"
SPLITS = ("calibration", "holdout", "regression")

#: 写回对准的冻结版本（v12）。
DEFAULT_VERSION = 12
#: 预期改动行数；不符即 fail-closed。
EXPECT_CHANGED = 26
#: pre-image 备份落点（.tmp/ 已 gitignore；也可从 git 取）。
BAK = Path(".tmp") / "candidates.jsonl.bak"


def load(p):
    return [
        json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()
    ]


def new_code(o):
    return {k: v for k, v in o.items() if k not in ("task_id", "url")}


def main(version=DEFAULT_VERSION, expect=EXPECT_CHANGED):
    raw = CAND.read_bytes()
    text = raw.decode("utf-8")
    if not text.endswith("\r\n"):
        raise SystemExit("candidates.jsonl 末尾没有换行，先人工核对")
    lines = text.split("\r\n")[:-1]
    rows = [json.loads(x) for x in lines]

    frozen = {}
    for split in SPLITS:
        p = G / f"v{version}" / (split + ".jsonl")
        if p.exists():
            for r in load(p):
                frozen[r["golden_id"]] = split
    omap = {(o["task_id"], o["url"]): o for o in load(OUT)}

    changed = []
    reserialized = []
    for line, row in zip(lines, rows):
        gid = row["golden_id"]
        key = (row.get("task_id"), row.get("url"))
        if gid in frozen and key in omap:
            old_b = (row.get("code") or {}).get("branch")
            if old_b != omap[key]["branch"]:
                row["code"] = new_code(omap[key])
                changed.append((gid, frozen[gid], old_b, omap[key]["branch"]))
        reserialized.append(json.dumps(row, ensure_ascii=False, sort_keys=True))

    diff_idx = [i for i, (a, b) in enumerate(zip(lines, reserialized)) if a != b]
    if len(changed) != expect or len(diff_idx) != expect:
        raise SystemExit(
            f"改动行数与预期不符：changed={len(changed)} "
            f"diff={len(diff_idx)} expect={expect}"
        )
    # 未命中的行必须逐字节不变：改动只允许落在「冻结内且 outcomes 命中」的行上。
    hit_idx = set()
    for i, line in enumerate(lines):
        row = rows[i]
        if row["golden_id"] in frozen and (row.get("task_id"), row.get("url")) in omap:
            hit_idx.add(i)
    if not set(diff_idx) <= hit_idx:
        raise SystemExit("有改动落在未命中行上")

    BAK.parent.mkdir(parents=True, exist_ok=True)
    BAK.write_bytes(raw)
    CAND.write_bytes(("\r\n".join(reserialized) + "\r\n").encode("utf-8"))

    print(f"OK 改写 {len(changed)} 行；备份 -> {BAK}")
    by = {}
    for gid, split, ob, nb in changed:
        by.setdefault(split, []).append((gid, ob, nb))
    for split in SPLITS:
        items = by.get(split, [])
        print(f"  [{split}] {len(items)} 行")
        for gid, ob, nb in items:
            print(f"    {gid}  {ob if ob is not None else '(None=success)'} -> {nb}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
