"""探针：v7 校准工作表**第 14–32 行**涉及的 URL 当场复采（同一把尺子，全部走 camoufox）。

为什么
------
这 19 行全是 D-10 的 ``eq_class_mismatch``（金标与评估器落在不同等价类）。
要判「这条分歧该记哪个桶」，得先知道**页面今天是什么形态**——付费墙？登录墙？
播放器报错？还是压根打不开？光看存档不够：存档是 2026-10-12/14 采的，站点会漂。

本探针把 19 行涉及的 **13 个不同 URL** 各开一次，落四组读数：

1. ``observe`` 的采集面读数（video/iframe 计数、正文长度、标题、降级项）；
2. **四类形态标记**的命中数——付费墙（会员/开通/试看/付费/续费/购买…）·
   登录墙（登录/请先登录/扫码登录…）· 播放器报错（发生了错误/当前设备不支持播放/
   错误码…）· 可播（有 video 标签或明确播放器控件）；
3. 播放器区文案（``#bilibili-player`` / ``.bili-player`` / ``.player-wrap`` 等）；
4. 跳转链接的**最终落地 URL**（百度链接会跳转，落地 URL 才是站点身份）。

⚠️ **漂移声明**：读数是**今天**的页面状态，存档采于 2026-10-12/14。
D-10 条款边界明写「ground truth 必须为构建时预存档；评分时不得临场核验网页
（状态漂移）」——所以这些读数**只用于解释分歧成因**，不能改写金标。
分诊档的桶仍由人填（D-C3）。

用法（必须在仓库根跑）::

    uv run python trajectory_pipeline/research/probes/probe_calib_rows14_32.py

产物：同目录 ``calib_rows14_32.json``（证据，不放结论）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Sequence

from trajectory_pipeline.executor.browser.camoufox_driver import CamoufoxDriver
from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession

#: 工作表 14–32 行涉及的 URL（去重后 13 个）。
#: 键 = 短名，值 = URL。百度跳转链接在 ``REDIRECTS`` 里单独记。
URLS: dict[str, str] = {
    "baidu_link_T001": (
        "http://www.baidu.com/link?url=yhCq-6TCWKzGANYOlrjNPzNvw5O5WOjPq5X5ghpb68gueAYr2"
        "VTo-i--dS4KBLcZYpyK-vNFpluyq6Hngo7x_rU9h45AvX4GpVVQ5UWWLhKNZadUDshSfbaHaTD4I99"
        "UOy5a5OsMCM80L_5lnqA6wa"
    ),
    # 行15（golden_id=T001__67a05faa__11e1faf9）的链接：与行14 那条**不是**同一条
    # （行14 的末段是 ``...Hngo7x_rU9h45AvX4GpVVQ5...``，这条是另一串），
    # 所以行15 要单独实采，不能拿行14 的读数顶替。
    "baidu_link_T001_row15": (
        "http://www.baidu.com/link?url=yhCq-6TCWKzGANYOlrjNPzNvw5O5WOjPq5X5ghpb68gueAYr2"
        "VTo-i--dS4KBLcZ-a17grDJQoEOeOzJQrM-kWRJZsJ01ledpBwcT13OCUoKZZqFBFACt2jYy40I_S8"
        "EzOJjmlb3pg3sL0nPyQdkIa"
    ),
    "iqiyi_search_E021": (
        "https://www.iqiyi.com/so/q_%E7%9C%8B%E4%B8%8D%E8%A7%81%E7%9A%84%E5%AE%A2%E4%BA%BA"
        "%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B%E5%AE%8C%E6%95%B4%E7%89%88"
    ),
    "luketv_E023": "https://www.luketv.tv/zh/title/breaking-bad/",
    "bili_ep1340752_E014": "https://www.bilibili.com/bangumi/play/ep1340752",
    "lecom_E014": "https://www.le.com/movie/10075153.html",
    "iqiyi_v19rrk_TPIN02": (
        "https://www.iqiyi.com/v_19rrk40at8.html?vfm=2008_aldbd&fc=828fb30b722f3164"
        "&fv=p_02_01"
    ),
    "ixigua_TPIN02": (
        "https://m.ixigua.com/video/6582085495839261192?utm_source=sogou_lvideo"
        "&utm_medium=sogou_referral&utm_campaign=cooperation"
    ),
    "mgtv_314653_E021": "https://www.mgtv.com/h/314653.html",
    "sohu_TPIN02": (
        "https://tv.sohu.com/v/MjAxNTAxMTkvbjQwNzg5MTgxOC5zaHRtbA==.html"
        "?txid=2501c78c3e10c8cdcbc7f701c70db188"
    ),
    "wmgtv_314653_E021": "https://w.mgtv.com/h/314653.html?lang=zh-cn",
    "youku_E021": (
        "https://v-wb.youku.com/v_show/id_XNjA2MjY1OTI4OA==.html?vid=XNjA2MjY1OTI4OA=="
    ),
    "mgtv_328192_E017": "https://www.mgtv.com/h/328192.html",
    "bili_ep313572": "https://www.bilibili.com/bangumi/play/ep313572",
    "qq_E031": "https://v.qq.com/x/cover/uiq0rxuywu508qr/o001241efrp.html",
}

#: 四类形态的标记词表。**只数次数，不下结论**。
MARKERS: dict[str, tuple[str, ...]] = {
    "paywall": ("大会员", "开通会员", "开通", "试看", "付费", "购买", "续费", "会员专享",
                "VIP", "vip", "充值", "价格", "立即购买", "首月", "限免", "超前点播"),
    "login_wall": ("请先登录", "立即登录", "扫码登录", "登录后观看", "登录后可",
                   "登录观看", "未登录", "账号登录"),
    "player_error": ("发生了错误", "请稍后再试", "刷新页面", "当前设备不支持播放",
                     "错误码", "播放失败", "无法播放", "加载失败", "视频加载失败"),
    "playable": ("立即播放", "播放", "正片", "免费观看"),
}

#: 播放器区选择器。取第一个命中的。
PLAYER_SELECTORS = (
    "#bilibili-player", ".bili-player", ".bili-player-video-area", ".player-wrap",
    "#player", ".video-player", ".mplayer", ".play-video",
)

DOM_JS = """() => {
  const SELS = %s;
  const q = (sel) => document.querySelectorAll(sel).length;
  let area = null;
  for (const sel of SELS) {
    const el = document.querySelector(sel);
    if (el) { area = {sel: sel, text: (el.innerText || "").slice(0, 500)}; break; }
  }
  const errs = Array.from(document.querySelectorAll("*"))
    .filter(el => el.children.length === 0
      && /发生了错误|请稍后再试|当前设备不支持播放|错误码|加载失败/.test(el.textContent || ""))
    .map(el => (el.textContent || "").trim().slice(0, 60))
    .filter(Boolean).slice(0, 6);
  return {
    video_tags: q("video"),
    iframe_tags: q("iframe"),
    has_player_area: !!area,
    player_area: area,
    error_texts: errs,
    body_len: document.body ? (document.body.innerText || "").length : 0,
    title: document.title,
  };
}""" % json.dumps(list(PLAYER_SELECTORS), ensure_ascii=False)

PER_URL_TIMEOUT = 75.0


async def visit(driver: CamoufoxDriver, key: str, url: str) -> dict[str, Any]:
    row: dict[str, Any] = {"key": key, "url": url}
    started = time.time()
    try:
        await driver.goto(url)
    except Exception as exc:  # noqa: BLE001 — 单站失败不许中断整批
        row["error"] = f"goto {type(exc).__name__}: {exc}"
        return row
    # 落地 URL（百度链接会跳转）
    row["landed_url"] = driver._session.page.url  # noqa: SLF001 — 探针读现场
    try:
        obs = await driver.observe()
        body = obs.body_text or ""
        row["observation"] = {
            "url": obs.url,
            "page_title": obs.page_title,
            "video_tag_count": obs.video_tag_count,
            "iframe_count": obs.iframe_count,
            "body_chars": len(body),
            "body_source": obs.body_source,
            "degraded": list(obs.degraded),
            "elements_total": obs.elements_total,
            "links_total": obs.links_total,
            "body_head": body[:1200],
        }
        row["markers"] = {
            group: {m: body.count(m) for m in words}
            for group, words in MARKERS.items()
        }
    except Exception as exc:  # noqa: BLE001
        row["error"] = f"observe {type(exc).__name__}: {exc}"
    try:
        row["dom"] = await driver.evaluate(DOM_JS)
    except Exception as exc:  # noqa: BLE001
        row["dom"] = {"error": f"{type(exc).__name__}: {exc}"}
    row["elapsed_ms"] = int((time.time() - started) * 1000)
    return row


async def main(only: Sequence[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "读数是今天的页面状态；存档采于 2026-10-12/14（页面可能已漂移）",
        "rows": [],
    }
    targets = {k: v for k, v in URLS.items() if not only or k in set(only)}
    session = await CamoufoxSession.launch(headless=True)
    try:
        driver = CamoufoxDriver(session)
        for key, url in targets.items():
            try:
                row = await asyncio.wait_for(visit(driver, key, url), PER_URL_TIMEOUT)
            except asyncio.TimeoutError:
                row = {"key": key, "url": url, "error": f"timeout > {PER_URL_TIMEOUT}s"}
            result["rows"].append(row)
            print(f"[done] {key} -> {row.get('landed_url') or row.get('error')}", flush=True)
    finally:
        await session.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        help="只采这几个 key（逗号分隔）；已存在的产物按 key 合并，不覆盖别的读数",
    )
    args = parser.parse_args()
    only = [k.strip() for k in args.only.split(",")] if args.only else None
    data = asyncio.run(main(only))
    out = Path(__file__).with_name("calib_rows14_32.json")
    if only and out.exists():
        # 增量：按 key 合并——重采一个站不该抹掉别的站的读数。
        prev = json.loads(out.read_text(encoding="utf-8"))
        merged = {r.get("key"): r for r in prev.get("rows") or []}
        for r in data["rows"]:
            merged[r.get("key")] = r
        prev["rows"] = list(merged.values())
        prev["collected_at"] = prev.get("collected_at")
        prev["last_probe_at"] = data["collected_at"]
        data = prev
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物：{out}")
