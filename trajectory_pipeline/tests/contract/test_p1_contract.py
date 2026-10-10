"""P1 字段级契约——**文档与代码必须同步**，不同步就红。

为什么需要这条
--------------

2026-10-09 那轮的教训：``run_config`` / ``elements_total`` / ``links_total`` /
``site_url`` / ``play_page_url`` 五个字段落地时，全部门禁是绿的
（``TestDegradedMarkersMatchSplit`` 只盯**缺件标记**，而新增字段
压根不产生缺件标记），而 ``docs/contracts/P1-observation.md`` 还是空的。

**没有测试盯着的文档就是没有**——它给出的「已覆盖」的假象比没有文档更坏：
读的人会以为契约已被核对过。

所以这里做三件事：
1. **字段集** 与 ``RunRecord.to_json()`` / ``_obs_json()`` /
   ``SiteOutcome.to_json()`` 的实际返回键**逐个相等**（不多不少）。
2. **文档里提到的每个字段名都真存在**——防「文档写了但代码没有」。
3. **真实存档逐份核对**（有则跑，无则跳过并点名缺什么）。

多一条口径：字段名出现次数 > 1 的必须能被 grep 定位，
所以这里也用 AST 读源码而不是 import 出来的 dict——
**import 会把契约测试绑死在当前实现上**，而这里要守的恰恰是
「实现改了文档没改」。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from trajectory_pipeline.assembler import observation_view, schema
from trajectory_pipeline.executor import branches, orchestrator

REPO = Path(__file__).resolve().parents[3]
ORCH_SRC = REPO / "trajectory_pipeline" / "executor" / "orchestrator.py"
BRANCH_SRC = REPO / "trajectory_pipeline" / "executor" / "branches.py"
DOC = REPO / "trajectory_pipeline" / "docs" / "contracts" / "P1-observation.md"


def _keys_of(path: Path, func_name: str) -> set[str]:
    """取某个 ``to_json`` 里 ``return {…}`` 的键集。**AST 读，不执行**。

    执行 ``to_json()`` 要构造全套值对象，而这里要守的恰恰是
    「实现改了而文档没改」——import 出来的 dict 是**当前实现**，
    构造它需要的东西比这份契约测试该有的依赖多得多。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Dict):
                    out: set[str] = set()
                    for k in stmt.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            out.add(k.value)
                    if out:
                        return out
    raise AssertionError(f"{path.name} 里读不到 {func_name} 的返回字典")


# ── 字段集：以实现为准 ────────────────────────────────────────────


