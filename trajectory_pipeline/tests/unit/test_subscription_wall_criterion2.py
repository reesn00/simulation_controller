"""``subscription_wall`` 的**判据二**（订阅价目门禁）单测。

2026-11-04：agent 路径开始核 ``subscription_wall`` 的前置事实后，真付费墙
页（youku《功夫》/ fun.tv / mgtv / iqiyi）因「没写『订阅后观看』那种措辞」
被降级成 ``unresolved`` ⇒ 掉出池子（台账 §M-37 记的副作用）。

判据二 = **页面级三要素同现**（价目 + 开通动作 + 权益承诺）+ **登录门排除**。
样例正文取自盘上存档（``output/pipeline/_backup_pre_m58/samples/``）与
现场重采（``research/probes/camofox_r5_step6_live_check.json``），
这里只保留判据要读的那几行。

判据的**放宽幅度**是实测出来的，不是论证出来的——实测见
``research/probes/subscription_wall_criterion2_measure.py``。
"""

from __future__ import annotations

from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.perception.base import Decision, Observation
from trajectory_pipeline.perception.rule_perceptor import (
    login_gate_phrase,
    paywall_evidence,
    paywall_price_evidence,
    subscription_wall_evidence,
)

#: youku《功夫》存档正文（实测：判据一读数是空串）。
YOUKU_KUNGFU = (
    "首页\n嫁金钗\n电视剧\n动漫\n分类\n历史\n客户端\n更清晰\n客户端\n更流畅\n客户端\n"
    "登录\n视频\n讨论1万+\n进站必看\n功夫\n简介\nN刷指数83.8 9.5分8.9分金马奖无厘头"
    "喜剧动作喜剧\n首3月每月12元\n立即开通\n语言选择\n普通话\n粤语\n同声评论\n正片\nVIP\n功夫\n"
    "周边视频\n周星驰巅峰之作，看小混混如何成为黑帮老大\n03:17\n"
)

#: fun.tv 现场正文（实测：判据一读数是空串）。
FUN_TV_KUNGFU = (
    "首页\n小城良方\n电视剧\n短剧\n电影\n综艺\n会员中心\n筛选\n搜你想看的\n搜索\n会员\n"
    "游戏\n客户端\n快捷访问\n消息\n历史\n上传\n登录\n精彩即将开始\n正在缓冲，请稍候\n功夫\n"
    "客户端流畅播放\n功夫\n详情\n9.1\n分\n2004年\n周星驰\n黄圣依\n喜剧\n犯罪\n动作\n"
    "加追\n下载\n分享\n黄金会员首月低至12元\n新朋友，开通享精彩内容\n开通会员\n功夫（双语）\n"
)

#: iqiyi《功夫》现场正文（实测：判据一读数是空串——DOM 换行把门禁句切开了）。
IQIYI_KUNGFU = (
    "首页\n小城良方\n电视剧\n短剧\n电影\n综艺\n会员中心\n筛选\n搜你想看的\n搜索\n会员\n"
    "游戏\n客户端\n使用爱奇艺Windows客户端\n享更佳视听体验\n高清视频\n杜比音效\n"
    "离线下载视频\n下载客户端\n查看更多\n快捷访问\n消息\n历史\n上传\n登录\n精彩即将开始\n"
    "正在缓冲，请稍候\n功夫（粤语）\n客户端流畅播放\n功夫（粤语）\n详情\n9.1\n分\n2004年\n"
    "周星驰\n黄圣依\n喜剧\n犯罪\n动作\n加追\n下载\n分享\n1元购3天黄金会员\n"
    "新朋友特惠 畅看热门好剧\n开通会员\n功夫（粤语）\n"
    "8\n14\n试看\n05:44\n后结束，观看完整视频请\n开通VIP会员\n功夫（粤语）\n"
)

#: youku《功夫》**现场**正文——同一页，但多了登录门那一句。
YOUKU_KUNGFU_LIVE = (
    "首页\n嫁金钗\n电视剧\n动漫\n分类\n会员特惠\n历史\n客户端\n登录\n登录/注册\n"
    "同步多端观看记录 尊享高清观影体验\n首\n3\n月每月\n12\n元，畅享精彩内容\n立即开通\n"
    "由于安全原因，请 登录 后继续观看\n视频\n讨论1万+\n功夫\n简介\n"
    "年卡约5.1折\n提前续费 续享大屏超清观看体验\n立即续费\n语言选择\n普通话\n粤语\n"
    "同声评论\n正片\nVIP\n"
)

#: v.qq 三傻大闹宝莱坞——**只有营销、没有价目**（实测：判据二不得成立）。
VQQ_MARKETING = (
    "电视剧\n电影\n综艺\n动漫\n少儿\n短剧\n纪录片\n全部\n会员专区\n游戏\n快捷访问\n"
    "历史\n创作\n应用\n下载客户端\n播放更流畅\n腾讯视频\n登录发个友善的弹幕吧！\n"
    "00:00\n/\n02:09\n倍速\n720P\n11 VIP可关闭该广告\n三傻大闹宝莱坞6\n讨论\n"
    "登录参与讨论\n暂无评论，快来说两句吧\n参与评论\n三傻大闹宝莱坞6\n"
    "486次播放 · 2021年4月28日上传\n新客VIP专享特惠！\n好剧热综持续更新，会员畅享精彩内容\n"
    "开通会员\n播放列表\n"
)

