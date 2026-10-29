"""``golden_ls`` 契约测试：导入边界 + 控件名一致 + 键齐全 + 署名分列。

沿用 ``tests/contract/test_ls_bridge_contract.py`` 的风格：交接面是**格式
与常量**，不是运行时行为。

守的八件事（每一条都对应一个**不会报错**的错）：

A1 导入方向单向           ``golden_ls`` → ``ls_bridge`` 可以；反过来不行
A2 控件名 ⊆ XML 控件名    LS 按 ``from_name`` 匹配控件，对不上**静默丢弃**
A3 task.data 键有占位     少一个 ``$key`` ⇒ 标注页那块渲染成空
A4 ``sample_id`` = ``golden_id`` 且**版内唯一**
A5 **两套署名分列**        合成一栏即失败（裁定④）
A6 缺字段**显式标注**     空串会被读成「页面里没有播放控件」
A7 人类署名集合只有一个真源  与仓库既有封条集合逐字一致
A8 凭据扫描覆盖 task.data 全文
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from trajectory_pipeline.evaluation.golden_ls import dataset, exporter
from trajectory_pipeline.evaluation.golden_ls import verdicts as ls_verdicts
from trajectory_pipeline.evaluation.golden_ls import label_config as lc

GOLDEN_LS_DIR = Path("trajectory_pipeline/evaluation/golden_ls")
LS_BRIDGE_DIR = Path("trajectory_pipeline/evaluation/ls_bridge")

#: ``golden_ls`` 允许 import 的 trajectory_pipeline 模块。
#:
#: **方向是单向的**：``golden_ls`` 用 ``ls_bridge`` 的 client / 推送 / 判重
#: 台账；``ls_bridge`` **不许**反过来 import ``golden_ls``——那会让 scout
#: 轨迹那一路的依赖边界（evaluation → storage，不碰 executor / golden）
#: 被黄金集这一路撑开。
#:
#: ⚠️ ``evaluation.access_surface`` 是**面词表的评估侧入口**（原样再导出
#: ``executor.browser.access_surface``，无 IO、无副作用）。它**已经是**
#: ``golden.builder`` 的传递依赖 ⇒ 白名单列它不是新开口子，只是把既有依赖
#: 显式写出来——``golden_ls`` 自己校验「人填的面认不认得出」时得按**同一份
#: 词表**（方案 20 §R3「不许自己再写一遍」），绕道别的模块拿反而是第二份真源。
_ALLOWED_TP_IMPORTS = {
    "trajectory_pipeline.evaluation.ls_bridge",
    "trajectory_pipeline.evaluation.golden",
    "trajectory_pipeline.evaluation.golden_ls",
    "trajectory_pipeline.evaluation.calibration",
    "trajectory_pipeline.evaluation.access_surface",
    "trajectory_pipeline.executor.branches",
    "trajectory_pipeline.executor.branch_eras",
    "trajectory_pipeline.storage",
    "trajectory_pipeline.storage.task_views",
}


def _sources(directory: Path) -> list[Path]:
    return sorted(p for p in directory.glob("*.py") if p.name != "__init__.py")


def _imports(path: Path) -> set[str]:
    """抓所有 import 的模块名（绝对 + 相对，含函数内延迟 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                mod = f"{'.' * node.level}{mod}"
            out.add(mod)
    return out


def _literal_set(node: ast.expr) -> set[str] | None:
    """取一个「集合字面量」的值——``{...}`` 或 ``frozenset({...})`` 都吃。

    ``ast.literal_eval`` **不吃** ``frozenset(...)``（那是 Call），所以得
    自己剥一层。
    """
    if isinstance(node, ast.Call):
        args = node.args
        if not args:
            return None
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name not in ("frozenset", "set"):
            return None
        node = args[0]
    if isinstance(node, ast.Set):
        values = set()
        for element in node.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                values.add(element.value)
        return values
    return None


# ── 共用样本（模块级函数，不做 fixture） ──────────────────────────
#
# ⚠️ 这里刻意**不用 class-scoped fixture**：pytest 8.4 起把它当弃用项，
# 而本仓库的 CI 还没到那个版本就会先吵起来——测试文件不该自带噪音。
# 另一个坑：模块**不能**叫 ``annotations``——包 ``__init__.py`` 里的
# ``from __future__ import annotations`` 会把那个名字绑成
# ``__future__._Feature``，同名子模块被顶掉（``_Feature`` 没有
# ``parse_export``）。⇒ 模块叫 ``verdicts``。


