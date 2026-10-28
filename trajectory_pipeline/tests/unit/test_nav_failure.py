"""``nav_failure`` 的机器码判据——「真·站点不可达」怎么判。

为什么这些用例长这样
--------------------
码表**建在实测原文上**：

- ``research/probes/camofox_nav_failure_codes.json``——camofox 面，
  **生产驱动**（``CamofoxDriver.goto``）现场抓的；
- ``research/probes/obscura_nav_failure_codes.json``——obscura 面，
  **绕过驱动层 200 字截断**、直接调 MCP tool 抓的完整原文。

⇒ 本文件里的**每一段原文都是现场抓的**，不是照文档编的。
**改码表先改探针、再改这里**——拿一条没抓过的原文来「测通过」，
等于用想象当证据（这正是 M-44 这次要治的病）。

⚠️ **本文件守的是「fail-closed 方向」**：判不出来的形态**必须**返回
``None``（⇒ 落 ``unresolved``，**不进池**），而不是猜一个类别。
"""

from __future__ import annotations

from trajectory_pipeline.executor.browser.nav_failure import site_unreachable


# ── camofox 面（Firefox / Playwright）───────────────────────────
# 原文取自 research/probes/camofox_nav_failure_codes.json。

def test_camofox_DNS解析不出来是真站点不可达():
    text = (
        'camofox tool camofox_navigate 返回错误: camofox error: 500: '
        '{"error":"page.goto: NS_ERROR_UNKNOWN_HOST\\nCall log:\\n'
        '  - navigating to \\"http://this-domain-should-not-exist-zzq7x9.invalid/\\",'
        ' waiting until \\"commit\\"\\n","retryable":false}'
    )
    hit = site_unreachable(text)
    assert hit is not None
    assert hit.category == "dns_failure"
    assert hit.code == "NS_ERROR_UNKNOWN_HOST"


def test_camofox_连接被拒是真站点不可达():
    text = (
        'camofox tool camofox_navigate 返回错误: camofox error: 500: '
        '{"error":"page.goto: NS_ERROR_CONNECTION_REFUSED\\nCall log:\\n'
        '  - navigating to \\"http://www.baidu.com:81/\\", waiting until \\"commit\\"\\n",'
        '"retryable":false}'
    )
    hit = site_unreachable(text)
    assert hit is not None
    assert hit.category == "connection_refused"


def test_camofox_超时证明不了站点不可达():
    """超时**证明不了**站点不可达——站点可能只是慢。

    ⚠️ 这条是 M-44 的核心：这类形态原来落 ``unreachable_hard``
    （进池）⇒ 教模型「这个站看不了」，其实只是「这次没打开」。
    """
    assert site_unreachable(
        'camofox 导航失败: camofox tool camofox_navigate 返回错误: '
        'camofox error: 500: {"error":"page.goto: NS_ERROR_NET_TIMEOUT\\n'
        'Call log:\\n  - navigating to \\"http://10.255.255.1:81/\\", '
        'waiting until \\"commit\\"\\n","retryable":false}'
    ) is None


def test_camofox_导航预算耗尽的超时形态也不算():
    assert site_unreachable(
        'camofox 导航失败: camofox tool camofox_navigate 返回错误: '
        'camofox error: 500: {"error":"page.goto: Timeout 30000ms exceeded.\\n'
        'Call log:\\n  - navigating to \\"http://example.com:81/\\", '
        'waiting until \\"commit\\"\\n","retryable":false}'
    ) is None


def test_camofox_被另一个导航打断不算():
    """打断的形态**包含重定向确实发生了**的情形 ⇒ 站点是通的。"""
    assert site_unreachable(
        'camofox 导航失败: camofox tool camofox_navigate 返回错误: '
        'camofox error: 500: {"error":"page.goto: Navigation to '
        '\\"http://127.0.0.1:9/\\" is interrupted by another navigation to '
        '\\"http://127.0.0.1:9/\\"\\nCall log:\\n  - navigating to '
        '\\"http://127.0.0.1:9/\\", waiting until \\"commit\\"\\n",'
        '"retryable":false}'
    ) is None


