"""导航失败的**机器码判据**——「真·站点不可达」的**唯一**依据。

M-44 裁定（owner，2026-10-27）
------------------------------
``unreachable_hard`` 这一支原来收了**成因完全不同**的失败：建页路由失败
（**根本没导航过**）、导航被打断（**重定向确实发生了**）、传输层超时
——它们**都不是**「这个站看不了」，只是「**这一次没打开**」。
⇒ 拆两支：

- **真·站点不可达**（DNS 解析不出来 / 连接被拒）⇒ 留在
  ``unreachable_hard``（**站点性质**，进负样本池）；
- **这次没打开**（超时 / 打断 / 建页失败 / 会话失效 / 快照失败 /
  其它传输层失败）⇒ 降 ``unresolved``（**没判出来**，**不进池**）。

判据必须落在**机器码**上，不能落在散文上
----------------------------------------
「超时」「连接失败」「DNS 解析失败」这些**英文短语**在两个上游里措辞
不同、还会**本地化**（obscura 在 Windows 上给的是中文「不知道这样的
主机。」）⇒ 拿散文当判据 = 猜。
⇒ 本模块只认**浏览器/内核自己给的网络错误码**。

⚠️ **码表建在实测原文上**，不是按文档抄的：

- ``research/probes/camofox_nav_failure_codes.json``——camofox 面，
  **生产驱动**实测（``CamofoxDriver.goto``）。
- ``research/probes/obscura_nav_failure_codes.json``——obscura 面，
  **绕过驱动层 200 字截断**、直接调 MCP tool 拿**完整原文**。

两个面各一套词汇，但**语义口径相同**
------------------------------------
============  =================================  ==============================
语义          camofox（Firefox / Playwright）      obscura（Rust / reqwest）
============  =================================  ==============================
DNS 解析不出来 ``NS_ERROR_UNKNOWN_HOST``          ``ConnectError("dns error"``
连接被拒       ``NS_ERROR_CONNECTION_REFUSED``     ``io::ErrorKind::ConnectionRefused``
============  =================================  ==============================

⇒ **判据是语义，不是码**：同一语义在两个面各有一套码 ⇒ **跨面可比**
（这是「分支口径跨面必须一致」在机器码层面的落法）。

⚠️ **两个面都有「证明不了」的形态**——那是常态不是例外：

- 超时（``NS_ERROR_NET_TIMEOUT`` / ``kind: TimedOut``）：网络静默，
  **站点可能只是慢**；
- 打断（``is interrupted by another navigation``）：**重定向确实发生了**
  ⇒ 站点**是通的**；
- abort / 空响应（``NS_ERROR_ABORT`` / ``NS_ERROR_NET_EMPTY_RESPONSE``）：
  连接建起来了，服务器没把话说完；
- 建页失败 / 会话失效 / 快照失败：**坏的是我们这一侧**；
- obscura 的 SSRF 护栏拦截（``Access to private/internal IP address``）：
  **采集侧策略**，不是站点性质。

这些**一律**落「这次没打开」⇒ ``unresolved``（**不进池**）。

⚠️ **本模块只回答「是不是真·站点不可达」**，不回答「该记哪支」——
分支口径归 :mod:`trajectory_pipeline.executor.steps.reachability`
（它是 ``unreachable_hard`` 的唯一产地），机器码归驱动层。
"""

from __future__ import annotations

import re
from typing import Mapping, NamedTuple

__all__ = [
    "SiteUnreachable",
    "SITE_UNREACHABLE_CODES",
    "site_unreachable",
]


class SiteUnreachable(NamedTuple):
    """一次「真·站点不可达」的判定结果。

    :param category: **语义类别**（``dns_failure`` / ``connection_refused``）
        ——跨面统一，两面用同一个词。
    :param code: 命中的**机器码标签**——进 evidence，让人看得出
        「凭什么判成站点不可达」（而不是只看到一个结论）。
    """

    category: str
    code: str


#: 语义类别 → 机器码模式。**实测来源见模块文档**。
#:
#: ⚠️ **码表里只有「机器码」**——散文短语一律不收（本地化 + 版本漂移）。
#: ⚠️ **每个模式都标了标签**，标签进 evidence：人核时看到的是
#: 「机器码 = NS_ERROR_UNKNOWN_HOST」，而不是「模型说它打不开」。
SITE_UNREACHABLE_CODES: Mapping[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "dns_failure": (
        # Firefox 内核（camofox / camoufox）——实测：``page.goto: NS_ERROR_UNKNOWN_HOST``
        ("NS_ERROR_UNKNOWN_HOST", re.compile(r"\bNS_ERROR_UNKNOWN_HOST\b")),
        # Rust / reqwest 内核（obscura）——实测：
        # ``ConnectError("dns error", Os { code: 11001, kind: Uncategorized, … })``
        ('reqwest ConnectError("dns error")',
         re.compile(r'ConnectError\(\s*"dns error"')),
    ),
    "connection_refused": (
        # Firefox 内核——实测：``page.goto: NS_ERROR_CONNECTION_REFUSED``
        # （目标 ``http://www.baidu.com:81/``：公网主机 + 关闭端口 ⇒ 回 RST）
        ("NS_ERROR_CONNECTION_REFUSED",
         re.compile(r"\bNS_ERROR_CONNECTION_REFUSED\b")),
        # Rust / reqwest 内核——实测：
        # ``ConnectError("tcp connect error", 127.0.0.1:9, Os { code: 10061,
        # kind: ConnectionRefused, … })``
        ("io::ErrorKind::ConnectionRefused",
         re.compile(r"\bkind:\s*ConnectionRefused\b")),
    ),
}


def site_unreachable(text: str) -> SiteUnreachable | None:
    """这段错误原文里有没有「**真·站点不可达**」的机器码？

    没有就返回 ``None``——**不是**判成「不可达」，而是「**证明不了**」。
    判不出来的形态（超时 / 打断 / 建页失败 / 快照失败……）一律走
    「这次没打开」那一支（``unresolved``，**不进池**）。

    ⚠️ **入参必须是完整原文**：obscura 的驱动层把错误正文砍到 200 字，
    而 ``ConnectError("dns error"`` 恰好落在第 200 字**之后** ⇒
    拿截断后的原文来判，**DNS 失败会被误判成「证明不了」**。
    ⇒ 调用方必须在**截断之前**判（见 ``obscura_driver.ObscuraDriver._call``）。
    """
    if not text:
        return None
    for category, patterns in SITE_UNREACHABLE_CODES.items():
        for label, pattern in patterns:
            if pattern.search(text):
                return SiteUnreachable(category=category, code=label)
    return None
