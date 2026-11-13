"""训练态观察的**精炼分支**（方案 25）。

精炼 = 「让模型选该保留的行」，而文本**由渲染层从 P1 原文重建**。
这一组盯的是四条：

1. **只可能漏，不可能造**：重建出来的每一行都逐字来自原文；
2. **对不上就回退**：保留的行不在原文里 ⇒ 整条作废，走规则裁剪
   （**与没精炼时逐字一致**——零回归）；
3. **留痕**：``body_refined`` / ``body_refined_lines`` /
   ``body_refined_dropped`` 三个字段必须如实报；
4. **两段投影同款**：P3 与 T 的精简标记**逐字一致**（O-5）。
"""

from __future__ import annotations

from trajectory_pipeline.assembler import observation_view, task_view, views
from trajectory_pipeline.common import refine_sidecar


def _obs(**kw):
    base = {
        "url": "https://x.test",
        "page_title": "测试页",
        "body_text": "",
        "degraded": [],
        "interactive_elements": [],
        "links": [],
        "video_tag_count": 0,
        "iframe_count": 0,
    }
    base.update(kw)
    return base


_BODY = "\n".join([
    "《让子弹飞》高清在线观看",
    "主演：姜文 周润发",
    "无关第一行内容",
    "无关第二行内容",
    "无关第三行内容",
])


def _index(body: str, kept: list[str]) -> dict[str, list[str]]:
    return {refine_sidecar.body_key(body): kept}


class Test精炼重建:
    def test_按选中的行重建(self):
        out = observation_view.render(
            _obs(body_text=_BODY),
            focus=("让子弹飞",),
            refined=_index(_BODY, ["主演：姜文 周润发"]),
        )
        assert out["body_text"] == "主演：姜文 周润发"
        assert out["body_refined"] is True

    def test_重建出来的每一行都逐字来自原文(self):
        out = observation_view.render(
            _obs(body_text=_BODY),
            refined=_index(_BODY, ["无关第一行内容"]),
        )
        for line in out["body_text"].splitlines():
            assert line in _BODY.splitlines()

    def test_按页面顺序重建(self):
        """模型可能乱序报行号；「从上到下」这个结构信号不能丢。"""
        out = observation_view.render(
            _obs(body_text=_BODY),
            refined=_index(_BODY, ["无关第三行内容", "主演：姜文 周润发"]),
        )
        assert out["body_text"] == "主演：姜文 周润发\n无关第三行内容"

    def test_对不上原文就回退规则裁剪(self):
        """旁挂与正文不同源（改了 P1？换了正文？）⇒ 整条作废。"""
        plain = observation_view.render(_obs(body_text=_BODY))
        out = observation_view.render(
            _obs(body_text=_BODY),
            refined=_index(_BODY, ["原文里没有的这一行"]),
        )
        assert out["body_text"] == plain["body_text"]
        assert out["body_refined"] is False

    def test_没有精炼时逐字不变(self):
        """**零回归**：不给精炼索引时，输出与改造前一模一样。"""
        out = observation_view.render(_obs(body_text=_BODY))
        assert out["body_refined"] is False
        assert out["body_refined_lines"] == 0
        assert out["body_refined_dropped"] == 0
        assert out["body_text"] == "\n".join([
            "《让子弹飞》高清在线观看",
            "主演：姜文 周润发",
            "无关第一行内容",
            "无关第二行内容",
            "无关第三行内容",
        ])

    def test_动作目标照旧无条件保留(self):
        """精炼**不放松**保真口径：动作目标的行仍必须留下。

        模型选的行里没有目标所在行时（正常流程下精炼层会拦掉），
        渲染层也要**自己把目标行补回来**——它不能因为换了裁法就
        放松第 6 条。
        """
        out = observation_view.render(
            _obs(body_text=_BODY),
            keep=("主演：姜文 周润发",),
            refined=_index(_BODY, ["无关第一行内容"]),
        )
        assert "主演：姜文 周润发" in out["body_text"]

    def test_动作目标被模型选中时只留一份(self):
        out = observation_view.render(
            _obs(body_text=_BODY),
            keep=("主演：姜文 周润发",),
            refined=_index(_BODY, ["主演：姜文 周润发"]),
        )
        assert out["body_text"] == "主演：姜文 周润发"