def _label_config_xml() -> str:
    return lc.render_label_config()


def _v14_rows() -> list[dict]:
    return dataset.load_rows(14)


def _ls_payload() -> dict:
    return {
        "tasks": [
            {"id": 11, "data": {"golden_id": "g1", "split": "calibration"}},
            {"id": 12, "data": {"golden_id": "g2", "split": "holdout"}},
        ],
        "annotations": [
            {
                "task": 11,
                "created_by": {"email": "owner@example.com"},
                "created_at": "2026-10-29T01:00:00+00:00",
                "updated_at": "2026-10-29T02:00:00+00:00",
                "result": [
                    {"from_name": lc.VERDICT_CONTROL, "to_name": "task",
                     "type": "choices", "value": {"choices": ["disagree"]}},
                    {"from_name": lc.HUMAN_BRANCH_CONTROL, "to_name": "task",
                     "type": "choices", "value": {"choices": ["blocked"]}},
                    {"from_name": lc.REASON_CONTROL, "to_name": "task_anchor",
                     "type": "textarea", "value": {"text": "理由"}},
                ],
            }
        ],
    }


def _ls_records() -> list[dict]:
    gold_rows = {
        "g1": {"golden": {"reviewed_by": "manual-review",
                          "reviewed_at": "2026-10-14T01:11:41+00:00",
                          "label": "success"}},
    }
    return ls_verdicts.parse_export(_ls_payload(), gold_rows=gold_rows)


# ── A1: 导入方向 ────────────────────────────────────────────────


class TestImportDirection:
    @pytest.mark.parametrize("path", _sources(GOLDEN_LS_DIR), ids=lambda p: p.name)
    def test_不import_archive(self, path: Path) -> None:
        for mod in _imports(path):
            assert not mod.startswith("archive"), (
                f"{path.name} import 了 {mod}; archive/v1 禁止 import"
            )

    @pytest.mark.parametrize("path", _sources(GOLDEN_LS_DIR), ids=lambda p: p.name)
    def test_trajectory_pipeline依赖白名单(self, path: Path) -> None:
        offenders = {
            mod
            for mod in _imports(path)
            if mod.startswith("trajectory_pipeline")
            and not any(
                mod == allowed or mod.startswith(allowed + ".")
                for allowed in _ALLOWED_TP_IMPORTS
            )
        }
        assert not offenders, (
            f"{path.name} import 了白名单外的模块: {sorted(offenders)}"
        )

    @pytest.mark.parametrize("path", _sources(LS_BRIDGE_DIR), ids=lambda p: p.name)
    def test_ls_bridge不反向依赖golden(self, path: Path) -> None:
        """``ls_bridge`` 不许 import ``golden_ls`` / ``golden`` / ``executor``。

        ⚠️ 这条是本方案**改落点**的原因：黄金集那一路必须读分支表、冻结档
        与现场读数，而 ``ls_bridge`` 的白名单（见
        ``test_ls_bridge_contract.py``）不含这三样。塞进去就得撑开白名单，
        scout 那一路的边界就没了。⇒ 另立 ``evaluation/golden_ls/``。
        """
        offenders = {
            mod
            for mod in _imports(path)
            if mod.startswith("trajectory_pipeline.evaluation.golden")
            or mod.startswith("trajectory_pipeline.executor")
        }
        assert not offenders, (
            f"{path.name} import 了 {sorted(offenders)}; "
            "ls_bridge 不得依赖 golden / executor（边界见 test_ls_bridge_contract.py）"
        )


# ── A2 / A3: 控件名与 task.data 键 ─────────────────────────────


