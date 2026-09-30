"""label_studio.task_exporter 单元测试 (P1, 2026-09-28).

重点: 文件名解析 / 4 视图字段映射 / **R11 凭据扫描 fail-closed** / 批量过滤。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from c3_fixtures import LEGACY_RICH_META, QF_TEXT, RICH_MESSAGES, RICH_META
from conftest import write_c3

from label_studio.errors import C3ParseError, CredentialLeakDetected
from label_studio.settings import (
    CredentialScanSettings,
    LabelStudioSettings,
    ScorecardSettings,
    UploadSettings,
)
from label_studio.task_exporter import (
    _NO_OPENAI_TEXT,
    _fmt_ts,
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
    render_openai_text,
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
    assert "qf_text" not in data, "qwenjina 内容不再上传 LS（2026-09-30 起）"
    assert data["openai"]["openai_messages"]
    assert data["training_value_score"] == 0.58
    assert data["complexity_tier"] == "medium"
    assert data["scorecard"]["schema_version"] == "scorecard.v1"


def test_timeline_fields_present(rich_c3: Path):
    """时间记录四字段 + 展示串。之前 task.data 没有任何时间, 标注员分不清
    轨迹是哪天跑的; 2026-09-30 起补齐（用户要求）。"""
    data, _ = build_task_data(rich_c3)
    # 只有 user 消息带 created_at → 起止同点, 收敛成一个时间
    assert data["session_started_at"] == "2026-09-30 04:50:12 UTC"
    assert data["session_ended_at"] == "2026-09-30 04:50:12 UTC"
    # qf_rendered_at 提为顶层字段（该键刻意不随视图载荷一起剥掉 ——
    # 它是 C3 渲染时间的唯一来源, 剥了标注页时间线就空）
    assert data["c3_rendered_at"] == "2026-09-30 00:00:00 UTC"
    assert data["pushed_at"].endswith(" UTC")
    text = data["timeline_text"]
    assert "轨迹 2026-09-30 04:50:12 UTC" in text, "同点起止收敛, 不渲染 t → t"
    assert "C3 渲染" in text
    assert "推送" in text and "轨迹时间缺失" not in text


def test_criteria_text_is_newline_separated(rich_c3: Path):
    """``criteria_text`` 是**换行分隔的单串**, 每行一条 ``[VERDICT] id (REASON) — message``。

    曾经是字符串列表绑给 perItem 锚点, 现在改成单串有两个原因, 都是实测:
      * LS 把 list 绑给 ``<Text>`` 会用 ``,`` 连成一整段 —— 6 条 criterion 在
        标注页上是**一行**逗号连文, 逐条核对连读都读不下去
      * 绑给文本控件又会 ``data['criteria']=...`` 400
    换行分隔的字符串两头都对。
    """
    data, _ = build_task_data(rich_c3)
    text = data["criteria_text"]
    assert isinstance(text, str)
    rows = text.splitlines()
    assert len(rows) >= 2, "多条 criterion 必须各占一行"
    assert "C1" in rows[0]
    assert "PASS" in rows[0].upper()
    assert "," not in text, "行内不该出现逗号分隔(那是 LS 拼 list 的方式)"


def test_criteria_text_never_blank_when_absent(tmp_path: Path):
    """没有 criterion_results 时给**明确字样**, 不给空白框。

    空白框分不清是"没跑验证"还是"渲染坏了" —— 与 ``audit_text`` 同一个道理。
    """
    from c3_fixtures import RICH_MESSAGES, RICH_META
    from conftest import write_c3

    meta = {k: v for k, v in RICH_META.items() if k != "criterion_results"}
    meta_path = write_c3(
        tmp_path / "refine_data", meta=meta, messages=RICH_MESSAGES
    )
    data, _ = build_task_data(meta_path)
    assert data["criteria_text"].strip()
    assert "criterion_results" in data["criteria_text"]


def test_criteria_list_no_longer_in_task_data(rich_c3: Path):
    """``criteria`` 列表已下线 —— 它只服务于失效的 perItem 锚点。

    结构化 criterion 原值仍在 ``data["metadata"]["criterion_results"]``,
    留着这个扁平列表只会多一份可能漂移的副本。
    """
    data, _ = build_task_data(rich_c3)
    assert "criteria" not in data
    assert data["metadata"]["criterion_results"]["criteria"]


def test_display_twin_fields_are_strings(rich_c3: Path):
    """``*_text`` 孪生字段是给 LS 文本标签看的, 必须是 JSON 字符串。

    结构化原值保留 (``messages`` / ``metadata`` / ``scorecard``), 但 label_config
    一律绑孪生字段 —— LS 1.23 的 Text/TextEditor 碰到 dict/list 直接 400。

    ⚠️ ``scorecard_text`` **不在这个列表里** —— 它是唯一渲染成人读文本的
    ``*_text`` 字段 (见 test_scorecard_text_is_readable_not_json)。
    """
    data, _ = build_task_data(rich_c3)
    for key, structured in (
        ("messages_text", data["messages"]),
        ("metadata_text", data["metadata"]),
    ):
        text = data[key]
        assert isinstance(text, str), key
        assert json.loads(text) == structured, key


def test_scorecard_text_is_readable_not_json(rich_c3: Path):
    """``scorecard_text`` 是**可读投影**, 不是 JSON 孪生。

    实测 (2026-09-30, project 30 task 9): 压成 ``json.dumps(indent=2)`` 是
    5679 字符, 标注页 TextArea 打开直接停在 evidence 数组中段, 第一眼是半行
    ``should_revise_task": false,`` —— 而最该先看的 suggested_decision 在
    几百行之上。所以结论必须排在最前, 维度分行, 依据逐条。
    """
    data, _ = build_task_data(rich_c3)
    text = data["scorecard_text"]

    # 不是 JSON 了 —— 完整结构化原值仍在 data["scorecard"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    assert json.loads(json.dumps(data["scorecard"])) == data["scorecard"]

    # 结论先行
    head = text.splitlines()[0]
    assert head.startswith("建议判定")
    assert "accept" in head or "revise" in head or "reject" in head

    # 六个维度都在, 且带 L 编号与 source
    for index, dim in enumerate(data["scorecard"]["dimensions"]):
        assert f"L{index} {dim['label']}" in text, dim["id"]
    assert text.count("source=") == len(data["scorecard"]["dimensions"])

    # 依据逐条展开, 不是塞在一行 JSON 里
    assert "依据 6 条：" in text or "依据" in text
    assert "{" not in text.splitlines()[0]


def test_scorecard_text_keeps_missing_dimensions_explicit(rich_c3: Path):
    """``source=missing`` 的维度也要占一行并写出不可用原因。

    静默丢掉会让标注员以为"没有这一项", 而实际是"跑不了" —— 两者对
    accept/reject 的含义完全相反。
    """
    data, _ = build_task_data(rich_c3)
    text = data["scorecard_text"]
    missing = [
        d for d in data["scorecard"]["dimensions"] if d.get("source") == "missing"
    ]
    for dim in missing:
        assert f"L{data['scorecard']['dimensions'].index(dim)} {dim['label']}" in text
        assert dim["unavailable_because"] in text


def test_scorecard_text_renders_booleans_in_chinese(rich_c3: Path):
    """``False`` 印成 ``否`` 而不是 ``False``。

    红线维的 ``score=False`` 是"没违规"(好), 与覆盖率 ``0`` (全灭) 在
    评分卡里含义相反; 印成英文 ``False`` 容易被扫成"没分"。
    """
    data, _ = build_task_data(rich_c3)
    text = data["scorecard_text"]
    # 依据里的 retryable 是布尔
    assert "retryable=否" in text or "retryable=是" in text
    assert "retryable=False" not in text


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


def test_qwenjina_is_never_uploaded(c3_dir: Path):
    """qwenjina.txt 即使在磁盘上也**不进 task.data**（2026-09-30 起不上传）。

    旧版会把 ``*.qwenjina.txt`` 全文读进 ``data["qf_text"]``, 且 meta.json
    内嵌的 ``qf_text`` 留底还经 ``metadata`` 二次上传 —— ChatML 全文等于推了
    两遍。现在展示块换成 openai 视图, 两处一起剥。

    用 :data:`LEGACY_RICH_META`（meta 内嵌 qf_text 的旧形态）才能真正测到
    剥离: 新形态的 meta 压根没有这个键, 断言会空转。磁盘上仍可能存在
    存量产物, 剥离逻辑作为防御保留。
    """
    path = write_c3(c3_dir, qf_text=QF_TEXT, meta=LEGACY_RICH_META)
    data, _ = build_task_data(path)
    assert "qf_text" not in data
    assert "qf_text" not in data["metadata"]
    assert "qf_text" not in json.loads(data["metadata_text"])


def test_metadata_view_payload_keys_are_pruned(c3_dir: Path):
    """旧形态 meta.json 里的 4 视图载荷仍不进 LS —— 存量防御。

    2026-09-30 起 C3 磁盘 meta 已不含这些键（``_VIEW_PAYLOAD_KEYS``）,
    但存量产物仍带; 推送侧 ``_META_VIEW_PAYLOAD_KEYS`` 保留为防御。
    审计键（criterion_results 等）必须原样保留。
    """
    path = write_c3(c3_dir, meta=LEGACY_RICH_META)
    data, _ = build_task_data(path)
    for key in ("openai_messages", "tools", "qf_text", "qf_stats", "qf_rendered_at"):
        assert key not in data["metadata"], key
        assert key not in json.loads(data["metadata_text"]), key
    assert data["metadata"]["criterion_results"]
    assert data["metadata"]["session_id"]


def test_new_format_meta_audit_keys_are_uploaded(rich_c3: Path):
    """新格式 meta 的审计替代键 (``tools_declared`` / ``views``) **要**上传。

    它们是 meta 承担审计能力的凭据 —— 未截断工具名清单 + 各视图的尺寸 /
    sha256。剥离清单只针对视图**内容**, 不能顺手把审计指针也剥掉。
    """
    data, _ = build_task_data(rich_c3)
    assert data["metadata"]["tools_declared"] == ["search"]
    assert set(data["metadata"]["views"]) == {"messages", "openai", "qwenjina"}
    assert "tools_declared" in json.loads(data["metadata_text"])


def test_missing_openai_is_tolerated(c3_dir: Path):
    path = write_c3(c3_dir, with_openai=False)
    data, _ = build_task_data(path)
    assert "openai" not in data
    # 展示块恒有值, 不给空白框（与 criteria_text / audit_text 同一原则）
    assert data["openai_text"].strip()


def test_openai_text_is_readable_not_json(rich_c3: Path):
    """``openai_text`` 是**人读渲染**, 不是 JSON 孪生 —— 同 scorecard_text 的理由。

    ``json.dumps(indent=2)`` 的话 role/正文埋在引号里扫不出对话流。结构化
    原值仍在 ``data["openai"]``。
    """
    data, _ = build_task_data(rich_c3)
    text = data["openai_text"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    assert data["openai"]["openai_messages"]
    # 消息分节头可扫
    assert "── [1] user" in text
    assert "hi" in text


def test_render_openai_text_message_shapes():
    """五种消息形态全接：system / user / assistant(+reasoning+tool_calls) /
    tool / 收尾 assistant。"""
    openai = {
        "openai_messages": [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "先想一步",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "Skill", "arguments": {"skill": "x"}},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "Skill", "content": "OUT"},
            {"role": "assistant", "content": "FINAL"},
        ],
        "tools": [{"type": "function", "function": {"name": "Skill", "description": "d"}}],
    }
    text = render_openai_text(openai)
    assert "── [1] system" in text and "S" in text
    assert "── [2] user" in text
    assert "── [3] assistant · tool_calls=1" in text
    assert "[reasoning]" in text and "先想一步" in text
    assert "（无文本内容）" in text, "content=None 的 assistant 要有明确字样"
    assert "→ tool_call call_1 name=Skill" in text
    assert '"skill": "x"' in text, "dict arguments 应 pretty-print"
    assert "── [4] tool · name=Skill · call=call_1" in text and "OUT" in text
    assert "FINAL" in text
    assert "── tools (1) ──" in text and "· Skill — d" in text


def test_render_openai_text_handles_argument_variants():
    """arguments 三态：JSON 字符串 → pretty；坏 JSON → 原样；其他 → str()。"""
    openai = {
        "openai_messages": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "A", "arguments": '{"a": 1}'}},
                {"id": "c2", "type": "function",
                 "function": {"name": "B", "arguments": "not json"}},
            ]},
        ],
        "tools": [],
    }
    text = render_openai_text(openai)
    assert '"a": 1' in text, "JSON 字符串 arguments 反序列化后 pretty-print"
    assert "not json" in text, "解析失败的裸串原样放行, 不抛"
    assert "── tools (0) ──" in text


def test_render_openai_text_never_blank():
    """缺失 / 空消息列表都给明确字样 —— 不给空白框（与 criteria_text 同一原则）。"""
    assert render_openai_text(None) == _NO_OPENAI_TEXT
    assert render_openai_text({"openai_messages": [], "tools": []}).strip()
    assert render_openai_text({"no_messages_key": 1}) == _NO_OPENAI_TEXT


# ---------------------------------------------------------------------------
# 时间记录
# ---------------------------------------------------------------------------


def test_timeline_span_from_first_and_last_user_message(c3_dir: Path):
    """轨迹起止取**非空** created_at 的首尾 —— 只有 user 轮带时间（真实形态）。"""
    messages = {"messages": [
        {"role": "system", "content": "S", "created_at": ""},
        {"role": "user", "content": "U1", "created_at": "2026-09-30T04:50:12.972292+00:00"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2", "created_at": "2026-09-30T04:55:42+00:00"},
    ]}
    path = write_c3(c3_dir, messages=messages, meta={
        **RICH_META, "qf_rendered_at": "2026-09-30T04:51:45.490758Z",
    })
    data, _ = build_task_data(path)
    assert data["session_started_at"] == "2026-09-30 04:50:12 UTC"
    assert data["session_ended_at"] == "2026-09-30 04:55:42 UTC"
    # 两种源格式（+00:00 / Z）归一到同一展示格式, 微秒去掉
    assert data["c3_rendered_at"] == "2026-09-30 04:51:45 UTC"
    text = data["timeline_text"]
    assert "轨迹 2026-09-30 04:50:12 UTC → 2026-09-30 04:55:42 UTC" in text
    assert "C3 渲染" in text


def test_timeline_missing_times_say_so(c3_dir: Path):
    """消息全无 created_at 时给明确字样, 但推送时间恒在 —— 不给空白框。"""
    messages = {"messages": [{"role": "user", "content": "U"}]}
    path = write_c3(c3_dir, messages=messages)
    data, _ = build_task_data(path)
    assert data["session_started_at"] == "" and data["session_ended_at"] == ""
    assert "轨迹时间缺失" in data["timeline_text"]
    assert "推送" in data["timeline_text"]


def test_fmt_ts_keeps_unparseable_and_naive():
    """解析失败原样放行（展示层不做校验器）; 无时区的裸时间不加 UTC 后缀。"""
    assert _fmt_ts("") == ""
    assert _fmt_ts("not-a-date") == "not-a-date"
    assert _fmt_ts("2026-09-30T04:50:12") == "2026-09-30 04:50:12"


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
    payload = {
        "messages": [{"content": "推荐三款耳机"}],
        "openai": {"openai_messages": [{"role": "user", "content": "hi"}]},
    }
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
    assert plan.rejected[0][1][0].view in ("messages", "openai", "metadata")


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
