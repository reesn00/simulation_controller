"""工具声明（:mod:`trajectory_pipeline.toolcatalog`）的**契约测试**。

这块新增的机械断言，每一条都对着一条**已踩过的坑**或一条**会被静默
毁掉训练数据**的口径：

1. **红线词** —— description 里混进 ``ref`` / ``obscura`` / ``cookie`` 这类词，
   等于把会话句柄或凭据处理方式教给模型。静默，且**训练时才发现**。
2. **``required`` 口径** —— 把可选参数标成必填，模型就会给每次回车都编
   一个元素（真实分布里 70% 的回车没有目标）。这条按**真实存档的出现率**
   判定，不按设计愿望。
3. **catalog 覆盖执行端** —— 执行器会发而 catalog 没有的动作 ⇒ 模型见到
   一种不被允许的动作。
4. **P2 / P3 / T 三处一致** —— 三处各抄一份的话，工具扩到 13 条时必漂。
5. **能力声明 vs 已验证动作** —— ``exercised=False`` 的工具**不允许**在真实
   存档里出现（它没有真实产地）；``exercised=True`` 的**必须**出现
   （标错了就是撒谎）。

⚠️ **所有口径都是逐工具面（surface）的**（2026-10-20 方案 15 §7 R1）
——``click`` 在 obscura 面是 ``click(tag,label)``、在两张 aria 面
（camoufox-1 / camofox-1）是 ``click(role,name)``，**同名不同参**。
拿一个面的口径去查另一个面，红绿都是假的。所以下面每条按面参数化，
查存档时**也按面过滤**（盘上现存的数据全是 obscura 面的，camoufox 面的
分母是 0——那不是「通过」，是**还没数据**，等它有数据再开）。

⚠️ **三张面，各有各的生命周期**（2026-10-26 owner 裁定）：

============  ==========================  ======================
面            定位                         状态
============  ==========================  ======================
obscura-1     ``{tag,label}``              存量方言（回溯参照系）
camoufox-1    ``{role,name}`` + exact     **已退役**，只读存量
camofox-1     ``{role,name}`` + 子串匹配   **现行生产面**
============  ==========================  ======================

「退役」说的是**不再产出**，不是「读不懂」——盘上 430 份 P2 样本与
14 份 P1 存档都记着 camoufox-1，删了这个代号那些产物就成了读不出来的
字符串。⇒ **删的是实现，不是证据**。
"""

from __future__ import annotations

import ast
import json
import re
import warnings
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from trajectory_pipeline import toolcatalog

PKG = Path(toolcatalog.__file__).parent
PIPELINE = PKG.parent / "output" / "pipeline"
SAMPLES = PIPELINE / "samples"
ARCHIVES = PIPELINE

#: 全部工具面。逐面参数化用。
SURFACES = tuple(toolcatalog.SURFACES)
DEFAULT_SURFACE = toolcatalog.DEFAULT_SURFACE


# ── 1 · 红线词 ──────────────────────────────────────────────────────

#: **两个面都禁**的词。**不是**风格偏好——每一条都对应一条红线
#: 或一条会让模型学错东西的表述。
#:
#: - ``ref`` / ``handle`` / ``句柄`` —— 会话内句柄，跨会话失效，进了训练
#:   数据等于让模型学一个随机数，且样本永远不可回放；
#: - ``mcp`` —— 传输协议名，与执行层无关；
#: - ``cookie`` / ``profile`` / ``authorization`` —— 凭据红线，永久禁。
#:
#: ⚠️ ``obscura`` **不在这一档**：D12 之后执行层是 camoufox（2026-10-26
#: 起是 camofox），禁的是
#: 「**已经不是**执行层的那个实现名」，而 obscura 恰好是所有面都要禁的
#: 那个（它是历史实现）。见 :data:`FORBIDDEN_PER_SURFACE`。
FORBIDDEN_COMMON: tuple[str, ...] = (
    "ref",
    "refs",
    "ref_id",
    "refid",
    "handle",
    "句柄",
    "mcp",
    "cookie",
    "profile",
    "authorization",
    "token",
    "令牌",
    "凭据",
    "password",
)

#: **逐面**的额外禁用词。
#:
#: 规则（方案 04 §5.1）：**只说当前执行层的调用面**。
#:
#: - 每个面都禁 ``obscura``——它已经不是执行层了；
#: - obscura 面禁 ``camoufox`` **与** ``camofox``——那两个是**别的**调用面，
#:   在 obscura 批的声明里点名它们，等于教模型一个它这批跑不出来的面；
#: - camofox 面（**现行生产面**）禁 ``camoufox``——那是**已退役**的调用面；
#: - camoufox 面（**只读存量面**）禁 ``camofox``——反过来同理；
#: - 每个 aria 面**允许**点名**自己**的机制（``camofox`` 面点名
#:   ``camofox``；``camoufox`` 面点名 ``camoufox`` / ``get_by_role``），
#:   且**必须**点名（见 :class:`Test工具面` 的正向断言）。
FORBIDDEN_PER_SURFACE: dict[str, tuple[str, ...]] = {
    toolcatalog.SURFACE_OBSCURA: ("obscura", "camoufox", "camofox"),
    toolcatalog.SURFACE_CAMOUFOX: ("obscura", "camofox"),
    toolcatalog.SURFACE_CAMOFOX: ("obscura", "camoufox"),
}


