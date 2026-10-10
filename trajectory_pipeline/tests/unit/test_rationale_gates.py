"""模块 4 闸门三查 + 词法层的单元测试。

最有分量的两条：

1. **观察不充分时不判幻觉**（rubric D-9 边界规则）。这是 §5.5 那条
   最贵教训的落点——iqiyi 播放页 ``video=1`` 被判「否」，模型把
   **采集失败**讲成了通顺的错结论。闸门要堵的就是这条。
2. **任务给的实体是豁免域**。rationale 提到片名「功夫」而观察里没有它，
   **不是幻觉**——片名来自任务。把任务给的词当「模型引入的实体」
   去核，会把每一条提到片名的 rationale 都判成幻觉。

其余每条纪律都配一个「反过来仍成立」的反例（避坑指南 §7.1）：
只有正例的检查可能是恒过的假检查。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.rationale import gates, lexicon


# ── 夹具：一份**完整**观察（无 degraded / truncated / 裁剪）────────


def _obs(**kw):
    base = {
        "url": "https://v.qq.com/x/cover/abc/o1.html",
        "page_title": "功夫 - 腾讯视频",
        "body_text": "电视剧 电影 当前设备不支持播放 重试 错误码 70017001.1",
        "video_tag_count": 0, "iframe_count": 2, "degraded": [],
        "truncated": False, "elements_total": 2,
        "interactive_elements": [{"tag": "button", "label": "重试"},
                                 {"tag": "button", "label": "看其他"}],
        "links": [{"text": "首页", "href": "https://v.qq.com/"}],
    }
    base.update(kw)
    return base


# ═══════════════════════════════════════════════════════════════════
# 词法层：实体抽取
# ═══════════════════════════════════════════════════════════════════


class Test实体抽取:
    def test_抽URL(self):
        got = [e.text for e in lexicon.extract_entities("打开 https://a.tv/x 看看")]
        assert got == ["https://a.tv/x"]

    def test_URL不吞中文标点(self):
        """``[^\\s]+`` 会把「，然后」一起吞进去，于是「这个 URL 不在
        观察里」永远成立——闸门会把每一条含 URL 的 rationale 判成幻觉。"""
        got = [e.text for e in lexicon.extract_entities("见 https://a.tv/x，然后退出")]
        assert got == ["https://a.tv/x"]

    def test_抽裸域名(self):
        got = [e.text for e in lexicon.extract_entities("这是 v.qq.com 的页面")]
        assert got == ["v.qq.com"]

    def test_裸域名不吞小数与集数(self):
        for text in ("耗时 3.5 秒", "看到第 2 集", "版本 v1.2.3"):
            assert not [e for e in lexicon.extract_entities(text)
                        if e.kind == "url"], f"{text!r} 被误抽成域名"

    def test_抽中文引号对(self):
        got = [e.text for e in lexicon.extract_entities("正文写着「立即播放」。")]
        assert got == ["立即播放"]

    def test_抽书名号(self):
        """片名常带《》。它进豁免域，但抽取要能抽到——否则豁免匹配不上。"""
        got = [e.text for e in lexicon.extract_entities("用户在找《功夫》。")]
        assert got == ["功夫"]

    def test_不抽ASCII引号(self):
        """obscura 的 label 格式是 ``"文本" 属性="值"``（见 executor/dom.py），
        拿 ASCII 引号当引号对会切出 ``属性=值`` 这种无意义片段。"""
        got = [e for e in lexicon.extract_entities('"电影" name="mv"')]
        assert not [e for e in got if e.kind == "quoted"]

    def test_剥掉PUA字符(self):
        """图标字体私有区字符会**主动误导**模型去找这个实体，
        而谁都复制不出那个字符。"""
        got = [e.text for e in lexicon.extract_entities("标签是「播放」")]
        assert got == ["播放"]

    def test_保序去重(self):
        got = [e.text for e in lexicon.extract_entities("「重试」…「重试」…「看其他」")]
        assert got == ["重试", "看其他"]

    def test_单字实体不抽(self):
        """单个汉字不是有意义的核查单位——「有」「的」几乎不可能
        出现在观察里，判它「不在观察里」就是纯假阳性。"""
        assert lexicon.extract_entities("「有」「的」") == ()

    def test_空文本抽不出实体(self):
        assert lexicon.extract_entities("") == ()
        assert lexicon.extract_entities("没有引号也没有网址的一段话") == ()

    def test_不用中文分词(self):
        """切词工具换一版就换一套结果，而闸门要跨批次可比。"""
        assert not [e for e in lexicon.extract_entities("这个页面看起来像播放页")
                    if e.kind == "quoted"]


# ═══════════════════════════════════════════════════════════════════
# 词法层：套话与后见之明
# ═══════════════════════════════════════════════════════════════════


class Test套话:
    def test_整条匹配才算套话(self):
        """子串包含会把「无法确认是否可用」这类**具体判断**误判成套话。"""
        assert lexicon.is_boilerplate("无法确认")
        assert not lexicon.is_boilerplate("正文写着「当前设备不支持播放」")

    def test_疑问后缀放行(self):
        assert lexicon.is_boilerplate("是否可用")

    def test_具体判断里的套话词不算套话(self):
        text = "「无法确认是否可用」这句在页面上"
        assert not lexicon.is_boilerplate("无法确认是否可用")

    def test_滤掉套话实体(self):
        got = lexicon.filter_entities(lexicon.extract_entities(
            "页面「无法确认」，但「重试」按钮在"))
        assert [e.text for e in got] == ["重试"]

    def test_空文本是套话(self):
        assert lexicon.is_boilerplate("")


class Test后见之明:
    def test_收得窄(self):
        """「其实」大量用于句中转折（「其实这个站可以看」），
        把它判成后见之明会误伤一大片正常表达。"""
        assert not lexicon.hindsight_spans("其实这个站可以看")

    def test_判真正的后见之明(self):
        assert lexicon.hindsight_spans("不出所料，这个站又是没用的")

    def test_保序去重(self):
        """按**文本位置**排序而非字典序——读 findings 的人是顺着
        thinking 读的，返回顺序与原文不一致会让他找错位置。"""
        got = lexicon.hindsight_spans("回头看，早知道就不来了")
        assert got == ("回头看", "早知道")


class Test语气:
    def test_确定语气(self):
        assert lexicon.has_certain_tone("页面有 video 标签")
        assert not lexicon.has_certain_tone("未见 video 标签")

    def test_不确定语气(self):
        """G-5：「允许思考表达不确定；确定语气陈述未观察事实 = 幻觉」。"""
        assert lexicon.has_uncertain_tone("未见 video 标签")
        assert not lexicon.has_uncertain_tone("页面有 video 标签")


# ═══════════════════════════════════════════════════════════════════
# 闸门：观察充分性
# ═══════════════════════════════════════════════════════════════════


class Test观察充分性:
    def test_完整观察无短板(self):
        assert gates.observation_shortfall(_obs()) == ""

    @pytest.mark.parametrize("over,needle", [
        ({"degraded": ["links_empty"]}, "降级"),
        ({"truncated": True}, "截断"),
        ({"elements_total": 99}, "裁剪"),
    ])
    def test_各有短板(self, over, needle):
        got = gates.observation_shortfall(_obs(**over))
        assert got and needle in got

    def test_正文与元素皆空(self):
        assert gates.observation_shortfall(
            _obs(body_text="", interactive_elements=[], elements_total=0))

    def test_没有观察(self):
        assert gates.observation_shortfall(None)


# ═══════════════════════════════════════════════════════════════════
# 闸门：查一 实体核查
# ═══════════════════════════════════════════════════════════════════


class Test实体核查:
    def test_引用观察里的内容通过(self):
        r = gates.gate("正文写着「当前设备不支持播放」，需要重试。",
                       action_tool="goto", observations=[_obs()], exempt="功夫")
        assert r.status == "passed" and r.checked_entities == 1

    def test_引用元素label通过(self):
        r = gates.gate("看到了「重试」和「看其他」两个按钮。",
                       action_tool="goto", observations=[_obs()], exempt="功夫")
        assert r.status == "passed" and r.checked_entities == 2

    def test_引用裸域名通过(self):
        """真实文本写 ``v.qq.com`` 而观察存 ``https://www.v.qq.com/...``
        是常态，按全串比会把它们判成幻觉。"""
        r = gates.gate("这是 v.qq.com 的页面。", action_tool="goto",
                       observations=[_obs()], exempt="功夫")
        assert r.status == "passed"

    @pytest.mark.parametrize("text,what", [
        ("页面上有「立即观看」按钮。", "编造按钮"),
        ("打开 https://www.notexist.tv/x。", "编造 URL"),
        ("对比 www.youtube.com 的做法。", "编造域名"),
    ])
    def test_编造实体判幻觉(self, text, what):
        r = gates.gate(text, action_tool="goto", observations=[_obs()],
                       exempt="功夫")
        assert r.status == "failed", f"{what} 没被判幻觉"
        assert [f.kind for f in r.hard_findings] == ["unsupported_entity"]

    def test_任务给的片名是豁免域(self):
        """片名来自任务、不是模型看来的——不是幻觉。
        这条最容易犯：把任务给的词当「模型引入的实体」，
        会把每一条提到片名的 rationale 都判成幻觉。

        ⚠️ **观察里必须不含片名**，否则豁免域不起作用而测试照样通过。
        本模块默认夹具的 ``page_title`` 是「功夫 - 腾讯视频」——
        变异测试实测：去掉豁免域逻辑后这条仍然绿，因为观察自己就含
        「功夫」（§7.2「fixture 是我们自己造的，规则和 fixture 一起错」）。
        """
        no_title_in_page = _obs(page_title="腾讯视频",
                                body_text="电视剧 电影 首页")
        assert "功夫" not in (no_title_in_page["page_title"]
                              + no_title_in_page["body_text"])
        r = gates.gate("用户要找《功夫》，这个站看着不像。",
                       action_tool="goto", observations=[no_title_in_page],
                       exempt="功夫")
        assert r.status == "passed"
        assert r.checked_entities == 1, "片名应当被抽到并走豁免匹配"

    def test_没引用实体不等于没幻觉(self):
        """抽不到实体时不判 fail——「没有可核项」≠「全部一致」。"""
        r = gates.gate("这个页面看起来不太对。", action_tool="goto",
                       observations=[_obs()], exempt="功夫")
        assert r.status == "passed" and r.checked_entities == 0

    # ── 最重的一条 ────────────────────────────────────────────────

    @pytest.mark.parametrize("over", [
        {"truncated": True},
        {"degraded": ["body_text"]},
        {"elements_total": 99},
    ])
    def test_观察不充分时不判幻觉(self, over):
        """rubric D-9 边界规则：「观察被截断时，落在截断外的实体
        **不得判成幻觉**——判『不可判定』」。

        这条是 §5.5 最贵教训的落点：iqiyi 播放页 ``video=1`` 被判「否」，
        模型把**采集失败**讲成了通顺的错结论。"""
        r = gates.gate("页面上有「立即观看」按钮，确认可以播放。",
                       action_tool="goto", observations=[_obs(**over)],
                       exempt="功夫")
        assert r.status == "passed", "观察不充分时判了幻觉"
        assert r.undeterminable, "不可判定的原因必须记下来"

    def test_一份完整观察就能定案(self):
        """任一份完整观察即可定案——被裁的那份**不能**把结论拖成不可判定。"""
        obs = [_obs(truncated=True), _obs()]
        r = gates.gate("页面上有「立即观看」按钮。", action_tool="goto",
                       observations=obs, step_index=1, exempt="功夫")
        assert r.status == "failed"
        assert "unsupported_entity" in [f.kind for f in r.hard_findings]


# ═══════════════════════════════════════════════════════════════════
# 闸门：查二 因果核查
# ═══════════════════════════════════════════════════════════════════


class Test因果核查:
    def test_提到别的动作判错(self):
        r = gates.gate("我要 click 那个按钮。", action_tool="goto",
                       observations=[_obs()], exempt="功夫")
        assert r.status == "failed"
        assert [f.kind for f in r.hard_findings] == ["action_mismatch"]

    def test_不提动作名不算错(self):
        """thinking 里不提动作名是完全正常的写法。**要求必须提到**
        会把好 thinking 判成错。"""
        r = gates.gate("正文写着「当前设备不支持播放」。", action_tool="goto",
                       observations=[_obs()], exempt="功夫")
        assert r.status == "passed"

    def test_提到当前动作不算错(self):
        r = gates.gate("刚 goto 过来的页面有问题。", action_tool="goto",
                       observations=[_obs()], exempt="功夫")
        assert r.status == "passed"


# ═══════════════════════════════════════════════════════════════════
# 闸门：查三 时序核查
# ═══════════════════════════════════════════════════════════════════


class Test时序核查:
    def test_引用后续步才出现的实体判后见之明(self):
        """这是**最强的**后见之明信号，纯代码可确证。
        真实形态：一步只看到站点页，下一步才点进播放页看到播放器；
        而 thinking 已经按播放页的内容说话。"""
        later = _obs(page_title="播放器", body_text="「正在播放」进度条",
                     interactive_elements=[{"tag": "div", "label": "正在播放"}],
                     elements_total=1)
        r = gates.gate("看到「正在播放」了。", action_tool="goto",
                       observations=[_obs(), later], step_index=0,
                       exempt="功夫")
        assert r.status == "failed"
        assert "temporal_leak" in [f.kind for f in r.hard_findings]

    def test_前置步就有的实体不算后见之明(self):
        later = _obs(page_title="别的页")
        r = gates.gate("正文写着「当前设备不支持播放」。", action_tool="goto",
                       observations=[_obs(), later], step_index=0,
                       exempt="功夫")
        assert r.status == "passed"

    def test_无后续步记不适用而不是通过(self):
        """「没有可查项」≠「查过了没问题」。"""
        r = gates.gate("正文写着「当前设备不支持播放」。", action_tool="goto",
                       observations=[_obs()], step_index=0, exempt="功夫")
        assert any("无后续步" in u for u in r.undeterminable)

    def test_后续步不充分时不判后见之明(self):
        """后续步观察不完整时，实体可能**当时就在**而只是没采到。"""
        later = _obs(truncated=True,
                     interactive_elements=[{"tag": "div", "label": "正在播放"}],
                     elements_total=1)
        r = gates.gate("看到「正在播放」了。", action_tool="goto",
                       observations=[_obs(), later], step_index=0,
                       exempt="功夫")
        assert r.status == "passed"
        assert any("后续步" in u for u in r.undeterminable)


# ═══════════════════════════════════════════════════════════════════
# 闸门：软信号与汇总
# ═══════════════════════════════════════════════════════════════════


class Test软信号:
    def test_通篇套话是软发现(self):
        r = gates.gate("无法确认，需要进一步核实。", action_tool="goto",
                       observations=[_obs()], exempt="功夫")
        kinds = [f.kind for f in r.findings]
        assert "boilerplate_only" in kinds
        assert r.status == "passed", "软发现不该让闸门 fail"
        assert not r.hard_findings

    def test_后见之明措辞是软发现(self):
        r = gates.gate("不出所料。", action_tool="goto",
                       observations=[_obs()], exempt="功夫")
        assert any(f.kind == "hindsight" for f in r.findings)
        assert r.status == "passed"

    def test_有具体实体就不算套话(self):
        r = gates.gate("正文写着「当前设备不支持播放」，无法确认能否播放。",
                       action_tool="goto", observations=[_obs()], exempt="功夫")
        assert not [f for f in r.findings if f.kind == "boilerplate_only"]


class Test汇总:
    def test_空rationale是not_run(self):
        """"没跑"不是"通过"——同 ``assembler.schema.GateMark``。"""
        assert gates.gate("").status == "not_run"
        assert gates.gate("   ").status == "not_run"

    def test_可序列化(self):
        r = gates.gate("页面上有「立即观看」按钮。", action_tool="goto",
                       observations=[_obs()], exempt="功夫")
        json.dumps(r.to_json(), ensure_ascii=False)

    def test_多处判错仍是一条汇总(self):
        r = gates.gate("我要 click 那个「立即观看」按钮。", action_tool="goto",
                       observations=[_obs()], exempt="功夫")
        kinds = {f.kind for f in r.hard_findings}
        assert kinds == {"action_mismatch", "unsupported_entity"}


# ═══════════════════════════════════════════════════════════════════
# 对真实观察
# ═══════════════════════════════════════════════════════════════════


class TestOnRealObservations:
    def test_真实观察能跑且抽得出实体(self):
        """**观察侧必须是真实的**——fixture 是我们自己造的，
        规则和 fixture 一起错、测试照样绿（CLAUDE.md 明写）。
        这一条至少保证闸门的输入形状与真实存档一致。"""
        root = Path(__file__).resolve().parents[2] / "output" / "pipeline" / "samples"
        files = sorted(root.glob("*.json"))
        if not files:
            pytest.skip("没有 P2 样本（先跑 cli build）")
        ran = 0
        for p in files:
            s = json.loads(p.read_text(encoding="utf-8"))
            obs = (s.get("six") or {}).get("observations") or []
            if not obs:
                continue
            # 用观察里**真实存在**的元素 label 当 rationale——
            # 照理应当全部通过（或因观察不充分而不可判定），不该判幻觉。
            labels = [str((e or {}).get("label") or "")
                      for e in (obs[-1].get("interactive_elements") or ())]
            labels = [x for x in labels if x][:3]
            for label in labels:
                r = gates.gate(f"页面上有「{label}」。", action_tool="goto",
                               observations=obs, step_index=len(obs) - 1,
                               exempt=s.get("title", ""))
                assert "unsupported_entity" not in [
                    f.kind for f in r.hard_findings], (
                    f"{p.stem}：观察里真实存在的「{label}」被判成幻觉")
                ran += 1
            if ran >= 20:
                break
        assert ran, "一份样本都没跑成"

    def test_真实观察的观察袋包含元素label(self):
        """少并一项就多一条假幻觉——实测元素 label 里的播放按钮名
        是最常被 rationale 引用的实体。"""
        root = Path(__file__).resolve().parents[2] / "output" / "pipeline" / "samples"
        for p in sorted(root.glob("*.json")):
            s = json.loads(p.read_text(encoding="utf-8"))
            obs = (s.get("six") or {}).get("observations") or []
            if not obs:
                continue
            bag = lexicon.resettable_text(obs[-1])
            assert str(obs[-1].get("url") or "") in bag
            for e in (obs[-1].get("interactive_elements") or ())[:3]:
                label = str((e or {}).get("label") or "")
                if label:
                    assert label in bag, f"元素 label「{label}」没进检索袋"
            break
