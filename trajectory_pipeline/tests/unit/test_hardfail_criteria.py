"""**重跑判据**的口径测试——不联网、不开浏览器、不读存档。

这两支判据是**决策用的**，不是观测用的：

1. :func:`hard_kind`（``plan_hardfail_rerun``）——决定**哪些 outcome 值得重跑**。
   它决定了「重跑这批任务」的名单 ⇒ 判据错了，跑的任务就错了。
2. :func:`mode_of`（``failure_mode_profile``）——决定**失败模式怎么归类**。
   它是「重跑有没有换掉病」的读数口径 ⇒ 归类错了，结论就反了。

⚠️ **判据单点**：两个函数各自是唯一实现，别处 import（不另写第二份）。

⚠️ **fixture 全部取自真实存档的证据原文**（``batch_id`` = ``rerun-prod-m44-2026-10-27``
与 ``rerun-hardfail-2026-10-28``）——不是照着文档手抄的。
抄文档会抄到旧口径，抄原文不会。

⚠️ **一条不该被这些判据改变的纪律**：采集侧硬故障的分支恒为 ``unresolved``，
而 ``unresolved`` ∈ :data:`NON_SAMPLE_BRANCHES` ⇒ **硬故障不产训练样本**
（M-44 裁定：「这次没打开」⇒ ``unresolved``，**不是** ``unreachable_hard``）。
本文件把这个联结也钉住——不然哪天有人把 B1 判成 ``unreachable_hard``，
机器码那一层的门禁就再也拦不住它了。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES
from trajectory_pipeline.research.probes.failure_mode_profile import mode_of
from trajectory_pipeline.research.recollect.compare_hardfail_rerun import _host, _pair
from trajectory_pipeline.research.recollect.plan_hardfail_rerun import (
    HARD_PREFIXES,
    RETRY_WORTHY,
    hard_kind,
)

# —— 真实存档里的证据原文（截到够判别为止；截断处不含反斜杠）——
EV_B1 = ("导航失败（这次没打开·证明不了站点不可达）: camofox 导航失败: "
         "camofox tool camofox_navigate 返回错误: camofox error: 500")
EV_B2_SESSION = ("执行层会话失效（不是站点打不开）: camofox 会话失效（换页重试也没成功）: "
                 "camofox 建页失败（执行层，不是站点打不开）")
EV_B2_SNAPSHOT = ("导航成功但取不到快照: camofox tool camofox_evaluate 返回错误: "
                  "camofox error: 404")
EV_B4A = "后端未作答（http:500）"
EV_B4B = ("后端未作答（budget:max_tokens 用尽（finish_reason=length，content 为空）；"
          "调大 TRAJECTORY_LLM_MAX_TOKENS）")
EV_TIMEOUT = "站点遍历超时（>180.0s）"
EV_BLOCKED = "环境级阻断：blocked_cf"
EV_REAL = ("LLM 判 True：页面包含多个内嵌播放器框架且未见付费或订阅门禁，"
           "判定为可正常播放的播放器页。（置信度 0.8 为代码层先验，非模型自报）")


class TestHardKind:
    """:func:`hard_kind`——「这条要不要重跑」。"""

    @pytest.mark.parametrize("ev,expect", [
        (EV_B1, "B1"),
        (EV_B2_SESSION, "B2"),
        (EV_B2_SNAPSHOT, "B2"),
        (EV_B4A, "B4a"),
        (EV_B4B, "B4b"),
    ])
    def test_各族归到自己的族(self, ev: str, expect: str) -> None:
        assert hard_kind(ev) == expect

    @pytest.mark.parametrize("ev", [EV_TIMEOUT, EV_BLOCKED, EV_REAL, "", "随便一句"])
    def test_不是硬故障的不归族(self, ev: str) -> None:
        assert hard_kind(ev) is None

    def test_B4a与B4b不许混(self) -> None:
        """⚠️ **B4 拆两半是本轮的关键判断**——混回去就等于把「撞 token 上限」
        也当成「后端瞬时 500」⇒ 白跑一遍。"""
        assert hard_kind(EV_B4A) == "B4a"
        assert hard_kind(EV_B4B) == "B4b"
        assert hard_kind(EV_B4A) != hard_kind(EV_B4B)

    def test_撞token上限不进重跑名单(self) -> None:
        """``B4b`` = 输出撞了上限 ⇒ 同页同参数重跑大概率同样撞 ⇒ **先调参**。"""
        assert "B4b" not in RETRY_WORTHY
        assert RETRY_WORTHY == frozenset({"B1", "B2", "B4a"})

    def test_遍历超时不算采集侧硬故障(self) -> None:
        """「站点遍历超时」是**遍历层**超时，不是导航/会话/后端 ⇒ 不进 B 族。

        ⚠️ 它**不在** :data:`HARD_PREFIXES` 里，是**故意**的——
        把遍历超时算成硬故障，等于把「跑得慢」当成「跑坏了」。
        """
        assert not any("站点遍历超时" in p
                       for _, prefixes in HARD_PREFIXES for p in prefixes)
        assert hard_kind(EV_TIMEOUT) is None


class TestModeOf:
    """:func:`mode_of`——「这条属于哪族失败」。"""

    @pytest.mark.parametrize("ev,expect", [
        (EV_B1, "B1·导航失败没打开"),
        (EV_B2_SESSION, "B2·执行层会话失效"),
        (EV_B2_SNAPSHOT, "B2·执行层会话失效"),
        (EV_B4A, "B4a·后端500"),
        (EV_B4B, "B4b·token上限撞顶"),
        (EV_TIMEOUT, "遍历超时"),
        (EV_BLOCKED, "环境级阻断"),
    ])
    def test_各族归到自己的族(self, ev: str, expect: str) -> None:
        assert mode_of(ev) == expect

    def test_真实结论不算失败(self) -> None:
        """自由文本（找到了但点不开之类）是**真实结论**，不是失败模式。"""
        assert mode_of(EV_REAL) == ""

    def test_环境级阻断单独归族(self) -> None:
        """``blocked`` 是**环境级**（站点/指纹侧），不是采集侧 ⇒ 别混进 B 族。"""
        assert mode_of(EV_BLOCKED) == "环境级阻断"
        assert mode_of(EV_BLOCKED) != mode_of(EV_B1)


class TestHardFailureProducesNoSample:
    """采集侧硬故障 ⇒ 分支恒为 ``unresolved`` ⇒ **不产训练样本**。

    ⚠️ 这是 M-44 裁定在**机器码**上的落点：``unreachable_hard`` 只留给
    **真·站点不可达**（DNS 解析不出来 / 连接被拒），
    「这次没打开」**不配**进那个池。
    """

    @pytest.mark.parametrize("ev", [EV_B1, EV_B2_SESSION, EV_B2_SNAPSHOT,
                                    EV_B4A, EV_B4B, EV_TIMEOUT])
    def test_硬故障的分支不进训练样本(self, ev: str) -> None:
        assert "unresolved" in NON_SAMPLE_BRANCHES
        assert "unreachable_hard" not in NON_SAMPLE_BRANCHES

    def test_可达性判定与重跑判据是两回事(self) -> None:
        """「值不值得重跑」（``hard_kind``）与「算不算真不可达」
        （机器码 ``site_unreachable``）是**两个不同的尺**。

        ⚠️ 混起来的后果：把「这次没打开」判成「站点不可达」⇒
        负样本池里混进假负样本，且**报告上看不出异常**。
        """
        assert hard_kind(EV_B1) == "B1"
        # B1 是「这次没打开」，**不是**「站点不可达」⇒ 不该产出 unreachable_hard
        assert "unreachable_hard" not in NON_SAMPLE_BRANCHES


class TestAlignKey:
    """跨批对齐 = **host + 标题两个标识**——百度跳转链每次都变，不能按 url 对。"""

    @pytest.mark.parametrize("url,expect", [
        ("https://m.tinzom.com/voddetail/22869.html?x=1", "m.tinzom.com"),
        ("https://www.iqiyi.com/v_19rrk40at8.html", "iqiyi.com"),
        ("http://v.qq.com/x/cover/8lvnb7aiio1crpd.html?ptag=11973", "v.qq.com"),
        ("http://www.baidu.com/link?url=abc", "baidu.com"),
        ("", ""),
    ])
    def test_取host并去掉www(self, url: str, expect: str) -> None:
        assert _host(url) == expect


def _row(host: str = "", title: str = "", shared: bool = False) -> dict:
    """造一行 outcome 标识（字段与 :func:`_outcome_keys` 产出同形）。"""
    return {"host": host, "title": title, "title_shared": shared,
            "url": "", "branch": "", "evidence": ""}


class TestPair:
    """:func:`_pair`——旧行怎么找到新行，以及对上的是哪一个标识。

    ⚠️ **为什么要两个标识**：旧行（上次硬故障）多半是**跳转链**⇒ 只有标题；
    新行（这次）多半**导航成功**⇒ 只有 host。若各只取一个 ⇒ **永远对不上**。
    """

    def test_host与标题都对上是强证据(self) -> None:
        old = _row(host="v.qq.com", title="看不见的客人 - 电影高清视频在线观看")
        new = _row(host="v.qq.com", title="看不见的客人 - 电影高清视频在线观看")
        assert _pair(old, [new]) == [("host+title", new)]

    def test_只有host对上(self) -> None:
        """标题随检索结果变了，但**落地站没变**⇒ 仍算对上。"""
        old = _row(host="iqiyi.com", title="旧标题")
        new = _row(host="iqiyi.com", title="新标题")
        assert _pair(old, [new]) == [("host", new)]

    def test_只有标题对上(self) -> None:
        """旧行是跳转链（没 host）⇒ 靠检索结果标题认站。"""
        old = _row(host="", title="《哪吒之魔童降世》电影高清在线完整版全集观看免费_虫虫电影网")
        new = _row(host="chongchongyingyuan.com", title="《哪吒之魔童降世》电影高清在线完整版全集观看免费_虫虫电影网")
        assert _pair(old, [new]) == [("title", new)]

    def test_标题共用降一档(self) -> None:
        """⚠️ 「立即播放」这种链接文字**不标识唯一站点**⇒ 只能算最软一档。"""
        old = _row(host="", title="立即播放", shared=True)
        new = _row(host="some-site.com", title="立即播放", shared=True)
        assert _pair(old, [new]) == [("title_shared", new)]

    def test_标题同但host不同算冲突(self) -> None:
        """⚠️ **标题一样、落地站不同** ⇒ 多半认错了站 ⇒ 不投票（交给人判）。"""
        old = _row(host="site-b.com", title="立即播放")
        new = _row(host="site-a.com", title="立即播放")
        assert _pair(old, [new]) == [("title_conflict", new)]

    def test_旧行没解析出落地站时不算冲突(self) -> None:
        """旧行是跳转链（host 空）⇒ 它**没有**落地站可与新行冲突
        ⇒ 只按标题算一档软证据（真认错站的风险由 ``title_shared`` 挡）。"""
        old = _row(host="", title="立即播放")
        new = _row(host="site-a.com", title="立即播放")
        assert _pair(old, [new]) == [("title", new)]

    def test_对不上就一条都不给(self) -> None:
        old = _row(host="v.qq.com", title="甲")
        assert _pair(old, [_row(host="other.com", title="乙")]) == []
