"""``rationale/writer.py`` 单测——**全程离线**，只把传输层换成替身。

形态上的一个选择：这里造的是**真** :class:`LLMClient` 再把 ``_client``
换成假传输，而不是整个替掉客户端对象。理由与 ``test_llm.py`` 同款——
把传输层替掉才能同时验到 ``_content_of``（``finish_reason`` 分流、
``content`` 为空时的处置），而那正是 writer 最容易踩坏的一层。

⚠️ **断言必须回到「payload 里到底有什么」，不能只看返回值。**
writer 的一半职责发生在**发出去之前**（``ref`` 有没有漏进提示词、
采集降级有没有写进去），这些在返回值里全都看不见——把它们写进
返回值断言就等于什么都没测。
"""

from __future__ import annotations

import json

import pytest

from trajectory_pipeline.llm.client import LLMClient, LLMConfig
from trajectory_pipeline.rationale import gates, writer


class _Transport:
    """假传输。**记录每次请求的 payload**——供「提示词里有什么」的断言。"""

    def __init__(self, reply=None, *, status: int = 200, finish: str = "stop") -> None:
        self.reply = reply
        self.status = status
        self.finish = finish
        self.sent: list[dict] = []

    def post(self, url, json=None, headers=None):  # noqa: A002 - 跟 httpx 同名
        self.sent.append(json if json is not None else {})
        if self.status >= 400:
            return _Resp({"error": "boom"}, self.status)
        return _Resp(
            {"choices": [{"message": {"content": self.reply},
                          "finish_reason": self.finish}]}, 200)

    def get(self, url, headers=None):
        return _Resp({"data": []}, 200)

    def close(self) -> None:
        pass


class _Resp:
    def __init__(self, payload, status: int) -> None:
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def _client(reply=None, *, status: int = 200, finish: str = "stop"):
    c = LLMClient(LLMConfig(base_url="http://x", model="m", max_retries=0))
    c._client = _Transport(reply, status=status, finish=finish)
    return c


def _ok(thinking: str = "页面正文只有导航栏，没有影片名。", conclusion: str = "无法确认。") -> str:
    return json.dumps({"thinking": thinking, "conclusion": conclusion},
                      ensure_ascii=False)


OBS_OK = {
    "url": "https://v.qq.com/x/cover/abc/o1.html",
    "page_title": "腾讯视频",
    "body_text": "电视剧 电影 综艺 少儿",
    "body_len": 11,
    "truncated": False,
    "degraded": [],
    "video_tag_count": 0,
    "iframe_count": 2,
    "interactive_elements": [{"tag": "a", "label": "电视剧", "ref": "e1"}],
}

ACTION_CLICK = {"tool": "click", "params": {},
                "target": {"tag": "a", "label": "立即播放", "ref": "e7"}}


def _write(client, **kw):
    payload = {"action": ACTION_CLICK, "observation": OBS_OK}
    payload.update(kw)
    return writer.write_one(client, **payload)


def _last_user_payload(client) -> dict:
    return json.loads(client._client.sent[-1]["messages"][1]["content"])


def _last_system(client) -> str:
    return client._client.sent[-1]["messages"][0]["content"]


# ── 不可用：一律是返回值，不是异常 ────────────────────────────────