class TestNoFutureNameCollision:
    """**模块名不能撞 ``__future__`` 的特性名**（踩过：``annotations``）。

    包 ``__init__.py`` 里的 ``from __future__ import annotations`` **真的会
    执行**这条 import，于是包命名空间里的 ``annotations`` 是
    ``__future__._Feature``——**同名子模块被顶掉**。后果：

    * ``from ...golden_ls import annotations`` 拿到 ``_Feature``，
      之后 ``annotations.parse_export(...)`` 报
      ``AttributeError: '_Feature' object has no attribute 'parse_export'``
      ——报错信息**完全指不到真正的原因**；
    * pytest 里更怪：模块名 ``annotations`` 还会撞 pytest 自己的内建
      ``annotations`` 属性。

    ⇒ 这条契约把「不许起这种名字」钉死。名字改成 ``verdicts`` 了。
    """

    def test_子模块名不撞__future__特性名(self) -> None:
        import __future__

        reserved = set(__future__.all_feature_names)
        offenders = sorted(
            path.stem
            for path in GOLDEN_LS_DIR.glob("*.py")
            if path.stem in reserved
        )
        assert not offenders, (
            f"golden_ls 里有子模块叫 {offenders}——"
            "会被包 ``__init__.py`` 的 ``from __future__ import ...`` 顶掉"
        )

    def test_包属性不被别的对象顶掉(self) -> None:
        """包命名空间里每个子模块名指向的必须是**那个模块本身**。

        ``from pkg import X`` 走的是 ``getattr(pkg, "X")``——它拿到的必须
        是模块，不能是别的什么东西（``__future__._Feature`` 就是现成的例子）。
        """
        import importlib

        import trajectory_pipeline.evaluation.golden_ls as pkg

        for path in GOLDEN_LS_DIR.glob("*.py"):
            stem = path.stem
            if stem == "__init__":
                continue
            mod = importlib.import_module(
                f"trajectory_pipeline.evaluation.golden_ls.{stem}"
            )
            attr = getattr(pkg, stem, None)
            assert attr is None or attr is mod, (
                f"包属性 {stem!r} 被 {type(attr).__name__} 顶掉了——"
                f"应为 {stem} 模块"
            )


class TestControlAndKeySync:
    def test_九个块都在XML里(self) -> None:
        xml = _label_config_xml()
        for control in lc.BLOCK_CONTROLS:
            assert f'name="{control}"' in xml, f"label_config 缺控件 {control}"

    def test_九个块都有占位符(self) -> None:
        """少一个 ``$key`` ⇒ 标注页那块渲染成空，**且 import 不报错**。"""
        xml = _label_config_xml()
        for control in lc.BLOCK_CONTROLS:
            assert f"${control}" in xml, f"label_config 缺占位符 ${control}"

    def test_裁决四控件都在(self) -> None:
        xml = _label_config_xml()
        for control in (
            lc.VERDICT_CONTROL,
            lc.HUMAN_BRANCH_CONTROL,
            lc.REASON_CONTROL,
            lc.NOTE_CONTROL,
        ):
            assert f'name="{control}"' in xml, f"label_config 缺控件 {control}"

    def test_文本域指向真实存在的tag(self) -> None:
        """LS 1.23 硬约束：``toName`` 必须指向真实存在的 tag。

        缺了直接 ``Validation failed on : 'toName' is a required property``。
        """
        xml = _label_config_xml()
        anchors = set(re.findall(r'<Text\s+name="([^"]+)"', xml))
        for target in set(re.findall(r'toName="([^"]+)"', xml)):
            assert target in anchors, f"toName={target!r} 指向不存在的 tag"

    def test_选项指向带value的文本(self) -> None:
        """``<Choices>`` 的 ``toName`` 必须指向**带 value 的** ``<Text>``。"""
        xml = _label_config_xml()
        valued = set(re.findall(r'<Text\s+name="([^"]+)"\s+value="[^"]*"', xml))
        for control in (lc.VERDICT_CONTROL, lc.HUMAN_BRANCH_CONTROL):
            block = re.search(
                rf'<Choices name="{control}"[^>]*toName="([^"]+)"', xml
            )
            assert block is not None, f"{control} 缺 toName"
            assert block.group(1) in valued, (
                f"{control} 的 toName={block.group(1)!r} 不是带 value 的 Text"
            )

    def test_不用alias(self) -> None:
        """``alias=`` 会把 LS 认的选项标识静默换成 alias（v1 实测）。"""
        stripped = re.sub(r"<!--.*?-->", "", _label_config_xml(), flags=re.S)
        assert "alias=" not in stripped

    def test_只用已注册标签(self) -> None:
        """本机 LS 1.23 未注册的标签 ⇒ 标注页整页报错。"""
        registered = {"View", "Header", "Text", "TextArea", "Choices", "Choice", "Style"}
        stripped = re.sub(r"<!--.*?-->", "", _label_config_xml(), flags=re.S)
        used = set(re.findall(r"<([A-Za-z]+)[\s>]", stripped))
        assert not (used - registered), f"用了未注册标签: {sorted(used - registered)}"


