"""**M-30 现场取证**：把那 10 条待人核的行对应的 URL 重新打开看一遍。

**为什么要有这个脚本**（2026-10-23，M-30 收尾）

M-30 的机器侧结论已经摆在 ``evaluation/golden/data/url_gold_conflicts.md``：
19 个多头金标 URL 里真正要人核的是**那 10 行**「金标不被自己的 ``code.evidence``
支撑」。但那些证据全是**存档里的**——存档记的是**当时那一次**的观察，
而观察可能是「上一个文档的残留」（URL 已是目标页，DOM 还停在跳转前），
也可能是「**页面还没渲染完就读了**」。

这个脚本**只给事实**：把同一批 URL 现场再采一遍，
把**现场读数**与**存档读数**并排摆出来，
并对一组与金标标签直接相关的关键词打命中（付费墙 / 登录墙 / 地区限制 / 预告 / 播放中 …）。

**现场 ≠ 存档** 有两种成因，处置相反：

============================  ==========================================
成因                          处置
============================  ==========================================
站点**现在**返回的是别的样子  当期存档判据仍成立（它记的是当时）
采集层读到了**跳转前的残留**   **采集层缺陷**——要修的是「goto 之后没确认文档已换」
============================  ==========================================

**分不清这两者，就不该动判断点的阈值**（台账 H-15）。

**每个 URL 采两读**（这是本脚本最要紧的一处）：``first`` = 导航一返回就观察，
``settled`` = 再等 ``--settle`` 秒后观察同一页。两次读数的差就是
「观察时机太早」的量——2026-10-23 实测 ``v.qq.com/x/cover/uiq0rxuywu508qr/…``
立刻读 **81 字**、等 4 秒读 **3686 字**，而存档里那一读是 **84 字**
⇒ 存档那读是**半成品**，不是站点漂移。**只记事实，不下结论。**

**导航失败的兜底**：实测 ``www.ixigua.com`` 会**中断** ``wait_until="load"``
的导航（``NS_BINDING_ABORTED``）⇒ 换 ``domcontentloaded`` 重来一次；
两次都失败就记失败，**不猜**。

**只写探针自己的 JSON**：不写 P1、不改代码、不并池、不动金标。

**用法**（必须在仓库根跑；**联网**）::

    uv run python -m trajectory_pipeline.research.probes.m30_live_check \\
        --json trajectory_pipeline/research/probes/m30_live_check.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any, Mapping

#: 要现场打开的 7 个 URL——M-30 清单里定性为「待查」的 7 条
#: （对应 10 条待人核的行）。**顺序 = 清单里的节号顺序**。
#: ⚠️ 这只是**默认清单**；`--targets-file` / `--url` 可以换掉它
#: （M-30 收尾时用它现场核了清单里剩下那 12 节）。
TARGETS: tuple[str, ...] = (
    "https://v.qq.com/x/cover/uiq0rxuywu508qr/o001241efrp.html",
    "https://www.ixigua.com/6532729724869804557?utm_source=baidu_lvideo",
    "http://tv.sohu.com/v/MjAxNTAxMTkvbjQwNzg5MTgxOC5zaHRtbA==.html"
    "?txid=2501c78c3e10c8cdcbc7f701c70db188",
    "http://v.youku.com/v_show/id_XMTQwNzMzMTc1Mg==.html?source=baidu"
    "&refer=sousuotoufang_market.qrwang_00002944_000000_QJFFvi_19031900",
    "https://www.iqiyi.com/v_19rrk40at8.html?vfm=2008_aldbd"
    "&fc=828fb30b722f3164&fv=p_02_01",
    "https://m.chusuiwenhua.com/bq/2te.html",
    "https://v.qq.com/x/page/s3237561yvx.html",
)

#: 存档目录——P1 存档都在这里（顶层 `*__*.json` ＝ `{task_id}__{run_id}.json`）。
ARCHIVE_DIR = pathlib.Path("trajectory_pipeline/output/pipeline")

#: 只并排比较这三个 run（**默认清单那 7 个 URL** 对应的存档）：
#: `T011__443c5ec0` 一条、`TPIN02__1a5e526d` / `TPIN02__1cf5deca` 各五条。
#: ⚠️ 换 `--targets-file` 时**不要**用它——那些 URL 的存档在别的 run 里，
#: 用 `--all-archives` 扫全目录（顶层 `*__*.json`）。
ARCHIVE_FILES: tuple[str, ...] = (
    "T011__443c5ec0.json",
    "TPIN02__1a5e526d.json",
    "TPIN02__1cf5deca.json",
)

#: 第二读之前等多久（秒）。**4 秒**是 2026-10-23 实测出来的值：
#: 短于此读不到渲染完成，长于此只是白等。
DEFAULT_SETTLE_SECONDS = 4.0

#: 关键词 → 这条关键词在**正文**里出现时说明什么。
#: 只收与这批金标标签（付费墙 / 登录墙 / 地区限制 / 预告 / 播放中 / 采集降级）直接相关的。
KEYWORDS: dict[str, str] = {
    "开通会员": "付费墙信号（要钱）",
    "续费会员": "付费墙信号（要钱）",
    "开通": "付费墙信号（要钱）",
    "VIP": "付费墙信号（要钱）",
    "会员": "付费墙信号（要钱）",
    "付费": "付费墙信号（要钱）",
    "购买": "付费墙信号（要钱）",
    "订阅": "付费墙信号（要钱）",
    "登录": "登录墙信号（要登录）",
    "未登录": "登录墙信号（要登录）",
    "请先登录": "登录墙信号（要登录）",
    "当前设备不支持播放": "地区限制 / 播放器不可用（仓库口径：70017001 = 地区限制）",
    "70017001": "地区限制错误码（仓库口径：地区限制）",
    "错误码": "播放器报错（要看后面跟的码）",
    "正在播放": "播放页（成功）",
    "预告": "预告片（trailer_only）",
    "试看": "试看片段",
    "免费": "免费播放",
    "开通App观看": "App 端独占",
    "打开App观看": "App 端独占",
    "发生了错误": "播放器组件故障（component_unverified）",
    "请稍后再试": "播放器组件故障（component_unverified）",
}


def _safe(text: Any) -> str:
    """按控制台编码转义（Windows 控制台是 GBK，见避坑指南 §6.3）。"""
    s = str(text)
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return s.encode(enc, "backslashreplace").decode(enc)
    except (LookupError, UnicodeError):
        return s.encode("ascii", "backslashreplace").decode("ascii")


def _p(*parts: Any) -> None:
    print(" ".join(_safe(x) for x in parts))


def _norm(url: str) -> str:
    """归一化 URL 到「够用来配对」的程度——**不比查询串**。"""
    u = str(url or "").strip()
    if not u:
        return ""
    for sep in ("#", "?"):
        idx = u.find(sep)
        if idx >= 0:
            u = u[:idx]
    return u.rstrip("/").lower()


def _hits(body: str) -> dict[str, int]:
    """正文里关键词的命中次数。**只给次数，不给结论**。"""
    out: dict[str, int] = {}
    for kw in KEYWORDS:
        n = body.count(kw)
        if n:
            out[kw] = n
    return out


def _archived(files: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """存档目录 → ``归一化 URL`` → 当时的观察读数（每个存档文件各一条）。"""
    names = files if files is not None else list(ARCHIVE_FILES)
    index: dict[str, list[dict[str, Any]]] = {}
    for name in names:
        path = ARCHIVE_DIR / name
        if not path.exists():
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for visit in rec.get("visits") or ():
            if not isinstance(visit, Mapping):
                continue
            obs = visit.get("site_obs")
            if not isinstance(obs, Mapping):
                continue
            key = _norm(str(visit.get("landed_url") or obs.get("url") or ""))
            if not key:
                continue
            body = str(obs.get("body_text") or "")
            index.setdefault(key, []).append({
                "archive": name,
                "landed_url": visit.get("landed_url"),
                "obs_url": obs.get("url"),
                "page_title": obs.get("page_title"),
                "body_len": obs.get("body_len"),
                "body_head": body[:200],
                "elements_total": obs.get("elements_total"),
                "video_tag_count": obs.get("video_tag_count"),
                "iframe_count": obs.get("iframe_count"),
                "degraded": list(obs.get("degraded") or ()),
                "body_source": obs.get("body_source"),
                "keywords": _hits(body),
            })
    return index


def _all_archive_files() -> list[str]:
    """存档目录顶层的全部 ``{task_id}__{run_id}.json``。"""
    if not ARCHIVE_DIR.is_dir():
        return []
    return sorted(
        p.name for p in ARCHIVE_DIR.glob("*.json")
        if "__" in p.name and not p.name.startswith("_")
    )


async def _read(driver: Any) -> dict[str, Any]:
    """读一次页面。**只记事实**；读失败记失败，不猜。"""
    try:
        obs = await driver.observe(max_chars=4000)
    except Exception as exc:  # noqa: BLE001 —— 探针不该被单站失败带崩
        return {"error": f"{type(exc).__name__}: {exc}"}
    body = obs.body_text or ""
    return {
        "observed_url": obs.url,
        "page_title": obs.page_title,
        "body_len": len(body),
        "body_head": body[:300],
        "body_full": body,
        "elements_total": obs.elements_total,
        "video_tag_count": obs.video_tag_count,
        "iframe_count": obs.iframe_count,
        "degraded": list(obs.degraded),
        "body_source": obs.body_source,
        "keywords": _hits(body),
    }


async def _probe(driver: Any, url: str, *, settle_seconds: float) -> dict[str, Any]:
    """采一遍——**两读**（立刻 / 等渲染），失败就记失败，不猜。"""
    wait_until = "load"
    try:
        await driver.goto(url, wait_until="load")
    except Exception as exc:  # noqa: BLE001
        first_err = f"{type(exc).__name__}: {exc}"
        # 实测有的站点会中断 `load` 导航（ixigua：NS_BINDING_ABORTED）⇒ 换一种等待重来
        wait_until = "domcontentloaded"
        try:
            await driver.goto(url, wait_until=wait_until)
        except Exception as exc2:  # noqa: BLE001
            return {
                "url": url,
                "wait_until": wait_until,
                "error": f"{type(exc2).__name__}: {exc2}",
                "first_error": first_err,
            }

    first = await _read(driver)
    if settle_seconds > 0:
        await asyncio.sleep(settle_seconds)
    settled = await _read(driver)
    return {
        "url": url,
        "wait_until": wait_until,
        "settle_seconds": settle_seconds,
        "first": first,
        "settled": settled,
    }


def _targets(args: argparse.Namespace) -> list[str]:
    """要采的 URL 清单——``--targets-file`` / ``--url`` 顶掉默认那 7 个。"""
    out: list[str] = []
    if args.targets_file:
        path = pathlib.Path(args.targets_file)
        for line in path.read_text(encoding="utf-8").splitlines():
            u = line.strip()
            if u and not u.startswith("#"):
                out.append(u)
    for u in args.url or ():
        u = u.strip()
        if u:
            out.append(u)
    return out or list(TARGETS)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from trajectory_pipeline.executor.browser.camoufox_driver import CamoufoxDriver
    from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession

    targets = _targets(args)
    report: dict[str, Any] = {
        "driver": "camoufox",
        "headless": not args.headful,
        "settle_seconds": args.settle,
        "targets": list(targets),
        "results": [],
    }
    archived = _archived(_all_archive_files() if args.all_archives else None)

    session = await CamoufoxSession.launch(headless=not args.headful)
    async with session:
        report["fingerprint"] = session.fingerprint
        driver = CamoufoxDriver(session)
        for url in targets:
            res = await _probe(driver, url, settle_seconds=args.settle)
            res["archived"] = archived.get(_norm(url), [])
            report["results"].append(res)
    return report


def _print_one(tag: str, r: Mapping[str, Any]) -> None:
    if r.get("error"):
        _p(f"   {tag:<8}: 读取失败: {r['error']}")
        return
    _p(f"   {tag:<8}: url={r.get('observed_url')}")
    _p(f"   {'':<8}  title={r.get('page_title')}")
    _p(f"   {'':<8}  body={r.get('body_len')}ch 源={r.get('body_source')} "
       f"degraded={r.get('degraded')}")
    _p(f"   {'':<8}  元素: total={r.get('elements_total')} "
       f"video={r.get('video_tag_count')} iframe={r.get('iframe_count')}")
    _p(f"   {'':<8}  命中: {r.get('keywords') or '（无）'}")


def _print(report: Mapping[str, Any]) -> None:
    _p(f"driver = {report['driver']}  headless = {report['headless']}  "
       f"settle = {report['settle_seconds']}s")
    for r in report["results"]:
        _p(f"\n=== {r['url']}")
        if r.get("error"):
            _p(f"   采集失败: {r['error']}")
            if r.get("first_error"):
                _p(f"   （第一次 {r['wait_until']} 导航也失败: {r['first_error']}）")
            continue
        _p(f"   wait_until = {r['wait_until']}")
        _print_one("立刻读", r["first"])
        _print_one("等定后", r["settled"])
        for a in r.get("archived") or ():
            _p(f"   ── 存档 {a['archive']}")
            _p(f"      title    : {a['page_title']}")
            _p(f"      body     : {a['body_len']}ch  源={a['body_source']} "
               f"degraded={a['degraded']}")
            _p(f"      元素     : total={a['elements_total']} video={a['video_tag_count']} "
               f"iframe={a['iframe_count']}")
            _p(f"      命中     : {a['keywords'] or '（无）'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="M-30 现场取证：重采那批 URL（每站两读）")
    ap.add_argument("--headful", action="store_true", help="有头浏览器（缺省无头）")
    ap.add_argument("--settle", type=float, default=DEFAULT_SETTLE_SECONDS,
                    help=f"第二读之前等几秒（缺省 {DEFAULT_SETTLE_SECONDS}）")
    ap.add_argument("--json", default=None, help="落盘路径")
    ap.add_argument("--targets-file", default=None,
                    help="要采的 URL 清单文件（一行一个；给了就顶掉默认那 7 个）")
    ap.add_argument("--url", action="append", default=None,
                    help="追加一个要采的 URL（可重复；与 --targets-file 叠加）")
    ap.add_argument("--all-archives", action="store_true",
                    help="并排比较**全部**存档（顶层 *_*.json），不只默认那三个 run")
    args = ap.parse_args(argv)

    report = asyncio.run(_run(args))
    _print(report)
    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        _p(f"\n落盘: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