class TestUnavailable:
    def test_后端不通判不可用且带原因(self):
        c = _client(None, status=503)
        r = _write(c)
        assert r.status == "unavailable"
        assert r.draft is None
        # 原因必须带后端给的状态码——否则「不可用」三个字排障时什么也不说
        assert "503" in r.reason

    def test_思考吃掉预算判不可用而不是模型没说话(self):
        """未闭合 ``<think>`` 被剥净后是空串。

        ⚠️ 这个空串与「模型这一轮没说话」在下游**同形**，
        所以必须显式记原因，且原因要指向「预算」而不是「模型」。
        """
        c = _client("<think>我得先想想这个页面到底演了什么" + "想了很久。" * 200)
        r = _write(c)
        assert r.status == "unavailable"
        assert r.draft is None
        assert "预算" in r.reason or "截断" in r.reason
        assert "不是" in r.reason          # 显式否认「模型没说话」

    def test_抠不出JSON判不可用且不静默留散文(self):
        """宁可少一条 rationale，也不可悄悄把散文当 thinking 用。"""
        c = _client("我认为这个页面大概是登录墙，所以没法确认。")
        r = _write(c)
        assert r.status == "unavailable"
        assert r.draft is None

    def test_字段漂移判不可用(self):
        c = _client('{"thought": "…", "verdict": "…"}')
        r = _write(c)
        assert r.status == "unavailable"
        assert "字段漂移" in r.reason

    def test_值为空判empty而不是unavailable(self):
        """「键在但值空」是**数据事实**，「键没有」是**形状漂移**。

        合成一个空串的后果：``empty`` 不该进负样本池而 ``unavailable`` 会，
        统计口径直接错。所以这两个状态必须能分开。
        """
        c = _client('{"thinking": "", "conclusion": "看不太出来"}')
        r = _write(c)
        assert r.status == "empty"
        assert r.draft is None
        assert "数据事实" in r.reason


# ── 红线：raw CoT 不落盘 ──────────────────────────────────────────


class TestRawCoTRedLine:
    def test_剥掉think后正常写出(self):
        c = _client("<think>先看正文有没有片名</think>" + _ok())
        r = _write(c)
        assert r.status == "written"
        assert r.draft is not None
        assert "<think>" not in r.draft.text

    @pytest.mark.parametrize("marker", [
        "<think>", "<thinking>", "<reasoning>",
    ])
    def test_开标签由strip_think负责(self, marker):
        """这三个**不在** writer 的残留表里——它们由 ``strip_think`` 吃掉。

        实测过：未闭合的 ``<reasoning>`` 会被 ``strip_think`` 连同其后
        全部内容一起删掉（``_THINK_ALT`` 的 ``$`` 分支），于是 writer
        根本看不到它。第一版把 ``<reasoning>`` 也列进残留表，
        结果那条守卫**从未触发**——是这条断言把它钉死的。

        ⚠️ **只测开标签。** 孤立的闭标签（``</think>`` 后面没有开标签）
        ``strip_think`` 不删，而它也不该删：闭标签前面若真有一段思维链，
        那段在开标签那里已经连根删了，剩下的闭标签是**空壳**，
        不构成 CoT 泄漏。按闭标签也拒写只会白白丢掉正常样本。
        """
        from trajectory_pipeline.llm.schema_parse import strip_think

        # 闭合形态与**未闭合**形态都要吃掉——后者是 max_tokens 用尽的形态
        for body in (f"前半{marker}思考{marker}后半", f"前半{marker}思考没写完"):
            assert marker not in strip_think(body + _ok())

    @pytest.mark.parametrize("marker", [
        "<|begin_of_thought|>", "<|channel|>", "<scratchpad>", "<reflection>",
    ])
    def test_残留特殊token拒写(self, marker):
        """这些是 ``strip_think`` **漏掉**的形态，writer 是最后一道。"""
        c = _client(f'{{"thinking": "{marker}先看控件", "conclusion": "x"}}')
        r = _write(c)
        assert r.status == "unavailable"
        assert r.draft is None
        assert "红线" in r.reason

    def test_残留表与strip_think不重叠(self):
        """**结构性守卫**：残留表里的每个标记都必须能挺过 ``strip_think``。

        有人日后往 ``_RESIDUAL_COT`` 里加一个 ``strip_think`` 已覆盖的标签，
        这条立刻变红——因为那个标记永远到不了残留检查，
        守卫是死的，而死守卫比没守卫更坏：它看起来在守着红线。
        """
        from trajectory_pipeline.llm.schema_parse import strip_think

        for marker in writer._RESIDUAL_COT:
            assert marker in strip_think(f"前{marker}后"), (
                f"{marker} 已被 strip_think 覆盖，不该再列进 _RESIDUAL_COT")


