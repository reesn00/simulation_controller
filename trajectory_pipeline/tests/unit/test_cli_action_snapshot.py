"""``cli._after_action_snapshot`` 的口径测试——不联网、不开浏览器。

为什么要单独测一个 CLI 里的小工具函数：**它第一次写错的时候看不出来。**
2026-10-20 的冒烟里，第一版「连续两次 ``(url,title)`` 一致就算读稳」在
真页面上给出了「没跳」的错误结论——因为**旧页在导航开始之前天然稳定**。
第二版又读到过渡态（``title == ''``）。两次都不报错、都看起来合理。

所以这里把三种形状钉住：

1. 旧页稳定但随后跳转 → 必须等到新页，``moved=True``；
2. 跳转后先经过过渡态（``title`` 空）→ 必须等到 ``title`` 读得出来，
   **不许把过渡态当结果返出去**（照那个读数写进批次元数据就是一条假的「无标题页」）；
3. URL 始终不变 → ``moved=False``，照实报告，**不猜**「大概跳了」。
"""

from __future__ import annotations

from collections import namedtuple

import pytest

from trajectory_pipeline.executor.cli import _after_action_snapshot

Snap = namedtuple("Snap", "url title")


class _FakeDriver:
    """按预设序列吐 ``(url, title)``，序列走完之后一直吐最后一个。"""

    def __init__(self, seq: list[tuple[str, str]]) -> None:
        self._seq = list(seq)
        self.reads = 0

    async def snapshot(self, *, max_chars: int) -> Snap:
        idx = min(self.reads, len(self._seq) - 1)
        self.reads += 1
        url, title = self._seq[idx]
        return Snap(url, title)


class TestAfterActionSnapshot:
    async def test_旧页稳定但随后跳转要等到新页(self):
        """**这是第一版写错的形状**：旧页连续两次一致 ≠ 导航没发生。"""
        driver = _FakeDriver([
            ("https://a/", "旧页"),
            ("https://a/", "旧页"),      # 前两读一致，但导航还没开始
            ("https://b/", "新页"),
            ("https://b/", "新页"),
        ])
        obs, reads, moved = await _after_action_snapshot(
            driver, clicked_from="https://a/", attempts=6, delay=0.0
        )
        assert moved is True
        assert obs.url == "https://b/"
        assert obs.title == "新页"
        assert reads >= 3              # 没有在第 2 读就「读稳」返回旧页

    async def test_不把过渡态当结果(self):
        """跳转中途 ``title`` 是空串；照那个读数写进批次就是假的「无标题页」。"""
        driver = _FakeDriver([
            ("https://a/", "旧页"),
            ("https://b/", ""),          # 导航已开始，新文档还没 commit
            ("https://b/", ""),
            ("https://b/", "新页标题"),
            ("https://b/", "新页标题"),
        ])
        obs, _reads, moved = await _after_action_snapshot(
            driver, clicked_from="https://a/", attempts=8, delay=0.0
        )
        assert moved is True
        assert obs.title == "新页标题"

    async def test_url_没变要照实报告不猜(self):
        """点了不导航（按钮/JS）是页面事实，不是故障——但**不能说它跳了**。"""
        driver = _FakeDriver([("https://a/", "同一页")])
        obs, reads, moved = await _after_action_snapshot(
            driver, clicked_from="https://a/", attempts=3, delay=0.0
        )
        assert moved is False
        assert obs.url == "https://a/"
        assert reads == 3              # 用满预算才放弃

    async def test_一直读不到title也要照实返回并让调用方提示(self):
        """「title 非空」只是「新文档 commit 了」的**廉价代理**，不是判据。

        真有页面没有 ``<title>``：这时**不许编标题、不许无限等**——
        用满预算后照实返回空标题，由调用方打出提示（可能与过渡态同形，
        所以提示里两种可能都写着，不替用户下结论）。
        """
        driver = _FakeDriver([("https://a/", ""), ("https://b/", "")])
        obs, reads, moved = await _after_action_snapshot(
            driver, clicked_from="https://a/", attempts=4, delay=0.0
        )
        assert moved is True
        assert obs.url == "https://b/"
        assert obs.title == ""         # 照实，不伪造
        assert reads == 4              # 用满预算才放弃


@pytest.mark.parametrize("clicked_from", ["https://a/"])
async def test_返回三元组(clicked_from):
    """签名是 ``(观察, 读了几次, 变没变)``——次数要打给用户看，不是内部细节。"""
    driver = _FakeDriver([("https://a/", "x")])
    out = await _after_action_snapshot(driver, clicked_from=clicked_from, attempts=2, delay=0.0)
    assert len(out) == 3