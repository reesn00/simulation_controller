"""正文候选行——规则去噪与相关度打分的**唯一真源**。

为什么放地基
------------
两处要用**同一份**规则：

- :mod:`trajectory_pipeline.assembler.observation_view`——训练态渲染的
  剔噪与预算填充（现状行为，一字不能变）；
- :mod:`trajectory_pipeline.refine`——LLM 精炼的**候选行**构造。

两份实现必然漂：漂了不报错，只是「精炼时给模型看的候选集」与「渲染时
剔掉的集」对不上，精炼结果里会多出规则本该剔掉的行（广告、备案号、导航），
而渲染层**不会再去剔一次**——它们已经被模型「选」过了（选过 = 保留）。

所以规则放地基（:mod:`trajectory_pipeline.common`，可被任何模块 import），
两边各自 import，**不各留一份**。

⚠️ 这层**只放纯函数**：不读文件、不发请求、不改状态。
"""

from __future__ import annotations

import re
from typing import Sequence

#: 整行精确匹配就算噪声的站内导航词。**只认整行**——正文里出现「首页」
#: 两个字不等于这一行是导航。
NAV_LINE_WORDS = frozenset({
    "首页", "图片", "新闻", "视频", "贴吧", "知道", "文库", "地图",
    "更多", "登录", "注册", "登录/注册", "设置", "意见反馈", "帮助", "客服",
    "关于我们", "联系我们", "隐私政策", "用户协议", "版权声明", "友情链接",
    "广告服务", "招聘", "举报", "网站地图", "热搜", "排行榜", "上一页",
    "下一页", "末页", "展开", "收起", "返回顶部", "下载客户端", "关注",
    "收藏", "分享", "评论", "点赞", "发帖", "百度首页", "个人中心",
})

#: 页脚/备案/广告等噪声行。命中即整行丢弃——这些行在绝大多数页面上
#: 是模板，与任务无关；**误伤正文的风险由「任务相关行优先」兜住**
#: （含任务片名/播放关键词的行评分更高，先占预算）。
NOISE_LINE_RE = re.compile(
    r"ICP备|ICP证|网文|许可证|备案号|Copyright|©|版权所有|免责声明|友情链接|"
    r"合作伙伴|关注我们|扫码|下载APP|客户端|用户协议|隐私政策|意见反馈|"
    r"返回顶部|广告|推广|赞助|举报电话|举报邮箱"
)

#: 在线视频场景的「任务相关」信号词。命中即视为有用行，优先占正文预算。
TASK_KEYWORD_RE = re.compile(
    r"播放|在线观看|高清|正片|全集|主演|导演|类型|地区|年份|简介|剧情|片长|"
    r"语言|评分|免费|会员|登录|订阅|预告|蓝光|完整版|在线|观看|第\s*\d+\s*集|"
    r"HD|超清|片名|上映|演员|制片|国家|时长|集数|更新"
)

#: 热搜榜/排行编号行（``1 某条新闻``）。只在整行较短时丢弃——
#: 长行更可能是正文里的编号列表。
HOT_LIST_RE = re.compile(r"^\d{1,2}[.、\s]\s*\S")


def is_noise_line(line: str) -> bool:
    """这一行是不是导航/页脚/广告噪声。

    **整行判定**：只看这一行的字面，不看上下文——「正文里出现『广告』
    两个字」不等于「这一行是广告」，按子串丢会误伤正文。
    """
    if line in NAV_LINE_WORDS:
        return True
    if NOISE_LINE_RE.search(line):
        return True
    return bool(HOT_LIST_RE.match(line) and len(line) < 40)


def line_score(line: str, focus: Sequence[str], keep: Sequence[str]) -> int:
    """一行正文的**任务相关度**。分数只决定先后，不决定去留。

    - 4：下一步动作的目标（``keep``）——必须在预算里；
    - 3：任务片名等 ``focus`` 词；
    - 2：播放/影片元数据关键词；
    - 1：正常长度的内容行；
    - 0：短行（多半是按钮/占位）。
    """
    if any(k and k in line for k in keep):
        return 4
    if any(f and f in line for f in focus):
        return 3
    if TASK_KEYWORD_RE.search(line):
        return 2
    return 1 if len(line) >= 6 else 0


def candidate_lines(body: str) -> list[str]:
    """正文 → **候选行**（去噪 + 去重，**页面顺序**）。

    去重是**逐行**的：同一行文字在页面上出现三次，只留第一次——
    推荐流与页脚的重复占掉大量字数却零信息。

    ⚠️ **去噪在前、去重在后**的顺序不能换：先剔噪声再算重复，
    否则一条噪声行会把后面同名的正文行「吃掉」（噪声行不进 ``seen``，
    所以真正被去重的是两条**都过噪**的同文行）。

    这里的顺序与 :func:`trajectory_pipeline.assembler.observation_view.
    _digest_body` 的历史实现逐字同款——渲染层的行为不能因为这次
    抽模块而变。
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line or line in seen or is_noise_line(line):
            continue
        seen.add(line)
        out.append(line)
    return out


def noise_dropped(body: str) -> int:
    """正文里被**噪声规则 + 去重**丢掉的行数。

    与「精炼丢掉的行数」是**两笔不同的账**：这笔记的是规则剔的
    （导航 / 页脚 / 重复），另一笔记的是模型选的（不相关）。
    两者都记但**不混算**——混算就看不出「裁掉的是噪声还是内容」，
    而这两者的后果完全不同（前者无害，后者要人复核）。
    """
    non_empty = [ln for ln in (body or "").splitlines() if ln.strip()]
    return len(non_empty) - len(candidate_lines(body))


__all__ = [
    "HOT_LIST_RE",
    "NAV_LINE_WORDS",
    "NOISE_LINE_RE",
    "TASK_KEYWORD_RE",
    "candidate_lines",
    "is_noise_line",
    "line_score",
    "noise_dropped",
]
