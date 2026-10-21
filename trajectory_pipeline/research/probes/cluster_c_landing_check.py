"""簇 C 词表覆盖率核对 — INC44 教训 #1 强制前置动作。

不动生产代码,只读 ../recollect/rounds/inc44_falsified.jsonl 离线核对:
  1) 簇 C 10 行的 URL 路径段**真**不命中 _LANDING_PATH_HINTS 词表
  2) 全 75 行里所有「同页降级」样本(evidence 含 "点击播放控件前后是同一个页面")
     的 URL 路径段**真**命中词表
两边都对才能动 Y 方案(着陆页前置)。
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from urllib.parse import urlparse

OUTCOMES = (Path(__file__).resolve().parent.parent
            / "recollect" / "rounds" / "inc44_falsified.jsonl")

# 簇 C 10 行的 golden_id(增量五十七实测,vs INC44 文档 9 行有 1 行数据漂移)
CLUSTER_C_IDS = {
    "E006__2729220b__e9b9542b",
    "E005__e35b7455__b8513a71",
    "E023__4d175424__9d49aca1",
    "E014__f50809fb__dec0b09f",
    "E006__2729220b__10352791",
    "E005__e35b7455__aa3626fb",
    "E005__e35b7455__49e41ed3",
    "T001__d063ddf4__1a473949",
    "E021__94a1392f__08347b3f",
    "T011__443c5ec0__22e0fb36",
}

# 方案 Y 词表(从 04-探针七层域.md 抄来,与生产代码改动前先核对)
_LANDING_PATH_HINTS = (
    "/list", "/lists/", "/search", "/search?", "/search/",
    "/category/", "/channel/", "/index", "/index.",
    "/tag/", "/tags/", "/browse", "/discover",
    "/explore", "/library", "/results", "/all",
)


def looks_like_landing_page(url: str) -> tuple[bool, str]:
    if not url:
        return False, "<empty>"
    parsed = urlparse(url)
    path = parsed.path.lower() + ("?" + parsed.query.lower() if parsed.query else "")
    for h in _LANDING_PATH_HINTS:
        if h in path:
            return True, h
    if "search." in (parsed.netloc or "").lower():
        return True, "search.<host>"
    return False, "-"


SAME_PAGE_RE = re.compile(r"点击播放控件前后是同一个页面")


def main() -> None:
    with OUTCOMES.open("r", encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    print(f"载入 {len(rows)} 行 outcomes\n")

    # ── 1) 簇 C 10 行 URL 词表匹配 ──
    print("=" * 70)
    print("【1】簇 C 10 行 URL 词表匹配(应为 0 命中,否则词表会误伤)")
    print("=" * 70)
    # 簇 C 行也按 evidence 模板筛(数据已漂移:13 行,不是 10 行)
    cluster_c_rows = []
    for r in rows:
        ev = r.get("evidence") or ""
        if isinstance(ev, str) and SAME_PAGE_RE.search(ev):
            cluster_c_rows.append(r)
    c_hits = 0
    c_misses = []
    for r in cluster_c_rows:
        gid = r.get("task_id") or ""
        url = r.get("url") or ""
        is_land, hit = looks_like_landing_page(url)
        status = "X 命中" if is_land else "OK 不命中"
        if is_land:
            c_hits += 1
        else:
            c_misses.append((gid, url))
        print(f"  {status:8s}  {gid:8s}  hit={hit:14s}  url={url[:60]}")
    total_c = len(cluster_c_rows)
    print(f"\n簇 C: 命中 {c_hits}/{total_c}  不命中 {total_c - c_hits}/{total_c}")
    if c_hits == 0:
        print(f"  -> 词表不会误伤簇 C {total_c} 行 OK")
    else:
        print(f"  -> WARN 词表会误伤 {c_hits} 行,需补着陆页黑名单或调整词表")

    # ── 2) 全 75 行「同页降级」样本 URL 词表匹配 ──
    print("\n" + "=" * 70)
    print("【2】全 75 行里所有「同页降级」样本 URL 词表匹配(应大部分命中)")
    print("=" * 70)
    same_page_rows = []
    for r in rows:
        evidence = (
            r.get("evidence")
            or r.get("rationale")
            or r.get("decision_evidence")
            or r.get("outcome", {}).get("evidence", "")
        )
        if isinstance(evidence, str) and SAME_PAGE_RE.search(evidence):
            same_page_rows.append(r)
    sp_hits = 0
    sp_misses = []
    for r in same_page_rows:
        gid = r.get("task_id") or ""
        url = r.get("url") or ""
        is_land, hit = looks_like_landing_page(url)
        status = "OK 命中" if is_land else "X 不命中"
        if is_land:
            sp_hits += 1
        else:
            sp_misses.append((gid, url))
        print(f"  {status:8s}  {gid:8s}  hit={hit:14s}  url={url[:60]}")
    total_sp = len(same_page_rows)
    print(f"\n同页降级: 命中 {sp_hits}/{total_sp}  不命中 {total_sp - sp_hits}/{total_sp}")
    if total_sp > 0:
        rate = sp_hits / total_sp
        print(f"  → 词表覆盖率 {rate:.0%}")
        if rate == 0.0:
            print(f"  -> WARN 关键洞察:触发同页守卫的样本 100% 都是 player 落地页,")
            print(f"    没有「列表→详情同页」真降级需要词表识别")
            print(f"    -> Y 方案词表根本用不上;直接删守卫(X/W)更直接")
        elif rate >= 0.8:
            print(f"  -> OK 词表覆盖率达标(>=80%),可以动 Y 方案")
        elif rate >= 0.6:
            print(f"  -> WARN 词表覆盖率不足,需补关键词")
        else:
            print(f"  -> X 词表覆盖率太低,Y 方案失防风险高")

    # ── 3) 总结 ──
    print("\n" + "=" * 70)
    print("【3】Y 方案可行性总结")
    print("=" * 70)
    print(f"  簇 C 误伤风险: {c_hits}/{total_c} {'OK 安全' if c_hits == 0 else 'X 需调整'}")
    if total_sp > 0:
        print(f"  同页降级样本覆盖率: {sp_hits}/{total_sp} ({sp_hits / total_sp:.0%})")
    verdict = (
        "OK 词表覆盖可行,可以动 Y 方案"
        if c_hits == 0 and (total_sp == 0 or sp_hits / total_sp >= 0.8)
        else "X 词表覆盖不达标,先补词表或选 X 方案"
    )
    print(f"\n  结论: {verdict}")


if __name__ == "__main__":
    main()
