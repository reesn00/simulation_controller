"""双跑对账（P5 的验收判据，设计方案 04 §6.1）——同一批任务在两个执行层上
各跑一遍，**逐条比**。

为什么要有这个东西
------------------
「camoufox 换上去对数据分布有没有影响」不能靠体感回答：两边产出的存档
**长得一模一样**（同一份 P1 结构），差异全在数值里——分支比例、正文长度、
元素条数。肉眼看两份 JSON 是看不出 54% 的落差的；而落差的后果是
**并池后的分布变了一个没人察觉的原因**。

所以判据写死在代码里：§6.1 的四条可机械判，两条必须人判（负样本成因、
动作可移植性）。**本模块只做前者，后者原样列给人看**——不投票、不合成
一个「总分」：分歧要暴露给人看，不是被一个加权数抹平。

口径
----
- ``--a`` 是**基线**（当前生产链 obscura），``--b`` 是**候选**（camoufox）。
  中位数差按 ``|a-b| / a`` 算——「相对基线变了多少」才是要回答的问题。
- 两侧必须是**同一批 task_id**。没配上的条目**逐条列出来**，不按并集悄悄比：
  少跑的那一边会把分母缩小，比例差看着反而更小。
- 两侧的 ``driver`` 字段不同是**预期的**（这正是双跑的意义），但它决定
  两批数据**不是同一口径**（P4：元素键名与词表都随面变）——所以本报告
  是「换层前后的分布差」，**不是**「两组同口径样本的质量对比」。

元素条数怎么判（2026-10-21，§4.9.6）
-------------------------------------
§6.1 判据③「interactive_elements 条数中位数」在换层场景下**原来不可判**：
两面数的是**不同的集合**（obscura = 上游工具给的交互元素；camoufox = ARIA 树里
过了词表的那些），拿它们直接比得出的「−70%」混着「枚举基础不同」与
「过滤规则不同」两件事，**不是一个可解释的量**。

现在分两条：

- **面内口径**：跨面时降为 ``incomplete``（不是 fail，也不是 pass）——
  不可解释的数不能拿来判过也不能拿来判不过。同面时才照旧判。
- **共享口径**：**同一段 JS + 同一把尺子**在两个浏览器上各跑一次，
  于是按构造可比——这条才承载 pass/fail。没有开 ``--element-basis``
  就没有这个数 ⇒ ``incomplete``（**判不了 ≠ 一样**）。

报告另给**归因表**：两本账逐档对相，**差值落在哪一档一眼可见**
（枚举基础？role 判定？无名/同名？）。
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from trajectory_pipeline.executor.archive import select_archives
from trajectory_pipeline.executor.browser.element_basis import (
    LADDER_STAGES,
    SHARED_BASE_STAGES,
)
from trajectory_pipeline.toolcatalog import surface_of_driver

#: 分支比例允许的**绝对**差（百分点）。§6.1
GATE_BRANCH_PP = 5.0
#: 正文长度 / 元素条数的中位数允许的**相对**差（相对 A 侧基线）。§6.1
GATE_MEDIAN_RATIO = 0.30

#: 与反爬/登录态有关的分支——拦截率由这几支 + ``search_blocked`` 组成。
#: ``login_wall_or_blocked`` 是 deprecated 的合并态，历史存档里还有，照算。
BLOCK_BRANCHES: frozenset[str] = frozenset(
    {"blocked", "login_required", "login_wall_or_blocked"}
)

#: **真·反爬拦截**——页面被验证码 / 地区限制 / 直接封掉。
#:
#: ⚠️ **它和「登录墙」不是一回事**（2026-10-21 双跑实测逼出来的）：
#: 合并口径的「拦截率」把两者算在一起，于是读出来的是「B 面更容易被拦」。
#: 拆开看，真·反爬上是 **A 14.89% vs B 9.09%**——B（camoufox 无头）
#: 反而**更少**被拦。那个反向的差全在登录墙桶里，而登录墙**不是反爬**：
#: 页面正常返回了，只是要登录。
#:
#: 另有一层：``login_wall_or_blocked`` 是**兜底分支**——模型答 False
#: 却没给 ``access_control_type`` 时才落它（见
#: ``llm_perceptor._is_reachable``）。它连「是哪类阻断」都没说清。
#:
#: ⚠️ **2026-10-21 起兜底不再落它**（台账 H-15，owner 裁定）——
#: ② 无 ``access_control_type`` 时改落 ``unresolved``，而 ``unresolved``
#: 既不在反爬桶也不在登录墙桶里。**本常量保留它是为了读历史存档**：
#: 清理前那 10 条还在池里，桶口径不能因此漂（漂了 ⇒ 双跑读数不可比）。
#: 新数据不会再落这一支。
ANTI_BOT_BRANCHES: frozenset[str] = frozenset({"blocked"})

#: **登录墙**——要登录/要会员才能看。**不是反爬拦截**：页面是正常返回的。
ACCESS_WALL_BRANCHES: frozenset[str] = frozenset(
    {"login_required", "login_wall_or_blocked"}
)


# ── 读存档 ──────────────────────────────────────────────────────


def load_runs(path: Path) -> list[dict[str, Any]]:
    """读一个目录（或单个文件）里的 P1 存档。**只认存档形状**。

    目录走 :func:`~trajectory_pipeline.executor.archive.select_archives`——
    那里已经把「探针取证不是存档」「复核档取代原档」两条规则写死了，
    这里再抄一份就会漂（实测目录 9 个 json 只有 4 个是存档）。
    """
    root = Path(path)
    if root.is_file():
        return [json.loads(root.read_text(encoding="utf-8"))]
    out: list[dict[str, Any]] = []
    for archive_path, _reviewed in select_archives(root):
        out.append(json.loads(archive_path.read_text(encoding="utf-8")))
    return out


#: 观察槽 → **族**。同族的页面形态相近（搜索页 / 站内步骤 / 站点页 / 播放页），
#: 混在一起取中位数会互相稀释——判据②③ 的读数都是**混合中位数**，
#: 所以要按族分列才看得出真实差异（见 :func:`_observation_slots`）。
SLOT_FAMILIES: tuple[str, ...] = ("search", "step", "visit_step", "site_obs", "player_obs")


def _slot_family(slot: str) -> str:
    """槽位名 → 族名。未知槽位归 ``step``——宁可并进去，也别丢数据。"""
    if slot in SLOT_FAMILIES:
        return slot
    if slot.startswith("visit"):
        if slot.endswith(".site_obs"):
            return "site_obs"
        if slot.endswith(".player_obs"):
            return "player_obs"
        return "visit_step"
    return "step"


def _observation_slots(record: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """一份存档里的**全部观察**，每个都带**族标签**。

    顶层 ``steps`` 是搜索阶段、``visits[].steps`` 是站内阶段——两者**不重叠**
    （实测：顶层 1 条 + 每个 visit 各自 1 条，URL 不同）。

    **为什么按族分列**：实测（2026-10-21 第二轮双跑）发现判据②③ 的
    中位数读数是**混合中位数**——搜索页、站内步骤、站点页、播放页的
    元素规模差一个量级，混在一起取中位数，结论会随各族的**占比**漂。
    分列之后同族相比才是同物相比。
    """
    out: list[tuple[str, Mapping[str, Any]]] = []
    obs = record.get("search_observation")
    if isinstance(obs, Mapping):
        out.append(("search", obs))
    for i, step in enumerate(record.get("steps") or ()):
        o = (step or {}).get("observation")
        if isinstance(o, Mapping):
            out.append((f"step{i}", o))
    for vi, visit in enumerate(record.get("visits") or ()):
        visit = visit or {}
        for key in ("site_obs", "player_obs"):
            o = visit.get(key)
            if isinstance(o, Mapping):
                out.append((f"visit{vi}.{key}", o))
        for si, step in enumerate(visit.get("steps") or ()):
            o = (step or {}).get("observation")
            if isinstance(o, Mapping):
                out.append((f"visit{vi}.step{si}", o))
    return out


def _observations(record: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """一份存档里的**全部观察**（搜索页 + 顶层步骤 + 站点页 + 播放页）。

    顶层 ``steps`` 是搜索阶段、``visits[].steps`` 是站内阶段——两者**不重叠**
    （实测：顶层 1 条 + 每个 visit 各自 1 条，URL 不同）。
    """
    for _slot, obs in _observation_slots(record):
        yield obs


def _body_len(obs: Mapping[str, Any]) -> int:
    """正文长度。``body_len`` 优先——它是落盘时算的，与 ``body_text`` 同源；
    老存档缺这个键才退回现数长度（**不假设两代一致**）。"""
    if isinstance(obs.get("body_len"), int):
        return int(obs["body_len"])
    return len(str(obs.get("body_text") or ""))


def _elements(obs: Mapping[str, Any]) -> int:
    """交互元素条数。``elements_total`` 优先：那是**采到的**条数，
    ``interactive_elements`` 可能是被裁过的落盘条数（P1 契约里写明的那条）。"""
    if isinstance(obs.get("elements_total"), int):
        return int(obs["elements_total"])
    return len(obs.get("interactive_elements") or ())


# ── 配对比较（2026-10-21）────────────────────────────────────────
#
# **为什么要有配对**：判据 ①（分支分布）比的是**两批样本的占比**，
# 而两批样本**看的不是同一批页面**——实测两处混淆：
#
# 1. **候选集不同**：两个指纹下百度返回的结果本身就不同（实测同一 task
#    两面各 13 站，落地 URL 只重叠 5 个）。
# 2. **观测能力不同**：obscura 在爱奇艺这类客户端渲染的 SPA 上抽不到正文
#    （实测 ``body_len=0`` + ``degraded=['body_text']``）⇒ 它把这些页面
#    记成 ``unresolved``，而 camoufox 能读出正文、给出具体分支。
#
# 于是占比差里混着「站点质量差」（要测的）与「观测能力差」（污染）。
# **配对**把同一个页面在两面的判定摊开，人才分得清这两种差。
# ——「禁止投票/集成，分歧必须暴露给人看」。


def _page_key(url: str) -> str:
    """落地 URL → 配对键（跨浏览器可比的「同一个页面」）。

    **只留 host + path**：百度跳转链接的 token 每次都变
    （``/link?url=XXXX``），查询串也每次都变（爱奇艺的 ``fr=``、
    1905 的 ``__hz=``）——留着就配不上对。``www.`` 与尾斜杠一并归一。
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    return f"{host}{path}"


