"""观察限量（方案 23 R1）——噪声剔除、任务相关摘要、动作目标保真。

这一组盯的是**训练侧文本**的四条口径（P1 存档一个字不动）：

1. 导航噪声（热搜榜 / 备案号 / 页脚 / 广告 / 站内菜单）不进正文；
2. 任务相关行（片名 / 播放关键词）优先占正文预算；
3. 「限量」不许把**下一步动作的目标**裁掉——否则训练样本里会出现
   「点了一个观察里没有的东西」，那是在教模型编目标；
4. 「本步动作无效」与「采集降级」分列——前者是动作白做，后者是
   这块信息没采到。混成一个标记，模型会把点击失败学成「页面没内容」。
"""

from __future__ import annotations

from trajectory_pipeline.assembler import observation_view, task_view, views


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


def _links(n, *, prefix="https://x.test/p"):
    return [{"text": f"条目{i}", "href": f"{prefix}{i}"} for i in range(n)]


def _elems(n, *, role="link"):
    return [{"role": role, "name": f"入口{i}"} for i in range(n)]


class Test正文摘要:
    def test_导航噪声不进正文(self):
        body = "\n".join([
            "首页", "登录", "京ICP备12345号", "Copyright © 2024",
            "《让子弹飞》高清在线观看", "主演：姜文", "1 某条热搜新闻",
        ])
        out = observation_view.render(_obs(body_text=body), focus=("让子弹飞",))
        assert "京ICP备" not in out["body_text"]
        assert "Copyright" not in out["body_text"]
        assert "1 某条热搜" not in out["body_text"]
        assert "高清在线观看" in out["body_text"]
        assert out["limits"]["body_lines_dropped"] >= 3

    def test_任务相关行优先占预算(self):
        noise = "\n".join(f"推荐内容第{i}条" for i in range(200))
        body = noise + "\n《让子弹飞》正片在线观看"
        out = observation_view.render(_obs(body_text=body), focus=("让子弹飞",))
        assert "《让子弹飞》正片在线观看" in out["body_text"]
        assert len(out["body_text"]) <= observation_view.VIEW_BODY_CHARS

    def test_正文触到上限要留痕(self):
        out = observation_view.render(_obs(body_text="字" * 5000))
        assert out["limits"]["body_chars"] == observation_view.VIEW_BODY_CHARS
        assert out["limits"]["cropped"] is True
        assert out["truncated"]["view"] is True
        assert len(out["body_text"]) == observation_view.VIEW_BODY_CHARS

    def test_空正文不编内容(self):
        out = observation_view.render(_obs(body_text=""))
        assert out["body_text"] == ""
        assert out["body_source"] == "missing"


class Test元素限量:
    def test_去噪去重(self):
        elems = [
            {"role": "link", "name": "首页"},
            {"role": "link", "name": "播放"},
            {"role": "link", "name": "播放"},
            {"role": "link", "name": "更多"},
            {"role": "button", "name": "立即播放"},
        ]
        out = observation_view.render(_obs(interactive_elements=elems))
        names = [e["name"] for e in out["interactive_elements"]]
        assert "首页" not in names
        assert names.count("播放") == 1
        assert out["limits"]["elements_dropped"] == 3

    def test_下一步动作的元素目标被保住(self):
        """目标在**限量之外**（第 30 个）也必须出现——它更靠前（keep 优先）。"""
        elems = _elems(30) + [{"role": "button", "name": "播放正片"}]
        out = observation_view.render(_obs(interactive_elements=elems), keep=("播放正片",))
        assert {"role": "button", "name": "播放正片"} in out["interactive_elements"]
        assert len(out["interactive_elements"]) == observation_view.VIEW_ELEMENTS

    def test_导航词里的目标也能保住(self):
        """「登录」是导航噪声词，但下一步动作真要点它——保住。"""
        elems = [{"role": "link", "name": "登录"}]
        out = observation_view.render(_obs(interactive_elements=elems), keep=("登录",))
        assert out["interactive_elements"] == [{"role": "link", "name": "登录"}]


