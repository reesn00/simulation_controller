"""``executor.dom`` 的离线单测——用**实测返回样本**而不是编造的 fixture。

样本取自 ``output/pipeline/obscura_returns_*.json``（probe_returns.py 产出）。
用真实样本而非手写假数据的原因：obscura 的返回格式有几处反直觉的地方
（count 返回 JSON 数字、links 是 NDJSON、interactive 是列式文本、
snapshot 正文混入 CSS）——这些正是本模块要处理的，也正是不用真实样本就测不出来的地方。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.executor.dom import (
    clean_body,
    detect_block,
    looks_like_error,
    parse_body_text,
    parse_count,
    parse_interactive_elements,
    parse_links,
    parse_snapshot,
)

ARCHIVE_DIR = Path(__file__).resolve().parents[2] / "output" / "pipeline"


def _load(name: str) -> dict:
    path = ARCHIVE_DIR / name
    if not path.exists():
        pytest.skip(f"实测存档不存在: {name}（先跑 probe_returns.py）")
    return json.loads(path.read_text(encoding="utf-8"))


def _text(report: dict, label: str) -> str:
    return report["results"][label]["text_head"]


# ═══════════════════════════════════════════════════════════════════════
# parse_snapshot：URL/Title 分离 + CSS 剥离
# ═══════════════════════════════════════════════════════════════════════


class TestParseSnapshot:
    def test_分离_url_与_title(self):
        snap = parse_snapshot("URL: https://a.test/x\nTitle: 示例页\n\n正文在这里")
        assert snap.url == "https://a.test/x"
        assert snap.title == "示例页"
        assert snap.body == "正文在这里"

    def test_剥离_style_块(self):
        """实测 baidu 首页的 snapshot 正文大半是 <style> 的 CSS。"""
        body = '<style data-for="result" type="text/css">html{font-size:100px}</style>\n真实内容'
        cleaned = clean_body(body)
        assert "<style" not in cleaned
        assert "font-size" not in cleaned
        assert "真实内容" in cleaned

    def test_剥离_script_块与残余标签(self):
        body = '<script>var x=1;</script><div class="a"><p>你好</p></div>'
        cleaned = clean_body(body)
        assert "var x" not in cleaned
        assert "<div" not in cleaned and "<p>" not in cleaned
        assert "你好" in cleaned

    def test_剥离未闭合的_style_块(self):
        """实测坑：obscura 的 max_chars 在 CSS 块中间硬截断，``</style>``
        根本没回来。配对式正则会失配，CSS 于是全裸进正文。"""
        body = '<style data-for="result" type="text/css" >html{font-size:100px}</style>正文'
        assert clean_body(body) == "正文"
        # 关键场景：闭合标签缺失，只能靠「下一个 <」定边界
        body_unclosed = '<style type="text/css">html{font-size:100px}\n<div>正文</div>'
        cleaned = clean_body(body_unclosed)
        assert "font-size" not in cleaned
        assert "正文" in cleaned

    def test_未闭合块顶到末尾则整段剥掉(self):
        """实测 baidu 首页就是这个形态：6000 字符里连一个 < 都没有。"""
        body = '<style type="text/css">' + "html{font-size:100px}" * 100
        assert clean_body(body) == ""

    def test_未闭合_script_块(self):
        body = '<script>var a=1;var b=2;</script><p>正文</p><script>var c=3;'
        cleaned = clean_body(body)
        assert "var a" not in cleaned and "var c" not in cleaned
        assert "正文" in cleaned

    def test_缺失_url_title_不猜(self):
        """找不到 URL:/Title: 行时返回空串——不猜。"""
        snap = parse_snapshot("只有正文")
        assert snap.url == ""
        assert snap.title == ""
        assert snap.body == "只有正文"

    def test_截断标记(self):
        raw = "URL: https://a.test/\nTitle: T\n\n" + "x" * 1000
        assert parse_snapshot(raw, max_chars=500).truncated is True
        assert parse_snapshot(raw, max_chars=None).truncated is False
        # 远小于预算时不判截断
        assert parse_snapshot(raw, max_chars=100_000).truncated is False

    def test_pollution_ratio_反映_css_占比(self):
        raw = "URL: https://a.test/\nTitle: T\n\n" + "<style>" + "y" * 900 + "</style>真实内容"
        snap = parse_snapshot(raw)
        assert snap.stripped_ratio < 0.5
        assert "真实内容" in snap.body

    def test_保留中文与换行(self):
        snap = parse_snapshot("URL: u\nTitle: 标题\n\n第一段\n\n第二段")
        assert "标题" in snap.title
        assert "第一段" in snap.body and "第二段" in snap.body

    def test_正文覆盖时优先_inner_text(self):
        """driver 正常路径：URL/Title 取自 snapshot，正文取自 innerText。"""
        raw = "URL: https://a.test/\nTitle: T\n\n" + "<style>" + "y" * 3000 + "</style>"
        snap = parse_snapshot(raw, max_chars=6000, body="新闻 hao123 地图")
        assert snap.url == "https://a.test/" and snap.title == "T"
        assert snap.body == "新闻 hao123 地图"
        assert snap.body_source == "inner_text"
        # innerText 路径下不拿 snapshot 的 raw_len 算污染率
        assert snap.stripped_ratio == 1.0

    def test_无_覆盖时标记为_snapshot_源(self):
        snap = parse_snapshot("URL: u\nTitle: T\n\n正文")
        assert snap.body_source == "snapshot"
        assert snap.stripped_ratio < 1.0

    def test_Snapshot_标记行不进正文(self):
        """``browser_snapshot`` 在 Title 之后还有一行 ``Snapshot:``。
        早期版本只剥 URL / Title，于是这行标记成了正文第一行——
        正文本身为空时 ``body_preview`` 就是 ``"Snapshot:"`` 这种纯噪声。"""
        raw = "URL: https://a.test/\nTitle: T\nSnapshot:\n第一段"
        snap = parse_snapshot(raw)
        assert snap.body.startswith("第一段")
        empty = parse_snapshot("URL: https://a.test/\nTitle: T\nSnapshot:\n")
        assert empty.body == ""


# ═══════════════════════════════════════════════════════════════════════
# parse_body_text：innerText 路径（正文的主来源）
# ═══════════════════════════════════════════════════════════════════════


class TestParseBodyText:
    def test_剥离图标字体私有区字符(self):
        """DOM 里的 PUA 字符几乎全是图标字体（实测 baidu 首页 label 里的
         / ），JSON 转义后还会误导 LLM 去找这个实体。"""
        pua = chr(0xE6DC) + chr(0xE613)
        assert parse_body_text('"新闻' + pua + '地图"') == "新闻地图"

    def test_保留非_pua_的_符号与_emoji(self):
        assert parse_body_text('"价格 99 元 🎬"') == "价格 99 元 🎬"
        assert parse_body_text('"a < b && c > d"') == "a < b && c > d"

    def test_实测返回是_json_字符串(self):
        """实测 obscura 对字符串表达式返回 JSON 字符串（带转义换行）。"""
        raw = '"新闻\\nhao123\\n地图"'
        assert parse_body_text(raw) == "新闻\nhao123\n地图"

    def test_裸文本形态也接受(self):
        assert parse_body_text("新闻\n地图") == "新闻\n地图"

    def test_破损_json_降级为裸文本(self):
        assert "正文" in parse_body_text('"正文\\n未闭合')

    def test_超预算截断(self):
        body = parse_body_text('"' + "x" * 100 + '"', max_chars=10)
        assert len(body) == 10

    def test_空返回(self):
        assert parse_body_text("") == ""

    def test_不剥标签_保留_less_than_文本(self):
        """innerText 是纯文本，再套一遍标签剥离会误伤真实内容：
        ``a < b && c > d`` 会被 ``_TAG_RE`` 吃成 ``a d``（实测踩过）。"""
        assert parse_body_text("a < b && c > d") == "a < b && c > d"
        # 可见文本里出现真标签时也**原样保留**——「LLM 看到 = 用户看到」比多洗一遍值钱
        assert parse_body_text("<p>正文</p>") == "<p>正文</p>"


# ═══════════════════════════════════════════════════════════════════════
# parse_links：NDJSON 逐行解析 + 哨兵 + 残行丢弃
# ═══════════════════════════════════════════════════════════════════════


class TestParseLinks:
    def test_解析实测_ndjson(self):
        report = _load("obscura_returns_baidu.json")
        links = parse_links(_text(report, "links"))
        assert len(links) > 3
        assert all(l.href for l in links)

    def test_单行_json(self):
        """实测 example.com 只有一条链接，整体就能解析成功。"""
        report = _load("obscura_returns_example.json")
        links = parse_links(_text(report, "links"))
        assert len(links) == 1
        assert links[0].href.startswith("http")

    def test_空页哨兵(self):
        assert parse_links("No links found.") == ()

    def test_空字符串(self):
        assert parse_links("") == ()

    def test_残行丢弃不补全(self):
        """末行可能被传输截断成半截 JSON——丢弃，不猜。"""
        text = '{"text":"完整","href":"https://a.test"}\n{"text":"半截","href":"https://b.te'
        links = parse_links(text)
        assert len(links) == 1
        assert links[0].href == "https://a.test"

    def test_缺_href_丢弃(self):
        assert parse_links('{"text":"无链接"}') == ()


# ═══════════════════════════════════════════════════════════════════════
# parse_interactive_elements：列式文本 + 转义引号 + ref 稳定性
# ═══════════════════════════════════════════════════════════════════════


class TestParseInteractive:
    def test_解析实测列式文本(self):
        report = _load("obscura_returns_example.json")
        items = parse_interactive_elements(_text(report, "interactive"))
        assert items
        ref, tag, label = items[0]
        assert ref == "e1"
        assert tag == "a"
        assert label == "Learn more"

    def test_空页哨兵(self):
        assert parse_interactive_elements("No interactive elements on this page.") == ()

    def test_还原转义引号(self):
        text = 'ref=e1    textarea               "<style data-for=\\"result\\" type=\\"text/css\\">html{font-size:100px}</style>"'
        items = parse_interactive_elements(text)
        assert len(items) == 1
        ref, tag, label = items[0]
        assert ref == "e1" and tag == "textarea"
        assert '\\"' not in label          # 转义已还原

    def test_外层引号配对_不吞_html_属性(self):
        """实测 baidu 首页 20/75 的行带属性。早期用 strip('"') 会解成
        ``登录" name="tj_login`` —— 标签里混进 DOM 属性会带偏判断点 ③。"""
        text = 'ref=e4    a                      "登录" name="tj_login"'
        ref, tag, label = parse_interactive_elements(text)[0]
        assert label == "登录"

    def test_属性里也有引号不误切(self):
        text = 'ref=e5    a    "第 \\"1\\" 集" title=\\"x\\""'
        assert parse_interactive_elements(text)[0][2] == '第 "1" 集'

    def test_剥掉_label_里的_css(self):
        """textarea 的 value 就是样式表，实测 baidu 首页 6/75 是这种情况。"""
        text = 'ref=e2    textarea               "<style type=\\"text/css\\">html{font-size:100px}</style>"'
        assert parse_interactive_elements(text)[0][2] == ""

    def test_保留_label_里的真实尖括号(self):
        """``<`` 不是标签就不能剥——"第 <3 集>" 是合法内容。"""
        text = 'ref=e6    a    "第 <3 集> 播放"'
        assert parse_interactive_elements(text)[0][2] == "第 <3 集> 播放"

    def test_无闭引号不猜(self):
        text = 'ref=e7    a    "未闭合的标签'
        assert parse_interactive_elements(text)[0][2] == "未闭合的标签"

    def test_丢弃格式不符行(self):
        """ref 错会导致代码点错元素——宁缺勿滥。"""
        text = 'ref=e1    a    "好"\n这是一行垃圾\nref=    \nref=e2    button    "播放"'
        items = parse_interactive_elements(text)
        assert [i[0] for i in items] == ["e1", "e2"]

    def test_label_含多个空格不被截断(self):
        text = 'ref=e3    a    "第 3 集  正片"'
        items = parse_interactive_elements(text)
        assert items[0][2] == "第 3 集  正片"


# ═══════════════════════════════════════════════════════════════════════
# parse_count：返回的是 JSON 数字，不是文本
# ═══════════════════════════════════════════════════════════════════════


class TestParseCount:
    def test_实测返回_json_数字(self):
        report = _load("obscura_returns_baidu.json")
        assert parse_count(_text(report, "count_anchor")) == 70

    def test_零(self):
        assert parse_count("0") == 0

    def test_浮点数字(self):
        assert parse_count("70.0") == 70

    def test_异常输入降级为_零(self):
        assert parse_count("") == 0
        assert parse_count("Error: nope") == 0

    def test_负数钳制(self):
        assert parse_count("-5") == 0


# ═══════════════════════════════════════════════════════════════════════
# looks_like_error：区分故障与业务事实
# ═══════════════════════════════════════════════════════════════════════


class TestLooksLikeError:
    def test_is_error_标记(self):
        assert looks_like_error("Navigated to x", True) is True

    def test_error_前缀(self):
        assert looks_like_error("Error: Network error: ...", False) is True

    def test_正常返回不算错误(self):
        assert looks_like_error('URL: https://a.test/', False) is False

    def test_空文本不算错误(self):
        """空返回是合法状态（空页面），不是错误。"""
        assert looks_like_error("", False) is False


# ═══════════════════════════════════════════════════════════════════════
# detect_block：把「被反爬拦」与「素材没价值」分开
# ═══════════════════════════════════════════════════════════════════════


class TestDetectBlock:
    """实测样本：连跑 3 个 task，第 1 个正常，后 2 个被百度验证码页拦下。"""

    def test_实测百度验证码页(self):
        """这三条就是真实存档里的 URL 与标题。"""
        url = ("https://wappass.baidu.com/static/captcha/tuxing_v2.html"
               "?&logid=8883167535741525163&ak=c27bbc89afca0463650ac9bde68ebe06")
        assert detect_block(url, "百度安全验证", "网络不给力，请稍后重试") == "captcha"

    def test_仅凭URL即可判定(self):
        assert detect_block("https://x.test/static/captcha/a.html", "", "") == "captcha"

    def test_仅凭标题即可判定(self):
        assert detect_block("https://x.test/", "百度安全验证", "") == "captcha"

    def test_限流从正文识别(self):
        assert detect_block("https://x.test/", "结果", "访问过于频繁，请稍后再试") \
            == "rate_limit"

    @pytest.mark.parametrize("url,title,body", [
        ("https://www.baidu.com/s?wd=x", "功夫 在线观看_百度搜索", "功夫 在线观看"),
        ("https://www.iqiyi.com/v_19rrk.html", "爱奇艺-在线视频网站", "海量正版"),
        ("https://tv.sohu.com/v/MjAx.html", "功夫 - 搜狐视频", "在线播放"),
        ("https://v.youku.com/v_show/id_1.html", "功夫-高清完整正版视频", "立即播放"),
        ("", "", ""),
    ])
    def test_正常页面不误判(self, url, title, body):
        assert detect_block(url, title, body) == ""

    def test_网络故障不误判成反爬(self):
        """⚠️ **本条是最容易写反的一条**。

        百度验证码页的正文恰好是「网络不给力，请稍后重试」——
        那句话**字面是网络错误**，在验证码页上只是模板文案。
        若把它当判据，真网络故障就会被误判成反爬，正好与本模块
        的目的相反（网络故障 → 重试；反爬 → 换引擎/加间隔）。
        """
        assert detect_block(
            "https://www.baidu.com/s?wd=x", "", "网络不给力，请稍后重试"
        ) == ""
        assert detect_block("https://x.test/", "超时", "网络不给力") == ""
        assert detect_block("https://x.test/", "无法访问此网站", "连接超时") == ""

    def test_URL优先于标题(self):
        """URL 路径最无歧义，优先。"""
        assert detect_block("https://x.test/captcha", "普通标题", "普通正文") \
            == "captcha"

    def test_空输入不崩(self):
        assert detect_block("", "", "") == ""