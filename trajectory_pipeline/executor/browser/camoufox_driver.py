"""camoufox 驱动的 :class:`PageDriver` 实现——P1 骨架 + P3 采集面 + P2 动作面。

**协议里的原语已全部落地**：``goto`` / ``snapshot`` / ``count`` /
``evaluate`` / ``close``（P1 骨架）· ``interactive``（P3-α）· ``links`` /
``extract`` / ``new_tab`` / ``close_tab`` / ``observe``（P3-β）· ``click`` /
``fill`` / ``press``（P2 动作面）。

骨架期那五个原语一度**显式抛 ``NotImplementedError``**——那不是拖延，
是有意的：把它们做成空列表/空正文，会与「页面上确实没有」完全同形，
而那正是本项目最贵的一类假负样本。P3-β 落地后仍保留同一取向：
**能确定区分的降级一律记进 ``degraded`` / 账目，不折叠成"空"**。

## 动作面的口径（P2，2026-10-20）

**目标编码是 ``role + name``，定位一律 ``get_by_role(role, name=name, exact=True)``。**
两条都不是风格选择：

- ``role+name`` 是 D12 定的**可移植**编码。``ref``（``e1``）是会话内句柄，
  进训练数据等于让模型学随机数；``{tag,label}`` 在半数元素上根本表达不出来
  （``<input>`` 没有文本内容，``:has-text`` 永不命中）。
- ``exact=True`` 是**判据的前提**：默认的 name 匹配是 substring + 忽略大小写，
  实测让命中率变成 100%（P-1 的判据因此变空），且 ``网页`` 能命中 ``\\ue608网页``。

**三个动作都走「先 ``count()`` 再操作」**（方案 04 §4.2）：playwright 里
「元素不存在」与「页面卡住」**都是 ``TimeoutError``**，一个该转 ``unresolved``
（业务事实），一个该转 :class:`DriverError`（能力不可用）。先数一遍是**确定性**
的区分，不靠解析异常消息。因此：

| 情形 | 动作 | 返回 |
|---|---|---|
| ``count == 0`` | 不操作 | ``None``（业务事实，静默） |
| ``count > 1``（真同名） | **不操作** | ``None``（选哪个都要赌，点击不可逆） |
| ``count`` 本身报错 | — | 抛 ``DriverError`` |
| 操作抛错（元素在页上） | — | 抛 ``DriverError`` |

动作返回 ``None``，于是「点成功了」与「元素不在」在调用方同形——**区分靠
:attr:`CamoufoxDriver.last_action`**（``op`` / ``role`` / ``name`` / ``outcome``），
与 :attr:`last_enumeration` 同一条纪律：过滤/失败必须能自证。

### ★ 调用方必须遵守的一条（2026-10-20 冒烟实测，不是推测）

**``role+name`` 是页面级定位：导航之后它不失效，而是可能静默点中新页的同名元素**
（``ref`` 是会话句柄，导航后必然报错——这道保护随可移植性一起被换掉了）。
所以：**任何一次导航之后必须先重新 ``interactive()``，才允许下一次动作。**

配套的第二条：**``click()`` 返回时不保证导航已经开始**。动作之后立刻读可能读到
**整个旧页**，而旧页在导航开始前**天然稳定**（「连续两次读数一致算读稳」这种判据
在这里恒真）。判据只能是**「URL 相对动作前变了没有」**，见 ``cli._after_action_snapshot``。
两条合起来是同一个坑的两个入口：**一个在名字过期，一个在读得太早**。
症状侧见 02-避坑指南 §8.8。

## ``interactive`` 的口径（P3-α，2026-10-20）

**一页一次 ``page.aria_snapshot()``，role+name 全取 playwright 的结论，采集层不重算。**

这条路不是选出来的，是量出来的（本机实测，200 个按钮/链接的合成页）：

| 做法 | 成本 | 结论 |
|---|---|---|
| 每元素 ``locator.aria_snapshot()`` | 3.3ms × N | 慢，且要 N 次往返 |
| 每元素 ``is_visible()`` | 5.8ms × N | 最贵的一项 |
| 每元素 ``evaluate(tagName)`` | 3.6ms × N | 只为拿 HTML 标签 |
| **一页一次 ``page.aria_snapshot()``** | **51ms / 页** | ✅ 采用 |

代价与取舍，逐条写清楚（**都不是漏做**）：

- **``tag`` 字段装的是 ARIA role**（``button`` / ``textbox``…），不是 HTML 标签名。
  这是 D12 的目标方向（``{tag,label}`` → ``{role,name}``）；要 HTML 标签得每元素
  一次 ``evaluate``，为 3.6ms×N 换一个下游只有两处用到的字段，不值。P4 契约迁移时
  字段名一并改掉。**下游两个消费点已按「标签名 ∪ role」放宽**（见
  ``steps/site_search._SEARCHABLE_TAGS`` 与 ``rule_perceptor.SEARCH_BOX_TAGS``）。
- **没有可访问名的节点直接不进列表**。它们**定位不到**（``get_by_role(role, name="")``
  命中 0 个且不报错），列出来等于给模型一个假的可点目标。实测这类占比很高
  （F5 真实页面段：41% 的枚举元素没有可访问名）。
- **真同名的节点整组丢弃**（``(role, name)`` 出现多次）。它们同样定位不到——
  playwright 的 strict mode 会报错。**丢而不是留，是因为「留」等于赌哪一个**，
  而 P2 的路径 3（代码按文本挑一个）还没落地。丢弃数量记在
  :attr:`CamoufoxDriver.last_enumeration` 里，**看得见**。
- **可见性没有逐元素过滤**（那要 5.8ms×N）。``aria_snapshot`` 本身就不含
  ``display:none`` / ``aria-hidden`` / ``visibility:hidden``；残余的不可见元素
  **若无名字会被上一条过滤掉**，有名字的仍可定位（不是坏目标，只是噪声）。
  这条与 F5 的「枚举先过可见性」的差异是**成本换来的**，量级写在上面。

``ref`` 是 ``e1..eN``，**会话内的位置编号**：``goto`` 会清空
:attr:`_refs`（与 obscura「ref 导航前有效」同义）。ref→(role,name) 的映射只活在
本驱动内部，供判断点 ③ 回传用；**动作面走 role+name**（D12）。

## 标签页（P3-β）：页面是**栈**，不是一个 ``page``

外链校验（``steps/link_check``）的固定形态是「开 tab → 观察 → 关 tab →
**回到中间页继续采**」。一个 ``_page`` 字段关完就无处可回，所以
:class:`CamoufoxSession` 持有页面**栈**：

- ``new_tab(url)`` 开页、压栈、切成当前页，返回**本驱动自己发的**句柄
  （``c1`` / ``c2``…）——**不是** obscura 那种服务端 tab id，两面 id 不可互换；
- ``close_tab()`` 关当前页并自动回到上一页；``close_tab(id)`` 认不出这个 id
  就**抛**，不猜一个关掉——关错页的后果是后续 ``observe`` 读到别的页，**不报错**；
- **只剩一页时先补一张空白页再关**：``page`` 必须永远可用。否则关掉唯一页之后
  每条 ``observe`` 都在读一个已关闭的句柄，症状是「页面读不到」，
  而真相是「我们自己把它关空了」；
- 开页时导航失败**把这一页收掉再抛**：留着它，当前页就停在一张 ``about:blank``
  上，一次外链失败会连带把中间页的证据一起弄丢。

## ``links`` / ``extract`` 的口径（P3-β）

**``links`` 取 ``a.href``（浏览器解析后的绝对地址），并丢掉空 ``href`` 的锚。**
空锚不能留：``<a href="">`` 的 ``a.href`` 会被解析成**当前页地址**，
留一堆「指向自己的链接」会污染候选集合，而它们在页面上确实存在——
这正是「数据看着有、其实全是噪声」的形态。

**``extract`` 一律「抽不到就不写这个键」，不编造空串。** 写成 ``""`` 会让
「抽不到」与「页面上是空的」同形，而 ``steps/search.result_titles`` 的判断恰恰是
「两个数组等长且非空」：给它一个填满空串的数组，它会把 8 条假配对当成 8 条好
结果交出去，判断点 ① 于是拿着它做**唯一判据**。

与 :class:`~trajectory_pipeline.executor.browser.obscura_driver.ObscuraDriver`
的口径差异（都是**有意的**，不是漏改）：

| 项 | obscura | camoufox |
|---|---|---|
| URL / Title | 从 ``browser_snapshot`` 的 ``URL:`` / ``Title:`` 前缀解析 | ``page.url`` / ``page.title()``——直读，不再解析 |
| ``evaluate`` 返回 | 文本 → ``json.loads`` → 失败则原文 | playwright 直接给 Python 值，**没有 JSON 解包这一步** |
| 错误三态 | ``isError`` / 正文 ``Error:`` 前缀 | playwright 异常；「元素不存在」与「页面卡住」都是 ``TimeoutError``，故 P2 起一律**先 ``count()`` 再操作**（方案 §4.2） |
| ``count`` 失败 | 返回 0（``on_error="zero"``） | 同口径返回 0，见 :meth:`CamoufoxDriver.count` 的警告 |
| ``interactive`` 的 ``tag`` | HTML 标签名（``input``/``button``） | **ARIA role**（``textbox``/``button``） |
| ``interactive`` 的过滤 | 服务端给什么拿什么 | 词表外的 role、无名字、真同名——**三类都丢**，数量见 ``last_enumeration`` |
| ``links`` / ``interactive`` / ``extract`` 失败 | 吞成空（``on_error="empty"``） | **抛** ``DriverError``；``observe`` 再把它转成 ``degraded`` + 空列表——**降级必须留痕** |
| ``degraded`` 里的名字 | **tool 名**（``browser_links``…） | **能力名**（``links`` / ``interactive`` / ``count:video,audio``）。抄 tool 名等于在 camoufox 里写谎话，下游只判非空 |
| ``close_tab`` 失败 | 静默（``on_error="empty"``） | **抛** ``DriverError``——关不掉却当成关掉了，下一步会读错页且不报错 |

``evaluate`` 跑在 **isolated world**（``main_world_eval=False``，见
:mod:`camoufox_session`）：DOM 共享，但读不到页面自己 ``window`` 上的全局变量。
``"mw:"`` 前缀会被 playwright 直接拒绝——那正是开着的开关的语义，**不包装它**。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Mapping

from trajectory_pipeline.executor.browser.aria_snapshot import parse_nodes
from trajectory_pipeline.executor.browser.element_basis import (
    ROLE_VOCAB,
    SHARED_DOM_JS,
    filter_ladder,
    parse_shared_basis,
)
from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.executor.dom import (
    IFRAME_SELECTOR,
    INNER_TEXT_EXPR,
    MEDIA_SELECTOR,
    TEXT_CONTENT_EXPR,
    TRUNCATION_RATIO,
    CleanedLink,
    CleanedSnapshot,
    clean_body,
    is_mojibake,
    parse_body_text,
)
from trajectory_pipeline.common.elements import (
    ELEMENT_VOCAB_ARIA_ROLE,
)
from trajectory_pipeline.perception.base import (
    InteractiveElement,
    LinkItem,
    Observation,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，运行期不导入
    # ⚠️ **刻意不在运行期导入** ``camoufox_session``（2026-10-21 改）：
    # 它只在 :meth:`CamoufoxDriver.__init__` 的**类型标注**里出现，
    # 而它会 ``import camoufox``——那是一条**把浏览器拉起**的依赖链。
    # 于是「import 驱动模块」与「能不能起浏览器」被绑在一起：驱动层的
    # 单测（本文件）本不该被浏览器栈的可安装性绑架。
    #
    # 与 ``probe_observe.py`` / ``probe_result_titles.py`` 同一手法——那里
    # 早就是**函数内导入**了（要真会话才导），这里只是把它提到类型层。
    from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession

__all__ = ["CamoufoxDriver", "ROLE_VOCAB"]

#: ``ROLE_VOCAB`` **住在** :mod:`trajectory_pipeline.executor.browser.element_basis`
#: （2026-10-21 搬走）：它现在是**两把尺子共用的词表**——
#: 本面的 ARIA 阶梯 + 跨面共享口径的阶梯。留在驱动里会让共享口径抄第二份，
#: 抄两份必然漂，而漂了之后「−70% 落在哪一档」又变成不可解释的量。
#: 这里只做**再导出**，让旧引用（``from ...camoufox_driver import ROLE_VOCAB``）
#: 不用改。

#: 元素列表的默认上限。与 obscura 的 ``DEFAULT_LIMIT`` 取同一个数，
#: 但**两个实现下的语义不同**（obscura 由服务端截断，这里由我们截断），
#: ``elements_total`` 的跨实现同义性仍未验证（方案 §4.1 末段）。
DEFAULT_LIMIT = 200

#: 单个动作的超时（毫秒）。**不是 playwright 的默认 30s**：
#: 目标元素已经被 count 确认存在，还给 30s 只会让「页面卡住」把整批拖死。
#: 预算里包含 humanize 的鼠标移动/停顿（最长 1.5s/次）。
#:
#: ⚠️ 这个数**只覆盖动作本身**，不覆盖「等它触发的那次导航」——后者用
#: ``no_wait_after=True`` 关掉了（实测 baidu 上默认等待要 9–13s，见 :meth:`_settle`）。
#: 把两件事混在一个超时里，会让点击超时，而失败原因看起来像页面故障。
#:
#: **2026-10-21：5s → 15s**。理由是实测上界——窗口不可渲染时（最小化 /
#: 息屏 / 锁屏 / 完全遮挡）单次 ``locator.click`` 要 **5–13s**
#: （``research/probes/click_occluded.json`` 三组：13323 / 9103 / 9063ms，
#: 且**事件照样落地**）。5s 预算下这三组必然超时，于是「点击没做成」
#: 被下游读成 ``component_unverified``——理由通顺的假负样本。
#: 15s 覆盖 13.3s 上界；**无头口径下实测 34–71ms，永远碰不到这个上限**，
#: 所以放宽只影响「人守着的桌面」那一条路。
#:
#: ⚠️ **超时后绝不重试**：那一次点击的事件**可能已经落地**（探针实测
#: ``pointerdown`` 计数上涨），重试 = 点两次。上层同样不重跑
#: （``orchestrator``：单次 tool 失败不重跑）——这条纪律由
#: ``tests/unit/test_camoufox_driver.py`` 钉住。
ACTION_TIMEOUT_MS = 15000

#: 动作之后等新文档 **DOM ready** 的上限（毫秒）。**有界**是重点：
#: 这里等的是「新页面能不能被观察」，不是「页面加载完没完」——
#: 后者由页面自己决定，等它等于把预算交给站点。
NAV_SETTLE_TIMEOUT_MS = 2000

#: playwright 异常的公共基类名不稳定，直接按「除 KeyError/TypeError 外的
#: 浏览器异常」捕获会更糟——所以显式列 playwright 的那两个基类。
try:  # pragma: no cover - import 结构，不是行为
    from playwright.async_api import Error as _PwError
except ImportError:  # pragma: no cover
    class _PwError(Exception):        # type: ignore[no-redef]
        """playwright 不可用时的占位——异常类型只用于捕获，不参与逻辑。"""


def _brief(exc: BaseException, *, limit: int = 400) -> str:
    """把 playwright 的异常压成**可读的一行**：首行 + 最后三行。

    为什么不是 ``str(exc)[:200]``：playwright 把真正的原因摆在 call log 的**末尾**
    （``- element is not stable`` / ``- performing click action``），
    按头截断恰好把它切掉——归档里只剩「Timeout 5000ms exceeded」，
    于是「页面卡住」与「元素动个不停」在证据上同形。2026-10-20 冒烟排查
    就卡在这个截断上：三次失败看日志都长一个样，实际卡在不同的步骤。
    """
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    if not lines:
        return type(exc).__name__
    picked = lines if len(lines) <= 4 else [lines[0], *lines[-3:]]
    return " | ".join(picked)[:limit]


class CamoufoxDriver:
    """camoufox 的 :class:`PageDriver` 实现（P1 骨架 + P3-α 采集面）。"""

    name = "camoufox"

    def __init__(self, session: CamoufoxSession, *,
                 collect_element_basis: bool = False) -> None:
        self._session = session
        #: **跨面可比的元素账目要不要采**（设计方案 04 §4.9.6）。
        #:
        #: **默认关**：它多一次 ``page.evaluate`` 往返，是**测量仪器**
        #: 不是生产链的必需项。开着的时候 :meth:`observe` 会把两本账
        #: （``shared`` 共享口径 + ``face`` 本面 ARIA 阶梯）落进
        #: :attr:`Observation.element_basis`。
        self._collect_element_basis = collect_element_basis
        #: ref → (role, name)。**只在采集面内部活着**：判断点 ③ 回传 ref，
        #: 代码按它取回目标；动作面另走 role+name（D12：ref 不进训练数据）。
        self._refs: dict[str, tuple[str, str]] = {}
        #: 上一次 :meth:`interactive` 的过滤账目。**空列表必须能自证**——
        #: 「页面上真没有元素」与「全被过滤了」在观察里同形，只有这里分得开。
        self.last_enumeration: dict[str, int] = {}
        #: 上一次动作的结果账目（``op`` / ``role`` / ``name`` / ``outcome``）。
        #: 动作返回 ``None``，成功与「元素不存在」于是同形——账目是唯一的区分，
        #: 也是 P5 双跑时逐条核对「动作到底发生了没有」的依据。
        self.last_action: dict[str, str] = {}
        #: 本驱动发出去的 tab 句柄 → page。**只在本会话内有意义**——
        #: 与 obscura 的服务端 tab id 不是一回事，两面不可互换。
        self._tabs: dict[str, Any] = {}
        self._tab_seq = 0
        #: 上一次 ``new_tab`` / ``close_tab`` 的账目（``op`` / ``tab`` /
        #: ``pages`` / ``outcome``）。标签页动作**没有返回值可判**，
        #: 「发生了没有」只能靠这里看。
        self.last_tab: dict[str, Any] = {}

    @property
    def _page(self) -> Any:
        return self._session.page

    @property
    def fingerprint(self) -> dict[str, Any]:
        """本 Run 的指纹读数（批次元数据用，见 :class:`CamoufoxSession`）。"""
        return self._session.fingerprint

    @property
    def refs(self) -> Mapping[str, tuple[str, str]]:
        """``ref`` → ``(role, name)`` 的只读快照（供上层定位，**不进动作流**）。"""
        return dict(self._refs)

    # ── 原子原语 ──────────────────────────────────────────────────

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        """导航。失败抛 :class:`DriverError`——调用方须 fail-closed。

        **导航即清空 ref 表**：与 obscura「ref 导航前有效」同义。不清的话，
        上一个页面残留的 ``e3`` 会指向本页一个合法但**不同**的元素——
        点错不可逆，而且看起来完全正常。
        """
        self._refs = {}
        try:
            await self._page.goto(url, wait_until=wait_until)
        except _PwError as exc:
            # 异常消息只取前 200 字符照原样带出：它是「网络不通 / DNS / TLS」
            # 的唯一线索。**不按消息文本分类**——playwright 的措辞随版本变，
            # 靠文本分类等于把版本升级变成静默行为变化。
            raise DriverError(f"camoufox 导航失败: {_brief(exc)}") from None

    async def snapshot(
        self, *, max_chars: int | None = None, degraded: list[str] | None = None
    ) -> CleanedSnapshot:
        """取快照：URL / Title 直读，正文按 **innerText → textContent → HTML** 三级取。

        三级不是冗余，是 obscura 侧实测出来的同一张降级表（见
        :mod:`trajectory_pipeline.executor.dom`）：

        1. ``innerText``  —— 渲染后的可见文本，是唯一可信的正文来源；
        2. ``textContent`` —— innerText 因 layout 计算失败返回空时的救济
           （JS-heavy 站点偶发），仍要先剥 ``script/style``；
        3. HTML          —— 前两者都空时经 :func:`clean_body` 剥标签兜底，
           ``body_source`` 会标成 ``"snapshot"`` 以示证据更弱。

        ``body_text`` 只在**三路都取不到正文**时记进 ``degraded``——降级标记
        记的是「没采到」，不是「走了哪条路」。乱码另记 ``body_mojibake`` /
        ``title_mojibake``：那是「采到了但不可读」，对下游更危险（看起来有内容）。
        """
        try:
            url = self._page.url
            title = await self._page.title()
        except _PwError as exc:
            # snapshot 是 fail-closed 的锚点：没有观察就没有判定（I4），
            # 故这一项**只能抛**，不能降级成空快照。
            raise DriverError(f"camoufox 取快照失败: {_brief(exc)}") from None

        body: str | None = None
        body_source = "inner_text"
        raw_len = 0
        stripped_ratio = 1.0

        raw = await self._eval_text(INNER_TEXT_EXPR)
        if not raw:
            raw = await self._eval_text(TEXT_CONTENT_EXPR)
        if raw:
            raw_len = len(raw)
            body = parse_body_text(raw, max_chars=max_chars)
        # ⚠️ 这里判的是 ``body`` 而不是 ``raw``：取到了**空白**文本
        # （渲染没完成时的 ``"\\n\\n"``）规范化后也是空，继续走 HTML 兜底。
        # 判 ``raw`` 会在这种页面上**既不兜底也不记降级**——正文空着进存档，
        # 读的人会当成「这页没内容」。
        if not body:
            html = await self._content()
            if html:
                cleaned = clean_body(html)
                raw_len = len(html)
                stripped_ratio = (len(cleaned) / raw_len) if raw_len else 1.0
                body_source = "snapshot"
                body = (cleaned[:max_chars]
                        if max_chars is not None and len(cleaned) > max_chars
                        else cleaned)
            if not body and degraded is not None:
                degraded.append("body_text")
        if (body and degraded is not None and body_source == "inner_text"
                and is_mojibake(body)):
            degraded.append("body_mojibake")
        if degraded is not None and title and is_mojibake(title):
            degraded.append("title_mojibake")

        cleaned_body = body or ""
        if body_source == "snapshot":
            truncated = (max_chars is not None
                         and raw_len >= max_chars * TRUNCATION_RATIO)
        else:
            truncated = max_chars is not None and len(cleaned_body) >= max_chars
        return CleanedSnapshot(
            url=url,
            title=title or "",
            body=cleaned_body,
            raw_len=raw_len,
            stripped_ratio=stripped_ratio,
            truncated=truncated,
            max_chars=max_chars,
            body_source=body_source,
        )

    async def interactive(
        self, *, limit: int = DEFAULT_LIMIT
    ) -> tuple[InteractiveElement, ...]:
        """枚举可交互元素：**一页一次** ``page.aria_snapshot()``。

        role + name 全部取 playwright 的结论，采集层不重算——F5 实测过自己
        拼的那条可访问名链在真实页面上只有 87.9% 一致，而**对不上的那些恰好
        定位不到**。命名口径的唯一真源是 playwright 的 ``aria_snapshot``。

        过滤顺序（每一步的丢弃数都进 :attr:`last_enumeration`）：
        role 词表 → 无名 → 真同名整组 → 截断到 ``limit``。ref 按**留下的顺序**
        编 ``e1..eN``，映射写进 :attr:`_refs`（判断点 ③ 靠它回查目标）。

        ⚠️ **失败抛** :class:`DriverError`。obscura 同位置是
        ``on_error="empty"``（吞成空列表），这里**有意不一致**：空列表意味着
        「页面上没有可点目标」，会把一次**能力故障**写成**页面事实**，
        而这两者的正确处置相反（一个该重试/换路，一个该判负）。
        """
        try:
            text = await self._page.aria_snapshot()
        except _PwError as exc:
            raise DriverError(f"camoufox 取 aria 快照失败: {_brief(exc)}") from None

        nodes = parse_nodes(text or "")
        # 过滤阶梯**与跨面共享口径共用一把尺子**
        # （:func:`element_basis.filter_ladder`）：role 词表 → 无名 →
        # 真同名整组 → 截断。同名整组丢而不是留第一个——留哪个是赌
        # （属「猜测」），而 ``get_by_role(role, name, exact=True)`` 对同名
        # 会触发 strict mode 报错，留下等于给动作面埋一颗必然失败的雷。
        kept_pairs, ladder = filter_ladder(
            ((node.role, node.name or "") for node in nodes),
            role_vocab=ROLE_VOCAB,
            limit=limit,
        )

        refs: dict[str, tuple[str, str]] = {}
        elements: list[InteractiveElement] = []
        for index, (role, name) in enumerate(kept_pairs, start=1):
            ref = f"e{index}"
            refs[ref] = (role, name)
            elements.append(InteractiveElement(ref=ref, role=role, name=name))

        self._refs = refs
        self.last_enumeration = {"nodes": len(nodes), **ladder}
        return tuple(elements)

    async def element_basis(self) -> dict[str, int]:
        """**跨面可比的**元素枚举账目（设计方案 04 §4.9.6）。

        跑的是 :data:`element_basis.SHARED_DOM_JS`——**与 obscura 面同一段
        JS、同一把尺子**，于是两面的数字**按构造可比**。

        与 :meth:`interactive` 的差别是**枚举基础**：
        这里是 **DOM 交互候选**（role/name 自己算），
        那里是 **playwright 的可访问性树**（role/name 引擎算）。
        两者**故意不同**——共享口径要回答的是「同一个页面在两个浏览器上
        差多少」，用同一段 JS 才量得出来。

        ⚠️ **失败抛** :class:`DriverError`：空账会被读成
        「这个页面上一个交互元素都没有」，而真相是「没测到」。
        """
        try:
            raw = await self._page.evaluate(SHARED_DOM_JS)
        except _PwError as exc:
            raise DriverError(f"camoufox 取共享口径失败: {_brief(exc)}") from None
        try:
            _, ledger = parse_shared_basis(raw)
        except ValueError as exc:
            raise DriverError(f"camoufox 共享口径返回不合法: {exc}") from None
        return ledger

    async def _collect_basis(
        self, degraded: list[str], face_ledger: Mapping[str, int]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """采两本元素账目。返回 ``(两本账, 没测到的原因)``。

        **没开开关就返回 ``(None, None)``**——``None`` = 「没测」，
        空账 = 「测了，什么都没有」。这两件事必须能分开。

        ⚠️ **测量失败不进 ``degraded``**（2026-10-21 修，与 obscura 面同口径）：
        ``degraded`` 是**内容块**的降级通道，下游只判它非空——
        ``extract_candidates`` 见到降级就**不取候选**，于是「尺子坏了」
        会**作废整批数据**。失败只记进 ``element_basis_error``，
        **face 账目照旧给**（它不依赖共享口径）。
        """
        if not self._collect_element_basis:
            return None, None
        try:
            shared = await self.element_basis()
        except DriverError as exc:
            return {"face": dict(face_ledger)}, str(exc)
        return {"shared": shared, "face": dict(face_ledger)}, None

    async def count(self, selector: str) -> int:
        """选择器命中数。

        ⚠️ **失败返回 0**，与 obscura 的 ``on_error="zero"`` 同口径——
        这条口径本身是有缺陷的（「能力不可用」被折叠成「页面上没有」），
        但 P1 的目标是**行为对齐**，换口径会让两个实现不可比。重标定在 P2
        （方案 §4.1 末段已把 ``elements_total`` 的跨实现同义性标为未验证）。
        """
        try:
            return int(await self._page.locator(selector).count())
        except _PwError:
            return 0

    async def evaluate(self, expression: str) -> Any:
        """执行表达式，**原样返回 playwright 给的值**。

        没有 obscura 那层 JSON 解包：playwright 直接把 JS 值序列化成 Python
        对象（``str`` / ``int`` / ``list`` / ``dict`` / ``None``）。反过来，
        表达式返回 ``undefined`` 时拿到 ``None``，与「取不到」同形——需要区分
        时由调用方自己包一层哨兵值。

        失败**抛** :class:`DriverError`（obscura 同位置是吞成 ``None``，
        有意不一致）：吞掉之后调用方无法把「表达式跑挂了」与「值是 null」
        分开，而这两者的正确处置相反——前者是能力故障，后者是业务事实。
        """
        try:
            return await self._page.evaluate(expression)
        except _PwError as exc:
            raise DriverError(f"camoufox 表达式失败: {_brief(exc)}") from None

    async def close(self) -> None:
        """关闭整次 launch（一个 Run 一份会话）。幂等。"""
        await self._session.close()

    # ── 动作面（D12：role+name 定位）──────────────────────────────
    #
    # 三个动作全部走同一条确定性路径：**先 count 再操作**（方案 04 §4.2）。
    # 为什么非要先数一遍：playwright 里「元素不存在」与「页面卡住」**都是
    # TimeoutError**——一个该转 ``unresolved``（业务事实），一个该转
    # :class:`DriverError`（能力不可用）。数一遍就能确定性地分开，
    # 不用去解析异常消息（那玩意儿随版本变）。代价是每次多一次往返。

    async def click(
        self,
        *,
        ref: str | None = None,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> None:
        """点一个可交互元素：``get_by_role(role, name=name, exact=True).click()``。

        ``exact=True`` **不是可选项**：默认的 name 匹配是 substring + 忽略大小写，
        实测那会让命中率变成 100%（判据变空），并且用 ``网页`` 能命中
        ``\\ue608网页``（图标字体字形）。钉死 exact 之后，名字对不上就是找不到。
        """
        locator = await self._locate("click", role=role, name=name,
                                     ref=ref, selector=selector)
        if locator is None:
            return
        await self._do(
            "click",
            lambda: locator.click(timeout=ACTION_TIMEOUT_MS, no_wait_after=True),
            role=role, name=name,
        )
        await self._settle()

    async def fill(
        self,
        *,
        text: str,
        ref: str | None = None,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> None:
        """往输入框**设值**（先清空再写）：``get_by_role(...).fill(text)``。

        与动作流的 ``fill`` 同名同义——``fill`` 就是「设值」，
        不再有 ``type_text`` 那层「看起来像追加」的错名（协议注释有账）。
        """
        locator = await self._locate("fill", role=role, name=name,
                                     ref=ref, selector=selector)
        if locator is None:
            return
        await self._do(
            "fill", lambda: locator.fill(text, timeout=ACTION_TIMEOUT_MS),
            role=role, name=name,
        )

    async def press(
        self, key: str, *, role: str | None = None, name: str | None = None
    ) -> None:
        """按键。**不带目标时是页面级按键**（``page.keyboard.press``）。

        带 ``role``/``name`` 时在元素上按（``locator.press``）——搜索框回车提交
        走的就是这条：焦点不必在元素上，playwright 会先聚焦再按。
        """
        if role is None and name is None:
            await self._do("press", lambda: self._page.keyboard.press(key))
            await self._settle()
            return
        locator = await self._locate("press", role=role, name=name)
        if locator is None:
            return
        await self._do(
            "press",
            lambda: locator.press(key, timeout=ACTION_TIMEOUT_MS, no_wait_after=True),
            role=role, name=name,
        )
        await self._settle()

    async def resolve(
        self, *, role: str | None, name: str | None
    ) -> dict[str, Any]:
        """**只定位，不动作**——给判据⑥（动作可移植性）的重放探针用。

        为什么需要它：可移植性问的是「**换个会话重放，还定位得到吗**」，
        判据是**定位读数**（0 个 / 恰好 1 个 / >1 个），**不是**「点了会怎样」。
        而点击**不可逆**（可能点进另一个视频、可能触发付费流程）——
        探针要的是读数，不该拿真页面去试点击。

        判定走的是 :meth:`_locate` **同一条路径**（同一把尺子：
        ``get_by_role(role, name=name, exact=True)``）——**不另写一份**，
        否则探针与生产链的定位口径会漂，而漂了之后「重放定位率」这个读数
        就不再是生产链的读数（判据⑥ 就判错了对象）。

        返回 ``{"outcome", "matches", "role", "name"}``：
        ``outcome`` 取 :meth:`_locate` 的三态
        ——``ok``（恰好 1 个）/ ``not_found``（0 个，或词表外 role，
        或空 name）/ ``ambiguous``（>1 个真同名）。
        ``matches`` 是页面上的匹配数（**整数**；找不到时是 0）。

        ⚠️ **失败抛** :class:`DriverError`——定位能力不可用（页面崩、
        浏览器坏）**不是**「元素不存在」，两者不能折叠。
        """
        locator = await self._locate("resolve", role=role, name=name)
        if locator is None:
            rec = dict(self.last_action)
            outcome = str(rec.get("outcome") or "not_found")
            try:
                matches = int(str(rec.get("matches") or "0"))
            except ValueError:
                matches = 0
            return {
                "outcome": outcome,
                "matches": matches,
                "role": role or "",
                "name": name or "",
            }
        # 命中 1 个：``_locate`` 这条路径上没记 ``last_action``（那是 ``_do`` 的活），
        # 这里自己记一份——账目不许出现「成功与不存在同形」的空档。
        self.last_action = {
            "op": "resolve", "role": role or "", "name": name or "",
            "outcome": "ok", "matches": "1",
        }
        return {"outcome": "ok", "matches": 1, "role": role or "", "name": name or ""}

    async def _locate(
        self,
        op: str,
        *,
        role: str | None,
        name: str | None,
        ref: str | None = None,
        selector: str | None = None,
    ) -> Any | None:
        """按 ``role+name`` 取定位器；**取不到返回 ``None``（业务事实），不抛**。

        三态，全部记进 :attr:`last_action` 的 ``outcome``：

        | outcome | 触发 | 处置 |
        |---|---|---|
        | ``ok`` | 恰好 1 个 | 执行动作 |
        | ``not_found`` | 0 个 / 词表外 role / 空 name | 静默返回——元素不在页面上 |
        | ``ambiguous`` | >1 个（真同名） | **不动作**，见下 |

        **``ambiguous`` 为什么不动手**：真同名是页面事实（F5-j 实测 6.3%），
        选哪一个都要赌——而点击**不可逆**（可能点进另一个视频）。方案 04 §4.5.3
        的「路径 3 由代码按文本挑一个」还没落地，落地前这里就是 fail-closed：
        不点、记账、让上层看得到。

        **失败一律抛** :class:`DriverError`（``count`` 本身报错 = 页面/浏览器坏了，
        不是「元素不存在」）——这一条与 :meth:`count` 的「失败返回 0」**有意不一致**：
        那条是对齐 obscura 的旧口径，这条是真判断的前置，不能把故障折叠成 0。
        """
        cross = [k for k, v in (("ref", ref), ("selector", selector)) if v]
        if cross:
            # 跨面调用是接线错误：camoufox 的定位词是 role+name。
            # 抛 ValueError（不是 DriverError）——后者会被 steps/ 捕获成
            # unreachable_hard，于是接线 bug 变成一条理由通顺的负样本。
            raise ValueError(
                f"camoufox 面的 {op} 不支持 {cross}（它的目标编码是 role+name）。"
                f"ref/selector 是 obscura 面的词汇——拿错面说明调用点接了错实现。"
            )
        if not role or not name:
            # 无名字的元素**不提供定位路径**：``get_by_role(role, name="")``
            # 命中 0 个且不报错，与「元素不存在」同形（F5-c 的死亡路径）。
            return self._miss(op, role, name, "not_found")
        if role not in ROLE_VOCAB:
            # 词表外的 role 不可枚举也不可能被正确指定——直接判不存在，
            # 省一次往返，也免得 playwright 抛 ValueError 时被误读成能力故障。
            return self._miss(op, role, name, "not_found")

        locator = self._page.get_by_role(role, name=name, exact=True)
        try:
            count = int(await locator.count())
        except _PwError as exc:
            raise DriverError(
                f"camoufox 定位失败（{op} {role}/{name!r}）: {_brief(exc)}"
            ) from None
        if count == 0:
            return self._miss(op, role, name, "not_found")
        if count > 1:
            self.last_action = {
                "op": op, "role": role, "name": name,
                "outcome": "ambiguous", "matches": str(count),
            }
            return None
        return locator

    def _miss(self, op: str, role: str | None, name: str | None, outcome: str) -> None:
        self.last_action = {
            "op": op, "role": role or "", "name": name or "", "outcome": outcome,
        }
        return None

    async def _do(
        self, op: str, fn: Any, *, role: str | None = None, name: str | None = None
    ) -> None:
        """执行动作。成功记 ``ok``；失败抛——走到这里说明元素确实在，
        所以任何异常都是**能力不可用**（页面卡住 / 浏览器崩），不是业务事实。"""
        try:
            await fn()
        except _PwError as exc:
            # 唯一残留的歧义：count==1 之后元素被页面脚本换掉 →
            # strict mode violation。不解析消息文本（随版本变），一律按
            # 能力故障抛——保守方向是「不当成点成功」，代价是极少数竞态
            # 被记成 DriverError 而不是 unresolved。
            raise DriverError(
                f"camoufox 动作失败（{op}）: {_brief(exc)}"
            ) from None
        self.last_action = {
            "op": op, "role": role or "", "name": name or "", "outcome": "ok",
        }

    async def _settle(self) -> None:
        """动作之后把新文档等到 **DOM ready**——有界，等不到要**说出来**。

        为什么不用 playwright 的默认行为（``click`` 会等它触发的那次导航「完成」）：
        本机实测（2026-10-20，baidu 结果页上点「百度首页」）默认等待 **9–13s**，
        而同一地址 ``goto`` 只要 **0.3s**；5s 的动作预算于是让**一半的点击直接超时**
        ——两次跑出 ``Locator.click: Timeout 5000ms exceeded``，日志停在
        ``performing click action``。这既不是页面事实（元素在、可见、可用、
        已滚入视口、包围盒稳定）也不是能力故障，是**我们在等一个没人需要的信号**。

        改成 ``no_wait_after=True`` + 这里等一次 ``domcontentloaded``（上限 2s）：
        动作返回时新文档**至少 DOM ready**，观察拿得到内容，而不再被
        页面里某个慢 iframe 拖住。等不到就照实返回，由调用方的读稳逻辑处理
        （``cli._after_action_snapshot``，避坑指南 §8.8）——**不在这里猜**。

        结果记进 ``last_action["settle"]``（``ok`` / ``timeout``）：
        「等到了」与「没等到」必须在账上分得开，否则下一步读到的旧页没法归因。
        """
        try:
            await self._page.wait_for_load_state(
                "domcontentloaded", timeout=NAV_SETTLE_TIMEOUT_MS
            )
        except _PwError:
            if self.last_action:
                self.last_action["settle"] = "timeout"
            return
        if self.last_action:
            self.last_action["settle"] = "ok"

    # ── 内部 ─────────────────────────────────────────────────────

    async def _eval_text(self, expression: str) -> str:
        """取文本类表达式；失败当空串——**降级由调用方统一记录**。"""
        try:
            value = await self._page.evaluate(expression)
        except _PwError:
            return ""
        return value if isinstance(value, str) else ""

    async def _content(self) -> str:
        try:
            return await self._page.content()
        except _PwError:
            return ""

    # ── 采集面：链接 / 抽取（P3-β）────────────────────────────────

    #: 取链接的 JS：先滤掉空 ``href`` 的锚，再取 ``[可见文本, 绝对地址]``。
    #:
    #: 两道口径都定死在这里：
    #: - ``getAttribute('href').trim()`` 判空——``<a href="">`` 的 ``a.href``
    #:   会被解析成**当前页地址**，不过滤就凭空多出一串「指向自己的链接」；
    #: - 取 ``a.href`` 而**不是**属性原文——它是**已解析的绝对地址**，与
    #:   obscura ``browser_links`` 的实测形态一致（``https://www.baidu.com/link?url=…``）。
    _LINKS_JS = (
        "els => els.filter(e => (e.getAttribute('href') || '').trim())"
        ".map(e => [(e.innerText || e.textContent || '').trim(), e.href])"
    )

    async def links(self, *, limit: int = DEFAULT_LIMIT) -> tuple[CleanedLink, ...]:
        """列链接：``a[href]`` 的 ``(文本, 绝对地址)``，按文档顺序，**按 href 去重**。

        **去重是量出来的对齐，不是洁癖**（2026-10-20 实测，同一对搜索页）：

        | 页面 | obscura ``browser_links`` | camoufox 不去重 | camoufox 去重后 |
        |---|---|---|---|
        | baidu 结果页 | ``total=131 / distinct=131`` | ``168 / 133`` | ``133`` |
        | bing 结果页 | ``total=33 / distinct=33`` | ``48 / 35`` | ``35`` |

        obscura 那边 ``total == distinct`` 恒成立——**它自己就按 href 去重**。
        不去重时 camoufox 的 ``links_total`` 比它高 28–37%，而 ``links_total``
        是要进 D-2 口径的：两边不同义，并池比出来的分布就是假的。
        保留**第一次**出现的那条（与 ``steps/search.result_titles`` 的
        「同一 URL 重复时保留第一次」同一口径）。

        ⚠️ **失败抛** :class:`DriverError`（obscura 同位置是 ``on_error="empty"``
        吞成空元组）。空元组意味着「页面上没有链接」——那是页面事实；
        把一次能力故障写成页面事实，正是本仓库最贵的一类错。``observe``
        会把它转成 ``degraded`` + 空元组，**那才是「没采到」的正确形态**。

        ⚠️ ``text`` 两面的**来源不同**：obscura 给的是「域名 + 路径」拼出来的
        面包屑，这里给的是锚自身的文本。所以 ``links[].text`` 在两面
        **不同义**——这条在 P5 双跑比 ``candidate text`` 分布时要单独看
        （``extract`` 的标题不受影响，判断点 ① 走的是 ``extract``）。
        """
        try:
            rows = await self._page.eval_on_selector_all("a[href]", self._LINKS_JS)
        except _PwError as exc:
            raise DriverError(f"camoufox 取链接失败: {_brief(exc)}") from None

        cap = max(int(limit), 0)
        out: list[CleanedLink] = []
        seen: set[str] = set()
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue                      # 形状不对就丢，不猜
            text = str(row[0] or "").strip()
            href = str(row[1] or "").strip()
            if not href or href in seen:
                continue
            seen.add(href)
            out.append(CleanedLink(text=text, href=href))
            if len(out) >= cap:
                break
        return tuple(out)

    @staticmethod
    def _pluck_js(attr: str | None) -> str:
        """生成「按元素取值」的 JS 函数体。``attr=None`` 取文本。

        两条口径，都是量出来的：

        - **文本 = ``innerText || textContent``**。2026-10-20 实测（bing 结果页，
          camoufox）：``li.b_algo h2 a`` 的 10 个锚 **``innerText`` 全是空串**，
          而 ``textContent`` 就是完整标题。只取 ``innerText`` 的症状是
          「抽到 10 个空标题」，``steps/search.result_titles`` 于是判「长度不等
          或为空」→ ① 退回 fail-closed——**看着像选择器失效，其实是取文本的路窄了**。
          ``textContent`` 的已知代价是把隐藏文本也取进来；对「取标题」这件事，
          取到带噪声的标题远好过取到空串。
        - **属性 = ``typeof e[attr] === 'string' ? e[attr] : getAttribute(attr)``**：
          ``href`` / ``src`` 这类**有 URL 语义**的属性，DOM 属性给的是**解析后的
          绝对地址**（与 :meth:`links` 同一口径，两边结果才能互相比对）；
          ``title`` / ``data-*`` 这类在 DOM 上没有同名属性，自然落到
          ``getAttribute``。两条都取不到时给 ``''``，由调用方决定丢弃。
        """
        if attr:
            quoted = json.dumps(attr)
            return (
                "els => els.map(e => {"
                f"const a = {quoted};"
                "const v = (typeof e[a] === 'string' ? e[a] : e.getAttribute(a));"
                "return v == null ? '' : String(v).trim();"
                "})"
            )
        return "els => els.map(e => ((e.innerText || e.textContent || '')).trim())"

    async def extract(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        """按 ``{字段名: CSS selector}`` 抽内容——**复刻 obscura 的两条入参约定**。

        契约原样透传，不重命名也不解释（重命名等于把上游约定抄一份，
        抄的那份迟早与上游漂）：

        | 形态 | 例 | 行为 |
        |---|---|---|
        | 普通 | ``"h2"`` | 首个匹配的**可见文本** |
        | 属性 | ``"a@href"`` | 该元素的属性值（URL 类属性取解析后的绝对值） |
        | 数组 | ``"titles[]"`` | 全部匹配，值为数组（**返回的键名不带** ``[]``） |

        **抽不到就不写这个键**，不编造 ``""``——写空串会让「抽不到」与
        「页面上是空的」同形，而 ``steps/search.result_titles`` 恰是按
        「两数组等长且非空」判有效性：给它一个填满空串的数组，它会把 N 条假配对
        当 N 条好结果交出去，判断点 ① 于是拿它做**唯一判据**。

        **失败抛** :class:`DriverError`（obscura 同位置吞成空映射）：选择器语法
        错误是**配置 bug**，不是页面事实，而两者症状同形（都表现为「抽不到标题」）
        却该反向处置。调用方 ``orchestrator`` 已按「异常 → 记原因 → ① fail-closed」
        处理，抛出去不会带崩采集。
        """
        if not fields:
            return {}

        out: dict[str, Any] = {}
        for raw_name, raw_spec in fields.items():
            name = str(raw_name)
            want_all = name.endswith("[]")
            key = name[:-2] if want_all else name
            spec = str(raw_spec)
            selector, sep, attr = spec.rpartition("@")
            if not sep:                      # 没有 ``@``：整串都是选择器
                selector, attr = spec, ""
            values = await self._pluck(selector, attr or None)
            if want_all:
                # 空数组照写：键在、值是空数组，这是「选择器没命中」的如实形态，
                # 与「字段根本没抽」必须分得开（`result_titles` 就是靠这个判的）。
                out[key] = values
            elif values:
                out[key] = values[0]
        return out

    async def _pluck(self, selector: str, attr: str | None) -> list[str]:
        try:
            rows = await self._page.eval_on_selector_all(
                selector, self._pluck_js(attr)
            )
        except _PwError as exc:
            target = f"{selector}@{attr}" if attr else selector
            raise DriverError(f"camoufox 抽取失败（{target}）: {_brief(exc)}") from None
        return [str(v) if v is not None else "" for v in (rows or [])]

    # ── 采集面：标签页（P3-β）────────────────────────────────────

    async def new_tab(self, url: str | None = None) -> str:
        """开一张新页并切成当前页；返回**本驱动发的**句柄（``c1`` / ``c2``…）。

        ⚠️ 句柄**不是** obscura 那种服务端 tab id，两面不可互换；它的唯一用途是
        让 ``close_tab(id)`` 能指名关后台页。现有调用点（``steps/link_check``）
        只用「当前页」语义。

        开页后导航失败**先收掉这一页再抛**：留着它，当前页就停在一张
        ``about:blank`` 上——一次外链取不到，会连带把中间页的证据一起弄丢。
        """
        page: Any = None
        try:
            page = await self._session.new_page()
            if url:
                await page.goto(url, wait_until="load")
        except _PwError as exc:
            if page is not None:
                try:
                    await self._session.close_page(page)
                except _PwError:              # 清理失败不得掩盖真因
                    pass
            self.last_tab = {"op": "new_tab", "tab": "", "pages": "", "outcome": "failed"}
            raise DriverError(f"camoufox 开新页失败: {_brief(exc)}") from None

        self._tab_seq += 1
        tab_id = f"c{self._tab_seq}"
        self._tabs[tab_id] = page
        self._refs = {}                       # 换页即换 ref 空间
        self.last_tab = {
            "op": "new_tab", "tab": tab_id,
            "pages": str(len(self._session.pages)), "outcome": "ok",
        }
        return tab_id

    async def close_tab(self, tab_id: str | None = None) -> None:
        """关掉一页（``None`` = 当前页），**关的是当前页就回到上一页**。

        认不出的 ``tab_id`` **抛**，绝不「找一个关掉」——关错页的后果是后续
        ``observe`` 读到别的页，而那**不会报错**。
        """
        if tab_id is None:
            page = self._page
            target = next((k for k, v in self._tabs.items() if v is page), "")
        else:
            page = self._tabs.get(tab_id)
            target = tab_id
            if page is None:
                raise DriverError(
                    f"camoufox 不认识 tab {tab_id!r}"
                    f"（本会话发过的是 {sorted(self._tabs) or '无'}）"
                )
        try:
            closed = await self._session.close_page(page)
        except _PwError as exc:
            self.last_tab = {"op": "close_tab", "tab": target, "pages": "", "outcome": "failed"}
            raise DriverError(f"camoufox 关页失败: {_brief(exc)}") from None

        if target:
            self._tabs.pop(target, None)
        self._refs = {}
        self.last_tab = {
            "op": "close_tab", "tab": target,
            "pages": str(len(self._session.pages)),
            "outcome": "ok" if closed else "not_found",
        }

    # ── 组合采集（P3-β）──────────────────────────────────────────

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        """采集结构化观察——与 obscura 的 ``observe`` **同字段、同语义**。

        ``snapshot`` 是唯一必需项：失败即抛（没有观察就没有判定，I4）。
        其余各项缺失**降级为空/0 并把降级项记进 ``degraded``**——宁可让感知层判
        ``answer=None``，也不让「没采到」被当成「页面上确实没有」。

        ⚠️ ``degraded`` 里的名字与 obscura **不同，这是有意的**：obscura 记的是
        **tool 名**（``browser_links``…），camoufox 没有 tool 这一层，记的是
        **能力名**（``links`` / ``interactive`` / ``count:video, audio``）。
        下游（``gatekeeper`` / ``branches`` / ``gates``）只判「是否为空」，
        不认具体字符串；硬抄 tool 名等于在这里写一句谎话。
        """
        degraded: list[str] = []
        snap = await self.snapshot(max_chars=max_chars, degraded=degraded)

        try:
            links = await self.links()
        except DriverError:
            degraded.append("links")
            links = ()
        try:
            elements = await self.interactive()
        except DriverError:
            degraded.append("interactive")
            elements = ()
        # **当场的** ARIA 阶梯账目：``last_enumeration`` 是驱动级状态，
        # 枚举失败时它还留着**上一页**的数——直接读会把上一页的账
        # 记到这一页头上。取不到就是空账。
        face_ledger = dict(self.last_enumeration) if elements else {}

        video_count = await self._count_or_zero(MEDIA_SELECTOR, degraded)
        iframe_count = await self._count_or_zero(IFRAME_SELECTOR, degraded)

        basis, basis_error = await self._collect_basis(degraded, face_ledger)

        return Observation(
            url=snap.url,
            page_title=snap.title,
            body_text=snap.body,
            interactive_elements=elements,
            links=tuple(LinkItem(text=l.text, href=l.href) for l in links),
            # 采到的条数（已被 DEFAULT_LIMIT 截断）。与 obscura 同义：
            # 存档侧还会再截到 80/50，落盘的量必须能对上，否则 D-2 分不清
            # 「被裁掉」与「不存在」（见 Observation.elements_total 的说明）。
            elements_total=len(elements),
            links_total=len(links),
            # 跨面可比的元素账目（**默认不采**，见 __init__ 的说明）。
            # 两本账平级：``shared`` 是跨面可比的，``face`` 是本面 ARIA 阶梯。
            element_basis=basis,
            element_basis_error=basis_error,
            # 方言由**驱动实现**自己填（P4）：camoufox 面的 role 是 ARIA role。
            # 不填会让按词表筛元素的地方把 ``textbox`` 拿去 HTML 标签名那套里比对，
            # 静默取回 0 个候选。
            element_vocabulary=ELEMENT_VOCAB_ARIA_ROLE,
            video_tag_count=video_count,
            iframe_count=iframe_count,
            max_chars=snap.max_chars,
            truncated=snap.truncated,
            raw_len=snap.raw_len,
            stripped_ratio=snap.stripped_ratio,
            body_source=snap.body_source,
            degraded=tuple(dict.fromkeys(degraded)),   # 去重且保序
        )

    async def _count_or_zero(self, selector: str, degraded: list[str]) -> int:
        """计数，**失败记降级并返回 0**。

        为什么不复用 :meth:`count`：那条按 obscura 的 ``on_error="zero"`` 口径
        已经把失败折叠成 0，于是 ``observe`` 里**看不出**这个 0 是「页面上没有」
        还是「没采到」——而这两者的正确处置相反。严格版把区别留下来，
        对外口径仍由 :meth:`count` 维持（P1 的行为对齐）。
        """
        try:
            return int(await self._page.locator(selector).count())
        except _PwError:
            degraded.append(f"count:{selector}")
            return 0