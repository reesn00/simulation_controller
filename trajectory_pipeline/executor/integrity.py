"""P1 存档完整性门禁——把「逐条打开存档读 evidence 核对」变成能跑的检查。

CLAUDE.md 把「每次真实运行后都要逐条打开存档读 evidence 核对」写成固定动作，
理由是「设计上的五类缺陷离线测试测不出来」。但**纯手工的固定动作等于没有**：
它靠人记得做，而它恰恰是最容易在赶批次时跳过的一步——设计时五类缺陷全靠它兜底。

所以把它做成门禁。这里实现的就是它的机械部分：跨存档比对、找自相矛盾、
找承诺了但没落地的。**判断「这条结论对不对」仍然不在这里**——那要人读证据，
本模块只保证「值得人读的东西没有悄悄烂掉」。

三档严重度，含义是「要不要拦下这批」而不是「严不严重」
------------------------------------------------------------
``FATAL``
    存档不可信。证据已损坏、或结论与存档自己的观察打架。
    **不可事后修**——只能重跑。
``DEGRADED``
    存档本身能读，但切出来的样本缺件，不能当训练数据。
    与 ``assembler.schema`` 的 ``degraded_from`` 同一套标记。
``INFO``
    值得看一眼，不影响可用性。

选择档案口径与报表一致
------------------------
检查范围由 :func:`~trajectory_pipeline.executor.archive.select_archives` 给出，
**不另写一套挑选规则**：探针取证文件、``.reviewed.json`` 的取代关系一旦在这里
分叉，就会出现「门禁说 4 个档全过、报表说 9 个档里 5 个有问题」，而两边都看着像对的。

不 import assembler
-------------------
见 ``tests/contract/test_p2_contract.py::TestNoExecutorImport`` 同一条纪律的反面：
本模块读 P1 是读**存档形状**，不是读 P2 的切条逻辑。两侧对「缺件」有各自的
权威定义（这里报门禁、那里报 ``degraded_from``），靠
:func:`degraded_marker_map` 显式对齐，而不是让一边 import 另一边。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from trajectory_pipeline.executor.archive import select_archives
from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES

#: 替换字符。**出现即证据链断裂且不可恢复**。
#:
#: 实测（2026-10-09）现存 12 份真实存档里，**37 条 outcome 的 evidence 无一损坏**；
#: 坏的是**某一个站的一处正文**——4 份 T001 存档的 ``visits[3]``
#: （tv.sohu.com）``body_preview`` 中间 109..225 那段，以及同页一个 ``<a>``
#: 的 label。同一段里中文与 ASCII 都好，只有中间夹着 U+01F3 / U+0439
#: （Latin 扩展与西里尔字母）那一小片烂成 U+FFFD。
#:
#: 三个由此确定、且都不是「落盘编码写错」的事实：
#:  1. 落盘路径是干净的——``archive.py`` 显式 ``encoding="utf-8"``，
#:     文件本身也是**合法 UTF-8**（严格 decode 通过）。
#:  2. 损坏发生在**页面的局部文本**，不是整档换个编码写错了。整档错编码
#:     会连 ``导航`` / ``倍速`` 这些正常中文一起烂掉，实测它们是好的。
#:  3. 所以触发条件是**站点声明的 charset 与实际内容不一致**，
#:     同一批里 11 个站没事、同一个站 4 次全坏——重跑同一批不会变好。
#:
#: ⚠️ **别把它当成「终端乱码」放过去**。Windows 控制台把 UTF-8 渲染成
#: ``ý���`` 是常事（第一次查这批存档时被这个骗过一次），那种情况下
#: raw 字节是好的、``reconfigure_stdio`` 能救。这里救不了：只有落盘后
#: 重新解码才能区分，所以门禁一律用 ``strict``。
#: 用转义写而不是字面量：U+FFFD 打进源码会经历各类工具链的规范化，
#: 而这正是「不可恢复地丢一轮字节」的那个字符——它自己的定义不该依赖工具链。
REPLACEMENT_CHAR = "\ufffd"

#: 观察里的媒体标签计数键名。``video_tag_count`` 覆盖 ``<video>`` 与
#: ``<audio>``（见 ``executor/dom.py``），所以一个键就够。
_MEDIA_KEY = "video_tag_count"


class Severity(str, Enum):
    FATAL = "FATAL"
    DEGRADED = "DEGRADED"
    INFO = "INFO"


#: 本模块的 DEGRADED 码 → ``assembler.schema`` 会写进 ``degraded_from`` 的标记。
#:
#: **两张表靠这里对齐，靠测试盯住**：``tests/unit/test_integrity.py::
#: TestDegradedMarkersMatchSplit`` 造一份缺件齐全的存档，同时跑
#: :func:`check_archive` 与 ``schema.split_archive``，断言两者给出的缺件集合相等。
#: 谁先改谁就红。直接照抄 schema 的判断会漂——那份判断在 ``_obs_missing`` /
#: ``split_archive`` 两处，不是单点。
def degraded_marker_map() -> dict[str, str]:
    return {
        "steps_missing": "steps",
        "user_prompt_missing": "user_prompt",
        "run_config_missing": "run_config",
        "body_preview_only": "body_preview_only",
        "no_body": "no_body",
        "outcome_missing": "outcome_missing",
        "success_branch_mismatch": "success_branch_mismatch",
    }


@dataclass(frozen=True, slots=True)
class Finding:
    """一条门禁发现。``where`` 是 JSON 路径式定位，出错时能直接翻到那一处。"""

    code: str
    severity: Severity
    archive: str
    where: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 仅格式化
        return f"[{self.severity.value}] {self.archive} {self.where}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ArchiveReport:
    """一份存档的体检结果。"""

    name: str
    findings: tuple[Finding, ...] = ()
    sites: int = 0
    outcomes: int = 0
    negatives: int = 0

    @property
    def fatal(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.FATAL)

    @property
    def ok(self) -> bool:
        """单份存档有没有 FATAL。``check_archive`` 也是公开入口，
        调用方手上只有一份报告时不该被逼着去拼 :class:`IntegrityReport`。"""
        return not self.fatal


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    archives: tuple[ArchiveReport, ...] = ()
    notes: tuple[str, ...] = ()
    #: 池级发现（负样本被成功记录推翻等）。**不属于任何一份存档**——
    #: 它是「池 vs 全库」的性质，单挂在某一份存档上会重复 N 遍。
    pool_findings: tuple[Finding, ...] = ()

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(f for a in self.archives for f in a.findings) + self.pool_findings

    def by_severity(self, severity: Severity) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is severity)

    @property
    def ok(self) -> bool:
        """没有任何 FATAL。**DEGRADED 不影响本批可用性**——那批样本降级重建，
        不是这批存档作废。

        池级 FATAL 计入：它意味着**已经写出去的标签是错的**，
        而已经落盘的东西不会因为「这批存档本身没问题」而变对。
        """
        return not self.by_severity(Severity.FATAL)


# ── 字符串遍历（找 U+FFFD）─────────────────────────────────────────

def _iter_strings(node: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    """深度遍历出 ``(JSON 路径, 字符串)``。

    递归而不是 ``re.search(整份 JSON 文本)``：后者会命中 URL 或 query 里
    恰好带 ``%EF%BF%BD`` 的情况（那反而是**对**的信号），但分不清是哪个字段烂了。
    门禁的价值在于「翻过去就能看见」，所以定位比计数重要。
    """
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for k, v in node.items():
            yield from _iter_strings(v, f"{path}.{k}")
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _iter_strings(v, f"{path}[{i}]")


# ── 观察缺件 ───────────────────────────────────────────────────────

def _obs_degraded(obs: Any, where: str, out: list[Finding], name: str) -> None:
    """与 ``schema._obs_missing`` 同一口径，但**分别实现**。

    分开实现是刻意的：executor 不 import assembler（见模块 docstring），
    靠 :func:`degraded_marker_map` + 契约测试对齐。

    注意 ``Finding.code`` 用的是**本模块的码**（``body_preview_only``）
    而不是 marker——两者恰好同名，但 ``steps_missing`` ≠ marker ``steps``。
    混着装过一次：``_CODE_HELP`` 按本模块的码建表，于是 ``steps`` 那两组
    在归组渲染里**静默消失**了。码只在本模块内自洽，映射关系交给测试盯。
    """
    if not isinstance(obs, Mapping) or not obs:
        return
    if obs.get("body_text"):
        return                                    # 全文在，不缺
    legacy = obs.get("body_preview")
    if isinstance(legacy, str) and legacy:
        out.append(Finding(
            "body_preview_only", Severity.DEGRADED, name, where,
            "正文只有 400 字符预览——rationale 的实体核查会把预览外的实体判成幻觉",
        ))
        return
    out.append(Finding(
        "no_body", Severity.DEGRADED, name, where,
        "观察里没有正文，rationale 无从核查",
    ))


def _media_count(obs: Any) -> int:
    if not isinstance(obs, Mapping):
        return 0
    try:
        return int(obs.get(_MEDIA_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def _match_outcome(
    outcomes: Sequence[Mapping[str, Any]], *urls: str
) -> Mapping[str, Any] | None:
    """按 url 找 outcome，候选 url 与落地 url 都试。

    与 ``schema._outcome_for`` 同一条纪律且同一条实测依据：站点做 ``http→https``
    跳转时 ``visits[i].url`` 与 ``outcomes[j].url`` 不等（实测 4 份真实存档的
    每个 run 都有 3 个访问点对不上）。只查一个的后果是**静默**的：找不到 → ``None``，
    而 ``None`` 与「搜索阶段本来就没有 outcome」同形，于是每次都报
    ``outcome_missing``，报错多到没人看。
    """
    wanted = {u for u in urls if u}
    for o in outcomes:
        if str(o.get("url") or "") in wanted:
            return o
    return None


# ── 单份存档 ───────────────────────────────────────────────────────

def check_archive(
    path: Path,
    *,
    pool_keys: frozenset[tuple[str, str, str]] | None = None,
) -> ArchiveReport:
    """体检一份 P1 存档。

    Args:
        pool_keys: 负样本池的 ``(task_id, url, branch)`` 去重键集合。
            ``None`` 表示不查并池（单份体检、离线测试）。形状与
            ``P1Archive._load_negative_keys`` 一致。
    """
    name = path.name
    out: list[Finding] = []

    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ArchiveReport(name, (
            Finding("not_utf8", Severity.FATAL, name, "$",
                    f"原始字节不是合法 UTF-8（{exc.reason} @ byte {exc.start}）——"
                    "存档已损坏，只能重跑"),
        ))
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return ArchiveReport(name, (
            Finding("bad_json", Severity.FATAL, name, "$",
                    f"JSON 解析失败：{exc}"),
        ))

    # ── 证据链完整性：U+FFFD ─────────────────────────────────────
    # 按位置分两档，因为**波及面差一个数量级**：
    #   outcomes[*].evidence 坏 = 判据的依据没了，那条结论作废（FATAL）
    #   观察内容坏           = 那条观察降级，同一份存档的其余访问点照常可用
    # 合成一档会让「1 个站的正文烂了」把整批 20 个访问点一起判死，
    # 而门禁一旦老报 FATAL，人就会开始整体忽略它。
    for where, s in _iter_strings(doc):
        if REPLACEMENT_CHAR not in s:
            continue
        on_evidence = where.startswith("$.outcomes[") and where.endswith(".evidence")
        out.append(Finding(
            "evidence_mojibake" if on_evidence else "mojibake",
            Severity.FATAL if on_evidence else Severity.DEGRADED, name, where,
            f"含 {s.count(REPLACEMENT_CHAR)} 个替换字符 U+FFFD，原文已不可恢复"
            + ("（该结论的依据没了，只能重跑）" if on_evidence
               else "（该条降级：不可用于 rationale 实体核查）"),
        ))

    task_id = str(doc.get("task_id") or "")
    if not task_id:
        out.append(Finding("missing_task_id", Severity.FATAL, name, "$.task_id",
                           "没有 task_id，无法归组也无法回查任务定义"))

    outcomes = doc.get("outcomes") or []
    visits = doc.get("visits") or []

    # ── 降级：缺件 ──────────────────────────────────────────────
    if not (doc.get("steps") or []):
        out.append(Finding("steps_missing", Severity.DEGRADED, name, "$.steps",
                           "没有动作流，切出来的样本没有 actions（训练时模型学不到发什么动作）"))
    if not doc.get("user_prompt"):
        out.append(Finding("user_prompt_missing", Severity.DEGRADED, name,
                           "$.user_prompt",
                           "没有用户开口那句，六件套第③件拿不到用户轮次"))
    if not doc.get("run_config"):
        # 缺 run_config 的代价**不在采集，在评估**：批次级断言 B-2
        # （检索覆盖完整性）的分母取自 run_config.max_candidates，
        # 没有它，「候选上限 5 的 5/5」与「默认上限的 5/20」在存档里
        # 一模一样——覆盖率差 4 倍而读档的人无从分辨。
        # 同理 B-4（异常处置）的时限判据 per_site_timeout_s 也不在，
        # 而它连 --help 都查不到（CLI 不设它，走 RunConfig 默认值）。
        out.append(Finding("run_config_missing", Severity.DEGRADED, name,
                           "$.run_config",
                           "没有运行参数快照，覆盖完整性(B-2)的分母不可知、"
                           "异常处置(B-4)的时限判据缺失"))
    _obs_degraded(doc.get("search_observation"), "$.search_observation", out, name)

    # ── 站点级 ──────────────────────────────────────────────────
    for i, visit in enumerate(visits):
        wv = f"$.visits[{i}]"
        if not (visit.get("steps") or []):
            out.append(Finding("steps_missing", Severity.DEGRADED, name,
                               f"{wv}.steps", "该访问点没有动作流"))
        for key in ("site_obs", "player_obs"):
            _obs_degraded(visit.get(key), f"{wv}.{key}", out, name)

        outcome = _match_outcome(outcomes, str(visit.get("url") or ""),
                                 str(visit.get("landed_url") or ""))
        if outcome is None:
            out.append(Finding("outcome_missing", Severity.DEGRADED, name,
                               wv, "该访问点查不到 outcome —— 这条没有分支结论"))
            continue
        succeeded = bool(visit.get("success"))
        if succeeded != (outcome.get("branch") is None):
            # 两个来源对不上时**标出来而不是挑一个**——挑一个就是在猜，
            # 而这条样本进不进训练集全看它。
            out.append(Finding(
                "success_branch_mismatch", Severity.DEGRADED, name,
                f"{wv}.success", f"success={succeeded} 但 branch="
                f"{outcome.get('branch')!r}",
            ))

        # ── 成功必须能被观测支撑 ──────────────────────────────
        if outcome.get("branch") is None and succeeded:
            # 判据只在**这份存档自己记下的观察**里找，不另去重跑感知层：
            # 门禁要能在浏览器关着的时候跑，且重跑会引入新代码的结论——
            # 那就变成「用今天的代码审昨天的数据」，两份东西混在一起谁也说不清。
            media = _media_count(visit.get("player_obs")) or _media_count(
                visit.get("site_obs"))
            source = str(outcome.get("source") or "")
            if media == 0:
                # W1 规则版的判据**只有** ``<video>``/``<audio>``，iframe 明确
                # 不作判据（rule_perceptor.py 里 hao123 15 个 iframe 的教训）。
                # 所以「判成功 + 观察里 0 个媒体标签」在 rule 版下是自相矛盾，
                # 在 llm 版下只是值得看一眼——W3 可以有别的判据。
                sev = Severity.INFO if source == "llm" else Severity.FATAL
                out.append(Finding(
                    "success_without_media", sev, name,
                    f"{wv}.player_obs.{_MEDIA_KEY}",
                    f"判成功但观察里 {_MEDIA_KEY}=0"
                    + ("（llm 版可另有判据，人工确认即可）" if source == "llm"
                       else "——rule 版的唯一判据就是媒体标签，这条结论没有支撑"),
                ))

    # ── 逐条 outcome：结论必须带证据 ────────────────────────────
    # 独立于 visits 循环：**孤儿 outcome**（没有对应访问点）同样要查——
    # 它会进负样本池并出现在报表上，证据为空就是报表上凭空多出来的数字。
    for j, outcome in enumerate(outcomes):
        branch = outcome.get("branch")
        if branch is None:
            continue
        if not str(outcome.get("evidence") or "").strip():
            out.append(Finding(
                "evidence_empty", Severity.FATAL, name, f"$.outcomes[{j}].evidence",
                f"分支 {branch} 有结论但 evidence 为空——报表上这个数字背后什么都没有",
            ))

    # ── 并池 ────────────────────────────────────────────────────
    negatives = [
        (j, o) for j, o in enumerate(outcomes)
        if o.get("branch") is not None and o.get("branch") not in NON_SAMPLE_BRANCHES
    ]
    if pool_keys is not None:
        for j, o in negatives:
            key = (task_id, str(o.get("url") or ""), str(o.get("branch") or ""))
            if key not in pool_keys:
                out.append(Finding(
                    "negative_not_pooled", Severity.DEGRADED, name,
                    f"$.outcomes[{j}]",
                    f"真负样本没并池：{o.get('branch')} {str(o.get('url') or '')[:60]}",
                ))

    return ArchiveReport(
        name=name, findings=tuple(out), sites=len(visits),
        outcomes=len(outcomes), negatives=len(negatives),
    )


# ── 整个目录 ───────────────────────────────────────────────────────

def _pool_keys(root: Path) -> frozenset[tuple[str, str, str]]:
    """读负样本池的去重键。

    **逐行解析、坏行丢弃**：与 ``P1Archive._load_negative_keys`` 同一处置
    （池文件被写坏时末行可能是半截 JSON）。
    """
    path = root / "negative.jsonl"
    if not path.exists():
        return frozenset()
    keys: set[tuple[str, str, str]] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, Mapping):
            keys.add((str(row.get("task_id") or ""), str(row.get("url") or ""),
                      str(row.get("branch") or "")))
    return frozenset(keys)


def _load(path: Path) -> Any:
    """读一份存档。**坏档返回 ``None``，不抛异常。**

    跨存档检查要遍历全库，一份坏档不该让整次检查崩掉——它已经在
    :func:`check_archive` 里被单独报成 FATAL 了。
    """
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _flag_contradicted_negatives(root: Path) -> tuple[Finding, ...]:
    """**跨存档**检查：池里的负样本有没有被同一 URL 的成功记录推翻。

    这是整个门禁里唯一一条需要横向比对的检查，而它要抓的东西最贵。
    实测（2026-10-09）判断点 ① 接线后的第一批，一查就是 3 条：

    ```text
    negative.jsonl  not_play_site  v-wb.youku.com/…id_XNDE0NjYzODAzNg==
        理由：现有检索摘要未显示作品标题，无法确认对应《功夫》
    T011__d6badc6f  成功            同一条 URL
        理由：LLM 判 True：页面为《功夫》优酷视频播放页
    ```

    单看任一份存档都自洽：被否的那份有理由，被肯定的那份也有理由。
    **只有把同一个 URL 在两份存档里的归属摆到一起，矛盾才现形**——
    而这是训练数据里最毒的一种错：一条「这里看不了」正对着一条
    「这里能看」，模型学到的是噪声。

    按 URL 比对而**不按 (task_id, url, branch)**：同一个站在不同 task /
    不同 persona 下出现矛盾，正是要抓的东西，按 task 比会把它漏掉。

    判定为 FATAL 而非 INFO：矛盾意味着**池里有一条标签是错的**，
    拿去训练就是掺噪声。处置不是「看着办」，是移出来重判。

    **结果挂在报告级而非某一份存档上**：矛盾是「池 vs 全库」的性质，
    不属于任何一份存档。挂到存档上会让同一条矛盾在 N 份存档里各报一遍
    （实测就是 3 条 × 14 份 = 42 行噪声），而 N 会随批次数增长——
    **噪声比漏报更容易让人忽略真正的信号**。
    """
    pool_path = root / "negative.jsonl"
    if not pool_path.exists():
        return ()

    # 成功归属：url → [(存档, evidence)]
    winners: dict[str, list[tuple[str, str]]] = {}
    for path, _reviewed in select_archives(root):
        doc = _load(path)
        if not isinstance(doc, Mapping):
            continue
        for o in doc.get("outcomes") or []:
            if not isinstance(o, Mapping) or o.get("branch") is not None:
                continue
            url = str(o.get("url") or "").strip()
            if url:
                winners.setdefault(url, []).append(
                    (path.name, str(o.get("evidence") or ""))
                )
    if not winners:
        return ()

    out: list[Finding] = []
    for line in pool_path.read_text(encoding="utf-8",
                                    errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, Mapping):
            continue
        url = str(row.get("url") or "").strip()
        if url not in winners:
            continue
        wins = winners[url]
        out.append(Finding(
            "negative_contradicted_by_success",
            Severity.FATAL, "negative.jsonl", "$.negative_pool",
            f"池把 {url} 记成 {row.get('branch')}（{row.get('task_id')}："
            f"{row.get('evidence')}），但 {len(wins)} 份存档把同一条 URL "
            f"记成成功（{wins[0][0]}：{wins[0][1]}）"
            f"——标签自相矛盾，这条不能进训练数据",
        ))
    return tuple(out)


def check_root(root: Path | str | None = None) -> IntegrityReport:
    """体检一个目录下的全部存档（口径与报表一致，见模块 docstring）。"""
    from trajectory_pipeline.executor.archive import DEFAULT_ROOT

    base = Path(root or DEFAULT_ROOT)
    if not base.exists():
        return IntegrityReport((), (f"{base} 不存在",))

    pool_keys = _pool_keys(base)
    reports: list[ArchiveReport] = []
    for path, _reviewed in select_archives(base):
        reports.append(check_archive(path, pool_keys=pool_keys))

    conflicts = _flag_contradicted_negatives(base)

    notes: list[str] = []
    total_neg = sum(a.negatives for a in reports)
    if total_neg and not pool_keys:
        notes.append(
            f"有 {total_neg} 条真负样本，但 {base / 'negative.jsonl'} 不存在——"
            "并池从未执行过（'每条分支都必须有对应样本入库'是硬要求）"
        )
    elif total_neg != len(pool_keys):
        notes.append(
            f"存档里 {total_neg} 条真负样本，池内 {len(pool_keys)} 条"
            f"（差值 {total_neg - len(pool_keys):+d}；池是跨 run 累积的，"
            "数量不等不一定是错，缺的那几条已逐条列在上面）"
        )
    return IntegrityReport(tuple(reports), tuple(notes), conflicts)


# ── 渲染 ───────────────────────────────────────────────────────────

_CODE_HELP: Mapping[str, str] = {
    "not_utf8": "原始字节不是 UTF-8",
    "bad_json": "JSON 解析失败",
    "mojibake": "含替换字符 U+FFFD（观察内容，不可恢复）",
    "evidence_mojibake": "含替换字符 U+FFFD（结论依据，不可恢复）",
    "missing_task_id": "没有 task_id",
    "evidence_empty": "有结论没证据",
    "success_without_media": "判成功但观察里没有媒体标签",
    "negative_contradicted_by_success": "负样本被同一 URL 的成功记录推翻",
    "steps_missing": "缺动作流",
    "user_prompt_missing": "缺用户开口那句",
    "run_config_missing": "缺运行参数快照（覆盖完整性分母不可知）",
    "body_preview_only": "正文只有预览",
    "no_body": "没有正文",
    "outcome_missing": "访问点没有 outcome",
    "success_branch_mismatch": "success 与 branch 打架",
    "negative_not_pooled": "真负样本没并池",
}


def format_report(report: IntegrityReport) -> str:
    """给人看的体检报告。**按严重度排序，坏的排前面**——门禁的产出是
    「先看哪一条」，不是「一共几条」。"""
    lines: list[str] = []
    order = {Severity.FATAL: 0, Severity.DEGRADED: 1, Severity.INFO: 2}
    # 只排存档级的：池级单独成节（见下）。混在一起会把同一条池级矛盾
    # 在逐份列表里再列一遍——**报告里重复的东西必然被忽略**。
    own = sorted((f for a in report.archives for f in a.findings),
                 key=lambda f: (order[f.severity], f.archive, f.where))

    lines.append(f"存档 {len(report.archives)} 份，"
                 f"访问点 {sum(a.sites for a in report.archives)}，"
                 f"outcome {sum(a.outcomes for a in report.archives)}，"
                 f"真负样本 {sum(a.negatives for a in report.archives)}")

    if not own and not report.pool_findings:
        lines.append("\n✓ 全部通过")
        return "\n".join(lines)

    # FATAL / INFO 逐条列——它们数量少、每条都要单独处理。
    # DEGRADED **按 code 归组**：实测 4 份老存档能刷出 50+ 条降级项，
    # 其中绝大多数是同一句「这批跑在 steps[] 落地之前」。逐条列的结果是
    # 真问题（sohu 那段烂正文、7 条没并池）被埋在重复里没人看——
    # 门禁一旦天天报同一批已知项，人就会开始整体跳过它，那它存在的意义也没了。
    label = {
        "FATAL": "致命——这批存档不可信，只能重跑",
        "DEGRADED": "降级——样本缺件，能读但不能直接当训练数据",
        "INFO": "提示——不影响可用性，人工看一眼",
    }
    for sev in (Severity.FATAL, Severity.INFO):
        group = [f for f in own if f.severity is sev]
        if not group:
            continue
        lines.append(f"\n── {sev.value} · {label[sev.value]} ──")
        for f in group:
            lines.append(f"  {f.archive}  {f.where}")
            lines.append(f"      {f.code}：{f.detail}")

    # 池级单独成节：它不是任何一份存档的问题，混在逐份列表里会让人
    # 误以为是某一份存档坏了，而要动的是**池**。
    if report.pool_findings:
        lines.append("\n── 负样本池 · 池与全库自相矛盾（要动的是池，不是某一份存档）──")
        for f in report.pool_findings:
            lines.append(f"  {f.code}：{f.detail}")

    degraded = [f for f in own if f.severity is Severity.DEGRADED]
    if degraded:
        lines.append(f"\n── DEGRADED · {label['DEGRADED']} ──")
        for code in _CODE_HELP:
            same = [f for f in degraded if f.code == code]
            if not same:
                continue
            detail = same[0].detail
            lines.append(f"  {code} ×{len(same)} — {_CODE_HELP[code]}")
            lines.append(f"      {detail}")
            if len(same) > 1:
                files = sorted({f.archive for f in same})
                shown = "、".join(files[:3]) + ("…" if len(files) > 3 else "")
                lines.append(f"      涉及 {len(files)} 份：{shown}")

    for note in report.notes:
        lines.append(f"\n注意：{note}")
    return "\n".join(lines)


__all__ = [
    "Severity", "Finding", "ArchiveReport", "IntegrityReport",
    "check_archive", "check_root", "format_report", "degraded_marker_map",
]