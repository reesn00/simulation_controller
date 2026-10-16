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

from trajectory_pipeline.perception.base import Q, Question

#: W1 规则版能确定性判定的题只有一道：**按钮文本是不是「预告片」**。
#: 它是纯文本匹配，不需要理解页面语义。其余三题都要语义，W1 一律 fail-closed。
TRAILER_ONLY_NEEDS_LLM = False


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
                "⚠️ **播放器存在 ≠ 能看**。页面明确要求订阅/付费才能播放时"
                "（开通会员、订阅后观看、购买单集…），"
                "**判否**——本项目的口径是「有播放页但需付费不算成功」。\n"
                "反之，页面只是**导航栏**里挂着「免费观看」「VIP」字样而并未"
                "挡住播放的，不要因此判否。\n\n"
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
                "- ``preview`` — 可看一部分（试看 6 分钟 / 前几集免费 / 限时免费 / 预告）；"
                "agents 不消耗会员也能拿到非空播放\n"
                "- ``trial``   — 试用 / 体验期（首次免、试用 N 天）\n"
                "- ``null``    — 不是以上情形（实在付费墙但形态不属于 preview/trial，"
                "或判 false 但阻挡原因不是付费，例如纯广告前贴片）\n\n"
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
#: 保留是为了**历史兼容**——旧存档合并态展示、以及 visit.py ② 未细分
#: 的老路径（Decision 无 payload 时仍落合并态）都需要它合法。
#:
#: 处理规则与 :func:`all_branches` / ``executor/branches.py::record`` 配套：
#:
#: - ``all_branches()`` **排除**已弃用分支——覆盖追踪（``missing_branches``）
#:   不再要求它产出，新增分支不会因为历史合并态「没产出」而误报；
#: - ``record()`` 校验时**额外放行**——历史路径仍可能产出它，抛
#:   ``UnknownBranch`` 会把旧调用点当场炸掉（test_visit.py 四处断言
#:   会全红）。
DEPRECATED_BRANCHES = frozenset(
    {
        # ② 的合并态：登录墙 / 验证码 / 地区限制。阶段 1 拆分为
        # ``login_required`` / ``blocked`` / ``broken`` 后不再主动产出，
        # 但 visit.py ② 不传 payload 的路径仍落它（决策口径见
        # ``executor/branches.py::decision_branch`` 的「payload 缺失保持现状」）。
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

# ⚠️ ``not_aggregator`` 是**双产地分支**（2026-10-16 路径 B/C）：
# ①B ``SELECT_AGGREGATOR_RESULTS`` 判 False 与 ⑤ ``IS_AGGREGATOR``
# 判 False 都记它。上面的动态推导按 REGISTRY 注册顺序**后者覆盖
# 前者**，所以 :func:`question_of_branch` 返回 ⑤——站点级确认题，
# 与 review_queue 按站点级 outcome 核归属的粒度一致。
# ①B 侧的 (question, branch) 合法性**不受影响**：d5_scorer 的
# ``QUESTION_TO_BRANCHES`` 是 question→branches 一对多推导，两道题
# 的允许集都会包含它。若未来复核工作流要贴这支，须先扩
# ``review_queue._fits`` 支持多产地分支，单一归属只核得出一个产地。


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