# ── 成功路径 ──────────────────────────────────────────────────────


class TestWritten:
    def test_正常写出(self):
        c = _client(_ok())
        r = _write(c)
        assert r.status == "written"
        assert r.written is True
        assert r.draft.text.startswith("页面正文")
        assert r.draft.conclusion == "无法确认。"

    def test_别名text与decision容错(self):
        """实测后端会漂字段名——别名容错是共享资产，不该每个模块重写。"""
        c = _client('{"text": "正文是导航", "decision": "不是详情页"}')
        r = _write(c)
        assert r.status == "written"
        assert r.draft.text == "正文是导航"
        assert r.draft.conclusion == "不是详情页"

    def test_超长正文截断并记账(self):
        """半截 CoT 与没有 CoT 是两种不同的坏，而只有记账能分出来。"""
        c = _client(_ok(thinking="理" * (writer.MAX_TEXT_CHARS + 500),
                        conclusion="结" * (writer.MAX_CONCLUSION_CHARS + 50)))
        r = _write(c)
        assert r.status == "written"
        assert len(r.draft.text) == writer.MAX_TEXT_CHARS
        assert len(r.draft.conclusion) == writer.MAX_CONCLUSION_CHARS
        assert "rationale_truncated" in r.degraded
        assert "conclusion_truncated" in r.degraded


# ── JSON 畸形修复：修可以，账要记 ─────────────────────────────────


class TestJSONRepair:
    def test_未转义引号被修好且记账(self):
        """实测形态：``页面标题只是笼统的"腾讯视频"，正文…``。

        大括号是配平的，取出来那段**看起来完全正常**，只有
        ``json.loads`` 知道它坏了。同一后端同一提示词**时好时坏**，
        所以这不是「换个模型就好了」。
        """
        c = _client('{"thinking": "标题只是笼统的"腾讯视频"，正文只有导航。",'
                    ' "conclusion": "无法确认"}')
        r = _write(c)
        assert r.status == "written"
        assert "腾讯视频" in r.draft.text
        assert r.degraded, "被修过的必须记账，不能静默修好"

    def test_未记账就是静默修好(self):
        """回归守卫：修可以，但 ``degraded`` 必须非空。

        去掉记账这一步后返回值一模一样（都是 written），
        所以只能直接盯 ``degraded``。
        """
        c = _client('{"thinking": "他说"是"首页", "conclusion": "x"}')
        r = _write(c)
        assert r.degraded


# ── 提示词里「有什么」（返回值里看不见的那一半职责）──────────────


