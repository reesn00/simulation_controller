"""Gatekeeper（统一拦截点）的离线单测——方案 12 阶段 G。

覆盖四件事：

1. **迁移再导出不变**：``dom.detect_block`` / ``branches._same_page``
   的旧引用必须解析到 gatekeeper 里的**同一个对象**——再导出姿势
   （INC43 同款）如果写错（比如复制了一份），行为漂移是静默的。
2. **intercept 的 Verdict 映射**：判定顺序 = 证据硬度（机械事实 >
   死循环 > 词表 > 降级分类）。
3. **DeadLoopDetector**：尾部连续语义（交替不触发、滑出不触发）。
4. **三档处置 + RunRecord 事件账本**的形状。

不碰真浏览器、不碰 LLM——gatekeeper 的纪律就是纯代码零 token。
"""

from __future__ import annotations

from trajectory_pipeline.executor import branches, dom, gatekeeper
from trajectory_pipeline.executor.gatekeeper import (
    DeadLoopDetector,
    EscalateModel,
    FailClosed,
    InjectGuidance,
    Verdict,
    intercept,
)
from trajectory_pipeline.perception.base import Observation


def obs_at(url="https://x.test/", *, title="T", body="正文", degraded=()) -> Observation:
    return Observation(
        url=url, page_title=title, body_text=body, degraded=tuple(degraded)
    )


# ═══════════════════════════════════════════════════════════════════════
# 迁移再导出：旧引用必须指向唯一所有者
# ═══════════════════════════════════════════════════════════════════════


class Test迁移再导出:
    def test_dom_detect_block_是同一对象(self):
        """再导出不是复制——两处名字必须指向同一个函数对象，
        否则「唯一所有者」是假的，两份实现会各自漂移。"""
        assert dom.detect_block is gatekeeper.detect_block

    def test_branches_same_page_是同一对象(self):
        assert branches._same_page is gatekeeper._same_page


class TestDetectBlock行为不变:
    """迁移前后行为基线——用 test_dom 的代表用例口径（完整覆盖在
    test_dom.py，经再导出自动钉住迁移正确性）。"""

    def test_验证码URL(self):
        assert gatekeeper.detect_block(
            "https://x.test/static/captcha/a.html", "", "") == "captcha"

    def test_验证码标题不受正文网络措辞干扰(self):
        assert gatekeeper.detect_block(
            "https://x.test/", "百度安全验证", "网络不给力，请稍后重试"
        ) == "captcha"

    def test_限流正文(self):
        assert gatekeeper.detect_block(
            "https://x.test/", "结果", "访问过于频繁，请稍后再试"
        ) == "rate_limit"

    def test_正常页空串(self):
        assert gatekeeper.detect_block("", "", "") == ""


# ═══════════════════════════════════════════════════════════════════════
# intercept：Verdict 映射与判定顺序
# ═══════════════════════════════════════════════════════════════════════


class TestIntercept:
    def test_正常页(self):
        assert intercept(obs_at()) is Verdict.NORMAL

    def test_无观察按normal(self):
        """观察采集失败走调用方自己的异常路径（StepLog.error /
        _visit_guarded），不由 gatekeeper 二次猜测。"""
        assert intercept(None) is Verdict.NORMAL

    def test_验证码URL映射CAPTCHA(self):
        assert intercept(obs_at(
            url="https://wappass.baidu.com/static/captcha/x.html",
            title="百度安全验证", body="x",
        )) is Verdict.CAPTCHA

    def test_CF挑战标题映射BLOCKED_CF(self):
        """detect_block 对 CF 挑战页返回 "captcha"（历史口径不能动），
        intercept 用 CF 标题子集把 Verdict 细分出来。"""
        assert intercept(obs_at(
            url="https://x.test/", title="Just a moment...",
        )) is Verdict.BLOCKED_CF

    def test_限流正文映射BLOCKED_CF(self):
        assert intercept(obs_at(
            title="结果", body="访问过于频繁，请稍后再试",
        )) is Verdict.BLOCKED_CF

    def test_登录墙(self):
        assert intercept(obs_at(body="本视频需要登录后观看")) is Verdict.LOGIN_WALL

    def test_付费墙强信号(self):
        assert intercept(obs_at(body="VIP 会员 开通专属特权")) is Verdict.PAYWALL

    def test_弱付费词单命中不拦(self):
        """「开通会员」单命中是 nav-bar CTA 噪声（增量三十二教训）——
        弱短语不构成拦截级付费墙证据。"""
        assert intercept(obs_at(body="开通会员 享更多精彩")) is Verdict.NORMAL

    def test_降级观察分类DEGRADED(self):
        """降级不是拦截——分类陈述，降级观察照走判定层 fail-closed。"""
        assert intercept(obs_at(degraded=("no_body",))) is Verdict.DEGRADED

    def test_机械事实优先于词表(self):
        """验证码 URL（无歧义）+ 付费词（可能误命中）并存时，
        报更硬的那条。"""
        assert intercept(obs_at(
            url="https://x.test/captcha", body="VIP 会员 开通",
        )) is Verdict.CAPTCHA

    def test_登录墙优先于付费墙(self):
        """两墙并存报 LOGIN_WALL——付费前提常是登录，登录墙是更上游
        的拦截面。这个顺序是设计口径，钉住防漂移。"""
        assert intercept(obs_at(body="请登录，VIP 会员 开通")) is Verdict.LOGIN_WALL

    def test_死循环优先于词表判定(self):
        d = DeadLoopDetector()
        for _ in range(3):
            d.record("https://x.test/", "click(播放)", 500)
        assert intercept(obs_at(body="VIP 会员"), dead_loop=d) is Verdict.DEAD_LOOP


