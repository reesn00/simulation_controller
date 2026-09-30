"""label_studio.task_exporter: C3 4 视图文件 → LS ``task.data`` + ``predictions``.

方案 §4.1 的字段映射在这里落地。三点实施要点:

1. **``task_id`` 不在 meta.json 里** —— 只能从文件名 stem 解析
   (``T001__useramulation-xxx`` , 契约里的 ``_refined`` 后缀可选, 见
   :data:`STEM_PATTERN`)。
2. **不持有 raw trajectory** —— C3 已是脱敏产物, 本模块只读 ``*.messages.json`` /
   ``*.openai.json`` / ``*.qwenjina.txt`` / ``*.meta.json`` 四份**视图文件**,
   绝不碰 C1 trajectory。
3. **R11 凭据扫描 fail-closed** —— 命中即 :class:`CredentialLeakDetected`,
   **不静默脱敏**。静默脱敏会让标注员看到的样本与训练用样本不一致, 污染标注
   语义; 宁可拒推让人来决定。

去重靠 LS 原生 ``inner_id = session_id``（方案 §16 R7）—— 不引本地索引文件。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from label_studio.errors import C3ParseError, CredentialLeakDetected
from label_studio.scorecard import build_risk_hints, build_scorecard
from label_studio.settings import (
    CredentialScanSettings,
    LabelStudioSettings,
    ScorecardSettings,
    UploadSettings,
)

log = logging.getLogger(__name__)

#: C3 文件 stem 形态: ``<TXXX|EXXX>__<session_id>`` , ``_refined`` 后缀可选。
#:
#: 契约 (CLAUDE.md / docs/contracts) 写的是带 ``_refined``, 但**生产端
#: ``etl_worker._output_filename(task_id, session_id, suffix="")`` 并不加这个
#: 后缀** —— 磁盘上真实的 C3 是 ``T001__<session_id>.meta.json``。原先这里强制
#: 匹配 ``_refined``, 结果上传器把每个真实 C3 都当坏名跳过, ``upload`` 恒推 0 条。
#:
#: 这里做成**两种形态都收**: 生产端改名会波及训练侧 glob 与已落盘的 C3, 属于
#: 契约变更, 不该由读取方单方面决定; 读取方保持兼容则新旧文件都能推。
#: ``session_id`` 本身可能含 ``__``, 故用非贪婪 + 尾部锚定; 结尾的 ``_refined``
#: 在匹配后剥掉 (见 :func:`parse_stem`)。
STEM_PATTERN = re.compile(
    r"^(?P<task_id>[TE]\d{3})__(?P<session_id>.+?)(?:_refined)?$"
)

#: 4 视图文件名后缀（``save_session_v2`` 产出）。
_SUFFIX_MESSAGES = ".messages.json"
_SUFFIX_OPENAI = ".openai.json"
_SUFFIX_QWENJINA = ".qwenjina.txt"
_SUFFIX_META = ".meta.json"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_stem(path_or_stem: Path | str) -> tuple[str, str]:
    """从 C3 文件名解析 ``(task_id, session_id)``。

    接受完整路径或裸 stem。``*.meta.json`` / ``*.messages.json`` 等 4 视图
    后缀会先剥掉再匹配。

    Raises:
        C3ParseError: 形态不符 —— 这是**数据问题**, 调用方按 task 跳过。
    """
    stem = Path(path_or_stem).name if isinstance(path_or_stem, (Path,)) else str(path_or_stem)
    for suffix in (_SUFFIX_META, _SUFFIX_MESSAGES, _SUFFIX_OPENAI):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    else:
        if stem.endswith(".qwenjina.txt"):
            stem = stem[: -len(".qwenjina.txt")]
    if stem.endswith(".txt"):
        stem = stem[: -len(".txt")]

    matched = STEM_PATTERN.match(stem)
    if not matched:
        raise C3ParseError(
            f"无法从 C3 文件名解析 task_id/session_id: {stem!r} "
            f"(期望形如 T001__<session_id>_refined)"
        )
    return matched.group("task_id"), matched.group("session_id")


# ---------------------------------------------------------------------------
# R11 凭据扫描
# ---------------------------------------------------------------------------


@dataclass
class ScanHit:
    """一次扫描命中记录。**只记录位置, 不记录命中的原文**（§16 R9）。"""

    view: str
    pattern_index: int
    char_offset: int

    def describe(self) -> str:
        return f"view={self.view} pattern#{self.pattern_index} offset={self.char_offset}"


def scan_for_credentials(
    payload: Any, *, view: str, settings: CredentialScanSettings
) -> list[ScanHit]:
    """在 payload 中扫描凭据形态。

    扫描**序列化后的文本**而非 Python 对象 —— tool_call 的 ``input`` 可能是
    任意嵌套 dict, 序列化后统一成一条可 grep 的文本流, 也不会漏掉被
    ``str()`` 掩盖的值。
    """
    if not settings.enabled:
        return []
    try:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        # 不可序列化本身就是异常形态, 交给上游 C3 解析报错, 这里不重复拦。
        return []

    hits: list[ScanHit] = []
    for index, source in enumerate(settings.patterns):
        try:
            matched = re.search(source, text)
        except re.error:
            log.warning("scan_for_credentials: pattern#%d 非法正则, 跳过", index)
            continue
        if matched:
            hits.append(ScanHit(view=view, pattern_index=index, char_offset=matched.start()))
    return hits


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


@dataclass
class ExportPlan:
    """一次批量推送的计划 + 逐条结果。不含任何凭据。"""

    tasks: list[dict[str, Any]] = field(default_factory=list)
    predictions: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (stem, reason)
    rejected: list[tuple[str, list[ScanHit]]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.tasks)

    def summary(self) -> str:
        return (
            f"待推送 {len(self.tasks)} 条 / 预标注 {len(self.predictions)} 条 / "
            f"跳过 {len(self.skipped)} 条 / 拒推 {len(self.rejected)} 条"
        )


def find_c3_files(refine_dir: Path) -> list[Path]:
    """列出 ``refine_dir`` 下所有 C3 的 ``*.meta.json``（以 meta 为入口找齐 4 视图）。"""
    directory = Path(refine_dir)
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"*{_SUFFIX_META}"))


def _load_json(path: Path, view: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise C3ParseError(f"C3 {view} 缺失: {path}") from exc
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise C3ParseError(f"C3 {view} 解析失败 {path.name}: {exc}") from exc


def _as_text(value: Any) -> str:
    """结构化值 → 给 LS 文本标签看的可读字符串（缩进 2, 保留中文）。

    ``ensure_ascii=False`` 是必须的: 默认 True 会把中文转成 ``\\uXXXX``,
    标注员在 LS 里看到的是一串转义码。
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _flatten_criteria(meta: dict[str, Any]) -> list[str]:
    """把 ``criterion_results.criteria`` 摊成给人读的一行一条字符串。

    判定值统一**大写** —— 标注员看到的是 "PASS" / "FAIL", 与本项目内部的小写
    枚举区分开, 避免两边对同一字符串理解不一致。

    ⚠️ 曾经返回的是 list 并直接绑给 perItem 控件的 ``<Text>`` 锚点, **实测行不通**
    （见 label_config 里「指令核对」段的说明）。现在只作为
    :func:`_criteria_text` 的中间产物, 不再进 ``task.data``。
    """
    evaluation = meta.get("criterion_results")
    criteria = (evaluation or {}).get("criteria") if isinstance(evaluation, dict) else None
    if not isinstance(criteria, list):
        return []
    rows: list[str] = []
    for item in criteria:
        if not isinstance(item, dict) or not item.get("criterion_id"):
            continue
        verdict = str(item.get("verdict") or "").upper()
        reason = str(item.get("reason_code") or "").strip()
        message = str(item.get("message") or "").strip()
        parts = [f"[{verdict}]", str(item.get("criterion_id"))]
        if reason:
            parts.append(f"({reason})")
        if message:
            parts.append(f"— {message}")
        rows.append(" ".join(parts))
    return rows


