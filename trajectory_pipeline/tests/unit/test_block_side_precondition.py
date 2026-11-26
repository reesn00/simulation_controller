"""``blocked`` 的**站点侧判据只有一份**（H-51 · 2026-11-26）。

owner 裁定（2026-11-26）：「d5 已经判『不算』（降 undetermined），标签侧要跟」
——落地形态是**先把判据抽成共享单点**，标签侧接不接**等实测读数再定**。

判据唯一所有者：:func:`rule_perceptor.block_side_evidence`（边缘挑战模板
排除 **且** 站点自述形态在场）。两处消费它：

1. ``evaluation.scorers.d5_scorer._check_block_side``——**出不出分**；
2. ``executor`` 标签侧——**要不要把「我们被拦了」记成「这个站看不了」**
   （仍未接，见 ``branches.BRANCH_PRECONDITION['blocked']``）。

⚠️ **补之前的样子**：``d5`` 自己一份 ``SITE_SELF_REPORT_KINDS``，标签侧
根本没有这道核 ⇒ 评分器说「站点侧不成立」，标签侧照贴 ``blocked``，
两边在报表上都看不出异常。这正是「判据必须单点」的理由。
"""

from __future__ import annotations

from trajectory_pipeline.evaluation.scorers import d5_scorer
from trajectory_pipeline.perception import rule_perceptor
from trajectory_pipeline.perception.rule_perceptor import block_side_evidence

#: CF / WAF 的中间页模板（跨站点一字不差）。
_CF_PAGE = (
    "Just a moment...\nChecking your browser before accessing example.com.\n"
    "Performance & security by Cloudflare\nRay ID: 8f2c1a9b"
)

#: 站点自己写的地区阻断（用 ``ACCESS_HINTS['region']`` 里的短语）。
_SITE_REGION = "抱歉，当前地区不支持播放，请切换网络后重试。"


class Test站点侧判据:
    def test_站点自述的地区阻断_成立(self):
        hit = block_side_evidence("视频详情", _SITE_REGION)
        assert hit and "region" in hit

    def test_CDN挑战页_不成立(self):
        """挑战页一个字都不是站点说的——「我们被拦了」不构成「这个站看不了」。"""
        assert block_side_evidence("Just a moment...", _CF_PAGE) == ""

    def test_挑战页里也有站点文案时_仍不成立(self):
        """两句都在场时**以边缘模板为准**：分不清是不是站点自己发的。"""
        assert block_side_evidence("Just a moment...", _CF_PAGE + _SITE_REGION) == ""

    def test_404页_不成立(self):
        """404 页没说「这个地区不给看」——那是 ``broken`` 的形态。"""
        assert block_side_evidence("404 Not Found", "页面不存在") == ""

    def test_空正文_不成立(self):
        assert block_side_evidence("视频详情", "") == ""


class Test判据单点:
    def test_d5与感知层是同一个集合(self):
        """``d5`` 只**再导出**，不定义第二份（改一处两处都在动）。"""
        assert (
            d5_scorer.SITE_SELF_REPORT_KINDS
            is rule_perceptor.SITE_SELF_REPORT_KINDS
        )

    def test_d5的检查5读同一个函数(self):
        """检查 5 的「成立」分支必须来自 ``block_side_evidence``——
        只认边缘模板（第一半）不构成立，词表永远不全。"""
        text = "抱歉，当前地区不支持播放"
        assert block_side_evidence("视频详情", text) != ""
        assert rule_perceptor.edge_challenge_evidence("视频详情", text) == ""
