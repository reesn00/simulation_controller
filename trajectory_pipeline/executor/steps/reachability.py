"""代码层可达性判定——``unreachable_hard`` 的**唯一**产地。

范围刻意收得极窄：**只有导航动作抛 :class:`DriverError` 才算不可达**。

理由是三层分工的边界：DNS 解析失败、连接超时、TLS 错误——这些是
**传输层事实**，没有任何语义判断空间，代码判就是对的。而

    403 + 挑战页 / 302 到登录页 / 200 但内容是「请登录」/ 地区限制提示

这些**需要理解页面在说什么**，属于语言层，归 ``IS_REACHABLE`` 让感知层判。
把它们混进 ``unreachable_hard`` 会让负样本池长出一支语义不纯的样本——
人工复核时看到「不可达」，去页面上却发现内容正常，就再也信不过这张表了。

另一个刻意的取舍：**不把 HTTP 状态码当判据**。obscura 的
``browser_navigate`` 只返回 ``Navigated to <url> — "<title>"``，没有状态码；
要用 ``browser_network_requests`` 补一次调用，但那个列表混着页面自己发起的
全部资源请求，从中挑出主文档的那一条要靠启发式——**用一个猜出来的信号
去判失败分支，比不用更糟**。
"""

from __future__ import annotations

from dataclasses import dataclass

from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.perception.base import Observation


@dataclass(frozen=True, slots=True)
class Reachability:
    """可达性结果。

    ``redirected_to`` 是**诊断信息**不是判据：点击后被重定向到别处是业务事实
    （比如点了播放按钮跳到登录页），落进 P1 存档供人工复核。
    """

    reachable: bool
    reason: str
    requested_url: str
    landed_url: str = ""
    redirected: bool = False


async def probe(driver, url: str) -> Reachability:
    """导航并判定是否「硬不可达」。

    调用方**必须**自己 ``observe()``——这里只管导航。理由：导航成功 ≠ 页面
    有内容；把两件事合成一步会让「加载了但空白页」被归成可达，
    而那恰恰是 ``IS_REACHABLE`` 该判的语义问题。
    """
    try:
        await driver.goto(url)
    except DriverError as exc:
        return Reachability(
            reachable=False,
            reason=f"导航失败（传输层）: {exc}",
            requested_url=url,
        )
    return Reachability(reachable=True, reason="导航成功", requested_url=url)


def check_landing(obs: Observation, requested_url: str) -> Reachability:
    """导航成功后核对落地 URL。

    只报告重定向，**不改判可达性**——重定向到登录页是业务事实
    （登录墙），归 ``IS_REACHABLE``；重定向到搜索引擎首页多半是站点挂了，
    也该由 ``IS_REACHABLE`` 结合正文判断。代码在这里越权判，
    就会把语义问题伪装成传输事实。
    """
    landed = obs.url or ""
    same = _normalize(landed) == _normalize(requested_url)
    if same or not landed:
        return Reachability(True, "落地 URL 与请求一致", requested_url, landed, False)
    return Reachability(
        True,
        f"被重定向：{requested_url} → {landed}（判据交给 IS_REACHABLE）",
        requested_url, landed, True,
    )


def _normalize(url: str) -> str:
    """比 URL 时去掉尾斜杠与 fragment——``/`` 与空 fragment 不该算重定向。"""
    base = url.split("#", 1)[0].strip()
    return base.rstrip("/").lower() or base