class TestPromptContent:
    def test_ref句柄绝不进提示词(self):
        """``ref`` 是 obscura 的会话内句柄，换个 session 就失效。

        进训练数据等于让模型学一个随机数，且样本永远不可回放。
        """
        c = _client(_ok())
        _write(c)
        raw = json.dumps(_last_user_payload(c), ensure_ascii=False)
        assert "e7" not in raw
        assert "ref" not in _last_user_payload(c)["this_action"]["target"]

    def test_params里的ref也被剔掉(self):
        """真实 P1 的 ref 在 ``target`` 里而 ``params`` 是空的，
        所以这条在真实数据上**从不触发**——它存在是为了让
        「整条动作里都不许有 ref」这句话为真，而那是**红线**。"""
        got = writer.render_action({"tool": "click",
                                    "params": {"ref": "e9", "x": 1},
                                    "target": {"tag": "a", "label": "播放",
                                               "ref": "e9"}})
        assert got["params"] == {"x": 1}
        assert "ref" not in got["target"]

    def test_采集降级写进提示词(self):
        """§5.5 的第一道防线：模型是**当场**编故事的，闸门是事后才发现。

        所以「这份观察不完整」必须**发出去**，而不是等闸门回来再判。
        """
        c = _client(_ok())
        _write(c, observation=dict(OBS_OK, degraded=["body_text"],
                                   interactive_elements=[]))
        payload = _last_user_payload(c)
        assert payload["observation_completeness"].startswith("不完整")
        assert "body_text" in payload["observation_completeness"]

    def test_完整观察标完整(self):
        c = _client(_ok())
        _write(c)
        assert _last_user_payload(c)["observation_completeness"] == "完整"

    def test_正文超预算时标出被截(self):
        c = _client(_ok())
        _write(c, observation=dict(OBS_OK, body_text="很长的正文" * 400))
        obs = _last_user_payload(c)["observation"]
        assert obs["body_truncated"] is True
        assert len(obs["body_text"]) == writer.BODY_CHARS
        assert obs["body_total_chars"] == 400 * 5

    def test_元素超预算时标出裁剪(self):
        c = _client(_ok())
        _write(c, observation=dict(OBS_OK, interactive_elements=[
            {"tag": "a", "label": f"e{i}", "ref": f"r{i}"} for i in range(50)]))
        obs = _last_user_payload(c)["observation"]
        assert obs["elements_total"] == 50
        assert len(obs["interactive_elements"]) == writer.ELEMENTS
        assert obs["elements_kept"] == writer.ELEMENTS

    def test_前置步只给动作不给观察(self):
        """给观察会让每一步 prompt 线性增长，而超时会拖垮整批。"""
        c = _client(_ok())
        _write(c, history=[{"tool": "goto", "params": {"url": "https://a"},
                            "observation": {"body_text": "不该出现的正文"}}])
        payload = _last_user_payload(c)
        assert payload["previous_actions"] == [
            {"tool": "goto", "params": {"url": "https://a"}}]
        assert "不该出现的正文" not in json.dumps(payload, ensure_ascii=False)

    def test_系统提示词不提执行端实现(self):
        """模型要学的是判断，不是怎么驱动某个浏览器。"""
        c = _client(_ok())
        _write(c)
        sys_text = _last_system(c).lower()
        for banned in ("obscura", "mcp", "ref", "snapshot"):
            assert banned not in sys_text


# ── shortfall 只有一个定义 ────────────────────────────────────────


class TestShortfall:
    @pytest.mark.parametrize("obs", [
        None,
        {},
        {"degraded": ["body_text"], "body_text": "x", "interactive_elements": [{}]},
        {"body_text": "x" * 100, "truncated": True,
         "interactive_elements": [{}], "elements_total": 1},
        {"body_text": "x" * 100, "interactive_elements": [], "elements_total": 9},
        {"body_text": "", "interactive_elements": []},
        {"body_text": "正常正文", "interactive_elements": [{}]},
    ])
    def test_与gates同一个定义(self, obs):
        assert writer.shortfall(obs) == gates.observation_shortfall(obs)

    def test_完整观察不标不完整(self):
        assert writer.shortfall(OBS_OK) == ""


# ── 不 import 上游 ────────────────────────────────────────────────


class TestNoUpstreamImport:
    """writer 喂的是**格式**，不是 executor 的对象。

    理由与 ``assembler.schema`` 同款：交接面是 import 的话，
    上游改一个 dataclass 字段，半年前那批存档就写不出 rationale。
    """

    def test_源码里没有executor_import(self):
        import ast
        import inspect
        from trajectory_pipeline.rationale import writer as w

        tree = ast.parse(inspect.getsource(w))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module)
            elif isinstance(node, ast.Import):
                mods += [a.name for a in node.names]
            for m in mods:
                assert "executor" not in m, f"writer 不该 import {m}"
                assert "assembler" not in m, f"writer 不该 import {m}"
                assert "perception" not in m, f"writer 不该 import {m}"
