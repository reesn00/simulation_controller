"""被排除平台的**访问级**过滤器（方案 23 A10 / R4-2(b)）。

v1 的 ``excluded_platforms`` 是**答案级**约束：``strict`` 时派生一条
硬 FAIL 准则，``advisory`` 时只进 prompt 引导。它们管的是「终局答复别
推荐这些平台」，不管「采集时有没有点进去」。

方案 23 的口径不同：交付用的训练集里**访问这些平台的步必须是 0**。
理由不是「这些平台不好」，而是：

- 任务要求排除它们，轨迹里却出现它们的页面——模型学到的是「指令可以
  不遵守」，而报告上看不出异常；
- 一条 unit 的 outcome / reflection 若指向被排除平台，而训练步里没有
  这个平台，因果链会在样本中间断掉；
- 只剔「命中那一步」会把一个 unit 截成前半段——前面几步还在为这个平台
  服务，outcome 却已经被扔掉。

所以剔的是**整条 unit**（search 或一个 site），不是单步。过滤只发生在
**组装层**（P2 ``split_archive_report`` / T ``_flatten_units`` 的调用方），
**P1 存档一个字不动**——证据与训练语料是两件事。

匹配只看 URL 的 hostname，不看页面标题：标题里出现平台名（导航、推荐位、
版权声明）不等于访问了该平台；反过来，跳到 ``www.iqiyi.com`` 才是访问。

⚠️ **2026-11-03 · re-base 到 per-Run**：per-Run 之后「unit」不再是样本的
粒度，但**剔除的边界仍然是 unit**——搜索阶段或某个访问点。判定的证据
（P1 的原始 steps）在 P2 与 T 两侧必须看**同一份**，否则同一条访问点
一个剔一个留，而报告上只会显示「少了一条」。⇒ 判定住这里，
两侧都调 :func:`excluded_unit_map`。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from trajectory_pipeline import outcomerules
from trajectory_pipeline.assembler import contracts

#: 任务库里出现的平台名 → 该平台的内容域名。
#:
#: 只收「播放/详情内容」所在的域名；``b23.tv`` 是 B 站的短链域名，跳过去
#: 仍是 B 站内容。新增平台时**先确认 tasks.yaml 里的字面名**——这个名字
#: 是过滤器的键，拼错一个字符过滤器就静默失效（不会报错，只会少剔）。
PLATFORM_DOMAINS: Mapping[str, tuple[str, ...]] = {
    "乐视视频": ("le.com", "letv.com"),
    "腾讯视频": ("v.qq.com",),
    "芒果tv": ("mgtv.com",),
    "爱奇艺": ("iqiyi.com",),
    "优酷": ("youku.com",),
    "哔哩哔哩": ("bilibili.com", "b23.tv"),
}

_DOMAIN_TO_PLATFORM: Mapping[str, str] = {
    domain: platform
    for platform, domains in PLATFORM_DOMAINS.items()
    for domain in domains
}


def platform_of_url(url: str) -> str:
    """URL 属于哪个被排除平台；不属于返回空串。

    ``hostname == domain`` 或 ``hostname.endswith("." + domain)`` 才算命中：
    用 ``in`` 判子串会把 ``notiqiyi.com`` 这类仿冒域也剔掉。
    """
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    for domain, platform in _DOMAIN_TO_PLATFORM.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return ""


def excluded_platforms(task_id: str) -> tuple[str, ...]:
    """任务声明的被排除平台名。读不到任务/字段 → 空元组（判据不适用）。

    **不看 ``excluded_platforms_severity``**：那个字段是 v1 的答案级执行
    口径（strict/advisory），与「训练集里能不能访问」是两件事。R4-2 的
    过滤对所有声明了名单的任务一视同仁。
    """
    entry = contracts.task_entry(task_id)
    if entry is None:
        return ()
    raw = entry.get("excluded_platforms")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(
        name for name in (str(x).strip() for x in raw) if name
    )


def _action_url(action: Any) -> str:
    """一条动作的 goto 网址；基础设施动作与无网址动作返回空串。"""
    if isinstance(action, Mapping):
        if action.get("origin") == "infrastructure":
            return ""
        params = action.get("params")
    else:
        if getattr(action, "origin", "") == "infrastructure":
            return ""
        params = getattr(action, "params", None)
    if not isinstance(params, Mapping):
        return ""
    return str(params.get("url") or "")


def _observation_url(obs: Any) -> str:
    if not isinstance(obs, Mapping):
        return ""
    return str(obs.get("url") or "")


def unit_excluded_platform(
    actions: Iterable[Any],
    observations: Iterable[Any],
    task_id: str,
) -> str:
    """一条 unit 访问了哪个被排除平台；没有访问返回空串。

    「访问」= **模型动作的 goto 网址**或**观察的 url** 落在排除域名上。
    ``origin="infrastructure"`` 的 ``new_tab`` / ``close_tab`` 不算：
    它们是执行器的会话管理，不是模型选的路径；其跳转后的页面若真被
    访问，紧随其后的观察会把它记下来（观察 URL 仍参与判定）。
    """
    platforms = excluded_platforms(task_id)
    if not platforms:
        return ""
    allowed = set(platforms)
    for action in actions:
        platform = platform_of_url(_action_url(action))
        if platform and platform in allowed:
            return platform
    for obs in observations:
        platform = platform_of_url(_observation_url(obs))
        if platform and platform in allowed:
            return platform
    return ""


# ── 整份存档的剔除账 ──────────────────────────────────────────────


def unit_steps(archive: Mapping[str, Any], unit: str) -> list[Any]:
    """``unit`` 名 → 它在 P1 里的**原始** ``steps[]``。

    访问级剔除要拿「这一步的观察」判 URL，所以必须回到原始 steps：

    - **搜索单元**的 P2 ``observations`` 只有一张汇总的 ``search_observation``
      （与动作不等长）。点进去的页面不在那里面，拿 Sample 字段判会漏掉
      「搜索阶段点进被排除平台」那一类；
    - T 侧读的也是逐步 observation。两边必须看**同一份证据**，否则同一条
      unit 在 P2 与 T 一个剔一个留，而报告上只会显示「少了一条」。

    ``unit`` 名不是 ``search`` / ``site-N`` 形态 → 空列表（不猜，也就不会
    误剔）。
    """
    if unit == "search":
        return list(archive.get("steps") or ())
    if not unit.startswith("site-"):
        return []
    try:
        index = int(unit[len("site-"):])
    except ValueError:
        return []
    visits = list(archive.get("visits") or ())
    if not 1 <= index <= len(visits):
        return []
    return list(visits[index - 1].get("steps") or ())


def excluded_unit_map(archive: Mapping[str, Any]) -> dict[str, str]:
    """``unit -> 被访问的排除平台``（方案 23 A10 / R4-2(b)）。

    **整条 unit 剔**：只剔命中那一步会留下半条轨迹，前面的步还在为这个
    平台服务，而结论已经被扔掉。

    P2 与 T 共用本函数——判定**只许有一份**。
    """
    task_id = str(archive.get("task_id") or "")
    out: dict[str, str] = {}
    names = ["search"] + [
        f"site-{i}" for i in range(1, len(archive.get("visits") or ()) + 1)
    ]
    for name in names:
        steps = unit_steps(archive, name)
        platform = unit_excluded_platform(
            [s.get("action") or {} for s in steps],
            [s.get("observation") for s in steps],
            task_id,
        )
        if platform:
            out[name] = platform
    return out


def excluded_outcome_indices(archive: Mapping[str, Any]) -> set[int]:
    """被剔除的访问点在 ``outcomes[]`` 里的**下标**集合。

    ⚠️ **按 URL 配对给下标**，不按位置：``outcomes[]`` 与 ``visits[]``
    不是一一对应的（孤儿记账、候选阶段记账），按位置剔会剔错行——
    实测同一批 8 份存档上「位置口径」与「URL 口径」的任务级结论
    **4 份不一致**。配对口径统一到 :mod:`trajectory_pipeline.outcomerules`。

    配不到的访问点（缺件）**不占下标**——没配到就没有行可剔。
    """
    blocked = excluded_unit_map(archive)
    if not blocked:
        return set()
    outcomes = list(archive.get("outcomes") or [])
    out: set[int] = set()
    for i, visit in enumerate(archive.get("visits") or (), 1):
        if f"site-{i}" not in blocked:
            continue
        player_obs = visit.get("player_obs")
        player_url = (
            str(player_obs.get("url") or "")
            if isinstance(player_obs, Mapping)
            else ""
        )
        hit = outcomerules.resolve_at(
            outcomes,
            str(visit.get("url") or ""),
            str(visit.get("landed_url") or ""),
            player_url,
        )
        if hit is not None:
            out.add(hit[0])
    return out


__all__ = [
    "PLATFORM_DOMAINS",
    "platform_of_url",
    "excluded_platforms",
    "unit_excluded_platform",
    "unit_steps",
    "excluded_unit_map",
    "excluded_outcome_indices",
]