def forbidden_words(surface: str) -> tuple[str, ...]:
    """某个面的完整禁用词表。"""
    return FORBIDDEN_COMMON + FORBIDDEN_PER_SURFACE.get(surface, ())


def _all_defs() -> list[tuple[str, object]]:
    """``(面, 声明)`` 的笛卡尔积——逐面参数化用。"""
    return [
        (surface, d)
        for surface, defs in toolcatalog.SURFACES.items()
        for d in defs
    ]


#: 全部面的全部声明。⚠️ **两个面的同名工具各算一条**——它们是**不同的
#: 声明**（参数不同、``exercised`` 不同），漏掉一个面就等于漏检一半。
ALL_DEFS: list[tuple[str, object]] = _all_defs()


def _contains_token(blob: str, token: str) -> bool:
    """词边界感知的包含判定。

    裸 ``"ref" in blob`` 会误伤 ``prefer``；而裸正则 ``\\bref\\b`` 在中文
    文本里又抓不到（中文与英文之间没有 ``\\b``）。所以自己判：token 两侧
    **不是 ASCII 字母数字**才算命中——于是 ``prefer`` 不命中、
    ``的ref`` 命中、``ref_id`` 命中。
    """
    low = blob.lower()
    t = token.lower()
    start = low.find(t)
    while start != -1:
        before = low[start - 1] if start > 0 else " "
        end = start + len(t)
        after = low[end] if end < len(low) else " "
        if not (before.isascii() and before.isalnum()) and \
           not (after.isascii() and after.isalnum()):
            return True
        start = low.find(t, start + 1)
    return False


class Test红线词:
    @pytest.mark.parametrize(
        "surface,d", ALL_DEFS, ids=lambda x: x if isinstance(x, str) else x.name)
    def test_说明不含红线词(self, surface, d):
        blob = d.instruction + " ".join(p.desc for p in d.params)
        for w in forbidden_words(surface):
            assert not _contains_token(blob, w), (
                f"工具 {d.name}（{surface} 面）的说明里出现了红线词 "
                f"{w!r}：{blob[:120]}"
            )

    def test_说明不是一句话敷衍(self):
        """模型看得见的工具，说明是**行为契约**——写清「什么时候会失败」
        「失败了该怎么办」。

        旧口径的说明只有「打开一个网址。」——模型据此不知道 navigate 失败
        时该换路还是该重试，于是学出「重复点同一个」这类动作。

        基础设施动作（``new_tab`` / ``close_tab``）**不检查**：模型根本
        看不到它们的说明。
        """
        for surface, defs in toolcatalog.SURFACES.items():
            for d in defs:
                if d.visibility != "model":
                    continue
                assert len(d.instruction) >= 20, (
                    f"工具 {d.name}（{surface} 面）的说明太短，"
                    f"读不出行为契约：{d.instruction!r}"
                )

    @pytest.mark.parametrize(
        "surface,d", ALL_DEFS, ids=lambda x: x if isinstance(x, str) else x.name)
    def test_每个参数都有说明(self, surface, d):
        for p in d.params:
            assert p.desc.strip(), f"工具 {d.name} 的参数 {p.name} 没有说明"

    @pytest.mark.parametrize(
        "surface,d", ALL_DEFS, ids=lambda x: x if isinstance(x, str) else x.name)
    def test_模型可见的说明不是markdown(self, surface, d):
        """工具说明进的是 JSON 的 ``function.description``——**不是文档**。

        Markdown 强调号（``**``）在那一层是纯噪声：模型看到的是两个
        星号，不是「强调」。写说明时容易顺手带上，因为同一批文字在
        docstring 里是给人看的。

        ⚠️ ``**/`` 是 **glob 的递归标记**（``**/*.json``），不是 markdown
        ——所以只查「后面不跟 ``/``」的 ``**``。
        """
        if d.visibility != "model":
            return
        blob = d.instruction + " ".join(p.desc for p in d.params)
        hit = re.search(r"\*\*(?!/)", blob)
        assert hit is None, (
            f"工具 {d.name} 的说明里带 markdown 强调号：{blob[:120]}"
        )

    def test_红线工具本身不在清单里(self):
        """上游文档里的红线工具**一个都不许**进训练侧声明。"""
        banned = {
            "get_cookies", "set_cookies", "clear_cookies",
            "set_storage_state", "get_storage_state",
        }
        for surface, defs in toolcatalog.SURFACES.items():
            assert not ({d.name for d in defs} & banned), (
                f"{surface} 面混进了红线工具"
            )


# ── 2 · required 口径 = 真实存档的出现率 ───────────────────────────


def _walk_steps(archive: dict):
    yield from archive.get("steps") or ()
    for v in archive.get("visits") or ():
        yield from v.get("steps") or ()


# ── 盘上产物怎么找（2026-10-21：glob 改递归）─────────────────────
#
# ⚠️ **原来的 glob 不递归，漏掉了一整批真实数据**。
# ``ARCHIVES.glob("*.json")`` 只看 ``output/pipeline/`` 根目录，而
# **双跑的存档在 ``dualrun/{obscura,camoufox}/`` 下** ⇒ camoufox 面的
# 真实调用**一条都看不见** ⇒ 那几条契约测试**永远是 skip 而不是 pass**。
# 「没数据」与「通过了」在报告上长得一模一样——这正是要治的病。
#
# 改成递归之后，**判「是不是存档」就不能靠路径了**（目录会变，
# ``dualrun/`` 是 2026-10-20 才加的）：改**按形状判**。

