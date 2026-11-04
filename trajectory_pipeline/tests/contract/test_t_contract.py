"""T（per-Run 完整任务视图）的**契约测试**——任何 P1 JSON 渲染出的
三视图合一 dict 必须满足的不变式。

与 ``tests/unit/test_views.py`` 的分工：
    unit/        测某个输入下渲染成什么
    contract/    测**任何**存档都必须满足的性质、文档与代码必须同步

本模块五块（与 test_p3_contract 对称）：

1. **模块图**：T 与 P3 互不消费对方代码路径。T 不 import executor
   （重建不能依赖执行端版本）；T 不 import assembler.schema / assembler.views
   （T 是 P1 的渲染，P3 是 P2 的渲染——交接面是格式不是对象，D14.3）；
   storage.task_views 不 import executor / assembler。
2. **产物根**：``task_views_dir`` 落在 ``trajectory_pipeline/output/`` 下
   （``parents[1]`` 不是 ``parents[2]``——写错不报错、只读不到数据）。
3. **T-task-view.md ↔ 代码双向核对**：契约文档是字段级文档，
   双向守护避免「文档漏字段」/「代码加了字段文档没跟上」。
4. **常量同步**：T 自己声明的 ``_TASK_SYSTEM_PROMPT`` / ``_TASK_TOOL_SPECS``
   / ``_CLOSING_DISPOSITION`` 与 assembler/schema 与 assembler/views
   **字面字符串一致**——同款纪律盯住。
5. **形状不变量**：tool_call_id 跨 unit 稠密、infrastructure 步整步跳过、
   rationale 逐步对齐、成对性自查、closing summary 来自 outcome+ledger——
   这些是「任何 P1 存档」都成立的性质。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from textwrap import dedent

import pytest

from trajectory_pipeline.assembler import schema, task_view, views
from trajectory_pipeline.storage import (
    DEFAULT_SAMPLES_PARENT,
    DEFAULT_TASK_VIEWS_DIRNAME,
    task_views_dir,
)
from trajectory_pipeline.storage import task_views

STORAGE_PKG = Path(task_views.__file__).parent
TASK_VIEW_SRC = Path(task_view.__file__)
VIEWS_SRC = Path(views.__file__)
DOC = STORAGE_PKG.parent / "docs" / "contracts" / "T-task-view.md"


# ── 工具：AST / 字段收集 ────────────────────────────────────────────


def _sources() -> list[Path]:
    """storage/ 全部模块 + assembler/task_view.py（渲染侧最末端）。"""
    return sorted(STORAGE_PKG.glob("*.py")) + [TASK_VIEW_SRC]


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:  # 相对导入也算跨层
                mod = f"{'.' * node.level}{mod}"
            out.append(mod)
    return out


def _assigned_dict_keys(path: Path, var_name: str) -> set[str]:
    """取某个函数体内 ``<var> = {…}`` 赋值的键集。AST 读，不执行。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == var_name for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            out = {
                k.value
                for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if out:
                return out
    raise AssertionError(f"{path.name} 里读不到 {var_name} 的赋值字典")


# ── 1. 模块图：T 是 P1 的另一份渲染，不读 P2 ──────────────────────


class Test模块图:
    def test_storage不import执行端点(self):
        """storage 读 T 只依赖文件名 + json，不 import executor。
        半年前重建的一批 T，今天必须还能原样读回来。"""
        for path in sorted(STORAGE_PKG.glob("*.py")):
            for name in _imports(path):
                assert "trajectory_pipeline.executor" not in name, (
                    f"{path.name} import 了 {name}：storage 与执行端版本绑死"
                )

    def test_storage不import组装端点(self):
        """storage/task_views.py 不读 P2 / P3。重建 T 不能依赖 assembler 当前版本。"""
        for path in sorted(STORAGE_PKG.glob("*.py")):
            for name in _imports(path):
                assert "trajectory_pipeline.assembler" not in name, (
                    f"{path.name} import 了 {name}：storage 与渲染端版本绑死"
                )

    def test_task_view不import执行端点(self):
        """D14.3 · T 不读 executor——交接面是格式不是对象。"""
        for name in _imports(TASK_VIEW_SRC):
            assert "trajectory_pipeline.executor" not in name, (
                f"task_view.py import 了 {name}：重建 T 不能依赖执行端当前版本"
            )

    def test_task_view不读P2(self):
        """D14.3 强化：T 不 import assembler.schema（连 SYSTEM_PROMPT
        与 TOOL_SPECS 都自己拷）——T 是 P1 的渲染，不读 P2。"""
        for name in _imports(TASK_VIEW_SRC):
            assert "trajectory_pipeline.assembler.schema" not in name, (
                f"task_view.py import 了 {name}：T 应当不读 P2，连常量都自己声明"
            )

    def test_task_view不import装配views(self):
        """T 与 P3 互不消费对方代码路径（D14.6）。"""
        for name in _imports(TASK_VIEW_SRC):
            assert "trajectory_pipeline.assembler.views" not in name, (
                f"task_view.py import 了 {name}：T 是 P1 的另一份渲染，与 P3 互不消费"
            )

    def test_projection地基不import功能模块(self):
        """两段投影口径挂顶层（``trajectory_pipeline/projection.py``）——
        同 toolcatalog：只放纯函数，**不 import 任何功能模块**，否则地基
        变成依赖源。"""
        src = Path(__file__).parents[2] / "projection.py"
        for name in _imports(src):
            assert not name.startswith("trajectory_pipeline"), (
                f"projection.py import 了 {name}：跨层地基不能挂功能模块"
            )

    def test_不读仓库根output(self):
        """产物隔离（D8）。"""
        for path in sorted(STORAGE_PKG.glob("*.py")) + [TASK_VIEW_SRC]:
            text = path.read_text(encoding="utf-8")
            assert '"output/' not in text and "'output/" not in text, (
                f"{path.name} 里出现了根 output/ 字面量"
            )


# ── 2. 产物根 ──────────────────────────────────────────────────────


class Test产物根:
    def test_落在trajectory_pipeline下而不是仓库根(self):
        """``parents[1]`` 不是 ``parents[2]``。错写后者落到仓库根 output/
        （CLAUDE.md 明令禁止），且不报错——只是读不到数据。"""
        d = task_views_dir()
        assert d.parent.name == DEFAULT_SAMPLES_PARENT, (
            f"task_views_dir 上层不是 {DEFAULT_SAMPLES_PARENT}：{d}"
        )
        # path 包含 trajectory_pipeline 这一层（不是仓库根的 output/）
        parts = d.resolve().parts
        assert "trajectory_pipeline" in parts, (
            f"task_views_dir 不在 trajectory_pipeline 下：{d}"
        )
        assert d.name == DEFAULT_TASK_VIEWS_DIRNAME

    def test_storage转发到默认根(self):
        """task_view 转发到 storage 的实现，不另写路径字面量。"""
        assert task_view.storage_task_views_dir is task_views_dir


# ── 3. 契约文档不是摆设 ────────────────────────────────────────────


class Test契约文档不是摆设:
    @pytest.fixture(scope="class")
    @classmethod
    def doc_text(cls):
        return DOC.read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    @classmethod
    def code_text(cls):
        return TASK_VIEW_SRC.read_text(encoding="utf-8")

    def test_文档非空且不是占位(self, doc_text):
        assert len(doc_text.strip()) > 1000
        assert "占位" not in doc_text and "TBD" not in doc_text

    def test_代码里命名的常量都在文档里(self, code_text, doc_text):
        """代码里出现的 T 视图专有常量（``_CLOSING_DISPOSITION`` / ``_TASK_SYSTEM_PROMPT``
        / ``_TASK_TOOL_SPECS`` / ``_REQUIRED_DISPOSITION_KEYS``）必须在
        契约文档里被提及——否则读文档的人以为没这回事。"""
        for name in [
            "_CLOSING_DISPOSITION",
            "_TASK_SYSTEM_PROMPT",
            "_TASK_TOOL_SPECS",
            "trajectory_id",
            "unit_outcomes",
            "ledger_summary",
            "task_gate",
            "closing_summary",
        ]:
            assert name in doc_text, (
                f"T 契约文档 {DOC.name} 没提 {name}——文档漏字段"
            )

    def test_文档提到的key都出现在代码里(self, doc_text):
        """从文档抓出 ``meta`` 字段表里的字段名（带反引号），逐个在
        ``to_task_meta`` 源码里能找到。"""
        # 取 'meta 对象' 章节到 '---' 之间的 '`xxx`' 字段
        meta_keys = set(re.findall(r"`([a-z_][a-z_0-9]*)`", doc_text))
        meta_keys -= {"role", "tool", "content", "messages", "openai", "tools",
                      "url", "page_title", "body_text", "video_tag_count",
                      "interactive_elements", "links", "function", "arguments",
                      "task_id", "run_id", "title", "name", "params"}
        text = TASK_VIEW_SRC.read_text(encoding="utf-8")
        for k in sorted(meta_keys):
            if k in {"trajectory_id", "task_id", "run_id", "title",
                     "source_archive", "schema_version", "messages", "openai",
                     "meta"}:
                # payload 顶层键必出现
                assert k in text, f"代码里没出现 T payload 顶层字段 {k!r}"
            # 其他键在 to_task_meta 的 return 字典里必出现
            elif k in {"unit_count", "unit_sample_ids", "unit_outcomes",
                       "ledger_summary", "task_gate", "gate_breakdown",
                       "rationale_missing", "degraded_from", "overall_branch",
                       "step_count", "observation_count", "infrastructure_actions",
                       "observation_complete", "missing_observation_count",
                       "closing_summary", "rationale_steps",
                       # 2026-10-20：能力声明 vs 已验证动作（同 P3）
                       "tools_declared", "tools_unused",
                       # 2026-10-20 方案 15 §7 R1：工具面（方言轴）
                       "tool_surface"}:
                assert k in text, f"代码 to_task_meta 里没出现 {k!r}"


# ── 4. 常量同步：T 与 schema.py / views.py 字面一致 ───────────────


class Test常量同步:
    """T 自己声明 ``_TASK_SYSTEM_PROMPT`` / ``_TASK_TOOL_SPECS`` /
    ``_CLOSING_DISPOSITION``——与 assembler.schema 与 assembler.views
    的对应常量字面字符串一致。纪律：D14.3 + 13 号增量 L4 不动
    Protocol 那一节同款。"""

    def test_SYSTEM_PROMPT与schema一致(self):
        assert task_view._TASK_SYSTEM_PROMPT == schema.SYSTEM_PROMPT

    def test_TOOL_SPECS与schema一致(self):
        schema_specs = [
            {"name": s.name, "params": list(s.params), "instruction": s.instruction}
            for s in schema.TOOL_SPECS
        ]
        task_specs = [
            {"name": s["name"], "params": list(s["params"]), "instruction": s["instruction"]}
            for s in task_view._TASK_TOOL_SPECS
        ]
        assert task_specs == schema_specs

    def test_CLOSING_DISPOSITION与views一致(self):
        assert task_view._CLOSING_DISPOSITION == views._CLOSING_DISPOSITION

    def test_处置词表包含BRANCH_LABELS全集(self):
        """quote 词表的 key 集与 ``executor.branches.BRANCH_LABELS`` 全集对应
        ——禁止漏键。**不含** ``success``——T 视图的成功处置词是手写字符串
        （``记录该网址为可播放资源``），不在词表里。

        不 import executor（纪律）——逐字在源码里 grep 拿 BRANCH_LABELS 字面集合。
        """
        # executor/branches.py 里 BRANCH_LABELS 的字面 key 列表
        # （与 `arch_v1` 同步不可变 —— 改这里要同步改 schema / task_view / 等等）。
        expected = {
            "not_play_site", "unreachable_hard", "no_play_control",
            "trailer_only", "trailer_suspect", "component_unverified",
            "player_unverified", "subscription_wall", "unresolved",
            "login_required", "blocked", "broken", "partial_free",
            "not_aggregator", "aggregator_no_entries", "not_article",
            "article_no_links", "no_site_search", "site_search_no_results",
            "site_search_routed", "login_wall_or_blocked",  # deprecated
        }
        keys = set(task_view._CLOSING_DISPOSITION.keys())
        missing = expected - keys
        extra = keys - expected
        assert not missing, f"_CLOSING_DISPOSITION 缺键：{missing}"
        assert not extra, f"_CLOSING_DISPOSITION 多键：{extra}"


# ── 5. 形状不变量（任何 P1 都成立） ────────────────────────────────


def _obs_full(**kw):
    base = {
        "url": "https://x.test",
        "page_title": "页面标题",
        "body_text": "正文内容很长" * 200,
        "body_source": "inner_text",
        "truncated": False,
        "raw_len": 1500,
        "stripped_ratio": 0.9,
        "degraded": [],
        "video_tag_count": 1,
        "iframe_count": 0,
        "elements_total": 30,
        "links_total": 20,
        "interactive_elements": [
            {"ref": "e1", "tag": "button", "label": "立即播放"},
        ],
        "links": [{"text": "首页", "href": "https://x.test/"}],
    }
    base.update(kw)
    return base


def _step(action, observation=None, rationale=None, error=""):
    """造一条 P1 ActionStep（与 executor.actions.ActionStep.to_json 同款形状）。"""
    return {
        "action": action,
        "observation": observation,
        "error": error,
        "rationale": rationale,
    }


def _action(tool, **kw):
    base = {
        "tool": tool,
        "params": kw.pop("params", {}),
        "origin": kw.pop("origin", "model"),
        "target": kw.pop("target", None),
    }
    base.update(kw)
    return base


def _archive(
    task_id="T001",
    run_id="b98b3e56",
    title="功夫",
    search_steps=None,
    visits=None,
    outcomes=None,
    ledger=None,
    user_prompt="给个能看正片的",
):
    return {
        "task_id": task_id,
        "run_id": run_id,
        "title": title,
        "user_prompt": user_prompt,
        "query": user_prompt,
        "search_url": "",
        "perceptor": "rule",
        "result_titles": 0,
        "search_blocked": "",
        "elapsed_ms": 1000,
        "warnings": [],
        "search_observation": _obs_full(),
        "steps": search_steps or [],
        "candidates": [],
        "visits": visits or [],
        "outcomes": outcomes or [],
        "ledger": ledger or {},
    }


def _visit(steps, url="https://x.test"):
    return {
        "url": url,
        "landed_url": url,
        "success": False,
        "notes": [],
        "steps": steps,
        "site_obs": _obs_full(),
        "player_obs": None,
    }


def _rendered(archive=None):
    archive = _archive() if archive is None else archive
    return {
        "trajectory_id": f"{archive['task_id']}__{archive['run_id']}",
        "task_id": archive["task_id"],
        "run_id": archive["run_id"],
        "title": archive["title"],
        "schema_version": "task-view.v1",
        "messages": task_view.to_task_messages(archive),
        "openai": {
            "trajectory_id": f"{archive['task_id']}__{archive['run_id']}",
            "messages": task_view.to_task_openai_messages(archive),
            "tools": task_view.task_tools(task_view._surface_of(archive)),
        },
        "meta": task_view.to_task_meta(archive),
    }


class Test形状不变量:
    """任何 P1 JSON 渲染出来都必须成立的不变式。"""

    def test_messages序列_system_user_assistant_user(self):
        """messages 顺序：system → user → assistant / user 交替 → 收尾 summary。"""
        search_steps = [
            _step(
                action=_action("goto", params={"url": "https://s.test"}),
                observation=_obs_full(),
                rationale={"status": "written", "text": "打开搜索页",
                           "conclusion": "下一步看候选"},
            ),
        ]
        site_steps = [
            _step(
                action=_action("click", target={"tag": "button", "label": "播放"}),
                observation=_obs_full(),
                rationale={"status": "written", "text": "点播放", "conclusion": "看正片"},
            ),
        ]
        archive = _archive(
            search_steps=search_steps,
            visits=[_visit(site_steps)],
            outcomes=[{"branch": "no_play_control", "branch_label": "找不到播放控件",
                       "evidence": "无播放器", "site_url": "https://x.test",
                       "play_page_url": ""}],
            ledger={"total_sites": 1, "succeeded": 0, "negative_samples": 1,
                    "by_branch": {"no_play_control": 1},
                    "missing_branches": [], "heuristic_candidates": 1},
        )
        msgs = task_view.to_task_messages(archive)
        # 第 0 条 system，第 1 条 user
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        # 第 2 条 assistant (搜索单元) —— 内容包含动作与观察
        assert msgs[2]["role"] == "assistant"
        assert "[动作] goto(" in msgs[2]["content"]
        # 第 3 条 user (搜索单元观察)
        assert msgs[3]["role"] == "user"
        # 第 4 条 assistant (site-1 单元动作) —— 包含 click 关键词
        assert msgs[4]["role"] == "assistant"
        assert "[动作] click" in msgs[4]["content"]
        # 第 5 条 user (site-1 单元观察)
        assert msgs[5]["role"] == "user"
        # 最后一条是**面向用户的收尾答复**（方案 23 R2）
        closing = msgs[-1]
        assert closing["role"] == "assistant"
        assert closing["content"] == (
            "没有找到《功夫》的可播放网址，"
            "已排除的站点：x.test（页面上没有播放控件，排除）。"
        )

    def test_infrastructure步整步跳过(self):
        """``new_tab`` / ``close_tab`` 是执行器自己做的，模型永远不该输出。
        整步跳过（动作连同它的观察与思考都不进消息流）。"""
        search_steps = [
            _step(action=_action("goto", params={"url": "https://s.test"}),
                  observation=_obs_full(), rationale={"status": "written"}),
            _step(action=_action("new_tab", params={"url": "https://y.test"},
                                  origin="infrastructure"),
                  observation=_obs_full(), rationale=None),
        ]
        archive = _archive(search_steps=search_steps)
        msgs = task_view.to_task_messages(archive)
        # 2 个动作 → 1 个 visible（new_tab 跳过）→ 1 对 assistant/user
        #   + 1 个 system + 1 个 user 首轮 + **1 条收尾答复**
        # 实际：system + user + (1 对 assistant/user) + 收尾 = 5 条
        # ⚠️ **2026-11-03 · re-base 到 per-Run**：per-unit 时代「没访问到
        # 站点 ⇒ 不发收尾」；per-Run 之后**每条有可见动作的轨迹都收尾**
        #（方案 23 R2-1：收尾是面向用户的答复——「没有找到《X》的可访问
        # 站点。」也是答复），P3 与 T 同款。
        assert len(msgs) == 5
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["content"] == "没有找到《功夫》的可访问站点。"
        # new_tab 不出现在任何 assistant content 里
        joined = "\n".join(m.get("content", "") for m in msgs)
        assert "new_tab" not in joined

    def test_tool_call_id跨unit稠密(self):
        """openai 形态里 tool_call_id 是全局稠密序号——infrastructure 跳过仍稠密。"""
        search_steps = [
            _step(action=_action("goto", params={"url": "https://s.test"}),
                  observation=_obs_full(), rationale={"status": "written"}),
            _step(action=_action("new_tab", origin="infrastructure"),
                  observation=_obs_full()),
        ]
        site_steps = [
            _step(action=_action("click", target={"tag": "a", "label": "首页"}),
                  observation=_obs_full(), rationale={"status": "written"}),
        ]
        archive = _archive(search_steps=search_steps,
                           visits=[_visit(site_steps)])
        msgs = task_view.to_task_openai_messages(archive)
        # 收集所有 tool_call_id
        ids = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
        assert ids == ["call_0", "call_1"]

    def test_arguments落盘是JSON字符串(self):
        """``function.arguments`` 是**字符串**不是 dict（OpenAI Chat
        Completions 契约，方案 23 R3-4）。T 与 P3 同款。"""
        archive = _archive(
            search_steps=[
                _step(action=_action("goto"), observation=_obs_full(),
                      rationale={"status": "written"})
            ],
            visits=[_visit([
                _step(action=_action("click", target={"tag": "a", "label": "首页"}),
                      observation=_obs_full(), rationale={"status": "written"})
            ])],
        )
        msgs = task_view.to_task_openai_messages(archive)
        calls = [
            c
            for m in msgs
            if m["role"] == "assistant" and m.get("tool_calls")
            for c in m["tool_calls"]
        ]
        assert calls, "夹具渲染不出任何 tool_calls，断言是空转"
        for call in calls:
            raw = call["function"]["arguments"]
            assert isinstance(raw, str), f"arguments 不是字符串：{type(raw)}"
            assert isinstance(json.loads(raw), dict)

    def test_meta_unit_outcomes_self合并(self):
        """unit_outcomes 长度 = 访问过的站点数（含 outcome 与没 outcome 的占位）。"""
        search_steps = [
            _step(action=_action("goto"), observation=_obs_full(),
                  rationale={"status": "written"}),
        ]
        visits = [_visit([]), _visit([]), _visit([])]
        outcomes = [
            {"branch": None, "branch_label": "成功", "evidence": "...", "site_url": "a", "play_page_url": "b"},
            {"branch": "broken", "branch_label": "内容损坏", "evidence": "...", "site_url": "c", "play_page_url": ""},
            {"branch": "unreachable_hard", "branch_label": "导航失败", "evidence": "...", "site_url": "d", "play_page_url": ""},
        ]
        archive = _archive(search_steps=search_steps, visits=visits, outcomes=outcomes,
                           ledger={"by_branch": {"broken": 1, "unreachable_hard": 1}})
        meta = task_view.to_task_meta(archive)
        assert meta["unit_count"] == 1 + 3  # search + 3 sites
        assert len(meta["unit_outcomes"]) == 3
        # ⚠️ **2026-10-30 · 方案 23**：任务级结论 = **最后一个结论**
        # （owner 裁定 ②）。旧口径是「任一 success ⇒ 成功」——它会把
        # 「中途成功过、后来又失败」的轨迹报成成功。
        assert meta["overall_branch"] == "unreachable_hard"
        assert meta["task_gate"]["status"] == "failed"

    def test_meta_overall_branch取最后一个结论(self):
        """⚠️ **2026-10-30 · 方案 23**：任务级结论看**最后一个**结论
        （owner 裁定 ②）——旧口径是「取首个失败分支」。"""
        search_steps = [
            _step(action=_action("goto"), observation=_obs_full(),
                  rationale={"status": "written"}),
        ]
        visits = [_visit([]), _visit([])]
        outcomes = [
            {"branch": "broken", "branch_label": "内容损坏", "evidence": "x", "site_url": "a", "play_page_url": ""},
            {"branch": "no_play_control", "branch_label": "...", "evidence": "y", "site_url": "b", "play_page_url": ""},
        ]
        archive = _archive(search_steps=search_steps, visits=visits, outcomes=outcomes,
                           ledger={"by_branch": {"broken": 1, "no_play_control": 1}})
        meta = task_view.to_task_meta(archive)
        assert meta["overall_branch"] == "no_play_control"

    def test_meta_unit_sample_ids反查P3(self):
        """``unit_sample_ids`` 给出从 T 反查 P3 的入口。

        ⚠️ **2026-10-30 · 方案 23**：一个任务一条轨迹 ⇒ P3 的
        ``sample_id`` 就是这条轨迹的 id（与 P1 存档名逐字相同），
        所以这里**只有一项**。
        """
        archive = _archive(
            visits=[_visit([]), _visit([])],
            outcomes=[{"branch": "broken", "evidence": "x", "site_url": "a", "play_page_url": ""},
                      {"branch": None, "evidence": "y", "site_url": "b", "play_page_url": "c"}],
        )
        meta = task_view.to_task_meta(archive)
        assert meta["unit_sample_ids"] == ["T001__b98b3e56"]

    def test_meta_step_count_observation_count(self):
        """step_count 与 observation_count 是全局（不是 per-unit）。"""
        search_steps = [
            _step(action=_action("goto"), observation=_obs_full()),
            _step(action=_action("new_tab", origin="infrastructure"),
                  observation=_obs_full()),
        ]
        site_steps = [
            _step(action=_action("click", target={"tag": "a", "label": "1"}),
                  observation=_obs_full()),
        ]
        archive = _archive(search_steps=search_steps, visits=[_visit(site_steps)])
        meta = task_view.to_task_meta(archive)
        # 3 步总数（含 infrastructure）+ 3 观察
        assert meta["step_count"] == 3
        assert meta["observation_count"] == 3
        assert "new_tab" in meta["infrastructure_actions"]

    def test_meta_rationale_steps_global_index(self):
        """rationale_steps.step_index 是**全局**重编，跨 unit 连续。"""
        search_steps = [
            _step(action=_action("goto"), observation=_obs_full(),
                  rationale={"status": "written", "text": "1", "conclusion": ""}),
            _step(action=_action("new_tab", origin="infrastructure"),
                  observation=_obs_full(), rationale=None),
        ]
        site_steps = [
            _step(action=_action("click", target={"tag": "a", "label": "x"}),
                  observation=_obs_full(),
                  rationale={"status": "unavailable", "text": "", "conclusion": ""}),
        ]
        archive = _archive(search_steps=search_steps, visits=[_visit(site_steps)])
        meta = task_view.to_task_meta(archive)
        # 3 步总（含 infra），但只有 step 0/2 有 rationale，序号仍 0..2
        # seq_index 全程 num_steps 增长（含 infra 步）
        assert [s["step_index"] for s in meta["rationale_steps"]] == [0, 2]
        assert [s["status"] for s in meta["rationale_steps"]] == ["written", "unavailable"]

    def test_meta_closing_summary_不播报分支计数(self):
        """closing 是给用户的答复：**不带** ``ledger.by_branch`` 摘要。

        旧口径写成「任务总结：… 分支分布：broken=1。」——分支名与计数
        是并池口径，不是助手语言（审计 G3 / 方案 23 R2-1）。
        """
        archive = _archive(
            visits=[_visit([])],
            outcomes=[{"branch": "broken", "evidence": "x", "site_url": "a", "play_page_url": ""}],
            ledger={"by_branch": {"broken": 1}, "missing_branches": [], "total_sites": 1},
        )
        msgs = task_view.to_task_messages(archive)
        # 任务级 summary 在 messages 末尾（assistant，无 tool_calls）
        # 但 search unit 没动作 → closing_summary=False（空轨迹不编总结）
        # 这里 site-1 没动作 → 实际不编 closing
        # 改 search unit 有动作、site-1 有 outcome → closing 应包含 by_branch 摘要
        archive2 = _archive(
            search_steps=[
                _step(action=_action("goto"), observation=_obs_full(),
                      rationale={"status": "written"}),
            ],
            visits=[_visit([])],
            outcomes=[{"branch": "broken", "evidence": "x", "site_url": "a", "play_page_url": ""}],
            ledger={"by_branch": {"broken": 1}},
        )
        msgs2 = task_view.to_task_messages(archive2)
        closing = msgs2[-1]
        assert closing["content"] == (
            "没有找到《功夫》的可播放网址，已排除的站点：a（内容损坏，排除）。"
        )
        assert "broken=1" not in closing["content"]
        assert "分支" not in closing["content"]

    def test_P3与T收尾同一句话(self):
        """R2-2：两侧收尾必须是**同一句话**（owner O-5 的「两段必须一致」）。

        差异只在附加信息——P3 多一句 unit 级 reflection 结论，T 多「别的
        站点怎么样」的清单。核心句对同一组事实逐字相同。
        """
        outcome = {"url": "https://x.test", "branch": "no_play_control",
                   "site_url": "https://x.test", "play_page_url": ""}
        archive = _archive(
            search_steps=[
                _step(action=_action("goto"), observation=_obs_full(),
                      rationale={"status": "written"}),
            ],
            visits=[_visit([
                _step(action=_action("click", target={"tag": "button", "label": "播放"}),
                      observation=_obs_full(), rationale={"status": "written"}),
            ])],
            outcomes=[outcome],
            ledger={"total_sites": 1, "by_branch": {"no_play_control": 1}},
        )
        # P3 侧：同一组事实（unit=site-1、同 title、同 outcome、无 conclusion）
        sample = {
            "task_id": "T001", "unit": "site-1", "title": "功夫",
            "outcome": outcome, "reflection": None,
        }
        steps = [(0, {"action": {"tool": "goto"}}, None, None)]
        assert task_view._closing_text(archive) == views._closing_text(sample, steps)

        # 成功侧同样一致（URL 句）
        ok = {"url": "https://x.test", "branch": None,
              "play_page_url": "https://x.test/play"}
        archive_ok = _archive(
            search_steps=[
                _step(action=_action("goto"), observation=_obs_full(),
                      rationale={"status": "written"}),
            ],
            outcomes=[ok], ledger={"total_sites": 1},
        )
        assert task_view._closing_text(archive_ok) == views._closing_text(
            {**sample, "outcome": ok}, steps)

    def test_空user_prompt不发空消息(self):
        """空 user_prompt 不发首轮空 user——同 P3 第 4 条判断。

        但 observation 回执**仍**是 user 角色（不是空消息，而是含观察
        文本的正常回执）。所以测的是：``msgs[0]=system``、``msgs[1]=assistant``
        直接紧跟（中间没有 ``user``），后续 observation 回执仍按需发。
        """
        search_steps = [
            _step(action=_action("goto"), observation=_obs_full(),
                  rationale={"status": "written"}),
        ]
        archive = _archive(search_steps=search_steps, user_prompt="")
        msgs = task_view.to_task_messages(archive)
        # 0: system, 1: assistant（首动作 + 思考）, 2: user（observation 回执）
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "assistant"
        assert msgs[2]["role"] == "user"
        # 中间没有空洞 user（system 后直接是 assistant）
        assert msgs[1]["role"] != "user"

    def test_动作无效与采集降级分列(self):
        """T 读 P1，``steps[i].error`` 要渲染成「本步动作无效」，
        与 ``degraded`` 的「采集降级」分列（方案 23 R1-2）。"""
        obs = _obs_full(degraded=["links_empty"])
        search_steps = [
            _step(action=_action("goto"), observation=obs,
                  rationale={"status": "written"},
                  error="click 失败: 元素不在页面上"),
        ]
        archive = _archive(search_steps=search_steps)
        msgs = task_view.to_task_messages(archive)
        user_msgs = [m for m in msgs if m["role"] == "user"]
        last = user_msgs[-1]["content"]
        assert "[本步动作无效: click 失败" in last
        assert "[采集降级: links_empty]" in last

    def test_observation_missing显式占位(self):
        """observation 为 None 时显式发占位回执——不空串（与 P3 第 8 条判断同款）。

        msgs 里 ``user`` 角色可能多条：首轮 ``user_prompt`` 是 user，
        每步的 observation 回执也是 user。占位回执是**最后**一条 user。
        """
        search_steps = [
            _step(action=_action("goto"), observation=None,
                  rationale={"status": "written", "text": "试着打开", "conclusion": ""}),
        ]
        archive = _archive(search_steps=search_steps)
        msgs = task_view.to_task_messages(archive)
        user_msgs = [m for m in msgs if m["role"] == "user"]
        # 至少有 2 条 user：首轮 user_prompt + observation 占位回执
        assert len(user_msgs) >= 2
        # 末尾那条 user 是 observation 占位
        assert "本步没有取到页面内容" in user_msgs[-1]["content"]
        # 首轮 user 是 user_prompt 原文
        assert user_msgs[0]["content"] == "给个能看正片的"


# ── 5a. 训练筛选（方案 23 R3-1 / R3-2）───────────────────────────


class Test训练筛选:
    """死循环步剔出两段消息，重复动作在 meta 标记。

    两段投影**共用同一份筛选结果**——各筛各的就会在「哪些步还在」上
    分叉，而 O-5 要求两段逐条一致。
    """

    def _click(self):
        return _action("click", target={"role": "button", "name": "播放"})

    def test_相邻同动作同观察只留一条(self):
        steps = [_step(self._click(), _obs_full()),
                 _step(self._click(), _obs_full())]
        archive = _archive(visits=[_visit(steps)])

        plain = [m for m in task_view.to_task_messages(archive)
                 if m["role"] == "assistant" and m["content"].startswith("[动作]")]
        calls = [m for m in task_view.to_task_openai_messages(archive)
                 if m.get("tool_calls")]
        assert len(plain) == 1
        assert len(calls) == 1
        assert calls[0]["tool_calls"][0]["id"] == "call_0"

    def test_观察变了要保留(self):
        steps = [_step(self._click(), _obs_full()),
                 _step(self._click(), _obs_full(body_text="第二屏"))]
        archive = _archive(visits=[_visit(steps)])
        calls = [m for m in task_view.to_task_openai_messages(archive)
                 if m.get("tool_calls")]
        assert len(calls) == 2

    def test_跨unit不剔(self):
        """换了一个站再做同一个动作，是换站访问不是原地打转。"""
        steps = [_step(self._click(), _obs_full())]
        archive = _archive(visits=[_visit(steps), _visit(steps)])
        calls = [m for m in task_view.to_task_openai_messages(archive)
                 if m.get("tool_calls")]
        assert len(calls) == 2

    def test_meta逐条记下剔了哪几步(self):
        steps = [_step(self._click(), _obs_full()),
                 _step(self._click(), _obs_full()),
                 _step(self._click(), _obs_full())]
        archive = _archive(visits=[_visit(steps)])
        meta = task_view.to_task_meta(archive)
        assert meta["deduped_steps"] == [1, 2]
        assert meta["deduped_step_count"] == 2

    def test_meta标记留在训练集里的重复动作(self):
        steps = [
            _step(self._click(), _obs_full()),
            _step(_action("press", params={"key": "Enter"}), _obs_full(body_text="乙")),
            _step(self._click(), _obs_full(body_text="丙")),
        ]
        archive = _archive(visits=[_visit(steps)])
        meta = task_view.to_task_meta(archive)
        assert meta["deduped_steps"] == []
        assert meta["repeat_actions"] == [{
            "step_index": 2, "unit": "site-1", "tool": "click", "occurrence": 2,
        }]
        assert meta["repeat_step_count"] == 1


# ── 5a2. 两段投影一致（方案 23 R5-2 / O-5）───────────────────────


class Test投影一致:
    """``messages`` 与 ``openai.messages`` 必须讲同一个故事。

    差异只允许**形态**（function-calling vs 纯文本），不允许内容分叉：
    同一动作序列 / 同一观察 / 同一收尾。校验跑两处——``build_task_view``
    落盘前拦渲染分叉，``ls_bridge.exporter`` 导出前拦旧档 / 手改档。
    """

    def _build_archive(self):
        search_steps = [
            _step(
                action=_action("goto", params={"url": "https://s.test"}),
                observation=_obs_full(),
                rationale={"status": "written", "text": "打开搜索页",
                           "conclusion": "看候选"},
            ),
        ]
        site_steps = [
            _step(
                action=_action("click", target={"tag": "button", "label": "播放"}),
                observation=_obs_full(),
                rationale={"status": "written", "text": "点播放",
                           "conclusion": "看正片"},
            ),
        ]
        return _archive(
            search_steps=search_steps,
            visits=[_visit(site_steps)],
            outcomes=[{"branch": "no_play_control", "branch_label": "找不到播放控件",
                       "evidence": "无播放器", "site_url": "https://x.test",
                       "play_page_url": ""}],
        )

    def _both(self):
        rendered = _rendered(self._build_archive())
        return rendered["messages"], rendered["openai"]["messages"]

    @staticmethod
    def _clone(messages):
        return json.loads(json.dumps(messages, ensure_ascii=False))

    def test_正常渲染逐条一致(self):
        plain, fc = self._both()
        assert task_view.projection_problems(plain, fc) == []

    def test_改观察文本能报出来(self):
        plain, fc = self._both()
        fc = self._clone(fc)
        tool_msgs = [m for m in fc if m.get("role") == "tool"]
        assert tool_msgs, "夹具渲染不出任何观察，断言是空转"
        tool_msgs[0]["content"] = "被改过的观察"
        problems = task_view.projection_problems(plain, fc)
        assert any("观察文本不一致" in p for p in problems), problems

    def test_删一步能报出来(self):
        plain, fc = self._both()
        fc = self._clone(fc)
        # 删掉第一个动作 assistant + 它的 tool 回执
        index = next(i for i, m in enumerate(fc) if m.get("tool_calls"))
        del fc[index:index + 2]
        problems = task_view.projection_problems(plain, fc)
        assert any("动作数不一致" in p for p in problems), problems

    def test_改收尾能报出来(self):
        plain, fc = self._both()
        fc = self._clone(fc)
        closing = fc[-1]
        assert closing["role"] == "assistant" and not closing.get("tool_calls")
        closing["content"] = "编出来的成功答复"
        problems = task_view.projection_problems(plain, fc)
        assert "收尾文本不一致" in problems, problems

    def test_tool_call_id被改能报出来(self):
        plain, fc = self._both()
        fc = self._clone(fc)
        call = next(c for m in fc for c in (m.get("tool_calls") or []))
        call["id"] = "call_9"
        problems = task_view.projection_problems(plain, fc)
        assert any("tool_call_id" in p for p in problems), problems

    def test_一步多个tool_call能报出来(self):
        plain, fc = self._both()
        fc = self._clone(fc)
        action = next(m for m in fc if m.get("tool_calls"))
        action["tool_calls"].append(dict(action["tool_calls"][0], id="call_x"))
        problems = task_view.projection_problems(plain, fc)
        assert any("tool_calls 不是恰好一条" in p for p in problems), problems

    def test_坏档里的非对象元素不崩(self):
        """导出侧要对旧档 / 手改档跑校验——一个标量元素不该把整批打断。"""
        plain, fc = self._both()
        fc = self._clone(fc) + ["这不是一条消息"]
        problems = task_view.projection_problems(plain, fc)
        assert any("非对象元素" in p for p in problems), problems

    def test_读步在两段投影里一致(self):
        """读原语（R4-1）是普通动作，不是旁路——它在两段投影里也得同款。

        读回执的正文块是**另一套排版**（只有被读的那一块、不带 counts），
        两段投影各写一份实现（P3 与 T 刻意不复用）。这条钉的是：换了一
        种回执形状，``projection_problems`` 仍然认得出两边讲的是同一个故事。
        """
        search_steps = [
            _step(
                action=_action("links"),
                observation=_obs_full(read="links", body_text="不该排版",
                                      links=[{"text": "正片", "href": "https://x.test/play"}]),
                rationale={"status": "written", "text": "看有哪些入口",
                           "conclusion": "挑一个"},
            ),
            _step(
                action=_action("goto", params={"url": "https://x.test/play"}),
                observation=_obs_full(),
                rationale={"status": "written", "text": "进站", "conclusion": "看播放器"},
            ),
        ]
        archive = _archive(
            search_steps=search_steps,
            visits=[_visit([_step(action=_action("snapshot"),
                                  observation=_obs_full(read="snapshot"))])],
            outcomes=[{"branch": "no_play_control", "branch_label": "找不到播放控件",
                       "evidence": "无播放器", "site_url": "https://x.test",
                       "play_page_url": ""}],
        )
        rendered = _rendered(archive)
        assert task_view.projection_problems(
            rendered["messages"], rendered["openai"]["messages"]
        ) == []
        # 两边都真的渲染出了读回执（否则上面那条断言是空转）。
        # 纯文本侧观察挂在 ``user`` 消息上，function-calling 侧才是 ``tool``。
        plain_obs = [m["content"] for m in rendered["messages"]
                     if m.get("role") == "user"]
        fc_obs = [m["content"] for m in rendered["openai"]["messages"]
                  if m.get("role") == "tool"]
        assert any("[读取: 链接]" in t for t in plain_obs), plain_obs
        assert any("[读取: 链接]" in t for t in fc_obs), fc_obs

    def test_build_task_view不一致不落盘(self, monkeypatch, tmp_path):
        """渲染分叉时**一份都不写**——fail-closed 不写半份。

        用 monkeypatch 把 openai 侧的动作整段抽掉，模拟渲染逻辑分叉；
        落盘函数必须在写盘前拦下，并把原因写进 skipped。
        """
        original = task_view.to_task_openai_messages
        monkeypatch.setattr(
            task_view,
            "to_task_openai_messages",
            lambda archive: [m for m in original(archive) if not m.get("tool_calls")],
        )
        archive = self._build_archive()
        result = task_view.build_task_view(archive, root=tmp_path)
        assert result.written == 0
        assert result.skipped and result.skipped[0].startswith("投影不一致"), result
        path = task_views_dir(tmp_path) / f"{archive['task_id']}__{archive['run_id']}.json"
        assert not path.exists(), "不一致的视图不该落盘"


# ── 5a-2. 工具声明与真实调用同一面（Q-A1）──────────────────────────


class Test工具声明与调用同一面:
    """``openai.tools`` 必须与 messages 里的真实调用**同面**。

    2026-11-03 · Q-A1 事故形状：``build_task_view`` 漏传面，声明走默认面
    （obscura 的 ``click {tag,label}``）、调用按存档走 camofox
    （``click {role,name}``）。两份各自都「合法」，投影闸门只看两段
    messages，于是坏视图静默落盘——报表上看不出任何异常。
    """

    @staticmethod
    def _camofox_archive():
        archive = _archive(
            search_steps=[
                _step(
                    action=_action(
                        "click", target={"role": "button", "name": "播放"}
                    ),
                    observation=_obs_full(),
                    rationale={"status": "written", "text": "点播放",
                               "conclusion": "看正片"},
                ),
            ],
        )
        archive["driver"] = "camofox"
        return archive

    def test_task_tools漏传面直接报错(self):
        """面是必答项：默认值曾让「漏传」与「传对」产出同一份声明。"""
        with pytest.raises(TypeError):
            task_view.task_tools()  # type: ignore[call-arg]

    def test_camofox面声明aria参数与读原语(self):
        tools = task_view.task_tools(
            task_view._surface_of(self._camofox_archive())
        )
        by_name = {t["function"]["name"]: t["function"] for t in tools}
        click = by_name["click"]["parameters"]
        assert set(click["properties"]) == {"role", "name"}
        assert set(click["required"]) == {"role", "name"}
        # 读原语只在 camofox / camoufox 面声明，obscura 面没有
        assert {"snapshot", "interactive", "links"} <= set(by_name)

    def test_obscura面点击声明tag与label(self):
        tools = task_view.task_tools(task_view._surface_of(_archive()))
        by_name = {t["function"]["name"]: t["function"] for t in tools}
        click = by_name["click"]["parameters"]
        assert set(click["properties"]) == {"tag", "label"}
        assert set(click["required"]) == {"tag", "label"}

    def test_camofox声明与调用相容(self):
        rendered = _rendered(self._camofox_archive())
        assert task_view.projection_problems(
            rendered["messages"], rendered["openai"]["messages"]
        ) == []
        assert task_view.tool_declaration_problems(
            rendered["openai"]["tools"], rendered["openai"]["messages"]
        ) == []

    def test_错面声明能报出来(self):
        """obscura 声明 + camofox 调用：参数键对不上，必须点名。"""
        rendered = _rendered(self._camofox_archive())
        obscura_tools = task_view.task_tools("obscura-1")
        problems = task_view.tool_declaration_problems(
            obscura_tools, rendered["openai"]["messages"]
        )
        assert any("不在声明里" in p for p in problems), problems

    def test_未声明工具能报出来(self):
        messages = [
            {"role": "assistant", "tool_calls": [{
                "id": "call_0", "type": "function",
                "function": {"name": "snapshot", "arguments": "{}"},
            }]},
        ]
        problems = task_view.tool_declaration_problems(
            task_view.task_tools("obscura-1"), messages
        )
        assert any("未声明的工具" in p for p in problems), problems

    def test_build_task_view面错配不落盘(self, monkeypatch, tmp_path):
        """声明与调用不同面 → fail-closed，不写半份（Q-A1 回归）。"""
        original = task_view.task_tools
        monkeypatch.setattr(
            task_view, "task_tools", lambda surface: original("obscura-1")
        )
        archive = self._camofox_archive()
        result = task_view.build_task_view(archive, root=tmp_path)
        assert result.written == 0
        assert result.skipped and result.skipped[0].startswith("投影不一致"), result
        path = task_views_dir(tmp_path) / f"{archive['task_id']}__{archive['run_id']}.json"
        assert not path.exists(), "面错配的视图不该落盘"

    def test_build_task_view按面声明并落盘(self, tmp_path):
        archive = self._camofox_archive()
        result = task_view.build_task_view(archive, root=tmp_path)
        assert result.written == 1, result
        path = task_views_dir(tmp_path) / f"{archive['task_id']}__{archive['run_id']}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["meta"]["tool_surface"] == "camofox-1"
        by_name = {
            t["function"]["name"]: t["function"]
            for t in payload["openai"]["tools"]
        }
        assert set(by_name["click"]["parameters"]["properties"]) == {
            "role", "name",
        }


# ── 5b. 来源与缺件（方案 23 R2-4）────────────────────────────────


class Test缺件与来源:
    """``meta.source_archive`` 与 ``meta.degraded_from`` 必须从事实来。

    两者曾经硬编码成 ``""`` / ``[]``：任何一份 P1 渲染出来都像全须全尾，
    而「这份视图来自哪份存档」「缺了什么」正是复核时要答的两句话。
    """

    def test_source_archive默认空串(self):
        """archive dict 里没有自己的文件名——猜不出来就不填。"""
        assert task_view.to_task_meta(_archive())["source_archive"] == ""

    def test_source_archive可注入(self):
        meta = task_view.to_task_meta(
            _archive(), source_archive="T001__b98b3e56.json"
        )
        assert meta["source_archive"] == "T001__b98b3e56.json"

    def test_degraded_from从P1事实算(self):
        """缺件要显式：user_prompt 空、搜索步为空、site_obs 只有 preview、
        没有 outcome——四种事实各自落到词表里的标记。"""
        search_obs = dict(_obs_full())
        search_obs.pop("body_text")
        search_obs["body_preview"] = "只有前 400 字"
        visits = [{
            "url": "https://x.test",
            "landed_url": "https://x.test",
            "success": True,
            "notes": [],
            "steps": [],
            "site_obs": search_obs,
            "player_obs": None,
        }]
        archive = _archive(user_prompt="", search_steps=[], visits=visits)
        degraded = task_view.to_task_meta(archive)["degraded_from"]
        for mark in ("user_prompt", "steps", "body_preview_only",
                     "outcome_missing"):
            assert mark in degraded, f"缺件 {mark} 没被记下来：{degraded}"
        # 去重（两个 unit 都缺 steps 也只出现一次）
        assert degraded.count("steps") == 1

    def test_success与branch对不上要标记(self):
        """``success=True`` 但 outcome 有失败分支——两个来源对不上时标记，
        不挑一个（挑一个就是在猜，而这条样本进不进训练集全看它）。"""
        visits = [{
            "url": "https://x.test",
            "landed_url": "https://x.test",
            "success": True,
            "notes": [],
            "steps": [_step(action=_action("click"),
                            observation=_obs_full(),
                            rationale={"status": "written"})],
            "site_obs": _obs_full(),
            "player_obs": None,
        }]
        archive = _archive(
            search_steps=[_step(action=_action("goto"), observation=_obs_full(),
                                rationale={"status": "written"})],
            visits=visits,
            outcomes=[{"url": "https://x.test", "branch": "unresolved",
                       "site_url": "https://x.test", "play_page_url": ""}],
        )
        assert "success_branch_mismatch" in task_view.to_task_meta(archive)[
            "degraded_from"
        ]

    def test_degraded_from按词表顺序(self):
        """顺序固定（同 P2 的 ``dict.fromkeys`` 去重口径），读的人不必
        每次重新对齐。"""
        archive = _archive(user_prompt="", search_steps=[])
        degraded = task_view.to_task_meta(archive)["degraded_from"]
        assert degraded == [
            k for k in task_view._TASK_DEGRADED_VOCAB if k in set(degraded)
        ]


# ── 6. storage 读写门禁 ────────────────────────────────────────────


class TestStorage读写:
    """storage.task_views 是 T 视图落盘的**唯一入口**，它的行为契约钉死。"""

    def test_写入读回一致(self, tmp_path):
        archive = _archive(
            search_steps=[
                _step(action=_action("goto", params={"url": "https://s.test"}),
                      observation=_obs_full(), rationale={"status": "written"}),
            ],
            visits=[_visit([
                _step(action=_action("click", target={"tag": "a", "label": "1"}),
                      observation=_obs_full(), rationale={"status": "written"}),
            ])],
            outcomes=[{"branch": None, "evidence": "x", "site_url": "a",
                       "play_page_url": "b"}],
        )
        rendered = _rendered(archive)
        trajectory_id = rendered["trajectory_id"]
        rep = task_views.write_task_view(
            rendered, trajectory_id, root=tmp_path, overwrite=True,
        )
        assert rep.written == 1 and rep.ok
        back = task_views.read_task_view(trajectory_id, root=tmp_path)
        assert back == rendered

    def test_原子写不留tmp残留(self, tmp_path):
        rendered = _rendered()
        task_views.write_task_view(
            rendered, rendered["trajectory_id"], root=tmp_path, overwrite=True,
        )
        assert list(task_views_dir(tmp_path).glob("*.tmp")) == []

    def test_默认不覆盖已存在(self, tmp_path):
        rendered = _rendered()
        # 第一次
        task_views.write_task_view(
            rendered, rendered["trajectory_id"], root=tmp_path, overwrite=True,
        )
        # 第二次（默认 overwrite=False）
        rep = task_views.write_task_view(
            rendered, rendered["trajectory_id"], root=tmp_path,
        )
        assert rep.written == 0 and not rep.ok
        assert "已存在" in rep.skipped[0]

    def test_缺payload键fail_closed(self, tmp_path):
        """payload 缺 ``messages`` / ``openai`` / ``meta`` 任意一个 → skipped，不写。"""
        rep = task_views.write_task_view(
            {"trajectory_id": "T001__b98b3e56", "messages": []},  # 缺 openai + meta
            "T001__b98b3e56",
            root=tmp_path,
        )
        assert rep.written == 0
        assert "缺 payload 键" in rep.skipped[0]

    def test_空trajectory_id不进文件(self, tmp_path):
        rep = task_views.write_task_view(
            {"messages": [], "openai": {}, "meta": {}},
            "",
            root=tmp_path,
        )
        assert rep.written == 0
        assert "空 id" in rep.skipped[0]

    def test_缺文件fail_closed(self, tmp_path):
        """缺一份 T = 这条读不到，返回 None——不返回半份数据、不猜。"""
        back = task_views.read_task_view("nonexistent__abc", root=tmp_path)
        assert back is None

    def test_坏JSON档fail_closed(self, tmp_path):
        d = task_views_dir(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        (d / "T001__b98b3e56.json").write_text("not json", encoding="utf-8")
        back = task_views.read_task_view("T001__b98b3e56", root=tmp_path)
        assert back is None