# ═══════════════════════════════════════════════════════════════════════
# 付费墙强信号真值表
# ═══════════════════════════════════════════════════════════════════════


class Test付费墙强信号:
    """强付费墙信号（``≥1强+≥1弱 / ≥2强 / ≥1强+价格``）的真值表。

    ⚠️ **2026-10-21 迁居**：这条判据原先挂在
    ``executor.branches._has_strong_paywall_signal`` 那个**没有调用点**
    的适配函数上——它随同页守卫窄化而退役，已删除。判据本身**没死**：
    它是 :func:`gatekeeper.is_paywall`（**拦截口径**）的判据，唯一所有者
    在 :mod:`trajectory_pipeline.perception.rule_perceptor`。

    ⚠️ **别把它和「分支前置事实」混起来**：贴 ``subscription_wall`` 标签
    认的是 :func:`rule_perceptor.paywall_evidence`（**门禁语境**）——
    人工复核判强弱组合是**营销**（实测 12 条 ``subscription_wall`` 没有
    一条站得住）。两套口径各管一段：**拦页面** vs **贴标签**。
    """

    #: ``(文本, 期望, 说明)``
    CASES = [
        ("需付费", False, "弱信号 1 关键词无价格"),
        # 仅弱关键词（开通/会员）= nav-bar CTA 模式，不拦
        ("开通会员观看", False, "仅弱关键词 不拦"),
        ("开通会员 享更多精彩", False, "仅弱关键词 不拦"),
        ("会员抢先看", False, "仅弱关键词 不拦（partial_free 同款）"),
        # 强 + 弱组合 = 像付费墙
        ("VIP 会员", True, "1 强+1 弱"),
        ("续费会员", True, "1 强+1 弱"),
        ("订阅 开通", True, "1 强+1 弱"),
        # ≥2 强关键词
        ("VIP 订阅 续费 开通会员", True, "多强关键词"),
        ("续费 包年", True, "2 强"),
        # 强 + 价格
        ("VIP 首3月9.9元/月", True, "1 强+价格"),
        ("¥99 包月", True, "1 强+价格"),
        ("订阅 9.9元", True, "1 强+价格"),
        # 弱+价格（无强关键词）= 仍弱——保守起见不拦
        ("开通会员 ¥9.9", False, "仅弱+价格 不拦"),
        ("", False, "空文本"),
    ]

    def test_真值表(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            has_strong_paywall_signal,
        )

        for text, want, note in self.CASES:
            got = has_strong_paywall_signal(text)
            assert got is want, f"{note}: text={text!r} want={want} got={got}"

    def test_拦截口径用它(self):
        """同一套判据经 :func:`gatekeeper.is_paywall` 落到拦截处置上。"""
        assert gatekeeper.is_paywall(obs_at(body="VIP 会员")) is True
        assert gatekeeper.is_paywall(obs_at(body="开通会员 享更多精彩")) is False

    def test_门禁语境是另一套判据(self):
        """**两套口径不许互相顶替**：强弱组合（拦截）认「VIP 会员」，
        门禁语境（贴标签）不认——它要的是「不订阅就看不了正片」。"""
        from trajectory_pipeline.perception.rule_perceptor import (
            paywall_evidence,
        )

        assert paywall_evidence("VIP 会员 开通") == ""
        assert paywall_evidence("订阅后观看") != ""


# ═══════════════════════════════════════════════════════════════════════
# DeadLoopDetector：尾部连续语义
# ═══════════════════════════════════════════════════════════════════════


