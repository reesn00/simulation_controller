"""label_studio.task_exporter 单元测试 (P1, 2026-09-28).

重点: 文件名解析 / 4 视图字段映射 / **R11 凭据扫描 fail-closed** / 批量过滤。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from c3_fixtures import QF_TEXT, RICH_MESSAGES, RICH_META
from conftest import write_c3

from label_studio.errors import C3ParseError, CredentialLeakDetected
from label_studio.settings import (
    CredentialScanSettings,
    LabelStudioSettings,
    ScorecardSettings,
    UploadSettings,
)
from label_studio.task_exporter import (
    RISK_HINTS_CONTROL,
    TASK_ANCHOR_CONTROL,
    build_prediction,
    build_task,
    build_task_data,
    export_batch,
    export_one,
    find_c3_files,
    iter_task_ids,
    parse_stem,
    push_batch,
    push_single_c3,
    render_risk_hints,
    scan_for_credentials,
)


# ---------------------------------------------------------------------------
# 文件名解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("T001__sess-1_refined", ("T001", "sess-1")),
        ("E030__sess-1_refined", ("E030", "sess-1")),
        ("T001__sess-1_refined.meta.json", ("T001", "sess-1")),
        ("T001__sess-1_refined.messages.json", ("T001", "sess-1")),
        ("T001__sess-1_refined.openai.json", ("T001", "sess-1")),
        ("T001__sess-1_refined.qwenjina.txt", ("T001", "sess-1")),
        ("T001__useramulation-2026_refined", ("T001", "useramulation-2026")),
        # 生产端实际形态: etl_worker 不加 _refined (2026-09-29 核实)。
        # 契约写的是带 _refined, 读取方两种都收, 否则真实 C3 全被跳过。
        ("T001__sess-1", ("T001", "sess-1")),
        ("T001__sess-1.meta.json", ("T001", "sess-1")),
        ("T001__sess-1.messages.json", ("T001", "sess-1")),
        ("T001__sess-1.openai.json", ("T001", "sess-1")),
        ("T001__sess-1.qwenjina.txt", ("T001", "sess-1")),
        ("T001__useramulation-fb7baa7545144ed6a3db3d55b03e5ade",
         ("T001", "useramulation-fb7baa7545144ed6a3db3d55b03e5ade")),
    ],
)
def test_parse_stem_variants(stem, expected):
    assert parse_stem(stem) == expected


def test_parse_stem_accepts_path():
    assert parse_stem(Path("output/refine_data/T007__abc_refined.meta.json")) == (
        "T007",
        "abc",
    )


def test_parse_stem_keeps_double_underscore_in_session():
    assert parse_stem("T001__a__b_refined") == ("T001", "a__b")
    assert parse_stem("T001__a__b") == ("T001", "a__b")


def test_parse_stem_session_id_may_itself_end_with_refined():
    """``_refined`` 是可选后缀, 不是 session_id 的一部分 —— 归属有歧义时
    取**最短** session_id (``_refined`` 视作后缀), 保证同一 session 无论
    磁盘上哪种形态都解析成同一个 id (``inner_id`` 去重依赖这一点)。"""
    assert parse_stem("T001__s_refined") == ("T001", "s")
    # 名字里更靠后的 _refined 无法与后缀区分 → 整体算 session_id
    assert parse_stem("T001__s_refined_v2") == ("T001", "s_refined_v2")


@pytest.mark.parametrize(
    "stem",
    [
        "no_prefix_refined",        # 缺 [TE]\d{3} 前缀
        "X001__s_refined",          # 前缀字母不合法
        "T1__s_refined",            # 位数不足
        "T001__",                   # session_id 为空
        "_refined",                 # 缺 task 前缀
    ],
)
def test_parse_stem_rejects_bad_names(stem):
    with pytest.raises(C3ParseError):
        parse_stem(stem)


# ---------------------------------------------------------------------------
# 4 视图字段映射
# ---------------------------------------------------------------------------


def test_build_task_data_maps_all_fields(rich_c3: Path):
    data, scorecard = build_task_data(rich_c3)
    assert data["task_id"] == "T001"
    assert data["session_id"] == RICH_META["session_id"]
    assert data["messages"]["messages"] == RICH_MESSAGES["messages"]
    assert data["qf_text"] == QF_TEXT
    assert data["openai"]["openai_messages"]
    assert data["training_value_score"] == 0.58
    assert data["complexity_tier"] == "medium"
    assert data["scorecard"]["schema_version"] == "scorecard.v1"


def test_criteria_are_flattened_to_strings(rich_c3: Path):
    """``criteria`` 必须是**字符串列表**。

    LS 的 perItem 文本控件只接受字符串, 传 dict 列表在 import 阶段就
    ``data['criteria']=...`` 400。所以这里从结构化 criterion 摊成人读的
    ``[VERDICT] id (REASON) — message`` 一行。
    """
    data, _ = build_task_data(rich_c3)
    criteria = data["criteria"]
    assert criteria and all(isinstance(row, str) for row in criteria)
    assert "C1" in criteria[0]
    assert "PASS" in criteria[0].upper()


def test_criteria_empty_when_meta_has_no_evaluation(rich_c3: Path):
    data, _ = build_task_data(rich_c3)
    assert isinstance(data["criteria"], list)


def test_display_twin_fields_are_strings(rich_c3: Path):
    """``*_text`` 孪生字段是给 LS 文本标签看的, 必须是 JSON 字符串。

    结构化原值保留 (``messages`` / ``metadata`` / ``scorecard``), 但 label_config
    一律绑孪生字段 —— LS 1.23 的 Text/TextEditor 碰到 dict/list 直接 400。
    """
    data, _ = build_task_data(rich_c3)
    for key, structured in (
        ("messages_text", data["messages"]),
        ("metadata_text", data["metadata"]),
        ("scorecard_text", data["scorecard"]),
    ):
        text = data[key]
        assert isinstance(text, str), key
        assert json.loads(text) == structured, key


def test_display_twins_keep_chinese_readable(rich_c3: Path):
    """``ensure_ascii=False`` —— 默认 True 会把中文转成 ``\\uXXXX``,
    标注员在 LS 里看到的是一串转义码。"""
    data, _ = build_task_data(rich_c3)
    assert "\\u" not in data["metadata_text"]


def test_scorecard_disabled_omits_text_twin(rich_c3: Path):
    data, _ = build_task_data(
        rich_c3, scorecard_settings=ScorecardSettings(enabled=False)
    )
    assert "scorecard" not in data
    assert "scorecard_text" not in data


def test_task_id_only_from_filename_not_meta(rich_c3: Path):
    """task_id 不在 meta.json 里 —— 只能从文件名解析。"""
    data, _ = build_task_data(rich_c3)
    assert "task_id" not in RICH_META
    assert data["task_id"] == "T001"


def test_inner_id_is_session_id(rich_c3: Path):
    data, scorecard = build_task_data(rich_c3)
    task = build_task(data, scorecard)
    assert task["inner_id"] == RICH_META["session_id"]


def test_missing_qwenjina_is_tolerated(c3_dir: Path):
    """qf_text 本就可能不存在 (F1 前的存量产物 / 无 user 轮)。"""
    path = write_c3(c3_dir, qf_text=None)
    data, _ = build_task_data(path)
    assert data["qf_text"] == ""


def test_missing_openai_is_tolerated(c3_dir: Path):
    path = write_c3(c3_dir, with_openai=False)
    data, _ = build_task_data(path)
    assert "openai" not in data


def test_missing_messages_is_fatal(c3_dir: Path):
    path = write_c3(c3_dir)
    path.with_name(path.name.replace(".meta.json", ".messages.json")).unlink()
    with pytest.raises(C3ParseError, match="messages"):
        build_task_data(path)


def test_corrupt_meta_is_fatal(c3_dir: Path):
    path = write_c3(c3_dir)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(C3ParseError, match="meta"):
        build_task_data(path)


def test_scorecard_disabled_omits_key(rich_c3: Path):
    data, card = build_task_data(rich_c3, scorecard_settings=ScorecardSettings(enabled=False))
    assert "scorecard" not in data
    assert card["enabled"] is False


# ---------------------------------------------------------------------------
# predictions
# ---------------------------------------------------------------------------


def test_prediction_result_is_a_region_list(rich_c3: Path):
    """``result`` 必须是 region **列表**。

    原实现发的是 dict, LS 1.23.0 实测回 ``201 {"created": 0}`` —— 201、零条
    prediction, 而上报当时还按 sent 兜底显示"推了 1 条"。一次假成功。
    """
    pred = build_prediction(*build_task_data(rich_c3))
    assert isinstance(pred["result"], list)
    for region in pred["result"]:
        assert set(region) >= {"from_name", "to_name", "type", "value"}, region


def test_prediction_regions_name_real_controls(rich_c3: Path):
    """``from_name`` 命中不了 label_config 里的控件 → 整条预测被静默丢弃。

    与 :class:`~tests.label_studio.test_label_config_xml` 里的
    ``test_prediction_control_matches_label_config`` 配对: 那边查 XML 里有这个
    控件, 这边查预测指向它, 两边都过才推得进去。
    """
    pred = build_prediction(*build_task_data(rich_c3))
    (region,) = pred["result"]
    assert region["from_name"] == RISK_HINTS_CONTROL
    assert region["to_name"] == TASK_ANCHOR_CONTROL
    assert region["type"] == "textarea"
    assert isinstance(region["value"]["text"], list)
    assert all(isinstance(line, str) for line in region["value"]["text"])


def test_prediction_never_sets_overall_decision(rich_c3: Path):
    data, card = build_task_data(rich_c3)
    pred = build_prediction(data, card)
    assert "overall_decision" not in str(pred)
    assert "suggested_accept" not in str(pred)
    assert all(r["from_name"] != "overall_decision" for r in pred["result"])


def test_prediction_carries_risk_hints(rich_c3: Path):
    data, card = build_task_data(rich_c3)
    pred = build_prediction(data, card)
    (region,) = pred["result"]
    text = "\n".join(region["value"]["text"])
    assert "C4" in text
    assert pred["task"] == data["session_id"]
    assert pred["model_version"].startswith("scorecard/")


def test_prediction_text_matches_task_data(rich_c3: Path):
    """预标注的文本与展示块绑的 ``$risk_hints_text`` 同源 —— 两条路都得有值,
    少一条标注员就看不到"机器已经查过什么"。"""
    data, _ = build_task_data(rich_c3)
    (region,) = build_prediction(data, data["scorecard"])["result"]
    assert "\n".join(region["value"]["text"]) == data["risk_hints_text"]


def test_risk_hints_never_blank(rich_c3: Path):
    """无命中时也必须有文案 —— 空块会被读成"推送失败"。

    兜底文案由 ``scorecard.build_risk_hints`` 独家负责 (它自己补「自动检查未见
    异常」那条); 本模块**不复制第二份**, 复制必然漂移。
    """
    assert render_risk_hints(None) == ""
    assert render_risk_hints({"enabled": True, "dimensions": []}).strip()
    data, _ = build_task_data(rich_c3)
    assert data["risk_hints_text"].strip()


def test_prediction_scores_by_confidence(rich_c3: Path):
    data, card = build_task_data(rich_c3)
    pred = build_prediction(data, card)
    assert 0.0 < pred["score"] <= 1.0


def test_prediction_none_when_disabled(rich_c3: Path):
    data, card = build_task_data(rich_c3, scorecard_settings=ScorecardSettings(enabled=False))
    assert build_prediction(data, card) is None


# ---------------------------------------------------------------------------
# R11 凭据扫描
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,pattern",
    [
        ({"headers": {"Authorization": "Bearer abc123def456ghi"}}, r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
        ({"note": "sk-abcdefghij1234567890"}, r"\bsk-[A-Za-z0-9]{16,}"),
        ({"token": "ghp_abcdefghijklmnopqrstuvwxyz01"}, r"\bghp_[A-Za-z0-9]{20,}"),
        ({"aws": "AKIAIOSFODNN7EXAMPLE"}, r"\bAKIA[0-9A-Z]{16}\b"),
        ({"pem": "-----BEGIN RSA PRIVATE KEY-----"}, r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ],
)
def test_scan_detects_credential_shapes(payload, pattern):
    scan = CredentialScanSettings(patterns=(pattern,))
    hits = scan_for_credentials(payload, view="messages", settings=scan)
    assert len(hits) == 1
    assert hits[0].pattern_index == 0


def test_scan_hit_records_location_not_content():
    """命中只留位置 —— 记录原文等于把凭据抄进日志 (§16 R9)。"""
    scan = CredentialScanSettings(patterns=(r"\bsk-[A-Za-z0-9]{16,}",))
    secret = "sk-abcdefghij1234567890"
    hits = scan_for_credentials({"a": secret}, view="messages", settings=scan)
    assert secret not in hits[0].describe()
    assert "offset=" in hits[0].describe()


def test_scan_disabled_is_noop():
    scan = CredentialScanSettings(enabled=False)
    assert scan_for_credentials({"x": "Bearer abc123def"}, view="m", settings=scan) == []


def test_scan_clean_payload_returns_empty():
    scan = CredentialScanSettings()
    payload = {"messages": [{"content": "推荐三款耳机"}], "qf_text": QF_TEXT}
    assert scan_for_credentials(payload, view="m", settings=scan) == []


def test_scan_survives_invalid_regex():
    scan = CredentialScanSettings(patterns=("[unclosed",))
    assert scan_for_credentials({"a": 1}, view="m", settings=scan) == []


def test_export_one_rejects_credential_bearing_c3(c3_dir: Path):
    """fail-closed: 命中即拒推, **不静默脱敏**。

    静默脱敏会让标注员看到的样本与训练用样本不一致, 污染标注语义。
    """
    meta = copy.deepcopy(RICH_META)
    path = write_c3(
        c3_dir,
        messages={"messages": [{"role": "user", "content": "sk-abcdefghij1234567890"}]},
        meta=meta,
    )
    task, pred, hits = export_one(path)
    assert task is None and pred is None
    assert hits and hits[0].view == "messages"


def test_export_batch_rejects_credential_task_but_keeps_clean_one(c3_dir: Path):
    write_c3(c3_dir, task_id="T001", session_id="bad-1",
             messages={"messages": [{"content": "Bearer abcdefghijklmn"}]})
    write_c3(c3_dir, task_id="T002", session_id="good-1")
    plan = export_batch(c3_dir, settings=LabelStudioSettings())
    assert [t["data"]["task_id"] for t in plan.tasks] == ["T002"]
    assert [stem for stem, _ in plan.rejected] == ["T001__bad-1_refined"]
    assert plan.rejected[0][1][0].view in ("messages", "qf_text", "openai", "metadata")


def test_push_single_c3_raises_on_credential_hit(c3_dir: Path, settings):
    path = write_c3(c3_dir, messages={"messages": [{"content": "sk-aaaaaaaaaaaaaaaaaaaa"}]})
    with pytest.raises(CredentialLeakDetected):
        push_single_c3(path, settings=settings, project_id=1,
                       client_factory=lambda: None)


# ---------------------------------------------------------------------------
# 批量计划
# ---------------------------------------------------------------------------


def test_find_c3_files_only_returns_meta(c3_dir: Path):
    write_c3(c3_dir, task_id="T001", session_id="a")
    write_c3(c3_dir, task_id="T002", session_id="b")
    found = find_c3_files(c3_dir)
    assert len(found) == 2
    assert all(p.name.endswith(".meta.json") for p in found)


def test_find_c3_files_on_missing_dir(tmp_path: Path):
    assert find_c3_files(tmp_path / "nope") == []


def test_batch_plan_shape(rich_c3: Path, c3_dir: Path):
    plan = export_batch(c3_dir, settings=LabelStudioSettings())
    assert plan.count == 1
    assert len(plan.predictions) == 1
    assert plan.skipped == [] and plan.rejected == []
    assert "待推送 1 条" in plan.summary()


def test_batch_skips_unparsable_filename(c3_dir: Path):
    write_c3(c3_dir)
    (c3_dir / "garbage.meta.json").write_text("{}", encoding="utf-8")
    plan = export_batch(c3_dir, settings=LabelStudioSettings())
    assert plan.count == 1
    assert any("garbage" in stem for stem, _ in plan.skipped)


def test_batch_filter_by_task_id(c3_dir: Path):
    write_c3(c3_dir, task_id="T001", session_id="a")
    write_c3(c3_dir, task_id="T002", session_id="b")
    plan = export_batch(c3_dir, settings=LabelStudioSettings(), task_id="T002")
    assert [t["data"]["task_id"] for t in plan.tasks] == ["T002"]


def test_batch_filter_by_min_score(c3_dir: Path):
    write_c3(c3_dir, task_id="T001", session_id="a")           # 0.58
    sparse = copy.deepcopy(RICH_META)
    sparse["training_value_score"] = 0.2
    write_c3(c3_dir, task_id="T002", session_id="b", meta=sparse)
    plan = export_batch(c3_dir, settings=LabelStudioSettings(), min_score=0.5)
    assert [t["data"]["task_id"] for t in plan.tasks] == ["T001"]
    assert plan.skipped[0][0] == "T002__b_refined"


def test_batch_filter_by_complexity_tier(c3_dir: Path):
    write_c3(c3_dir, task_id="T001", session_id="a")           # medium
    easy = copy.deepcopy(RICH_META)
    easy["complexity_tier"] = "easy"
    write_c3(c3_dir, task_id="T002", session_id="b", meta=easy)
    settings = LabelStudioSettings(
        upload=UploadSettings(filter_complexity_tiers=("easy",))
    )
    plan = export_batch(c3_dir, settings=settings)
    assert [t["data"]["task_id"] for t in plan.tasks] == ["T002"]


def test_batch_respects_skip_task_ids(c3_dir: Path):
    write_c3(c3_dir, task_id="T001", session_id="a")
    settings = LabelStudioSettings(upload=UploadSettings(skip_task_ids=("T001",)))
    plan = export_batch(c3_dir, settings=settings)
    assert plan.count == 0
    assert "skip_task_ids" in plan.skipped[0][1]


def test_batch_no_predictions_when_disabled(c3_dir: Path):
    write_c3(c3_dir)
    plan = export_batch(c3_dir, settings=LabelStudioSettings(), include_predictions=False)
    assert plan.predictions == []


def test_iter_task_ids_dedupes(c3_dir: Path):
    write_c3(c3_dir, task_id="T001", session_id="a")
    write_c3(c3_dir, task_id="T001", session_id="b")
    write_c3(c3_dir, task_id="T002", session_id="c")
    assert list(iter_task_ids(c3_dir)) == ["T001", "T002"]


def test_empty_dir_yields_empty_plan(tmp_path: Path):
    plan = export_batch(tmp_path / "empty", settings=LabelStudioSettings())
    assert plan.count == 0 and plan.summary().startswith("待推送 0 条")


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------


class FakeClient:
    """模拟 LS 1.23 的两个关键行为:

    1. ``/import`` **只回计数**, 不回 task id —— 所以调用方得回头查。
    2. ``import/predictions`` 的 ``task`` 只认数字 id, 且**不去重** ——
       推几次就是几条。
    """

    def __init__(self) -> None:
        self.imported: list[list] = []
        self.predicted: list[list] = []
        self._next_id = 100
        self._tasks: list[dict] = []

    def import_tasks(self, project_id, tasks):
        batch = list(tasks)
        self.imported.append(batch)
        for task in batch:
            self._next_id += 1
            self._tasks.append({"id": self._next_id, "data": task.get("data", {})})
        return len(batch)

    def list_recent_tasks(self, project_id, *, limit=100):
        # LS 1.23 的 /api/projects/{id}/tasks 是**倒序**的
        return list(reversed(self._tasks))[:limit]

    def import_predictions(self, project_id, preds):
        batch = list(preds)
        self.predicted.append(batch)
        return len(batch)


def test_push_single_c3_roundtrip(rich_c3: Path, settings):
    client = FakeClient()
    result = push_single_c3(
        rich_c3, settings=settings, project_id=7, client_factory=lambda: client
    )
    assert result["pushed"] == 1
    assert result["task_id"] == "T001"
    assert result["rejected"] is False
    assert client.imported[0][0]["data"]["scorecard"]["dimensions"]


def test_push_single_c3_records_ls_task_id(rich_c3: Path, settings):
    """台账要记下 LS 侧的数字 id —— 预标注的 ``task`` 字段只认它。"""
    from label_studio.push_index import PushIndex

    client = FakeClient()
    push_single_c3(rich_c3, settings=settings, project_id=7, client_factory=lambda: client)
    index = PushIndex.load(PushIndex.default_path(7, settings.output_root))
    ls_task_id = index.task_id(result_session(rich_c3))
    assert isinstance(ls_task_id, int)
    assert client.predicted[0][0]["task"] == ls_task_id


def result_session(meta_path: Path) -> str:
    from label_studio.task_exporter import build_task_data

    data, _ = build_task_data(meta_path)
    return str(data["session_id"])


def test_push_single_c3_skips_already_pushed(rich_c3: Path, settings):
    """重跑 hook 不推重复 —— LS 1.23 那边没有任何去重挡板。"""
    client = FakeClient()
    first = push_single_c3(
        rich_c3, settings=settings, project_id=7, client_factory=lambda: client
    )
    second = push_single_c3(
        rich_c3, settings=settings, project_id=7, client_factory=lambda: client
    )
    assert first["pushed"] == 1
    assert second["pushed"] == 0
    assert len(client.imported) == 1


def test_push_single_c3_can_skip_prediction(rich_c3: Path, settings):
    client = FakeClient()
    push_single_c3(
        rich_c3, settings=settings, project_id=7,
        include_prediction=False, client_factory=lambda: client,
    )
    assert client.predicted == []


def test_push_batch_respects_batch_size(c3_dir: Path, tmp_path: Path):
    for i in range(5):
        write_c3(c3_dir, task_id="T001", session_id=f"s{i}")
    client = FakeClient()
    settings = LabelStudioSettings(
        upload=UploadSettings(batch_size=2), output_root=tmp_path / "output"
    )
    plan = export_batch(c3_dir, settings=settings)
    result = push_batch(plan, settings=settings, project_id=1,
                        client_factory=lambda: client)
    assert result["tasks_pushed"] == 5
    assert result["batches"] == 3
    assert [len(b) for b in client.imported] == [2, 2, 1]
    # predictions 与 task 批次对齐 (LS 要求 task 已存在)
    assert [len(b) for b in client.predicted] == [2, 2, 1]


def test_push_batch_dedupes_against_index(c3_dir: Path, tmp_path: Path):
    """同一批推两次, 第二次全跳过。

    LS 1.23 既不认字符串 inner_id 也不按它去重 (实测 inner_id=42 推三次 →
    id 7/8/9), 所以本地台账是唯一的挡板。
    """
    for i in range(3):
        write_c3(c3_dir, session_id=f"s{i}")
    settings = LabelStudioSettings(output_root=tmp_path / "output")
    first = FakeClient()
    second = FakeClient()
    push_batch(export_batch(c3_dir, settings=settings), settings=settings,
               project_id=1, client_factory=lambda: first)
    result = push_batch(export_batch(c3_dir, settings=settings), settings=settings,
                        project_id=1, client_factory=lambda: second)
    assert result["tasks_pushed"] == 0
    assert result["skipped_duplicate"] == 3
    assert second.imported == []


def test_push_batch_survives_unresolvable_task_ids(c3_dir: Path, tmp_path: Path):
    """取不回 id 时 task 照样推成功 —— 只是没有预标注。"""
    for i in range(2):
        write_c3(c3_dir, session_id=f"s{i}")

    class NoIds(FakeClient):
        def list_recent_tasks(self, project_id, *, limit=100):
            raise RuntimeError("LS 抽风")

    client = NoIds()
    settings = LabelStudioSettings(output_root=tmp_path / "output")
    result = push_batch(export_batch(c3_dir, settings=settings), settings=settings,
                        project_id=1, client_factory=lambda: client)
    assert result["tasks_pushed"] == 2
    assert result["predictions_pushed"] == 0


def test_push_batch_reports_progress(c3_dir: Path, tmp_path: Path):
    for i in range(3):
        write_c3(c3_dir, session_id=f"s{i}")
    settings = LabelStudioSettings(
        upload=UploadSettings(batch_size=1), output_root=tmp_path / "output"
    )
    plan = export_batch(c3_dir, settings=settings)
    seen: list[tuple[int, int]] = []
    push_batch(plan, settings=settings, project_id=1,
               client_factory=lambda: FakeClient(),
               on_progress=lambda d, t: seen.append((d, t)))
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_push_batch_on_empty_plan_is_noop(tmp_path: Path):
    result = push_batch(
        export_batch(tmp_path, settings=LabelStudioSettings()),
        settings=LabelStudioSettings(output_root=tmp_path / "output"), project_id=1,
    )
    assert result == {
        "tasks_pushed": 0,
        "predictions_pushed": 0,
        "batches": 0,
        "skipped_duplicate": 0,
    }


# ---------------------------------------------------------------------------
# 低分标记字段 (audit_text)
# ---------------------------------------------------------------------------


def test_audit_text_always_present(rich_c3: Path):
    """正常样本也必须有 audit_text。

    label_config 的 ``$audit_text`` 是**无条件绑定**的: 字段缺失时 LS 把那块
    渲染成空白框, 标注员分不清是"没被拒收"还是"渲染坏了"。所以这里显式
    写「（无）」, 用一行噪音换掉一整类误解。
    """
    data, scorecard = build_task_data(rich_c3)
    assert scorecard["enabled"] is True
    assert data["audit_text"] == "（无）"


def test_audit_text_carries_reason_and_note(rich_c3: Path):
    meta_path = Path(rich_c3)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["audit_reason"] = "judge_discard"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    data, scorecard = build_task_data(meta_path)
    assert scorecard["audit"]["reason"] == "judge_discard"
    assert "【低分样本】" in data["audit_text"]
    assert "judge_discard" not in data["audit_text"]   # 翻成了人话
    assert "结构合格" in data["audit_text"]