class Test精炼只改正文:
    """精炼分支**只许改正文相关的字段**——其余逐字不动。

    为什么盯这条
    ------------
    精炼是「换一种裁法」，不是「重写这条观察」。改了裁法却顺手动了
    元素表、链接表、降级标记、截断标记……每一处都是**悄悄**的：
    报表上看不出任何异常，而消费方拿到的是一份「部分字段是新口径」的
    观察——比全旧或全新都难查。

    真实批次实测（2026-11-13，``T001__67a05faa``，7 条观察）：
    走精炼的 5 条里，**非正文字段漂移 0 处**。
    """

    #: 允许变的字段——正文本身 + 它的两个账。
    MUTABLE = {
        "body_text", "body_chars", "body_refined",
        "body_refined_lines", "body_refined_dropped",
    }

    def test_除正文外逐字不动(self):
        obs = _obs(
            body_text=_BODY,
            interactive_elements=[{"role": "button", "name": "立即播放"}],
            links=[{"text": "正片", "href": "https://x.test/play"}],
            degraded=["links_empty"],
            truncated={"source": True, "view": False},
            video_tag_count=2,
        )
        plain = observation_view.render(obs, focus=("让子弹飞",))
        refined = observation_view.render(
            obs, focus=("让子弹飞",),
            refined=_index(_BODY, ["主演：姜文 周润发"]),
        )
        assert refined["body_refined"] is True
        for key in set(plain) | set(refined):
            if key in self.MUTABLE:
                continue
            if key == "limits":
                for lk in set(plain[key]) | set(refined[key]):
                    if lk == "body_chars":       # 正文长度变了 ⇒ 这笔账跟着变
                        continue
                    assert plain[key].get(lk) == refined[key].get(lk), (
                        f"limits.{lk} 被精炼动了："
                        f"{plain[key].get(lk)!r} -> {refined[key].get(lk)!r}"
                    )
                continue
            assert plain.get(key) == refined.get(key), (
                f"{key} 被精炼动了：{plain.get(key)!r} -> {refined.get(key)!r}"
            )

    def test_元素表与链接表完全不动(self):
        """元素/链接是**另一套裁法**（按相关度取前 N），与正文裁法无关——
        精炼**不许**牵连它们。"""
        obs = _obs(
            body_text=_BODY,
            interactive_elements=[
                {"role": "button", "name": "立即播放"},
                {"role": "link", "name": "相关推荐"},
            ],
            links=[{"text": "正片", "href": "https://x.test/play"}],
        )
        plain = observation_view.render(obs)
        refined = observation_view.render(
            obs, refined=_index(_BODY, ["主演：姜文 周润发"]),
        )
        assert plain["interactive_elements"] == refined["interactive_elements"]
        assert plain["links"] == refined["links"]

    def test_降级与截断标记不动(self):
        """``degraded``（信息没采到）与 ``truncated``（证据不全）是
        **另一笔账**——精炼不碰它们。"""
        obs = _obs(body_text=_BODY, degraded=["links_empty"])
        plain = observation_view.render(obs)
        refined = observation_view.render(
            obs, refined=_index(_BODY, ["主演：姜文 周润发"]),
        )
        assert plain["degraded"] == refined["degraded"]
        assert plain["truncated"] == refined["truncated"]


class Test精炼留痕:
    def test_三个字段如实报(self):
        out = observation_view.render(
            _obs(body_text=_BODY),
            refined=_index(_BODY, ["主演：姜文 周润发"]),
        )
        assert out["body_refined"] is True
        assert out["body_refined_lines"] == 1
        # 候选行 5 行，最终留下 1 行 ⇒ 丢掉 4 行
        assert out["body_refined_dropped"] == 4

    def test_留痕不进limits也不进truncated(self):
        """``limits`` 是 7 键契约、``truncated`` 是 2 键契约——
        加第三个键就破了别人的语义（``gates._truncated_flag``）。"""
        out = observation_view.render(
            _obs(body_text=_BODY),
            refined=_index(_BODY, ["主演：姜文 周润发"]),
        )
        assert set(out["limits"]) == {
            "body_chars", "elements", "links",
            "body_lines_dropped", "elements_dropped", "links_dropped",
            "cropped",
        }
        assert set(out["truncated"]) == {"source", "view"}

    def test_噪声丢弃与精炼丢弃是两笔账(self):
        body = _BODY + "\n首页\n京ICP备12345号"
        out = observation_view.render(
            _obs(body_text=body),
            refined=_index(body, ["主演：姜文 周润发"]),
        )
        assert out["limits"]["body_lines_dropped"] == 2
        assert out["body_refined_dropped"] == 4

    def test_空观察也有这三个字段(self):
        out = observation_view.render(None)
        assert out["body_refined"] is False
        assert out["body_refined_lines"] == 0
        assert out["body_refined_dropped"] == 0


class Test两段投影同款:
    def test_P3带精简标记(self):
        obs = observation_view.render(
            _obs(body_text=_BODY),
            refined=_index(_BODY, ["主演：姜文 周润发"]),
        )
        text = views.render_observation(obs)
        assert "[内容已精简: 保留 1 行]" in text

    def test_T带精简标记(self):
        obs = observation_view.render(
            _obs(body_text=_BODY),
            refined=_index(_BODY, ["主演：姜文 周润发"]),
        )
        text = task_view._render_observation_text(obs)
        assert "[内容已精简: 保留 1 行]" in text

    def test_P3与T的精炼回执逐字同款(self):
        """两段投影刻意不互相 import，字面一致靠这条盯着（O-5）。"""
        obs = observation_view.render(
            _obs(body_text=_BODY),
            refined=_index(_BODY, ["主演：姜文 周润发"]),
        )
        assert task_view._render_observation_text(obs) == views.render_observation(obs)

    def test_没精炼时不带标记(self):
        obs = observation_view.render(_obs(body_text=_BODY))
        assert "内容已精简" not in views.render_observation(obs)
        assert "内容已精简" not in task_view._render_observation_text(obs)