# ── A4: 键与唯一性（对着真档跑） ────────────────────────────────


class TestTaskDataKeys:
    def test_每版golden_id唯一(self) -> None:
        rows = _v14_rows()
        ids = [str(r.get("golden_id") or "") for r in rows]
        assert len(ids) == len(set(ids)), "v14 的 golden_id 有重复"

    def test_三档行数(self) -> None:
        counts: dict[str, int] = {}
        for row in _v14_rows():
            counts[row["__split__"]] = counts.get(row["__split__"], 0) + 1
        assert counts == {"calibration": 131, "regression": 60, "holdout": 103}

    def test_sample_id等于golden_id(self) -> None:
        row = _v14_rows()[0]
        data = exporter.build_golden_task_data(
            row,
            version_meta=dataset.version_meta(14),
            clauses={"D-10": "x", "D-4": "y", "D-5": "z"},
            scene={},
            trajectory=None,
        )
        assert data["sample_id"] == data["golden_id"] == row["golden_id"]


# ── A5: 两套署名分列 ────────────────────────────────────────────


class TestTwoSignatures:
    def test_两栏都在且不是同一栏(self) -> None:
        record = _ls_records()[0]
        assert "annotator" in record
        assert "gold" in record and "reviewed_by" in record["gold"]
        assert record["annotator"] == "owner@example.com"
        assert record["gold"]["reviewed_by"] == "manual-review"

    def test_两栏的值不互相顶替(self) -> None:
        """合并成一栏 ⇒ 「owner 复核过」与「owner 当初定的金标」分不清。"""
        record = _ls_records()[0]
        assert record["annotator"] != record["gold"]["reviewed_by"]
        assert record["gold"]["reviewed_by"] not in ("", None)

    def test_没复核的也出一行(self) -> None:
        """漏掉没复核的那些 ⇒ 覆盖率会读成 100%。"""
        records = _ls_records()
        assert len(records) == 2
        assert records[1]["verdict"] == ls_verdicts.UNKNOWN
        assert records[1]["annotator_kind"] == "unknown"

    def test_LS123的身份字段认得出来(self) -> None:
        """⚠️⚠️ **LS 1.23.0 实测**（2026-10-29）：标注对象里**没有**
        ``created_by``——身份在 ``created_username``（实测形态
        ``" reesn@qq.com, 1"``）与 ``completed_by`` 里。

        初稿只读 ``created_by`` ⇒ **署名列恒为「（不可用）」**⇒ 裁定④
        形同虚设，而且**不报错**（导出照样出 5 行，只是那栏全是不可用）。
        """
        payload = {
            "tasks": [
                {"id": 3, "data": {"golden_id": "g1", "split": "calibration"}},
            ],
            "annotations": [
                {
                    "id": 7,
                    "task": 3,
                    "created_username": " reesn@qq.com, 1",
                    "completed_by": 1,
                    "created_at": "2026-10-29T05:42:48Z",
                    "updated_at": "2026-10-29T05:42:48Z",
                    "result": [
                        {"from_name": lc.VERDICT_CONTROL, "to_name": "task",
                         "type": "choices", "value": {"choices": ["agree"]}},
                    ],
                }
            ],
        }
        records = ls_verdicts.parse_export(payload)
        assert records[0]["annotator"] == "reesn@qq.com"
        assert records[0]["annotator_kind"] == "human"

    def test_身份只拿到数字id时也照记(self) -> None:
        """⚠️ 退到 ``completed_by``（数字 id）也是**身份**——不是「不可用」。

        ⚠️ **别把它写成空**：空串会被读成「没复核过」，而真相是
        「服务端只给了数字 id」。⇒ 记成 id，但 `annotator_kind` 仍是
        ``human``（它判不了「这个账号是不是机器」）。
        """
        payload = {
            "tasks": [
                {"id": 3, "data": {"golden_id": "g1", "split": "calibration"}},
            ],
            "annotations": [
                {
                    "id": 7,
                    "task": 3,
                    "completed_by": 1,
                    "created_at": "2026-10-29T05:42:48Z",
                    "updated_at": "2026-10-29T05:42:48Z",
                    "result": [],
                }
            ],
        }
        records = ls_verdicts.parse_export(payload)
        assert records[0]["annotator"] == "1"
        assert records[0]["annotator_kind"] == "human"

    def test_什么都读不到时显式标不可用(self) -> None:
        """读不到身份 ⇒ **显式标注**，不猜、不给空串。"""
        payload = {
            "tasks": [
                {"id": 3, "data": {"golden_id": "g1", "split": "calibration"}},
            ],
            "annotations": [
                {
                    "id": 7,
                    "task": 3,
                    "created_at": "2026-10-29T05:42:48Z",
                    "updated_at": "2026-10-29T05:42:48Z",
                    "result": [],
                }
            ],
        }
        records = ls_verdicts.parse_export(payload)
        assert records[0]["annotator"] == ls_verdicts.UNKNOWN
        assert records[0]["annotator_kind"] == "unknown"

    def test_裁决与分支按from_name取(self) -> None:
        record = _ls_records()[0]
        assert record["verdict"] == "disagree"
        assert record["human_branch"] == "blocked"
        assert record["reason"] == "理由"

    def test_多条标注取最后一条(self) -> None:
        payload = {
            "tasks": [{"id": 1, "data": {"golden_id": "g", "split": "calibration"}}],
            "annotations": [
                {"task": 1, "created_by": {"email": "a@x.test"},
                 "created_at": "2026-10-29T01:00:00+00:00",
                 "updated_at": "2026-10-29T01:00:00+00:00",
                 "result": [{"from_name": lc.VERDICT_CONTROL, "to_name": "task",
                             "type": "choices", "value": {"choices": ["agree"]}}]},
                {"task": 1, "created_by": {"email": "b@x.test"},
                 "created_at": "2026-10-29T02:00:00+00:00",
                 "updated_at": "2026-10-29T03:00:00+00:00",
                 "result": [{"from_name": lc.VERDICT_CONTROL, "to_name": "task",
                             "type": "choices", "value": {"choices": ["disagree"]}}]},
            ],
        }
        records = ls_verdicts.parse_export(payload)
        assert records[0]["verdict"] == "disagree"
        assert records[0]["annotator"] == "b@x.test"
        assert records[0]["annotation_count"] == 2

    def test_标注挂在task里也认(self) -> None:
        """⚠️ **LS 实测形态**（1.23.0）：标注**只挂在 task 自己的
        ``annotations`` 里**，顶层可能压根没有 ``annotations`` 键。

        初稿只读顶层的 ⇒ 那种形态下**每条都读成「没复核」**——
        而这是**最坏的一种错**：它不报错，只让「复核了 0 条」看起来像
        「一条都没复核」，人根本分不出是真没复核还是没读到。
        """
        payload = {
            "tasks": [
                {
                    "id": 11,
                    "data": {"golden_id": "g1", "split": "calibration"},
                    "annotations": [
                        {
                            "id": 501,
                            "task": 11,
                            "created_by": {"email": "owner@example.com"},
                            "created_at": "2026-10-29T01:00:00+00:00",
                            "updated_at": "2026-10-29T02:00:00+00:00",
                            "result": [
                                {"from_name": lc.VERDICT_CONTROL, "to_name": "task",
                                 "type": "choices", "value": {"choices": ["agree"]}},
                            ],
                        }
                    ],
                },
                {"id": 12, "data": {"golden_id": "g2", "split": "holdout"},
                 "annotations": []},
            ],
        }
        records = ls_verdicts.parse_export(payload)
        assert records[0]["verdict"] == "agree"
        assert records[0]["annotator"] == "owner@example.com"
        assert records[0]["annotation_count"] == 1
        # 没复核的那条仍然是「一行 + 不可用」，不是「没有这一行」
        assert len(records) == 2
        assert records[1]["verdict"] == ls_verdicts.UNKNOWN


