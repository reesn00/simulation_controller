"""执行步骤——业务语义唯一栖身处。

``browser/`` 只暴露原子原语（导航/快照/点击/输入/执行 JS），
**任何业务判断都必须落在本包**，这样驱动层没有可被 LLM 触及的决策面。

发现链的三个中间层 step（2026-10-16 路径 B/C）：

- :mod:`.link_check`   外链校验（B/C 共用，含跳转确认页检查）
- :mod:`.site_search`  最小站内搜索（首页型候选的「先找片再核验」）
- :mod:`.aggregator`   聚合站访问链（路径 B 中间页单元）
- :mod:`.article`      文章页访问链（路径 C 中间页单元）

站点遍历的两条链（2026-10-17 阶段 A2）：

- :mod:`.visit`        scripted：七段固定序列。**进站段与 ⑦ 段是两条链
                       共用的那两段**（``enter_site`` / ``judge_player``）
- :mod:`.agent_visit`  agent：薄壳，把站内的那一段换成
                       :func:`~trajectory_pipeline.executor.agent_loop.agent_loop`

两条链落的都是 :class:`.visit.VisitResult`，P1 落盘对下游零差异。

中间页的两条链（2026-10-17 阶段 A2b，任务③④）：

- :mod:`.aggregator` / :mod:`.article`          scripted
- :mod:`.agent_intermediate`                    agent（B/C 各一个入口）

四条链落的都是 :class:`.aggregator.IntermediateResult`；两条 agent 链
**共用** scripted 的 ``probe_and_observe``（进站）与
``resolve_into_candidates``（外链校验），换的只有中间「页面内找链接」
那一段。三道判断题（``IS_AGGREGATOR`` / ``SELECT_SITE_ENTRIES`` /
``SELECT_REFERRAL_LINKS``）在 agent 模式下**一道都不跑**，故
:mod:`.agent_intermediate` 的函数签名里根本没有 ``perceptor`` 的位置。
"""
