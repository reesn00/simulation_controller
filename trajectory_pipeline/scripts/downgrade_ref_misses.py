"""一次性重判脚本（2026-10-10）——把「采集缺件当业务结论」的 outcome 降级。

两类形态，**判据不同但同一条纪律**：

1. **找不到控件 ref**（12 条，``tv.sohu`` / ``v-wb.youku``）——判定理由
   全是「找不到可点击的控件 ref」，而同 URL 另有判**成功**的存档。
   ref 是会话内句柄，采不到不等于控件不存在。
2. **0 个交互元素**（4 条，``iqiyi`` / ``ixigua``）——判定理由是
   「0 个交互元素中无一命中播放控件词表」，而[避坑指南 §5.15]明写：
   哨兵返回无法区分「页面没渲染完」与「页面确实没有控件」，
   **判 False 就是把采集失败写成业务负样本**。

两类都按 CLAUDE.md 的 fail-closed（I4）与「unresolved 不是负样本」：
降级 ``unresolved``（人工复核分支，不入负样本池）。

⚠️ **现状已修**：LLM 版与规则版现在都有「0 元素 → fail-closed」守卫，
所以重跑不会再产生第 2 类；本脚本处理的是**历史存量**。

跑法（幂等，已降级的不会再次命中）::

    uv run python trajectory_pipeline/scripts/downgrade_ref_misses.py
"""

from __future__ import annotations

import collections
import json
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1] / "output" / "pipeline"

#: 形态 1：同 URL 另有成功记录，而本条却判了负分支。
URLS_WITH_SUCCESS = (
    "tv.sohu.com/v/MjAxNTAxMTkvbjQwNzg5MTgxOC5zaHRtbA==",
    "v-wb.youku.com/v_show/id_XNDE0NjYzODAzNg==",
)
#: 形态 2：判定理由自称「0 个交互元素」——采集失败的形态，不是业务结论。
#:
#: ⚠️ **必须精确匹配，而且只能看原始判定正文**。两个坑叠在一起：
#:
#: 1. ``"0 个交互元素" in "200 个交互元素"`` 是 **True**——子串匹配；
#: 2. 每次降级/恢复都会在 evidence 后面追加一段中文说明，而**那段说明
#:    里也会出现「0 个交互元素」这几个字**。第二版改用正则却仍用
#:    ``search`` 全文匹配，于是自己写的说明又把刚恢复的 5 条重新命中。
#:
#: 所以判据走 :func:`_base_evidence`——**只取第一段**（原判定正文），
#: 任何追加的备注一律不参与判断。负向前视保证 ``200 个`` 不被误认。
ZERO_ELEMENT_RE = re.compile(r"(?<!\d)0 个交互元素")

OLD_BRANCHES = {"no_play_control", "not_play_site"}

STAMP_REF = (
    "人工降级 2026-10-10：本条的否定依据是「找不到可点击的控件 ref」，"
    "而**同 URL 另有判成功的存档**。找不到 ref 是**采集层的缺件**，"
    "不是「这个站没有播放控件」这个业务结论——按 fail-closed"
    "（I4）应记 unresolved 转人工复核，不贴负标签"
)
STAMP_NP = STAMP_REF.replace("找不到可点击的控件 ref", "判断点 ① 未选中该站点")
STAMP_ZERO = (
    "人工降级 2026-10-10：本条的否定依据是「0 个交互元素中无一命中"
    "播放控件词表」，而**哨兵返回无法区分「页面没渲染完」与「页面确实"
    "没有控件」**（避坑指南 §5.15）。判 False 就是把**采集失败**写成"
    "业务负样本——按不变式 I4 应记 unresolved 转人工复核。"
    "现状：LLM 版与规则版均已加「0 元素 → fail-closed」守卫，重跑不再产生"
)


def _base_evidence(evidence: str) -> str:
    """**只取原始判定正文**——去掉 ``[原判 X] `` 前缀与所有 ``

``
    之后的追加段落。

    这是判据正确性的关键：追加的说明里也会出现「0 个交互元素」字样
    （本脚本的 NOTE 自带这句话），全文匹配会把它当成判据——
    第二版就是这么把刚恢复的 5 条又降级了一次。
    """
    text = evidence
    if text.startswith("[原判 ") and "]" in text:
        text = text[text.find("]") + 1:]
    return text.split("\n\n")[0]


def _why(branch: str, evidence: str) -> str:
    """按形态选理由——**理由要写清是哪一种**，否则半年后没人知道
    这条当初凭什么被降级。"""
    if ZERO_ELEMENT_RE.search(_base_evidence(evidence)):
        return STAMP_ZERO
    if branch == "not_play_site":
        return STAMP_NP
    return STAMP_REF


def main() -> int:
    changed: collections.Counter = collections.Counter()
    touched: list = []
    for p in sorted(ROOT.glob("T*__*.json")):
        a = json.loads(p.read_bytes().decode("utf-8"))
        led = a.get("ledger") or {}
        by = led.get("by_branch") or {}
        n = 0
        for o in a.get("outcomes") or []:
            b = o.get("branch")
            if b not in OLD_BRANCHES:
                continue
            evidence = o.get("evidence") or ""
            url = o.get("url") or ""
            if not (any(x in url for x in URLS_WITH_SUCCESS)
                    or ZERO_ELEMENT_RE.search(_base_evidence(evidence))):
                continue
            o["branch"] = "unresolved"
            o["evidence"] = f"[原判 {b}] {evidence}\n\n{_why(b, evidence)}"
            o["reached_play_page"] = False
            if "is_negative_sample" in o:
                o["is_negative_sample"] = False
            by[b] = max(0, by.get(b, 0) - 1)
            by["unresolved"] = by.get("unresolved", 0) + 1
            n += 1
            changed[b] += 1
        if not n:
            continue
        if led:
            led["by_branch"] = {k: v for k, v in by.items() if v}
            led["negative_samples"] = max(0, led.get("negative_samples", 0) - n)
            miss = led.setdefault("missing_branches", [])
            if "unresolved" not in miss:
                miss.append("unresolved")
            miss.sort()
            a["ledger"] = led
        a.setdefault("warnings", []).append(
            "人工降级 2026-10-10："
            f"{n} 条 outcome 由负分支降级为 unresolved"
            "（否定依据是采集缺件，不是业务结论）"
        )
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(a, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        touched.append((p.stem, n))

    print(f"改动 {len(touched)} 份存档：{dict(changed)} 合计 {sum(changed.values())} 条")
    for name, n in touched:
        print(f"  {name} ×{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())