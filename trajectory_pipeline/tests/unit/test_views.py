"""``assembler.views`` 的单元测试——P3 三视图的渲染口径。

这个模块的价值全在**九条设计判断**上（见 ``views.py`` 模块 docstring）。
它们逐条都是「看起来可以随便选、选错了不报错、只是训练数据悄悄变差」，
所以每条配至少一条断言：

1. 观察是 ``role: tool`` + ``tool_call_id``，不是 ``role: user``
2. ``new_tab`` 不进 tools 声明，**整步（动作+观察+思考）不进消息流**
3. ``click`` 的 ``target`` 平铺进 ``arguments``
4. 空 ``user_prompt`` 不发空 user 消息
5. 降级样本照常渲染，``meta`` 必须带 ``degraded_from``
6. ``rationale`` 缺失时 assistant 无思考段，meta 记 ``rationale_missing``
7. 每条轨迹以面向用户的答复收尾（无 ``tool_calls`` 的 assistant 消息）
8. 缺观察的步显式发占位回执，不留悬空 ``tool_calls``
9. 死循环步（相邻同动作 + 观察逐字相同）剔出训练视图；重复动作在 meta 标记

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


def _site_outcome(outcome, *, index=1, step_start=0, step_end=1):
    """一条站点级明细——形状同 :meth:`SiteOutcomeView.to_json`。

    ⚠️ **2026-11-03 · re-base 到 per-Run**：per-Run 之后收尾是**整条轨迹**
    的，站点级明细要参与收尾的生成 ⇒ 夹具里必须带 ``step_start`` /
    ``step_end``（重复判定的边界靠它）。
    """
    return {
        "index": index,
        "url": "https://x.test",
        "landed_url": "https://x.test",
        "outcome": outcome,
        "matched": outcome is not None,
        "is_success": outcome is not None and outcome.get("branch") is None,
        "step_start": step_start,
        "step_end": step_end,
        "outcome_index": 0,
    }


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
        # ⚠️ **2026-10-30 · 方案 23**：一个任务一条轨迹——``sample_id``
        # 不再带单元后缀，``unit`` 字段整个消失。
        "sample_id": "T001__abc123",
        "task_id": "T001", "title": "功夫",
        "source_archive": "T001__abc123.json",
        # 交付面（方案 23 R3-3）：P3 按 tool_surface 分池落盘，夹具走交付面
        "tool_surface": "camofox-1",
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
    # 站点级明细**跟着任务级结论走**（真实样本里两者同源）：不一致会造出
    # 一条「任务成功但站点全失败」的假样本，收尾文本会自相矛盾。
    if "site_outcomes" not in over:
        # 站点区间**覆盖全部动作步**——夹具里没有独立的「搜索段」，
        # 而重复判定的边界就是 unit（方案 23 R3-1/R3-2）：边界落在
        # site-1 里，这些步才同属一个单元。
        base["site_outcomes"] = [
            _site_outcome(dict(base["outcome"]), step_end=len(six["actions"]))
        ]
    return base


# ── 判断 1：观察是 role:tool ──────────────────────────────────────


class Test观察是工具回执:
    def test_openai视图观察是tool角色(self):
        msgs = views.to_openai_messages(_sample())
        roles = [m["role"] for m in msgs]
        assert "tool" in roles
        # 末尾的 assistant 是「任务总结」收尾（判断 7）。
        assert roles == ["system", "user", "assistant", "tool", "assistant"]

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
        assert [m["role"] for m in msgs] == \
            ["system", "user", "assistant", "user", "assistant"]

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
        # system, user, (assistant, tool) × 2, 收尾 assistant
        assert [m["role"] for m in msgs] == \
            ["system", "user", "assistant", "tool", "assistant", "tool", "assistant"]


# ── 判断 2：new_tab 的可见性 ─────────────────────────────────────


class Test基础设施动作:
    def test_new_tab不进工具声明(self):
        """模型永远不该输出它——但它是真实发生过的动作，
        删掉就无法回放。**可见性由 ToolSpec 唯一决定**。"""
        payload = {"six": {"tools": _sample()["six"]["tools"]}}
        names = [t["function"]["name"] for t in views._visible_tools(payload)]
        assert names == ["goto", "click"]
        assert "new_tab" not in names

    def test_new_tab整步不进消息流(self):
        """2026-10-15 拍板反转：训练视图的任务是教模型**它该做的动作**，
        让模型看到一条它永远不会发、也不该发的 assistant ``tool_calls``，
        就是教它输出越权动作。可回放的完整动作流仍在 P2 的
        ``six.actions`` 里，消息视图整步跳过（动作连同它的观察与思考）。"""
        s = _sample(six={
            "actions": [
                {"tool": "goto", "params": {"url": "a"}, "target": None, "origin": "model"},
                {"tool": "new_tab", "params": {"url": "b"}, "target": None,
                 "origin": "infrastructure"},
                {"tool": "click", "params": {}, "target": {"tag": "b", "label": "播放"},
                 "origin": "model"},
            ],
            "observations": [_obs("https://a.test"),
                             {"degraded": ["observation_missing"]},
                             _obs("https://a.test/play")],
        })
        msgs = views.to_openai_messages(s)
        names = [m["tool_calls"][0]["function"]["name"]
                 for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
        assert names == ["goto", "click"]
        # tool_call_id 按可见步稠密派生——跳过的步不占号
        ids = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
        assert ids == ["call_0", "call_1"]
        # click 配到的是**它自己那一位**的观察（播放页），不是 new_tab 的占位
        click回执 = msgs[[m["role"] for m in msgs].index("tool", 4)]
        assert "https://a.test/play" in click回执["content"]

    def test_infra步的观察与思考一起跳过(self):
        """原地留着只会配到别的动作上——那正是 E005 错位的形态。"""
        s = _sample(six={
            "actions": [
                {"tool": "goto", "params": {"url": "a"}, "target": None, "origin": "model"},
                {"tool": "new_tab", "params": {"url": "b"}, "target": None,
                 "origin": "infrastructure"},
                {"tool": "click", "params": {}, "target": {"tag": "b", "label": "播放"},
                 "origin": "model"},
            ],
            "observations": [_obs("https://a.test"),
                             {"degraded": ["observation_missing"]},
                             _obs("https://a.test/play")],
            "rationales": [
                {"step_index": 0, "text": "先打开站点页", "conclusion": "",
                 "status": "written", "reason": ""},
                {"step_index": 1, "text": "执行器自己开的标签页", "conclusion": "",
                 "status": "written", "reason": ""},
                {"step_index": 2, "text": "看到播放按钮，点它", "conclusion": "",
                 "status": "written", "reason": ""},
            ],
        })
        msgs = views.to_openai_messages(s)
        blob = json.dumps(msgs, ensure_ascii=False)
        assert "执行器自己开的标签页" not in blob
        assistants = [m for m in msgs if m["role"] == "assistant"]
        assert "先打开站点页" in (assistants[0]["content"] or "")
        assert "看到播放按钮，点它" in (assistants[1]["content"] or "")

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


# ── 判断 9：死循环与重复动作（方案 23 R3-1 / R3-2）───────────────


def _click(name="立即播放"):
    return {"tool": "click", "params": {},
            "target": {"role": "button", "name": name}, "origin": "model"}


class Test死循环与重复动作:
    def test_openai视图剔掉相邻同动作同观察(self):
        s = _sample(six={
            "actions": [_click(), _click()],
            "observations": [_obs(), _obs()],
        })
        calls = [m for m in views.to_openai_messages(s) if m.get("tool_calls")]
        assert len(calls) == 1
        # 剔一步后序号仍然稠密——训练流里不存在的动作不占号
        assert calls[0]["tool_calls"][0]["id"] == "call_0"

    def test_messages视图同样剔掉(self):
        s = _sample(six={
            "actions": [_click(), _click()],
            "observations": [_obs(), _obs()],
        })
        msgs = views.to_messages(s)
        acts = [m for m in msgs if m["role"] == "assistant"
                and m["content"].startswith("[动作]")]
        assert len(acts) == 1

    def test_观察变了就不是死循环(self):
        """页面刷新 / 滚动加载后是同动作不同观察——必须留着。"""
        s = _sample(six={
            "actions": [_click(), _click()],
            "observations": [_obs(), _obs(body_text="第二屏")],
        })
        calls = [m for m in views.to_openai_messages(s) if m.get("tool_calls")]
        assert len(calls) == 2

    def test_meta逐条记下剔了哪几步(self):
        s = _sample(six={
            "actions": [_click(), _click(), _click()],
            "observations": [_obs(), _obs(), _obs()],
        })
        meta = views.to_meta(s)
        assert meta["deduped_steps"] == [1, 2]
        assert meta["deduped_step_count"] == 2

    def test_meta标记留在训练集里的重复动作(self):
        """A/B/A 不相邻，但不是死循环（观察不同）——只标不剔。"""
        s = _sample(six={
            "actions": [
                _click(),
                {"tool": "press", "params": {"key": "Enter"}, "target": None,
                 "origin": "model"},
                _click(),
            ],
            "observations": [_obs(), _obs(body_text="乙"), _obs(body_text="丙")],
        })
        meta = views.to_meta(s)
        assert meta["deduped_steps"] == []
        assert meta["repeat_actions"] == [{
            "step_index": 2, "unit": "site-1", "tool": "click", "occurrence": 2,
        }]
        assert meta["repeat_step_count"] == 1


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
        args = json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"])
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
        args = json.loads(
            views.to_openai_messages(s)[2]["tool_calls"][0]["function"]["arguments"]
        )
        assert args == {"page": "1", "tag": "a", "label": "下一页"}

    def test_空target不产生空键(self):
        """``label=""`` 是真实存在的元素（P1 里有大量空 label 的
        ``<a>``），但渲染成 ``label: ""`` 会让模型学会发空串。"""
        s = _sample(six={
            "actions": [{"tool": "click", "params": {},
                         "target": {"tag": "a", "label": ""}, "origin": "model"}],
            "observations": [_obs()],
        })
        args = json.loads(
            views.to_openai_messages(s)[2]["tool_calls"][0]["function"]["arguments"]
        )
        assert "label" not in args
        assert args == {"tag": "a"}

    def test_arguments是JSON字符串(self):
        """OpenAI Chat Completions 里 ``function.arguments`` 是**字符串**
        （方案 23 R3-4）。落成 dict 的下游会在 ``json.loads`` 处崩，
        或者更糟——静默按 Python 对象用掉，训出来的模型学会发 dict。"""
        s = _sample(six={
            "actions": [{"tool": "click", "params": {},
                         "target": {"tag": "button", "label": "播放"},
                         "origin": "model"}],
            "observations": [_obs()],
        })
        raw = views.to_openai_messages(s)[2]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(raw, str)
        # 中文不转义（ensure_ascii=False），与真机 token 口径一致
        assert "播放" in raw

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
        assert [m["role"] for m in msgs] == ["system", "assistant", "tool", "assistant"]

    def test_缺user_prompt同样不发(self):
        msgs = views.to_openai_messages(_sample(six={"user_prompt": None}))
        assert [m["role"] for m in msgs] == ["system", "assistant", "tool", "assistant"]


# ── 判断 7：任务总结收尾 ─────────────────────────────────────────


class Test收尾总结:
    """2026-10-15 拍板补入——此前 302/304 条样本全部以动作终结，
    教模型「发完最后一个动作就结束」。

    2026-11-02（方案 23 R2）：收尾从「任务总结：… 处置：…」改成**面向
    用户的答复**——成功给网址，失败给已排除的站点与原因；分支枚举名、
    分支计数一律不进文本（审计 G3）。
    """

    def _closing(self, sample):
        msgs = views.to_openai_messages(sample)
        last = msgs[-1]
        assert last["role"] == "assistant"
        assert "tool_calls" not in last
        return last["content"]

    def test_已知分支给排除原因(self):
        assert self._closing(_sample()) == (
            "没有找到《功夫》的可播放网址，"
            "已排除的站点：x.test（页面上没有播放控件，排除）。"
        )

    def test_成功记播放页地址(self):
        s = _sample(outcome={"url": "https://x.test", "branch": None,
                             "play_page_url": "https://x.test/play",
                             "source": "rule", "fallback_used": False,
                             "succeeded": True})
        assert self._closing(s) == \
            "找到了《功夫》的可播放网址：https://x.test/play。"

    def test_成功缺play_page_url退回落地地址(self):
        s = _sample(outcome={"url": "https://x.test", "branch": None,
                             "succeeded": True})
        assert "https://x.test" in self._closing(s)

    def test_未知分支不写分支名(self):
        """分支枚举名是并池口径，不是助手语言（R2-1）。

        未知分支**不能静默消失**——它退化成「原因未能确认」，分支本身
        仍在 ``outcome`` / ``ledger`` 里可查。
        """
        s = _sample(outcome={"url": "https://x.test", "branch": "future_branch"})
        text = self._closing(s)
        assert "future_branch" not in text
        assert "原因未能确认" in text

    def test_收尾不带分支计数(self):
        """旧收尾的「分支分布：k=v · …」是台账播报，不许再出现。"""
        text = self._closing(_sample())
        assert "分支分布" not in text
        assert "=" not in text

    def test_没有站点结论判不出来(self):
        """⚠️ **2026-10-30 · 方案 23**：样本不再有「搜索单元」这种形态——
        一条样本就是**整条轨迹**，它的结论要么有、要么没有。

        没有结论时**不说「接下来逐一打开候选站点核验」**：那句是搜索阶段
        的过渡语，拿它当轨迹收尾会让人以为「后面还有活要干」，而这条
        轨迹已经跑完了。判不出来就**判不出来**（fail-closed）。"""
        s = _sample(outcome=None, site_outcomes=[])
        assert self._closing(s) == "没有找到《功夫》的可访问站点。"

    def test_站点结论缺失如实说原因未能确认(self):
        """访问过站点但没配到 outcome = **缺件**，不是成功。

        **不编原因**，也不拿搜索话术顶替——缺件要说「原因未能确认」。"""
        s = _sample(outcome=None, site_outcomes=[_site_outcome(outcome=None)])
        assert self._closing(s) == "没有找到《功夫》的可播放网址，原因未能确认。"

    def test_reflection的conclusion带上(self):
        """INC46：结论来源是 unit 级 ``reflection``，不是末步 rationale。

        改动作前动机之后，末步 rationale 写的是「为什么点这一下」——
        拿它当收尾，消息流会以一句「我打算点开看看」结束。
        """
        s = _sample(reflection={"status": "written",
                                "rationale": {"text": "这一趟确认没有正片",
                                              "conclusion": "这站没有播放入口"},
                                "reason": "", "degraded": []})
        assert self._closing(s) == (
            "没有找到《功夫》的可播放网址，"
            "已排除的站点：x.test（页面上没有播放控件，排除）。"
            "这站没有播放入口。"
        )

    def test_结论内部的句号不被截断(self):
        """结论本身可能是多句——按最后一个句号截断会只剩半句。"""
        s = _sample(reflection={"status": "written",
                                "rationale": {"conclusion": "先看了首页。再点了播放"},
                                "reason": "", "degraded": []})
        assert self._closing(s).endswith("先看了首页。再点了播放。")

    def test_末步rationale不再充当收尾(self):
        """反过来仍成立（§7.1）：有末步 conclusion 但**没有** reflection
        时，收尾不提它——**不拿预期冒充结论**。"""
        s = _sample(six={
            "rationales": [
                {"step_index": 0, "text": "找了一圈", "conclusion": "我打算点开看看",
                 "status": "written", "reason": ""},
            ],
        })
        text = self._closing(s)
        assert "我打算点开看看" not in text
        assert text.startswith("没有找到")

    def test_reflection未写成时不带结论(self):
        """``unavailable`` 的 reflection 没有文本，与「没有」同路。"""
        s = _sample(reflection={"status": "unavailable", "rationale": None,
                                "reason": "LLM 不可用", "degraded": []})
        assert self._closing(s) == (
            "没有找到《功夫》的可播放网址，"
            "已排除的站点：x.test（页面上没有播放控件，排除）。"
        )

    def test_空轨迹不编总结(self):
        """一个可见模型动作都没有的样本没有可总结的事。"""
        s = _sample(six={"actions": [], "observations": []})
        msgs = views.to_openai_messages(s)
        assert all("没有找到" not in (m.get("content") or "") for m in msgs)
        assert views.to_meta(s)["closing_summary"] is False

    def test_messages视图同样收尾(self):
        msgs = views.to_messages(_sample())
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["content"] == (
            "没有找到《功夫》的可播放网址，"
            "已排除的站点：x.test（页面上没有播放控件，排除）。"
        )


# ── 判断 8：缺观察显式发占位回执 ──────────────────────────────────


class Test占位回执:
    def test_末步缺观察不留悬空tool_call(self):
        """发出去就没回音的 ``tool_calls`` 是不合法的 function-calling
        序列——「没采到」靠占位文本表达，不是靠消息缺席。"""
        s = _sample(six={
            "actions": [
                {"tool": "goto", "params": {"url": "a"}, "target": None, "origin": "model"},
                {"tool": "click", "params": {}, "target": {"tag": "b", "label": "c"},
                 "origin": "model"},
            ],
            "observations": [_obs()],
        })
        msgs = views.to_openai_messages(s)
        assert [m["role"] for m in msgs] == \
            ["system", "user", "assistant", "tool", "assistant", "tool", "assistant"]
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert tool_msgs[1]["tool_call_id"] == "call_1"
        assert tool_msgs[1]["content"] == "（本步没有取到页面内容）"

    def test_缺失占位渲染成占位文本(self):
        """P2 的 MISSING_OBSERVATION 真值为真（防下游真值过滤再丢），
        但语义就是「没采到」——不把内部占位结构露给模型。"""
        out = views.render_observation({"degraded": ["observation_missing"]})
        assert out == "（本步没有取到页面内容）"


# ── rationale 稀疏对齐 ────────────────────────────────────────────


class Test稀疏对齐:
    def test_中间缺一条不串位(self):
        """``align_rationales`` 的产出是稀疏序列（跳号），按列表位置取
        会让「第 1 步没写成」变成「第 1 步说了第 2 步的话」。"""
        s = _sample(six={
            "actions": [
                {"tool": "goto", "params": {"url": "a"}, "target": None, "origin": "model"},
                {"tool": "click", "params": {}, "target": {"tag": "b", "label": "播放"},
                 "origin": "model"},
            ],
            "observations": [_obs(), _obs("https://y.test")],
            "rationales": [
                {"step_index": 1, "text": "第二步的思考", "conclusion": "",
                 "status": "written", "reason": ""},
            ],
        })
        msgs = views.to_openai_messages(s)
        assistants = [m for m in msgs
                      if m["role"] == "assistant" and m.get("tool_calls")]
        assert assistants[0]["content"] is None, "第 0 步没写成就是没写成"
        assert assistants[1]["content"] == "第二步的思考"


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
        # ⚠️ **2026-10-30 · 方案 23**：``unit`` 字段没了（单元不再是样本
        # 粒度）；站点级明细改挂在 ``site_outcomes`` 上——训练侧只吃
        # 轨迹级结论，站点明细是**轨迹内部的细节**。
        assert "unit" not in m
        assert m["site_outcomes"][0]["url"] == "https://x.test"
        assert m["site_outcomes"][0]["matched"] is True

    def test_outcome带进meta(self):
        m = views.to_meta(_sample())
        assert m["outcome"]["branch"] == "no_play_control"

    def test_成对性自查(self):
        """动作多于观察 = 末步没取到观察。真实轨迹里会发生
        （点了点不动），消息视图显式发占位回执，但批次筛选仍要
        能一眼分出「观察齐全」与「有步没采到」。"""
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

    def test_缺失观察计数(self):
        """占位多的样本观察面薄，复核优先级该高——计数要在 meta 里。"""
        s = _sample(six={
            "actions": [
                {"tool": "goto", "params": {"url": "a"}, "target": None, "origin": "model"},
                {"tool": "click", "params": {}, "target": {"tag": "b", "label": "c"},
                 "origin": "model"},
            ],
            "observations": [_obs(), {"degraded": ["observation_missing"]}],
        })
        m = views.to_meta(s)
        assert m["missing_observation_count"] == 1
        assert m["observation_complete"] is True  # 逐位对位后不再少观察

    def test_收尾标记(self):
        assert views.to_meta(_sample())["closing_summary"] is True

    def test_rationale_steps按条目自带step_index(self):
        """稀疏序列按列表位置编号会把跳号抹平——「缺中间那步」
        与「缺最后那步」处置不同，编号必须保真。"""
        s = _sample(six={
            "rationales": [
                {"step_index": 2, "text": "t", "conclusion": "",
                 "status": "written", "reason": ""},
            ],
        })
        assert views.to_meta(s)["rationale_steps"] == [
            {"step_index": 2, "status": "written"}
        ]


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
            "T001__abc123_messages.json",
            "T001__abc123_meta.json",
            "T001__abc123_openai.json",
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


class Test产品根:
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


# ── 增量六十一回归：rationale 嵌套形态 ─────────────────────────────


class TestRationaleText形态:
    """``_rationale_text`` 必须兼容 P1 真实写盘形态——嵌套
    ``{"rationale": {"text": think, "conclusion": "..."}}``。

    **历史坑（2026-10-19 增量六十一发现）**：本函数历史上读
    ``rationale["text"]`` 扁平字段,而生产 ``agent_loop._native_think_payload``
    与 thought_refactor 都写嵌套 ``rationale["rationale"]["text"]``。
    结果**所有 P1 不论 scripted / agent** 思考文本都被丢掉,assistant 消息
    只有 tool_call。LS 上看就是「LLM 没思考直接调工具」——其实是渲染期
    路径错了。本类盯两条路径都支持,并以嵌套形态为主路径。
    """

    def test_嵌套形态_text被读到(self):
        """主路径：嵌套 ``{"rationale": {"text": "..."}}``。"""
        from trajectory_pipeline.assembler.views import _rationale_text
        r = {
            "status": "written",
            "reason": "native-think",
            "rationale": {"text": "我先打开百度搜索页", "conclusion": ""},
            "degraded": [],
        }
        out = _rationale_text(r)
        assert "我先打开百度搜索页" in out

    def test_嵌套形态_conclusion也带上(self):
        from trajectory_pipeline.assembler.views import _rationale_text
        r = {
            "status": "written",
            "rationale": {"text": "点搜索按钮", "conclusion": "看正片"},
            "reason": "native-think", "degraded": [],
        }
        out = _rationale_text(r)
        assert "点搜索按钮" in out
        assert "看正片" in out

    def test_扁平形态向后兼容(self):
        """扁平 ``{"text": "..."}`` 顶层形态（防御性兜底）。"""
        from trajectory_pipeline.assembler.views import _rationale_text
        r = {"status": "written", "text": "旧形态", "conclusion": ""}
        out = _rationale_text(r)
        assert "旧形态" in out

    def test_status非written返空串(self):
        """status 不是 written → 空串——与同款纪律(避免污染 CoT SFT)。"""
        from trajectory_pipeline.assembler.views import _rationale_text
        # nested + status != written
        assert _rationale_text({"status": "absent", "rationale": {"text": "x"}}) == ""
        # 缺 status
        assert _rationale_text({"rationale": {"text": "x"}}) == ""
        # None
        assert _rationale_text(None) == ""

    def test_嵌套空text_扁平空text都返空串(self):
        """两条路径 text 都是空 → 空串（不会编占位思考）。"""
        from trajectory_pipeline.assembler.views import _rationale_text
        assert _rationale_text({
            "status": "written",
            "rationale": {"text": "", "conclusion": ""},
        }) == ""
        assert _rationale_text({"status": "written", "text": "", "conclusion": ""}) == ""

    def test_messages视图_嵌套rationale真文本进content(self, tmp_path):
        """端到端：嵌套 rationale 真文本必须出现在 to_messages 产出里。
        这条覆盖"LS 上看不到 LLM 思考"的根因路径。
        """
        from trajectory_pipeline.assembler import views as vw

        sample = {
            "sample_id": "s-001",
            "task_id": "T001",
            "run_id": "a1",
            "reflection": None,
            "outcome": None,
            "ledger": None,
            "six": {
                "system_prompt": "",
                "user_prompt": "帮我找资源",
                "tools": [],
                "actions": [
                    {"tool": "goto", "params": {"url": "https://baidu.com"},
                     "origin": "model", "target": None},
                ],
                "observations": [
                    {"url": "https://baidu.com", "page_title": "百度",
                     "body_text": "百度搜索", "truncated": False, "degraded": [],
                     "video_tag_count": 0, "iframe_count": 0,
                     "interactive_elements": [], "links": []},
                ],
                "rationales": [
                    {"step_index": 0, "status": "written", "reason": "native-think",
                     "rationale": {"text": "我要先打开百度", "conclusion": "搜资源"},
                     "degraded": []},
                ],
            },
        }
        msgs = vw.to_messages(sample)
        # 找到 assistant 消息
        assistant = next(m for m in msgs if m["role"] == "assistant")
        assert "我要先打开百度" in assistant["content"], \
            f"思考文本没渲染进 assistant.content：{assistant['content']!r}"


# ── 2026-11-10：空状态前缀不进训练视图 ────────────────────────────


class Test去空状态前缀:
    """「目前没有此前操作。」是模型对空上下文的陈述，不是思考。

    ⚠️ 只删句首的**精确句**：正文里出现（复述、引用）不动，
    「目前没有找到可播放页面」这类有效事实更不动。
    """

    def test_openai视图去掉句首空状态(self):
        s = _sample(six={"rationales": [{
            "step_index": 0, "status": "written",
            "rationale": {
                "text": "目前没有此前操作。要寻找正片，先打开搜索页。",
                "conclusion": "下一步打开搜索页。",
            },
        }]})
        assistant = next(
            m for m in views.to_openai_messages(s) if m["role"] == "assistant"
        )
        assert not assistant["content"].startswith("目前没有此前操作。")
        assert "要寻找正片" in assistant["content"]
        assert "下一步打开搜索页。" in assistant["content"]

    def test_messages视图同样去掉(self):
        s = _sample(six={"rationales": [{
            "step_index": 0, "status": "written",
            "rationale": {
                "text": "目前没有此前操作。先搜索。",
                "conclusion": "打开搜索页。",
            },
        }]})
        assistant = next(
            m for m in views.to_messages(s) if m["role"] == "assistant"
        )
        assert "目前没有此前操作" not in assistant["content"]

    def test_正文里的同一句不动(self):
        """只删句首。复述/引用里的同一句是有效内容，删了就是伪造。"""
        s = _sample(six={"rationales": [{
            "step_index": 0, "status": "written",
            "rationale": {
                "text": "复核员说「目前没有此前操作。」这句要删，我先按新口径走。",
                "conclusion": "继续。",
            },
        }]})
        assistant = next(
            m for m in views.to_openai_messages(s) if m["role"] == "assistant"
        )
        assert "目前没有此前操作。" in assistant["content"]
