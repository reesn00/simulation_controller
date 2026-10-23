"""探针：T040__c9982a1c__22e0fb36（B 站《功夫》播放页）**当场复采**。

为什么有这件事
--------------
v7 校准工作表最后一行是一条 D-10 分歧卡：金标 ``component_unverified``
（页面播放器报「发生了错误」且 video=0），评估器给 ``login_wall_or_blocked``。
两侧等价类不同 ⇒ 真分歧。要判「谁对」得看**页面当时是什么形态**。

本探针做的事只有一件：用**生产同款 camoufox 执行层**（同 session 参数、同
driver、同 observe 口径）打开该 URL，落下四组读数：

1. **采集面读数**（``observe``）——video/iframe 计数、正文、标题、降级项，
   与存档里的 observation 同字段，可直接对照；
2. **正文标记计数**——「发生了错误 / 请稍后再试 / 登录 / 大会员 / 试看」等
   词各多少处，判定页面是**登录墙**、**付费墙**还是**播放器报错**；
3. **DOM 读数**——播放器区是否存在、页内有没有支付/登录浮层；
4. **站点接口读数**——B 站 ``pgc/player/web/playurl`` 的返回码
   （``-10403`` = 大会员专享），用来独立验证「这集到底是不是会员内容」。

⚠️ **漂移声明**：读数是**今天**的页面状态，而存档是 **2026-10-14** 采的。
D-10 条款边界明写「ground truth 必须为构建时预存档；评分时不得临场核验网页
（状态漂移）」——所以本探针的读数**只用于解释分歧成因**，**不能**用来改写
金标或替人做分诊裁定（桶由人填，D-C3）。

用法（必须在仓库根跑）::

    uv run python trajectory_pipeline/research/probes/probe_bili_ep313572.py

产物：同目录 ``bili_ep313572.json``（证据，不放结论）。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from trajectory_pipeline.executor.browser.camoufox_driver import CamoufoxDriver
from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession

URL = "https://www.bilibili.com/bangumi/play/ep313572"
EP_ID = "313572"

OUT = Path(__file__).with_name("bili_ep313572.json")

#: 判读用的词表。**只数次数，不下结论**——结论由人给。
MARKERS: tuple[str, ...] = (
    "发生了错误", "请稍后再试", "刷新页面",
    "登录", "大会员", "试看", "开通", "付费", "购买", "会员",
)

#: 页内读数脚本。返回的每项都是**页面上读回来的事实**。
DOM_JS = """() => {
  const q = (sel) => document.querySelectorAll(sel).length;
  const txt = (sel) => {
    const el = document.querySelector(sel);
    return el ? (el.innerText || "").slice(0, 600) : null;
  };
  const errs = Array.from(document.querySelectorAll("*"))
    .filter(el => (el.children.length === 0)
      && /发生了错误|请稍后再试|播放器|出错了/.test(el.textContent || ""))
    .map(el => (el.textContent || "").trim().slice(0, 80))
    .filter(Boolean)
    .slice(0, 8);
  return {
    video_tags: q("video"),
    iframe_tags: q("iframe"),
    has_player_area: !!document.querySelector(
      "#bilibili-player, .bili-player, .bili-player-video-area"),
    player_area_text: txt("#bilibili-player, .bili-player, .bili-player-video-area"),
    vip_masks: q(".bili-mini-mask, .pay-vip, .bili-vip-pay, .vip-pay-popup"),
    login_masks: q(".bili-mini-login-tip, .login-tip, .bili-login-mask, .login-panel"),
    error_texts: errs,
    body_len: (document.body ? (document.body.innerText || "").length : 0),
    title: document.title,
  };
}"""


async def main() -> dict[str, Any]:
    started = time.time()
    result: dict[str, Any] = {
        "url": URL,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "读数是今天的页面状态；存档采于 2026-10-14（页面可能已漂移）",
    }
    # ``launch`` 是协程的 classmethod：先 await 拿会话句柄，再用它。
    session = await CamoufoxSession.launch(headless=True)
    try:
        driver = CamoufoxDriver(session)
        result["fingerprint"] = session.fingerprint
        await driver.goto(URL)
        # 与采集面同口径：goto 之后取一次结构化观察。
        obs = await driver.observe()
        body = obs.body_text or ""
        result["observation"] = {
            "url": obs.url,
            "page_title": obs.page_title,
            "video_tag_count": obs.video_tag_count,
            "iframe_count": obs.iframe_count,
            "body_chars": len(body),
            "body_source": obs.body_source,
            "raw_len": obs.raw_len,
            "degraded": list(obs.degraded),
            "elements_total": obs.elements_total,
            "links_total": obs.links_total,
            "body_head": body[:2000],
        }
        result["markers"] = {m: body.count(m) for m in MARKERS}
        # DOM 读数在 observe 之后取：观察本身是多次往返，再插一次 evaluate
        # 可能读到跨文档的半成品——页内读数只用于辅助，不参与 fail-closed 判定。
        try:
            result["dom"] = await driver.evaluate(DOM_JS)
        except Exception as exc:  # noqa: BLE001 — 探针不许因单项失败而中断
            result["dom"] = {"error": f"{type(exc).__name__}: {exc}"}
        # 站点接口读数：独立验证「这集是不是大会员专享」。
        try:
            api = await session.context.request.get(
                "https://api.bilibili.com/pgc/player/web/playurl"
                f"?ep_id={EP_ID}&qn=16&fnval=1&fourk=1",
                headers={
                    "Referer": URL,
                    "User-Agent": result["fingerprint"].get("ua", ""),
                },
                timeout=20000,
            )
            payload = await api.json()
            data = payload.get("data") or {}
            result["playurl_api"] = {
                "http": api.status,
                "code": payload.get("code"),
                "message": payload.get("message"),
                "is_drm": data.get("is_drm"),
                "quality": data.get("quality"),
                "accept_quality": data.get("accept_quality"),
                "accept_description": data.get("accept_description"),
                "trial_start": data.get("trial_start"),
                "max_time": data.get("max_time"),
            }
        except Exception as exc:  # noqa: BLE001
            result["playurl_api"] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        await session.close()
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


if __name__ == "__main__":
    data = asyncio.run(main())
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n产物：{OUT}")
