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
        "            guard = \"\"\n            if guard:",
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
        "            verdict = classify_trailer(label)\n            if verdict == \"only\":",
        "            verdict = \"none\"\n            if verdict == \"only\":",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::TestTrailerRuleWins",
    ),
    (
        "解析取外层不取内层",
        "trajectory_pipeline/llm/schema_parse.py",
        "    for i, ch in enumerate(clean):\n        if ch != \"{\":",
        "    for i, ch in reversed(list(enumerate(clean))):\n        if ch != \"{\":",
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
        "            record.candidate_source = \"heuristic\"\n            record.warnings.append(\n"
        "                f\"判断点 ① 未取得结论（{selection.evidence}）——\"\n"
        "                f\"候选仍按代码启发式全遍历，不中断采集\"\n            )",
        "            record.candidate_source = \"heuristic\"\n            record.candidates = []\n            record.warnings.append(\n"
        "                f\"判断点 ① 未取得结论（{selection.evidence}）——已中断\"\n            )",
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
        "            \"not_play_site\",\n            f\"判断点 ① 排除该站点。{why}\" if why",
        "            \"unreachable_hard\",\n            f\"判断点 ① 排除该站点。{why}\" if why",
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
        "            for l in _ordered_links(obs, question, target_title)[:LLM_LINKS]",
        "            for l in obs.links[:LLM_LINKS]",
        "trajectory_pipeline/tests/unit/test_llm_perceptor.py::Test预检与渲染同源",
    ),
    (
        "①写回标题用双键查找",
        "trajectory_pipeline/executor/orchestrator.py",
        "                    dataclasses.replace(link, text=search_step.title_for(titles, link.href)\n                                        or link.text)",
        "                    dataclasses.replace(link, text=titles.get(link.href)\n                                        or link.text)",
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
        "            result = await self._visit_guarded(candidate, ledger, record)\n            await self._write_rationales(result, record)",
        "            result = await self._visit_guarded(candidate, ledger, record)",
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
        '        draft = payload',
        "trajectory_pipeline/tests/unit/test_assembler.py::TestRationale对齐",
    ),
    (
        "闸门全判通过",
        "trajectory_pipeline/assembler/schema.py",
        '        if res.status == "failed":',
        '        if False:',
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
        "    if decision.payload.get(\"paywall\"):\n        return questions.PAY_WALL",
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
        '    if error:',
        '    if False:',
        "trajectory_pipeline/tests/unit/test_writer.py::Test动作失败进提示词",
    ),
]


def run(selector: str) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", selector, "-q", "--no-header", "-x"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
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
            print(f"[NOOP] {label}: 变异未落盘——**不算「守卫有效」，也不算"
                  f"「守卫无效」，这条本次没验成**")
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