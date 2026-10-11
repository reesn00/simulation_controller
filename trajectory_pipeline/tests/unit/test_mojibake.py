"""解码损坏（mojibake）的采集侧防护。

这个文件测的不是「怎么检测乱码」，而是**一条具体的失效路径**：

    坏正文 → 感知层 → rationale 闸门实体核查 → 负样本池

链路末端是最贵的那个。闸门把「模型引用了页面上真实存在、但已损坏的实体」
判成**幻觉**（I1 / D-9），于是凭空造出一条负样本——而这条负样本
理由通顺、格式正确、**不报任何错**。

所以防护点选在**采集期**：那里记一次 ``degraded``，
闸门侧 ``gates.observation_shortfall`` 就自动拒绝判幻觉
（见到 ``degraded`` 非空即降级为不可判定）。不在闸门里加第二道门。

fixture 用的是**真实损坏形态**，不是编造的 ``"�" * 5``：
``MOJIBAKE_BODY`` 是把 ``tv.sohu.com`` 的真实 GBK 字节按 UTF-8 解出来的
结果（实测该站 meta 声明 GBK、实际字节也是 GBK，损坏发生在页面内容
经 MCP 传出时被按 UTF-8 处理）。而损坏的关键特征是**局部**——
同一段里中文与 ASCII 都好，只有中间一小片烂掉。编造的 fixture 很容易
写成「整页全烂」，那样就测不到「局部损坏也要拦」这条了。
"""

from __future__ import annotations

from trajectory_pipeline.executor.dom import (
    MOJIBAKE_MIN_COUNT,
    REPLACEMENT_CHAR,
    count_mojibake,
    is_mojibake,
)

#: 真实的**局部**损坏形态：片名与站点名完好，中间一小片是替换符。
#: 三处替换符刚好到阈值——低于它不降级（见 test_偶发个位数不降级）。
MOJIBAKE_BODY = (
    "爱奇艺视频\n"
    "首页 电视剧 综艺 动漫��电影 纪录片\n"
    "全部影视���在线观看\n"
    "搜狐视频 - 电视剧���综艺"
)

#: 完好正文。**刻意含中文与 ASCII**，与上面那份同源——这样两条 fixture
#: 只差乱码这一个变量。
CLEAN_BODY = (
    "爱奇艺视频\n"
    "首页 电视剧 综艺 动漫 电影 纪录片\n"
    "全部影视在线观看\n"
    "搜狐视频 - 电视剧 综艺"
)


class Test检测函数:
    def test_真实损坏被检出(self):
        assert is_mojibake(MOJIBAKE_BODY)

    def test_完好正文不误报(self):
        assert not is_mojibake(CLEAN_BODY)

    def test_空串不算损坏(self):
        """空正文的处置是另一条路（``body_text``），不是 ``mojibake``——
        两件事混起来会让「没采到」与「采到了但不可读」在数据上同形。"""
        assert not is_mojibake("")

    def test_偶发个位数不降级(self):
        """页面上真印着一个 ``�`` 是可能的（图标字体损坏、页脚符号）。
        单个就降级会让「观察不充分」这条路径过于宽松，最后人人都忽略它。"""
        assert count_mojibake("正常正文" + REPLACEMENT_CHAR) == 1
        assert not is_mojibake("正常正文" + REPLACEMENT_CHAR)

    def test_阈值是常量而非散落的字面量(self):
        """阈值出现在两处就会漂。改这里之前先确认调用点用的是常量。"""
        assert MOJIBAKE_MIN_COUNT == 3

    def test_替换符常量就是U_FFFD(self):
        """常量定义若被工具链规范化，检测会静默失效——
        字符串比的是同一个坏字符，谁也发现不了。"""
        assert REPLACEMENT_CHAR == chr(0xFFFD)
        assert len(REPLACEMENT_CHAR) == 1


class Test闸门侧联动:
    """防护的**真正落点**不在检测函数，在闸门。"""

    def test_乱码标记让闸门拒绝判幻觉(self):
        from trajectory_pipeline.rationale.gates import observation_shortfall

        clean = {"body_text": CLEAN_BODY, "degraded": ()}
        broken = {"body_text": MOJIBAKE_BODY, "degraded": ("body_mojibake",)}
        assert observation_shortfall(clean) == ""
        assert observation_shortfall(broken), (
            "带乱码的观察必须被判为不可判定——否则闸门会把"
            "「引用了已损坏实体」当成幻觉，凭空造出负样本"
        )

    def test_乱码与截断同级_都不判幻觉(self):
        """两种「事实残缺」处置相同：**都判不可判定**，不指控模型。

        ⚠️ 断言的是「都非空」而不是「两者返回同一个字符串」——
        :func:`observation_shortfall` 按原因各自给说明（截断说是截断、
        降级说是降级），那是**诊断价值**，要求它们字面相同会把它抹掉。
        真正要守的是**处置同级**：任何一种残缺都不得放行判幻觉。"""
        from trajectory_pipeline.rationale.gates import observation_shortfall

        assert observation_shortfall({"body_text": MOJIBAKE_BODY, "truncated": True})
        assert observation_shortfall(
            {"body_text": MOJIBAKE_BODY, "degraded": ("body_mojibake",)}
        )

    def test_两种原因分别出现在各自的说明里(self):
        """合成一句话会让复核人看不出是哪一种残缺——而两者的处置不同
        （截断只影响截断区外，乱码影响整段）。诊断信息不能为了「统一」丢掉。"""
        from trajectory_pipeline.rationale.gates import observation_shortfall

        assert "截断" in observation_shortfall(
            {"body_text": MOJIBAKE_BODY, "truncated": True})
        assert "body_mojibake" in observation_shortfall(
            {"body_text": MOJIBAKE_BODY, "degraded": ("body_mojibake",)})