#: cctv 导航栏形态——只有「VIP会员」，**没有价目**（第 1 项撤池的那类形态）。
CCTV_NAV = (
    "首页\n电视剧\n电影\n综艺\n少儿\n纪录片\n新闻\n体育\n儿童\n气象\n"
    "VIP会员\n登录\n注册\n"
)


def _obs(url: str = "https://x.test/v/1", body_text: str = "") -> Observation:
    return Observation(url=url, page_title="标题", body_text=body_text)


def _paywall_decision() -> Decision:
    return Decision(
        question="player.ok",
        answer=False,
        confidence=0.9,
        evidence="页面要付钱才能看正片",
        source="llm",
        payload={"paywall": True},
    )


class Test判据二读数:
    """判据二的真值表——读数钉住，防回潮。"""

    def test_youku存档正文_判据一为空串(self):
        """**为什么需要判据二**：真付费墙页在判据一（门禁语境）下读数是空串。

        正因为它没有写「订阅后观看」那种措辞，agent 路径核前置事实后
        才会把它降级成 ``unresolved``。
        """
        assert paywall_evidence(YOUKU_KUNGFU) == ""
        assert paywall_price_evidence(YOUKU_KUNGFU) != ""

    def test_youku存档正文_判据二成立(self):
        hit = paywall_price_evidence(YOUKU_KUNGFU)
        assert hit != ""
        assert "开通动作" in hit and "权益承诺" in hit

    def test_fun_tv现场正文_判据二成立(self):
        assert paywall_evidence(FUN_TV_KUNGFU) == ""
        assert paywall_price_evidence(FUN_TV_KUNGFU) != ""

    def test_iqiyi现场正文_判据二成立(self):
        assert paywall_evidence(IQIYI_KUNGFU) == ""
        assert paywall_price_evidence(IQIYI_KUNGFU) != ""

    def test_vqq营销页_判据二不成立(self):
        """v.qq 只有营销（「新客VIP专享特惠！/开通会员」）**没有价目** ⇒ 不成立。"""
        assert paywall_price_evidence(VQQ_MARKETING) == ""

    def test_cctv导航页_判据二不成立(self):
        """cctv 导航栏只有「VIP会员」⇒ 不成立（第 1 项撤池的那类形态）。"""
        assert paywall_price_evidence(CCTV_NAV) == ""

    def test_youku现场正文_登录门排除后不成立(self):
        """同一页的**现场**正文多了「由于安全原因，请 登录 后继续观看」
        ⇒ 那是**登录墙**（``login_required``），不是订阅墙。"""
        assert login_gate_phrase(YOUKU_KUNGFU_LIVE) == "请登录"
        assert paywall_price_evidence(YOUKU_KUNGFU_LIVE) == ""

    def test_登录门短语_空白容忍(self):
        """DOM ``inner_text`` 常把 CJK 拆成带空格的碎片
        （「请 登录 后继续观看」）——裸子串匹配会整个漏掉。"""
        assert login_gate_phrase("由于安全原因，请 登录 后继续观看") == "请登录"
        assert login_gate_phrase("请先登录或注册") == "请先登录"
        assert login_gate_phrase("客户端\n登录\n会员中心") == ""

    def test_没有价目就没有判据二(self):
        """只有开通动作与权益承诺、**没有价目** ⇒ 不成立
        （纯营销与纯导航栏 CTA 都被这一条挡住）。"""
        assert paywall_price_evidence("开通会员\n正片\nVIP") == ""


class Test分支前置事实核验:
    """``executor.branches.precondition_failed`` 的两条路径（scripted / agent）
    共用同一份核——这里核它认判据二。"""

    def test_youku正文_不再降级unresolved(self):
        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/movie",
            _paywall_decision(),
            obs=_obs(body_text=YOUKU_KUNGFU),
            pre_obs=_obs(body_text=YOUKU_KUNGFU),
        )
        assert out.branch == "subscription_wall", (
            f"真付费墙被降级成 {out.branch}——判据二没接上"
        )

    def test_vqq营销页_仍然降级unresolved(self):
        """没有价目的营销页仍然是假阳 ⇒ 继续降级（不靠判据二复活）。"""
        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/movie",
            _paywall_decision(),
            obs=_obs(body_text=VQQ_MARKETING),
            pre_obs=_obs(body_text=VQQ_MARKETING),
        )
        assert out.branch == "unresolved"

    def test_门禁语境仍走判据一(self):
        """判据一没被改动：门禁命题页面照旧成立。"""
        assert subscription_wall_evidence("订阅后观看") != ""

    def test_救济路径口径不变(self):
        """H-16 修复① 的证伪记录：营销模块**不进**门禁语境判据。

        判据二只服务 ``subscription_wall`` 的前置事实核验，
        不得渗进 :func:`paywall_evidence`（救济路径认它）。
        """
        assert paywall_evidence("新用户首月9.9元，开通会员 立即抢购") == ""
        assert paywall_price_evidence("新用户首月9.9元，开通会员 立即抢购") == ""