#: 没有 criterion_results 时显示的字样。**恒给这句话, 不给空白框** ——
#: 空白框分不清是"没跑验证"还是"渲染坏了", 与 audit_text 同一个道理。
_NO_CRITERIA_TEXT = "（本样本没有 criterion_results —— simulate 端未产出或未注入 C3）"


def _criteria_text(meta: dict[str, Any]) -> str:
    """criterion 逐条清单 → **一个换行分隔的字符串**（给 TextArea 展示块）。

    必须是字符串而不是字符串列表: LS 把 list 绑给 ``<Text>`` 会用 ``,`` 连成
    一整段（实测 6 条 criterion 在标注页上是**一行**逗号连文, 逐条核对无从下手）,
    绑给文本控件又直接 400 ``data['criteria']=...``。换行分隔的字符串两头都对。
    """
    rows = _flatten_criteria(meta)
    return "\n".join(rows) if rows else _NO_CRITERIA_TEXT


def build_task_data(
    meta_path: Path,
    *,
    scorecard_settings: ScorecardSettings | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """C3 meta.json → ``(task.data, scorecard)``。

    4 视图中缺哪份都不阻断 —— ``qf_text`` 本就可能不存在（F1 前的旧产物
    或无 user 轮的 session）, ``messages`` 缺失才是硬错误。

    Raises:
        C3ParseError: meta 或 messages 缺失 / 损坏。
    """
    meta_path = Path(meta_path)
    task_id, stem_session = parse_stem(meta_path)
    meta = _load_json(meta_path, "meta")
    if not isinstance(meta, dict):
        raise C3ParseError(f"C3 meta 顶层不是对象: {meta_path.name}")

    base = meta_path.name[: -len(_SUFFIX_META)]
    messages_path = meta_path.with_name(base + _SUFFIX_MESSAGES)
    openai_path = meta_path.with_name(base + _SUFFIX_OPENAI)
    qwenjina_path = meta_path.with_name(base + _SUFFIX_QWENJINA)

    messages = _load_json(messages_path, "messages")
    openai = _load_json(openai_path, "openai") if openai_path.exists() else None
    qf_text = (
        qwenjina_path.read_text(encoding="utf-8")
        if qwenjina_path.exists() else None
    )

    session_id = str(meta.get("session_id") or stem_session)
    scorecard = build_scorecard(
        meta, task_id=task_id, session_id=session_id, settings=scorecard_settings
    )

    data: dict[str, Any] = {
        "task_id": task_id,
        "session_id": session_id,
        "messages": messages,
        "qf_text": qf_text or "",
        "metadata": meta,
        "training_value_score": meta.get("training_value_score"),
        "complexity_tier": meta.get("complexity_tier"),
        # 指令核对区的展示块。**换行分隔的单串, 不是列表** —— 理由见
        # _criteria_text 的 docstring。结构化 criterion 原值仍在
        # data["metadata"]["criterion_results"]["criteria"]。
        "criteria_text": _criteria_text(meta),
    }
    # 展示用字符串孪生字段。LS 1.23 的 Text/TextArea/TextEditor 绑定到结构化
    # 值 (dict / list) 时 import 直接 400 ``data['messages']=...`` —— 只有
    # <Table> 之类结构感知标签能吃结构化数据。结构化原值保留给下游脚本用,
    # label_config 一律绑这些 *_text 孪生字段。
    data["messages_text"] = _as_text(messages)
    data["metadata_text"] = _as_text(meta)
    if openai is not None:
        data["openai"] = openai
    # 低分标记。**恒存在** —— label_config 的 $audit_text 无条件绑定这个字段,
    # 让它有时无时会踩两条路: 字段缺失时 LS 把那一块渲染成空白框, 标注员
    # 分不清是"没被拒收"还是"渲染坏了"。所以正常样本显式写"（无）"。
    audit = scorecard.get("audit")
    data["audit_text"] = (
        f"【低分样本】{audit.get('label')}\n{audit.get('note')}"
        if isinstance(audit, dict) and audit.get("audited")
        else "（无）"
    )
    if scorecard.get("enabled"):
        data["scorecard"] = scorecard
        # ⚠️ 唯一**不是** JSON 孪生的 *_text 字段 —— 评分卡压成 JSON 不可读
        # (实测 5679 字符, 打开停在 evidence 中段)。见 render_scorecard_text。
        data["scorecard_text"] = render_scorecard_text(scorecard)
        # 风险提示既进 label_config 的只读展示块, 也进 predictions 预标注 ——
        # 两条路都得有值, 少一条标注员就看不到"机器已经查过什么"。
        data["risk_hints_text"] = render_risk_hints(scorecard)
    return data, scorecard


#: 预标注落地的控件名。**必须与 label_config 里的 name 逐字一致** —— LS 按
#: ``from_name`` 匹配控件, 对不上就静默丢弃整条预测 (实测 201 + created:0)。
#: 由 ``tests/label_studio/test_label_config_xml.py`` 守住两侧一致。
RISK_HINTS_CONTROL = "risk_hints"
TASK_ANCHOR_CONTROL = "task_anchor"


def render_risk_hints(scorecard: dict[str, Any] | None) -> str:
    """风险提示 → 给 label_config 只读展示块 / prediction 预标注的**单段字符串**。

    :func:`label_studio.scorecard.build_risk_hints` 产出 ``list[str]``（方案
    §5.2 的四行提示表），并**保证非空** —— 无命中时它自己补「自动检查未见异常」
    那一条。这里只做「列表 → 一段文本」的收敛, 不再兜底: 兜底文案留一份就够,
    复制两份必然漂移 (第一版就在这栽了)。

    LS 的 TextArea 只吃字符串, 预标注的 ``value.text`` 也要求字符串列表。
    """
    if not scorecard:
        return ""
    return "\n".join(f"· {h}" for h in build_risk_hints(scorecard))


#: 单个字段 / 单条依据的渲染上限。超了截断标 "…" —— 完整内容在
#: ``task.data["scorecard"]`` 结构化列里, 展示块不复述。
_RENDER_LIMIT = 240

#: 维度 dict 里这些键当表头单独排版, 不混进字段列表。
_SCORECARD_HEAD_KEYS = frozenset(
    {"id", "label", "score", "score_kind", "source", "evidence"}
)


def _clip(text: str, limit: int = _RENDER_LIMIT) -> str:
    """折行压成一行再截断。JSON 里的换行会在 TextArea 里变成多行, 把
    维度列表冲散, 标注员就看不出哪条依据属于哪一维。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + " …"


def _fmt(value: Any) -> str:
    """任意值 → 人读的一小段文本。``None`` / 空串统一显示 ``—``。

    布尔走中文 (``是`` / ``否``) —— ``score=False`` 与 ``score=0`` 在评分卡里
    含义完全不同 (红线"没违规" vs 覆盖率 0), 印成 ``False`` 容易被扫成"没分"。

    容器**不整体 JSON 化**: L0 的 ``fail_evaluation`` 有十几个键, L4 的
    ``components`` 是七维分量表, ``json.dumps`` 出来都是一坨带引号括号的
    字符串, 标注员要读的 ``failure_category`` / ``health`` 埋在中间。拆成
    ``k=v · k=v`` 才扫得出来; 只有嵌套容器才退回紧凑 JSON 并截断。
    """
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, dict):
        parts = [
            f"{key}={_fmt(item)}"
            for key, item in value.items()
            if item not in (None, "", [], {})
        ]
        return _clip(" · ".join(parts) if parts else "—")
    if isinstance(value, (list, tuple)):
        parts = [_fmt(item) for item in value if item not in (None, "", [], {})]
        if parts and all(not isinstance(item, (dict, list, tuple)) for item in value):
            return _clip("、".join(parts))
        return _clip(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))
    return _clip(value)


def render_scorecard_text(scorecard: dict[str, Any] | None) -> str:
    """评分卡 → 给标注页展示块看的**人读文本**（不是 JSON 孪生）。

    ⚠️ 这与 ``messages_text`` / ``metadata_text`` 不同: 那两个是纯 JSON 孪生
    （内容本来就是 JSON, 换个排版就没法还原), 而评分卡是**有结构的判定**,
    压成 ``json.dumps(indent=2)`` 之后 5679 个字符里有一半是括号和引号 ——
    实测 2026-09-30 在标注页上打开, TextArea 直接停在 evidence 数组中段,
    标注员第一眼看到的是半行 ``should_revise_task": false,``, 而最该先看的
    ``overall.suggested_decision`` 在几百行之上。这里改成「结论先行 + 维度
    分行 + 依据逐条」。

    **不丢信息**: 结构化原值仍完整保留在 ``task.data["scorecard"]``
    (JSON_MIN 导出里是独立的 ``scorecard`` 列), 展示块只是它的可读投影。
    超长字段截断标 ``…``, 就是提示"完整内容在那儿"。

    通用渲染, 不按维度 id 写分支 —— 六个维度各写一套必然随 builder 漂移。
    """
    if not scorecard:
        return ""

    lines: list[str] = []
    overall = scorecard.get("overall")
    if isinstance(overall, dict):
        lines.append(
            f"建议判定  {_fmt(overall.get('suggested_decision'))}"
            f"（置信度 {_fmt(overall.get('confidence'))}）"
        )
        for key in ("derivation", "note"):
            text = overall.get(key)
            if text:
                lines.append(f"          {text}")
    summary = scorecard.get("dimension_summary")
    if isinstance(summary, dict) and summary:
        lines.append(
            "维度覆盖  "
            + " · ".join(f"{k}={_fmt(v)}" for k, v in summary.items())
        )
    lines.append("")

    dimensions = scorecard.get("dimensions")
    for index, dim in enumerate(dimensions or []):
        if not isinstance(dim, dict):
            continue
        head = f"L{index} {dim.get('label') or dim.get('id') or '?'}"
        lines.append(
            f"── {head} ──  {_fmt(dim.get('score'))} {dim.get('score_kind') or ''}"
            f" · source={_fmt(dim.get('source'))}"
        )
        extra = [
            f"{key}={_fmt(value)}"
            for key, value in dim.items()
            if key not in _SCORECARD_HEAD_KEYS and value not in (None, "", [], {})
        ]
        if extra:
            lines.append("   " + " · ".join(extra))
        evidence = dim.get("evidence") or []
        if evidence:
            lines.append(f"   依据 {len(evidence)} 条：")
            lines.extend(f"     · {_fmt(item)}" for item in evidence)
        lines.append("")

    return "\n".join(lines).rstrip()


def build_task(
    data: dict[str, Any], scorecard: dict[str, Any] | None = None
) -> dict[str, Any]:
    """包成 LS import 需要的 task 对象。

    ``inner_id = session_id`` —— LS 原生去重键（方案 §16 R7）。同一 session 重跑
    ``upload`` 时 LS 覆盖而非新增, 所以不引本地索引文件。
    """
    return {"data": data, "inner_id": data["session_id"]}


#: ``overall.confidence`` 枚举 → LS ``score`` 数值。LS 用它给预测排序, 不参与
#: 标注语义; 映射只为让"低置信度的建议"排在后面, 人先看高置信度的。
_CONFIDENCE_TO_SCORE = {"high": 0.9, "medium": 0.6, "low": 0.3}


def build_prediction(
    data: dict[str, Any], scorecard: dict[str, Any] | None
) -> dict[str, Any] | None:
    """ML 预标注（方案 §5.2）—— **只填风险提示, 绝不填 overall_decision**。

    ``overall_decision`` 留空强制人工选择: 自动 accept/reject 预判会把
    最该看的 hard 样本自动 reject 掉, 与方案 §3 自相矛盾。

    ⚠️ ``result`` 必须是 ``[{from_name, to_name, type, value}]`` **region 列表**,
    且 ``from_name`` 必须是 label_config 里**真实存在**的控件名。这两条都是
    LS 1.23.0 实测 (2026-09-29), 而且**违反时 LS 一声不吭**:
    ``POST .../import/predictions`` 照样回 ``201 {"created": 0}``。

    实测三种 payload 的结果 (一次性项目, 推完查 ``/api/tasks/{id}``)::

        result 是 dict                       → {"created": 0}  建了 0 条
        result 是 region 列表, from_name 命中 → {"created": 1}  建了 1 条 ✓
        result 是 region 列表, from_name 没命中 → {"created": 0}  建了 0 条

    原实现把 risk_hints 塞进 ``result`` 的自由键里, 想着"不在控件名里也照样
    存下来供审计" —— **那是不成立的**: LS 按 ``from_name`` 逐条匹配控件, 匹配
    不上就整条丢弃。所以风险提示必须有控件可落, 即 label_config 里的
    ``<TextArea name="risk_hints" ...>``。
    """
    if scorecard is None or not scorecard.get("enabled"):
        return None
    overall = scorecard.get("overall") or {}
    return {
        "task": data["session_id"],
        "result": [
            {
                "from_name": RISK_HINTS_CONTROL,
                "to_name": TASK_ANCHOR_CONTROL,
                "type": "textarea",
                "value": {"text": render_risk_hints(scorecard).splitlines()},
            }
        ],
        "model_version": f"scorecard/{scorecard.get('schema_version', 'scorecard.v1')}",
        "score": _CONFIDENCE_TO_SCORE.get(str(overall.get("confidence")), 0.5),
    }


def export_one(
    meta_path: Path,
    *,
    scorecard_settings: ScorecardSettings | None = None,
    scan_settings: CredentialScanSettings | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[ScanHit]]:
    """单条 C3 → ``(task, prediction, scan_hits)``。

    扫描命中时返回 ``(None, None, hits)`` —— **由调用方决定 fail-closed 还是
    skip**, 本函数不自行抛异常, 便于批量场景逐条记录。
    """
    data, scorecard = build_task_data(meta_path, scorecard_settings=scorecard_settings)
    scan = scan_settings or CredentialScanSettings()

    hits: list[ScanHit] = []
    for view in ("messages", "qf_text", "openai", "metadata"):
        if view in data and data[view]:
            hits.extend(scan_for_credentials(data[view], view=view, settings=scan))
    if hits:
        return None, None, hits

    return build_task(data, scorecard), build_prediction(data, scorecard), []


def export_batch(
    refine_dir: Path,
    *,
    settings: LabelStudioSettings | None = None,
    task_id: str | None = None,
    min_score: float | None = None,
    complexity_tier: str | None = None,
    include_predictions: bool | None = None,
) -> ExportPlan:
    """扫目录 → 逐条导出 → 过滤 → 汇总成 :class:`ExportPlan`。

    过滤顺序：``skip_task_ids`` → ``tier`` → ``min_score`` → 凭据扫描。
    凭据扫描放在**最后**是有意的：过滤是"这次不推"，扫描是"这个样本有问题"，
    两者的处置完全不同（前者静默跳过即可，后者必须留痕）。
    """
    settings = settings or LabelStudioSettings()
    upload: UploadSettings = settings.upload
    plan = ExportPlan()
    predictions_on = (
        upload.include_predictions if include_predictions is None else include_predictions
    )
    min_score = upload.filter_min_training_value_score if min_score is None else min_score
    tiers = settings.tier_filter()

    for meta_path in find_c3_files(refine_dir):
        stem = meta_path.name[: -len(_SUFFIX_META)]
        try:
            parsed_task_id, _ = parse_stem(meta_path)
        except C3ParseError as exc:
            plan.skipped.append((stem, str(exc)))
            continue

        if task_id and parsed_task_id != task_id:
            continue
        if parsed_task_id in upload.skip_task_ids:
            plan.skipped.append((stem, f"task_id 在 skip_task_ids 中: {parsed_task_id}"))
            continue

        try:
            data, scorecard = build_task_data(
                meta_path, scorecard_settings=settings.scorecard
            )
        except C3ParseError as exc:
            plan.skipped.append((stem, str(exc)))
            continue

        tier = data.get("complexity_tier")
        if tiers and tier not in tiers:
            plan.skipped.append((stem, f"complexity_tier={tier} 不在 filter 中"))
            continue
        score = data.get("training_value_score")
        if isinstance(score, (int, float)) and score < min_score:
            plan.skipped.append((stem, f"training_value_score={score} < {min_score}"))
            continue

        hits: list[ScanHit] = []
        for view in ("messages", "qf_text", "openai", "metadata"):
            if data.get(view):
                hits.extend(
                    scan_for_credentials(
                        data[view], view=view, settings=settings.credential_scan
                    )
                )
        if hits:
            plan.rejected.append((stem, hits))
            log.error(
                "export_batch: %s 命中凭据模式, fail-closed 拒推: %s",
                stem, [h.describe() for h in hits],
            )
            continue

        plan.tasks.append(build_task(data, scorecard))
        if predictions_on:
            prediction = build_prediction(data, scorecard)
            if prediction:
                plan.predictions.append(prediction)

    return plan


def push_single_c3(
    meta_path: Path,
    *,
    settings: LabelStudioSettings,
    project_id: int,
    include_prediction: bool = True,
    client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """推送单条 C3（orchestration hook 与 ``--task-id`` 调试共用）。

    Args:
        client_factory: 注入 client 的工厂（测试用）；None 时按 settings 建。

    Returns:
        ``{"pushed": int, "rejected": bool, "reason": str|None, "task_id", "session_id"}``

    Raises:
        CredentialLeakDetected: 扫描命中（fail-closed）。
        C3ParseError: C3 缺失 / 形态不符。
    """
    task, prediction, hits = export_one(
        meta_path,
        scorecard_settings=settings.scorecard,
        scan_settings=settings.credential_scan,
    )
    task_id, _ = parse_stem(meta_path)
    if hits:
        # fail-closed: 静默脱敏会污染标注语义, 拒推并留痕让人决定。
        raise CredentialLeakDetected(
            f"C3 {meta_path.name} 命中凭据模式, 已拒推: "
            f"{[h.describe() for h in hits]}"
        )
    if task is None:
        raise C3ParseError(f"C3 导出为空: {meta_path.name}")

    if client_factory is None:
        from label_studio.client import build_client as _build_client

        client = _build_client(settings)
    else:
        client = client_factory()

    from label_studio.push_index import PushIndex

    index = PushIndex.load(
        PushIndex.default_path(project_id, settings.output_root)
    )
    session_id = str(task["inner_id"])
    if index.has(session_id):
        # 重跑 hook 会推重复 —— LS 1.23 既不认字符串 inner_id 也不按它去重,
        # 所以本地台账是唯一的挡板。
        return {
            "pushed": 0,
            "rejected": False,
            "reason": "已推送过, 跳过",
            "task_id": task_id,
            "session_id": session_id,
        }

    pushed = client.import_tasks(project_id, [task])
    ls_task_id: int | None = None
    try:
        recent = client.list_recent_tasks(project_id, limit=1)
        for item in recent:
            if str((item.get("data") or {}).get("session_id") or "") == session_id:
                candidate = item.get("id")
                if isinstance(candidate, int):
                    ls_task_id = candidate
                break
    except Exception as exc:  # noqa: BLE001 - 取不到 id 不该让 task 推送算失败
        log.warning("push_single_c3: 取不回 LS task id, 预标注跳过: %s", exc)
    if ls_task_id is not None:
        index.record(session_id, ls_task_id, task_ref=task_id)
        if include_prediction and prediction:
            client.import_predictions(
                project_id, [{**prediction, "task": ls_task_id}]
            )
    return {
        "pushed": pushed,
        "rejected": False,
        "reason": None,
        "task_id": task_id,
        "session_id": session_id,
    }


def push_batch(
    plan: ExportPlan,
    *,
    settings: LabelStudioSettings,
    project_id: int,
    client_factory: Callable[[], Any] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    index: Any | None = None,
) -> dict[str, Any]:
    """按 ``upload.batch_size`` 分批推送。``plan`` 为空时直接返回。

    Args:
        index: :class:`~label_studio.push_index.PushIndex`, 记录
            ``session_id → LS task id``。**不传就退化成不判重**（推重复）,
            所以生产路径必须传。

    去重说明: LS 1.23 **既不认字符串 inner_id、也不按 inner_id 去重**
    （实测 inner_id=42 连推三次得到 id 7/8/9), 所以判重只能靠本地台账。
    推完一批回头捞 LS 刚建的那批 id 存进台账 —— 同一份数据同时供判重和
    预标注 (``import/predictions`` 的 ``task`` 只认数字 id) 使用。
    """
    from label_studio.push_index import PushIndex

    if not plan.tasks:
        return {
            "tasks_pushed": 0,
            "predictions_pushed": 0,
            "batches": 0,
            "skipped_duplicate": 0,
        }

    if client_factory is None:
        from label_studio.client import build_client as _build_client

        client = _build_client(settings)
    else:
        client = client_factory()

    if index is None:
        index = PushIndex.load(
            PushIndex.default_path(project_id, settings.output_root)
        )

    size = max(1, settings.upload.batch_size)
    tasks_pushed = 0
    predictions_pushed = 0
    batches = 0
    skipped_duplicate = 0
    # predictions 按 session_id 对齐到 task 批次 —— LS 要求 task 已存在。
    pred_by_session = {p["task"]: p for p in plan.predictions}

    # 判重: 台账里有的一律不再推。LS 侧那份被删干净时台账会过期,
    # 但**宁可不推也不推重复** —— 重复样本会污染标注统计, 少推一条补一次
    # ``purge`` 索引就能找回。
    fresh: list[dict[str, Any]] = []
    for task in plan.tasks:
        session_id = str(task.get("inner_id") or "")
        if session_id and index.has(session_id):
            skipped_duplicate += 1
            continue
        fresh.append(task)
    if skipped_duplicate:
        log.info(
            "push_batch: %d 条已推过, 本次跳过 (LS 端不按 inner_id 去重)",
            skipped_duplicate,
        )

    for start in range(0, len(fresh), size):
        batch = fresh[start : start + size]
        tasks_pushed += client.import_tasks(project_id, batch)

        # 解析这批刚建出来的 LS task id。import 的返回体只有计数不带 id。
        pairs: list[tuple[str, int]] = []
        try:
            recent = client.list_recent_tasks(project_id, limit=len(batch))
        except Exception as exc:  # noqa: BLE001 - 预标注不该拖垮 task 推送
            log.warning(
                "push_batch: 取不回刚推送的 task id, 预标注将跳过: %s", exc
            )
            recent = []
        id_by_session = {
            str((item.get("data") or {}).get("session_id") or ""): item.get("id")
            for item in recent
        }
        for task in batch:
            session_id = str(task.get("inner_id") or "")
            ls_task_id = id_by_session.get(session_id)
            if isinstance(ls_task_id, int):
                pairs.append((session_id, ls_task_id))
        if pairs:
            index.record_many(
                pairs,
                task_refs={
                    str(t.get("inner_id") or ""): str(
                        (t.get("data") or {}).get("task_id") or ""
                    )
                    for t in batch
                },
            )

        preds = []
        for session_id, ls_task_id in pairs:
            pred = pred_by_session.get(session_id)
            if pred is not None:
                preds.append({**pred, "task": ls_task_id})
        if preds:
            predictions_pushed += client.import_predictions(project_id, preds)
        batches += 1
        if on_progress:
            on_progress(min(start + size, len(fresh)), len(fresh))

    return {
        "tasks_pushed": tasks_pushed,
        "predictions_pushed": predictions_pushed,
        "batches": batches,
        "skipped_duplicate": skipped_duplicate,
    }


def iter_task_ids(refine_dir: Path) -> Iterator[str]:
    """遍历目录下出现的 task_id（去重、保序）—— 供 CLI 报告。"""
    seen: set[str] = set()
    for meta_path in find_c3_files(refine_dir):
        try:
            parsed, _ = parse_stem(meta_path)
        except C3ParseError:
            continue
        if parsed not in seen:
            seen.add(parsed)
            yield parsed


__all__ = [
    "ExportPlan",
    "ScanHit",
    "STEM_PATTERN",
    "build_prediction",
    "build_task",
    "build_task_data",
    "export_batch",
    "export_one",
    "find_c3_files",
    "iter_task_ids",
    "parse_stem",
    "push_batch",
    "push_single_c3",
    "scan_for_credentials",
]
