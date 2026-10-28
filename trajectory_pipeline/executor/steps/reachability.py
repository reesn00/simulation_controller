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

⚠️ **第二档失败：执行层会话失效**（2026-10-27 补）。本模块原来只有
「传输层失败」一档，而「**我们的那一页被上游销毁了**」既不是站点事实、
也不是语义问题——那个站点**一次都没被试过**。把它塞进 ``unreachable_hard``
就是**把采集侧的故障写成站点的性质**：实测一个候选连炸 9 次，
9 条全进了负样本池，而站点一次都没被试过。
⇒ 单独一档 :attr:`Reachability.session_failed`，由调用方记成
「没判出来」那一支（**不进池**）。

⚠️ **第三档失败：这次没打开 ≠ 站点不可达**（M-44 裁定，owner
2026-10-27）。「传输层失败」这一档原来太粗——它把**四种成因完全
不同**的失败全收进来：

============  ==========================  ==========================
形态          例子                        归属
============  ==========================  ==========================
超时          ``NS_ERROR_NET_TIMEOUT``    站点可能只是慢
打断          ``is interrupted by …``      **重定向确实发生了**
abort/空响应  ``NS_ERROR_ABORT``           连上了，服务器没说完
传输层其它    ``client error (Connect)``   **证明不了**站点不可达
============  ==========================  ==========================

⇒ 池里那条断言是「**这里真的看不了**」，而事实是「**这一次没打开**」
⇒ **训练信号被污染**（教「这个站看不了」，其实只是慢 / 被打断）。
⇒ 收窄成：**只有机器码证明站点本身不可达**（DNS 解析不出来 /
连接被拒）才留 ``unreachable_hard``，其余降 ``unresolved``（**不进池**）。
判据（机器码表）见 :mod:`trajectory_pipeline.executor.browser.nav_failure`——
**码表建在实测原文上**，码表认不出来的形态一律按「证明不了」处理
（fail-closed：不确定 ⇒ 不进池）。

⚠️ **第四道护栏：归因对象**（owner 裁定 2026-10-28 · 台账 **Q-24 甲**）。
上面三档问的是「**打不开**这件事是什么性质」，这一道问的是
「**这个结论说的是谁**」：百度结果给的
``http://www.baidu.com/link?url=<密文>`` **不是站点**，是搜索引擎的
中转端点——导航到它失败时，机器码证明的是「**这条链**打不开」，
那个影视站**一次都没被识别过**（``landed_url`` 为空、call log 只有一跳）
⇒ 「这里真的看不了」**没有可指的对象** ⇒ 不配记 ``unreachable_hard``，
降 ``unresolved``（机器码原文照录进 reason，证据不丢）。
"""

from __future__ import annotations

from dataclasses import dataclass

from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    DriverSessionError,
    DriverUnreachableError,
)
from trajectory_pipeline.executor.steps.search import engine_redirect_engine
from trajectory_pipeline.perception.base import Observation


@dataclass(frozen=True, slots=True)
class Reachability:
    """可达性结果。

    ``redirected_to`` 是**诊断信息**不是判据：点击后被重定向到别处是业务事实
    （比如点了播放按钮跳到登录页），落进 P1 存档供人工复核。

    ⚠️ ``session_failed`` 把**执行层故障**与**站点传输层失败**分开
    （2026-10-27）：前者坏的是**我们的浏览器会话或页面**（页被上游
    销毁 / 开不出新页 / 上游明说「会话过期、重来即可」），那个站点
    **一次都没被试过** ⇒ 不能记成 ``unreachable_hard``（那是站点性质）。

    ⚠️ ``proven_unreachable`` 是 M-44 之后**唯一**能记
    ``unreachable_hard`` 的凭据：**机器码证明**站点本身不可达
    （DNS 解析不出来 / 连接被拒）。它与 ``session_failed`` **互斥**：
    一个说「坏的是站点那一侧」，一个说「坏的是我们这一侧」。
    """

    reachable: bool
    reason: str
    requested_url: str
    landed_url: str = ""
    redirected: bool = False
    #: 失败是**执行层故障**（会话失效 / 开不出新页 / 上游明说的重试类），
    #: **不是**站点打不开。
    session_failed: bool = False
    #: 失败是**真·站点不可达**——**机器码证明**（DNS 解析不出来 /
    #: 连接被拒）。只有这一档配记 ``unreachable_hard``；其余一律
    #: ``unresolved``（**不进池**）。
    proven_unreachable: bool = False


async def probe(driver, url: str) -> Reachability:
    """导航并判定是否「硬不可达」。

    调用方**必须**自己 ``observe()``——这里只管导航。理由：导航成功 ≠ 页面
    有内容；把两件事合成一步会让「加载了但空白页」被归成可达，
    而那恰恰是 ``IS_REACHABLE`` 该判的语义问题。
    """
    try:
        await driver.goto(url)
    except DriverSessionError as exc:
        # ⚠️ **先接这一支**：它是 ``DriverError`` 的子类，顺序反了就
        # 又被当成「传输层失败」⇒ 执行层的故障被写成站点的性质。
        return Reachability(
            reachable=False,
            reason=f"执行层会话失效（不是站点打不开）: {exc}",
            requested_url=url,
            session_failed=True,
        )
    except DriverUnreachableError as exc:
        # ⚠️ **归因对象护栏**（owner 裁定 2026-10-28 · 台账 Q-24 甲）。
        # 机器码证明的是「**我们打开的那条 URL** 打不开」——而当那条 URL
        # 是**搜索引擎的结果中转链**（``www.baidu.com/link?url=<密文>``）时，
        # 「那个影视站」**从头到尾没被识别过**（``landed_url`` 为空、
        # call log 只有一跳）⇒ 「这个站真的看不了」这句话**没有可指的对象**
        # ⇒ 不配记 ``unreachable_hard``（那是**站点性质**、进负样本池）。
        #
        # ⚠️ 机器码原文**照录进 reason**（不丢证据）：判据从「站点不可达」
        # 降成「归因对象不明」，但「确实拒连了」这个事实要留在存档里。
        engine = engine_redirect_engine(url)
        if engine:
            return Reachability(
                reachable=False,
                reason=(
                    f"导航失败（机器码 {exc.code}；⚠️ 归因对象是{engine}的结果中转链"
                    f"——不是站点本身，且未解析出落点 ⇒ 不配记 unreachable_hard）: {exc}"
                ),
                requested_url=url,
            )
        # ⚠️ **再接这一支**：它也是 ``DriverError`` 的子类，但它是
        # **机器码证明过的站点事实**（DNS 解析不出来 / 连接被拒）
        # ⇒ 与上面那支正好相反：这一支**是**站点性质，配记
        # ``unreachable_hard``。机器码标签进 reason，人核时看得出
        # 「凭什么判成站点不可达」。
        return Reachability(
            reachable=False,
            reason=f"导航失败（真·站点不可达·机器码 {exc.code}）: {exc}",
            requested_url=url,
            proven_unreachable=True,
        )
    except DriverError as exc:
        # ⚠️ **兜底这一档 = 「这次没打开」**：超时 / 打断 / abort /
        # 空响应 / 建页失败……它们**证明不了**站点不可达 ⇒
        # 调用方按 ``unresolved`` 记（**不进池**）。
        return Reachability(
            reachable=False,
            reason=f"导航失败（这次没打开·证明不了站点不可达）: {exc}",
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