#: 产物的文件名形状：``<task_id>__<run_id>.json``（P1 存档）或
#: ``<task_id>__<run_id>__<slot>.json``（P2 样本 / P3 / T 视图）。
#: ``run_id`` 是 8 位十六进制（``uuid4().hex[:8]``，见 ``executor/archive.py``）。
#: **按名字筛只是省钱**（探针报告、计划文件、工作表都在同一个根下），
#: 判据是下面的**形状**。
#:
#: ⚠️ **2026-10-22 修正：末尾那段 ``(..)?`` 是后补的，之前漏了**。
#: 原式 ``^[^/]+__[0-9a-f]{8}\.json$`` **只放行两段名**，于是
#: **盘上 1444 个 P2 样本一个都进不来**（实测 ``_walk_products(_is_p2_sample)``
#: 恒返回 **0**）。后果不是报错，是**静默换统计源**：
#: :func:`_observed_from_p2` 永远交白卷 ⇒ :func:`_observed` 走
#: 「P2 没落盘就退回 P1」那条 fallback ⇒ **``required`` 一直是按 P1 存档
#: 算的，而代码与文档都写着「统计源选 P2 样本」**（required 是训练数据的
#: 口径，训练数据是 P2）。
#:
#: 为什么一直没被发现：P1 存档**确实有**数据，fallback 拿得到分母，
#: 于是断言照常跑、照常绿——**两个口径给出相近结论，差异被抹平了**。
#: 这与增量七十一抓到的那条谎话是同一类：**标注/口径说着一套，数据是另一套**。
_ARCHIVE_NAME = re.compile(r"^[^/]+__[0-9a-f]{8}(?:__.+)?\.json$")

#: 读过的文件缓存。键里带 ``mtime`` 与大小 ⇒ **文件一改缓存自动失效**，
#: 不会拿着上一版的读数当这一版的。
_FILE_CACHE: dict[tuple[str, int, int], Any] = {}


def _load_json(path: Path) -> Any:
    """读一个 JSON 文件。读不了 / 不是 JSON → ``None``。

    ``None`` 的含义是「**没读到**」，不是「读到空内容」——这两者在
    统计里必须分开（前者不进分母，后者会让分母变小）。
    """
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _FILE_CACHE:
        return _FILE_CACHE[key]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    _FILE_CACHE[key] = payload
    return payload


def _is_p1_archive(payload: Any) -> bool:
    """是不是 P1 存档——**按形状判，不按路径判**。

    判据：顶层有 ``run_config``（采集运行的配置）**且**有 ``steps`` 或
    ``visits``（动作流）。

    ⚠️ **为什么不用目录判**：目录清单会漂——``dualrun/`` 是后加的，
    ``proberun/`` 是探针的，``_backup_pre_motivation/`` 是备份的。
    按目录列白名单，漏一个目录就是**静默少算一批数据**，而少算的那批
    在报告上看不出任何异常。

    P2 样本 / P3 视图 / T 视图 / 媒体探针结果**都不满足**这条：
    P2 有 ``six`` 没有 ``run_config``；P3 视图是消息数组或
    ``{messages, tools, sample_id}``；T 视图是 ``{trajectory_id, messages}``；
    媒体探针是 ``{requested_url, media_count, …}``。
    """
    if not isinstance(payload, dict):
        return False
    if "run_config" not in payload:
        return False
    return bool(payload.get("steps") or payload.get("visits"))


def _is_p2_sample(payload: Any) -> bool:
    """是不是 P2 样本——**按形状判**：顶层有 ``six``（六件套的容器）。"""
    return isinstance(payload, dict) and isinstance(payload.get("six"), dict)


