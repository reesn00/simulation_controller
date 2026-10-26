"""Gatekeeper——外部判断与恢复控制的**统一拦截点**。

方案 12（agent 执行器升级）阶段 G 的产物：把此前散落三处的拦截类规则
收拢到一个模块、一个出口——

====================================  =====================================
规则                                  迁移前所有者
====================================  =====================================
反爬 / 限流 / 验证码（detect_block）  ``executor/dom.py``
付费墙 / 登录墙词表判定               ``perception/rule_perceptor.py``
同页守卫（_same_page）                ``executor/branches.py``
====================================  =====================================

收拢姿势（与 INC43 付费墙强弱词表迁移同款）：

- **物理迁移**：``detect_block``（连同三张 marker 表）与 ``_same_page``
  移入本模块，原模块反向导入再导出——orchestrator / cli / static_probe /
  site_search 与全部存量测试的旧引用继续解析，**零行为变化**。
- **引用收拢**：付费墙 / 登录墙词表**留在感知层**（``rule_perceptor``）。
  物理搬过来会迫使 perception 反向 import executor，破「perception
  不得 import executor」的层约束；而词表所有者本来就应该是感知层——
  gatekeeper 收拢的是**拦截判定出口**，不是词表本身。同一条纪律的另一
  个说法：感知层管「页面上有什么」，gatekeeper 管「要不要拦下来」。

为什么没有「静默自愈」档
------------------------
外部参考方案（agentruntime-plan，原稿已随 2026-10-25 废弃文本清理删除）的第一层是「换 IP / 清指纹静默重试，
对 LLM 完全透明」。2026-10-16 拍板（方案 12 D-A2）**否决**了它：本项目
是合成轨迹管线，不是在线服务——「被拦 → 修复 → 重试」的完整过程
**本身就是训练信号**，静默抹掉等于把轨迹伪造成「从未发生拦截」。
所以处置只有三档（:class:`InjectGuidance` / :class:`EscalateModel` /
:class:`FailClosed`），且每一档都落 ``RunRecord.gatekeeper_events``
**完全可见**。

判定纪律
--------
- **纯代码、零 token**：全部判据是字符串事实与机械行为特征，不调 LLM。
- **判定逻辑在使用点，不在采集层**：本模块只提供判定函数，由控制流
  显式调用——不塞进 ``driver.observe()``。这是 2026-10-09 那轮的教训
  （见避坑指南 / 00-总体方案 §结论）：判定进采集层会让 FakeDriver
  构造的观察带不上拦截状态，测试与真实路径行为分叉。
- **确定性事实优先**：``intercept`` 的判定顺序是「机械事实 > 词表判定」
  ——URL/标题/限流词（无歧义）> 死循环（行为机械事实）> 登录墙 >
  付费墙（词表）> 观察降级。与 L5 仲裁的口径同源。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Final
from urllib.parse import urlsplit

from trajectory_pipeline.perception.base import Observation
# 词表判定复用感知层既有所有者（executor → perception 单向依赖，与
# branches.py 引 has_strong_paywall_signal 同款，不破层）。
from trajectory_pipeline.perception.rule_perceptor import (
    ACCESS_HINTS,
    has_strong_paywall_signal,
)


# ═══════════════════════════════════════════════════════════════════════
# Verdict：拦截判定的封闭取值集
# ═══════════════════════════════════════════════════════════════════════


class Verdict(str, Enum):
    """拦截判定结论（方案 12 §4.1 定义的取值集，封闭不扩）。"""

    NORMAL = "normal"
    #: CF / WAF / 限流——环境级阻断，换站、加间隔或换引擎才有的救。
    BLOCKED_CF = "blocked_cf"
    #: 验证码 / 人机验证——同属反爬，但成因与 CF 不同档。
    CAPTCHA = "captcha"
    #: 登录墙。处置预期是 InjectGuidance（换站），不是突破。
    LOGIN_WALL = "login_wall"
    #: 付费墙。判定用**强信号三判据**（见 :func:`is_paywall`）。
    PAYWALL = "paywall"
    #: 行为死循环（:class:`DeadLoopDetector` 的状态判定）。
    DEAD_LOOP = "dead_loop"
    #: 观察降级——**不拦截**，只是分类陈述；降级观察照走判定（fail-closed
    #: 纪律：降级→None→unresolved，由判定层自己收口，不由 gatekeeper 拦）。
    DEGRADED = "degraded"


#: 「**被拦**」的判定集——账本盖 ``blocked_surface`` / ``attempt`` 只盖这四条
#: （方案 20 §R3）。
#:
#: 四条都是**页面侧的访问控制**：CF/WAF 拦截 · 验证码 · 登录墙 · 付费墙。
#: 报表要按 verdict 分桶（02 避坑 §3.7：真·反爬单列、登录墙单列），所以这里
#: 只是「哪些算被拦」的**范围**，不是把四者当一类。
#:
#: 另外三条**不在内**，各有各的理由：
#:
#: - ``normal`` / ``degraded`` —— 本来就不拦（见 ``_NONTERMINATING_VERDICTS``）；
#: - ``dead_loop`` —— **行为失败**，不是页面属性。「被拦几次」数进死循环，
#:   会把「模型绕不出来」读成「站点在拦我们」。
BLOCK_VERDICTS: frozenset[Verdict] = frozenset({
    Verdict.BLOCKED_CF,
    Verdict.CAPTCHA,
    Verdict.LOGIN_WALL,
    Verdict.PAYWALL,
})


# ═══════════════════════════════════════════════════════════════════════
# 反爬拦截识别（自 executor/dom.py 迁入；dom.py 反向再导出）
# ═══════════════════════════════════════════════════════════════════════
#
# 为什么要单独认出来
# ------------------
# 搜索引擎对连发请求会返回验证码页。实测：同批连跑 3 个 task，
# 第 1 个正常，第 2、3 个被重定向到
# ``wappass.baidu.com/static/captcha/tuxing_v2.html``（标题「百度安全验证」）。
#
# 这类页面的表现是「正文极短 + 零链接」，而当前控制流把它记成
# 「搜索页未提取到候选」——**措辞指向「这批素材没价值」，
# 真相却是「基础设施被拦了」**。二者处置完全不同：
# 前者该换素材，后者该加间隔/换引擎。
#
# 混在一起的代价是**静默的**：跑一批 20 条，被拦掉 6 条，
# 报表上表现为「这批搜索页质量差」，没人会去查反爬。
#
# 只看 URL 与标题，不看正文
# -------------------------
# 百度验证码页正文是「网络不给力，请稍后重试」——**那句话字面是网络错误**，
# 在验证码页上只是模板文案。若把它当判据，真网络故障就会被误判成反爬，
# 正好与本模块的目的相反。URL 路径（``/captcha``、``wappass``）与
# 页面标题（「安全验证」）才是无歧义的强信号。

#: URL 路径里的反爬特征。**全部是子路径标记**，不匹配裸域名。
_BLOCK_URL_MARKERS: Final = (
    "/captcha", "wappass", "antispider", "secverify", "/verifycode",
    "/challenge", "/sorry/index",
)

#: 标题里的反爬特征。
_BLOCK_TITLE_MARKERS: Final = (
    "安全验证", "验证码", "访问验证", "百度安全", "人机验证",
    "robot check", "just a moment", "attention required", "access denied",
)

#: 正文里的**限流**特征。只认「频繁」类措辞——它们在正文里是明确的限流信号，
#: 且与网络故障的措辞（「网络不给力」「连接超时」）不重叠。
_BLOCK_BODY_MARKERS: Final = (
    "访问过于频繁", "请求过于频繁", "操作过于频繁",
    "too many requests", "unusual traffic", "rate limit",
)

#: 标题里的 **CF / WAF 挑战**特征——``_BLOCK_TITLE_MARKERS`` 的子集。
#: ``detect_block`` 对它们统一返回 ``"captcha"``（历史口径，不能动——
#: 它落在 P1 的 ``search_blocked`` 字段上，存档口径已冻结）；
#: :func:`intercept` 用这份子集把 Verdict 细分为 ``BLOCKED_CF``。
_CF_TITLE_MARKERS: Final = (
    "just a moment", "attention required", "access denied",
)


def detect_block(url: str, title: str, body: str = "") -> str:
    """这个页面是不是被反爬/限流拦了。返回原因串，空串 = 没被拦。

    纯字符串事实判定，**不做语义猜测**——URL 路径与页面标题都是
    已经观察到的客观字段，符合「代码层做护栏」的分工。

    优先级 ``URL > 标题 > 正文``：越靠前越无歧义。
    """
    lowered = (url or "").lower()
    if any(marker in lowered for marker in _BLOCK_URL_MARKERS):
        return "captcha"
    head = (title or "").lower()
    if any(marker.lower() in head for marker in _BLOCK_TITLE_MARKERS):
        return "captcha"
    text = (body or "").lower()
    if any(marker.lower() in text for marker in _BLOCK_BODY_MARKERS):
        return "rate_limit"
    return ""


# ═══════════════════════════════════════════════════════════════════════
# 同页守卫（自 executor/branches.py 迁入；branches.py 反向再导出）
# ═══════════════════════════════════════════════════════════════════════


def _same_page(a: Observation | None, b: Observation | None) -> bool:
    """这两份观察是不是**同一个页面**——rubric D-4 边界规则的可机械形态。

    与 :mod:`evaluation.scorers.d4_scorer` 的同名判定**同源同规则**
    （url / 元素数 / 正文三个粗粒度特征），但**不 import**——executor
    不依赖 evaluation，两处漂移由 test_branches 的对照测试钉住。

    只比三个粗粒度特征而不是逐字段：网址会从 http 跳 https、
    正文会因懒加载微调，逐字段比对会把「同一页的两次快照」判成不同页。
    """
    if a is None or b is None:
        return False
    if a.url != b.url:
        return False
    if len(a.interactive_elements) != len(b.interactive_elements):
        return False
    return a.body_text == b.body_text


# ═══════════════════════════════════════════════════════════════════════
# 登录墙 / 付费墙判定（词表在感知层；判定出口收拢于此）
# ═══════════════════════════════════════════════════════════════════════


def _hints_hit(kind: str, text: str) -> bool:
    """:data:`ACCESS_HINTS` 里某一类的短语是否命中文本。"""
    for k, phrases in ACCESS_HINTS:
        if k == kind and any(p in text for p in phrases):
            return True
    return False


def is_login_wall(obs: Observation) -> bool:
    """页面是不是登录墙——ACCESS_HINTS 的 login 类短语命中正文。

    与感知层 fallback（``infer_access_from_evidence``）同源同词表，
    区别只在用途：那边是②的**细分证据**（login → login_required），
    这边是拦截口径（要不要把页面拦下来）。两处若口径分叉，先改词表
    所有者（rule_perceptor），再同步这里——不存在第二份词表。

    ⚠️ 误拦风险已知：导航栏「请登录」按钮与登录墙同词。agent 模式
    （A2）接线时用实测校准强度闸（比如要求命中 ≥2 短语）；阶段 G
    只收拢出口，不改判据。
    """
    return _hints_hit("login", obs.body_text or "")


def is_paywall(obs: Observation) -> bool:
    """页面是不是付费墙——**强信号三判据**（≥1强+≥1弱 / ≥2强 / ≥1强+价格）。

    刻意不用 :func:`classify_paywall` 的弱短语命中：``PAYWALL_WORDS``
    第一条就是「开通会员」——它在站点导航栏广告（nav-bar CTA）里
    高频出现，单凭它分不清真付费墙与入口页广告。强信号三判据是
    增量三十二以来被同页守卫旁路与 INC43 救济链两头验证过的口径，
    与 ``branches._precondition_failed`` 的旁路判据**同一句话**。
    """
    return has_strong_paywall_signal(obs.body_text or "")


# ═══════════════════════════════════════════════════════════════════════
# 行为死循环检测（新增；阶段 G 只实现不接线，A2 接 agent_loop）
# ═══════════════════════════════════════════════════════════════════════


class DeadLoopDetector:
    """滑动窗口死循环检测——「模型在同一处打转」的机械事实判定。

    两条规则（方案 12 §2.2，命中任一即判死循环）：

    - **重复动作**：窗口尾部连续 ``repeat_threshold`` 步是**同一域名上的
      同一动作**（``(host, action)`` 全同）；
    - **空正文**：窗口尾部连续 ``empty_body_threshold`` 步有效正文为空
      （``body_len <= 0``）——页面反复取不到内容，多半是渲染失败或
      模型在 JS 骨架页上空转。

    判定口径是**尾部连续**而非「窗口内计数」：交替模式（A站→B站→A站）
    本身不是死循环，不该触发；这是刻意的保守——误拦一次真探索的
    代价（轨迹断了）比漏判一次死循环（步数预算兜底）高。

    scripted 模式固定四步序列不存在死循环，本检测器**无调用方**；
    A2 的 ``agent_loop`` 每步 ``record`` 一次、每步 ``detect`` 一次。
    """

    def __init__(
        self,
        window: int = 8,
        repeat_threshold: int = 3,
        empty_body_threshold: int = 3,
    ) -> None:
        self._history: deque[tuple[str, str, int]] = deque(maxlen=window)
        self._repeat_threshold = repeat_threshold
        self._empty_body_threshold = empty_body_threshold

    def record(self, url: str, action: str, body_len: int) -> None:
        """记一步。``action`` 是调用方定义的动作键（tool+目标），
        ``body_len`` 是该步观察的**有效正文长度**（strip 后），
        ``url`` 用于提取 host（提取失败按空串——不影响重复判定）。
        """
        host = ""
        try:
            host = urlsplit(url or "").hostname or ""
        except ValueError:
            host = ""
        self._history.append((host.lower(), action or "", body_len))

    def detect(self) -> str:
        """当前是不是死循环。返回原因串（``repeat_action`` / ``empty_body``），
        空串 = 不是。"""
        tail = list(self._history)[-self._repeat_threshold:]
        if (
            len(tail) == self._repeat_threshold
            and tail[0][1] != ""
            and len({(h, a) for h, a, _ in tail}) == 1
        ):
            return "repeat_action"
        empty_tail = list(self._history)[-self._empty_body_threshold:]
        if (
            len(empty_tail) == self._empty_body_threshold
            and all(n <= 0 for _, _, n in empty_tail)
        ):
            return "empty_body"
        return ""


# ═══════════════════════════════════════════════════════════════════════
# 三档处置（方案 12 §4.2；L1 静默自愈已按 D-A2 否决删除）
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class InjectGuidance:
    """档一：阻断页面数据灌入 + 向任务状态注入标准引导指令 + 重规划。

    适用：登录墙 / 付费墙这类「该站没救了，但任务还能救」的拦截。
    """

    #: 触发判定的 Verdict（审计用——将来读账本的人要能对回页面）。
    verdict: Verdict
    #: 标准引导指令原文（如「目标站点存在付费墙已被拦截，请换站」）。
    #: **必须**落 RunRecord——引导文案本身就是训练信号。
    guidance: str

    def to_event(self) -> dict[str, str]:
        return {
            "kind": "inject_guidance",
            "verdict": self.verdict.value,
            "guidance": self.guidance,
        }


@dataclass(frozen=True)
class EscalateModel:
    """档二：切换高阶后端接管后续步骤。

    适用：连续失败 / 复杂 SPA——判定当前模型啃不动这个页面。
    后端配置（``TRAJECTORY_LLM_ESCALATE_*``）在 A2 落地，阶段 G 只
    对象化形状。
    """

    #: 升配触发原因（机械事实描述，如「连续 3 步空正文」）。
    reason: str
    #: 目标后端配置名。空 = 未接线（阶段 G）。
    backend: str = ""

    def to_event(self) -> dict[str, str]:
        return {"kind": "escalate_model", "reason": self.reason,
                "backend": self.backend}


@dataclass(frozen=True)
class FailClosed:
    """档三：代码收口——agent 的失败必须收敛回代码控制的出口。

    ``branch`` 空 = 运行级收口**不落分支**（如搜索页反爬拦截：发生在
    取候选之前，没有 outcome 可记，与 ``search_blocked`` 的口径一致）；
    非空 = 收口到该失败分支（agent 模式，22 分支封闭集之一）。
    """

    reason: str
    branch: str = ""

    def to_event(self) -> dict[str, str]:
        return {"kind": "fail_closed", "reason": self.reason,
                "branch": self.branch}


# ═══════════════════════════════════════════════════════════════════════
# 统一拦截点
# ═══════════════════════════════════════════════════════════════════════


def intercept(obs: Observation | None, dead_loop: DeadLoopDetector | None = None) -> Verdict:
    """这份观察要不要拦？拦在哪一类？

    判定顺序 = 证据硬度（确定性事实优先）：

    1. ``detect_block``：URL / 标题 / 限流词——无歧义机械事实；
    2. 死循环：行为机械事实（需调用方传入有状态的 detector）；
    3. 登录墙 / 付费墙：词表判定（可能误命中，放机械事实之后）；
    4. 观察降级：分类陈述，**不是拦截**（降级观察照走判定层 fail-closed）。

    ``obs is None``（观察采集失败）返回 :attr:`Verdict.NORMAL`——
    采集失败走调用方自己的异常路径（``_visit_guarded`` / StepLog 的
    error 字段），不该由 gatekeeper 二次猜测。
    """
    if obs is None:
        return Verdict.NORMAL
    reason = detect_block(obs.url, obs.page_title, obs.body_text)
    if reason == "captcha":
        head = (obs.page_title or "").lower()
        if any(marker in head for marker in _CF_TITLE_MARKERS):
            return Verdict.BLOCKED_CF
        return Verdict.CAPTCHA
    if reason == "rate_limit":
        return Verdict.BLOCKED_CF
    if dead_loop is not None and dead_loop.detect():
        return Verdict.DEAD_LOOP
    if is_login_wall(obs):
        return Verdict.LOGIN_WALL
    if is_paywall(obs):
        return Verdict.PAYWALL
    if obs.degraded:
        return Verdict.DEGRADED
    return Verdict.NORMAL