class TestDeadLoopDetector:
    def test_同域名连续重复动作(self):
        d = DeadLoopDetector()
        d.record("https://a.test/1", "click(播放)", 100)
        d.record("https://a.test/2", "click(播放)", 100)
        d.record("https://a.test/3", "click(播放)", 100)
        assert d.detect() == "repeat_action"

    def test_连续空正文(self):
        """动作各不相同（不是动作重复），但正文连续取空——页面取不到
        内容的死循环形态。"""
        d = DeadLoopDetector()
        for i in range(3):
            d.record(f"https://a.test/{i}", f"goto(https://a.test/{i})", 0)
        assert d.detect() == "empty_body"

    def test_重复与空正文并存时报更具体的repeat(self):
        """同序列同时满足两条规则时报 repeat_action——动作重复是更
        具体的信号（空正文只是它的伴随症状）。"""
        d = DeadLoopDetector()
        for _ in range(3):
            d.record("https://a.test/", "goto(https://a.test/)", 0)
        assert d.detect() == "repeat_action"

    def test_交替模式不触发(self):
        """A站→B站→A站不是死循环——尾部连续语义是刻意的保守。"""
        d = DeadLoopDetector()
        d.record("https://a.test/", "click(x)", 10)
        d.record("https://b.test/", "click(x)", 10)
        d.record("https://a.test/", "click(x)", 10)
        assert d.detect() == ""

    def test_阈值不足不触发(self):
        d = DeadLoopDetector()
        d.record("https://a.test/1", "click(播放)", 100)
        d.record("https://a.test/2", "click(播放)", 100)
        assert d.detect() == ""

    def test_滑出窗口不触发(self):
        """三步同动作之后换动作——尾部不再连续，不判死循环。"""
        d = DeadLoopDetector()
        for _ in range(3):
            d.record("https://a.test/", "click(播放)", 100)
        d.record("https://a.test/", "goto(https://a.test/play)", 200)
        d.record("https://a.test/", "click(字幕)", 200)
        assert d.detect() == ""

    def test_空动作键不判重复(self):
        """action 传空串（调用方没有动作语义）时重复判定是伪信号。"""
        d = DeadLoopDetector()
        for _ in range(3):
            d.record("https://a.test/", "", 100)
        assert d.detect() == ""

    def test_域名字母大小写归一(self):
        d = DeadLoopDetector()
        d.record("https://A.test/1", "click(x)", 10)
        d.record("https://a.test/2", "click(x)", 10)
        d.record("https://A.TEST/3", "click(x)", 10)
        assert d.detect() == "repeat_action"


# ═══════════════════════════════════════════════════════════════════════
# 三档处置与事件账本
# ═══════════════════════════════════════════════════════════════════════


class Test三档处置:
    def test_InjectGuidance事件形状(self):
        e = InjectGuidance(
            verdict=Verdict.PAYWALL, guidance="目标站点存在付费墙已被拦截，请换站"
        )
        assert e.to_event() == {
            "kind": "inject_guidance",
            "verdict": "paywall",
            "guidance": "目标站点存在付费墙已被拦截，请换站",
        }

    def test_EscalateModel事件形状(self):
        e = EscalateModel(reason="连续 3 步空正文", backend="")
        assert e.to_event() == {
            "kind": "escalate_model", "reason": "连续 3 步空正文", "backend": "",
        }

    def test_FailClosed默认不落分支(self):
        """branch 空 = 运行级收口（如搜索页拦截：取候选之前，
        没有 outcome 可记）。"""
        e = FailClosed(reason="搜索页被反爬拦截（captcha）")
        assert e.to_event() == {
            "kind": "fail_closed",
            "reason": "搜索页被反爬拦截（captcha）",
            "branch": "",
        }

    def test_FailClosed可落指定分支(self):
        e = FailClosed(reason="步数预算耗尽", branch="unresolved")
        assert e.to_event()["branch"] == "unresolved"


class Test事件账本:
    def test_默认空list且落盘(self):
        from trajectory_pipeline.executor.orchestrator import RunRecord

        rec = RunRecord(task_id="T001", title="t", query="q", search_url="u")
        assert rec.gatekeeper_events == []
        # 空 list 照落——「没有拦截事件」与「没这个键」是两回事。
        assert rec.to_json()["gatekeeper_events"] == []

    def test_构造期拷贝防引用共享(self):
        from trajectory_pipeline.executor.orchestrator import RunRecord

        events = [{"stage": "search", "verdict": "captcha"}]
        rec = RunRecord(
            task_id="T001", title="t", query="q", search_url="u",
            gatekeeper_events=events,
        )
        events.append({"stage": "x", "verdict": "normal"})
        assert len(rec.gatekeeper_events) == 1