def _walk_products(is_product: Any) -> list[dict]:
    """扫 ``output/pipeline/`` **整棵子树**，挑出形状对得上的产物。

    ⚠️ **按 run_id / sample_id 去重**：``_backup_pre_motivation/`` 里躺着
    根目录某条存档的**副本**——同一个 run 算两次，``required`` 口径
    （按出现率算）**分母就被重复计数**。**一个 run 一份存档**，副本不另算。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for path in sorted(PIPELINE.rglob("*.json")):
        if not _ARCHIVE_NAME.match(path.name):
            continue
        payload = _load_json(path)
        if not is_product(payload):
            continue
        ident = str(payload.get("run_id") or payload.get("sample_id") or "").strip()
        if ident:
            if ident in seen:
                continue
            seen.add(ident)
        out.append(payload)
    return out


def _observed_from_p2(surface: str = DEFAULT_SURFACE) -> tuple[Counter, dict[str, Counter], dict[str, Counter]]:
    """扫 P2 样本，返回 ``(调用次数, 键在场次数, 非空值次数)``。

    统计源选 **P2 样本**而不是 P1 存档：``required`` 是**训练数据**的口径，
    训练数据是 P2。P1 存档是取证态，量纲一样但口径不同（同一个动作在 P1
    里按 run 计数，在 P2 里按决策单元切分）。

    ``surface`` 过滤：**只统计那个方言的样本**。P2 没有 ``tool_surface``
    键的（2026-10-20 之前建的）算默认面——那批样本真实跑出来的就是它。
    """
    calls: Counter = Counter()
    keys: dict[str, Counter] = defaultdict(Counter)
    nonempty: dict[str, Counter] = defaultdict(Counter)
    for sample in _walk_products(_is_p2_sample):
        sample_surface = str(sample.get("tool_surface") or "") or DEFAULT_SURFACE
        if sample_surface != surface:
            continue
        for a in (sample.get("six") or {}).get("actions") or ():
            tool = str(a.get("tool") or "")
            if not tool:
                continue
            calls[tool] += 1
            raw: dict = dict(a.get("params") or {})
            target = a.get("target")
            if isinstance(target, dict):
                raw.update(target)
            for k, v in raw.items():
                keys[tool][k] += 1
                if v not in (None, "", [], {}):
                    nonempty[tool][k] += 1
    return calls, keys, nonempty


def _observed_from_p1(surface: str = DEFAULT_SURFACE) -> tuple[Counter, dict[str, Counter], dict[str, Counter]]:
    """P2 没落盘时退回 P1 存档（口径同上，量纲按 run 计）。

    同样按 ``surface`` 过滤：面从存档的 ``driver`` 折算（没记 = 默认面）。
    """
    calls: Counter = Counter()
    keys: dict[str, Counter] = defaultdict(Counter)
    nonempty: dict[str, Counter] = defaultdict(Counter)
    for archive in _walk_products(_is_p1_archive):
        try:
            arch_surface = toolcatalog.surface_of_driver(archive.get("driver"))
        except ValueError:
            continue  # 认不出的驱动名 = 认不出的方言，不参与统计
        if arch_surface != surface:
            continue
        for step in _walk_steps(archive):
            action = step.get("action") or {}
            tool = str(action.get("tool") or "")
            if not tool:
                continue
            calls[tool] += 1
            raw: dict = dict(action.get("params") or {})
            target = action.get("target")
            if isinstance(target, dict):
                raw.update(target)
            for k, v in raw.items():
                keys[tool][k] += 1
                if v not in (None, "", [], {}):
                    nonempty[tool][k] += 1
    return calls, keys, nonempty


def _observed(surface: str = DEFAULT_SURFACE):
    """统计结果 + 样本数。**没有产物就跳过**（产物目录不入库）。

    ⚠️ **按面过滤**：盘上现存的数据全是 obscura 面的，camoufox 面
    一次都没跑过 ⇒ 查 camoufox 面时**分母是 0**。
    那不是「通过」，是**还没数据**——所以这里 ``skip``，不是静默放过。
    """
    calls, keys, nonempty = _observed_from_p2(surface)
    if not calls:
        calls, keys, nonempty = _observed_from_p1(surface)
    if not calls:
        pytest.skip(f"没有真实产物（{surface} 面的 P2 样本 / P1 存档都没落盘）")
    return calls, keys, nonempty


class Test参数口径与真实存档一致:
    @pytest.mark.parametrize("surface", SURFACES)
    def test_必填参数就是每次都出现的参数(self, surface):
        """``required`` 按**键在场率**定——标成必填会让模型给每次回车都
        编一个元素，而真实分布里 70% 的回车没有目标（``press`` 的
        ``tag``/``label``）。"""
        calls, keys, _ = _observed(surface)
        for d in toolcatalog.defs_of(surface):
            if not d.exercised:
                # 能力声明：没有任何真实调用 ⇒ 无数据可依，required 由
                # catalog 自己声明，**不参与**统计校验。
                continue
            n = calls.get(d.name, 0)
            if not n:
                continue
            for p in d.params:
                rate = keys[d.name].get(p.name, 0) / n
                if p.required:
                    assert rate == 1.0, (
                        f"{d.name}.{p.name} 标成必填，但真实存档里只有 "
                        f"{rate:.0%} 带着它（{keys[d.name].get(p.name, 0)}/{n}）"
                    )
                else:
                    assert rate < 1.0, (
                        f"{d.name}.{p.name} 标成可选，但真实存档里 100% 出现"
                        f"（{keys[d.name].get(p.name, 0)}/{n}）——该标成必填"
                    )

    @pytest.mark.parametrize("surface", SURFACES)
    def test_必填参数的值基本不为空(self, surface):
        """键在场 ≠ 值可用。

        标成必填会教模型**每次都填**这个参数——若它有键但基本是空串，
        模型就学会了输出空字符串。护栏：非空值 ≥ 90%。

        （``click`` 的 ``label`` 是 90/91 = 98.9%，过；那个空串是个
        没有可见文字的按钮，属正常噪声。）
        """
        calls, _, nonempty = _observed(surface)
        for d in toolcatalog.defs_of(surface):
            if not d.exercised:
                continue
            n = calls.get(d.name, 0)
            if not n:
                continue
            for p in d.params:
                if not p.required:
                    continue
                occ = nonempty[d.name].get(p.name, 0)
                assert occ / n >= 0.9, (
                    f"{d.name}.{p.name} 标成必填，但真实存档里只有 "
                    f"{occ}/{n} 带非空值——必填等于教模型输出空串"
                )

    @pytest.mark.parametrize("surface", SURFACES)
    def test_真实存档里没出现过的参数不能是必填(self, surface):
        """「出现过但没带这个参数」与「压根没这个参数」是两回事。

        后者是**真缺**——标成必填会让模型每次都编一个不存在的参数。
        """
        calls, keys, _ = _observed(surface)
        for d in toolcatalog.defs_of(surface):
            if not d.exercised:
                continue
            n = calls.get(d.name, 0)
            if not n:
                continue
            for p in d.params:
                if p.required:
                    assert keys[d.name].get(p.name, 0) > 0, (
                        f"{d.name}.{p.name} 标成必填，但真实存档里从没带过它"
                    )


# ── 3 · catalog 覆盖执行端，且不谎报 exercised ─────────────────────


class Test与执行端对齐:
    def _executor_tools(self) -> set[str]:
        """AST 读 ``executor/actions.py`` 的 ``TOOLS`` 字面量。**不 import**
        ——import 会把组装端与执行端焊死，而这正是要守住的边界。"""
        src = (PKG.parent / "executor" / "actions.py").read_text(encoding="utf-8")
        for node in ast.parse(src).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == "TOOLS":
                return {
                    e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
        raise AssertionError("读不到 executor.actions.TOOLS 的字面量")

    def test_执行器会发的动作catalog里都有(self):
        """执行端发得出、catalog 没有 ⇒ 模型见到一种不被允许的动作。"""
        for surface, defs in toolcatalog.SURFACES.items():
            missing = self._executor_tools() - {d.name for d in defs}
            assert not missing, f"执行器会发但 {surface} 面没声明: {missing}"

    @pytest.mark.parametrize("surface", SURFACES)
    def test_标成已验证的工具在真实存档里出现过(self, surface):
        """``exercised=True`` 声称「有真实运行产生过调用」。没出现过就是撒谎。"""
        calls, _, _ = _observed(surface)
        missing = [d.name for d in toolcatalog.defs_of(surface)
                   if d.exercised and d.name not in calls]
        assert not missing, f"{surface} 面标成已验证但真实存档里没有: {missing}"

    @pytest.mark.parametrize("surface", SURFACES)
    def test_标成能力声明的工具不该在真实存档里出现(self, surface):
        """``exercised=False`` 声称「本项目没有真实调用」。出现了就是标注错了。"""
        calls, _, _ = _observed(surface)
        appearing = [d.name for d in toolcatalog.defs_of(surface)
                     if not d.exercised and d.name in calls]
        assert not appearing, (
            f"{surface} 面板成能力声明但真实存档里出现过: {appearing}"
            f"——该标成 exercised=True"
        )

    @pytest.mark.parametrize("surface", SURFACES)
    def test_能力声明条数在4到8之间(self, surface):
        """用户口径：「任务调用的工具说明 + 4~8 个额外不被使用的工具」。

        太多则稀释了模型对可用动作的注意力；太少则能力清单单薄。
        这条钉住**范围**，不钉住具体是哪几条——那由「语义相邻 + v2 概念上
        可实现 + 不碰红线」三条准则决定，见 catalog 模块 docstring。

        ⚠️ 按 **``family="capability"``** 计，不按「没被真实调用过」计——
        后者会把**读原语**也算进去（它们还没跑过），而读原语是**任务
        自己的工具**（pull 模式下任务真的会调它），不是「额外能力」。
        """
        n = len(toolcatalog.capability_names(surface))
        assert 4 <= n <= 8, f"{surface} 面额外（能力声明）工具 {n} 个，超出 4~8 的口径"

    def test_能力声明不含文件系统写入与多agent协作(self):
        """那些能力会把模型教去做代码任务，而不是找资源。"""
        banned = {"write_file", "append_file", "edit_file", "run_tool_batch",
                  "materialize_skill", "spawn_subagent", "submit_to_agent",
                  "chat_with_agent", "list_agents", "check_agent_task",
                  "execute_shell_command"}
        for surface in SURFACES:
            assert not (set(toolcatalog.visible_names(surface)) & banned), (
                f"{surface} 面能力声明里混进了不该有的工具: "
                f"{set(toolcatalog.visible_names(surface)) & banned}"
            )

    def test_不混写v1独占形态(self):
        """v1 的 ``browser``（Browser SDK 代码）与 v2 的 ``PageDriver`` 原语
        没有任何对应关系——两套不混写（docs/tools.md 末节）。"""
        for surface in SURFACES:
            assert "browser" not in toolcatalog.visible_names(surface)


# ── 4 · 三处一致（P2 / P3 / T） ───────────────────────────────────


class Test三处一致:
    @pytest.mark.parametrize("surface", SURFACES)
    def test_P2的TOOL_SPECS是catalog的投影(self, surface):
        from trajectory_pipeline.assembler import schema

        specs = schema.TOOL_SPECS_BY_SURFACE[surface]
        defs = toolcatalog.defs_of(surface)
        assert [t.name for t in specs] == [d.name for d in defs]
        for spec, d in zip(specs, defs):
            assert spec.params == tuple(p.name for p in d.params)
            assert spec.instruction == d.instruction
            assert spec.visible_to_model() == (d.visibility == "model")

    def test_默认面的TOOL_SPECS仍是catalog的投影(self):
        """``TOOL_SPECS`` 这个名字**留在默认面上**——下游大量按「默认面」
        取声明。改了它就是改了历史样本的训练契约。"""
        from trajectory_pipeline.assembler import schema

        assert [t.name for t in schema.TOOL_SPECS] == [
            d.name for d in toolcatalog.CATALOG]

    def test_T的工具声明是catalog的投影(self):
        from trajectory_pipeline.assembler import task_view

        assert [s["name"] for s in task_view._TASK_TOOL_SPECS] == [
            d.name for d in toolcatalog.CATALOG]

    @pytest.mark.parametrize("surface", SURFACES)
    def test_P3与T的openai工具声明逐字一致(self, surface):
        """两边是**两种独立渲染**（P3 读 P2、T 读 P1），但工具声明必须
        逐字一致——否则复核者看到的与模型学到的不是同一套。"""
        from trajectory_pipeline.assembler import task_view, views

        sample = {
            "tool_surface": surface,
            "six": {
                "tools": [d.to_json() for d in toolcatalog.defs_of(surface)],
            }
        }
        p3 = views._visible_tools(sample)
        t = task_view.task_tools(surface)
        assert p3 == t, f"{surface} 面 P3 与 T 的工具声明漂移"

    @pytest.mark.parametrize("surface", SURFACES)
    def test_可见集就是catalog的可见集(self, surface):
        from trajectory_pipeline.assembler import views

        sample = {
            "tool_surface": surface,
            "six": {
                "tools": [d.to_json() for d in toolcatalog.defs_of(surface)],
            }
        }
        names = [t["function"]["name"] for t in views._visible_tools(sample)]
        assert names == list(toolcatalog.visible_names(surface))
        # infrastructure 动作一个都不许进模型可见集
        for infra in toolcatalog.infra_names(surface):
            assert infra not in names, f"{infra} 是执行器自己的动作，不该给模型看"

    def test_函数定义带参数说明与正确required(self):
        """``click`` 必须声明 ``tag``/``label``——旧口径下它是空参数，
        训出来的模型会发空参数的点击。"""
        by_name = {f["function"]["name"]: f["function"]
                   for f in toolcatalog.openai_functions()}
        click = by_name["click"]
        assert set(click["parameters"]["properties"]) == {"tag", "label"}
        assert click["parameters"]["required"] == ["tag", "label"]
        for p in click["parameters"]["properties"].values():
            assert p.get("description", "").strip(), "参数没有说明"

        press = by_name["press"]
        assert press["parameters"]["required"] == ["key"]
        assert "tag" in press["parameters"]["properties"]
        assert "label" in press["parameters"]["properties"]

    @pytest.mark.parametrize("surface", SURFACES)
    def test_函数定义逐面带参数说明与正确required(self, surface):
        """**逐面**的函数定义：每个参数都有说明、``required`` 非空。

        camoufox 面的 ``click`` 声明的是 ``role``/``name``——与 obscura
        面的 ``tag``/``label`` **不同义**。渲染时拿错面就是「声明与调用
        对不上」，而那训出来的模型会发空参数的点击。
        """
        for f in toolcatalog.openai_functions(surface=surface):
            fn = f["function"]
            props = fn["parameters"]["properties"]
            for name, prop in props.items():
                assert prop.get("description", "").strip(), (
                    f"{surface} 面 {fn['name']}.{name} 没有参数说明"
                )
            if fn["name"] in ("click", "fill"):
                assert props, f"{surface} 面 {fn['name']} 是空参数——模型无从知道要传什么"

    def test_不编造参数类型也不加enum(self):
        """``press`` 的 ``key`` 本来是任意按键名——加 enum 会把训练集
        框死在取值子集上。"""
        for f in toolcatalog.openai_functions():
            for name, prop in f["function"]["parameters"]["properties"].items():
                assert prop.get("type") == "string", (
                    f"{f['function']['name']}.{name} 的类型不是 string"
                )
                assert "enum" not in prop, (
                    f"{f['function']['name']}.{name} 加了 enum——会框死取值分布"
                )

    def test_落盘的P2工具集与catalog一致(self):
        """盘上 P2 样本的 ``six.tools`` 名字集 == **它自己那个面**的 catalog。

        不一致 = **产物没重建**（catalog 加了工具但 P2 还是旧的）。
        这不是代码缺陷，但它是**静默**的：P3 会照着旧 P2 渲染出旧工具
        清单，而报告上看不出任何异常。

        ⚠️ **期望值必须逐面取，不能用 ``CATALOG``**（2026-10-22 修正）：
        ``toolcatalog.CATALOG`` 是 ``SURFACES[DEFAULT_SURFACE]``——
        **obscura 面**的定义集（13 条）。而 P2 的 ``six.tools`` 取的是
        :data:`assembler.schema.TOOL_SPECS_BY_SURFACE[surface]`
        = **该面的全部定义**。

        两个面**定义集不同**（方案 04 §2.2：camoufox 面多三条采集面
        读原语 ``snapshot`` / ``interactive`` / ``links``）⇒ 用 obscura
        面的集合去核 camoufox 面的样本**必然不等**。

        ⇒ **第一批 camoufox 批次落盘时，这条断言就是这么红的**——
        且红得**有信息量**（指名的正是那三条多出来的采集面读原语）。
        ⚠️ 但它红的**原因**是断言写错了，不是产物错了：产物按自己的面
        渲染，是对的。**改断言，不改产物**——把期望值换成样本自己的面。

        ⚠️ **源存档已不在的样本不参与校验**。那些是 ``purge-run`` 回滚后
        的**孤儿**：P1 存档删了，派生出来的 P2 还在（回滚只删存档与负样本
        池行，不删派生产物）。孤儿样本没有可重建的来源，重建也救不回来
        ——它只能被**清掉**。这里用警告把它们**显形**，不假装没看见。
        """
        files = sorted(SAMPLES.glob("*.json"))
        if not files:
            pytest.skip("没有落盘的 P2 样本（产物目录不入库）")
        orphans: list[str] = []
        for p in files:
            sample = json.loads(p.read_text(encoding="utf-8"))
            source = str(sample.get("source_archive") or "")
            if source and not (ARCHIVES / source).exists():
                orphans.append(p.name)
                continue
            # 期望值按**样本自己的面**取：没记 tool_surface 的（2026-10-20
            # 之前建的）算默认面——那批样本真实跑出来的就是它。
            surface = str(sample.get("tool_surface") or "") or toolcatalog.DEFAULT_SURFACE
            expected = {d.name for d in toolcatalog.defs_of(surface)}
            names = [str(t.get("name")) for t in (sample.get("six") or {}).get("tools") or ()]
            assert set(names) == expected, (
                f"{p.name}（{surface} 面）的 six.tools 与该面的 catalog 对不上——"
                f"跑 cli build --overwrite 重建"
            )
        if orphans:
            warnings.warn(
                f"有 {len(orphans)} 条 P2 样本的源存档已被回滚删除，"
                f"它们是孤儿（重建救不回来，只能清掉）：{orphans[:5]}",
                stacklevel=1,
            )


# ── 5 · 模块图：catalog 是跨层地基 ─────────────────────────────────


class Test工具面:
    """**方言轴**的机械断言（2026-10-20 方案 15 §7 R1）。

    工具面是「这批存档是哪个执行层跑的」的投影。两个面**同名不同参**
    （``click`` 的 ``{tag,label}`` vs ``{role,name}``），所以面错了就是
    声明与调用对不上——而那在报告上看不出任何异常。
    """

    def test_驱动实现名与工具面一一对应(self):
        """**每个驱动实现的 ``name`` 都要有对应的工具面**。

        新加一个驱动实现却没在 catalog 里登记 ⇒ 那批存档的工具面
        「认不出」→ 组装期抛错。**认不出就报错**是对的（静默回落会让
        「配错了」与「配对了」产出同一份工具声明），但那意味着新驱动
        上线前必须先在这里登记——这条断言就是那个登记口。
        """
        for path in sorted((PKG.parent / "executor" / "browser").glob("*_driver.py")):
            src = path.read_text(encoding="utf-8")
            names = re.findall(r'^\s{4}name\s*=\s*"([^"]+)"', src, re.M)
            for name in names:
                assert name in toolcatalog.DRIVER_SURFACE, (
                    f"{path.name} 的驱动实现 {name!r} 没在 toolcatalog.DRIVER_SURFACE "
                    f"里登记——那批存档的工具面认不出，组装期会抛"
                )

    def test_没记驱动按默认面(self):
        """空串 = **没记**，不是「未知方言」。

        历史批次没有 ``driver`` 字段，而那批真实跑出来的就是默认面——
        按默认面渲染是**如实**的，不是兜底。
        """
        assert toolcatalog.surface_of_driver("") == toolcatalog.DEFAULT_SURFACE
        assert toolcatalog.surface_of_driver(None) == toolcatalog.DEFAULT_SURFACE
        assert toolcatalog.surface_of_driver("  ") == toolcatalog.DEFAULT_SURFACE

    def test_认不出的驱动报错不回落(self):
        """静默回落到默认面 = 「配错了」与「配对了」产出同一份声明。

        而「配错了」那批在报告上看不出任何异常——它只是**训出了一个
        按错编码点按钮的模型**。
        """
        with pytest.raises(ValueError):
            toolcatalog.surface_of_driver("playwright-never-heard-of-it")

    def test_目标编码的工具必须点名本面(self):
        """**正向断言**（方案 04 §5.1 沿用到 camofox 面）。

        目标编码从 ``{tag,label}`` 变成 ``{role,name}`` 之后，模型得知道
        「这个 name 怎么用」——不点名**本面的机制**，有人「顺手」把措辞
        改回抽象说法时测试不会红，而数据会静默地退回到「脱离本项目
        不成立」的状态。

        ⚠️ **逐面点名、各自点名自己**：camofox 面（现行生产面）必须
        点名 ``camofox``；camoufox 面（只读存量面）必须点名
        ``camoufox`` / ``get_by_role``。在 camofox 面里点名 camoufox
        （或反过来）⇒ 模型被教了一个**它这批跑不出来**的机制。
        """
        for surface, must in (
            (toolcatalog.SURFACE_CAMOFOX, ("camofox",)),
            (toolcatalog.SURFACE_CAMOUFOX, ("camoufox", "get_by_role")),
        ):
            target_bearing = [
                d for d in toolcatalog.visible_defs(surface)
                if d.param("role") is not None
            ]
            assert target_bearing, f"{surface} 面没有带 role 参数的工具——目标编码没接上"
            for d in target_bearing:
                blob = (d.instruction + " " + " ".join(p.desc for p in d.params)).lower()
                assert any(w in blob for w in must), (
                    f"{surface} 面的 {d.name} 的说明没点名 {must}——"
                    f"模型学不会「匹配到多个元素时怎么办」"
                )

    def test_两个面的同名工具参数不同(self):
        """``click`` 在 obscura 面与 aria 面里**必须**是两条不同的声明。

        有人图省事把两边并成一条（比如按 ``tag``/``label`` 与 ``role``/
        ``name`` 都列上）时，模型会学到「有时按标签点、有时按角色点」——
        那是**两种任务**的混合，而它在报告上看不出异常。
        """
        obsc = toolcatalog.by_name("click", toolcatalog.SURFACE_OBSCURA)
        camo = toolcatalog.by_name("click", toolcatalog.SURFACE_CAMOUFOX)
        assert obsc is not None and camo is not None
        assert {p.name for p in obsc.params} != {p.name for p in camo.params}
        assert "role" not in obsc.params and "tag" not in camo.params

    def test_两张aria面的同名工具说明不同(self):
        """**同一张方言、两种语义 ⇒ 说明必须分开**（2026-10-26）。

        camoufox 面与 camofox 面的 ``click`` **参数完全相同**
        （都是 ``role``/``name``）——但**定位语义不同**：

        - camoufox 面：``get_by_role(role, name=name, exact=True)``——精确；
        - camofox 面：上游是 ``getByRole(role, {name}).nth(nth)``——
          **没有 exact**，名字走大小写不敏感的**子串匹配**。

        ⇒ 参数相同、说明必须不同。若 camofox 面照抄 camoufox 面的
        ``exact=True``，就是教模型一个**引擎不保证**的性质——
        「名字是别人的子串」时它会点错，而报告上看不出异常。
        """
        camo = toolcatalog.by_name("click", toolcatalog.SURFACE_CAMOUFOX)
        camf = toolcatalog.by_name("click", toolcatalog.SURFACE_CAMOFOX)
        assert camo is not None and camf is not None
        assert {p.name for p in camo.params} == {p.name for p in camf.params}
        assert camo.instruction != camf.instruction, (
            "两张 aria 面的 click 说明一样——其中一个面的定位语义被写错了"
        )
        assert "exact" in camo.instruction.lower()
        assert "exact" not in camf.instruction.lower(), (
            "camofox 面写了 exact=True——上游没有 exact，是子串匹配"
        )

    def test_读原语只在aria面(self):
        """pull 模式才有「读」这个动作（方案 15 §5.4）。

        obscura 面是 push 模式：观察由执行器推给模型，模型没有读原语。
        把它加进 obscura 面 = 告诉模型一个它那批**调用不了**的工具，
        训出来的 ``tool_calls`` 会被运行时拒掉。

        两张 aria 面（camoufox / camofox）**共用同一份**读原语声明
        ——一个词表，不造第二套。
        """
        for d in toolcatalog.defs_of(toolcatalog.SURFACE_OBSCURA):
            assert d.family != "collection", (
                f"obscura 面混进了读原语 {d.name}——那是 pull 模式的东西"
            )
        for surface in (toolcatalog.SURFACE_CAMOUFOX, toolcatalog.SURFACE_CAMOFOX):
            cams = [d.name for d in toolcatalog.defs_of(surface)
                    if d.family == "collection"]
            assert cams == ["snapshot", "interactive", "links"], (
                f"{surface} 面的读原语与驱动原语对不上: {cams}"
            )

    def test_基础设施动作在两个面里都不可见(self):
        """``new_tab`` / ``close_tab`` 是执行器自己的动作。

        它们留在 P2 的动作空间里（删了 P1 无法回放），但**两个面**都
        不许进模型可见集。
        """
        for surface in SURFACES:
            visible = set(toolcatalog.visible_names(surface))
            for infra in toolcatalog.infra_names(surface):
                assert infra not in visible, (
                    f"{surface} 面把 {infra} 放进了模型可见集"
                )


class Test模块图:
    def test_不import任何功能模块(self):
        """catalog 挂在 ``trajectory_pipeline`` 顶层（不是 ``assembler/``
        下），因为 P3 与 T 互不 import（``test_t_contract.py::Test模块图``），
        工具声明得有个**两边都能用**的地方。

        而它自己**不能** import 任何功能模块——否则地基变成依赖源。
        """
        src = PKG / "__init__.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith("trajectory_pipeline.executor"), (
                    f"catalog import 了 {name}——与执行端绑死"
                )
                assert not name.startswith("trajectory_pipeline.assembler"), (
                    f"catalog import 了 {name}——与组装端绑死"
                )

    def test_不读仓库根output(self):
        """产物隔离（D8）。"""
        for p in sorted(PKG.glob("*.py")):
            text = p.read_text(encoding="utf-8")
            assert '"output/' not in text and "'output/" not in text, (
                f"{p.name} 里出现了根 output/ 字面量"
            )
