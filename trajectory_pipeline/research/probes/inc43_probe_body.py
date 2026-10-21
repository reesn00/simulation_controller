# -*- coding: utf-8 -*-
"""INC43 前研探针：无 LLM，直访 18 行失败簇 URL，只采 body_text，
跑 classify_paywall + 强弱信号分析，验证「②③负分支漏付费墙」假设。

另加对照组：当前判对的若干负分支行（no_play_control/subscription_wall
金标一致），量假阳性风险。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from trajectory_pipeline.perception.rule_perceptor import (
    PAYWALL_WORDS,
    classify_paywall,
)

HERE = Path(__file__).resolve().parent
TIMEOUT_S = 60.0
OUT = HERE / "inc43_probe_bodies.jsonl"

# 18 行失败簇（金标 subscription_wall → 代码各异）+ 对照组
TARGETS = [
    # A 簇：③ 哨兵 elements=0（iqiyi ×3）
    ("E014", "https://www.iqiyi.com/v_19rrkqj66k.html"),
    ("E017", "https://www.iqiyi.com/v_19rrcuke28.html"),
    ("TPIN02", "https://www.iqiyi.com/v_19rrk40at8.html?vfm=2008_aldbd&fc=82"),
    # B 簇：② False → login_wall_or_blocked（bilibili×4 + tudou×1）
    ("E014", "https://www.bilibili.com/bangumi/play/ep1340752"),
    ("T012", "https://www.bilibili.com/bangumi/play/ep313572"),
    ("T033", "https://www.bilibili.com/bangumi/play/ep313572"),
    ("T036", "https://www.bilibili.com/bangumi/play/ep313572"),
    ("E014", "https://play.tudou.com/v_show/id_XNDIwMjI0OTE0MA%3D%3D"),
    # C 簇：③ no_play_control（v-wb.youku / mgtv×2 / m.ixigua×2）
    ("E021", "https://v-wb.youku.com/v_show/id_XNjA2MjY1OTI4OA==.html?vid="),
    ("E021", "https://w.mgtv.com/h/314653.html?lang=zh-cn"),
    ("E021", "https://www.mgtv.com/h/314653.html"),
    ("T025", "https://m.ixigua.com/video/6582085495839261192?utm_source=so"),
    ("TPIN02", "https://m.ixigua.com/video/6582085495839261192?utm_source=so"),
    # D 簇：④ answer=null（le.com / mgtv.com h328192）
    ("E014", "https://www.le.com/movie/10075153.html"),
    ("E017", "https://www.mgtv.com/h/328192.html"),
    # E 簇：④ 记账降级（luketv / T036 bilibili）
    ("E023", "https://www.luketv.tv/zh/title/breaking-bad/"),
    # 对照组 1：金标 subscription_wall 且已判对（v.youku 正例）
    ("TPIN02", "https://v.youku.com/v_show/id_XMTQwNzMzMTc1Mg==.html?source="),
    # 对照组 2：金标 no_play_control 判对（非付费墙负样本，量假阳性）
    ("E005", "https://v.qq.com/zt/variety/thevoice.html"),
]


async def main():
    from trajectory_pipeline.executor.browser.mcp_client import McpClient
    from trajectory_pipeline.executor.browser.obscura_driver import ObscuraDriver

    rows = []
    client = McpClient.from_env()
    done = 0
    async with client:
        driver = ObscuraDriver(client)
        with OUT.open("w", encoding="utf-8") as fh:
            for (task_id, url) in TARGETS:
                rec = {"task_id": task_id, "url": url}
                try:
                    await asyncio.wait_for(driver.goto(url), timeout=TIMEOUT_S)
                    obs = await asyncio.wait_for(
                        driver.observe(max_chars=6000), timeout=TIMEOUT_S)
                    body = obs.body_text or ""
                    rec["body_len"] = len(body)
                    rec["degraded"] = list(obs.degraded or ())
                    rec["n_elements"] = len(obs.interactive_elements or ())
                    rec["paywall_word"] = classify_paywall(body)
                    # 强弱信号计数（branches.py 同款口径）
                    strong_kws = ["VIP", "订阅", "续费", "包月", "包年", "专享", "特惠"]
                    weak_kws = ["开通", "会员"]
                    rec["strong_hits"] = [k for k in strong_kws if k in body]
                    rec["weak_hits"] = [k for k in weak_kws if k in body]
                    rec["body_excerpt"] = body[:600]
                except Exception as exc:
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                rows.append(rec)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                done += 1
                pw = rec.get("paywall_word") or "-"
                print(f"[{done}/{len(TARGETS)}] {task_id} {url[:55]} "
                      f"-> paywall_word={pw}", flush=True)
    print("完成 ->", OUT)


if __name__ == "__main__":
    asyncio.run(main())
