"""L0 静态可达性探测（HTTP 层，无浏览器）。

2026-10-12 增量十八配套：阶段 4 L0 静态可达性探测落地。

设计纪律（与设计文档 §4.1 + §6 / 增量十八对齐）：

1. **不复用 :mod:`executor.steps.reachability`** —— 后者刻意不用
   HTTP 状态码。L0 是**新通道**，与 reachability 并列。

2. **只产 ``broken`` / ``blocked`` 初筛** —— 网络层异常
   （``timeout`` / ``tls_error`` / ``connection_error``）**不归**任何
   分支（设计文档明确：``unreachable_hard`` 由 reachability 的
   **机器码判定**触发，留给 reachability）。

3. **不依赖 LLM / 不依赖浏览器** —— 纯字符串事实判定，复用
   :func:`trajectory_pipeline.executor.dom.detect_block` 判定反爬特征。

4. **关键边界**：``broken`` 与 ``unreachable_hard`` 不是同一等价类——
   一个是「内容损坏」（页面/媒体在但坏），一个是「环境导航失败」
   （连页面都没到）。L0 的 HTTP 404/5xx 初筛只产 ``broken``，不产
   ``unreachable_hard``。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import httpx

from trajectory_pipeline.executor.archive import (
    CredentialLeak,
    credential_samples,
    scan_credentials,
)
from trajectory_pipeline.executor.dom import detect_block

#: 响应时间硬上限（秒）。设计文档 §4.1。
DEFAULT_TIMEOUT_S: float = 10.0

#: 单次探测 body 截断（字符）。detect_block 只看 body 限流词，多取无益；
#: 超过 4KB 几乎都是视频站首页脚注/广告。
MAX_BODY_CHARS: int = 4096

#: 重定向链阈值（> 此值视为 redirect_loop）。设计文档 §4.1。
REDIRECT_LOOP_THRESHOLD: int = 3

#: ``<title>`` 提取正则。**只取第一个**，不处理多 title / 注释包裹。
_TITLE_RE = re.compile(
    r"<title[^>]*>(.*?)</title>",
    re.IGNORECASE | re.DOTALL,
)

#: landing_status 的合法值集合。集中一处，便于 ``to_branch`` 与测试共同引用。
LandingStatus = Literal[
    "ok",                  # 干净
    "broken_signal",       # 404 / 410 / 5xx 命中
    "blocked_signal",      # 反爬 / 限流特征命中
    "redirect_loop",       # 重定向环
    "timeout",             # 响应超时
    "tls_error",           # TLS 证书异常
    "connection_error",    # DNS / 连接拒绝
    "unknown",             # 其他 HTTP 异常
]


@dataclass(frozen=True, slots=True)
class StaticProbeResult:
    """L0 静态可达性探测结果（探测层，**不下分支结论**）。

    字段映射设计文档 §4.1 ``landing_status`` / ``final_url`` /
    ``redirect_count`` 三件套；本结构另加探测细节以供回溯。
    """

    requested_url: str
    final_url: str
    landing_status: LandingStatus
    status_code: int | None
    redirect_count: int
    redirect_chain: tuple[str, ...]
    elapsed_ms: int
    block_reason: str                # detect_block 命中标记
    title: str                       # 从 body 提取的 <title>
    body_excerpt: str                # 前 MAX_BODY_CHARS 字符
    error: str = ""                  # 异常时的描述


def to_branch(result: StaticProbeResult) -> str | None:
    """探测结果 → 分支映射（仅 ``broken`` / ``blocked`` / ``None``）。

    关键边界（设计文档 §6 / 增量十八）：

    - **L0 HTTP 404/5xx 初筛只产 ``broken``，不产 ``unreachable_hard``**
      ——``unreachable_hard`` 由 reachability 的**机器码判定**触发
      （``DriverUnreachableError``：DNS 解析不出来 / 连接被拒）。
    - **网络层异常（``timeout`` / ``tls_error`` / ``connection_error``）不归任何分支**
      ——L0 报"网络异常"但不下分支，留给下一层。
    - **2xx/3xx + 反爬特征 → ``blocked``**（页面被重定向到反爬页 / 限流页）。
    - **200/3xx 干净 → ``None``**（passes to next layer）。

    返回 ``None`` 表示「L0 没结论」，调用方走下一层（L1 / L2）。
    """
    sc = result.status_code

    # 1. 重定向环：链接坏掉 → 内容损坏
    if result.landing_status == "redirect_loop":
        return "broken"

    # 2. 404 / 410 / 5xx：内容性损坏
    if sc is not None and (sc == 404 or sc == 410 or sc >= 500):
        return "broken"

    # 3. 401：身份验证 → 环境阻断
    if sc == 401:
        return "blocked"

    # 4. 403：兜底 anti_bot（反爬站常返回极简 403 页，可能不带反爬关键词）
    if sc == 403:
        return "blocked"

    # 5. 2xx / 3xx + 反爬 / 限流特征命中 → blocked
    if sc is not None and sc < 400 and result.block_reason:
        return "blocked"

    # 6. 网络层异常 + 其他：留给下一层
    return None


async def probe(
    url: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_body_chars: int = MAX_BODY_CHARS,
    client: httpx.AsyncClient | None = None,
) -> StaticProbeResult:
    """HTTP 层可达性探测。

    设计要点（设计文档 §4.1）：

    - **GET 单次**：HEAD 不带 body，detect_block 无法判定；改用 GET 但
      限制 body 截断到 :data:`MAX_BODY_CHARS` 字符。
    - **超时 :data:`DEFAULT_TIMEOUT_S` 秒**：硬上限。
    - **重定向 > :data:`REDIRECT_LOOP_THRESHOLD` 标记 ``redirect_loop``**。
    - **复用 :func:`detect_block`** 判定反爬 / 限流。

    异常分类：

    - :class:`httpx.TimeoutException` → ``landing_status="timeout"``
    - :class:`httpx.ConnectError`    → ``landing_status="connection_error"``
    - 其他 :class:`httpx.RequestError` → ``landing_status="unknown"``

    入参 ``client`` 可选；测试时塞 mock client。
    """
    started = time.perf_counter()
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout_s,
            headers={"User-Agent": "trajectory-staticprobe/1.0"},
        )
    try:
        try:
            response = await client.get(url)
        except httpx.TimeoutException:
            return _network_error_result(
                url, started, "timeout",
                f"timeout after {timeout_s}s",
            )
        except httpx.ConnectError as exc:
            return _network_error_result(
                url, started, "connection_error",
                f"{type(exc).__name__}: {exc}",
            )
        except httpx.RequestError as exc:
            return _network_error_result(
                url, started, "unknown",
                f"{type(exc).__name__}: {exc}",
            )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        # ``follow_redirects=True`` 时 ``response.url`` 即终态；
        # ``response.history`` 是中间跳转节点（不含最终响应）。
        redirect_chain: tuple[str, ...] = tuple(
            str(r.url) for r in response.history
        )
        final_url = str(response.url)
        body_excerpt = (response.text or "")[:max_body_chars]
        title = _extract_title(body_excerpt)
        block_reason = detect_block(final_url, title, body_excerpt)

        sc = response.status_code

        # landing_status 优先级：
        # redirect_loop > blocked_signal（含 401/403 硬码与反爬特征）
        #              > broken_signal（404/410/5xx）
        #              > ok（2xx/3xx 干净）
        #              > unknown（其他 4xx）
        #
        # 401/403 与 ``to_branch`` 严格对齐：403 就算无反爬关键词也算
        # blocked（兜底 anti_bot），于是探测层先把它们标 blocked_signal，
        # 不留给 ``unknown``。
        if len(response.history) > REDIRECT_LOOP_THRESHOLD:
            landing_status: LandingStatus = "redirect_loop"
        elif block_reason:
            landing_status = "blocked_signal"
        elif sc is not None and (sc == 401 or sc == 403):
            landing_status = "blocked_signal"
        elif sc is not None and (sc == 404 or sc == 410 or sc >= 500):
            landing_status = "broken_signal"
        elif 200 <= sc < 400:
            landing_status = "ok"
        else:
            # 4xx 其他（418/451 等）/ 异常码
            landing_status = "unknown"

        return StaticProbeResult(
            requested_url=url,
            final_url=final_url,
            landing_status=landing_status,
            status_code=sc,
            redirect_count=len(response.history),
            redirect_chain=redirect_chain,
            elapsed_ms=elapsed_ms,
            block_reason=block_reason,
            title=title,
            body_excerpt=body_excerpt,
        )
    finally:
        if own_client:
            await client.aclose()


def _network_error_result(
    requested_url: str,
    started: float,
    landing_status: LandingStatus,
    error: str,
) -> StaticProbeResult:
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return StaticProbeResult(
        requested_url=requested_url,
        final_url=requested_url,
        landing_status=landing_status,
        status_code=None,
        redirect_count=0,
        redirect_chain=(),
        elapsed_ms=elapsed_ms,
        block_reason="",
        title="",
        body_excerpt="",
        error=error,
    )


def _extract_title(body: str) -> str:
    """从 HTML body 提取 ``<title>`` 内容。取第一个匹配，剥空白。

    不处理：

    - ``<title>`` 在注释里
    - 多 ``<title>``（极少见，会取第一个）
    - HTML entity（detect_block 不依赖原始字符，所以**不**做反转义）

    失败返回空串——detect_block 的 title 检查会回退到 URL / 正文。
    """
    m = _TITLE_RE.search(body)
    if not m:
        return ""
    return m.group(1).strip()


def dump_result(result: StaticProbeResult, path: Path) -> Path:
    """落盘探测结果：JSON + 原子写 + 凭据扫描。

    复用 :func:`trajectory_pipeline.executor.archive.scan_credentials`
    走与 P1 存档**同一道**凭据闸门（CLAUDE.md R11）——L0
    ``body_excerpt`` 可能含 cookie / token，**不能绕过**。

    ``scan_credentials`` 只返回命中的 pattern 字符串，排错不便；
    本函数额外把匹配到的样本文本（前 60 字）打到
    :class:`CredentialLeak` 异常消息里——HTML 页面里偶尔会嵌
    ``Authorization:`` / ``Bearer xxx`` / 资源链接带 ``user:pass@``
    这类误报，保留 fail-closed，但要把噪音看清楚。
    """
    payload = json.dumps(asdict(result), ensure_ascii=False, indent=2)
    hits = scan_credentials(payload)
    if hits:
        samples = credential_samples(payload)
        raise CredentialLeak(
            f"L0 探测结果命中 {len(hits)} 项凭据特征，已拒绝写盘: "
            f"{hits[:3]}; samples={samples}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return path