# ── A6: 缺字段显式标注 ──────────────────────────────────────────


class TestMissingIsExplicit:
    def test_现场读不到写明不是没有控件(self) -> None:
        row = {
            "golden_id": "g", "task_id": "T", "run_id": "r", "url": "u",
            "source_kind": "archive_outcome",
            "golden": {"label": "success"},
        }
        data = exporter.build_golden_task_data(
            row,
            version_meta={"version": 14},
            clauses={"D-10": "x", "D-4": "y", "D-5": "z"},
            scene={"found": False, "status": "not_found", "note": "存档不存在"},
            trajectory=None,
        )
        assert "没读到现场" in data["scene_block"]
        assert "存档不存在" in data["scene_block"]
        assert data["scene_block"].strip() != ""

    def test_T视图读不到写明不是没做事(self) -> None:
        row = {
            "golden_id": "g", "task_id": "T", "run_id": "r", "url": "u",
            "source_kind": "archive_outcome",
            "golden": {"label": "success"},
        }
        data = exporter.build_golden_task_data(
            row,
            version_meta={"version": 14},
            clauses={"D-10": "x", "D-4": "y", "D-5": "z"},
            scene={"found": True, "status": "located"},
            trajectory=None,
        )
        assert "没读到 T 视图" in data["trajectory_block"]
        assert data["trajectory_block"].strip() != ""