def _page_rows(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """一份存档 → 页面行列表（配对的原料）。

    一行 = **一次落地的判定**：分支 + 证据 + 采集读数（降级、正文长度）。
    行从 ``outcomes[]`` 来（它带最终 URL 与分支）；``visits[]`` 的观察
    读数按落地 URL 挂上来——**挂不上就不挂**（缺键 ≠ 0）。
    """
    visit_reads: dict[str, Mapping[str, Any]] = {}
    for visit in record.get("visits") or ():
        visit = visit or {}
        obs = visit.get("site_obs")
        key = _page_key(str(visit.get("landed_url") or ""))
        if key and isinstance(obs, Mapping):
            visit_reads.setdefault(key, obs)

    rows: list[dict[str, Any]] = []
    for outcome in record.get("outcomes") or ():
        if not isinstance(outcome, Mapping):
            continue
        url = str(outcome.get("url") or outcome.get("site_url") or "")
        key = _page_key(url)
        if not key:
            continue
        obs = visit_reads.get(key, {})
        rows.append({
            "key": key,
            "url": url,
            "branch": str(outcome.get("branch") or ""),
            "branch_label": str(outcome.get("branch_label") or ""),
            "evidence": str(outcome.get("evidence") or "")[:300],
            "degraded": list(obs.get("degraded") or ()) if obs else [],
            "body_len": obs.get("body_len") if obs else None,
        })
    return rows


def pair_pages(
    a_pages: Mapping[str, list[Mapping[str, Any]]],
    b_pages: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, Any]:
    """按落地 URL 把两面的页面行配对，再比分支。

    返回**读数**，不是判据：配对数、单边数、分支分歧矩阵、分歧逐条。
    **不合成一个数**——配对数本身就说不了「谁更好」。
    """
    per_task: dict[str, Any] = {}
    totals = {"paired": 0, "only_a": 0, "only_b": 0, "disagreements": 0,
              "a_blocked": 0, "b_blocked": 0, "a_degraded": 0, "b_degraded": 0,
              "a_antibot": 0, "b_antibot": 0, "a_loginwall": 0, "b_loginwall": 0}
    matrix: dict[str, int] = {}

    for task in sorted(set(a_pages) | set(b_pages)):
        a_by_key = {str(r["key"]): r for r in (a_pages.get(task) or [])}
        b_by_key = {str(r["key"]): r for r in (b_pages.get(task) or [])}
        shared = sorted(set(a_by_key) & set(b_by_key))
        only_a = sorted(set(a_by_key) - set(b_by_key))
        only_b = sorted(set(b_by_key) - set(a_by_key))

        disagreements: list[dict[str, Any]] = []
        a_blocked = b_blocked = a_degraded = b_degraded = 0
        a_antibot = b_antibot = a_loginwall = b_loginwall = 0
        for key in shared:
            ra, rb = a_by_key[key], b_by_key[key]
            ba = str(ra.get("branch") or "")
            bb = str(rb.get("branch") or "")
            cell = f"{ba or '(成功)'} | {bb or '(成功)'}"
            matrix[cell] = matrix.get(cell, 0) + 1
            if ba in BLOCK_BRANCHES:
                a_blocked += 1
            if bb in BLOCK_BRANCHES:
                b_blocked += 1
            if ba in ANTI_BOT_BRANCHES:
                a_antibot += 1
            if bb in ANTI_BOT_BRANCHES:
                b_antibot += 1
            if ba in ACCESS_WALL_BRANCHES:
                a_loginwall += 1
            if bb in ACCESS_WALL_BRANCHES:
                b_loginwall += 1
            if ra.get("degraded"):
                a_degraded += 1
            if rb.get("degraded"):
                b_degraded += 1
            if ba != bb:
                disagreements.append({
                    "url": ra.get("url") or rb.get("url") or key,
                    "a_branch": ba, "b_branch": bb,
                    "a_degraded": list(ra.get("degraded") or ()),
                    "b_degraded": list(rb.get("degraded") or ()),
                    "a_body_len": ra.get("body_len"),
                    "b_body_len": rb.get("body_len"),
                })

        per_task[task] = {
            "paired": len(shared),
            "only_a": only_a,
            "only_b": only_b,
            "a_blocked": a_blocked, "b_blocked": b_blocked,
            "a_antibot": a_antibot, "b_antibot": b_antibot,
            "a_loginwall": a_loginwall, "b_loginwall": b_loginwall,
            "a_degraded": a_degraded, "b_degraded": b_degraded,
            "disagreements": disagreements,
        }
        totals["paired"] += len(shared)
        totals["only_a"] += len(only_a)
        totals["only_b"] += len(only_b)
        totals["disagreements"] += len(disagreements)
        totals["a_blocked"] += a_blocked
        totals["b_blocked"] += b_blocked
        totals["a_antibot"] += a_antibot
        totals["b_antibot"] += b_antibot
        totals["a_loginwall"] += a_loginwall
        totals["b_loginwall"] += b_loginwall
        totals["a_degraded"] += a_degraded
        totals["b_degraded"] += b_degraded

    paired_n = totals["paired"]

    def _rate(key: str) -> float | None:
        return (totals[key] / paired_n * 100.0) if paired_n else None

    return {
        "per_task": per_task,
        "totals": totals,
        "matrix": dict(sorted(matrix.items())),
        "a_block_rate_pct": _rate("a_blocked"),
        "b_block_rate_pct": _rate("b_blocked"),
        "a_antibot_rate_pct": _rate("a_antibot"),
        "b_antibot_rate_pct": _rate("b_antibot"),
        "a_loginwall_rate_pct": _rate("a_loginwall"),
        "b_loginwall_rate_pct": _rate("b_loginwall"),
        "note": ("配对子集上的读数——**同一批页面**两面的判定。"
                 "占比口径的读数混着「站点质量差」与「观测能力差」，"
                 "这里只留后者能被剥离的那部分。"
                 "**配对数小就是结论弱**，不是结论好。"
                 "**真·反爬**与**登录墙**分开报——合并口径会给出相反的结论。"),
    }


# ── 元素账目（跨面可比口径，设计方案 04 §4.9.6）──────────────────


def _basis_buckets(
    obs: Mapping[str, Any],
) -> tuple[dict[str, list[int]], dict[str, list[int]], bool, bool]:
    """一份观察里的两本元素账 → ``(shared 桶, face 桶, 有账?, 有 shared?)``。

    **桶是「每个观察一条」的列表**，最后取中位数——不是把各观察的数加起来：
    加起来得到的是「这一批一共有多少个元素」，与「一个页面有多少个元素」
    是两件事，而对账要回答的是后者。

    形状不对就当**没有账**（桶为空 ⇒ 中位数缺席 ⇒ 判据 ``incomplete``）。
    半截的账比没有账更坏——读的人会拿它当「测过」的数去比。
    """
    shared: dict[str, list[int]] = {}
    face: dict[str, list[int]] = {}
    raw = obs.get("element_basis")
    if not isinstance(raw, Mapping):
        return shared, face, False, False
    for target, key in ((shared, "shared"), (face, "face")):
        block = raw.get(key)
        if not isinstance(block, Mapping):
            continue
        for stage, value in block.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            target.setdefault(str(stage), []).append(int(value))
    return shared, face, True, bool(shared)


def _stage_medians(buckets: Mapping[str, list[int]]) -> dict[str, float]:
    """各档位的中位数。**缺档就缺着**——不填 0（填 0 会被读成「一条都没被过滤」）。"""
    return {
        stage: float(statistics.median(values))
        for stage, values in buckets.items()
        if values
    }


# ── 单侧汇总 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SideSummary:
    """一侧（一个执行层 / 一个目录）的分布摘要。"""

    label: str
    source: str
    runs: int
    task_ids: tuple[str, ...]
    drivers: Mapping[str, int]
    surfaces: Mapping[str, int]
    observations: int
    branch_counts: Mapping[str, int]
    body_len_median: float | None
    elements_median: float | None
    blocked_outcomes: int
    search_blocked_runs: int
    negatives: tuple[Mapping[str, Any], ...]
    #: **跨面可比口径**的元素账（设计方案 04 §4.9.6）。
    #:
    #: - ``basis_observations``：带 ``element_basis`` 的观察数。
    #: - ``shared_observations``：带 ``shared`` 子账的观察数——**只有它
    #:   才支撑可比判据**。
    #: - ``shared_ledger`` / ``face_ledger``：各档位的**中位数**。
    basis_observations: int = 0
    shared_observations: int = 0
    shared_ledger: Mapping[str, float] = field(default_factory=dict)
    face_ledger: Mapping[str, float] = field(default_factory=dict)
    #: **真·反爬**（``blocked``）与**登录墙**（``login_required`` +
    #: ``login_wall_or_blocked``）分开计——合并口径会把「要登录」
    #: 算成「被反爬拦」（见 :data:`ANTI_BOT_BRANCHES`）。
    #:
    #: ⚠️ ``login_wall_or_blocked`` **只对历史存档有效**——兜底已改落
    #: ``unresolved``（台账 H-15，2026-10-21），新数据不再落这一支。
    antibot_outcomes: int = 0
    loginwall_outcomes: int = 0
    #: 每个 task 的**页面行**（落地 URL 归一化后的键 + 分支 + 采集读数）——
    #: 配对比较的原料，见 :func:`pair_pages`。
    pages: Mapping[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    #: **按观察槽族**分列的中位数——``body_len_median_by_family`` 是正文长度，
    #: ``shared_ledger_by_family`` 是共享账各档。
    #:
    #: ⚠️ **混合中位数不可当结论**：搜索页 / 站内步骤 / 站点页 / 播放页的
    #: 元素规模差一个量级，混在一起取中位数，结论会随各族的**占比**漂
    #: （实测：整体 ``kept`` 是 B 更好，但**播放页族是 A 更好**）。
    #: 分列之后同族相比才是同物相比。
    body_len_median_by_family: Mapping[str, float] = field(default_factory=dict)
    shared_ledger_by_family: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    #: 各族的**观察条数**——分列读数的分母，也是「混合中位数」的成分表。
    observations_by_family: Mapping[str, int] = field(default_factory=dict)
    shared_observations_by_family: Mapping[str, int] = field(default_factory=dict)

    @property
    def outcomes_total(self) -> int:
        return sum(self.branch_counts.values())

    @property
    def branch_shares(self) -> dict[str, float]:
        """分支占比（%）。分母是**出过分支的站点数**，不是 run 数。"""
        total = self.outcomes_total
        if not total:
            return {}
        return {b: c / total * 100.0 for b, c in self.branch_counts.items()}

    @property
    def block_rate(self) -> float | None:
        """反爬拦截率（%）：被拦站点 / 全部站点。

        ⚠️ 这只是**一个读数**，不是「采集体感」：真·拦截率要人看被拦的是
        哪一类站点。§6.1 明确它**不构成否决**（除非显著变高）。

        ⚠️ **它是合并口径**（反爬 + 登录墙）。要判「谁更容易被反爬拦」
        看 :attr:`antibot_rate`——实测两者会给出**相反**的结论。
        """
        if not self.outcomes_total:
            return None
        return self.blocked_outcomes / self.outcomes_total * 100.0

    @property
    def antibot_rate(self) -> float | None:
        """**真·反爬拦截率**（%）：被验证码/地区限制/封掉的站点 / 全部站点。

        与 :attr:`block_rate` 的差别就是把登录墙摘出去——**登录墙不是
        反爬**：页面正常返回了，只是要登录。
        """
        if not self.outcomes_total:
            return None
        return self.antibot_outcomes / self.outcomes_total * 100.0

    @property
    def loginwall_rate(self) -> float | None:
        """**登录墙占比**（%）：要登录/要会员才能看的站点 / 全部站点。"""
        if not self.outcomes_total:
            return None
        return self.loginwall_outcomes / self.outcomes_total * 100.0

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "source": self.source,
            "runs": self.runs,
            "task_ids": list(self.task_ids),
            "drivers": dict(self.drivers),
            "surfaces": dict(self.surfaces),
            "observations": self.observations,
            "branch_counts": dict(self.branch_counts),
            "branch_shares": {k: round(v, 2) for k, v in self.branch_shares.items()},
            "outcomes_total": self.outcomes_total,
            "body_len_median": self.body_len_median,
            "elements_median": self.elements_median,
            "block_rate_pct": None if self.block_rate is None else round(self.block_rate, 2),
            "antibot_rate_pct": None if self.antibot_rate is None else round(self.antibot_rate, 2),
            "loginwall_rate_pct": None if self.loginwall_rate is None else round(self.loginwall_rate, 2),
            "antibot_outcomes": self.antibot_outcomes,
            "loginwall_outcomes": self.loginwall_outcomes,
            "search_blocked_runs": self.search_blocked_runs,
            "negatives": [dict(n) for n in self.negatives],
            "basis_observations": self.basis_observations,
            "shared_observations": self.shared_observations,
            "shared_ledger": dict(self.shared_ledger),
            "face_ledger": dict(self.face_ledger),
            "body_len_median_by_family": dict(self.body_len_median_by_family),
            "shared_ledger_by_family": {
                fam: dict(ledger)
                for fam, ledger in self.shared_ledger_by_family.items()
            },
            "observations_by_family": dict(self.observations_by_family),
            "shared_observations_by_family": dict(self.shared_observations_by_family),
        }


def _surface_of(driver: str) -> str:
    """驱动名 → 面。认不出的**不猜**：记 ``unknown`` 并在报告里点名。

    这里与 ``surface_of_driver`` 的「抛」口径不同，因为对账工具是**只读**
    的事后审计：一份认不出的 driver 不该让整份报告出不来，而该让它显形。
    """
    try:
        return surface_of_driver(driver)
    except ValueError:
        return "unknown"


def summarize(
    label: str, path: Path, *, surface: str | None = None
) -> SideSummary:
    """把一侧的存档汇总成 :class:`SideSummary`。

    ``surface`` 只统计**那个面**的存档（``obscura-1`` / ``camoufox-1``）。
    ⚠️ **盘上现在两面都有**（obscura 是存量面、camoufox 是生产面），
    不筛就会把**两代读数混成一个数**——而两代的驱动行为不同
    （camoufox 等新文档 commit、obscura 不等），混起来的数**既不是
    旧批次的数，也不是新批次的数**，是最坏的一种数。
    ⇒ 筛的键取**面**而不是 ``driver`` 字段：存量 obscura 批次**没记
    ``driver``**（该字段是 2026-10-22 才进的），而它们真实跑的就是
    obscura ⇒ 按 ``driver==""`` 筛会把存量批次**全筛掉**。
    """
    records = load_runs(path)
    if surface:
        records = [r for r in records if _surface_of(str(r.get("driver") or "")) == surface]
    branch_counts: dict[str, int] = {}
    drivers: dict[str, int] = {}
    surfaces: dict[str, int] = {}
    body_lens: list[int] = []
    elements: list[int] = []
    observations = 0
    blocked = 0
    antibot = 0
    loginwall = 0
    search_blocked_runs = 0
    negatives: list[Mapping[str, Any]] = []
    task_ids: list[str] = []
    shared_buckets: dict[str, list[int]] = {}
    face_buckets: dict[str, list[int]] = {}
    #: **按族分列**的桶——判据②③ 的读数是混合中位数，分列后才同物相比。
    family_body: dict[str, list[int]] = {}
    family_shared: dict[str, dict[str, list[int]]] = {}
    family_count: Counter = Counter()
    family_shared_count: Counter = Counter()
    basis_observations = 0
    shared_observations = 0
    pages: dict[str, list[Mapping[str, Any]]] = {}

    for record in records:
        task_id = str(record.get("task_id") or "")
        if task_id:
            task_ids.append(task_id)
        driver = str(record.get("driver") or "")
        drivers[driver or "(未记)"] = drivers.get(driver or "(未记)", 0) + 1
        surface = _surface_of(driver)
        surfaces[surface] = surfaces.get(surface, 0) + 1
        if record.get("search_blocked"):
            search_blocked_runs += 1

        ledger = record.get("ledger") or {}
        for branch, count in (ledger.get("by_branch") or {}).items():
            branch_counts[branch] = branch_counts.get(branch, 0) + int(count)
            if branch in BLOCK_BRANCHES:
                blocked += int(count)
            if branch in ANTI_BOT_BRANCHES:
                antibot += int(count)
            if branch in ACCESS_WALL_BRANCHES:
                loginwall += int(count)

        for slot, obs in _observation_slots(record):
            observations += 1
            family = _slot_family(slot)
            family_count[family] += 1
            body_lens.append(_body_len(obs))
            family_body.setdefault(family, []).append(_body_len(obs))
            elements.append(_elements(obs))
            shared, face, has_basis, has_shared = _basis_buckets(obs)
            if has_basis:
                basis_observations += 1
            if has_shared:
                shared_observations += 1
                family_shared_count[family] += 1
            for stage, values in shared.items():
                shared_buckets.setdefault(stage, []).extend(values)
                family_shared.setdefault(family, {}).setdefault(stage, []).extend(values)
            for stage, values in face.items():
                face_buckets.setdefault(stage, []).extend(values)

        for outcome in record.get("outcomes") or ():
            if not isinstance(outcome, Mapping):
                continue
            if not outcome.get("is_negative_sample"):
                continue
            negatives.append({
                "task_id": task_id,
                "url": outcome.get("url") or outcome.get("site_url") or "",
                "branch": outcome.get("branch") or "",
                "evidence": str(outcome.get("evidence") or "")[:300],
            })

        rows = _page_rows(record)
        if rows:
            pages.setdefault(task_id, []).extend(rows)

    return SideSummary(
        label=label,
        source=str(path),
        runs=len(records),
        task_ids=tuple(sorted(task_ids)),
        drivers=drivers,
        surfaces=surfaces,
        observations=observations,
        branch_counts=branch_counts,
        body_len_median=statistics.median(body_lens) if body_lens else None,
        elements_median=statistics.median(elements) if elements else None,
        blocked_outcomes=blocked,
        search_blocked_runs=search_blocked_runs,
        antibot_outcomes=antibot,
        loginwall_outcomes=loginwall,
        negatives=tuple(negatives),
        basis_observations=basis_observations,
        shared_observations=shared_observations,
        shared_ledger=_stage_medians(shared_buckets),
        face_ledger=_stage_medians(face_buckets),
        body_len_median_by_family={
            fam: float(statistics.median(vals))
            for fam, vals in sorted(family_body.items())
            if vals
        },
        shared_ledger_by_family={
            fam: _stage_medians(buckets)
            for fam, buckets in sorted(family_shared.items())
        },
        observations_by_family=dict(sorted(family_count.items())),
        shared_observations_by_family=dict(sorted(family_shared_count.items())),
        pages=pages,
    )


# ── 判据 ────────────────────────────────────────────────────────


def _round(value: float | None) -> float | None:
    """四舍五入到两位。``None`` 透传——**缺读数不是 0**。"""
    return None if value is None else round(value, 2)


def _pct(value: float | None) -> str:
    """百分比读数的显示。**缺读数显示「—」，不显示 0.0%**——
    「没配上一对」和「配上了但一个都没被拦」是两件事。"""
    return "—" if value is None else f"{value:.1f}%"


def _ratio_diff(base: float | None, cand: float | None) -> float | None:
    """``|cand-base| / base``。基线为 0/None 时**返回 None（不可判）**，
    不返回 0——那会把「没法判」写成「一模一样」。"""
    if not base:
        return None
    return abs((cand or 0.0) - base) / base


def _surface_label(side: SideSummary) -> str:
    """一侧的面（人类可读）。多个面就都说出来——「混了两种面」的目录
    本身就是要报出来的事实。"""
    return "/".join(sorted(side.surfaces)) or "(未记)"


def _same_surface(a: SideSummary, b: SideSummary) -> bool:
    """两侧是不是**同一个面**。

    同面 ⇒ 面内口径的 ``elements_total`` 数的是同一个集合，可以直接比。
    不同面 ⇒ 两个数**不是同一个东西**，差值不可解释。
    """
    return bool(a.surfaces) and a.surfaces == b.surfaces


def _family_rows(
    a_values: Mapping[str, float],
    b_values: Mapping[str, float],
    *,
    a_counts: Mapping[str, int] | None = None,
    b_counts: Mapping[str, int] | None = None,
) -> dict[str, dict[str, Any]]:
    """按**观察槽族**分列的两面对比——同族相比才是同物相比。

    ⚠️ **不是投票**：各族一行都摆出来，谁好谁坏**由人看**。
    整体中位数只是「各族混在一起」的读数，可能被某一族的占比带走。
    """
    rows: dict[str, dict[str, Any]] = {}
    for fam in sorted(set(a_values) | set(b_values)):
        va, vb = a_values.get(fam), b_values.get(fam)
        rows[fam] = {
            "a": va,
            "b": vb,
            "diff": None if va is None or vb is None else round(vb - va, 2),
            "ratio": None if va in (None, 0) or vb is None else round(vb / va, 4),
            "a_n": (a_counts or {}).get(fam),
            "b_n": (b_counts or {}).get(fam),
        }
    return rows


def _attribution_rows(
    ledger_a: Mapping[str, float], ledger_b: Mapping[str, float]
) -> list[dict[str, Any]]:
    """两本账逐档对相——**差值落在哪一档，一眼可见**。

    档位顺序用 :data:`element_basis.LADDER_STAGES` + 共享口径的基数档
    （``candidates`` / ``hidden`` / ``visible``）——**枚举基础在前、过滤档在后**，
    读的时候顺序就是「差在哪一步」。
    """
    order = list(SHARED_BASE_STAGES) + [
        s for s in LADDER_STAGES if s not in SHARED_BASE_STAGES
    ]
    known = set(order)
    extra = sorted(
        (set(ledger_a) | set(ledger_b)) - known,
        key=lambda s: s,
    )
    rows: list[dict[str, Any]] = []
    for stage in order + extra:
        va = ledger_a.get(stage)
        vb = ledger_b.get(stage)
        if va is None and vb is None:
            continue
        rows.append({
            "stage": stage,
            "a": va,
            "b": vb,
            "diff": None if (va is None or vb is None) else round(vb - va, 2),
            "ratio": None if va in (None, 0) or vb is None else round(vb / va, 4),
        })
    return rows


def compare(a: SideSummary, b: SideSummary) -> dict[str, Any]:
    """逐条判 §6.1 的四条机械判据，并把两条人判项原样列出来。

    返回 JSON 可序列化的报告。**不合成总分**——``verdict`` 只有
    ``pass`` / ``fail`` / ``incomplete``（有判不了的条目）三种，
    且每条判据的读数都留在报告里，人可以逐条推翻。
    """
    paired = sorted(set(a.task_ids) & set(b.task_ids))
    only_a = sorted(set(a.task_ids) - set(b.task_ids))
    only_b = sorted(set(b.task_ids) - set(a.task_ids))

    # 配对子集（同一批页面）——占比口径的读数要先经它才知道能不能信
    paired_pages = pair_pages(a.pages, b.pages)

    criteria: list[dict[str, Any]] = []

    # ① 分支分布：逐分支的占比差（百分点），超出 5pp 的逐条点名
    shares_a, shares_b = a.branch_shares, b.branch_shares
    branch_diff: dict[str, dict[str, float]] = {}
    offenders: list[str] = []
    for branch in sorted(set(shares_a) | set(shares_b)):
        sa = shares_a.get(branch, 0.0)
        sb = shares_b.get(branch, 0.0)
        branch_diff[branch] = {"a_pct": round(sa, 2), "b_pct": round(sb, 2),
                               "diff_pp": round(sb - sa, 2)}
        if abs(sb - sa) > GATE_BRANCH_PP:
            offenders.append(branch)
    criteria.append({
        "criterion": "分支分布",
        "gate": f"逐分支占比差 ≤ {GATE_BRANCH_PP}pp",
        "result": "pass" if not offenders else "fail",
        "detail": {"per_branch": branch_diff, "offenders": offenders},
    })

    # ② 正文中位数差
    #
    # ⚠️ 这是**混合中位数**——搜索页 / 站内步骤 / 站点页 / 播放页混在一起取。
    # 各族正文规模差一个量级（实测：站内步骤几百字，客户端渲染的站点页 0 字），
    # 混起来的读数**随各族占比漂**。所以另给**按族分列**的读数。
    body_ratio = _ratio_diff(a.body_len_median, b.body_len_median)
    criteria.append({
        "criterion": "body_text 长度中位数",
        "gate": f"相对基线差 ≤ {GATE_MEDIAN_RATIO:.0%}",
        "result": ("incomplete" if body_ratio is None
                   else "pass" if body_ratio <= GATE_MEDIAN_RATIO else "fail"),
        "detail": {"a": a.body_len_median, "b": b.body_len_median,
                   "ratio": None if body_ratio is None else round(body_ratio, 4),
                   "by_family": _family_rows(
                       a.body_len_median_by_family, b.body_len_median_by_family,
                       a_counts=a.observations_by_family,
                       b_counts=b.observations_by_family),
                   "note": ("整体读数是**混合中位数**（各族混在一起取）——"
                            "按族分列的 by_family 才是同物相比。"
                            "各族没有谁好谁坏，**由人看**。")},
    })

    # ③ 面内元素条数中位数差（**跨面时不可判**——见下面那条）
    #
    # ⚠️ 这一条以前是拿两面的 ``elements_total`` 直接比，得出「−70%」。
    # 那**不是一个可解释的量**：obscura 数的是上游工具给的交互元素，
    # camoufox 数的是 ARIA 树里过了词表的那些——两个不同的集合。
    # 差值里混着「枚举基础不同」与「过滤规则不同」两件事。
    #
    # 所以跨面时它降为 ``incomplete``（**不是 fail，也不是 pass**）：
    # 判据的结果只有三种，缺一不可——把「判不了」写成「一样」或
    # 「差太多」都是编造。真正能判的是下一条**共享口径**。
    elements_ratio = _ratio_diff(a.elements_median, b.elements_median)
    if _same_surface(a, b):
        elements_result = ("incomplete" if elements_ratio is None
                           else "pass" if elements_ratio <= GATE_MEDIAN_RATIO
                           else "fail")
        elements_note = ("两面同一个面（{}），面内口径数的是同一个集合，"
                         "可以直接比".format(_surface_label(a)))
    else:
        elements_result = "incomplete"
        elements_note = (
            "**两面数的是不同的集合**（A={} / B={}）——obscura 数上游工具给的"
            "交互元素，camoufox 数 ARIA 树里过了词表的那些。这个差值混着"
            "「枚举基础不同」与「过滤规则不同」两件事，**不可解释**。"
            "要判就判下一条共享口径的判据（同一段 JS + 同一把尺子，"
            "按构造可比）。".format(_surface_label(a), _surface_label(b))
        )
    criteria.append({
        "criterion": "interactive_elements 条数中位数（面内口径）",
        "gate": f"相对基线差 ≤ {GATE_MEDIAN_RATIO:.0%}"
                + ("（仅同面可比）" if not _same_surface(a, b) else ""),
        "result": elements_result,
        "detail": {"a": a.elements_median, "b": b.elements_median,
                   "ratio": None if elements_ratio is None else round(elements_ratio, 4),
                   "note": elements_note},
    })

    # ③' **跨面可比**的元素条数中位数（共享口径，设计方案 04 §4.9.6）
    #
    # 这一条才是 §6.1 判据③在换层场景下的**可判形式**：
    # 同一段 JS、同一把尺子，在两个浏览器上各跑一次 ⇒ 按构造可比。
    # 没有开 ``--element-basis`` 就没有这个数 ⇒ ``incomplete``，
    # **不是 pass**：「没测」与「一样」是两件事。
    shared_a = a.shared_ledger.get("kept")
    shared_b = b.shared_ledger.get("kept")
    shared_ratio = _ratio_diff(shared_a, shared_b)
    criteria.append({
        "criterion": "跨面可比元素条数中位数（共享口径）",
        "gate": f"相对基线差 ≤ {GATE_MEDIAN_RATIO:.0%}",
        "result": ("incomplete" if shared_ratio is None
                   else "pass" if shared_ratio <= GATE_MEDIAN_RATIO else "fail"),
        "detail": {
            "a": shared_a, "b": shared_b,
            "ratio": None if shared_ratio is None else round(shared_ratio, 4),
            "a_shared_observations": a.shared_observations,
            "b_shared_observations": b.shared_observations,
            "by_family": _family_rows(
                {fam: ledger.get("kept")
                 for fam, ledger in a.shared_ledger_by_family.items()},
                {fam: ledger.get("kept")
                 for fam, ledger in b.shared_ledger_by_family.items()},
                a_counts=a.shared_observations_by_family,
                b_counts=b.shared_observations_by_family),
            "note": ("共享口径 = 同一段 JS + 同一把尺子在两个浏览器上各跑一次。"
                     "没有开 --element-basis 就没有这个数——"
                     "**判不了不等于一样**。"
                     "⚠️ 整体读数是**混合中位数**：各族（搜索页/站内步骤/站点页/"
                     "播放页）规模差一个量级，混起来的结论会**随各族占比漂**，"
                     "甚至**在不同族之间翻号**（实测：站内步骤 B 更好、"
                     "播放页 A 更好）。by_family 才是同物相比。"),
        },
    })

    # ④ 拦截率：**读数不是判据**（§6.1 改过口径）
    #
    # ⚠️ 占比口径的拦截率**混着观测能力差**（两批样本看的不是同一批页面，
    #    且两面抽正文的能力不同）⇒ 另给**配对子集**上的读数：同一批页面
    #    两面各判成什么。那个数才是「同一个站点，B 面是不是更容易被拦」。
    #
    # ⚠️⚠️ **合并口径（反爬 + 登录墙）会给出相反的结论**——实测：
    #    合并口径 B 34.09% > A 19.15%（像「B 更容易被拦」），
    #    但**真·反爬**是 B 9.09% < A 14.89%——B 反而更少被拦。
    #    差全在登录墙桶里，而登录墙不是反爬。**两个读数都得给。**
    criteria.append({
        "criterion": "反爬拦截率（读数，不构成否决）",
        "gate": "仅当 b 显著高于 a 才是否决项（人判）",
        "result": "needs_manual",
        "detail": {"a_pct": a.to_json()["block_rate_pct"],
                   "b_pct": b.to_json()["block_rate_pct"],
                   "a_antibot_pct": a.to_json()["antibot_rate_pct"],
                   "b_antibot_pct": b.to_json()["antibot_rate_pct"],
                   "a_loginwall_pct": a.to_json()["loginwall_rate_pct"],
                   "b_loginwall_pct": b.to_json()["loginwall_rate_pct"],
                   "paired_a_pct": _round(paired_pages["a_block_rate_pct"]),
                   "paired_b_pct": _round(paired_pages["b_block_rate_pct"]),
                   "paired_a_antibot_pct": _round(paired_pages["a_antibot_rate_pct"]),
                   "paired_b_antibot_pct": _round(paired_pages["b_antibot_rate_pct"]),
                   "paired_a_loginwall_pct": _round(paired_pages["a_loginwall_rate_pct"]),
                   "paired_b_loginwall_pct": _round(paired_pages["b_loginwall_rate_pct"]),
                   "paired_pages": paired_pages["totals"]["paired"]},
    })

    # ⑤ 负样本成因：必须逐条人读
    criteria.append({
        "criterion": "负样本成因",
        "gate": "逐条人工读（最贵的静默是「理由通顺的假负样本」）",
        "result": "needs_manual",
        "detail": {"a_negatives": len(a.negatives), "b_negatives": len(b.negatives)},
    })

    # ⑥ 动作可移植性：要脱离本项目在裸 camoufox 里重放，本工具不做
    criteria.append({
        "criterion": "动作可移植性",
        "gate": "P1 steps[] 的动作按 camoufox 原始调用重放，脱离本项目也能跑通",
        "result": "needs_manual",
        "detail": "本工具只读存档，不重放动作",
    })

    results = {c["result"] for c in criteria}
    if "fail" in results:
        verdict = "fail"
    elif "incomplete" in results:
        verdict = "incomplete"
    else:
        verdict = "pass"

    return {
        "verdict": verdict,
        "paired_task_ids": paired,
        "only_in_a": only_a,
        "only_in_b": only_b,
        "criteria": criteria,
        "paired_pages": paired_pages,
        "attribution": {
            "shared": _attribution_rows(a.shared_ledger, b.shared_ledger),
            "face": _attribution_rows(a.face_ledger, b.face_ledger),
            "shared_by_family": {
                fam: _attribution_rows(ledger, b.shared_ledger_by_family.get(fam, {}))
                for fam, ledger in a.shared_ledger_by_family.items()
            },
            "note": ("两本账**平级**：shared 是跨面可比的那本，face 是各面自己的。"
                     "**不做差、不合成一个数**——两面的基础不是同一个东西，"
                     "合起来得到的数在两边都不是任何真实集合的大小。"
                     "⚠️ **每一档的中位数是各自独立取的**，所以这张表**不可相减**"
                     "（实测：candidates 52 − hidden 3 ≠ visible 31，"
                     "因为三档的中位数来自不同的观察）。"
                     "要逐观察看落差，跑 "
                     "research/probes/inc72_ladder_identity.py。"),
        },
        "a": a.to_json(),
        "b": b.to_json(),
        "note": ("本报告比的是「换层前后两次运行的分布差」，两批样本"
                 "**不是同一口径**（元素键名/词表随面变，见 P4）。"
                 "不要拿它当「两组同口径样本的质量对比」。"),
    }


# ── 报告 ────────────────────────────────────────────────────────


def format_report(report: Mapping[str, Any]) -> str:
    """人能读的版本。**表里每个数字都要能在 JSON 里找到同一个**。"""
    a, b = report["a"], report["b"]
    lines: list[str] = []
    lines.append(f"双跑对账：{a['label']}（A/基线） vs {b['label']}（B/候选）")
    lines.append("")
    lines.append(f"  A {a['label']}  {a['runs']} 份存档 · {a['observations']} 个观察"
                 f" · 驱动 {a['drivers']} · 面 {a['surfaces']}")
    lines.append(f"  B {b['label']}  {b['runs']} 份存档 · {b['observations']} 个观察"
                 f" · 驱动 {b['drivers']} · 面 {b['surfaces']}")
    lines.append("")
    if report["only_in_a"] or report["only_in_b"]:
        lines.append("⚠️ 两侧任务集合不同——比例差的分母因此不同义：")
        if report["only_in_a"]:
            lines.append(f"   只在 A：{', '.join(report['only_in_a'])}")
        if report["only_in_b"]:
            lines.append(f"   只在 B：{', '.join(report['only_in_b'])}")
        lines.append("")
    lines.append(f"判定：{report['verdict']}")
    lines.append("")
    lines.append("判据：")
    for c in report["criteria"]:
        lines.append(f"  [{c['result']}] {c['criterion']} —— {c['gate']}")
        detail = c.get("detail")
        if isinstance(detail, str):
            lines.append(f"        {detail}")
            continue
        for key, value in (detail or {}).items():
            if isinstance(value, dict):
                continue
            if key == "note":
                # 说明文字单独起行——它是给人读的判断依据，
                # 挤成 ``note = …`` 一行会被当成又一个字段扫过去。
                lines.append(f"        ※ {value}")
                continue
            lines.append(f"        {key} = {value}")
    lines.append("")
    family_sections = [
        (c["criterion"], c["detail"]["by_family"])
        for c in report["criteria"]
        if isinstance(c.get("detail"), dict)
        and isinstance(c["detail"].get("by_family"), dict)
        and c["detail"]["by_family"]
    ]
    if family_sections:
        lines.append("按观察槽族分列（**同族相比才是同物相比**——"
                     "整体中位数混着各族，规模差一个量级）：")
        for title, fam_rows in family_sections:
            lines.append(f"  {title}:")
            lines.append(f"    {'族':<14} {'A':>10} {'B':>10} {'比值':>8} {'条数 A/B':>12}")
            for fam, r in fam_rows.items():
                va = "—" if r["a"] is None else r["a"]
                vb = "—" if r["b"] is None else r["b"]
                ratio = "—" if r["ratio"] is None else f"{r['ratio']:.2f}"
                n = f"{r.get('a_n', '—')}/{r.get('b_n', '—')}"
                lines.append(f"    {fam:<14} {va:>10} {vb:>10} {ratio:>8} {n:>12}")
        lines.append("")
        lines.append("  ⚠️ 各族**没有投票、没有合成**——谁好谁坏由人看。"
                     "整体那一行会被各族占比带走。"
                     "（条数：正文那张是该族**观察数**；"
                     "共享口径那张是该族**带共享账的观察数**。）")
        lines.append("")
    paired = report.get("paired_pages") or {}
    if paired:
        totals = paired.get("totals") or {}
        lines.append("配对比较（**同一批页面**两面的判定——占比口径的读数要经它才可信）：")
        lines.append(f"  配对 {totals.get('paired', 0)} 个页面"
                     f" · 只在 A {totals.get('only_a', 0)}"
                     f" · 只在 B {totals.get('only_b', 0)}")
        lines.append(f"  分歧 {totals.get('disagreements', 0)} 处"
                     f" · 配对子里带降级的 A {totals.get('a_degraded', 0)}"
                     f" / B {totals.get('b_degraded', 0)}")
        a_pct = paired.get("a_block_rate_pct")
        b_pct = paired.get("b_block_rate_pct")
        lines.append(f"  配对子集拦截率： A {_pct(a_pct)}   B {_pct(b_pct)}")
        lines.append(f"    ├ 真·反爬（blocked）：        A {_pct(paired.get('a_antibot_rate_pct'))}"
                     f"   B {_pct(paired.get('b_antibot_rate_pct'))}")
        lines.append(f"    └ 登录墙（要登录/要会员）：   A {_pct(paired.get('a_loginwall_rate_pct'))}"
                     f"   B {_pct(paired.get('b_loginwall_rate_pct'))}")
        for cell, n in (paired.get("matrix") or {}).items():
            lines.append(f"    {cell:<52} ×{n}")
        for task, t in (paired.get("per_task") or {}).items():
            if not t.get("disagreements"):
                continue
            lines.append(f"  {task} 的分歧逐条：")
            for d in t["disagreements"]:
                lines.append(f"    · {str(d['url'])[:70]}")
                lines.append(f"        A: {d['a_branch'] or '(成功)'}"
                             f"  degraded={d['a_degraded']}  body={d['a_body_len']}")
                lines.append(f"        B: {d['b_branch'] or '(成功)'}"
                             f"  degraded={d['b_degraded']}  body={d['b_body_len']}")
        lines.append("")
        lines.append(f"  {paired.get('note', '')}")
        lines.append("")
    attribution = report.get("attribution") or {}
    shared_rows = attribution.get("shared") or []
    face_rows = attribution.get("face") or []
    if shared_rows or face_rows:
        lines.append("元素差值归因（**差在哪一档**）：")
        for title, rows in (("shared（跨面可比）", shared_rows),
                            ("face（各面自己的）", face_rows)):
            if not rows:
                continue
            lines.append(f"  {title}:")
            lines.append(f"    {'档位':<20} {'A':>10} {'B':>10} {'差':>10}")
            for row in rows:
                va = "—" if row["a"] is None else row["a"]
                vb = "—" if row["b"] is None else row["b"]
                diff = "—" if row["diff"] is None else f"{row['diff']:+g}"
                lines.append(f"    {row['stage']:<20} {va:>10} {vb:>10} {diff:>10}")
        lines.append("")
        lines.append(f"  {attribution.get('note', '')}")
        lines.append("")
    for side in (a, b):
        lines.append(f"{side['label']} 分支分布：")
        for branch, pct in sorted(side["branch_shares"].items(),
                                  key=lambda kv: -kv[1]):
            lines.append(f"    {branch:<28} {side['branch_counts'][branch]:>4}  {pct:>6.1f}%")
        lines.append(f"    —— 合并拦截率 {side.get('block_rate_pct')}% ＝"
                     f" 真·反爬 {side.get('antibot_rate_pct')}%"
                     f" ＋ 登录墙 {side.get('loginwall_rate_pct')}%"
                     f"（站点 {side.get('outcomes_total')}）")
        lines.append("")
    lines.append("负样本（逐条读，不要只看条数）：")
    for side in (a, b):
        lines.append(f"  [{side['label']}] {len(side['negatives'])} 条")
        for n in side["negatives"][:20]:
            lines.append(f"    - {n['task_id']} {n['branch']} {n['url']}")
            lines.append(f"      {n['evidence'][:160]}")
        if len(side["negatives"]) > 20:
            lines.append(f"    …另 {len(side['negatives']) - 20} 条（见 --json）")
    lines.append("")
    lines.append(report["note"])
    return "\n".join(lines)