class Test链接限量:
    def test_不可导航与重复项被剔除(self):
        links = [
            {"text": "", "href": "javascript:void(0)"},
            {"text": "首页", "href": "https://x.test/"},
            {"text": "播放", "href": "https://x.test/play"},
            {"text": "播放", "href": "https://x.test/play"},
        ]
        out = observation_view.render(_obs(links=links))
        hrefs = [l["href"] for l in out["links"]]
        assert "javascript:void(0)" not in hrefs
        assert hrefs.count("https://x.test/play") == 1
        assert out["limits"]["links_dropped"] == 3

    def test_下一步动作的链接目标被保住(self):
        links = _links(30) + [{"text": "正片", "href": "https://x.test/play"}]
        out = observation_view.render(_obs(links=links), keep=("https://x.test/play",))
        assert {"text": "正片", "href": "https://x.test/play"} in out["links"]
        assert len(out["links"]) == observation_view.VIEW_LINKS

    def test_保住的链接排在最前(self):
        links = _links(30) + [{"text": "正片", "href": "https://x.test/play"}]
        out = observation_view.render(_obs(links=links), keep=("https://x.test/play",))
        assert out["links"][0]["href"] == "https://x.test/play"


class Test动作无效标记:
    def test_与采集降级分列(self):
        out = observation_view.render(
            _obs(degraded=["links_empty"]), action_error="click 失败: 元素不在页面上",
        )
        assert out["degraded"] == ["links_empty"]
        assert out["action_error"] == "click 失败: 元素不在页面上"

    def test_P3回执带标记(self):
        text = views.render_observation(
            _obs(action_error="click 失败: 元素不在页面上")
        )
        assert "[本步动作无效: click 失败" in text
        assert "[采集降级" not in text

    def test_T回执带标记(self):
        text = task_view._render_observation(
            _obs(), action_error="press 失败: 按键不支持"
        )
        assert "[本步动作无效: press 失败" in text

    def test_空观察也带标记(self):
        out = observation_view.render(None, action_error="goto 失败: 导航超时")
        assert out["action_error"] == "goto 失败: 导航超时"
        assert "observation_missing" in out["degraded"]


class Test读步:
    """R4-1：读步的观察**只有被读的那一块**，训练视图的窗口也更宽。

    两条口径必须同时成立：

    1. **只排版被读的块**——读 ``links`` 却把空的正文排成「正文：（空）」，
       模型会以为这一页真没正文（其实只是没读）；
    2. 读窗口比普通步宽（2000/50/50 vs 320/10/4）——否则读原语与普通
       回执没有差别，R1 的压缩会把读的意义抵消掉。
    """

    def test_snapshot读的正文窗口更宽(self):
        out = observation_view.render(
            _obs(read="snapshot", body_text="字" * 5000)
        )
        assert out["read"] == "snapshot"
        assert len(out["body_text"]) == observation_view.VIEW_READ_BODY_CHARS
        assert out["limits"]["body_chars"] == observation_view.VIEW_READ_BODY_CHARS

    def test_links读只留链接块(self):
        out = observation_view.render(
            _obs(read="links", body_text="不该出现", interactive_elements=_elems(3),
                 links=_links(80)),
            keep=("https://x.test/p0",),
        )
        assert out["read"] == "links"
        assert out["body_text"] == ""
        assert out["interactive_elements"] == []
        assert len(out["links"]) == observation_view.VIEW_READ_LINKS

    def test_interactive读只留元素块(self):
        out = observation_view.render(
            _obs(read="interactive", body_text="不该出现",
                 interactive_elements=_elems(80), links=_links(3))
        )
        assert out["read"] == "interactive"
        assert out["body_text"] == ""
        assert out["links"] == []
        assert len(out["interactive_elements"]) == observation_view.VIEW_READ_ELEMENTS

    def test_P3读回执带读标记且不排counts(self):
        text = views.render_observation(
            _obs(read="links", links=_links(2))
        )
        assert "[读取: 链接]" in text
        assert "→ https://x.test/p0" in text
        assert "video 标签" not in text  # counts 属于整页回执

    def test_T与P3的读回执逐字同款(self):
        """两段投影刻意不互相 import，字面一致靠这条盯着（O-5）。"""
        obs = _obs(read="interactive", interactive_elements=_elems(2))
        assert task_view._render_observation(obs) == views.render_observation(obs)


class Test批量对齐:
    def test_focus与keep按下标对齐(self):
        obs = [
            _obs(body_text="无关内容一\n《甲》在线观看"),
            _obs(body_text="无关内容二\n《乙》在线观看"),
        ]
        out = observation_view.render_all(
            obs,
            focuses=[("甲",), ("乙",)],
            keeps=[(), ("https://x.test/play",)],
            action_errors=["", "click 失败"],
        )
        assert "《甲》" in out[0]["body_text"]
        assert "《乙》" not in out[0]["body_text"]
        assert out[0]["body_text"] == "无关内容一\n《甲》在线观看"
        assert out[1]["action_error"] == "click 失败"