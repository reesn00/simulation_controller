"""变异测试——**验证守卫真的会红**。

绿测试不等于有覆盖：一个从不执行的 ``if`` 也能让全部测试通过。
每一条变异都指向一条真实守卫，跑对应测试确认它变红，然后**恢复原状**。

**这不是测试，pytest 不收集它**（文件名不匹配 ``test_*.py``，
门禁 ``testpaths`` 收录的也是文件名模式）。跑法::

    uv run python -m trajectory_pipeline.tests.mutation_check

放在 ``tests/`` 下是因为它消费的正是测试选择器——每加一条守卫就往
``MUTATIONS`` 里加一条变异，两张清单必须同步维护，分开放必然漂。

⚠️ **变异会写文件**。它把 ``old`` 替换成 ``new``、跑子集、无论成败都在
``finally`` 里改回去。运行期间**不要**同时编辑同一批源文件。

⚠️ **只对 scripted 批次承担守卫职责**（2026-10-18 A3-5 收口，方案 13
§6.5）。变异测试的前提是「结论可从 task_id 纯函数重算」——scripted 的
代码决策段成立；**agent 批次不成立**：决策侧多了 LLM，同 task 重跑不
保证同轨迹。agent 批的重跑可比性改由两条承担：``executor_mode`` 分批
记账（每批自报是谁跑的）+ 归因锚点存档全保真（llm_calls[] /
gatekeeper_events[] / 落盘观察），A/B 口径从「单次重跑」改为
「同 task N 次重跑取分布」。所以本工具的变异**不指向 agent_loop 的
模型决策**，只指它两侧的护栏（I6 白名单、预算、死循环、分支映射
——那些恰好是 agent 模式最该守住的东西）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: ``tests/`` 的上一级是包根，上上级是仓库根。
ROOT = Path(__file__).resolve().parents[2]

#: pytest 的两个「**一个测试也没跑到**」退出码：
#: **4** = usage error（选择器指向不存在的测试）· **5** = no tests collected。
#: 两者都**非 0**，但都**不代表**「有断言红了」——判红前必须先排掉它们。
_NO_TEST_EXIT_CODES = (4, 5)

#: (标签, 相对路径, 原片段, 变异片段, 应当变红的测试选择器)
#:
#: ⚠️ **两张清单都必须与源码同步**——「旧片段」找不到（锚点漂）与
#: 「选择器指向不存在的测试」（选择器漂）是**两种不同的漂**，各自会
#: 让工具**看不见地**少验一条守卫。2026-10-23（L-20）实测两种都发生过。
#: 锚点漂会被 ``[SKIP]`` 抓出来；**选择器漂不会**——pytest 退出码非 0，
#: 而「非 0」原本被读成「红」⇒ 假红。已加 ``[BAD-SEL]`` 单独判（见
#: :func:`main`）。
MUTATIONS = [
    (
        "铁律2·采集预检",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "            guard = _sufficiency(question, obs, target_title=self._target_title)\n            if guard:",
        '            guard = ""\n            if guard:',
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::TestSufficiencyGuard",
    ),
    (
        "铁律3·ref白名单",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "        if ref and ref not in known:",
        "        if False and ref and ref not in known:",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::TestRefWhitelist",
    ),
    (
        # 2026-10-23（M-26）：「铁律1·确定性优先」那条**作废**——它盯的
        # 是被删掉的那道短路（``video_tag_count >= 1`` ⇒ 直接判成功）。
        # 护栏可以**否**，不能**许**：媒体计数只能当必要条件。
        # 下面两条盯的是**新口径**。
        "媒体计数不许许诺成功·LLM版",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "            decision = handler(spec, obs, started)",
        "            decision = handler(spec, obs, started)\n"
        "            if question == Q.PLAYER_OK and obs.video_tag_count >= 1:\n"
        "                return self._answer(spec, True, evidence='x', started=started)",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::Test媒体计数不许许诺成功",
    ),
    (
        "媒体计数不许许诺成功·规则版",
        "trajectory_pipeline/perception/rule_perceptor.py",
        '                spec, None,\n'
        '                evidence=(\n'
        '                    f"存在 <video>/<audio> 标签 ×{media}（iframe={frames}）——"',
        '                spec, True,\n'
        '                evidence=(\n'
        '                    f"存在 <video>/<audio> 标签 ×{media}（iframe={frames}）——"',
        "trajectory_pipeline/tests/contract/test_perception.py::TestRealSiteRegressions::test_有video标签时不判成功",
    ),
    (
        "预告判定以词表为准",
        "trajectory_pipeline/perception/llm_perceptor.py",
        '            verdict = classify_trailer(label)\n            if verdict == "only":',
        '            verdict = "none"\n            if verdict == "only":',
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::TestTrailerRuleWins",
    ),
    (
        "解析取外层不取内层",
        "trajectory_pipeline/llm/schema_parse.py",
        '    for i, ch in enumerate(clean):\n        if ch != "{":',
        '    for i, ch in reversed(list(enumerate(clean))):\n        if ch != "{":',
        "trajectory_pipeline/tests/unit/test_llm.py::TestExtractJson",
    ),
    (
        "as_bool数字只认0与1",
        "trajectory_pipeline/llm/schema_parse.py",
        "        if value == 1:\n            return True\n        if value == 0:\n            return False\n        return None",
        "        return value != 0",
        "trajectory_pipeline/tests/unit/test_llm.py::TestAsBool",
    ),
    (
        # 2026-10-23（L-20 重锚）：旧 ①（``Q.SELECT_PLAY_SITES``）已被 A1 的
        # 分类任务（``Q.CLASSIFY_SEARCH_LINKS``）取代——**判据没变，断点变了**：
        # fail-closed 批次仍「按代码启发式全遍历，不中断采集」，只是这句话
        # 现在挂在分类任务的批次循环里。
        # ⚠️ 旧选择器里的类名 ``TestSelectPlaySitesWiring`` **已改名**
        # ``TestClassifyCandidatesWiring``（旧名只留在测试文件的说明里）⇒
        # 旧选择器一直「一个测试也没选中」，而 pytest 退出码非 0 ⇒
        # **被读成假红**。这就是 L-20 里那条「选择器漂」的实例。
        "①None不中断采集",
        "trajectory_pipeline/executor/orchestrator.py",
        '            if decision.answer is None:\n'
        '                heuristic_kept.extend(batch)\n'
        '                record.warnings.append(\n'
        '                    f"{gate} 分类任务 fail-closed（{decision.evidence}）——"\n'
        '                    f"该批 {len(batch)} 个候选仍按代码启发式全遍历，不中断采集"\n'
        '                )\n'
        '                continue',
        '            if decision.answer is None:\n'
        '                record.warnings.append(\n'
        '                    f"{gate} 分类任务 fail-closed（{decision.evidence}）——已中断"\n'
        '                )\n'
        '                continue',
        "trajectory_pipeline/tests/unit/test_visit.py::TestClassifyCandidatesWiring::test_未取得结论不中断采集",
    ),
    (
        # 2026-10-23（L-20 重锚）：「无证据不记负样本」在新流程里的形态是
        # 「**模型没点名** ⇒ 记 unresolved，不入负样本池」。
        # 旧实现用 ``named`` 布尔量表达，新实现用 ``named`` **dict** + ``hit``。
        # 变异「全按已点名」：未点名的候选被当成被点名 ⇒ 落进负样本池
        # ⇒ ``test_未点名的候选记unresolved`` 红。
        "①无证据不记负样本·全按已点名",
        "trajectory_pipeline/executor/orchestrator.py",
        "                hit = named.get(candidate.url)",
        '                hit = named.get(candidate.url) or ("", "")',
        "trajectory_pipeline/tests/unit/test_visit.py::Test无证据不记负样本",
    ),
    (
        # 变异「全按未点名」：被点名的候选也被当成没点名 ⇒ 目标类一个都留不下
        # ⇒ ``test_点名的候选仍记not_play_site`` 红。
        "①无证据不记负样本·全按未点名",
        "trajectory_pipeline/executor/orchestrator.py",
        "                hit = named.get(candidate.url)",
        "                hit = None",
        "trajectory_pipeline/tests/unit/test_visit.py::Test无证据不记负样本",
    ),
    (
        # 2026-10-23（L-20 重锚）：落选分支名不再硬编码 ``not_play_site``，
        # 而是取 ``_CLASSIFY_DISPATCH[discovery_path]`` 的 ``branch``。
        # 变异＝把这条记账的分支名写死成另一个 ⇒ ``not_play_site`` 变
        # ``unreachable_hard`` ⇒ 守卫红。
        "①落选分支名正确",
        "trajectory_pipeline/executor/orchestrator.py",
        '                    ledger.record(\n'
        '                        candidate.url,\n'
        '                        branch,\n'
        '                        f"{gate} 分类为 {hit[0]}（非本路径目标类 "',
        '                    ledger.record(\n'
        '                        candidate.url,\n'
        '                        "unreachable_hard",\n'
        '                        f"{gate} 分类为 {hit[0]}（非本路径目标类 "',
        "trajectory_pipeline/tests/unit/test_visit.py::Test无证据不记负样本",
    ),
    (
        # 2026-10-23（L-20 重锚）：**口径反过来了**——旧 ① 按**原始 href**
        # 匹配（``source_href or url``），A1 之后按**解包后**的
        # ``Candidate.url`` 匹配（「模型看到的链接」与「分发命中的候选」
        # 同源，不靠 source_href 猜测）。⇒ 变异＝改回按 source_href 匹配
        # ⇒ 百度 ``/link?url=`` 包裹形态对不上 ⇒ 守卫红。
        "①分类匹配用解包后URL而非原始href",
        "trajectory_pipeline/executor/orchestrator.py",
        "                hit = named.get(candidate.url)",
        "                hit = named.get(candidate.source_href or candidate.url)",
        "trajectory_pipeline/tests/unit/test_visit.py::TestClassifyCandidatesWiring::test_分类匹配用解包后URL而非原始href",
    ),
    (
        "工厂auto无配置退回规则版",
        "trajectory_pipeline/perception/factory.py",
        "        return RulePerceptor()\n\n    if client is None:",
        "        raise LLMUnavailable('no backend')\n\n    if client is None:",
        "trajectory_pipeline/tests/unit/test_factory.py::TestModeSelection",
    ),
    (
        # 2026-10-11：json.dumps 拆行 + ensure_ascii=False 后旧锚点漂移成 SKIP
        "题目写进请求正文",
        "trajectory_pipeline/perception/llm_perceptor.py",
        '        user = json.dumps(\n            {"question": spec.id, "observation": rendered}, ensure_ascii=False\n        )',
        '        user = json.dumps(\n            {"observation": rendered}, ensure_ascii=False\n        )',
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py",
    ),
    (
        "①渲染窗口前置命中链接",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "        window = _link_window(obs, question, target_title)\n",
        "        window = list(obs.links)\n",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::Test预检与渲染同源",
    ),
    (
        "①写回标题用双键查找",
        "trajectory_pipeline/executor/orchestrator.py",
        "dataclasses.replace(\n                        link, text=search_step.title_for(titles, link.href) or link.text\n                    )",
        "dataclasses.replace(\n                        link, text=titles.get(link.href) or link.text\n                    )",
        "trajectory_pipeline/tests/unit/test_visit.py::Test无证据不记负样本",
    ),
    # 2026-10-23（L-20 重锚）：原这里还有一条「①无证据不记负样本」的
    # ``named = False`` 变体——它与上面新加的「全按未点名」**是同一条变异**
    # （旧实现用布尔 ``named``、新实现用 ``hit``，语义一一对应）⇒ 已合并，
    # 不重复占一行。**清单条目数不因重锚而变**（48 → 48）。
    # ── 模块 4 接线（2026-10-10）────────────────────────────────
    (
        # 2026-10-23（L-20 重锚）：``_visit_guarded`` 的调用点后来加了
        # ``allow_site_search=`` / ``title=`` 两个实参，旧的整块锚点
        # （含调用行）已经对不上 ⇒ 只锚**写入那一步**，变异＝摘掉它。
        "编排器不挂writer",
        "trajectory_pipeline/executor/orchestrator.py",
        "            await self._write_rationales(\n                result.steps, record, result.candidate.url[:60]\n            )",
        "            # 变异：摘掉逐步 rationale 写入",
        "trajectory_pipeline/tests/unit/test_visit.py::TestRationale接线",
    ),
    (
        "搜索步不补记",
        "trajectory_pipeline/executor/orchestrator.py",
        '        record.steps.settle(step_search, record.search_obs)\n        # 搜索步也补记 rationale——P2 按决策单元切条，搜索阶段是正式样本\n        # （"一条 = 一个决策单元：搜索阶段一条 / 每个站点一条"），它没有\n        # rationale 会让整条搜索样本恒 gate=not_run、D-9 永远不可判定。\n        # 失败路径（search_obs=None）不产 search 样本，无须补记。\n        await self._write_rationales(record.steps, record, "搜索页")',
        "        record.steps.settle(step_search, record.search_obs)",
        "trajectory_pipeline/tests/unit/test_visit.py::TestRationale接线",
    ),
    (
        "动作流不落rationale",
        "trajectory_pipeline/executor/actions.py",
        '            "rationale": dict(self.rationale) if self.rationale else None,',
        '            "rationale": None,',
        "trajectory_pipeline/tests/unit/test_visit.py::TestRationale接线",
    ),
    (
        "对齐读错字段路径",
        "trajectory_pipeline/assembler/schema.py",
        '        draft = payload.get("rationale")',
        "        draft = payload",
        "trajectory_pipeline/tests/unit/test_assembler.py::TestRationale对齐",
    ),
    (
        "闸门全判通过",
        "trajectory_pipeline/assembler/schema.py",
        '        if res.status == "failed":',
        "        if False:",
        "trajectory_pipeline/tests/unit/test_assembler.py::Test闸门真跑",
    ),
    (
        "闸门按下标取步号而不是step_index",
        "trajectory_pipeline/assembler/schema.py",
        "        i = r.step_index",
        "        i = 0",
        # INC46：rationales 是**稀疏**序列，按下标取会让每步前移一格，
        # 而窗口错一格就是把后见之明判成 anchored。
        "trajectory_pipeline/tests/unit/test_assembler.py::Test闸门真跑",
    ),
    (
        "闸门窗口含本步观察",
        "trajectory_pipeline/rationale/gates.py",
        "    visible = list(observations[:step_index])",
        "    visible = list(observations[: step_index + 1])",
        # 含了本步观察 = 允许后见之明——那正是 INC46 要修的东西。
        "trajectory_pipeline/tests/unit/test_rationale_gates.py::Test时序核查",
    ),
    (
        "首步不早退",
        "trajectory_pipeline/rationale/gates.py",
        "    if not observations:\n        return findings, 0, [\"首步无历史观察，实体核查不适用\"]",
        "    if False:\n        return findings, 0, []",
        # 空窗口走隐式路径会把每一条 goto 首步判成幻觉。
        "trajectory_pipeline/tests/unit/test_rationale_gates.py::Test实体核查",
    ),
    (
        # INC46 单档真跑 E005 site-4 撞出来的：合成实体「A/B」要求
        # **每段都命中**。改成 any 就成了「看到 A 就能顺手编个 B」，
        # 实体核查直接被放空。
        "合成实体任一段即通过",
        "trajectory_pipeline/rationale/gates.py",
        "    return all(p.lower() in haystack_l for p in parts)",
        "    return any(p.lower() in haystack_l for p in parts)",
        "trajectory_pipeline/tests/unit/test_rationale_gates.py::Test实体核查",
    ),
    (
        # 逗号进了分隔符表 = 一句编造的话被拆成短片段逐段核，
        # 短片段极易意外命中，核查被放空。
        "逗号当合成分隔符",
        "trajectory_pipeline/rationale/lexicon.py",
        'COMPOSITE_SEP_RE = re.compile(r"\\s*[/／|｜、]\\s*")',
        'COMPOSITE_SEP_RE = re.compile(r"\\s*[/／|｜、，,]\\s*")',
        "trajectory_pipeline/tests/unit/test_rationale_gates.py::Test实体核查",
    ),
    (
        "①零选中仍判True",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "        if not selected:",
        "        if False:",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::Test选中为空不算答完",
    ),
    # ── 付费墙（2026-10-10 口径裁定）────────────────────────────
    (
        "付费墙payload优先于题绑定",
        "trajectory_pipeline/executor/branches.py",
        '    if decision.payload.get("paywall"):\n        return questions.PAY_WALL',
        "    if False:\n        return questions.PAY_WALL",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::Test付费墙记账",
    ),
    (
        # 2026-10-23（L-20 重锚）：**同页守卫已经窄化**（2026-10-19
        # 增量六十·方案 X）——原来那个 5 条分支的集合早没了，付费墙分支
        # 改走**门禁语境前置**（页面必须明说不给钱/不订阅）。
        # ⇒ 旧锚点（那个集合字面量）不可能再找到。这条变异改盯
        # **现役的付费墙守卫**：门禁语境判据不成立时必须降级 unresolved，
        # 不许照贴 subscription_wall。
        "付费墙守卫·门禁语境前置",
        "trajectory_pipeline/executor/branches.py",
        '        if not hit:\n'
        '            return (\n'
        '                f"页面里没有付费墙门禁证据——"\n'
        '                f"{BRANCH_PRECONDITION[branch]}"\n'
        '            )',
        '        if False:\n'
        '            return (\n'
        '                f"页面里没有付费墙门禁证据——"\n'
        '                f"{BRANCH_PRECONDITION[branch]}"\n'
        '            )',
        "trajectory_pipeline/tests/unit/test_branches.py::TestBranchPrecondition",
    ),
    (
        # 2026-10-23（M-27）：点击失败 ⇒ 记 ``unresolved``（采集缺件），
        # 不记 ``component_unverified``（那要求「到了播放页」这个前置事实，
        # 而点击失败时**从未到达**播放页）。⇒ 变异＝把分类改回去。
        "点击失败记unresolved",
        "trajectory_pipeline/executor/steps/visit.py",
        '        ledger.record(\n'
        '            result.landed_url, "unresolved",\n'
        '            f"点击 ref={ref} 失败: {exc}", decision=control, site_url=url,\n'
        '        )',
        '        ledger.record(\n'
        '            result.landed_url, "component_unverified",\n'
        '            f"点击 ref={ref} 失败: {exc}", decision=control, site_url=url,\n'
        '        )',
        "trajectory_pipeline/tests/unit/test_visit.py::TestVisitFlow::test_点击失败记unresolved",
    ),
    (
        # 2026-10-23（M-27）：同一族的第二条路——点击**成功**但快照取不到。
        "点击后取不到快照记unresolved",
        "trajectory_pipeline/executor/steps/visit.py",
        '        ledger.record(\n'
        '            result.landed_url, "unresolved",\n'
        '            f"点击后取不到页面快照: {exc}", decision=control, reached=True,\n'
        '            site_url=url,',
        '        ledger.record(\n'
        '            result.landed_url, "component_unverified",\n'
        '            f"点击后取不到页面快照: {exc}", decision=control, reached=True,\n'
        '            site_url=url,',
        "trajectory_pipeline/tests/unit/test_visit.py::TestVisitFlow::test_点击后取不到快照记unresolved",
    ),
    (
        # 2026-10-23（L-20 重锚·**新增**）：上一条挪去盯付费墙守卫之后，
        # **同页守卫本身就没有变异盯了**——而它是「点击没换页就不许贴
        # component_unverified」这条前置事实核的**唯一**防线。
        # ⇒ 补一条。**清单条目数 48 → 49**（重锚 9 条 + 合并 1 − 1 + 补 1）。
        "同页守卫·component_unverified",
        "trajectory_pipeline/executor/branches.py",
        "        if _same_page(pre_obs, obs):",
        "        if False and _same_page(pre_obs, obs):",
        "trajectory_pipeline/tests/unit/test_branches.py::TestBranchPrecondition",
    ),
    (
        # 2026-10-23（L-20 重锚）：H-16 修复①给这条加了「让位」条件
        # （``and not access``——模型自己判出地区限制时付费墙让位），
        # 旧锚点（``bool(answer is False and hit)``）已对不上。
        # 口径没变：**只在模型判否时**才升级成付费墙。
        "付费墙只在模型判否时升级",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "        paywall = bool(answer is False and hit and not access)",
        "        paywall = bool(hit and not access)",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::Test付费墙不算成功",
    ),
    (
        "成功不记负分支",
        "trajectory_pipeline/executor/branches.py",
        "            branch = None\n        else:",
        "            branch = questions.negative_branch_of(decision.question)\n        else:",
        "trajectory_pipeline/tests/unit/test_branches.py::TestSerialization",
    ),
    (
        # INC46：``error`` 参数已删除（动作未执行谈不上失败），
        # 这一条改盯**「采集降级必须写进提示词」**这条仍在的守卫。
        "降级提示不再进提示词",
        "trajectory_pipeline/rationale/writer.py",
        "    last_obs = last_history_observation(history)\n    if last_obs is not None:",
        "    last_obs = None\n    if last_obs is not None:",
        "trajectory_pipeline/tests/unit/test_writer.py::TestPromptContent",
    ),
    (
        # 首步没有历史观察时**不写**完整性键——写「完整」等于谎报输入充分。
        "首步仍写完整性键",
        "trajectory_pipeline/rationale/writer.py",
        "    if last_obs is not None:",
        "    if True:",
        "trajectory_pipeline/tests/unit/test_writer.py::TestStep0EmptyHistory",
    ),
    (
        "乱码检测恒假",
        "trajectory_pipeline/executor/dom.py",
        "    return count_mojibake(text) >= threshold",
        "    return False",
        "trajectory_pipeline/tests/unit/test_mojibake.py::Test检测函数",
    ),
    (
        "乱码阈值降到个位数",
        "trajectory_pipeline/executor/dom.py",
        "MOJIBAKE_MIN_COUNT = 3",
        "MOJIBAKE_MIN_COUNT = 1",
        "trajectory_pipeline/tests/unit/test_mojibake.py::Test检测函数",
    ),
    (
        "正文乱码不记降级",
        "trajectory_pipeline/executor/browser/obscura_driver.py",
        "        elif body is not None and degraded is not None and is_mojibake(body):",
        "        elif False:",
        "trajectory_pipeline/tests/unit/test_obscura_driver.py::TestMojibakeAccounting",
    ),
    (
        "标题乱码不记降级",
        "trajectory_pipeline/executor/browser/obscura_driver.py",
        "        if degraded is not None and snap.title and is_mojibake(snap.title):",
        "        if False:",
        "trajectory_pipeline/tests/unit/test_obscura_driver.py::TestMojibakeAccounting",
    ),
    (
        "乱码标记闸门不认",
        "trajectory_pipeline/rationale/gates.py",
        '    if list(obs.get("degraded") or ()):',
        "    if False:",
        "trajectory_pipeline/tests/unit/test_mojibake.py::Test闸门侧联动",
    ),
    (
        # 括号里的隐式字符串拼接少一个尾逗号——四个评分器里**只有这一处**漏过，
        # 其余三处有，所以任何逐个确认的读法都不可靠，测试改成跨四个的性质断言。
        "D-2 无 click 路径的理由退化成字符串",
        "trajectory_pipeline/evaluation/scorers/code_scorer.py",
        '"「没有可校验项」不等于「全部可找到」",))',
        '"「没有可校验项」不等于「全部可找到」"))',
        "trajectory_pipeline/tests/unit/test_code_scorer.py::TestReasons结构",
    ),
    (
        # 2026-10-11：模型看得到按钮却不回 ref 时，旧代码 fail-closed 成 None
        # → 不点击 → 没有 player_obs → 整批 D-2 全是 undetermined。
        "③ ref 缺失时不由词表取回",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "        picked = play_control_candidates(obs)\n        if picked:",
        "        picked = play_control_candidates(obs)\n        if False:",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::TestRef由代码取回",
    ),
    (
        # 反向守卫：取回不是「一律判 True」。判据删掉后每个页面都会
        # 变成「找到了播放控件」，负样本池直接被稀释。
        # 2026-10-11：_answer 参数拆行后旧锚点漂移成 SKIP
        "③ 无控件时不再判否",
        "trajectory_pipeline/perception/llm_perceptor.py",
        '        return self._answer(\n            spec,\n            False,\n            evidence=(\n                f"{len(obs.interactive_elements)} 个交互元素中，"',
        '        return self._answer(\n            spec,\n            True,\n            evidence=(\n                f"{len(obs.interactive_elements)} 个交互元素中，"',
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::TestRef由代码取回",
    ),
    (
        # W1 落 trailer_only、W3 落 no_play_control 的那条缝。
        "③ 纯预告控件不再落trailer_only",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "        trailers = trailer_only_candidates(obs)\n        if trailers:",
        "        trailers = trailer_only_candidates(obs)\n        if False:",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::TestRef由代码取回",
    ),
    (
        # 白名单不能因为「兜底取回」而放松——模型给了不存在的 ref
        # 仍然是编造，点错不可逆。
        "③ 编造ref的白名单被取回逻辑架空",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "        if ref and ref not in known:",
        "        if False and ref and ref not in known:",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::TestRef由代码取回",
    ),
    (
        # 「没作答」与「地址全对不上」合成一句时，evidence 指错方向：
        # 排查会奔着 URL 匹配去，而真正的问题是模型一个都没给。
        "① 没作答与地址对不上共用一句话",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "            if not raw_selected and not raw_rejected:",
        "            if False:",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::TestNoConclusionVsUnmatched",
    ),
    (
        # 编号是链接题的**回指契约**。去掉之后模型回 i 就全落空，
        # 而失败形态是「整批 unresolved」——正是 2026-10-11 那批的形状。
        # 2026-10-23（L-20 重锚）：编号题集合已从「只有 ①」扩成
        # ``_NUMBERED_LINK_QUESTIONS``（链接选择题 + ①）⇒ 旧锚点
        # （``question == Q.SELECT_PLAY_SITES``）对不上。口径没变：
        # **该给编号的题必须给**。
        "链接题不再给编号",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "        numbered = question in _NUMBERED_LINK_QUESTIONS",
        "        numbered = False",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::Test回指编号而不是url",
    ),
    (
        # 三处窗口必须同源。改 `_render` 用回全量排序前的 obs.links，
        # 预检与渲染就又错位一次（实测：预检放行、模型收到全是导航）。
        "① 渲染窗口不再同源",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "        window = _link_window(obs, question, target_title)\n        # ① 题给每条链接编号",
        "        window = list(obs.links)\n        # ① 题给每条链接编号",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::Test回指编号而不是url",
    ),
    (
        # 站点首页核：删掉之后 hao123 又会走完整条访问链，
        # 落进 no_play_control——「找错站」与「站看不了」混成一条。
        "① 站点首页不再剔除",
        "trajectory_pipeline/executor/orchestrator.py",
        "    kept: list[Candidate] = []\n    dropped: list[Candidate] = []\n    for candidate in record.candidates:\n        (dropped if _is_site_homepage(candidate.url) else kept).append(candidate)",
        "    kept: list[Candidate] = []\n    dropped: list[Candidate] = []\n    kept = list(record.candidates)",
        "trajectory_pipeline/tests/unit/test_visit.py::Test站点首页核",
    ),
    (
        # 红线「不保存 raw CoT」的落盘点。去掉 strip_think，推理模型的
        # 思考段落就写进 evidence——那是 SiteOutcome 的持久字段，直落 P1。
        # 这条曾经漏了很久而门禁全绿，因为 fake 的 think 与 prose 组合
        # 不出这个形态（已一并修好 fake，否则这条守不住）。
        # 2026-10-11：strip_think 在 _excerpt 与 _why_unparsable 各一处，无上下文
        # 的锚点会被 replace(...,1) 打在第一处 _excerpt，而测试打的是
        # _why_unparsable——变异落不到靶心，整条 ALIVE。锚点用 `return (`
        # 唯一锁定 _why_unparsable。
        "raw CoT 不再从evidence剥掉",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "    clean = schema_parse.strip_think(raw).strip()\n    if not clean:\n        return (",
        "    clean = raw.strip()\n    if not clean:\n        return (",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::Test不落rawCoT",
    ),
    (
        # C-13（owner 2026-10-23 裁定 D-C4：合并等价类**不需 owner 签**，
        # 但「无人记账」没被裁掉）：把 trailer_suspect 挪去别的等价类而不
        # 补注记 ⇒ 合并记账检查必须报出问题。
        # 形态选「挪标签」而不是「改注记」：后者改的是记账本身，
        # 守的是「记账说得对不对」；这里守的是「表动了、账没跟着动」。
        "等价类合并不再记账",
        "trajectory_pipeline/evaluation/golden/alignment.py",
        '    "trailer_suspect": "TRAILER",',
        '    "trailer_suspect": "NOT_PLAY_SITE",',
        "trajectory_pipeline/tests/contract/test_calibration_provenance.py::test_等价类合并记账与表逐标签对齐",
    ),
    (
        # C-10（D-C7 = rubric 变更强制连带改 prompt）：改锚点表而不改
        # rubric 文档 ⇒ 「表与文档逐档对齐」必须红。
        # 这条守的正是 D-C7 的核心：改档不改 anchor 是个**错误**，
        # 而不只是疏忽。
        "锚点表与rubric文档脱钩",
        "trajectory_pipeline/evaluation/scorers/rubric_version.py",
        '        5: "判对，且选中/排除理由引用了观察中的具体依据",',
        '        5: "判对（改了个说法，但没改口径）",',
        "trajectory_pipeline/tests/contract/test_rubric_traceability.py::test_锚点表与rubric文档逐档对齐",
    ),
    (
        # C-14（D-C6：阈值调整不算 rubric 级变更）：把取值改回字面量
        # ⇒ 「表里那条确实被用到」必须红（改线只改表一处）。
        "阈值改回各写一份字面量",
        "trajectory_pipeline/evaluation/calibration/triggers.py",
        "T2_HARD_LINE = thresholds.value(\"t2_hard_kappa\")",
        "T2_HARD_LINE = 0.70",
        "trajectory_pipeline/tests/contract/test_calibration_provenance.py::test_阈值表每条都被声明的使用处真的引用 "
        "trajectory_pipeline/tests/contract/test_calibration_provenance.py::test_比率型阈值不在使用模块里留字面量",
    ),
    (
        # C-14 的机械证明：阈值与 rubric 版本**互不依赖**。让阈值表
        # import rubric_version ⇒ 「阈值不是 rubric 级变更」这条
        # 不变式在依赖图上就破了。
        "阈值表引入rubric版本依赖",
        "trajectory_pipeline/evaluation/scorers/thresholds.py",
        "from typing import Any",
        "from typing import Any\n\nfrom trajectory_pipeline.evaluation.scorers import rubric_version",
        "trajectory_pipeline/tests/contract/test_calibration_provenance.py::test_阈值表与rubric版本互不依赖",
    ),
    (
        # C-11：pred 出处不带版本 ⇒ 分歧卡答不出「旧 prompt 还是新
        # prompt 判的」，而这件事**看起来毫无异常**。
        "pred出处不再带版本",
        "trajectory_pipeline/evaluation/calibration/snapshot.py",
        '    return f"{kind}@{version or PRED_VERSION_UNRECORDED}"',
        "    return kind",
        "trajectory_pipeline/tests/contract/test_calibration_provenance.py::test_pred_provider带版本",
    ),
    # ── 面感知 + 版本级台账（方案 16 C-11 补齐：两个尾巴）─────────
    (
        # 采集侧 pred（D-10 的 ``code.branch``）不是评分器的产物 ⇒
        # 可比性**不适用**（永远 None）。把它判成「可比」⇒ 报表上
        # 会出现「采集侧 pred 与当前评分面一致」这种**根本没问过**的话。
        "采集侧pred被当成可比",
        "trajectory_pipeline/evaluation/calibration/disagreement.py",
        "    face = str(card.get(\"pred_face\") or \"\")\n"
        "    if face == FACE_COLLECTION:\n"
        "        return None",
        "    face = str(card.get(\"pred_face\") or \"\")\n"
        "    if face == FACE_COLLECTION:\n"
        "        return True",
        "trajectory_pipeline/tests/contract/test_provenance_registry.py::test_采集侧pred比不了且理由是不适用",
    ),
    (
        # 「已知不可比」（重算证明当前该面复现不出这批读数）被降级成
        # 「不可判定」⇒ 报表上「不可比」这一档**消失**，全变成「答不出来」
        # ——而后者是**弱得多**的结论。
        "已知不可比被降级成不可判定",
        "trajectory_pipeline/evaluation/calibration/disagreement.py",
        '    if str(card.get("pred_face_status") or "") == STATUS_DIVERGED:',
        '    if str(card.get("pred_face_status") or "") == "__never_diverged__":',
        "trajectory_pipeline/tests/contract/test_provenance_registry.py::test_复现不出判不可比而不是不可判定",
    ),
    (
        # 版本不同版 ⇒ 不可比。这条挡的是「一律判可比」：拿旧读数跟新读数
        # 对账，得到的会是**假的「评分器变差了」**。
        "不同版也被当成可比",
        "trajectory_pipeline/evaluation/calibration/disagreement.py",
        '    if pred in ("", PRED_VERSION_UNRECORDED) or cur == "":\n'
        "        return None\n"
        "    return pred == cur",
        '    if pred in ("", PRED_VERSION_UNRECORDED) or cur == "":\n'
        "        return None\n"
        "    return True",
        "trajectory_pipeline/tests/contract/test_provenance_registry.py::test_代码面异版不可比",
    ),
    (
        # 台账说「当前该面复现得出来」⇒ 该给出版本。拿掉 ⇒ 存量冻结档的
        # 出处永远补不上（「补齐」这件事就白做了），而**看不出来**。
        "台账复现结论不补出版本",
        "trajectory_pipeline/evaluation/calibration/snapshot.py",
        '        if status == "reproduced":\n'
        '            face_version = str(rec.get("version") or VERSION_UNRECORDED)\n'
        "            face_status = status",
        '        if status == "reproduced":\n'
        "            face_version = VERSION_UNRECORDED\n"
        "            face_status = status",
        "trajectory_pipeline/tests/contract/test_provenance_registry.py::test_存量行靠台账补出代码面版本",
    ),
    (
        # 重算取证的核心：**逐维比对**。比对判据一改（恒真）⇒ 所有行都
        # 变成「一致」⇒ 台账会把 v6/v7 那些**真不一致**的版本写成
        # ``reproduced`` ⇒ 「已知不可比」这一档整个消失。
        "重算比对恒真",
        "trajectory_pipeline/evaluation/golden/builder.py",
        "            if before == after:\n"
        '                per_dim[dim]["compared"] += 1\n'
        '                per_dim[dim]["matched"] += 1\n'
        "                continue",
        "            if True:\n"
        '                per_dim[dim]["compared"] += 1\n'
        '                per_dim[dim]["matched"] += 1\n'
        "                continue",
        "trajectory_pipeline/tests/contract/test_provenance_registry.py::test_重算逐行比对能判出复现与分歧",
    ),
    (
        # 「不猜」的最后一道：认不出的维度**不许**被塞进某一面。
        # 塞进代码面 ⇒ 批次级维度（d7/d8）会拿代码面版本去比，
        # 报出一个**没人问过**的可比性结论。
        "认不出的维度被塞进代码面",
        "trajectory_pipeline/evaluation/calibration/provenance.py",
        '    return DIM_FACES.get(str(dimension or ""), FACE_UNKNOWN)',
        '    return DIM_FACES.get(str(dimension or ""), FACE_CODE)',
        "trajectory_pipeline/tests/contract/test_provenance_registry.py::test_批次级维度不落任何一面",
    ),
    (
        # **接线**：可比性三态不写进卡 ⇒ 卡上那一栏永远是「答不出来」，
        # 而函数算得对也没人看得到 ⇒ **存量出处的答案等于没答**。
        # 这个坑本仓库踩过：一个没生效的检查和一个真红长得一样。
        "可比性结论不写进卡",
        "trajectory_pipeline/evaluation/calibration/disagreement.py",
        '        card["pred_comparability"] = pred_comparability(card)',
        '        card["pred_comparability"] = None',
        "trajectory_pipeline/tests/contract/test_provenance_registry.py::test_可比性结论写在卡上不是只在函数里",
    ),
    (
        # **接线**：理由不写进卡 ⇒ 三态是**半份答案**——读的人分不出
        # 「已知不可比」与「压根没记到」。
        "可比性理由不写进卡",
        "trajectory_pipeline/evaluation/calibration/disagreement.py",
        '        card["pred_comparability_reason"] = pred_comparability_reason(card)',
        '        card["pred_comparability_reason"] = ""',
        "trajectory_pipeline/tests/contract/test_provenance_registry.py::test_可比性结论写在卡上不是只在函数里",
    ),
    (
        # 台账取证里**行数**与**指纹数**混成一个数 ⇒ 「500 行记了同一版」
        # 被读成「只有 1 行记了」。v9 起行里真会记版本，这个错会活过来。
        "行数被填成指纹数",
        "trajectory_pipeline/evaluation/golden/builder.py",
        '                    "rows_with_scorer_version": rows_recorded,',
        '                    "rows_with_scorer_version": len(recorded),',
        "trajectory_pipeline/tests/contract/test_provenance_registry.py::test_登记台账时行数与指纹数分开记",
    ),
    (
        # 可比性**停在卡上**没走到工作表 ⇒ 人判桶时看不到「不是同一版
        # 判的」⇒ 结论到不了该看它的人眼前。
        "可比性没走到工作表",
        "trajectory_pipeline/evaluation/calibration/worklist.py",
        '                "pred_comparability": row.get("pred_comparability"),',
        '                "pred_comparability": None,',
        "trajectory_pipeline/tests/contract/test_provenance_registry.py::test_可比性一路走到人看的那张表",
    ),
]


def run(selector: str) -> tuple[int, str]:
    # 选择器按空白拆成多个 argv——一条变异可能由**两个**测试守着
    # （例：阈值改回字面量，一个测试管「表被引用」，另一个管「不留字面量」）。
    # 不拆的话 pytest 会收到一个带空格的参数 ⇒ 退出码 4（usage error），
    # 而 4 会被读成「没验成」而不是「验红了」。
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *selector.split(), "-q", "--no-header", "-x"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()]
    return r.returncode, (tail[-1] if tail else "")


def _read(path: Path) -> tuple[str, str]:
    """读文件 → ``(用于匹配的文本, 原行尾)``。

    匹配文本统一成 ``\\n``（``MUTATIONS`` 里的锚点是 ``\\n`` 写的）；
    **行尾单独记下来**，写回时照原样还回去。

    为什么不用 ``read_text``/``write_text``：那两个走 Python 的通用换行
    转换——``read_text`` 把 CRLF 读成 LF，``write_text`` 又在 Windows 上把
    LF 写回成 CRLF。合起来的后果是：**一个 LF 文件被这个工具碰过一次，
    行尾就变成了 CRLF**，而 git 会把它记成一次真实改动。仓库是逐文件
    统一行尾的（LF / CRLF 混用），这种"顺手改掉行尾"最容易被当成
    「代码变了」而混进提交。
    """
    raw = path.read_bytes()
    eol = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8").replace("\r\n", "\n"), eol


def _write(path: Path, text: str, eol: str) -> None:
    """写回，**行尾与原文件一致**（见 :func:`_read` 的理由）。"""
    out = text.replace("\n", eol) if eol != "\n" else text
    path.write_bytes(out.encode("utf-8"))


def _drop_pyc(path: Path) -> int:
    """删掉被改模块的 ``.pyc``。**这条是工具正确性的前提，不是清洁工作。**

    实测（2026-10-10）：「铁律1·确定性优先」同一条变异**两次运行一次 RED
    一次 ALIVE**——写回校验显示变异确实落盘了，而被测进程仍跑的是旧代码。

    原因是字节码缓存：``write_text`` 之后源文件 mtime 的粒度可能与
    ``__pycache__`` 里记的一致，CPython 判定「没变」直接复用 ``.pyc``。
    于是**测试跑的是未变异的模块**，全绿，而报表会把它记成
    「这条守卫无效」——**一次没生效的变异被读成「守卫不需要」**。

    只删**被改文件**对应的缓存：全量删会让整份清单慢一个量级，
    而精度要求只落在真正被变异的那几个模块上。
    """
    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return 0
    stem = path.stem
    gone = 0
    for pyc in cache.glob(f"{stem}.*.pyc"):
        try:
            pyc.unlink()
            gone += 1
        except OSError:
            pass
    return gone


def apply(path: Path, src: str, old: str, new: str, eol: str = "\n") -> bool:
    """写入变异、**清掉字节码缓存**、再读回校验。

    读回校验防「替换没生效」；:func:`_drop_pyc` 防「生效了但被测进程
    读的是缓存」。两者缺一，整份清单的结论都不可信，而报表上
    只会显示一个安静的「ALIVE」。
    """
    mutated = src.replace(old, new, 1)
    if mutated == src:
        return False
    _write(path, mutated, eol)
    _drop_pyc(path)
    # 读回。子进程稍后启动会读到它，但**写与读之间存在窗口**，
    # 与其相信窗口，不如当场核一次。
    return new in path.read_text(encoding="utf-8")


def main() -> int:
    survivors = []
    for label, rel, old, new, selector in MUTATIONS:
        path = ROOT / rel
        src, eol = _read(path)
        if old not in src:
            print(f"[SKIP] {label}: 锚点未找到（代码已变）")
            survivors.append(label)
            continue
        landed = apply(path, src, old, new, eol)
        if not landed:
            _write(path, src, eol)
            print(
                f"[NOOP] {label}: 变异未落盘——**不算「守卫有效」，也不算"
                f"「守卫无效」，这条本次没验成**"
            )
            survivors.append(label)
            continue
        try:
            code, tail = run(selector)
        finally:
            _write(path, src, eol)
            _drop_pyc(path)
        if code in _NO_TEST_EXIT_CODES:
            # 2026-10-23（L-20 重锚时读出）：pytest 对「选择器指向不存在的
            # 测试」退出码是 **4**（usage error），对「一个也没选中」是 **5**。
            # 两者都**非 0** ⇒ 只看 `died = code != 0` 会把它们**读成 RED**
            # ⇒ 一条**选择器早就漂了**的变异会**假红**、被记成「守卫有效」。
            # 实测：`TestSelectPlaySitesWiring` 那个类早已改名
            # `TestClassifyCandidatesWiring`，旧选择器一直报
            # "no tests ran in 0.11s"——**没有任何断言失败**，却算红。
            # ⇒ 这里单独判：**没选中测试 = 没验成**，既不算有效也不算无效。
            print(
                f"[BAD-SEL] {label}: 选择器没选中任何测试"
                f"（pytest exit={code}）——**这条没验成**，"
                f"不算「守卫有效」也不算「守卫无效」"
            )
            survivors.append(label)
            continue
        died = code != 0
        crashed = "error" in tail
        verdict = "CRASH" if crashed else ("RED " if died else "ALIVE")
        # error 而非 failed = 变异把代码改崩了，不是守卫在起作用。
        # 崩了也算红，但它不证明这条断言有效，必须单独标出来。
        print(f"[{verdict}] {label}: {tail}")
        if crashed:
            print(f"    ^ 变异引入了崩溃而非断言失败，这条不算有效验证")
        if not died:
            survivors.append(label)

    print("\n" + "=" * 60)
    if survivors:
        print(f"FAIL {len(survivors)}/{len(MUTATIONS)} 条变异存活（守卫无效）:")
        for s in survivors:
            print(f"   - {s}")
        return 1
    print(f"PASS {len(MUTATIONS)}/{len(MUTATIONS)} 条变异全部变红——每条守卫都真的在拦")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
