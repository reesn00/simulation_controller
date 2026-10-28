"""**H-01 重测**：`unresolved` 剩余桶里有多少是「机器本可以判出来」的。

台账 H-01 那条明写：簇 C 修完之后「**旧的 46% 已不作数，必须重测**才能知道剩下
的是谁」。本脚本就是那次重测。

**做什么**：把全库每一条 `branch == "unresolved"` 的 outcome 拿去问两个
**代码里现成的判据**（不手抄词表）：

| 子族 | 判据 | 问的是什么 |
|---|---|---|
| ① | `rule_perceptor.infer_access_defect(title + body_text)` | **页面自己的正文**里有没有终局形态（404 / 地区限制 / 验证码 / 反爬） |
| ② | `rule_perceptor.classify_paywall(evidence)` | **agent 的叙述**里有没有引用门禁短语（付费墙） |

⇒ 命中 = 「机器本可以判出来，却落成了 `unresolved`」⇒ **`unresolved` 不进池**
⇒ **真负样本没进池**（池行断言是「这里真的看不了」，而事实是「机器没去问」）。

⚠️ **只统计正文真的抓到了的**（`body_text` 非空）——正文没抓到是**采集缺件**，
那是另一件事（M-27 那族），不能混进来。

⚠️ **子族 ② 的命中要当「线索」而不是「结论」**：agent 叙述是**模型自述**，
`rule_perceptor` 词表的注释自己写着「LLM evidence 是『结论+证据』混合，
『视频不存在』经常是模型自述结论而非页面文案」⇒ **拿叙述当页面证据要另设口径**。

**输入**：`trajectory_pipeline/output/pipeline/*.json`（P1 存档，**离线**，不联网）
**输出**：`trajectory_pipeline/research/probes/h01_unresolved_rebucket.json`

⚠️ **必须在仓库根跑**（路径按仓库根算）。
⚠️ **不含凭据**：这里只有页面 `inner_text`、agent 叙述与判据读数。
"""
from __future__ import annotations

import collections
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]  # ← trajectory_pipeline/
sys.path.insert(0, str(ROOT.parent))

from trajectory_pipeline.perception.rule_perceptor import (  # noqa: E402
    classify_paywall,
    infer_access_defect,
)

PIPELINE = ROOT / "output" / "pipeline"
OUT = ROOT / "research" / "probes" / "h01_unresolved_rebucket.json"

#: **当前代**的判据：**agent 执行层 + camofox 面**。更早的两代（LLM 感知层 /
#: 脚本化执行层）走的是别的收口路径 ⇒ 读数**分开报**，不混着比。
def _is_current_era(doc: dict) -> bool:
    return (doc.get("executor_mode") == "agent"
            and doc.get("driver") == "camofox")


def _batch_of(path: pathlib.Path) -> str:
    """按落盘时刻分「本批 / 基线」。**批次边界前后分布会跳变** ⇒ 分开报。"""
    # 2026-10-28 10:15 之后落盘的算 M-46 那批（`rerun-hardfail-2026-10-28`）
    ts = datetime.datetime(2026, 10, 28, 10, 15, 0).timestamp()
    return "本批" if path.stat().st_mtime >= ts else "基线"


def _visits_by_url(doc: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    by_landed: dict[str, dict] = {}
    by_site: dict[str, dict] = {}
    for v in doc.get("visits") or ():
        if v.get("landed_url"):
            by_landed.setdefault(str(v["landed_url"]), v)
        if v.get("url"):
            by_site.setdefault(str(v["url"]), v)
    return by_landed, by_site


def _closing_reason(visit: dict) -> str:
    """这次 visit 是怎么收口的。**只有 `gave_up` 那条路不查判据**。"""
    notes = " ".join(str(x) for x in (visit.get("notes") or ()))
    if "gave_up" in notes:
        return "agent 自报放弃(gave_up)"
    if "步数预算耗尽" in notes:
        return "步数预算耗尽"
    if "llm_calls 预算耗尽" in notes:
        return "llm_calls 预算耗尽"
    if "PLAYER_OK=False" in notes:
        return "PLAYER_OK=False"
    return "其它"


def main() -> int:
    rows: list[dict] = []
    for path in sorted(PIPELINE.glob("*.json")):
        if path.name.endswith(".reviewed.json"):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict) or "outcomes" not in doc:
            continue
        by_landed, by_site = _visits_by_url(doc)
        for o in doc.get("outcomes") or ():
            if str(o.get("branch") or "") != "unresolved":
                continue
            visit = (by_landed.get(str(o.get("url")))
                     or by_site.get(str(o.get("site_url"))))
            if not visit:
                continue
            site_obs = visit.get("site_obs") or {}
            body = str(site_obs.get("body_text") or "")
            if not body.strip():
                continue  # 正文没抓到 = 采集缺件，不算「机器本可以判」
            title = str(site_obs.get("page_title") or "")
            defect = infer_access_defect(title + "\n" + body)
            wall = "" if defect else classify_paywall(str(o.get("evidence") or ""))
            if not (defect or wall):
                continue
            rows.append({
                "archive": path.name,
                "task_id": str(doc.get("task_id") or ""),
                "url": str(o.get("url") or ""),
                "era": "当前代" if _is_current_era(doc) else "旧代",
                "batch": _batch_of(path),
                "closing": _closing_reason(visit),
                "shape": f"access:{defect}" if defect else "paywall",
                "hit": defect or wall,
                "body_len": int(site_obs.get("body_len") or 0),
                "evidence": str(o.get("evidence") or "")[:200],
            })

    def count(*keys: str) -> dict[str, int]:
        c = collections.Counter(" / ".join(str(r[k]) for k in keys) for r in rows)
        return dict(sorted(c.items()))

    payload = {
        "what": "H-01 重测：`unresolved` 里「机器本可以判出来」的条数",
        "judges": {
            "family_1_body": "perception.rule_perceptor.infer_access_defect(title + body_text)",
            "family_2_narrative": "perception.rule_perceptor.classify_paywall(evidence)",
        },
        "caveat": ("子族②的命中是「线索」不是「结论」——agent 叙述是模型自述，"
                   "拿它当页面证据要另设口径（见台账 Q-25）"),
        "total": len(rows),
        "by_era_batch": count("era", "batch"),
        "by_era_closing_shape": count("era", "closing", "shape"),
        "by_shape": count("shape"),
        "distinct_urls": len({r["url"] for r in rows}),
        "rows": rows,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"→ 已写 {OUT}（{len(rows)} 条）")
    for k, v in payload["by_era_closing_shape"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