class Test字段集一致:
    def test_顶层字段与文档一致(self):
        """顶层键集合。**不多不少**——多一个没写进文档的字段，
        读的人就会漏看它；少一个则说明文档写了不存在的东西。"""
        actual = _keys_of(ORCH_SRC, "to_json")
        expected = {
            "task_id", "title", "query", "user_prompt", "search_url",
            "perceptor", "candidate_source", "result_titles", "search_blocked",
            "candidate_filter", "run_config", "provenance", "elapsed_ms",
            "warnings", "search_observation", "steps", "candidates",
            "visits", "ledger", "outcomes",
        }
        assert actual == expected, (
            f"顶层字段漂移：多 {actual - expected} / 少 {expected - actual}。"
            f"改动请同步 docs/contracts/P1-observation.md"
        )

    def test_观察字段与文档一致(self):
        actual = _keys_of(ORCH_SRC, "_obs_json")
        expected = {
            "url", "page_title", "body_text", "body_len", "body_source",
            "truncated", "raw_len", "stripped_ratio", "degraded",
            "video_tag_count", "iframe_count", "elements_total",
            "links_total", "interactive_elements", "links",
        }
        assert actual == expected, (
            f"观察字段漂移：多 {actual - expected} / 少 {expected - actual}"
        )

    def test_Outcome字段与文档一致(self):
        actual = _keys_of(BRANCH_SRC, "to_json")
        expected = {
            "url", "site_url", "play_page_url", "branch", "branch_label",
            "evidence", "question", "source", "fallback_used",
            "reached_play_page", "is_negative_sample",
        }
        assert actual == expected, (
            f"outcome 字段漂移：多 {actual - expected} / 少 {expected - actual}"
        )

    def test_Visit字段与文档一致(self):
        """``visits[]`` 的元素是 orchestrator 里就地构造的字面量，
        不是独立的 ``to_json``——所以按源码里那个 dict 的键来核。"""
        actual = set(_keys_of(ORCH_SRC, "to_json"))
        # visits 的键嵌在 to_json 的返回值里，取它的子 dict
        tree = ast.parse(ORCH_SRC.read_text(encoding="utf-8"))
        visit_keys: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = {k.value for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if {"url", "landed_url", "success", "site_obs"} <= keys:
                    visit_keys = keys
                    break
        assert visit_keys, "读不到 visits[] 的字典字面量"
        expected = {"url", "landed_url", "success", "notes", "steps",
                    "site_obs", "player_obs"}
        assert visit_keys == expected, (
            f"visit 字段漂移：多 {visit_keys - expected} / 少 {expected - visit_keys}"
        )


# ── 文档提到的东西必须真存在 ──────────────────────────────────────


class Test文档不是摆设:
    """文档里**提到**的每个字段名都必须在源码里真的出现。

    方向与上一组相反：那一组抓「代码有文档没有」，这一组抓
    「文档写了代码没有」——**文档债的两种形态都要能被发现**。
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def doc_text():
        assert DOC.exists(), f"{DOC} 不存在——P1 契约文档是本模块存在的理由"
        return DOC.read_text(encoding="utf-8")

    @staticmethod
    @pytest.fixture(scope="class")
    def code_text():
        return (ORCH_SRC.read_text(encoding="utf-8")
                + BRANCH_SRC.read_text(encoding="utf-8")
                + (REPO / "trajectory_pipeline" / "executor" / "archive.py")
                .read_text(encoding="utf-8"))

    def test_文档非空且不是占位(self, doc_text):
        assert len(doc_text) > 2000, "契约文档短得不像话——多半是占位"
        assert "待写" not in doc_text.replace("P2 契约 / P3 契约 —— 待写", ""), \
            "P1 契约里还留着「待写」占位"

    #: 文档正文里点名的顶层与嵌套字段。逐个在源码里核。
    NAMED = (
        "task_id", "title", "query", "user_prompt", "search_url",
        "perceptor", "candidate_source", "result_titles", "search_blocked",
        "candidate_filter", "run_config", "provenance", "elapsed_ms",
        "warnings", "search_observation", "steps", "candidates", "visits",
        "ledger", "outcomes",
        "url", "page_title", "body_text", "body_len", "body_source",
        "truncated", "raw_len", "stripped_ratio", "degraded",
        "video_tag_count", "iframe_count", "elements_total", "links_total",
        "interactive_elements", "links",
        "landed_url", "success", "notes", "site_obs", "player_obs",
        "site_url", "play_page_url", "branch", "branch_label", "evidence",
        "question", "source", "fallback_used", "reached_play_page",
        "is_negative_sample",
    )

    @pytest.mark.parametrize("name", NAMED, ids=lambda n: n)
    def test_文档点名的字段在源码里存在(self, name, doc_text, code_text):
        assert f"`{name}`" in doc_text, f"字段 {name} 不在文档里"
        assert f'"{name}"' in code_text, (
            f"文档写了 {name}，但源码里找不到它——文档已过期")

    def test_九条分支都在文档里点名(self, doc_text):
        """漏一条分支比字段漂移更贵：它会让人以为「那几支没出现
        是因为它们不存在」。"""
        for name in branches.NON_SAMPLE_BRANCHES:
            assert f"`{name}`" in doc_text, f"非样本分支 {name} 没写进契约"

    def test_门禁三档码都在文档里(self, doc_text):
        """P1 契约要告诉读的人「缺什么字段判什么档」，
        而 DEGRADED 码是与 assembler 的 ``degraded_from`` 共享的
        ——漏写一个，两张表就分叉了。"""
        from trajectory_pipeline.executor.integrity import degraded_marker_map
        for code, marker in degraded_marker_map().items():
            assert f"`{marker}`" in doc_text, (
                f"降级标记 {marker}（门禁码 {code}）没写进契约")

    def test_批次边界警告在文档里(self, doc_text):
        """``player_unverified`` 不回填历史，跨批次不可比——
        这条忘了写，半年后就会有人把 ``unresolved`` 骤降读成质量变好。"""
        assert "player_unverified" in doc_text
        assert "不回填" in doc_text


# ── 真实存档逐份核对 ──────────────────────────────────────────────


def _real_archives() -> list[Path]:
    root = REPO / "trajectory_pipeline" / "output" / "pipeline"
    found = [p for p in sorted(root.glob("*.json"))
             if "__" in p.stem and not p.name.endswith(".reviewed.json")]
    if not found:
        pytest.skip("没有真实 P1 存档（output/pipeline/ 是产物目录，未入库）")
    return found


class TestOnRealArchives:
    """对着**真实存档**核，而不是对着自造的 fixture。

    fixture 是我们自己造的，于是规则和 fixture 一起错、测试照样绿
    （CLAUDE.md 明写的那条）。真存档里有 fixture 造不出来的东西：
    http→https 跳转、缺键的老存档、`is_negative_sample` 与
    ``success`` 对不上的组合。
    """

    def test_真实存档字段与契约一致(self):
        """每份存档的顶层键都落在契约声明的集合内（老存档可能缺新增
        字段，但**不能有契约之外的多余字段**——那是另一条漂移）。"""
        expected = _keys_of(ORCH_SRC, "to_json")
        seen: set[str] = set()
        for p in _real_archives():
            doc = json.loads(p.read_text(encoding="utf-8"))
            extra = set(doc) - expected
            assert not extra, f"{p.name} 出现契约之外的顶层字段：{extra}"
            seen |= set(doc)
        # 至少覆盖到契约里的一大半字段，否则这条断言在残档上也是空的
        assert len(seen) >= len(expected) - 3, (
            f"真实存档只覆盖了 {len(seen)}/{len(expected)} 个顶层字段，"
            f"契约里这批数据没验证到的部分：{expected - seen}")

    def test_真实存档切得出的样本形状与契约一致(self):
        """跑一遍真的 ``split_archive``——契约说的六件套与
        ``assembler.schema.Sample.to_json`` 说的要一致。"""
        six = None
        for p in _real_archives():
            for s in schema.split_archive(p):
                six = s.to_json()["six"]
                break
            if six:
                break
        assert six is not None, "一份样本都切不出来——档全坏了？"
        assert set(six) == {
            "system_prompt", "tools", "user_prompt", "provenance",
            "actions", "observations", "rationale",
        }, f"六件套形状与契约不符：{sorted(six)}"

    def test_真实存档的观察能过训练态渲染(self):
        """训练态渲染器要能吃下真存档里的观察——
        而真观察里带 ``ref``（P1 有、P2 没有），是渲染器要剥掉的那一半。"""
        from trajectory_pipeline.assembler.builder import _sample_to_json
        from trajectory_pipeline.assembler.schema import split_archive

        checked = 0
        for p in _real_archives():
            samples = split_archive(p)
            if not samples:
                continue
            blob = json.dumps(_sample_to_json(samples[0]), ensure_ascii=False)
            assert '"ref"' not in blob, (
                f"{p.name} 的 P2 里漏了 ref——会话句柄不能进训练数据")
            checked += 1
            if checked >= 5:
                break
        assert checked, "一份样本都切不出来"
