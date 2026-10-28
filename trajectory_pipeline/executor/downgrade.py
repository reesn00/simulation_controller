"""按判据把**不成立的判定**降为 ``unresolved``——取证后的处置动作。

为什么这是一个模块而不是三个脚本
--------------------------------
2026-10-10 有三个一次性脚本做同一件事，判据各不相同：

* ``downgrade_ref_misses``：否定依据是「找不到可点击的控件 ref」或
  「0 个交互元素中无一命中播放控件词表」——**采集缺件**，不是业务结论；
* ``downgrade_false_success``：``branch=None``（判成功，``source=llm``）
  而现场实测播放器压根没渲染——**误判成功**；
* 第三个是回滚第二个的误伤（那次事故的教训见[避坑指南 §1.5](../docs/02-避坑指南.md)，
  那次的事故脚本本身**没有**沉成子命令——判据是「某次事故的痕迹」，
  沉成通用工具只会被下一个人拿去修别的批次）。

两个判据、一个动作。动作沉成模块，判据由调用方传进来——
因为判据是**那批数据的结论**，不是通用参数。

八条踩过的坑，每一条都写进了对应函数
--------------------------------------
1. ⚠️ **两份都改**（原档 + ``.reviewed.json``）——2026-10-28 改。
   原先只动原档，理由是「复核档是 ``review --apply`` 从原档合并出来的，
   在它上面改会被下一次 ``--apply`` 静默覆盖回去」。
   **那条理由只说对了一半**：复核档才是门禁与报表**实际读的那一份**
   （:func:`executor.archive.select_archives` **优先取复核档**）⇒
   只改原档 ⇒ 复核档还留着旧分支 ⇒ 门禁当场报 ``negative_not_pooled``，
   而人看到的却是「已经降过级了」——**改判与门禁对不上**。
   :mod:`executor.relabel` 早就是**两份都改**（见它的
   ``_load_archives`` docstring，那段话就是在躲这个坑）。

   **两份都改 ⇒ 下次 ``review --apply`` 覆盖不回来**（原档也降了），
   本坑要防的那个坑仍然被躲开。
   ⚠️ **但人工结论优先**：复核档上带 ``review_verdict`` /
   ``site_verdict`` 的记录**不受理**代码侧的降级——那是**人的裁定**，
   代码侧的「判定不成立」不许把它顶掉（见 :func:`_human_verdict`）。
   原档上**不**做这条豁免：原档记的是「代码原判 + 处置戳」，
   人工结论活在复核档那一层，两层各记各的（这正是既存的
   「原档 ``unresolved`` / 复核档 ``not_play_site``」那 33 处分层）。
2. **evidence 判据只看第一段**（:func:`base_evidence`）。
   ``"0 个交互元素" in "200 个交互元素"`` 是 **True**（子串），而每次降级
   追加的中文说明里**也会出现这几个字**——全文匹配会让自己刚降的记录
   再降一次。这个 bug 真发生过：第二版改用正则却仍全文匹配，
   把刚恢复的 5 条又降了级。
3. **负向前视**（:data:`ZERO_ELEMENTS`）。``200 个交互元素``
   不该被认成「0 个交互元素」。
4. **不动 ``reached_play_page``**（:func:`apply_downgrades`）。
   它记的是**采集事实**（控制流到没到过播放页 URL），不是结论。
   为了让 D-5 判 5 分而把它改成 False，等于**改写采集事实去迎合结论**。
5. **「成功」在存档里记在两处**（:func:`_sync_visit_success`）。
   ``outcome.branch is None`` 与 ``visits[i].success``。只改一处就留下
   ``success=True 但 branch=unresolved``，``check-archives`` 的
   ``success_branch_mismatch`` 正查这个。
6. **命中条件放宽 ≠ 判据放宽**（:func:`select_by_evidence`）。
   为求幂等把匹配条件从 ``branch is None`` 放宽成
   ``branch in (None, "unresolved")``，**多降级了 8 条本来就该是
   unresolved 的记录**，还翻了 21 个 ``visits[].success``，而跑完只报
   「降级 N 条」，读起来像干完了活。

   **这个坑在沉成子命令后又长出来一次**：选择器只在 ``branches`` 参数
   **显式给了** ``TARGET_BRANCH`` 时才拒绝，而 ``--evidence`` 走的是
   ``branches=None``——于是它把 9 条**已经 unresolved** 的记录整整齐齐
   选了出来（实测，判据就是 ``0 个交互元素``）。跑出来每行写着
   ``unresolved×1``，**一行就够看出是空转**。

   所以「已经 ``unresolved`` 的记录不受理」必须是**无条件**的，不该挂在
   调用方给不给分支过滤上：**目标态与过滤条件无关**，它就是处置动作本身
   的定义。筛选器在这里只负责**排除**，没有「包括进来」的余地。
7. **「成功」记在三处，不是两处**（:func:`apply_downgrades`）。
   坑 5 说 ``outcome.branch is None`` 与 ``visits[i].success``——漏了
   ``ledger.succeeded``（:func:`~trajectory_pipeline.executor.branches.
   SiteOutcomeRecorder.summary` 落盘时算好的**快照**）。
   降级一条**成功**（``branch is None``）时它不减 ⇒ 存档里
   「成功 3 条」而 ``outcomes`` 里只剩 2 条成功。

   为什么这次才暴露：本模块的三个原始判据全是**负样本**的判据
   （"没找到控件" / "0 个交互元素" / "误判成功"），而**误判成功**
   那一支（2026-10-10 的 ``downgrade_false_success``）恰恰降的是
   ``branch=None`` 的记录——它当时也没管 ``ledger.succeeded``，
   只是那批数据的报表没读这个字段，所以没显形。

   实测（2026-10-22）：camoufox 批次里 5 条「成功」被降级后，
   5 份存档的 ``ledger.succeeded`` 全部多算了 1 ⇒ 报表上的成功率
   会比实际高，而**门禁不查这一项**（:mod:`executor.integrity` 只查
   ``visits[].success`` 与 ``outcome.branch`` 是否自洽）。
   ⇒ 降级成功时必须同步减 ``ledger.succeeded``（连带
   ``ledger.negative_samples``，见 :func:`apply_downgrades`）。
8. ⚠️ **降级后别用 ``review --apply`` 去「同步」复核档**（2026-10-28 实测）。
   ``review --apply`` 是**从原档重新合并**复核档 ⇒ 复核档上**新口径的
   rationale 与 unit 级 ``reflection`` 整段丢失**：实测 78 份存档里
   **56 份**的复核档是**新口径**（``pre_action``，带 ``reflection`` 键），
   而原档是**旧口径**（``post_hoc``）——两者 rationale 正文**逐条不同**。
   而 P2/P3/T 样本**从复核档切**（:func:`executor.cli.cmd_build` 走
   ``select_archives``）⇒ 重新合并 = **训练样本的口径退回旧代**，
   且 ``cmd build`` 会报「混批：pre_action 与 post_hoc 同时存在」。

   ⇒ 同步复核档的**唯一**正确姿势是**两份都改**（坑 1），不是重新合并。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from collections.abc import Callable, Iterable
from typing import Any

from trajectory_pipeline.executor.archive import REVIEWED_SUFFIX, select_archives

#: 降级的目标分支。它**不在** ``branches.NON_SAMPLE_BRANCHES`` 里——
#: 「unresolved 不是负样本」是硬纪律，混进池里会污染它。
TARGET_BRANCH = "unresolved"


def base_evidence(evidence: str) -> str:
    """**只取原始判定正文**——去掉 ``[原判 X] `` 前缀与所有 ``\\n\\n``
    之后的追加段落。

    这是判据正确性的关键，见模块 docstring 坑 2：追加的说明里也会
    出现判据关键词（人写「依据是『0 个交互元素』」这类理由时很自然），
    全文匹配会把它当成判据。
    """
    text = evidence or ""
    if text.startswith("[原判 ") and "]" in text:
        text = text[text.find("]") + 1:]
    return text.split("\n\n")[0]


#: 「0 个交互元素」——负向前视保证 ``200 个交互元素`` 不被误认。
#:
#: ⚠️ 这是**判据的唯一实现**：选不中一条记录只看
#: ``ZERO_ELEMENTS.search(base_evidence(...))``。不要另写一个
#: ``zero_elements_hit`` 之类的便捷谓词——两份判据会漂移，而漂移的
#: 表现是「一处认了、另一处没认」，跑完只报一个总数。
#:
#: 这条判据本身按 fail-closed（I4）与避坑指南 §5.15：哨兵返回**无法
#: 区分**「页面没渲染完」与「页面确实没有控件」——判 False 就是把
#: **采集失败**写成业务负样本。
ZERO_ELEMENTS = re.compile(r"(?<!\d)0 个交互元素")


def _sync_visit_success(archive: dict[str, Any], urls: set[str]) -> int:
    """``visits[].success`` 必须与 outcome 一起翻（模块 docstring 坑 5）。"""
    n = 0
    for visit in archive.get("visits") or ():
        if not isinstance(visit, dict) or not visit.get("success"):
            continue
        keys = {str(visit.get("url") or ""), str(visit.get("landed_url") or "")}
        if keys & urls:
            visit["success"] = False
            visit["success_downgraded_by"] = "人工降级（cli downgrade）"
            n += 1
    return n


def apply_downgrades(archive: dict[str, Any], *,
                     hits: list[dict[str, Any]],
                     reason: str,
                     name: str = "",
                     dry_run: bool = True,
                     ) -> dict[str, Any]:
    """降级 **``archive`` 里已经挑好的**那些 outcome。返回改动摘要。

    ⚠️ **收的是 ``archive`` 对象而不是路径**——这是两个都真的咬过人的
    决定的合并。

    第一版收 ``path``，内部自己 ``json.loads`` 一份再写盘，而调用方
    传的 ``hits`` 来自**它自己读的那一份**：改的是 A 对象，写的是 B
    对象，**结果什么都没落盘，而返回值说「降级 N 条」**。

    ——静默失败，且它长得很像成功：清单打印得整整齐齐，下一次读存档
    却发现一条没变。这比报错坏得多，因为**没有任何东西提示要查**。

    所以 archive 对象由调用方持有并持有到底：筛选、处置、写盘全程在
    同一个对象上，「改的不是写的那个」这种事**结构上不可能发生**。

    ⚠️ **本函数只改内存里的 ``archive``，不落盘。** 落盘是
    :func:`apply_to_root` 的事（它读完、处置完再原子写）。

    这条要在函数名上就看出来，所以它不叫 ``downgrade_file`` 也不收
    ``path``：**收了路径就会有人传路径进来，然后以为它写过了**——
    而「以为写过了」是这整份代码里最贵的一类错（报告说降了、文件没变、
    下一次读一切照旧，没有任何东西提示要查）。

    ⚠️ **参数是 ``hits`` 而不是匹配条件**：筛选归调用方（它知道那批
    数据的具体判据），处置只归这里（它知道怎么把结论改对）。一份判据，
    两处都用——分家就会分叉，而分叉的表现是**处置范围静默扩大**。

    :param reason: **必填**，且会原样进 evidence。它是半年后唯一能说清
        「这条当初凭什么被降」的依据——空理由等于降了个无名氏。

    ``dry_run`` **默认 True**：降级改的是结论，且 ``purge-run`` 之外
    没有反向操作。先看清单再落盘。
    """
    if not reason.strip():
        raise ValueError("降级必须给 --reason：它是这条日后唯一的依据")
    if not hits:
        return {"archive": name, "downgraded": 0, "visits": 0}

    led = archive.get("ledger") or {}
    by = led.get("by_branch") or {}
    summary: dict[str, Any] = {
        "archive": name, "downgraded": len(hits),
        "branches": {}, "urls": [],
    }
    urls: set[str] = set()
    #: **实际在负样本池里**的那几条。``negative_samples`` 只能减这么多——
    #: 判成功的（``branch=None``，见模块 docstring 顶部）本来就**不在池里**，
    #: 按 ``len(hits)`` 减会把池子减成比实际小，而报告看不出是哪条减的。
    was_negative = 0
    #: **被降掉的「成功」条数**——``ledger.succeeded`` 是落盘时的**快照**
    #: （:func:`~trajectory_pipeline.executor.branches.SiteOutcomeRecorder.
    #: summary` 算好的），降级不改它就会多算（模块 docstring 坑 7）。
    was_success = 0
    for rec in hits:
        old = rec.get("branch")
        summary["branches"][str(old)] = summary["branches"].get(
            str(old), 0) + 1
        if old is not None and old != TARGET_BRANCH:
            was_negative += 1
        elif old is None:
            was_success += 1
        urls.add(str(rec.get("url") or ""))
        if dry_run:
            continue
        rec["branch_before_downgrade"] = old
        rec["branch"] = TARGET_BRANCH
        rec["evidence"] = (f"[原判 {old}] {rec.get('evidence') or ''}"
                           f"\n\n{reason}")
        # 负样本标记必须摘掉（模块 docstring 坑 4 的同一原则）：
        # 它是「这条是否进池」的判据，而 unresolved 不进池。
        rec.pop("is_negative_sample", None)
        by[str(old)] = max(0, by.get(str(old), 0) - 1)
        by[TARGET_BRANCH] = by.get(TARGET_BRANCH, 0) + 1

    if dry_run:
        summary["urls"] = sorted(u for u in urls if u)
        return summary

    summary["visits"] = _sync_visit_success(archive, urls)
    if led:
        led["by_branch"] = {k: v for k, v in by.items() if v}
        led["negative_samples"] = max(
            0, led.get("negative_samples", 0) - was_negative)
        if was_success:
            # ⚠️ **快照要跟着改**（模块 docstring 坑 7）：``succeeded`` 是
            # 落盘时算好的「成功条数」，降级把一条成功改成了 unresolved
            # ⇒ 成功条数少一条。不改的话报表上的成功率会**比实际高**，
            # 而门禁不查这一项（它只查 visits[].success ↔ branch）。
            led["succeeded"] = max(
                0, int(led.get("succeeded") or 0) - was_success)
        miss = led.setdefault("missing_branches", [])
        if TARGET_BRANCH not in miss:
            miss.append(TARGET_BRANCH)
        miss.sort()
        archive["ledger"] = led
    archive.setdefault("warnings", []).append(
        f"人工降级 {len(hits)} 条 outcome → {TARGET_BRANCH}：{reason[:80]}")
    return summary


def _atomic_write(path: pathlib.Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def original_archive_paths(root: pathlib.Path) -> list[pathlib.Path]:
    """本目录下**原档**的路径（复核档折回它的原档名）。

    「什么算一份存档」的口径**只有一处**：
    :func:`~trajectory_pipeline.executor.archive.select_archives`
    （``<task_id>__<run_id>.json`` 才算，同目录的探针取证不算）。这里
    只多一件事——**折回原档**：原档是「代码原判 + 处置戳」那一层，
    复核档是「人工结论」那一层（模块 docstring 坑 1）。

    ⚠️ 这个函数存在的原因是一个**真的发生在生产目录上的**漏扫：
    这里原先是 ``root.glob("T*__*.json")``，只认 ``T`` 前缀。实测
    （2026-10-26）生产根目录 78 份存档里 **28 份是 ``E``/``P`` 前缀**
    （``E005__e35b7455.json`` / ``P001__…``），双跑那批判是 ``DR01__``
    ——它们**结构性地降不掉**。而 CLI 那层什么都不说：它只会打印
    「没有匹配的 outcome —— 归档可能已被降过（幂等），或判据不匹配」
    ⇒ **判据被冤枉**，读的人会去改判据，改不好的。
    """
    out: list[pathlib.Path] = []
    for path, reviewed in select_archives(root):
        out.append(
            path.with_name(path.name[: -len(REVIEWED_SUFFIX)] + ".json")
            if reviewed else path
        )
    return out


def reviewed_archive_paths(root: pathlib.Path) -> list[pathlib.Path]:
    """本目录下**复核档**（``.reviewed.json``）的路径。

    ⚠️ **只收「有配对原档」的复核档**：``<run>.reviewed.json`` 这种
    死文件（对不上任何 ``trajectory_id``）**不是**复核档，是别的产物。
    """
    out: list[pathlib.Path] = []
    for path in sorted(root.glob(f"*{REVIEWED_SUFFIX}")):
        if "__" not in path.stem:
            continue                      # 死文件 / 探针取证
        base = path.name[: -len(REVIEWED_SUFFIX)]
        if not (root / f"{base}.json").exists():
            continue                      # 没有配对原档 ⇒ 不成对
        out.append(path)
    return out


#: 复核档上**记着人工结论**的字段。带这些的记录**不受理**代码侧降级。
#:
#: ⚠️ **只认「人工」这两个字段，不认 ``source == "human"``**：
#: ``source`` 也被 :mod:`executor.relabel` 写成 ``human``，而改判
#: （「标签错了但仍是负样本」）**不是**站点级结论——它仍可被降级出池。
#: ``review_verdict`` / ``site_verdict`` 才是**人的裁定**。
HUMAN_VERDICT_FIELDS: tuple[str, ...] = ("review_verdict", "site_verdict")


def _human_verdict(rec: dict[str, Any]) -> bool:
    """这条记录上有没有**人的裁定**？

    人的裁定**优先于**代码侧的「判定不成立」：代码能说「这条标签没依据」，
    不能说「人判的这条不是负样本」——后者是**另一层**的结论
    （模块 docstring 坑 1 末段）。
    """
    return any(str(rec.get(f) or "").strip() for f in HUMAN_VERDICT_FIELDS)



def apply_to_root(root: pathlib.Path, *,
                  select: Callable[[dict[str, Any]], list[dict[str, Any]]],
                  reason: str,
                  dry_run: bool = True,
                  ) -> list[dict[str, Any]]:
    """遍历存档目录并降级。

    :param select: ``存档 → 该降级的 outcome 列表``。**筛选判据全在这
        一个函数里**，且 :func:`apply_downgrades` 拿到的就是它筛完的
        ``hits``——所以「哪些该降」与「降了之后怎么改」**不可能分叉**。

        给它一个函数而不是一组参数，正是为了这个：各批数据的判据形态
        不同（URL 集合 / evidence 正则 / 成功与否），而**处置动作只有
        一份**，重复实现处置动作是当初三个脚本互相漂移的原因。

    ⚠️ **两层都改**（模块 docstring 坑 1）：原档 + 复核档。
    复核档是门禁与报表**实际读的那一份**（``select_archives`` 优先取它）
    ⇒ 只改原档 ⇒ 门禁看到的还是旧分支 ⇒ 当场报 ``negative_not_pooled``。
    复核档上**带人工裁定**的记录**不受理**（:func:`_human_verdict`）。

    路径分别由 :func:`original_archive_paths` 与
    :func:`reviewed_archive_paths` 给，别在这函数里再写一份文件名判据
    （第一份就漏了 ``E``/``P`` 前缀，见那个函数的说明）。
    """
    out: list[dict[str, Any]] = []
    for path, layer in ([(p, "原档") for p in original_archive_paths(root)]
                        + [(p, "复核档") for p in reviewed_archive_paths(root)]):
        try:
            archive = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        hits = select(archive)
        if layer == "复核档":
            # 人的裁定优先于代码侧的「判定不成立」。
            hits = [h for h in hits if not _human_verdict(h)]
        if not hits:
            continue
        # 同一个 archive 对象走完「筛选 → 处置 → 写盘」全程——
        # 见 apply_downgrades docstring 里那个「改的不是写的那个」的坑。
        summary = apply_downgrades(archive, hits=hits, reason=reason,
                                   name=path.name, dry_run=dry_run)
        if not summary["downgraded"]:
            continue
        summary["layer"] = layer
        if not dry_run:
            _atomic_write(path, archive)
        out.append(summary)
    return out


def select_by_url(*fragments: str,
                  branches: Iterable[str] | None = None,
                  ) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    """按 URL 片段挑 outcome。**多片段是 OR。**

    给「那批数据的具体 URL」用的选择器——判据在这批数据上是 URL，
    但**动作**（证据戳、ledger 记账、visits 同步）共用 :mod:`本模块`。

    ⚠️ 与 :func:`select_by_evidence` 同样**无条件排除 ``TARGET_BRANCH``**：
    「这批 URL 的判定不成立」里的「不成立」，本身就排除了已经
    ``unresolved`` 的那些——它们是结论，不是待处置的判定。

    :param branches: **限定分支**（``None`` = 不限）。

    ⚠️ **这个参数不是可有可无的**（2026-10-28 实测踩到）：CLI 的
    ``--branch`` 原本只走 ``--evidence`` 那条路，``--url`` 那条路**静默忽略**
    它 ⇒ ``downgrade --url X --branch unreachable_hard`` 会把 **X 上别的
    分支**（``not_play_site`` / ``login_required`` / …）**一起降级**。
    实测那批数据里一个 URL 挂着 **11 条 ``not_play_site`` + 4 条
    ``unreachable_hard``** ⇒ 不限分支就是**误伤 11 条真负样本**，
    而报告上只看得到「降级 15 条」。

    ⇒ 分支过滤必须**跟着 URL 走**：调用方给了 ``branches`` 就照办。
    """
    if not fragments:
        raise ValueError("至少要给一个 URL 片段")
    want = set(branches) if branches else None
    if want and TARGET_BRANCH in want:
        raise ValueError(
            f"branches 不该含 {TARGET_BRANCH!r}：它已经是降级后的状态。"
            "幂等判断看「有没有处置戳」，不靠 branch 值。")

    def _select(archive: dict[str, Any]) -> list[dict[str, Any]]:
        return [r for r in (archive.get("outcomes") or ())
                if isinstance(r, dict)
                and str(r.get("branch")) != TARGET_BRANCH
                and (want is None or str(r.get("branch")) in want)
                and any(f in str(r.get("url") or "") for f in fragments)
                and not str(r.get("evidence") or "").startswith("[原判 ")]
    return _select


def select_by_evidence(patterns: Iterable[re.Pattern[str]], *,
                       branches: Iterable[str] | None = None,
                       ) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    """按**原始判定正文**里是否命中某条正则来挑。

    ⚠️ 判据走 :func:`base_evidence`——**只看第一段**。全文匹配会命中
    降级理由本身（模块 docstring 坑 2），把刚降的记录再降一次。

    :param branches: 限定分支。传 ``TARGET_BRANCH`` 直接报错，别指望它
        被静默忽略——静默忽略会让人以为筛过了。

    ⚠️ **``TARGET_BRANCH`` 是无条件排除的**，不给 ``branches`` 也排除。
    「已经 unresolved 的记录不受理」是**处置动作的定义**，不是一条可选的
    过滤规则——把它挂在「调用方恰好给了什么参数」上，就是模块 docstring
    坑 6 换个入口再长一次。
    """
    pats = list(patterns)
    if not pats:
        raise ValueError("至少要给一条判据正则")
    want = set(branches) if branches else None
    if want and TARGET_BRANCH in want:
        raise ValueError(
            f"branches 不该含 {TARGET_BRANCH!r}：它已经是降级后的状态。"
            f"幂等判断看「有没有处置戳」，不靠 branch 值。")

    def _select(archive: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for r in (archive.get("outcomes") or ()):
            if not isinstance(r, dict):
                continue
            if str(r.get("branch")) == TARGET_BRANCH:
                continue
            if want is not None and str(r.get("branch")) not in want:
                continue
            if str(r.get("evidence") or "").startswith("[原判 "):
                continue
            head = base_evidence(str(r.get("evidence") or ""))
            if any(p.search(head) for p in pats):
                out.append(r)
        return out
    return _select