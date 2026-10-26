"""P3 视图的**契约测试**——任何 P2 JSON 渲染出的三份视图必须满足的性质。

与 ``tests/unit/test_views.py`` 的分工：
    unit/        测某个输入下渲染成什么
    contract/    测**任何**存档都必须满足的性质、文档与代码必须同步

本模块四块：

1. **模块图**：storage 是格式交接面的**最末端**——读 P3 只该依赖
   「文件名 + json」（``read_p3.py`` 的 docstring 原话），不 import
   assembler/executor。半年前重建的一批 P3，今天必须还能原样读回来。
2. **视图名三处一致**：storage 拼文件名的 ``VIEW_NAMES`` /
   ``DEFAULT_VIEWS_DIRNAME`` 与渲染侧（``assembler/views.py`` 顶部重新导出）
   是同一份；``views_dir`` 只落在 ``trajectory_pipeline/output/`` 下
   （``parents[1]`` 不是 ``parents[2]``——写错不报错、只读不到数据）。
3. **P3-views.md ↔ 代码双向核对**，两个方向都要能被发现：
   方向一（代码有、文档没写）：文档漏字段 ⇒ 读文档的人以为没这回事；
   方向二（文档写了、代码没有）：按错的形状写消费代码，样本本身正常、
   **没有任何报错**（与 ``test_p1/p2_contract`` 同款）。
4. **形状不变量**：tool_call_id 配对、new_tab 只在动作流不在声明、
   rationale 逐步对齐、成对性自查——这些是「任何存档」都成立的性质，
   由样例夹具之外的通用断言盯住。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from trajectory_pipeline.assembler import views
from trajectory_pipeline.storage import (
    DEFAULT_SAMPLES_PARENT,
    DEFAULT_VIEWS_DIRNAME,
    VIEW_NAMES,
    views_dir,
)
from trajectory_pipeline.storage import read_p3, write_p3

STORAGE_PKG = Path(read_p3.__file__).parent
VIEWS_SRC = Path(views.__file__)
DOC = STORAGE_PKG.parent / "docs" / "contracts" / "P3-views.md"


def _sources() -> list[Path]:
    """storage/ 全部模块 + assembler/views.py（渲染侧最末端）。"""
    return sorted(STORAGE_PKG.glob("*.py")) + [VIEWS_SRC]


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


def _keys_of(path: Path, func_name: str) -> set[str]:
    """取某个函数里 ``return {…}`` 的键集。**AST 读，不执行**。"""
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


# ── 1. 模块图：storage 是格式交接面最末端 ─────────────────────────


class Test模块图:
    def test_storage不import组装端点(self):
        """read：训练侧读 P3 只依赖文件名 + json。import 了 assembler，
        半年前的 P3 今天重建不出来（生产端改一个字段就断）。"""
        for path in sorted(STORAGE_PKG.glob("*.py")):
            for name in _imports(path):
                assert "trajectory_pipeline.assembler" not in name, (
                    f"{path.name} import 了 {name}：storage 读 P3 与生产端版本绑死"
                )

    def test_storage不import执行端点(self):
        for path in sorted(STORAGE_PKG.glob("*.py")):
            for name in _imports(path):
                assert "trajectory_pipeline.executor" not in name, (
                    f"{path.name} import 了 {name}：storage 读 P3 与执行端版本绑死"
                )

    def test_views不import执行端点(self):
        for name in _imports(VIEWS_SRC):
            assert "trajectory_pipeline.executor" not in name, (
                f"views.py import 了 {name}：重建 P3 不能依赖执行端当前版本"
            )

    def test_storage不读仓库根output(self):
        """产物隔离（D8）。读了根 output/ 就继承存量那套隐性耦合。"""
        for path in sorted(STORAGE_PKG.glob("*.py")) + [VIEWS_SRC]:
            text = path.read_text(encoding="utf-8")
            assert '"output/' not in text and "'output/" not in text, (
                f"{path.name} 里出现了根 output/ 字面量"
            )


# ── 2. 视图名三处一致 ──────────────────────────────────────────────


class Test视图名与渲染侧一致:
    def test_渲染侧导出同一份VIEW_NAMES(self):
        """storage 拼接文件名、views 渲染三份 dict——用的是**同一个元组**。
        各抄一份迟早一边改了另一边忘（拼出来的文件名与渲染出来的内容
        对不上 = 读到错位的视图）。"""
        assert views.VIEW_NAMES == VIEW_NAMES
        assert set(VIEW_NAMES) == {"messages", "openai", "meta"}
        assert views.DEFAULT_VIEWS_DIRNAME == DEFAULT_VIEWS_DIRNAME

    def test_渲染产出的键集与VIEW_NAMES一致(self):
        """``build_views`` 造出的 payload 键不多不少正好是 VIEW_NAMES。
        渲染侧加了一种新视图却忘补 VIEW_NAMES ⇒ 文件写了但拼不出名字。"""
        payload_keys = _assigned_dict_keys(VIEWS_SRC, "payloads")
        assert payload_keys == set(VIEW_NAMES), (
            f"payload 键 {payload_keys} 与 VIEW_NAMES {set(VIEW_NAMES)} 漂移"
        )

    def test_to_meta键集是文档那20个(self):
        """AST 读 ``to_meta`` 的返回字面量。**不多不少**——
        多了文档没写，少了文档写空话。"""
        actual = _keys_of(VIEWS_SRC, "to_meta")
        expected = {
            "sample_id",
            "task_id",
            "unit",
            "title",
            "source_archive",
            "gate",
            "rationale_missing",
            "degraded_from",
            "outcome",
            "provenance",
            "step_count",
            "observation_count",
            "missing_observation_count",
            "infrastructure_actions",
            "observation_complete",
            "closing_summary",
            "rationale_steps",
            # 2026-10-20：能力声明 vs 已验证动作。工具声明扩到 13 条
            # （11 条模型可见）之后，「这条没用过哪些」不显眼时，读样本的
            # 人会把**能力声明**当成**已验证的动作**。
            "tools_declared",
            "tools_unused",
            # 2026-10-20 方案 15 §7 R1：工具面（方言轴）。同名工具在两个
            # 面里参数不同，面错了就是声明与调用对不上。
            "tool_surface",
            # 2026-10-25 INC46 分代：人工复核/批次筛选要能一眼看出
            # 这条的思考是「动作前动机」还是「事后反思」——判读手册里
            # 两类文本的读法不同。
            "rationale_era",
        }
        assert actual == expected, (
            f"to_meta 键集漂移：多 {actual - expected} / 少 {expected - actual}。"
            f"改动请同步 docs/contracts/P3-views.md"
        )


class Test产物根:
    def test_落在trajectory_pipeline下而不是仓库根(self):
        """``parents[2]`` 会落到**仓库根**的 ``output/pipeline/``——存量
        产物目录（D8），而且**不报错**：只是读不到视图、像「这批没样本」。
        路径形状 ``<包根>/output/pipeline/views``，从 views 往上数三级
        才到包根。"""
        resolved = views_dir().resolve()
        assert resolved.name == DEFAULT_VIEWS_DIRNAME
        assert resolved.parent.name == DEFAULT_SAMPLES_PARENT
        assert resolved.parent.parent.name == "output"
        assert resolved.parent.parent.parent.name == "trajectory_pipeline", (
            f"产物落到了 {resolved}——storage 的 parents[1] 写成 parents[2] 了？"
        )

    def test_views转发到storage(self):
        """渲染侧不另定义路径字面量——改布局只动 storage 一处。"""
        assert views.views_dir("R") == views_dir("R")
        assert views.views_dir() == views_dir()


# ── 3. P3 契约文档 ↔ 代码 双向核对 ───────────────────────────────


class Test契约文档不是摆设:
    """与 ``test_p1/p2_contract.py`` 的同名类同款：两个方向都要能被发现。"""

    @staticmethod
    @pytest.fixture(scope="class")
    def doc_text():
        assert DOC.exists(), f"{DOC} 不存在——P3 字段级契约是模块 6 的交付物之一"
        return DOC.read_text(encoding="utf-8")

    @staticmethod
    @pytest.fixture(scope="class")
    def code_text():
        return "\n".join(p.read_text(encoding="utf-8") for p in _sources())

    def test_文档非空且不是占位(self, doc_text):
        assert len(doc_text) > 1500, "契约文档短得不像话——多半是占位"
        assert "待写" not in doc_text

    @pytest.mark.parametrize(
        "field",
        [
            # to_meta 的 15 键
            "sample_id",
            "task_id",
            "unit",
            "title",
            "source_archive",
            "gate",
            "rationale_missing",
            "degraded_from",
            "outcome",
            "provenance",
            "step_count",
            "observation_count",
            "infrastructure_actions",
            "observation_complete",
            "rationale_steps",
            # 2026-10-20 方案 15 §7 R1：工具面（方言轴）
            "tool_surface",
            # 嵌套字段
            "step_index",
            "status",
            "messages",
            "tools",
            "id",
            "type",
            "function",
            "name",
            "arguments",
            "description",
            "parameters",
            "properties",
            "required",
            "role",
            "content",
            "tool_call_id",
        ],
    )
    def test_代码有字段文档没写(self, doc_text, field):
        """方向一：代码产出的字段必须在文档里点名。"""
        assert f"`{field}`" in doc_text, f"P3 字段 {field!r} 没写进契约"

    def test_六条判断每一条都在文档里(self, doc_text):
        for needle in (
            '不是 `role: "user"`',
            "new_tab",
            "平铺",
            "空 `user_prompt`",
            "降级",
            "逐步对齐",
        ):
            assert needle in doc_text, f"契约文档漏了关键判断：{needle!r}"

    def test_代码里的字段名都在文档里(self, code_text, doc_text):
        """方向二：文档提到的**字段名**必须在源码里真的存在。

        透传的值域（栅门三态、rationale 三态）**不在 P3 源码里是正常的**
        ——P3 只原样透传、不产生它们，值域由 P2 契约
        （``docs/contracts/P2-sample.md`` + ``assembler/schema.py``）保证。
        硬把这些字面量塞进 storage/views 才是假同步。
        """
        import re

        # 从上游透传、不由本层产生或校验的值域——权威在 P2 契约
        passthrough_values = {
            "passed",
            "failed",
            "not_run",
            "written",
            "unavailable",
            "empty",
        }
        for name in set(re.findall(r"`([a-z_]{4,})`", doc_text)):
            if name.startswith(("sample_", "p1_")) or name in passthrough_values:
                continue
            assert name in code_text, (
                f"P3 契约文档提到 {name!r}，但 storage/views 源码里找不到它——"
                f"文档写了实现没有的东西"
            )


# ── 4. 形状不变量（任何存档都成立） ───────────────────────────────


def _obs(url="https://x.test", **kw):
    base = {
        "url": url,
        "page_title": "页面标题",
        "body_text": "正文内容",
        "body_source": "body_text",
        "body_chars": 4,
        "truncated": {"source": False, "view": False},
        "degraded": [],
        "interactive_elements": [{"tag": "button", "label": "立即播放"}],
        "links": [{"text": "首页", "href": "https://x.test/"}],
        "video_tag_count": 1,
        "iframe_count": 0,
        "limits": {
            "body_chars": None,
            "elements": None,
            "links": None,
            "cropped": False,
        },
    }
    base.update(kw)
    return base


def _sample(**over):
    six = {
        "system_prompt": "系统提示词",
        "tools": [
            {"name": "goto", "params": ["url"], "instruction": "打开网址"},
            {"name": "click", "params": [], "instruction": "点击元素"},
            {"name": "new_tab", "params": ["url"], "instruction": "开新标签"},
        ],
        "user_prompt": "给个能看正片的",
        "provenance": {"genre": "喜剧"},
        "actions": [
            {
                "tool": "goto",
                "params": {"url": "https://x.test"},
                "target": None,
                "origin": "model",
            },
            {
                "tool": "new_tab",
                "params": {"url": "https://y.test"},
                "target": None,
                "origin": "infrastructure",
            },
        ],
        "observations": [_obs(), _obs("https://y.test")],
        "rationales": [
            {
                "step_index": 0,
                "status": "written",
                "text": "先开一个搜索页",
                "conclusion": "下一步去点播放",
            },
        ],
    }
    base = {
        "sample_id": "T001__abc123__site-1",
        "task_id": "T001",
        "unit": "site-1",
        "title": "功夫",
        "source_archive": "T001__abc123.json",
        "six": six,
        "gate": {"status": "passed", "reasons": ["gate"]},
        "rationale_missing": False,
        "outcome": {
            "url": "https://x.test",
            "branch": "no_play_control",
            "source": "rule",
            "fallback_used": False,
            "succeeded": False,
        },
        "degraded_from": [],
    }
    for k, v in over.items():
        if k == "six":
            six.update(v)
        else:
            base[k] = v
    return base


def _rendered(sample=None):
    sample = _sample() if sample is None else sample
    payloads = {
        "messages": views.to_messages(sample),
        "openai": {
            "sample_id": sample["sample_id"],
            "messages": views.to_openai_messages(sample),
            "tools": views._visible_tools(sample),
        },
        "meta": views.to_meta(sample),
    }
    return payloads


class Test形状不变量:
    """这些断言不针对某个输入——任何 P2 JSON 渲染出来都必须成立。

    与 unit 的分工：unit 验证八条判断逐条在具体输入上成立；
    这里验证的是「一批样本整体读回来仍保持结构承诺」。"""

    def test_每个动作都有一条assistant且tool_call_id配对(self):
        """openai 视图里**带 tool_calls 的** assistant 消息数与工具回执数
        一致，每对 ``tool_call_id`` / ``id`` 咬合。关联是训练信号，丢了
        模型就学不会「观察是对那个动作的回应」。

        夹具有 2 个动作，其中 ``new_tab`` 是 infrastructure——整步不进
        消息流（2026-10-15 判断 2 反转），故可见的只有 1 对；末尾那条
        没有 tool_calls 的 assistant 是「任务总结」收尾（判断 7）。"""
        payload = _rendered()
        msgs = payload["openai"]["messages"]
        assistants = [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]
        tools = [m for m in msgs if m["role"] == "tool"]
        assert len(assistants) == len(tools) == 1
        for a, t in zip(assistants, tools):
            assert len(a["tool_calls"]) == 1
            assert t["tool_call_id"] == a["tool_calls"][0]["id"], (
                "观察回执没有指向它回应的那个动作"
            )
        closing = msgs[-1]
        assert closing["role"] == "assistant" and "tool_calls" not in closing
        assert closing["content"].startswith("任务总结：")

    def test_工具声明不含基础设施动作(self):
        """new_tab 是执行器自己的会话隔离，模型永远不该输出它——
        既不进工具声明，也**整步不进消息流**（2026-10-15 拍板反转：
        让模型看到一条它永远不会发的 assistant tool_calls，就是教它
        输出越权动作）。可回放的完整动作流留在 P2 的 six.actions。"""
        payload = _rendered()
        declared = {t["function"]["name"] for t in payload["openai"]["tools"]}
        assert "new_tab" not in declared
        flow = {
            c["function"]["name"]
            for m in payload["openai"]["messages"]
            if m["role"] == "assistant" and m.get("tool_calls")
            for c in m["tool_calls"]
        }
        assert "new_tab" not in flow

    def test_rationale逐步对齐不合成计数(self):
        """rationale_steps 逐条列出、step_index 从 0 起连续——
        合成一个计数就分不出「缺中间那步」与「缺最后那步」。"""
        meta = _rendered()["meta"]
        steps = meta["rationale_steps"]
        assert [s["step_index"] for s in steps] == list(range(len(steps)))
        assert all(s["status"] in ("written", "unavailable", "empty") for s in steps)
        # "一条都没有"时 rationale_missing 是真——单独标记，不与计数合并
        assert meta["rationale_missing"] is (len(steps) == 0)

    def test_成对性自查(self):
        """动作多于观察 ⇒ 末步没取到观察——真实轨迹的一部分，
        不修但要看得见（训练侧会看到「发出去没回音」的动作）。"""
        meta = _rendered()["meta"]
        assert meta["step_count"] == 2
        assert meta["observation_count"] == 2
        assert meta["observation_complete"] is True
        # 打破对：末步动作没有观察
        s = _sample(
            six={
                "actions": [
                    {
                        "tool": "goto",
                        "params": {"url": "a"},
                        "target": None,
                        "origin": "model",
                    },
                    {
                        "tool": "goto",
                        "params": {"url": "b"},
                        "target": None,
                        "origin": "model",
                    },
                ],
                "observations": [_obs()],
            }
        )
        assert _rendered(s)["meta"]["observation_complete"] is False

    def test_空user_prompt不发空消息(self):
        """发一条 ``content: ""`` 的 user 消息会教模型「用户可以什么都不说」。
        （判断 4）"""
        s = _sample(six={"user_prompt": ""})
        roles = [m["role"] for m in _rendered(s)["openai"]["messages"]]
        assert "user" not in roles


# ── 5. storage 读写门禁（训练侧入口的行为契约） ──────────────────


class TestStorage读写:
    """storage 是训练侧读 P3 的**唯一入口**，它的承诺在这里钉死。"""

    def test_写入读回一致(self, tmp_path):
        payload = _rendered()
        rep = write_p3.write_views(
            payload, "T001__abc123__site-1", root=tmp_path, overwrite=True
        )
        assert rep.written == 3 and rep.ok
        back = read_p3.read_views("T001__abc123__site-1", root=tmp_path)
        assert back == dict(payload)

    def test_原子写不留tmp残留(self, tmp_path):
        write_p3.write_views(_rendered(), "S1", root=tmp_path, overwrite=True)
        assert list(views_dir(tmp_path).glob("*.tmp")) == []

    def test_缺文件fail_closed(self, tmp_path):
        """缺一份视图 = 这条读不到，返回 None——不返回半份数据、不猜。"""
        write_p3.write_views(_rendered(), "S1", root=tmp_path, overwrite=True)
        (views_dir(tmp_path) / "S1_meta.json").unlink()
        assert read_p3.read_views("S1", root=tmp_path) is None

    def test_坏JSONfail_closed(self, tmp_path):
        write_p3.write_views(_rendered(), "S1", root=tmp_path, overwrite=True)
        (views_dir(tmp_path) / "S1_openai.json").write_text("{ 坏", encoding="utf-8")
        assert read_p3.read_views("S1", root=tmp_path) is None

    def test_非法视图名fail_closed(self, tmp_path):
        assert read_p3.read_view("S1", "not_a_view", root=tmp_path) is None

    def test_默认不覆盖(self, tmp_path):
        """重建批不该悄悄把别人刚标好的视图抹掉。"""
        write_p3.write_views(_rendered(), "S1", root=tmp_path, overwrite=True)
        rep = write_p3.write_views(_rendered(), "S1", root=tmp_path)
        assert rep.written == 0
        assert len(rep.skipped) == 3

    def test_空id起不出文件名(self, tmp_path):
        rep = write_p3.write_views(_rendered(), "", root=tmp_path, overwrite=True)
        assert rep.written == 0 and rep.skipped
        assert list(views_dir(tmp_path).glob("*.json")) == []
