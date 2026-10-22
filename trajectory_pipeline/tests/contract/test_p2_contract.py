"""模块 5 的**契约测试**——P2 必须满足什么，与谁来读它无关。

与 ``tests/unit/test_assembler.py`` 的分工：
    unit/        测某个函数在这个输入下返回什么
    contract/    测**任何**存档、任何一版 schema 都必须满足的性质

最有分量的一条是 A1（不 import executor）。它不是风格约束而是一条
**可执行的架构断言**：assembler 一旦 import executor，「重建 P2 独立于
执行端当前版本」就破了——executor 改一个字段，半年前的 P2 就重建不出来。
所以这里不是检查代码风格，是检查模块图。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from trajectory_pipeline.assembler import observation_view, schema

PKG = Path(schema.__file__).parent


def _sources() -> list[Path]:
    return sorted(p for p in PKG.glob("*.py"))


# ── A1：依赖方向 ────────────────────────────────────────────────────

class TestNoExecutorImport:
    """P2 读 P1 按**文件格式**，不 import executor。

    理由：P1 贵、P2 便宜。重建一批 P2 必须独立于 executor 的当前版本——
    一旦 import 过去，executor 改 dataclass 就可能让老 P1 切不出样本，
    而那批样本是永久损失。交接面是格式，不是 import
    （与 ``executor/plan.py`` 不 import taskgen 同一条纪律）。
    """

    def _imports(self, path: Path) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level:                    # 相对导入也算跨层
                    mod = f"{'.' * node.level}{mod}"
                out.append(mod)
        return out

    @pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
    def test_不import_executor(self, path):
        for name in self._imports(path):
            assert "trajectory_pipeline.executor" not in name, (
                f"{path.name} import 了 {name}：重建 P2 会绑死在执行端当前版本上")

    def test_不读仓库根output(self):
        """产物隔离（D8）。读了根 output/ 就继承存量那套隐性耦合。"""
        for path in _sources():
            text = path.read_text(encoding="utf-8")
            assert '"output/' not in text and "'output/" not in text, (
                f"{path.name} 里出现了根 output/ 字面量")


# ── A2：六件套齐整 ──────────────────────────────────────────────────

class TestSixPieceShape:
    SIX = ("system_prompt", "tools", "user_prompt", "provenance",
           "actions", "observations", "rationales")

    @pytest.fixture
    def sample(self, tmp_path):
        return _split(tmp_path)[0]

    def test_七件都在(self, sample):
        js = sample.to_json()["six"]
        assert set(self.SIX) == set(js), "六件套缺件或多件"

    def test_可序列化(self, sample):
        json.dumps(sample.to_json(), ensure_ascii=False)

    def test_系统提示词随样本冻结(self, sample):
        """常量改一次，历史样本之间的可比性就没了。"""
        assert sample.system_prompt == schema.SYSTEM_PROMPT
        assert sample.to_json()["six"]["system_prompt"] == schema.SYSTEM_PROMPT

    def test_闸门不是passed(self, sample):
        """夹具是**老存档**（跑在模块 4 接线之前），闸门必然是 ``not_run``。

        渲染成 ``passed`` 会让批次上线时看起来像过了闸，而它根本没被检查过。
        新存档走的是真闸门——那是 :func:`schema.gate_of` 的职责，
        这里只钉住「没有 rationale 时不得渲染成通过」。
        """
        assert sample.gate.status == "not_run"
        assert sample.gate.reasons, "not_run 必须带理由，否则与「通过」无法区分"

    def test_rationale缺失有独立标记(self, sample):
        """空元组与「模型这轮没说话」同形，所以另有标记。

        2026-10-10 起第 ⑥ 件是**与动作对齐的元组**（原为单条 ``Rationale | None``）：
        逐步生成的东西不该压成一条——压了就对不上动作，
        时序核查也拿不到 ``step_index``。
        """
        assert sample.rationales == ()
        assert sample.rationale_missing is True
        assert sample.to_json()["six"]["rationales"] == []


# ── A3：动作里没有会话句柄 ──────────────────────────────────────────

class TestNoRefInActions:
    def test_渲染后无ref(self, tmp_path):
        s = _split(tmp_path)[0]
        blob = json.dumps(s.to_json(), ensure_ascii=False)
        assert '"ref"' not in blob, "会话句柄漏进训练数据：换个 session 就失效"

    def test_点击目标是语义化的(self, tmp_path):
        site = [s for s in _split(tmp_path) if s.unit == "site-1"][0]
        click = [a for a in site.actions if a.tool == "click"]
        assert click, "夹具里就该有一个点击"
        assert set(click[0].target or {}) == {"tag", "label"}

    def test_观察视图也剥掉ref(self):
        """P1 的 ``interactive_elements`` 带 ``ref``。渲染时必须剥——
        模型看到两套编号（观察里 e5、动作里「立即播放」）会学着去对齐
        一个它无法复现的东西。"""
        out = observation_view.render({
            "url": "u", "body_text": "b",
            "interactive_elements": [{"ref": "e5", "tag": "button", "label": "播放"}],
        })
        assert out["interactive_elements"] == [{"tag": "button", "label": "播放"}]


# ── A4：训练动作空间不漂移 ──────────────────────────────────────────

class TestToolSpaceDrift:
    """``TOOL_SPECS`` 自己声明（不能 import executor），漂移由测试抓。

    漂移有两个方向，各查各的：

    - **漏了**：控制流发得出、训练空间里没有 → 模型见到一种不被允许的动作；
    - **多列了**：训练空间列了执行流从未产生过的动作 → 那类样本永远不会被
      真实运行验证过，而离线评测是绿的。
      这条**由 :mod:`trajectory_pipeline.executor.actions` 自己的注释负责**
      （收窄是刻意的，理由写在那里），这里反向查代价大而收益低。
    """

    def test_盖住执行器声明的原语表(self):
        """对着 ``executor/actions.py`` 的 ``TOOLS`` 查。

        **AST 解析而不是 import**——import 会把 assembler 与执行端焊死
        （A1 禁的就是这个），而这里要的就是「读它的字面量」这件事本身。
        顺带这条测试**不会空跑**：``TOOLS`` 是常量，任何时候都读得到。
        """
        declared = {t.name for t in schema.TOOL_SPECS}
        src = (PKG.parent / "executor" / "actions.py").read_text(encoding="utf-8")
        native = _literal_tuple(_find_assign(src, "TOOLS"))
        assert native, "读不到 executor.actions.TOOLS 的字面量（模块改名了？）"
        assert native <= declared, f"执行器会发但训练空间没声明: {native - declared}"

    def test_存档里出现过的动作都在声明内(self):
        """控制流真发过的动作，训练空间里必须都有。

        ⚠️ **当前这批真实存档里一条动作都没有**——它们跑在 ``steps[]``
        落地之前。所以这条测试现在是空跑，而**空跑的断言和没写一样**：
        它给的是「已覆盖」的假象。故 ``seen`` 为空时**跳过并点名缺什么**，
        而不是安静地通过。
        """
        declared = {t.name for t in schema.TOOL_SPECS}
        seen: set[str] = set()
        for p in _real_archives():
            for st in _walk_steps(json.loads(p.read_text(encoding="utf-8"))):
                if st.get("action", {}).get("tool"):
                    seen.add(st["action"]["tool"])
        if not seen:
            pytest.skip("现有真实存档都跑在 actions 落地之前，steps[] 为空——"
                        "重新跑一批 run --plan 之后这条才会真正生效")
        assert seen <= declared, f"存档里出现过但训练空间没声明: {seen - declared}"

    def test_执行器自己做的动作对模型不可见(self):
        """``new_tab`` / ``close_tab`` 是会话管理的实现细节。模型永远
        不该输出它们——但它们仍要留在 P1 里（删掉就无法回放），
        所以靠可见性区分。"""
        invisible = {t.name for t in schema.TOOL_SPECS if not t.visible_to_model()}
        assert invisible == {"new_tab", "close_tab"}

    def test_声明的每个动作都有说明(self):
        for t in schema.TOOL_SPECS:
            assert t.instruction.strip(), f"{t.name} 没有说明，模型无从选它"


# ── A4.5：收尾处置词表不漂移 ───────────────────────────────────────

class TestClosingDispositionDrift:
    """``views._CLOSING_DISPOSITION`` 自己声明（views 不能 import
    executor，与 A1 同一条纪律），与 ``executor.branches.BRANCH_LABELS``
    键集的同步由这条测试盯。

    词表漏一个分支不会炸——收尾总结走「不予记录（分支：…）」兜底，
    而兜底话术是给**还没进词表的新分支**的；已在词表里的分支落进
    兜底说明两张表漂移了。与 :class:`TestToolSpaceDrift` 同一形态：
    交接面是格式，漂移是测试的事。
    """

    def test_盖住分支词表(self):
        from trajectory_pipeline.assembler import views
        from trajectory_pipeline.executor import branches

        missing = set(branches.BRANCH_LABELS) - set(views._CLOSING_DISPOSITION)
        assert not missing, f"执行器有分支而收尾处置词表没有: {missing}"


# ── A5：裁剪必须留痕 ────────────────────────────────────────────────

class TestTruncationIsVisible:
    def test_本层裁剪写进limits(self):
        out = observation_view.render({
            "url": "u", "body_text": "字" * 5000,
            "interactive_elements": [], "links": [],
        })
        assert out["limits"]["body_chars"] == observation_view.VIEW_BODY_CHARS
        assert out["limits"]["cropped"] is True
        assert out["truncated"]["view"] is True
        assert len(out["body_text"]) == observation_view.VIEW_BODY_CHARS

    def test_没裁剪时不谎报(self):
        out = observation_view.render({"url": "u", "body_text": "短"})
        assert out["limits"] == {"body_chars": None, "elements": None,
                                 "links": None, "cropped": False}
        assert out["truncated"] == {"source": False, "view": False}

    def test_上游截断透传且与本层分列(self):
        """P1 的 ``truncated``（dom 层预算用尽）与本层裁剪是两件事，
        模型看到的却是同一个不完整的页面。只写自己那半就等于造出
        「页面本来是全的」这种错觉。"""
        out = observation_view.render({"url": "u", "body_text": "字" * 5000,
                                       "truncated": True})
        assert out["truncated"] == {"source": True, "view": True}

    def test_降级项原样透传(self):
        """没有 ``degraded``，「没采到」与「确实为空」在观察里完全一样，
        模型会学着在证据缺失时编结论。"""
        out = observation_view.render({"url": "u", "body_text": "b",
                                       "degraded": ["links_empty"]})
        assert out["degraded"] == ["links_empty"]

    def test_老存档的预览正文被点名(self):
        """400 字符的 ``body_preview`` 冒充全文，rationale 的实体核查会
        把落在预览外的实体判成幻觉——那不是幻觉，是**没存**。"""
        out = observation_view.render({"url": "u", "body_preview": "只有前四百字"})
        assert out["body_source"] == "body_preview_only"
        assert out["body_chars"] == 6

    def test_缺观察时不编内容(self):
        """本层永不补内容。渲染器一旦「顺手补默认标题」，它就成了
        另一个感知层，而感知层输出必须可溯源到 Observation（I1）。
        空的就是空的——空本身是信号。"""
        assert observation_view.render(None)["body_text"] == ""
        assert "observation_missing" in observation_view.render(None)["degraded"]


# ── 夹具 ────────────────────────────────────────────────────────────

def _obs(url: str, **kw) -> dict:
    base = {"url": url, "page_title": "T", "body_text": "正文",
            "body_len": 2, "body_source": "inner_text", "truncated": False,
            "raw_len": 2, "stripped_ratio": 1.0, "degraded": [],
            "video_tag_count": 0, "iframe_count": 0,
            "interactive_elements": [], "links": []}
    base.update(kw)
    return base


def _split(tmp_path) -> list[schema.Sample]:
    p = tmp_path / "T001__abc123.json"
    p.write_text(json.dumps(_archive(), ensure_ascii=False), encoding="utf-8")
    return schema.split_archive(p)


def _archive(**over) -> dict:
    steps = [{"action": {"tool": "goto", "params": {"url": "https://s.test/?q=1"},
                         "origin": "model", "target": None},
              "observation": _obs("https://s.test/?q=1"), "error": ""}]
    vsteps = [{"action": {"tool": "click", "params": {},
                          "origin": "model",
                          "target": {"tag": "button", "label": "立即播放"}},
               "observation": _obs("https://x.test/play", video_tag_count=1),
               "error": ""}]
    base = {
        "task_id": "T001", "title": "功夫", "query": "功夫 在线观看",
        "user_prompt": "有没有能看正片的 给个链接呗",
        "search_url": "https://s.test/?q=1", "perceptor": "rule",
        "candidate_source": "heuristic", "search_blocked": "",
        "candidate_filter": {}, "provenance": {"genre": "喜剧"},
        "elapsed_ms": 1, "warnings": [],
        "search_observation": _obs("https://s.test/?q=1"),
        "steps": steps,
        "candidates": [{"url": "https://x.test/movie", "text": "x",
                        "rank": 1, "host": "x.test"}],
        "visits": [{"url": "http://x.test/movie", "landed_url": "https://x.test/movie",
                    "success": True, "notes": [], "steps": vsteps,
                    "site_obs": _obs("https://x.test/movie"),
                    "player_obs": _obs("https://x.test/play", video_tag_count=1)}],
        "ledger": {"task_id": "T001", "total_sites": 1, "succeeded": 1,
                   "negative_samples": 0, "by_branch": {},
                   "missing_branches": [], "heuristic_candidates": 0},
        "outcomes": [{"url": "https://x.test/movie", "branch": None,
                      "branch_label": "", "evidence": "e", "question": "player.ok",
                      "source": "rule", "fallback_used": True,
                      "reached_play_page": True, "is_negative_sample": False}],
    }
    base.update(over)
    return base


def _walk_steps(archive: dict):
    yield from archive.get("steps") or ()
    for v in archive.get("visits") or ():
        yield from v.get("steps") or ()


def _find_assign(src: str, name: str):
    """找 ``<name> = (...)`` 的赋值节点。找不到返回 ``None``。

    带注解的 ``TOOLS: tuple[str, ...] = (...)`` 是 :class:`ast.AnnAssign`
    而不是 :class:`ast.Assign`——两种都得认，否则「读不到」会被误当成
    「模块改名了」。
    """
    for node in ast.parse(src).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            return node.value
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    return None


def _literal_tuple(node) -> set[str]:
    """解出元组字面量里的字符串元素。**不解引用、不执行**——只认字面量。"""
    if not isinstance(node, (ast.Tuple, ast.List)):
        return set()
    return {e.value for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)}


def _real_archives() -> list[Path]:
    """批次里真实跑出来的存档。**取不到就跳过**，不拿 fixture 冒充。

    ⚠️ **必须递归**（2026-10-22 补记）：这里原先是 ``glob("*.json")``
    ——只扫顶层，子目录下的批次（``dualrun/`` / ``camoufox_prod1/``）
    一条都核不到。与 ``test_p1_contract._real_archives`` 与
    ``test_element_dialect._run_records`` 是同一个漏的三个副本。

    这里要的形状比 P1 那处**宽**：P2 契约核的是
    ``split_archive`` 的输入，所以判据是「顶层有 ``run_config``
    **或** 有 ``steps``/``visits``」——老存档两种布局都见过。
    仍然按 ``run_id`` 去重。
    """
    root = Path(__file__).resolve().parents[2] / "output" / "pipeline"
    found: list[Path] = []
    seen: set[str] = set()
    for p in sorted(root.rglob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or "__" not in p.stem:
            continue
        if "run_config" not in doc and "steps" not in doc and "visits" not in doc:
            continue
        run_id = str(doc.get("run_id") or "").strip()
        if run_id:
            if run_id in seen:
                continue
            seen.add(run_id)
        found.append(p)
    if not found:
        pytest.skip("没有真实 P1 存档（output/pipeline/ 是产物目录，未入库）")
    return found


# ── P2 契约文档 ↔ 代码 双向核对 ───────────────────────────────────


DOC = PKG.parent / "docs" / "contracts" / "P2-sample.md"


class Test契约文档不是摆设:
    """与 ``test_p1_contract.py`` 的同名类同款：**两个方向都要能被发现**。

    - 方向一（代码有文档没有）：文档漏字段 ⇒ 读文档的人以为没这回事；
    - 方向二（文档写了代码没有）：文档写了实现没有 ⇒ 读文档的人按错的
      形状写消费代码，而样本本身完全正常、**没有任何报错**。

    ⚠️ 2026-10-10 之前 P2 **没有字段级契约文档**，而 `six` 的第 ⑥ 件
    在那天从单值 ``rationale`` 改成了 ``rationales`` 元组——那次变更
    如果没有文档兜底，就是「改了一处、文档与消费方各自漂移」。
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def doc_text():
        assert DOC.exists(), f"{DOC} 不存在——P2 字段级契约是模块 5 的交付物之一"
        return DOC.read_text(encoding="utf-8")

    @staticmethod
    @pytest.fixture(scope="class")
    def code_text():
        return "\n".join(p.read_text(encoding="utf-8") for p in _sources())

    def test_文档非空且不是占位(self, doc_text):
        assert len(doc_text) > 1500, "契约文档短得不像话——多半是占位"
        assert "待写" not in doc_text

    @pytest.mark.parametrize("field", [
        "sample_id", "task_id", "unit", "title", "source_archive",
        "six", "gate", "rationale_missing", "outcome", "degraded_from",
        # INC46：unit 级终局反思。它是**新键**，最容易漏的就是它——
        # 消费方按没有它写的代码不会报错，只是收尾话术悄悄退回旧形态。
        "reflection",
        # 2026-10-20 方案 15 §7 R1：工具面（方言轴）。同名工具在两个面里
        # 参数不同，面错了 = 声明与调用对不上，而**没有任何报错**。
        "tool_surface",
    ])
    def test_代码有字段文档没写(self, doc_text, field):
        """方向一：代码产出的字段必须在文档里点名。"""
        assert f"`{field}`" in doc_text, f"P2 字段 {field!r} 没写进契约"

    @pytest.mark.parametrize("key", [
        "system_prompt", "tools", "user_prompt", "provenance",
        "actions", "observations", "rationales",
    ])
    def test_七件套每件都在文档里(self, doc_text, key):
        assert f"`{key}`" in doc_text, f"六件套第 {key!r} 件没写进契约"

    def test_第六件写的是与动作对齐(self, doc_text):
        """这条最容易被漏：文档若仍写「一条 rationale」，
        消费方会按单值去读，而代码给的是元组——**读到 None 且不报错**。"""
        assert "rationales" in doc_text
        assert "对齐" in doc_text
        assert "step_index" in doc_text

    def test_闸门三档都在文档里(self, doc_text):
        for s in ("passed", "failed", "not_run"):
            assert f"`{s}`" in doc_text, f"闸门状态 {s} 没写进契约"
        assert "not_run" in doc_text and "不是「通过」" in doc_text

    def test_代码里的字段名都在文档里(self, code_text, doc_text):
        """方向二：文档提到的字段必须在源码里真的存在。"""
        import re
        for name in set(re.findall(r"`([a-z_]{4,})`", doc_text)):
            if name.startswith(("sample_", "p1_")):
                continue
            assert name in code_text, (
                f"P2 契约文档提到 {name!r}，但 assembler 源码里找不到它——"
                f"文档写了实现没有的东西")
