"""判断题注册表——**一道题与它的负样本分支的绑定关系只在这里定义一次**。

为什么值得单开一个模块：四个判断点如果各写各的，「我判 false 时这条样本归到
哪个失败分支」这件事就会散落在控制流里，然后**迟早漏掉一条**——负样本池少一支，
训练数据里就少一类失败原因，而这种缺失是静默的：报表上分部数字都在，
就是有一支永远是 0。:attr:`Question.negative_branch` 把这个绑定提到类型层面，
新增失败分支时必须同步填，否则构造时就报错。

W1 阶段只有 :data:`TRAILER_ONLY_NEEDS_LLM = False` 的题能确定性判定，
其余返回 ``answer=None``（I4 fail-closed）——**不猜**。
这不是「没实现」，是 W1 的正确行为：宁可这一批全是 unresolved，
也不能让规则版假装能判断语义。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from trajectory_pipeline.perception.base import Q, Question

#: W1 规则版能确定性判定的题只有一道：**按钮文本是不是「预告片」**。
#: 它是纯文本匹配，不需要理解页面语义。其余三题都要语义，W1 一律 fail-closed。
TRAILER_ONLY_NEEDS_LLM = False

#: 分类任务（方案 12 阶段 A1）的四个类别——**产物契约的单一出处**。
#: prompt 题面、schema_hint 数组键、llm_perceptor 的 payload 落位、
#: orchestrator 的按类分发四处说的是同一组名字；改名等于破坏性契约
#: 变更（存档 payload 与 P1 里都是这四个键）。
CLASSIFY_CATEGORIES: tuple[str, ...] = (
    "video_site",
    "aggregator",
    "article_post",
    "noise",
)


REGISTRY: MappingProxyType[str, Question] = MappingProxyType(
    {
        Q.SELECT_PLAY_SITES: Question(
            id=Q.SELECT_PLAY_SITES,
            prompt=(
                "以下是从搜索结果页取回的链接，每条带编号 i。"
                "选出其中**提供在线观看该作品**的站点（回 i），"
                "并说明选择理由；不选的要说明排除理由。"
                "**selected 与 rejected 至少要有一条**："
                "哪怕一个都选不出来，也要把最像的几个放进 rejected 并写明为什么。"
                "两个都留空表示「没作答」，会被判为无法采信，整页候选原样保留——"
                "那等于这批什么素材都没采到。"
            ),
            answer_type="decision",
            schema_hint=MappingProxyType(
                {
                    "selected": [{"i": "int", "title": "str", "why": "str"}],
                    "rejected": [{"i": "int", "reason": "str"}],
                }
            ),
            # selected **和** rejected 都要落盘——只存 selected 就无法复核
            # 「为什么把爱奇艺排掉了」，负样本池会退化成「没被选中的都算失败」。
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="not_play_site",
        ),
        Q.IS_REACHABLE: Question(
            id=Q.IS_REACHABLE,
            prompt=(
                "这个页面是否**正常打开了内容**？"
                "登录墙、验证码、404、地区限制、空白页都算不可达。"
                "注意：内容里**提到**「请登录」不代表不可达——"
                "要判断的是页面主体是不是被挡住了。\n\n"
                "1. **填写 ``answer``**：``true`` / ``false`` / ``null`` "
                "三选一——证据不足时给 ``null``，不要猜。\n\n"
                # 2026-10-12 增量十七 · P4a 第二轮：page_quotes 提到 answer
                # 之后第一个必填字段位置——这样无论 answer 真假，模型都被要求
                # 先把"看到的页面原文"写进 page_quotes，再去写 access_control_type
                # / evidence。P4a 第一轮实跑发现 LLM 把页面引文塞进 evidence
                # 字段（用「」/"" 包裹），page_quotes 字段空——fallback 拿不到
                # 证据。把 page_quotes 放首位是根除此问题的姿势：让模型先填
                # page_quotes，再写 evidence。
                "2. **必填 ``page_quotes``**（array of str）——"
                "把你看到的**页面上真实出现的**中文/英文短语**逐字**引用：\n"
                "- 例：``[\"请先登录再继续\", \"登录后观看完整版\", \"您的 IP 不在服务区\"]``\n"
                "- 例：``[\"404 Not Found\", \"视频不存在\"]``\n"
                "- **不**改写、**不**翻译、**不**补前置后置文字（\"用户须\",\"页面含\"等）\n"
                "- **不**写自己的推断（不写\"看起来是登录墙\"）\n"
                "- 没看到明显短语就空数组 ``[]``\n\n"
                "3. 仅当 **answer=false** 时**同时**给出 ``access_control_type`` 字段，"
                "六个值之一：\n"
                "- ``login``      — 提示登录后可继续看（如「请登录」「登录后观看完整版」"
                "等可绕过的登录墙，agent 没登录态因此取不到正片）\n"
                "- ``region``     — 地区限制（如海外 IP 限制、错误码 70017001 等"
                "特定地区码）\n"
                "- ``captcha``    — 图形/滑动/短信验证码\n"
                "- ``anti_bot``   — Cloudflare 验证、机器人检测、JS challenge"
                "（「Performing security verification」「Just a moment…」等）\n"
                "- ``not_found``  — 404 页面、内容已被删除/下架\n"
                "- ``null``       — 判 false 但不属于以上情形（兜底；宁可 null 也别猜）\n\n"
                # 2026-10-12 增量十七 · P4a 第二轮：evidence 明示禁止
                # 任何引号包裹的页面字符。页面引文只能进 page_quotes 字段——
                # evidence 是模型自述（含结论与推断），不允许出现「...」/"..."
                # 包裹的页面原文。这样避免 P3 v1 实证过的「evidence 自述误触
                # 关键字集合」regression 让 fallback 永远可触发。
                "4. **evidence** 只允许写**你自己的一句话判断**（如\"这是登录墙\"、"
                "\"地区码 70017001 不在服务区\"），**禁止**在 evidence 里出现"
                "「」/\"\"\"任何引号包裹的页面上看到的字符——那种引文一律搬到"
                "``page_quotes`` 里。evidence 内禁止引用页面文案——引文都在 "
                "page_quotes 里。"
            ),
            answer_type="bool",
            schema_hint=MappingProxyType(
                {
                    # 2026-10-12 增量十七 · P4a 第二轮：page_quotes 标首位。
                    # ``MappingProxyType`` 保持声明序 = 迭代序，prompt 构造
                    # 与解析容错都按这个顺序遍历。放首位 = 模型先看到 page_quotes。
                    "page_quotes": "array of str",
                    "access_control_type": "login|region|captcha|anti_bot|not_found|null",
                }
            ),
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="login_wall_or_blocked",
        ),
        Q.FIND_PLAY_CONTROL: Question(
            id=Q.FIND_PLAY_CONTROL,
            prompt=(
                "页面上是否存在能开始播放的控件（剧集列表项 / 播放按钮 / 播放器）？"
                "**必须回传该控件的 ref**，代码要用它点击，不要自己找。"
                "如果唯一候选的文本是「预告片」/「预告」，把 trailer_only 置为 true。"
            ),
            answer_type="decision",
            schema_hint=MappingProxyType(
                {"ref": "str", "trailer_only": "bool", "why": "str"}
            ),
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="no_play_control",
        ),
        Q.PLAYER_OK: Question(
            id=Q.PLAYER_OK,
            prompt=(
                "这是点击播放控件后到达的页面。"
                "结合代码测得的媒体元素数量，判断它**是不是一个能正常播放的播放器页**。\n"
                "⚠️ **播放器存在 ≠ 能看**。页面**明说**不给钱/不订阅就看不了正片时"
                "（门禁命题：「订阅后观看」「购买单集」「开通会员观看」"
                "「以下内容需下载…」；或同一句里既有订阅 CTA（开通会员/续费/充值）"
                "又有观看语境（试看/解锁/正片/全集/完整版）），"
                "**判否**——本项目的口径是「有播放页但需付费不算成功」。\n"
                "反之，页面只是**导航栏**里挂着「免费观看」「VIP」「开通会员」"
                "字样而并未挡住播放的，不要因此判否。\n"
                # 2026-10-19 增量 A3 修缮的反假阳性段，2026-10-21（H-16 修复①）
                # **改口径**：原来写「看到续费营销 CTA 就必须判否（留给
                # subscription_wall 大类）」。那条与收紧后的**代码**判据打架——
                # 代码按**门禁语境**判，裸「续费」不成立；而让人为续费横幅
                # 判否、代码又不认门禁，落点就成笼统的 component_unverified
                # （「点了但没组件」——页面明明有组件）。⇒ 两侧改成同一口径：
                # 续费营销**本身不是门禁**，门禁是页面明说的那句话。
                "**反假阳性**：页内出现「**立即续费** / 「**续费享优惠**」 / "
                "「**会员 X 天后到期**」 / 「**包月 / 包年续费**」等"
                "续费营销 CTA / 弹窗 / 横幅时，那是**对老用户的挽留营销**——"
                "它不说明「这里看不了正片」，**不要**仅凭它判否；"
                "只有页面**明说**「续费后才能观看 / 解锁 / 看全集」这类"
                "**门禁命题**时才判否。\n\n"
                "1. **填写 ``answer``**：``true`` / ``false`` / ``null`` "
                "三选一——证据不足时给 ``null``，不要猜。\n\n"
                # 2026-10-12 增量十七 · P4a 第二轮：page_quotes 提到 answer
                # 之后第一个必填字段位置——这样无论 answer 真假，模型都被要求
                # 先把"看到的页面原文"写进 page_quotes，再去写 paywall_type
                # / evidence。P4a 第一轮实跑发现 LLM 把页面引文塞进 evidence
                # 字段（用「」/"" 包裹），page_quotes 字段空——fallback 拿不到
                # 证据。把 page_quotes 放首位是根除此问题的姿势：让模型先填
                # page_quotes，再写 evidence。
                "2. **必填 ``page_quotes``**（array of str）——"
                "把你看到的**页面上真实出现的**付费提示短语**逐字**引用：\n"
                "- 例：``[\"开通 VIP 后观看\", \"试看 6 分钟，订阅继续观看\", \"购买单集 ¥6\"]``\n"
                "- 例：``[\"免费试用 7 天\", \"首月免费\", \"仅限会员\"]``\n"
                "- **不**改写、**不**翻译（页面写「开通会员」就**只**写「开通会员」，"
                "**不**写「提示用户付费」）\n"
                "- **不**写自己的推断（不写\"看起来在催付费\"）\n"
                "- 没看到明显短语就空数组 ``[]``\n\n"
                "3. 仅当 **answer=false** 且页面**确实阻挡**播放（不是单纯提示）时"
                "**同时**给出 ``paywall_type`` 字段，三个值之一：\n"
                "- ``preview`` — 可看一部分（**试看 N 分钟 / 前 N 集免费 / "
                "限时免费 / 限免 / 抢先看（带「部分」「试看」「片段」类词） / "
                "前 6 分钟 / 免费试看**）；agents 不消耗会员也能拿到非空播放。"
                "看到「**试看**」「**前 6 分钟**」「**前几集免费**」类"
                "**结构性**短语就归这里，**不要**归到 ``null`` / ``subscription_wall``\n"
                "- ``trial``   — 试用 / 体验期（**首次免 / 首月免费 / 试用 N 天 / "
                "7 天免费 / 新用户免费 / 免费体验**）\n"
                "- ``null``    — 不是以上情形（实在付费墙但形态不属于 preview/trial，"
                "或判 false 但阻挡原因不是付费，例如纯广告前贴片 / "
                "纯续费营销 CTA——见上方反假阳性段）\n\n"
                # 2026-10-12 增量十七 · P4a 第二轮：evidence 明示禁止
                # 任何引号包裹的页面字符，与 P4a 第一轮同款根因——避免
                # evidence 自述误触 paywall 关键词集合（P3 v1 实证过 regression）。
                "4. **evidence** 只允许写**你自己的一句话判断**（如\"试看付费墙\"、"
                "\"前贴片广告\"），**禁止**在 evidence 里出现「」/\"\"\"任何引号"
                "包裹的页面上看到的字符——那种引文一律搬到 ``page_quotes`` 里。"
                "evidence 内禁止引用页面文案——引文都在 page_quotes 里。"
            ),
            answer_type="bool",
            schema_hint=MappingProxyType(
                {
                    # 2026-10-12 增量十七 · P4a 第二轮：page_quotes 标首位，
                    # ``MappingProxyType`` 保持声明序 = 迭代序。
                    "page_quotes": "array of str",
                    "paywall_type": "preview|trial|null",
                }
            ),
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="component_unverified",
        ),
        # ── 路径 B/C 发现链六题（2026-10-16）────────────────────────
        # 设计原则与上面四题一致：代码取回候选（链接 / 元素），
        # 语义归属（「是不是聚合站」「是不是正式入口」「是不是搜索框」）
        # 全部问模型。B/C 只是**候选来源不同**，产出 Candidate 之后
        # 复用环节② visit 链与分支记账，不重写执行器。
        Q.SELECT_AGGREGATOR_RESULTS: Question(
            id=Q.SELECT_AGGREGATOR_RESULTS,
            prompt=(
                "以下是用**通用聚合查询**（与任何片名无关）搜出的链接，"
                "每条带编号 i。选出其中**本身是影视聚合站 / 影视导航站**"
                "的结果（回 i）——判据是页面用途：聚合多个影视站点入口的"
                "导航页，而不是某部作品的播放页。"
                "并说明选择理由；不选的要说明排除理由。"
                "**selected 与 rejected 至少要有一条**："
                "哪怕一个都选不出来，也要把最像的几个放进 rejected 并写明"
                "为什么。两个都留空表示「没作答」，会被判为无法采信。"
            ),
            answer_type="decision",
            schema_hint=MappingProxyType(
                {
                    "selected": [{"i": "int", "title": "str", "why": "str"}],
                    "rejected": [{"i": "int", "reason": "str"}],
                }
            ),
            # ① 模式：零选中不是「这页没有聚合站」（那是 False 的意思），
            # 是没给出选站结论——负样本走逐候选点名（rejected）路径产生。
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="not_aggregator",
        ),
        Q.IS_AGGREGATOR: Question(
            id=Q.IS_AGGREGATOR,
            prompt=(
                "这个页面是不是**影视聚合站 / 影视导航站**——"
                "即汇总多个影视站点入口（站名 + 外链列表）的导航页？\n"
                "判「否」的典型：单部作品的播放页、新闻资讯页、"
                "论坛首页、下载站。\n\n"
                "1. **填写 ``answer``**：``true`` / ``false`` / ``null`` "
                "三选一——证据不足时给 ``null``，不要猜。\n"
                "2. **evidence** 写你自己的一句话判断"
                "（如「整页是影视站名与外链列表」）。"
            ),
            answer_type="bool",
            schema_hint=MappingProxyType({"evidence": "str"}),
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="not_aggregator",
        ),
        Q.SELECT_SITE_ENTRIES: Question(
            id=Q.SELECT_SITE_ENTRIES,
            prompt=(
                "这是一个影视聚合站页面，页面上有若干链接，每条带编号 i。"
                "选出其中**通向影视站点的正式入口**（回 i）——"
                "即点过去能到某个影视站点（播放站 / 资源站）的链接。\n"
                "排除的写进 rejected 并说明形态：介绍页（讲这个站但"
                "不通向它）、广告位、站内分类页、 dead link 等。\n"
                "**selected 为空且 rejected 非空** = 你确认这个聚合站"
                "页面上没有正式影视站入口——这是一条**真结论**（不是没答）。\n"
                "**两个都留空**才表示「没作答」，会被判为无法采信。"
            ),
            answer_type="decision",
            schema_hint=MappingProxyType(
                {
                    "selected": [{"i": "int", "why": "str"}],
                    "rejected": [{"i": "int", "reason": "str"}],
                }
            ),
            # ⑥ 模式：题问的是「**这个页面**上的入口」，范围收敛到单页，
            # 排除全部候选后零选中是对**本页**的真结论（aggregator_no_entries）
            # ——与 ①（整张搜索页、噪声占多数）刻意不同口径。
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="aggregator_no_entries",
        ),
        Q.SELECT_ARTICLE_RESULTS: Question(
            id=Q.SELECT_ARTICLE_RESULTS,
            prompt=(
                "以下是搜索结果页的链接，每条带编号 i。选出其中"
                "**讨论在线观看资源的文章 / 社区帖子页**（回 i）——"
                "判据是页面形态：影视推荐文、观影攻略、贴吧/论坛讨论帖，"
                "而不是播放站本身、聚合导航站或官方资讯页。"
                "并说明选择理由；不选的要说明排除理由。"
                "**selected 与 rejected 至少要有一条**："
                "哪怕一个都选不出来，也要把最像的几个放进 rejected 并写明"
                "为什么。两个都留空表示「没作答」，会被判为无法采信。"
            ),
            answer_type="decision",
            schema_hint=MappingProxyType(
                {
                    "selected": [{"i": "int", "title": "str", "why": "str"}],
                    "rejected": [{"i": "int", "reason": "str"}],
                }
            ),
            # ① 模式，同 SELECT_AGGREGATOR_RESULTS。
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="not_article",
        ),
        Q.SELECT_REFERRAL_LINKS: Question(
            id=Q.SELECT_REFERRAL_LINKS,
            prompt=(
                "这是一篇讨论在线观看资源的文章 / 帖子页面，"
                "页面上有若干链接，每条带编号 i。选出正文中"
                "**引荐读者前往的影视站外链**（回 i）——"
                "即文章推荐去看片的站点链接。\n"
                "排除的写进 rejected 并说明形态：站内导航、"
                "参考文献、广告、社交分享等。\n"
                "**selected 为空且 rejected 非空** = 你确认这篇文章"
                "没有引荐任何影视站外链——这是一条**真结论**（不是没答）。\n"
                "**两个都留空**才表示「没作答」，会被判为无法采信。"
            ),
            answer_type="decision",
            schema_hint=MappingProxyType(
                {
                    "selected": [{"i": "int", "why": "str"}],
                    "rejected": [{"i": "int", "reason": "str"}],
                }
            ),
            # ⑥ 模式，同 SELECT_SITE_ENTRIES。
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="article_no_links",
        ),
        Q.FIND_SITE_SEARCH: Question(
            id=Q.FIND_SITE_SEARCH,
            prompt=(
                "这是一个影视站点的**首页**（不是某部作品的页面），"
                "目标作品的具体入口不在首页上。"
                "页面上是否存在**站内搜索框**（可用于按片名检索）？"
                "**必须回传该输入框的 ref**，代码要用它填入片名，"
                "不要自己找。页面上没有搜索框时 ref 给 null。"
            ),
            answer_type="decision",
            schema_hint=MappingProxyType({"ref": "str", "why": "str"}),
            # ③ 模式：ref 必须可溯源到 obs.interactive_elements（I6），
            # 词表（input/搜索关键词）作为代码侧兜底与模型交叉印证。
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="no_site_search",
        ),
        # ── 方案 12 阶段 A1（2026-10-16）：搜索页链接分类任务 ──────────
        # 一道题统一 ①/①B/①C 的「选某类链接」语义：不再按发现路径问
        # 三道不同的题，而是一次问出四分类，分发时按路径取对应类
        # （direct → video_site、aggregator → aggregator、article →
        # article_post）。①/①B/①C 保留在注册表（site_search 结果页
        # 仍复用 ①；金标重放与 A/B 对照也需要它们），orchestrator 的
        # 搜索页分叉改用本题。
        Q.CLASSIFY_SEARCH_LINKS: Question(
            id=Q.CLASSIFY_SEARCH_LINKS,
            prompt=(
                "以下是从搜索结果页提取的候选链接（已剔除搜索引擎自身与"
                "结构性噪声），每条带编号 i。把每条链接归入以下四类之一，"
                "并给出理由：\n"
                "- video_site —— 影视播放站：提供在线观看的视频站点"
                "（爱奇艺、腾讯视频、优酷等）或其作品页 / 播放页\n"
                "- aggregator —— 影视聚合站 / 导航站：汇总多个影视站点"
                "入口的导航页\n"
                "- article_post —— 文章 / 社区帖：讨论「在哪看」的推荐文、"
                "观影攻略、问答帖、豆瓣 / 微博讨论帖——正文里可能引荐"
                "影视站外链（知乎、豆瓣、微博的链接多属此类）\n"
                "- noise —— 其他：新闻资讯、下载站、工具页等与在线观看"
                "无关的链接\n\n"
                "判断依据是链接的标题文本与 URL 形态；标题里写着作品名的"
                "播放页是最强的 video_site 证据。\n"
                "每条链接归入且只归入一类；**四个数组全部为空**表示"
                "「没作答」，会被判为无法采信。漏分类的链接会被视为"
                "「未点名」，不计入任何结论。"
            ),
            answer_type="decision",
            schema_hint=MappingProxyType(
                {
                    "video_site": [{"i": "int", "title": "str", "why": "str"}],
                    "aggregator": [{"i": "int", "title": "str", "why": "str"}],
                    "article_post": [{"i": "int", "title": "str", "why": "str"}],
                    "noise": [{"i": "int", "title": "str", "why": "str"}],
                }
            ),
            # 分类是**类型判定**（这条链接是什么站），不是片名↔URL 的
            # 对应判定（那是 ① 的题面），所以预检只查链接非空，
            # _title_link_sufficiency 不适用——与 ①B/①C 同口径。
            #
            # negative_branch 绑定 direct 路径的默认口径；另两条路径
            # （aggregator → not_aggregator、article → not_article）由
            # orchestrator 分发点显式指定（与旧 ①B/①C 的 branch 参数化
            # 同款）。Question.negative_branch 是标量绑不死三种口径，
            # d5_scorer 侧按本题特例放行另两支（见
            # d5_scorer._build_question_to_branches 的 CLASSIFY 特例）。
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="not_play_site",
        ),
    }
)

#: 不属于任何一道 Question 的 ``negative_branch`` 的分支——它们由**代码**
#: 判定（含按 ``Decision.payload`` 选用细分）或兜底，不经 LLM。
#: 挂在注册表里是为了让「失败分支全集」有唯一出处（``executor/branches.py`` 校验）。
#:
#: 来源分两类：
#:
#: **代码直判 / 兜底**：
#:   ``unreachable_hard``  导航失败，代码直接判（``DriverError`` → I4）
#:   ``trailer_only``      预告片词表命中，代码判
#:   ``trailer_suspect``   疑似预告但词表判不准，**留给人工兜底**
#:   ``unresolved``        能力不可用，fail-closed 的落点（I4）
#:   ``player_unverified`` 判断点④ 判不出来，**但控件点了、播放页在眼前**
#:                         ——它与 ``unresolved`` 都「不是负样本」，
#:                         成因却不同（页面证据不足 vs 代码能力不足），
#:                         混在一支里复核队列就没法分工。详见
#:                         ``executor/branches.py`` 的
#:                         :meth:`RunLedger.record_unresolved_reached`
#:
#: **判断题的 payload 细分**（2026-10-12 阶段 1，Q1–Q4 口径冻结）：
#: 题绑定仍是合并态（② ``login_wall_or_blocked``、④ ``component_unverified``），
#: 细分分支由代码按 payload 选用——它们同样必须先出现在注册表里，
#: 否则 ``executor/branches.py`` 落盘校验会当场炸：
#:   ``subscription_wall``  ④ 的细分：**有播放页但需订阅/付费**（2026-10-10
#:                         口径裁定，用户看不到正片 → 不算成功）
#:   ``login_required``     ② 的细分（Q2）：登录后可免费看，agent 无登录态
#:   ``blocked``            ② 的细分（Q4）：地区限制/验证码/反爬/robots 环境阻断
#:   ``broken``             内容损坏（404/媒体不可解码/duration=0），与
#:                         ``unreachable_hard``（环境导航失败）刻意分离
#:   ``partial_free``       ④ 的细分（Q3）：有免费部分（试看/预告），
#:                         完整内容需付费
#:
#: **站内搜索链的代码判定**（2026-10-16 路径 B/C，见
#: ``.trae/documents/路径BC发现链实施计划.md``）：
#:   ``site_search_no_results``  首页型候选的站内搜索**提交成功、到达了
#:                               结果页，但没有产出任何子候选**（选题
#:                               ``SELECT_PLAY_SITES`` 判 False 或启发式
#:                               为空）——由 site_search 代码统一选用，
#:                               与①的 ``not_play_site`` 刻意分流：同一
#:                               个「选不出播放站」结论，产地不同
#:                               （任务级搜索页 vs 站内搜索结果页），
#:                               归因时不能混
#:   ``site_search_routed``      首页型候选经站内搜索成功派发了子候选——
#:                               **路由记账不是结论**：它回答「这个站的处理
#:                               走了哪条路」而不是「这个站行不行」，所以
#:                               不入负样本池（``NON_SAMPLE_BRANCHES``）
CODE_ONLY_BRANCHES = frozenset(
    {
        "unreachable_hard",
        "trailer_only",
        "trailer_suspect",
        "unresolved",
        "player_unverified",
        "subscription_wall",
        "login_required",
        "blocked",
        "broken",
        "partial_free",
        "site_search_no_results",
        "site_search_routed",
    }
)

#: **已弃用**的分支（2026-10-12 阶段 1）：不再由新代码路径主动产生，
#: 保留是为了**历史兼容**——旧存档合并态展示需要它合法。
#:
#: ⚠️ **2026-10-21 起，② 的兜底也不再落它**（台账 H-15，owner 裁定
#: 「只落 ``unresolved``，不开新支」）。原先的兜底路径——② 答 False
#: **且没填** ``access_control_type``——落进这个合并态，而**墙 / 验证码 /
#: 反爬这三个前提一个都没观测到**：实测 10 条这样判出来的负样本，
#: LLM 自己的 evidence 里从没提过「登录 / 验证码 / 反爬」，
#: 现场重采与存档**逐字一致**（不是采集残留），而 A 面（obscura）
#: 对同一批 URL 落的正是 ``unresolved``。⇒ **10/39 = 26% 的负样本池
#: 是分支越权造出来的**。现在兜底 fail-closed 落 ``unresolved``。
#:
#: 处理规则与 :func:`all_branches` / ``executor/branches.py::record`` 配套：
#:
#: - ``all_branches()`` **排除**已弃用分支——覆盖追踪（``missing_branches``）
#:   不再要求它产出，新增分支不会因为历史合并态「没产出」而误报；
#: - ``record()`` 校验时**额外放行**——**历史存档**仍可能带它（清理前
#:   一直合法），抛 ``UnknownBranch`` 会把旧调用点当场炸掉。
#: - ``executor/branches.py::decision_branch`` 的兜底**遇到它就改落
#:   ``unresolved``**——「合并态不再兜底」这条规则按**语义**判
#:   （``fallback in DEPRECATED_BRANCHES``），不按题号判。
DEPRECATED_BRANCHES = frozenset(
    {
        # ② 的合并态：登录墙 / 验证码 / 地区限制。阶段 1 拆分为
        # ``login_required`` / ``blocked`` / ``broken`` 后不再主动产出；
        # **2026-10-21 起兜底路径也不再落它**（② 无 payload ⇒ 落
        # ``unresolved``）——理由见上面那段「分支越权」的记录。
        "login_wall_or_blocked",
    }
)

#: ④ ``PLAYER_OK`` 判 False 时的**细分分支大类**——由 ``Decision.payload``
#: 里的 ``paywall`` / ``paywall_type`` 标记选用，不走 ``negative_branch``
#: （那条是 ``component_unverified``，即「点了但没组件」）。
#:
#: **为什么单独一支**（2026-10-10 口径裁定）：「有播放页但要付费」既不是
#: 成功（用户看不到正片），也不是「页面上没有播放控件」（控件是有的，
#: 卡在付费）。混进 ``component_unverified`` 就丢掉了这个区别，而两者
#: 训练出来的东西完全不同——一个学「识别付费墙并退出」，一个学「控件没渲染」。
#:
#: 2026-10-12 阶段 1 起是**大类**而不是单支：``paywall_type=preview/trial``
#: （试看/预告后付费）落 :data:`partial_free` 分支，其余（订阅/租赁/购买）
#: 仍落本常量。选型见 ``executor/branches.py::decision_branch``。
PAY_WALL = "subscription_wall"


def get(question_id: str) -> Question:
    """取题定义。未知 id 直接 ``KeyError``——**不返回 None**。

    返回 None 会让调用方的 ``if q is None`` 分支把「拼错 id」当成
    「这题不需要判定」静默跳过，是最难查的一类 bug。
    """
    return REGISTRY[question_id]


def negative_branch_of(question_id: str) -> str:
    """取题判 false 时的失败分支——负样本池的入池依据。"""
    return get(question_id).negative_branch or ""


def all_branches() -> frozenset[str]:
    """活跃失败分支全集（20 条，已排除 :data:`DEPRECATED_BRANCHES`）。

    这是契约的一部分：``executor/branches.py`` 落盘时按这个集合校验，
    出现集合外的分支名即报错，避免「分支静默消失」。

    2026-10-12 阶段 1：10 → 13（10 旧 − ``login_wall_or_blocked``
    deprecated + ``login_required`` / ``blocked`` / ``broken`` /
    ``partial_free`` 四个新分支）。已弃用分支不进本集合——它不再要求
    有产出（``missing_branches`` 追踪），但 ``record()`` 校验按
    :data:`DEPRECATED_BRANCHES` 单独放行，见该常量说明。

    2026-10-16 路径 B/C：13 → 20（+ 注册表负分支 ``not_aggregator`` /
    ``aggregator_no_entries`` / ``not_article`` / ``article_no_links`` /
    ``no_site_search`` 五支 + 代码判定 ``site_search_no_results`` /
    ``site_search_routed`` 两支）。
    """
    from_question = {q.negative_branch for q in REGISTRY.values() if q.negative_branch}
    return frozenset((from_question | CODE_ONLY_BRANCHES) - DEPRECATED_BRANCHES)


#: **分支 → 判断点**的反向映射。与 :data:`REGISTRY` 同源推导，
#: 不另手写字面量（手写漏一条的代价见
#: ``evaluation/scorers/d5_scorer.py::_build_question_to_branches`` 的 docstring）。
#:
#: ⚠️ **放在这里而不是 d5_scorer**：``executor.review_queue`` 也要用它，
#: 而 ``executor`` 不能 import ``evaluation``（反向依赖）。``questions`` 是
#: 两者的公共上游，放这儿它才真的只有一个出处。
#:
#: 值是**单个** question_id 而非集合——每个分支都归属唯一一个判断点
#: （``subscription_wall`` 与 ``component_unverified`` 同属④但不是同一支）。
#: ``unresolved`` 归 ``""``：它是 :func:`executor.branches.record_from` 的
#: **通用** fail-closed 出口，任何判断点判 ``None`` 都走它，因此谈不上归属。
_BRANCH_QUESTION: dict[str, str] = {
    **{q.negative_branch: qid for qid, q in REGISTRY.items() if q.negative_branch},
    "subscription_wall": Q.PLAYER_OK,  # ④ 的细分，见 PAY_WALL
    "player_unverified": Q.PLAYER_OK,  # record_unresolved_reached
    "trailer_only": Q.FIND_PLAY_CONTROL,  # ③ 的词表分支
    "trailer_suspect": Q.FIND_PLAY_CONTROL,  # ③，词表判不准待人工
    "unreachable_hard": Q.IS_REACHABLE,  # ② 导航失败，代码判
    "unresolved": "",
    # 2026-10-12 阶段 1 的 payload 细分分支——同样先出现在注册表，
    # 归属才不会被 :func:`question_of_branch` 判成「映射外组合」。
    "login_required": Q.IS_REACHABLE,  # ② 的细分（Q2）：登录后可免费看
    "blocked": Q.IS_REACHABLE,  # ② 的细分（Q4）：地区/验证码/反爬
    "broken": Q.IS_REACHABLE,  # ② 的细分（404）；媒体损坏形态见 media_signal
    "partial_free": Q.PLAYER_OK,  # ④ 的细分（Q3）：试看/预告后付费
    # 2026-10-16 路径 B/C 的站内搜索链——都发生在 FIND_SITE_SEARCH
    # 判 True（找到搜索框）之后的代码路径上，归属以起点判断点计。
    "site_search_no_results": Q.FIND_SITE_SEARCH,  # 结果页未产出子候选
    "site_search_routed": Q.FIND_SITE_SEARCH,  # 路由记账，非结论
}


#: **payload 细分分支**——决策树里这些分支**不是题绑定的**，而是由
#: ``Decision.payload`` 选用（见
#: :func:`~trajectory_pipeline.executor.branches.decision_branch`）：
#: ``access_control_type`` 选 ``login_required`` / ``blocked`` /
#: ``broken``，``paywall`` / ``paywall_type`` 选 ``subscription_wall`` /
#: ``partial_free``。
#:
#: **单一真源**：``d5_scorer.QUESTION_TO_BRANCHES``（判一个
#: ``(question, branch)`` 组合合不合法）与 ``review_queue._fits``
#: （人工贴标签前核归属）都从这张表取。两处各留一份的后果是
#: 「d5 放行、归属拒绝」或反过来，而那种分歧是**静默**的——
#: 一边把行判成分支走错，另一边把同一条判成跨判断点。
#:
#: 2026-10-21（H-16 修复①）**④ 也进了这张表**：播放页**本身**弹
#: 地区限制 / 验证码 / 404 时，④ 才是**最早**看到它的判断点——
#: ② 在点击播放之前，那会儿页面还没报错（实测 v.qq.com 8 条：② 判
#: 「可达」，④ 才撞上「当前设备不支持播放」+ 70017001.1）。
#: ④ 的 payload 此前**没有** ``access_control_type`` 键，于是那种形态
#: 只能被降成笼统的 ``component_unverified``。
#:
#: 同日 **②③ 也进了这张表**（``subscription_wall``）：付费墙**救济**
#: 路径在②判否、③判否时补 ``payload["paywall"]``，``decision_branch``
#: 据此落订阅墙。这条路径 2026-10-15（INC43）就在，判据在
#: 2026-10-21 收紧成**门禁语境**（见
#: :func:`trajectory_pipeline.perception.rule_perceptor.paywall_evidence`）。
#: **不登记的后果**：代码自己产出的 (site.is_reachable, subscription_wall)
#: 会被 D-5 判成「分支走错」——**打分器与生产链互相打架**，而那种
#: 分歧在报告上看不出异常（正是 H-16 记的「同一个仓库里两套口径」）。
#:
#: ⚠️ **这张表只收「payload 选出来的」细分分支**。分类题
#: （``classify.search_links``）的 ``not_aggregator`` / ``not_article``
#: 是**路径分发**选的（orchestrator 按 discovery_path 指定），不是
#: payload 选的——混进来会让 :func:`questions_of_branch` 把
#: ``not_aggregator`` 的产地算成三处，而它只有两处（①B + ⑤）。
PAYLOAD_FINE_BRANCHES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    Q.IS_REACHABLE: (
        "login_required",   # access_control_type=login
        "blocked",          # region / captcha / anti_bot
        "broken",           # not_found
        # 2026-10-21（H-16 修复①）：② 也有**付费墙救济**路径——
        # ``llm_perceptor`` 在②判否且正文有**门禁语境**证据时补
        # ``payload["paywall"]``，``decision_branch`` 据此落本支。
        # 不登记的后果：代码自己产出的 (site.is_reachable, subscription_wall)
        # 会被 D-5 判成「分支走错」——**打分器与生产链互相打架**，
        # 而那种分歧在报告上看不出异常。
        "subscription_wall",
    ),
    Q.PLAYER_OK: (
        "subscription_wall",   # payload["paywall"]
        "partial_free",        # payload["paywall_type"]=preview/trial
        "player_unverified",   # record_unresolved_reached（环节⑦）
        # 2026-10-21：④ 也能带 access_control_type（见上）——播放页本身
        # 弹地区限制 / 验证码 / 404 时，④ 才是最早看到它的判断点。
        # ⚠️ **不含 ``login_required``**：④ 的形态推断走
        # ``rule_perceptor.infer_access_defect``，它**刻意不含 login**
        # （导航栏「请登录」与「开通会员」一样高频，让它让位会把整批
        # 订阅墙赶进 unresolved）。代码产不出的组合不登记。
        "blocked", "broken",
    ),
    Q.FIND_PLAY_CONTROL: (
        "component_unverified",  # I6 违约记账：answer=True 却无 ref
        # 同上：③ 的付费墙救济路径（门禁语境）。
        "subscription_wall",
    ),
})

# ⚠️ ``not_aggregator`` 是**双产地分支**（2026-10-16 路径 B/C）：
# ①B ``SELECT_AGGREGATOR_RESULTS`` 判 False 与 ⑤ ``IS_AGGREGATOR``
# 判 False 都记它。上面的动态推导按 REGISTRY 注册顺序**后者覆盖
# 前者**，所以 :func:`question_of_branch` 返回 ⑤——站点级确认题，
# 与 review_queue 按站点级 outcome 核归属的粒度一致。
# ①B 侧的 (question, branch) 合法性**不受影响**：d5_scorer 的
# ``QUESTION_TO_BRANCHES`` 是 question→branches 一对多推导，两道题
# 的允许集都会包含它。
#
# 2026-10-16 方案 12 阶段 A1：``not_play_site`` 成为第二个双产地分支
# （① ``SELECT_PLAY_SITES`` 与 ①.5 ``CLASSIFY_SEARCH_LINKS`` 同绑），
# 且**复核工作流真的要贴它**（``confirm_not_play_site``）——老存档
# outcome 记 ``question=select.play_sites``、新档记
# ``question=classify.search_links``，单一归属只核得出一个产地。
# 因此 ``review_queue._fits`` 已扩为 :func:`questions_of_branch`
# 复数核归属（任一产地命中即合规），本注释前两段的「粒度一致」
# 论述只对 not_aggregator 成立，不再是一般规则。


def question_of_branch(branch: str) -> str:
    """这个分支归属哪个判断点。未知分支返回 ``""``。

    **用途是「贴标签前核归属」**：人工复核给一条 outcome 换分支时，
    换上去的分支若不属于它**当前那个判断点**，那条记录就不再是
    「这个判断点上判出了这个结论」——决策树（rubric D-5 判的就是它）
    会被塞进一对映射外的 (question, branch) 组合。

    实测踩过（2026-10-10）：``review --apply`` 把「这不是播放站」（①的结论）
    贴到 ``question=site.find_play_control``（③）的 outcome 上，共 14 条，
    其中 15 条触发 D-5 一票否决，且门禁 ``test_d5_scorer::Test同源`` 直接红。
    """
    return _BRANCH_QUESTION.get(branch, "")


def questions_of_branch(branch: str | None) -> list[str]:
    """所有能产出这个分支的判断点（**多产地分支**用，2026-10-16 方案 12 阶段 A1）。

    与 :func:`question_of_branch` 的差别：那个按「后者覆盖前者」只返回
    一个产地，双产地分支（``not_play_site`` = ① + ①.5、``not_aggregator``
    = ①B + ⑤）的另一半会被覆盖掉；本函数把 REGISTRY 里
    ``negative_branch`` 相同的**全部**判断点都返回。

    消费方是 ``executor.review_queue._fits`` 的归属校验：复核回填时
    **只要有一个产地命中当前 outcome 的 question 就合规**，不强制唯一
    ——老档 ``select.play_sites`` 与新档 ``classify.search_links`` 的
    ``not_play_site`` 是同一支「这不是播放站」。

    单产地分支返回单元素列表；仅手工登记的细分分支
    （``subscription_wall`` 等）走 :data:`_BRANCH_QUESTION` 兜底；
    未知分支 / 空入参返回空列表（空列表 = 任何 question 都不合规，
    与旧 ``question_of_branch`` 返回 ``""`` 的拒绝语义一致）。

    2026-10-21（H-16 修复①）：改成**三处来源的并集**——
    REGISTRY 的题绑定 + :data:`PAYLOAD_FINE_BRANCHES` 的 payload 细分
    产地 + :data:`_BRANCH_QUESTION` 兜底。原来是「先到先得」：
    ``component_unverified`` 在 REGISTRY 里绑着④，于是③ 的产地
    （I6 违约记账）被吞掉；而 ``blocked`` 只登记在 :data:`_BRANCH_QUESTION`
    里，④ 也产它的事实（2026-10-21 起）就登记不进去。
    """
    if not branch:
        return []
    out: list[str] = []
    for qid, q in REGISTRY.items():
        if q.negative_branch == branch and qid not in out:
            out.append(qid)
    for qid, branches in PAYLOAD_FINE_BRANCHES.items():
        if branch in branches and qid not in out:
            out.append(qid)
    if not out:
        hit = _BRANCH_QUESTION.get(branch)
        if hit:
            out.append(hit)
    return out