# ── A7: 人类署名集合只有一个真源 ────────────────────────────────


class TestHumanSignatureSingleSource:
    def test_与仓库既有封条集合逐字一致(self) -> None:
        """``tests/unit/test_inc37_validator_pass.py`` 的 ``HUMAN_SIGNATURES``
        是人类身份封条那套集合——生产侧（``golden.signatures``）必须与它
        **逐字一致**。

        两份各写一遍会漂：漂了之后「人核过 82 条」这个数字会慢慢不对，
        而它正是本方案最有力的论据。
        """
        from trajectory_pipeline.evaluation.golden import signatures

        path = Path("trajectory_pipeline/tests/unit/test_inc37_validator_pass.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "HUMAN_SIGNATURES":
                        found = _literal_set(node.value)
        assert found is not None, "封条测试里找不到 HUMAN_SIGNATURES"
        assert set(found) == set(signatures.HUMAN_SIGNATURES), (
            f"生产侧 {sorted(signatures.HUMAN_SIGNATURES)} 与封条测试 "
            f"{sorted(found)} 不一致"
        )


# ── A8: 凭据扫描覆盖 task.data 全文 ────────────────────────────


class TestCredentialScan:
    def test_条款原文也在扫描范围内(self) -> None:
        """条款原文来自 rubric 文档；文档里若混进凭据形态，**也必须拒推**。"""
        from trajectory_pipeline.evaluation.ls_bridge.scan import scan_for_credentials
        from trajectory_pipeline.evaluation.ls_bridge.settings import (
            CredentialScanSettings,
        )

        payload = {"rubric_block": "参考 Authorization: Bearer abcdefghijklmnop"}
        hits = scan_for_credentials(
            payload, view="task_data", settings=CredentialScanSettings()
        )
        assert hits, "task.data 全文没被扫描"

    def test_命中即拒推(self) -> None:
        row = {
            "golden_id": "g", "task_id": "T", "run_id": "r", "url": "u",
            "source_kind": "archive_outcome",
            "golden": {"label": "success", "note": "sk-abcdefghijklmnopqrst"},
        }
        result = exporter.export_golden_rows(
            [row],
            version_meta={"version": 14},
            clauses={"D-10": "x", "D-4": "y", "D-5": "z"},
        )
        assert result.count == 0
        assert len(result.plan.rejected) == 1


# ── 台账（append-only，坏行跳过） ───────────────────────────────


class TestProjectLedger:
    def test_坏行跳过不抛(self, tmp_path: Path) -> None:
        from trajectory_pipeline.evaluation.golden_ls.projects import (
            GoldenProjectIndex,
        )

        path = tmp_path / "ledger.jsonl"
        path.write_text(
            "不是 json\n"
            + json.dumps({"version": 14, "project_id": 78, "title": "golden-v14"})
            + "\n",
            encoding="utf-8",
        )
        index = GoldenProjectIndex.load(path)
        assert index.has(14)
        assert index.get(14).project_id == 78

    def test_标题是版本号派生的(self) -> None:
        from trajectory_pipeline.evaluation.golden_ls.projects import project_title

        assert project_title(14) == "golden-v14"
