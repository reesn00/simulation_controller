"""任务骨架——存量 ``tasks.yaml`` 的**只读**解析结果。

纪律
----
**只读参考，不 import。** 存量 ``tasks.yaml`` 通过 ``yaml.safe_load``
按文件读取，绝不 ``import configuration.*``。理由与新树整体纪律一致：
存量的 ``config`` 与 ``gdr/config`` 是顶层同名命名空间包，
一旦 import 就会出现双加载（详见方案 §0）。

骨架里**什么不可改**
--------------------
判分标准整体只读：``goal`` / ``output_contract`` / ``acceptance_criteria`` /
``excluded_platforms`` 原样保留。persona 只碰首轮**表述**，
改的是前缀背景句、后缀催促句、语气词与指代方式（见
:mod:`trajectory_pipeline.taskgen.persona.lexicon`）。

检索模式：为什么必须分叉
------------------------
存量 98 个 task **不是同一种任务**，把 98 个都套进「``{片名} 在线观看``」
这个模板会产出 17 条检索式彻底错误的轨迹。实测三类：

============  ====  =====================================  ===============
模式          数量  例子                                 检索目标
============  ====  =====================================  ===============
single_title    81  找到电视剧《武林外传》全集…             一个站的播放页
aggregate       12  周星驰执导的所有电影…按年份整理成目录     **一个集合**
unknown_title     5  找一部高分国产电影（不记得片名）        **先发现片名**
============  ====  =====================================  ===============

这三种要用**不同的检索式**，且后两种 W1 控制流**跑不了**：

- ``aggregate``：目标是多个源，而 W1 控制流的成功判据是
  「找到一个可播放的站点页」。拿单站判据去评聚合任务，
  会把「只找到一个」记成成功——**判分标准与任务不匹配，比跑不了更糟**。
- ``unknown_title``：需要「先找片、再找资源」两步，W1 是单步。

所以 :func:`load_skeletons` 会把模式标出来，调用方据此**显式排除**，
而不是让它们混进批次再产出假结论。这个字段不设默认值：
默认成 ``single_title`` 就等于把 17 个 task 静默变成错误的检索式。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Mapping

from trajectory_pipeline.taskgen.persona.lexicon import CONSTRAINT_PROBES

RetrievalMode = Literal["single_title", "aggregate", "unknown_title"]

#: 存量 task 库的位置。**刻意写成相对仓库根的路径**而不是 import 配置常量——
#: 新树不 import 存量，路径自己声明一份。找不到时由调用方显式处理。
DEFAULT_TASKS_YAML: Final = Path("simulate_serve/config/tasks.yaml")

#: 片名抽取。书名号是主形态（81 个里的绝大多数），
#: 直/弯引号是次形态（英文片名与部分国语片名用的是直角引号）。
#:
#: 长度上限**不足以**挡住误判——实测 ``我想找"电影"在线观看`` 里的「电影」
#: 长度为 2，远在上限内，但它是品类词不是片名。真正要挡的是这个，
#: 所以另配 :data:`_TITLE_STOPWORDS`：**长度挡不住的，靠词挡**。
_TITLE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"《([^》]{1,60})》"),
    re.compile(r"[\"“]([^\"”]{1,40})[\"”]"),
)

#: 通用品类/功能词。出现在直角引号里的**且整体由这些词构成**时不是片名。
#:
#: 误判代价不对称：把「"电影"」当成片名 → 任务被判成 ``single_title``
#: → 生成的检索式是「电影 在线观看」，整条链搜到的是**电影分类页**而不是某部片，
#: 而且它长得完全正常，不报错。
_TITLE_STOPWORDS: Final[tuple[str, ...]] = (
    "电影", "电视剧", "电视", "视频", "片子", "剧", "剧集", "综艺", "动画",
    "纪录片", "资源", "在线", "播放", "网址", "链接", "地址", "网站", "搜索",
    "在线播放", "在线观看", "高清", "免费", "全集",
)

#: **指代前缀**——引号里的串以这些词开头，说明用户是在**指**一部作品，
#: 而不是在**命名**它。存量实测只命中一条：T041
#: 「我想搜索"那个周星驰的片子"合法在线资源」。
#:
#: ⚠️ **只凭指代前缀不能判否**：《那个杀手不太冷》是真实存在的电影。
#: 所以判据必须是「指代前缀 **且** 含品类词」的**合取**——
#: 「那个周星驰的**片子**」两段都占，而「那个杀手不太冷」只占前一段。
#: 与 :data:`_TITLE_STOPWORDS` 的关系是**超集关系**：那个是「整体由品类词构成」，
#: 这个是「含指代前缀 + 含品类词」，后者覆盖前者。
_TITLE_REFERRING_PREFIXES: Final[tuple[str, ...]] = (
    "那个", "这个", "那部", "这部", "某部", "某片", "一部",
)
#: 品类词在串**任意位置**出现即算数（不要求整体等于）——
#: 「那个周星驰的片子」整体不等于任何 stopword，靠的就是这条。
_TITLE_NOUN_MARKERS: Final[tuple[str, ...]] = _TITLE_STOPWORDS


def _is_title_word(text: str) -> bool:
    """整体由品类/功能词构成的短串 → 不是片名。"""
    norm = text.strip().strip("!！?？。，,")
    if not norm:
        return True
    return any(norm == w or norm == w + "片" or norm == w + "剧"
               for w in _TITLE_STOPWORDS)


def _is_referring_phrase(text: str) -> bool:
    """引号里的是**指代短语**（指一部片却说不出名字）→ 不是片名。

    命中后果与 :func:`_is_title_word` 完全一致，且更严重：
    「那个周星驰的片子」被当成片名后，生成的检索式是
    **「那个周星驰的片子 在线观看」**——引擎里根本没有这个条目，
    搜出来的是一堆无关页面（或什么都没有）。而这条轨迹在分支分布里
    长得与「这站没有播放控件」一模一样，失败归因会指错方向。

    判为指代后 :func:`_detect_mode` 会把它落到 ``aggregate``（无片名、
    无单数信号），于是 W1 批次自然排除它——**这正是它该去的地方**：
    「周星驰的片子」是集合任务，用户自己也不知道要看哪一部。
    """
    norm = text.strip().strip("!！?？。，,")
    if not norm:
        return True
    if not any(norm.startswith(p) for p in _TITLE_REFERRING_PREFIXES):
        return False
    return any(marker in norm for marker in _TITLE_NOUN_MARKERS)

#: 集合信号——出现即「目标是多个源」。
#:
#: ⚠️ 这些词**只用于判模式，不参与 persona 渲染**：把「周星驰」抽成
#: 检索主体是另一种任务形态（按人名检索），不在 W1 范围内。
_AGGREGATE_MARKERS: Final[tuple[str, ...]] = (
    "所有", "全部", "历届", "合集", "系列", "榜单", "Top250", "TOP250",
    "目录", "分类", "策展", "整理成", "若干部", "多部", "部作品",
    "排名前", "片单",
)

#: 「单片但不知道片名」信号——句式是「一部/找一部」却没有任何片名形态。
_SINGLE_UNKNOWN_MARKERS: Final[tuple[str, ...]] = (
    "不记得", "想看一部", "找一部", "看一部",
)


class SkeletonError(ValueError):
    """骨架不可用（文件缺失 / 结构不符）。**显式抛**，不静默返回空。"""


@dataclass(frozen=True, slots=True)
class Constraint:
    """一条判分要求的**只读**摘要。

    ``key`` 对应 :data:`~trajectory_pipeline.taskgen.persona.lexicon.CONSTRAINT_PROBES`
    的键；``label`` 是人读描述。改写后由 :mod:`normalizer` 拿 ``key``
    去查探针词，确认这条要求没在改写里丢掉。
    """

    key: str
    label: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TaskSkeleton:
    """一个任务的骨架。

    ``criterion_texts`` 存判分标准的原文——**只读副本**，
    normalizer 比对的就是它们，改写**不允许**覆盖这个字段。
    """

    task_id: str
    scenario_id: str
    dimension: str
    task_type: str
    explain: str
    initial_request: str
    goal: str
    title: str | None
    retrieval_mode: RetrievalMode
    constraints: tuple[Constraint, ...]
    criterion_texts: tuple[str, ...]
    output_contract: Mapping[str, Any]
    excluded_platforms: tuple[str, ...]
    reference_notes: tuple[str, ...]
    #: 骨架侧的客观热度线索（存量没有该字段时为 None）。
    #: 用于 :meth:`PersonaProfile.content_tier_for` 的否决判断。
    skeleton_popularity: str | None = None

    @property
    def title_available(self) -> bool:
        return self.title is not None

    @property
    def runnable_in_w1(self) -> bool:
        """W1 控制流能否跑这个 task。

        ``aggregate`` / ``unknown_title`` 返回 False。理由见模块 docstring——
        不是"暂时没做"，是**判分标准与 W1 单站判据不匹配**。
        """
        return self.retrieval_mode == "single_title"


@dataclass(frozen=True, slots=True)
class TaskInstance:
    """一次采样的产出：骨架 + 画像 → 可执行的首轮任务。

    ``provenance`` 是评估切片轴的**唯一**来源，没有它样本就退化成
    「一堆文本」，无法回答"强口语 × 长尾这类组合是否退化"。
    """

    task_id: str
    persona_id: str
    scenario_id: str
    prompt_text: str
    search_query: str
    has_standard: bool
    retrieval_mode: RetrievalMode
    title: str | None
    provenance: Mapping[str, Any] = field(default_factory=dict)
    #: 表述是否经过了非平凡改写。False = 原样用骨架 initial_request。
    rewritten: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "persona_id": self.persona_id,
            "scenario_id": self.scenario_id,
            "prompt_text": self.prompt_text,
            "search_query": self.search_query,
            "has_standard": self.has_standard,
            "retrieval_mode": self.retrieval_mode,
            "title": self.title,
            "rewritten": self.rewritten,
            "provenance": dict(self.provenance),
        }


# ══════════════════════════════════════════════════════════════════════
# 解析
# ══════════════════════════════════════════════════════════════════════


def extract_title(text: str) -> str | None:
    """从任务表述里抽片名。抽不到返回 ``None``——**不猜**。

    抽不到不是失败：存量里 17/98 个 task 本来就没有具体片名
    （按导演/榜单/场景检索），那些走 :data:`RetrievalMode` 的另两档。

    两种「不是片名」都要挡，且**理由不同**：

    - :func:`_is_title_word` —— 整体由品类词构成（「"电影"」）
    - :func:`_is_referring_phrase` —— 指代短语（「"那个周星驰的片子"」）
    """
    for pattern in _TITLE_PATTERNS:
        m = pattern.search(text)
        if m:
            title = m.group(1).strip()
            if title and not _is_title_word(title) \
                    and not _is_referring_phrase(title):
                return title
    return None


def _detect_mode(title: str | None, text: str) -> RetrievalMode:
    """判定检索模式。**纯字符串事实**，不含语义猜测。

    判定顺序即特异度从高到低：片名 > 单数信号 > 集合信号 > 默认。
    """
    if title:
        return "single_title"
    if any(marker in text for marker in _SINGLE_UNKNOWN_MARKERS):
        return "unknown_title"
    # 默认落到 aggregate 而**不是** unknown_title：没有片名、
    # 也没有"找一部"这种单数信号，任务的目标就是「符合某条件的若干源」。
    # 早期版本默认 unknown_title，把「梁朝伟参演的文艺片」这类
    # 明显的集合任务判成了单片——单片判据会直接漏掉它们该找的多个源。
    return "aggregate"


def _derive_constraints(task: Mapping[str, Any], text: str) -> tuple[Constraint, ...]:
    """从 ``output_contract`` 与**首轮表述原文**推导判分要求清单。

    ⚠️ 只看首轮表述，不看 ``criterion_texts``。这不是偷懒，是判别标准：
    persona 改写的是**首轮消息**，不是判分标准；往首轮里塞判分标准里
    才有、用户本来没提的要求，等于替用户加需求。
    判分标准由"骨架原文整段保留"这条结构性纪律保护。

    ⚠️ 检测词**必须**取自 :data:`CONSTRAINT_PROBES`，不得另写一套。
    早期版本在这里硬编码 ``("集数", "季数", …)`` 而探针那边多一个"全集"，
    于是 T001 的「全集」推不出 ``has_episode_range``（带空格/省略的写法
    处处漏），约束检查对最典型的那条要求形同虚设。
    **两套词不同源 = 推导与检查互相打架，且没有任何报错。**
    现在检测与检查共用同一份词表，这类不一致从结构上不再可能发生。

    只映射到有探针的那几档；其余判分维度（如 ``excluded_platforms``）
    不进探针——给没探针的要求硬造一个探针，只会制造恒过的假检查。

    ⚠️ **全部从表述文本反查，不从 ``output_contract`` 推导。**
    早期版本对 ``min_results`` / ``min_urls`` 做了特判推导，结果��
    ``has_count`` 被推出来、而用户表述里**从来没有"要 5 个"这个要求**
    （存量首轮写的是「找到…的可播放网址」，单数；条数只出现在判分标准里）。
    于是 normalizer 每条都判它丢失，实测接受率 4%——而根因不在渲染器，
    在这条推导。它甚至让 12 条采样里 9 条退回原文，persona 特征全部失效，
    表象却只是"归一通过率偏低"，极难定位。

    判断标准是**用户说没说过**，不是判分标准要求什么：
    没说的话，改写没有义务保留；判分标准里的条数约束由骨架原文
    与 :class:`TaskSkeleton.criterion_texts` 原样承载，不走探针。
    """
    out: list[Constraint] = []
    contract = task.get("output_contract") or {}
    lowered = text.lower()

    for key, (label, probes) in CONSTRAINT_PROBES.items():
        hit = next((p for p in probes if p.lower() in lowered), None)
        if hit is None:
            continue
        detail = hit
        if key == "has_url":
            detail = f"{hit}（min_urls={contract.get('min_urls')}）"
        elif key == "has_count":
            detail = f"{hit}（min_results={contract.get('min_results')}）"
        out.append(Constraint(key, label, detail))
    return tuple(out)


def _criterion_texts(task: Mapping[str, Any]) -> tuple[str, ...]:
    """判分标准原文。只读副本。"""
    out: list[str] = []
    for crit in task.get("acceptance_criteria") or []:
        if isinstance(crit, dict):
            desc = str(crit.get("description") or crit.get("item") or "").strip()
            if desc:
                out.append(desc)
        elif isinstance(crit, str) and crit.strip():
            out.append(crit.strip())
    for prio in ((task.get("intent") or {}).get("priorities") or []):
        if isinstance(prio, dict):
            req = str(prio.get("requirement") or "").strip()
            if req:
                out.append(req)
    return tuple(dict.fromkeys(out))       # 去重且保序


def parse_skeleton(task: Mapping[str, Any]) -> TaskSkeleton:
    """单条 task 记录 → :class:`TaskSkeleton`。

    ``task_id`` 缺失直接抛——没有 id 的骨架无法落盘，也无法回溯到存量条目，
    与其生成一条匿名轨迹不如让它在采样前就炸。
    """
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise SkeletonError(f"task 记录缺 task_id: {sorted(task)[:8]}")

    request = str(task.get("initial_request") or "").strip()
    intent = task.get("intent") or {}
    goal = str(intent.get("goal") or request).strip()
    if not request:
        raise SkeletonError(f"task {task_id} 缺 initial_request")

    title = extract_title(request)
    reference = task.get("reference") or {}
    notes = reference.get("evaluation_notes") if isinstance(reference, Mapping) else None

    return TaskSkeleton(
        task_id=task_id,
        scenario_id=str(task.get("scenario") or "").strip(),
        dimension=str(task.get("dimension") or "").strip(),
        task_type=str(task.get("task_type") or "").strip(),
        explain=str(task.get("explain") or "").strip(),
        initial_request=request,
        goal=goal,
        title=title,
        retrieval_mode=_detect_mode(title, request),
        constraints=_derive_constraints(task, request),
        criterion_texts=_criterion_texts(task),
        output_contract=dict(task.get("output_contract") or {}),
        excluded_platforms=tuple(
            str(p) for p in (task.get("excluded_platforms") or []) if str(p).strip()
        ),
        reference_notes=tuple(str(n) for n in (notes or []) if str(n).strip()),
    )


def load_skeletons(path: Path | str = DEFAULT_TASKS_YAML) -> tuple[TaskSkeleton, ...]:
    """读存量 ``tasks.yaml`` → 骨架序列。

    **只读文件，不 import 存量包。** 文件缺失或 schema 不符时显式抛
    :class:`SkeletonError`——静默返回空列表会让整批采样产出零样本，
    而零样本的表现是"跑完了，没有数据"，不报错。
    """
    import yaml   # 延迟导入：本模块的其余部分（类型与解析）不需要它

    p = Path(path)
    if not p.exists():
        raise SkeletonError(
            f"找不到存量任务库 {p}；新树不 import 存量，只能按路径读——"
            f"路径不对时显式报错，不猜默认位置"
        )
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SkeletonError(f"读 {p} 失败: {type(exc).__name__}: {exc}") from exc

    tasks = (data or {}).get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise SkeletonError(f"{p} 里没有 tasks 列表（schema_version="
                            f"{(data or {}).get('schema_version')!r}）")
    skeletons = [parse_skeleton(t) for t in tasks if isinstance(t, Mapping)]
    if not skeletons:
        raise SkeletonError(f"{p} 的 tasks 里没有可解析的映射")
    return tuple(skeletons)


def mode_distribution(skeletons: tuple[TaskSkeleton, ...]) -> dict[str, int]:
    """三种模式的分布。**采集前的必看项**。

    分布不打印出来的话，``aggregate`` 那些 task 会被混进批次，
    然后产出一批"检索式彻底跑偏"的轨迹，而它们的失败原因
    在分支分布里长得跟"这站没有播放控件"一模一样。
    """
    counts: dict[str, int] = {}
    for sk in skeletons:
        counts[sk.retrieval_mode] = counts.get(sk.retrieval_mode, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))