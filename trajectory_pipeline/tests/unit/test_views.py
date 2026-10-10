"""``assembler.views`` 的单元测试——P3 三视图的渲染口径。

这个模块的价值全在**六条设计判断**上（见 ``views.py`` 模块 docstring）。
它们逐条都是「看起来可以随便选、选错了不报错、只是训练数据悄悄变差」，
所以每条配至少一条断言：

1. 观察是 ``role: tool`` + ``tool_call_id``，不是 ``role: user``
2. ``new_tab`` 不进 tools 声明，但仍在动作流里
3. ``click`` 的 ``target`` 平铺进 ``arguments``
4. 空 ``user_prompt`` 不发空 user 消息
5. 降级样本照常渲染，``meta`` 必须带 ``degraded_from``
6. ``rationale`` 缺失时 assistant 无思考段，meta 记 ``rationale_missing``

外加一条**产物根**断言——那条曾经真实发生过：``parents[2]`` 写成
``parents[1]`` 的兄弟（差一级），落点从 ``trajectory_pipeline/output/``
挪到**仓库根** ``output/``（CLAUDE.md 明令禁止的存量目录），
而且全程不报错，只是读不到数据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.assembler import views


def _obs(url="https://x.test", **kw):
    base = {"url": url, "page_title": "页面标题", "body_text": "正文内容",
            "body_source": "body_text", "body_chars": 4,
            "truncated": {"source": False, "view": False}, "degraded": [],
            "interactive_elements": [{"tag": "button", "label": "立即播放"}],
            "links": [{"text": "首页", "href": "https://x.test/"}],
            "video_tag_count": 1, "iframe_count": 0,
            "limits": {"body_chars": None, "elements": None, "links": None,
                       "cropped": False}}
    base.update(kw)
    return base


def _sample(**over):
    six = {
        "system_prompt": "系统提示词",
        "tools": [
            {"name": "goto", "params": ["url"], "instruction": "打开网址"},
            {"name": "click", "params": [], "instruction": "点击元素"},
            {"name": "new_tab", "params": ["url"], "instruction": "开新标签"},
        ],
        "user_prompt": "有没有能看正片的",
        "provenance": {"genre": "喜剧"},
        "actions": [{"tool": "goto", "params": {"url": "https://x.test"},
                     "target": None, "origin": "model"}],
        "observations": [_obs()],
        "rationale": None,
    }
    base = {
        "sample_id": "T001__abc123__site-1",
        "task_id": "T001", "unit": "site-1", "title": "功夫",
        "source_archive": "T001__abc123.json",
        "six": six,
        "gate": {"status": "not_run", "reasons": ["rationale 模块未落地"]},
        "rationale_missing": True,
        "outcome": {"url": "https://x.test", "branch": "no_play_control",
                    "source": "rule", "fallback_used": False, "succeeded": False},
        "degraded_from": ["run_config"],
    }
    for k, v in over.items():
        if k == "six":
            six.update(v)
        else:
            base[k] = v
    return base


# ── 判断 1：观察是 role:tool ──────────────────────────────────────


class Test观察是工具回执:
    def test_openai视图观察是tool角色(self):
        msgs = views.to_openai_messages(_sample())
        roles = [m["role"] for m in msgs]
        assert "tool" in roles
        assert roles == ["system", "user", "assistant", "tool"]

    def test_tool消息配对tool_call_id(self):
        """关联是 agent 轨迹的训练信号，丢了模型就学不会
        「这个观察是对那个动作的回应」。"""
        msgs = views.to_openai_messages(_sample())
        assistant = next(m for m in msgs if m["role"] == "assistant")
        tool = next(m for m in msgs if m["role"] == "tool")
        assert tool["tool_call_id"] == assistant["tool_calls"][0]["id"]

    def test_tool_call_id按下标派生(self):
        """从 step 序号算出来的，不是从 P1 找的——
        能从数据确定的就不另找来源（找不到就编一个？那才是坑）。"""
        s = _sample(six={
            "actions": [
                {"tool": "goto", "params": {"url": "a"}, "target": None, "origin": "model"},
                {"tool": "click", "params": {}, "target": {"tag": "b", "label": "播放"},
                 "origin": "model"},
            ],
            "observations": [_obs(), _obs("https://y.test")],
        })
        msgs = views.to_openai_messages(s)
        ids = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
        assert ids == ["call_0", "call_1"]

    def test_messages视图观察也带关联(self):
        """通用视图降级成纯文本，但**不能丢掉观察本身**——
        丢掉它等于这条样本只剩一个动作，模型无从学习反馈闭环。"""
        msgs = views.to_messages(_sample())
        assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]

    def test_动作与观察交替(self):
        s = _sample(six={
            "actions": [
                {"tool": "goto", "params": {"url": "a"}, "target": None, "origin": "model"},
                {"tool": "click", "params": {}, "target": {"tag": "b", "label": "播放"},
                 "origin": "model"},
            ],
            "observations": [_obs(), _obs("https://y.test")],
        })
        msgs = views.to_openai_messages(s)
        # system, user, assistant, tool, assistant, tool
        assert [m["role"] for m in msgs] == \
            ["system", "user", "assistant", "tool", "assistant", "tool"]


# ── 判断 2：new_tab 的可见性 ─────────────────────────────────────


class Test基础设施动作:
    def test_new_tab不进工具声明(self):
        """模型永远不该输出它——但它是真实发生过的动作，
        删掉就无法回放。**可见性由 ToolSpec 唯一决定**。"""
        payload = {"six": {"tools": _sample()["six"]["tools"]}}
        names = [t["function"]["name"] for t in views._visible_tools(payload)]
        assert names == ["goto", "click"]
        assert "new_tab" not in names

    def test_new_tab仍在动作流里(self):
        """保留是为了可回放，不是为了让模型学它。"""
        s = _sample(six={
            "actions": [{"tool": "new_tab", "params": {"url": "https://x.test"},
                         "target": None, "origin": "infrastructure"}],
        })
        msgs = views.to_openai_messages(s)
        call = next(m for m in msgs if m["role"] == "assistant")
        assert call["tool_calls"][0]["function"]["name"] == "new_tab"

    def test_meta记下基础设施动作(self):
        """人读 meta 就知道这条轨迹里混了执行器自己的动作，
        复核时不会误判成「模型输出了 new_tab」。"""
        s = _sample(six={
            "actions": [
                {"tool": "goto", "params": {"url": "a"}, "target": None, "origin": "model"},
                {"tool": "new_tab", "params": {"url": "b"}, "target": None,
                 "origin": "infrastructure"},
            ],
            "observations": [_obs()],
        })
        assert views.to_meta(s)["infrastructure_actions"] == ["new_tab"]


# ── 判断 3：click 的 target 平铺 ──────────────────────────────────


class Test点击目标:
    def test_target平铺进arguments(self):
        """嵌套 target 会训出「往 arguments.target.label 里填」，
        而真实 API 要的是平铺。"""
        s = _sample(six={
            "actions": [{"tool": "click", "params": {},
                         "target": {"tag": "button", "label": "立即播放"},
                         "origin": "model"}],
            "observations": [_obs()],
        })
        msgs = views.to_openai_messages(s)
        args = msgs[2]["tool_calls"][0]["function"]["arguments"]
        assert args == {"tag": "button", "label": "立即播放"}

    def test_params与target合并(self):
        """goto 的 url 在 params、click 的目标在 target，
        键空间不冲突——两处都要能落到 arguments。"""
        s = _sample(six={
            "actions": [{"tool": "click", "params": {"page": "1"},
                         "target": {"tag": "a", "label": "下一页"},
                         "origin": "model"}],
            "observations": [_obs()],
        })
        args = views.to_openai_messages(s)[2]["tool_calls"][0]["function"]["arguments"]
        assert args == {"page": "1", "tag": "a", "label": "下一页"}

    def test_空target不产生空键(self):
        """``label=""`` 是真实存在的元素（P1 里有大量空 label 的
        ``<a>``），但渲染成 ``label: ""`` 会让模型学会发空串。"""
        s = _sample(six={
            "actions": [{"tool": "click", "params": {},
                         "target": {"tag": "a", "label": ""}, "origin": "model"}],
            "observations": [_obs()],
        })
        args = views.to_openai_messages(s)[2]["tool_calls"][0]["function"]["arguments"]
        assert "label" not in args
        assert args == {"tag": "a"}

    def test_actions里没有ref(self):
        """ref 是会话句柄（见 ``executor/actions.py``）——
        换 session 就失效，进训练数据等于让模型学随机数。"""
        s = _sample(six={
            "actions": [{"tool": "click", "params": {},
                         "target": {"tag": "a", "label": "播"}, "origin": "model"}],
        })
        blob = json.dumps(views.to_openai_messages(s), ensure_ascii=False)
        assert '"ref"' not in blob


# ── 判断 4：空 user_prompt ────────────────────────────────────────


class Test用户轮次:
    def test_有user_prompt才发user消息(self):
        msgs = views.to_openai_messages(_sample())
        assert msgs[1] == {"role": "user", "content": "有没有能看正片的"}

    def test_空user_prompt不发空消息(self):
        """发一条 ``content: ""`` 的 user 消息会教模型
        「用户可以什么都不说」——而用户开口就是一句诉求，
        手工路径下它是**没采到**。"""
        msgs = views.to_openai_messages(_sample(six={"user_prompt": ""}))
        assert [m["role"] for m in msgs] == ["system", "assistant", "tool"]

    def test_缺user_prompt同样不发(self):
        msgs = views.to_openai_messages(_sample(six={"user_prompt": None}))
        assert [m["role"] for m in msgs] == ["system", "assistant", "tool"]


# ── 判断 5 + 6：meta 的可用性标记 ─────────────────────────────────


class TestMeta:
    def test_降级标记带进meta(self):
        """降级样本照常渲染（因为降级就丢等于扔掉一半数据），
        但 meta 必须显眼（因为静默渲染等于缺件不可见）。"""
        m = views.to_meta(_sample(degraded_from=["run_config", "steps"]))
        assert m["degraded_from"] == ["run_config", "steps"]

    def test_rationale缺失有标记(self):
        """「流水线没长出来」与「模型这轮没说话」是两回事。"""
        assert views.to_meta(_sample())["rationale_missing"] is True

    def test_闸门状态带进meta(self):
        """not_run 不是 passed——渲染成「通过」会让批次上线时
        看起来像过了闸。"""
        assert views.to_meta(_sample())["gate"]["status"] == "not_run"

    def test_来源可反查(self):
        """从 meta 就能定位到 P1，不必开文件。"""
        m = views.to_meta(_sample())
        assert m["source_archive"] == "T001__abc123.json"
        assert m["task_id"] == "T001"
        assert m["unit"] == "site-1"

    def test_outcome带进meta(self):
        m = views.to_meta(_sample())
        assert m["outcome"]["branch"] == "no_play_control"

    def test_成对性自查(self):
        """动作多于观察 = 末步没取到观察。真实轨迹里会发生
        （点了点不动），不修但要看得见。"""
        s = _sample(six={
            "actions": [
                {"tool": "goto", "params": {"url": "a"}, "target": None, "origin": "model"},
                {"tool": "click", "params": {}, "target": {"tag": "b", "label": "c"},
                 "origin": "model"},
            ],
            "observations": [_obs()],
        })
        m = views.to_meta(s)
        assert m["step_count"] == 2
        assert m["observation_count"] == 1
        assert m["observation_complete"] is False


# ── 观察渲染：留痕 ────────────────────────────────────────────────


class Test观察渲染:
    def test_空观察不渲染成空串(self):
        """``content: ""`` 会被当成「页面真的什么都没有」，
        而空有三种成因（没采到 / 被拦 / 真的空），空串抹平它们。"""
        out = views.render_observation(None)
        assert out
        assert "没有取到" in out

    def test_采集降级如实报出(self):
        out = views.render_observation(_obs(degraded=["links_empty"]))
        assert "[采集降级: links_empty]" in out

    def test_上游截断与本次裁剪分列(self):
        out = views.render_observation(
            _obs(truncated={"source": True, "view": True}))
        assert "[内容不完整: 上游截断、本次裁剪]" in out

    def test_没降级时不谎报(self):
        out = views.render_observation(_obs())
        assert "采集降级" not in out
        assert "内容不完整" not in out

    def test_空正文明写空(self):
        """正文为空与「没有正文这个键」不同；渲染成
        「正文：（空）」让模型看到这是个事实而不是渲染失败。"""
        out = views.render_observation(_obs(body_text=""))
        assert "正文：（空）" in out

    def test_元素标签都在(self):
        out = views.render_observation(_obs())
        assert "立即播放" in out
        assert "<button>" in out

    def test_媒体计数如实报(self):
        out = views.render_observation(_obs(video_tag_count=1, iframe_count=2))
        assert "video 标签 1 个" in out
        assert "iframe 2 个" in out


# ── 落盘 ──────────────────────────────────────────────────────────


class Test落盘:
    def test_三份视图都写(self, tmp_path):
        res = views.build_views(_sample(), root=tmp_path, overwrite=True)
        assert res.written == 3
        out = views.views_dir(tmp_path)
        assert sorted(p.name for p in out.glob("*.json")) == [
            "T001__abc123__site-1_messages.json",
            "T001__abc123__site-1_meta.json",
            "T001__abc123__site-1_openai.json",
        ]

    def test_每份都是合法JSON(self, tmp_path):
        views.build_views(_sample(), root=tmp_path, overwrite=True)
        for p in views.views_dir(tmp_path).glob("*.json"):
            json.loads(p.read_text(encoding="utf-8"))

    def test_默认不覆盖(self, tmp_path):
        views.build_views(_sample(), root=tmp_path, overwrite=True)
        res = views.build_views(_sample(), root=tmp_path)
        assert res.written == 0
        assert len(res.skipped) == 3

    def test_缺sample_id不写(self, tmp_path):
        """没 id 连文件名都起不出来——报出来而不是写个 ``.json``。"""
        res = views.build_views({"six": {}}, root=tmp_path, overwrite=True)
        assert res.written == 0
        assert res.skipped

    def test_全目录批处理(self, tmp_path):
        samples = tmp_path / "samples"
        samples.mkdir()
        (samples / "T001__a__search.json").write_text(
            json.dumps(_sample(sample_id="T001__a__search"), ensure_ascii=False),
            encoding="utf-8")
        (samples / "T002__b__site-1.json").write_text(
            json.dumps(_sample(sample_id="T002__b__site-1"), ensure_ascii=False),
            encoding="utf-8")
        rep = views.build_views_from_dir(samples, root=tmp_path, overwrite=True)
        assert rep.total_written == 6
        assert rep.ok is True

    def test_坏样本不打断整批(self, tmp_path):
        samples = tmp_path / "samples"
        samples.mkdir()
        (samples / "T001__a__search.json").write_text("{ 坏", encoding="utf-8")
        (samples / "T002__b__site-1.json").write_text(
            json.dumps(_sample(sample_id="T002__b__site-1"), ensure_ascii=False),
            encoding="utf-8")
        rep = views.build_views_from_dir(samples, root=tmp_path, overwrite=True)
        assert rep.total_written == 3
        assert rep.total_skipped == 1


# ── 产物根：那个不会报错的错 ──────────────────────────────────────


class Test产物根:
    def test_落在trajectory_pipeline下而不是仓库根(self):
        """``parents[2]`` 会落到**仓库根**的 ``output/pipeline/``——
        CLAUDE.md 明令禁止碰的存量产物目录（D8）。而它**不报错**，
        只是读不到数据、看起来像「这批没样本」。

        路径形状 ``<包根>/output/pipeline/views``，所以从 ``views``
        往上数三级才到包根。
        """
        resolved = views.views_dir().resolve()
        assert resolved.name == "views"
        assert resolved.parent.name == "pipeline"
        assert resolved.parent.parent.name == "output"
        assert resolved.parent.parent.parent.name == "trajectory_pipeline", (
            f"产物落到了 {resolved}——仓库根的 output/ 是存量目录，"
            f"新树产物必须落在 trajectory_pipeline/output/（D8）")