def test_camofox_abort与空响应不算():
    assert site_unreachable("page.goto: NS_ERROR_ABORT") is None
    assert site_unreachable("page.goto: NS_ERROR_NET_EMPTY_RESPONSE") is None


# ── obscura 面（Rust / reqwest）───────────────────────────────
# 原文取自 research/probes/obscura_nav_failure_codes.json。

def test_obscura_DNS解析不出来是真站点不可达():
    text = (
        "Error: Network error: Network error: "
        "http://this-domain-should-not-exist-zzq7x9.invalid/: error sending "
        "request for uri (http://this-domain-should-not-exist-zzq7x9.invalid/): "
        "client error (Connect) (source: Some(Error { kind: Connect, source: "
        'Some(ConnectError("dns error", Os { code: 11001, kind: Uncategorized, '
        'message: "不知道这样的主机。" })), connect_info: None }))'
    )
    hit = site_unreachable(text)
    assert hit is not None
    assert hit.category == "dns_failure"


def test_obscura_连接被拒是真站点不可达():
    text = (
        "Error: Network error: Network error: http://127.0.0.1:9/: error "
        "sending request for uri (http://127.0.0.1:9/): client error (Connect) "
        "(source: Some(Error { kind: Connect, source: Some(ConnectError("
        '"tcp connect error", 127.0.0.1:9, Os { code: 10061, '
        "kind: ConnectionRefused, message: \"由于目标计算机积极拒绝，无法连接。\" "
        "})), connect_info: None }))"
    )
    hit = site_unreachable(text)
    assert hit is not None
    assert hit.category == "connection_refused"


def test_obscura_超时不算():
    text = (
        "Error: Network error: Network error: http://10.255.255.1:81/: error "
        "sending request for uri (http://10.255.255.1:81/): client error (Connect) "
        '(source: Some(Error { kind: Connect, source: Some(ConnectError('
        '"tcp connect error", 10.255.255.1:81, Os { code: 10060, '
        "kind: TimedOut, message: \"由于连接方在一段时间后没有正确答复…\" "
        "})), connect_info: None }))"
    )
    assert site_unreachable(text) is None


def test_obscura_SSRF护栏拦截不是站点性质():
    """obscura 有 SSRF 护栏：拦的是**我们这一侧**的策略，不是站点打不开。"""
    text = (
        "Error: Network error: Network error: Access to private/internal IP "
        "address 127.0.0.1 is not allowed\nLocal/private network access is "
        "blocked by the SSRF guard."
    )
    assert site_unreachable(text) is None


def test_obscura_截断到200字后DNS形态认不出来():
    """⚠️ **回归闸**：驱动层把错误正文砍到 200 字，而
    ``ConnectError("dns error"`` 恰好落在**第 200 字之后** ⇒
    拿截断后的原文判，**DNS 失败会被误判成「证明不了」**。

    ⇒ 调用方必须在**截断之前**判（``obscura_driver._call`` 就是这么做的）。
    这条测试钉住「为什么不能在截断之后判」。
    """
    full = (
        "Error: Network error: Network error: "
        "http://this-domain-should-not-exist-zzq7x9.invalid/: error sending "
        "request for uri (http://this-domain-should-not-exist-zzq7x9.invalid/): "
        "client error (Connect) (source: Some(Error { kind: Connect, source: "
        'Some(ConnectError("dns error", Os { code: 11001, kind: Uncategorized, '
        'message: "不知道这样的主机。" })), connect_info: None }))'
    )
    assert site_unreachable(full) is not None
    truncated = full[:200]
    assert 'dns error' not in truncated, "前提变了：机器码已在 200 字之内"
    assert site_unreachable(truncated) is None


# ── 通用：散文短语一律不算 ─────────────────────────────────────

def test_散文短语不算机器码():
    """「DNS」「超时」「连接失败」这些**散文**不收——本地化 + 版本漂移。"""
    for text in (
        "导航失败（网络/DNS/超时）",
        "DNS 解析失败",
        "connection failed",
        "操作超时",
    ):
        assert site_unreachable(text) is None


def test_空串返回None():
    assert site_unreachable("") is None
