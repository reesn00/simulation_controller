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
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: ``tests/`` 的上一级是包根，上上级是仓库根。
ROOT = Path(__file__).resolve().parents[2]

#: (标签, 相对路径, 原片段, 变异片段, 应当变红的测试选择器)
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
        "铁律1·确定性优先",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "            if question == Q.PLAYER_OK and obs.video_tag_count >= 1:",
        "            if False and question == Q.PLAYER_OK and obs.video_tag_count >= 1:",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::TestDeterministicFirst",
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
        "①None不中断采集",
        "trajectory_pipeline/executor/orchestrator.py",
        '            record.candidate_source = "heuristic"\n            record.warnings.append(\n'
        '                f"判断点 ① 未取得结论（{selection.evidence}）——"\n'
        '                f"候选仍按代码启发式全遍历，不中断采集"\n            )',
        '            record.candidate_source = "heuristic"\n            record.candidates = []\n            record.warnings.append(\n'
        '                f"判断点 ① 未取得结论（{selection.evidence}）——已中断"\n            )',
        "trajectory_pipeline/tests/unit/test_visit.py::TestSelectPlaySitesWiring::test_未取得结论不中断采集",
    ),
    (
        "①无证据不记负样本",
        "trajectory_pipeline/executor/orchestrator.py",
        "        named = key in rejected or candidate.url in rejected\n        if not named:\n            undecided += 1",
        "        named = True\n        if not named:\n            undecided += 1",
        "trajectory_pipeline/tests/unit/test_visit.py::Test无证据不记负样本",
    ),
    (
        "①落选分支名正确",
        "trajectory_pipeline/executor/orchestrator.py",
        '            "not_play_site",\n            f"判断点 ① 排除该站点。{why}"\n            if why',
        '            "unreachable_hard",\n            f"判断点 ① 排除该站点。{why}"\n            if why',
        "trajectory_pipeline/tests/unit/test_visit.py::Test无证据不记负样本",
    ),
    (
        "①按原始href匹配",
        "trajectory_pipeline/executor/orchestrator.py",
        "        key = candidate.source_href or candidate.url",
        "        key = candidate.url",
        "trajectory_pipeline/tests/unit/test_visit.py::TestSelectPlaySitesWiring::test_按原始href匹配而非解包后",
    ),
    (
        "工厂auto无配置退回规则版",
        "trajectory_pipeline/perception/factory.py",
        "        return RulePerceptor()\n\n    if client is None:",
        "        raise LLMUnavailable('no backend')\n\n    if client is None:",
        "trajectory_pipeline/tests/unit/test_factory.py::TestModeSelection",
    ),
    (
        "题目写进请求正文",
        "trajectory_pipeline/perception/llm_perceptor.py",
        '        user = json.dumps({"question": spec.id, "observation": rendered},',
        '        user = json.dumps({"observation": rendered},',
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
    (
        "①无证据不记负样本",
        "trajectory_pipeline/executor/orchestrator.py",
        "        named = key in rejected or candidate.url in rejected",
        "        named = False",
        "trajectory_pipeline/tests/unit/test_visit.py::Test无证据不记负样本",
    ),
    # ── 模块 4 接线（2026-10-10）────────────────────────────────
    (
        "编排器不挂writer",
        "trajectory_pipeline/executor/orchestrator.py",
        "            result = await self._visit_guarded(candidate, ledger, record)\n            await self._write_rationales(\n                result.steps, record, result.candidate.url[:60]\n            )",
        "            result = await self._visit_guarded(candidate, ledger, record)",
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
        "付费墙同页守卫",
        "trajectory_pipeline/executor/branches.py",
        '    if branch in ("component_unverified", "subscription_wall"):',
        '    if branch == "component_unverified":',
        "trajectory_pipeline/tests/unit/test_branches.py::TestBranchPrecondition",
    ),
    (
        "付费墙只在模型判否时升级",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "        paywall = bool(answer is False and hit)",
        "        paywall = bool(hit)",
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
        "失败原因不进提示词",
        "trajectory_pipeline/rationale/writer.py",
        "    if error:",
        "    if False:",
        "trajectory_pipeline/tests/unit/test_writer.py::Test动作失败进提示词",
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
        "③ 无控件时不再判否",
        "trajectory_pipeline/perception/llm_perceptor.py",
        '        return self._answer(\n            spec, False,\n            evidence=(\n                f"{len(obs.interactive_elements)} 个交互元素中，"',
        '        return self._answer(\n            spec, True,\n            evidence=(\n                f"{len(obs.interactive_elements)} 个交互元素中，"',
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
        # 编号是 ① 的回指契约。去掉之后模型回 i 就全落空，
        # 而失败形态是「整批 unresolved」——正是 2026-10-11 那批的形状。
        "① 不再给链接编号",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "        numbered = question == Q.SELECT_PLAY_SITES",
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
        "raw CoT 不再从evidence剥掉",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "    clean = schema_parse.strip_think(raw).strip()",
        "    clean = raw.strip()",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::Test不落rawCoT",
    ),
]


def run(selector: str) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", selector, "-q", "--no-header", "-x"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()]
    return r.returncode, (tail[-1] if tail else "")


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


def apply(path: Path, src: str, old: str, new: str) -> bool:
    """写入变异、**清掉字节码缓存**、再读回校验。

    读回校验防「替换没生效」；:func:`_drop_pyc` 防「生效了但被测进程
    读的是缓存」。两者缺一，整份清单的结论都不可信，而报表上
    只会显示一个安静的「ALIVE」。
    """
    mutated = src.replace(old, new, 1)
    if mutated == src:
        return False
    path.write_text(mutated, encoding="utf-8")
    _drop_pyc(path)
    # 读回。子进程稍后启动会读到它，但**写与读之间存在窗口**，
    # 与其相信窗口，不如当场核一次。
    return new in path.read_text(encoding="utf-8")


def main() -> int:
    survivors = []
    for label, rel, old, new, selector in MUTATIONS:
        path = ROOT / rel
        src = path.read_text(encoding="utf-8")
        if old not in src:
            print(f"[SKIP] {label}: 锚点未找到（代码已变）")
            survivors.append(label)
            continue
        landed = apply(path, src, old, new)
        if not landed:
            path.write_text(src, encoding="utf-8")
            print(
                f"[NOOP] {label}: 变异未落盘——**不算「守卫有效」，也不算"
                f"「守卫无效」，这条本次没验成**"
            )
            survivors.append(label)
            continue
        try:
            code, tail = run(selector)
        finally:
            path.write_text(src, encoding="utf-8")
            _drop_pyc(path)
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
