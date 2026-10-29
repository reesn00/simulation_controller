"""分歧卡的**现场读数**（方案 16 C-06 配套 · 读现场那一半）。

为什么需要它
------------
``worksheet.jsonl`` 给的是「结论 + 一句话证据」，**不是现场**。D-10 的判据原文
是「对照黄金集**预存档**的站点核验结论」——预存档不在表里。工作表上那句
``存在 <video>/<audio> 标签 ×6`` 是**存档被解读后的产物**，不是存档本身。

而分支标签的**语义**也不在表里：``success`` / ``not_play_site`` /
``component_unverified`` 这些词是在 rubric 的 **D-4（组件检测）** 与
**D-5（决策树）** 里定义的，代码里只有名字。判「代码这一步该不该这么判」，
必须同时看得见：**现场**（页面长什么样）+ **条款**（尺子怎么写的）。

所以本模块做两件事，都只读：

1. **把冻结行反查回存档**——复用 builder 的同一条路径
   （:func:`assembler.schema.split_archive` + 按 url 匹配站点样本），
   **不自己猜文件路径**：两份查找逻辑必然漂，而漂了不会报错——只会读到
   别的版本的观察，然后基于错的现场做出对的结论。
2. **取 rubric 条款原文**——条款语义在 markdown 里。**不抄一份到代码里**
   （CLAUDE.md 纪律 6 的老账：真源两份必然漂）。

fail-closed
-----------
定位不到现场时**说定位不到**，并说清为什么（存档不存在 / 切不开 /
outcome 缺失 / url 对不上，并列出候选），**不返回空读数**——空读数会被
读成「页面里没有 video 标签」，那是把「没找到」当成「看到了没有」。

分诊档本模块**一行都不写**：``bucket`` 的唯一写入口仍是
:func:`trajectory_pipeline.evaluation.calibration.triage.validate_rows`。
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Mapping, Sequence

from trajectory_pipeline.evaluation.golden.builder import PIPELINE_ROOT
from trajectory_pipeline.evaluation.scorers.rubric_version import RUBRIC_DOC

#: 条款块的起始行：``**D-10 结论正确性 ★** | 类别：决策正确 | …``
_CLAUSE_HEAD_RE = re.compile(r"^\*\*(?P<id>[A-Z]-\d+(?:\.\d+)?)(?:\s|\*\*|★)")

#: 条款块的**终止**：下一条款头、markdown 标题、或 ``---`` 分隔线，
#: **谁先到谁算**。不截断的话，D-10 那块会把「## 批次级断言表」的引言
#: 一并吞进去——人判尺子时读到半段别人的话，比读不到更糟。
_BLOCK_STOP_RE = re.compile(r"^(?:#{1,6}\s|---\s*$)")

#: 分诊时**默认附带**的条款：卡自己的条款 + 分支语义的两条。
#:
#: 为什么是这两条：D-10 只说「结论与 ground truth 一致」（比**什么**），
#: 而「该给哪个分支标签」写在 D-5 的决策树里、「什么算播放组件」写在 D-4
#: 里。**只给 D-10，人会拿不到尺子**——这不是漏，是尺子分两处写着。
DEFAULT_EXTRA_CLAUSES: tuple[str, ...] = ("D-4", "D-5")

#: 正文预览的默认字符数。与 07 号手册里现场取证的 ``--preview 400`` 同口径。
DEFAULT_PREVIEW_CHARS = 400


# --------------------------------------------------------------------------
# 条款原文
# --------------------------------------------------------------------------


def parse_clauses(text: str) -> dict[str, str]:
    """rubric markdown → ``{条款号: 条款块原文}``。

    条款块从 ``**D-10 …**`` 那行起，到**下一个条款头 / markdown 标题 /
    ``---`` 分隔线**（谁先到谁算）止。**解析不出某条款就查不到那一键**
    （调用方要如实印「条款原文不可用」，不许静默跳过——人判尺子时少一条
    条款，结论会偏向「没歧义」）。
    """
    lines = text.splitlines()
    heads: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _CLAUSE_HEAD_RE.match(line)
        if m:
            heads.append((i, m.group("id")))
    out: dict[str, str] = {}
    for idx, (start, clause_id) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        block: list[str] = []
        for line in lines[start:end]:
            if _BLOCK_STOP_RE.match(line):
                break
            block.append(line)
        while block and not block[-1].strip():
            block.pop()
        out[clause_id] = "\n".join(block).rstrip()
    return out


def load_clauses(doc_path: pathlib.Path | str | None = None) -> dict[str, str]:
    """读 rubric 文档并解析条款。文档读不到 → **抛**（不返回空表）。

    返回空表等于「尺子上没有条款」——那会让每张卡都「判不了」，
    而真实原因只是路径错了。
    """
    path = pathlib.Path(doc_path) if doc_path else RUBRIC_DOC
    if not path.exists():
        raise FileNotFoundError(f"rubric 文档不存在：{path}")
    clauses = parse_clauses(path.read_text(encoding="utf-8"))
    if not clauses:
        raise ValueError(f"rubric 文档里没解析出任何条款：{path}")
    return clauses


def clause_text(clause_id: str, clauses: Mapping[str, str]) -> str:
    """取条款原文；**查不到返回空串**（调用方负责印出「不可用」标记）。

    这里返回空串而不是抛：一次 `show` 要印三条条款，其中一条查不到时，
    把另外两条也一起丢掉是更大的损失。但**空串必须被显式印成「不可用」**，
    绝不能当成「这条条款没有内容」。
    """
    return clauses.get(clause_id, "")


# --------------------------------------------------------------------------
# 现场定位
# --------------------------------------------------------------------------


def locate_sample(
    row: Mapping[str, Any], *, p2_root: pathlib.Path | str | None = None
) -> dict[str, Any]:
    """把冻结行反查成「存档里的那条样本」。

    返回 dict，键：

    - ``kind``     —— 冻结行的 ``source_kind``；
    - ``path``     —— 定位到的文件（找不到时为空串）；
    - ``sample``   —— 样本 dict（``Sample.to_json()`` 的形态）；找不到为 ``None``；
    - ``unit``     —— 单元名（``site-3`` 之类）；
    - ``note``     —— **人话说明**（定位到了也要说「怎么定位到的」）；
    - ``candidates`` —— 存档里现有的单元与 url（对不上时列出来）。

    三种 ``source_kind`` 分别处理：``archive_outcome`` 切 P1 存档按 url 匹配；
    ``p2_sample`` 直接读那份样本 JSON；``negative_pool`` 只有 outcome 行，
    **没有可切的存档** ⇒ 读不到现场（不是「现场为空」）。
    """
    root = pathlib.Path(p2_root) if p2_root else PIPELINE_ROOT
    kind = str(row.get("source_kind") or "")
    task_id = str(row.get("task_id") or "")
    run_id = str(row.get("run_id") or "")
    url = str(row.get("url") or "")

    if kind == "p2_sample":
        sample_id = str(row.get("sample_id") or "")
        path = root / "samples" / f"{sample_id}.json"
        if not path.exists():
            return {
                "kind": kind, "path": str(path), "sample": None, "unit": "",
                "note": f"P2 样本文件不存在：{path}", "candidates": [],
            }
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "kind": kind, "path": str(path), "sample": data,
            "unit": str(data.get("unit") or ""),
            "note": f"P2 样本 {data.get('sample_id')}",
            "candidates": [],
        }

    if kind == "negative_pool":
        return {
            "kind": kind, "path": "", "sample": None, "unit": "",
            "note": "负样本池行：只有 outcome 行，没有可切的存档 ⇒ **读不到现场**",
            "candidates": [],
        }

    if kind != "archive_outcome":
        return {
            "kind": kind, "path": "", "sample": None, "unit": "",
            "note": f"source_kind={kind!r} 不认识（不猜）", "candidates": [],
        }

    if not (task_id and run_id):
        return {
            "kind": kind, "path": "", "sample": None, "unit": "",
            "note": "冻结行缺 task_id / run_id ⇒ 无法定位存档", "candidates": [],
        }

    path = root / f"{task_id}__{run_id}.json"
    if not path.exists():
        return {
            "kind": kind, "path": str(path), "sample": None, "unit": "",
            "note": f"P1 存档不存在：{path}", "candidates": [],
        }

    from trajectory_pipeline.assembler.schema import split_archive

    try:
        samples = split_archive(path)
    except Exception as exc:  # noqa: BLE001 —— 切不开要说切不开
        return {
            "kind": kind, "path": str(path), "sample": None, "unit": "",
            "note": f"存档切不开（{type(exc).__name__}: {exc}）", "candidates": [],
        }

    # 按 url 匹配站点样本：先 outcome.url，再退回观察里的 url。
    # 两级都试是因为「outcome 缺失」本身是降级标记（outcome_missing），
    # 那时候页面**看过**，只是没记分支结论——现场还在。
    for s in samples:
        if s.outcome is not None and s.outcome.url == url:
            return {
                "kind": kind, "path": str(path), "sample": s.to_json(),
                "unit": s.unit, "note": f"按 outcome.url 匹配到 unit={s.unit}",
                "candidates": [],
            }
    for s in samples:
        for obs in s.observations:
            if str(obs.get("url") or "") == url:
                return {
                    "kind": kind, "path": str(path), "sample": s.to_json(),
                    "unit": s.unit,
                    "note": (
                        f"按观察里的 url 匹配到 unit={s.unit}"
                        "（该单元 outcome 缺失 ⇒ 分支结论要另找来源）"
                    ),
                    "candidates": [],
                }

    return {
        "kind": kind, "path": str(path), "sample": None, "unit": "",
        "note": "存档里没有匹配该 url 的站点样本",
        "candidates": [
            f"{s.unit}: " + (s.outcome.url if s.outcome is not None else "(无 outcome)")
            for s in samples
        ],
    }


# --------------------------------------------------------------------------
# 读数
# --------------------------------------------------------------------------


def observation_digest(
    obs: Mapping[str, Any], *, preview_chars: int = DEFAULT_PREVIEW_CHARS
) -> dict[str, Any]:
    """一条观察的读数（**只报事实**，不判断「页面是不是播放站」）。

    字段形状有两种，两边都读：**P1 存档**用 ``body_len`` / ``elements_total``
    （真值源），**P2 样本**用 ``body_chars`` / ``interactive_elements``（裁剪
    过的训练视图）。只认一种形状 ⇒ 换了个来源就读成空值，而空值会被读成
    「页面里没有 video 标签」。
    """
    body = str(obs.get("body_text") or "")
    elements = obs.get("interactive_elements")
    truncated = obs.get("truncated")
    body_chars = obs.get("body_chars")
    if body_chars is None:
        body_chars = obs.get("body_len")
    if body_chars is None and body:
        body_chars = len(body)
    return {
        "url": str(obs.get("url") or ""),
        "page_title": str(obs.get("page_title") or ""),
        "video_tag_count": obs.get("video_tag_count"),
        "iframe_count": obs.get("iframe_count"),
        "elements_listed": len(elements) if isinstance(elements, list) else None,
        "elements_total": obs.get("elements_total"),
        "links_total": obs.get("links_total"),
        "body_chars": body_chars,
        "raw_len": obs.get("raw_len"),
        "body_source": str(obs.get("body_source") or ""),
        "truncated": dict(truncated) if isinstance(truncated, Mapping) else {},
        "degraded": [str(d) for d in (obs.get("degraded") or [])],
        "body_preview": body[:preview_chars].replace("\n", " | "),
    }


def _archive_and_steps(
    row: Mapping[str, Any],
    *,
    p2_root: pathlib.Path | str | None = None,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """冻结行 → ``(存档读数, 每步读数, 样本 dict)``。

    **配对逻辑只有这一份。** 「动作与观察逐步配对」是这段代码里最容易漂的
    地方（两份查找逻辑必然漂，而漂了不会报错——只会读到别的版本的观察）。
    分歧卡（:func:`card_digest`）与行级读数（:func:`row_digest`）都走它。
    """
    located = locate_sample(row, p2_root=p2_root)
    sample = located.get("sample") or {}
    six = sample.get("six") if isinstance(sample, Mapping) else {}
    six = six if isinstance(six, Mapping) else {}
    actions = list(six.get("actions") or [])
    observations = list(six.get("observations") or [])

    steps: list[dict[str, Any]] = []
    for i, obs in enumerate(observations):
        action = actions[i] if i < len(actions) else None
        steps.append({
            "index": i + 1,
            "action": dict(action) if isinstance(action, Mapping) else None,
            "observation": observation_digest(obs, preview_chars=preview_chars),
        })

    archive = {
        "kind": located.get("kind") or "",
        "path": located.get("path") or "",
        "unit": located.get("unit") or "",
        #: **现场有没有读到**。没有 ≠ 现场是空的（见模块 docstring），
        #: 所以它必须是一个显式的布尔，而不是「有没有 sample 键」。
        "found": located.get("sample") is not None,
        "note": located.get("note") or "",
        "candidates": list(located.get("candidates") or []),
        "degraded_from": list(
            (sample.get("degraded_from") or []) if isinstance(sample, Mapping) else []
        ),
    }
    return archive, steps, sample


def row_digest(
    row: Mapping[str, Any],
    *,
    p2_root: pathlib.Path | str | None = None,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
) -> dict[str, Any]:
    """**行级**读数（不依赖分歧卡也要现场时用）。

    与 :func:`card_digest` 的差别只在**轴**：卡以「分歧」为轴（一个
    ``golden_id`` 可能出多张卡），行以「这一条 URL 判成什么」为轴。
    现场读数本身共用 :func:`_archive_and_steps`——**不抄第二份**。

    键：``golden_id`` / ``site`` / ``archive`` / ``steps``。
    ``site.title`` 是存档里的任务标题（不是页面标题；页面标题在每步观察里）。
    """
    archive, steps, sample = _archive_and_steps(
        row, p2_root=p2_root, preview_chars=preview_chars
    )
    return {
        "golden_id": str(row.get("golden_id") or ""),
        "site": {
            "url": str(row.get("url") or ""),
            "site_url": str(row.get("site_url") or ""),
            "play_page_url": str(row.get("play_page_url") or ""),
            "title": str(sample.get("title") or "") if isinstance(sample, Mapping) else "",
        },
        "archive": archive,
        "steps": steps,
    }


def card_digest(
    card: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    p2_root: pathlib.Path | str | None = None,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
) -> dict[str, Any]:
    """一张分歧卡的**完整读数**（冻结行 + 现场 + 定位说明）。

    ``card`` 与 ``row`` 分开传：卡来自 worklist（派生），冻结行来自黄金集
    （真值）。两者按 ``golden_id`` 配对——配不上是**版本错配**，不是「没数据」。
    """
    code = row.get("code") if isinstance(row.get("code"), Mapping) else {}
    golden = row.get("golden") if isinstance(row.get("golden"), Mapping) else {}
    archive, steps, sample = _archive_and_steps(
        row, p2_root=p2_root, preview_chars=preview_chars
    )

    return {
        "golden_id": str(row.get("golden_id") or card.get("golden_id") or ""),
        "dimension": str(card.get("dimension") or ""),
        "rubric_ref": str(card.get("rubric_ref") or ""),
        "delta_kind": card.get("delta_kind"),
        "gold_value": card.get("gold_value"),
        "pred_value": card.get("pred_value"),
        "pred_evidence": card.get("pred_evidence") or "",
        "suggested_bucket": card.get("suggested_bucket"),
        "suggested_reason": card.get("suggested_reason") or "",
        "needs_human_reason": card.get("needs_human_reason") or "",
        "facts": dict(card.get("facts") or {}),
        "gold": {
            "label": golden.get("label"),
            "label_label": golden.get("label_label") or "",
            "reviewed_by": golden.get("reviewed_by") or "",
            "reviewed_at": golden.get("reviewed_at") or "",
            "note": golden.get("note") or "",
        },
        "code": {
            "branch": code.get("branch"),
            "branch_label": code.get("branch_label") or "",
            "question": code.get("question") or "",
            "source": code.get("source") or "",
            "reached_play_page": code.get("reached_play_page"),
            "fallback_used": code.get("fallback_used"),
            "evidence": code.get("evidence") or "",
        },
        "site": {
            "url": str(row.get("url") or ""),
            "site_url": str(row.get("site_url") or ""),
            "play_page_url": str(row.get("play_page_url") or ""),
            "title": str(sample.get("title") or "") if isinstance(sample, Mapping) else "",
        },
        "archive": archive,
        "steps": steps,
    }


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

_RULE = "─" * 64


def _short(text: str, width: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _obs_line(obs: Mapping[str, Any]) -> str:
    """一条观察的**一行**读数（人扫一眼就能比）。"""
    bits = [
        f"video={obs.get('video_tag_count')}",
        f"iframe={obs.get('iframe_count')}",
    ]
    listed = obs.get("elements_listed")
    total = obs.get("elements_total")
    if listed is not None:
        bits.append(f"元素={listed}" + (f"/{total}" if total is not None else ""))
    elif total is not None:
        bits.append(f"元素={total}")
    bits.append(f"正文={obs.get('body_chars')}字")
    src = obs.get("body_source") or ""
    if src:
        bits.append(f"body={src}")
    tr = obs.get("truncated") or {}
    if tr:
        flags = ",".join(f"{k}" for k, v in tr.items() if v)
        if flags:
            bits.append(f"截断={flags}")
    degraded = obs.get("degraded") or []
    if degraded:
        bits.append(f"⚠降级={'/'.join(degraded)}")
    return " ".join(bits)


# --------------------------------------------------------------------------
# 现场摘要（工作表那一栏）
# --------------------------------------------------------------------------


def scene_summary(digest: Mapping[str, Any]) -> dict[str, Any]:
    """一张卡的**现场摘要**——给工作表的 ``scene`` 栏用。

    为什么要有这一栏：工作表原本给的是「结论 + 一句话证据」，**不是现场**。
    判读的人打开表看不到「这个页面到底长什么样」，于是「该归哪个桶」这件事
    答不了——**这不是他不想判，是表里没有可判的东西**。把现场塞进同一行，
    就不用为了看一条现场去开第二个文件、跑第二条命令。

    键：

    - ``found``     —— 现场**读到了没有**。没有 ≠ 现场是空的（见模块 docstring）；
    - ``status``    —— ``located``（读到了）/ ``located_no_steps``（读到了存档
      但那一步没有观察）/ ``not_found``（找了，没找到）/ ``not_read``（压根没去
      读）。**四态而不是一个布尔**：``not_found`` 与 ``not_read`` 归到一起就
      分不清「存档里没有」与「没查」，而这两种的修法完全不同；
    - ``unit``      —— 匹配到的决策单元（``site-4`` 之类）；
    - ``note``      —— **怎么定位到的**（或为什么定位不到；``located_no_steps``
      时会说明**为什么那一步没有观察**）；
    - ``degraded``  —— 存档自报的降级标记（``steps`` / ``outcome_missing` …）；
    - ``title``     —— 页面标题（第一个有标题的观察）；
    - ``steps``     —— 每步一行（动作 + 该步观察的读数）；
    - ``body``      —— 结论所在那一步的正文预览；
    - ``body_from`` —— **这段正文来自第几步**（不标出来，人会默认是最后一步；
      而最后一步常常正是没读到正文的那一步）；
    - ``candidates``—— 定位不到时，存档里现有的单元与 url（人好判断是
      「没访问过」还是「url 对不上」）。

    仍是**只读上下文**：这一栏不进分诊档（``triage.strip_row`` 只留裁定字段），
    也不作数——桶由人填。
    """
    archive = digest.get("archive") or {}
    steps = digest.get("steps") or []

    lines: list[str] = []
    title = ""
    for step in steps:
        action = step.get("action") or {}
        obs = step.get("observation") or {}
        tool = str(action.get("tool") or "（无动作）")
        params = action.get("params") or {}
        url = str(params.get("url") or "")
        head = f"{step.get('index')}. {tool}"
        if url:
            head += f" {_short(url, 60)}"
        lines.append(f"{head} → {_obs_line(obs)}")
        if not title:
            title = str(obs.get("page_title") or "")

    # 正文：**从最后一步往回找**第一个有正文的。往回找而不只看最后一步，
    # 是因为最后一步常常是 new_tab/降级那步——而结论下的地方在更早的一页。
    # 代价是可能取到**别的页**的正文，所以必须标出来源步骤（body_from）。
    body = ""
    body_from = ""
    for step in reversed(steps):
        obs = step.get("observation") or {}
        preview = str(obs.get("body_preview") or "")
        if preview:
            body = preview
            body_from = f"第{step.get('index')}步（{(step.get('action') or {}).get('tool') or '无动作'}）"
            break
    if not body and steps:
        body_from = f"第{steps[-1].get('index')}步（无正文读数）"

    if archive.get("found"):
        status = "located" if steps else "located_no_steps"
    else:
        status = "not_found"

    note = str(archive.get("note") or "")
    degraded = list(archive.get("degraded_from") or [])
    if status == "located_no_steps":
        # 匹配到了存档，**那一步却没有观察**——这跟「没找到」是两回事，
        # 而且多半是存档自己声明的降级（`degraded_from`）。不说清楚，人会
        # 把「降级丢了观察」读成「这一步什么都没发生」。
        why = ("存档降级标记 " + "/".join(degraded)) if degraded else "存档里那一步没有观察"
        note = f"{note}；⚠ {why} ⇒ **现场读不到**（不是「页面是空的」）"

    return {
        # 键的顺序 = 读的顺序：人在 JSONL 的**一行**里从头往下扫，
        # 所以「这页长什么样」（title / steps）排在前面，长正文排最后。
        "found": bool(archive.get("found")),
        "status": status,
        "unit": str(archive.get("unit") or ""),
        "note": note,
        "degraded": degraded,
        "title": title,
        "steps": lines,
        "candidates": list(archive.get("candidates") or []),
        "body_from": body_from,
        "body": body,
    }


def clauses_markdown(clauses: Mapping[str, str], wanted: Sequence[str] | None = None) -> str:
    """把条款原文拼成**一份**可读的尺子（写进 ``v{N}/clauses.md``）。

    为什么单独一个文件而不是每行都塞一份：D-5 的决策树有三十来行，33 行工作表
    各带一份 ⇒ 同一个文件长 33 倍，而**尺子是同一把**。读一次就够，所以单独
    落一份，人把它开在编辑器旁边。

    条款原文**现读**（不抄第二份到代码里）——抄一份必然漂，而漂了不报错。
    """
    if wanted is None:
        wanted = list(DEFAULT_EXTRA_CLAUSES)
    ids = list(dict.fromkeys([str(w) for w in wanted if str(w)]))
    out = [
        "# 分诊用的尺子（rubric 条款原文）",
        "",
        "⚠️ **现读生成**：条款原文来自 `docs/设计方案/在线视频场景rubric.md`，"
        "**没有抄第二份**——条款改了重跑 `worklist build` 即可。",
        "",
        "看哪几条：D-10 判「结论与 ground truth 一致」（比**什么**）；"
        "「该给哪个分支标签」在 **D-5 决策树**；「什么算播放组件」在 **D-4**。",
        "",
    ]
    for cid in ids:
        text = clause_text(cid, clauses)
        out.append(f"## {cid}")
        out.append("")
        if not text:
            out.append(f"⚠️ 条款 {cid} 原文不可用（rubric 文档里没有这一条）")
            out.append("")
            continue
        out.append(text)
        out.append("")
    return "\n".join(out)


def render_card(digest: Mapping[str, Any], clauses: Mapping[str, str]) -> str:
    """一张卡的完整读数（人读的形态）。"""
    lines: list[str] = []
    add = lines.append

    add(_RULE)
    add(
        f"卡 {digest.get('golden_id')} · {digest.get('dimension')}"
        f" · 条款 {digest.get('rubric_ref')}"
    )
    add(_RULE)

    code = digest.get("code") or {}
    gold = digest.get("gold") or {}
    site = digest.get("site") or {}
    archive = digest.get("archive") or {}

    branch = code.get("branch")
    branch_text = "成功（branch=null ⇒ 记成功）" if branch is None else str(branch)
    add(
        f"【两侧】gold={digest.get('gold_value')}  pred={digest.get('pred_value')}"
        f"  delta={digest.get('delta_kind')}"
    )
    add(
        f"【金标】{gold.get('label')}"
        + (f"（{gold.get('label_label')}）" if gold.get("label_label") else "")
        + f" · 审于 {gold.get('reviewed_at')} by {gold.get('reviewed_by')}"
    )
    if gold.get("note"):
        add(f"       金标备注 {gold['note']}")
    add(
        f"【判据侧】branch={branch_text} · question={code.get('question')}"
        f" · source={code.get('source')}"
    )
    add(
        f"         reached_play_page={code.get('reached_play_page')}"
        f" · fallback_used={code.get('fallback_used')}"
    )
    if code.get("evidence"):
        add(f"         evidence：{code['evidence']}")

    suggested = digest.get("suggested_bucket")
    if suggested:
        add(f"【机器建议】{suggested} —— {digest.get('suggested_reason')}")
    else:
        add(f"【机器建议】无 —— {digest.get('needs_human_reason')}")

    add("")
    add("── 站点 " + _RULE[3:])
    add(f"URL    {site.get('url')}")
    if site.get("play_page_url"):
        add(f"播放页 {site.get('play_page_url')}")
    if archive.get("path"):
        add(f"存档   {archive['path']} · unit={archive.get('unit') or '—'}")
    add(f"定位   {archive.get('note')}")
    if archive.get("degraded_from"):
        add(f"       存档降级标记 {'/'.join(archive['degraded_from'])}")
    if archive.get("candidates"):
        add("       存档里的单元：" + " · ".join(archive["candidates"]))
    if site.get("title"):
        add(f"任务   {_short(str(site['title']), 60)}")
    first_title = ""
    for step in digest.get("steps") or []:
        t = str((step.get("observation") or {}).get("page_title") or "")
        if t:
            first_title = t
            break
    if first_title:
        add(f"页面   {_short(first_title, 90)}")

    steps = digest.get("steps") or []
    add("")
    if steps:
        add(f"── 动作与观察（{len(steps)} 步）" + _RULE[10:])
    else:
        add("── 动作与观察 " + _RULE[8:])
        add("  ⚠ 没有读到现场：读数缺失，别把「没找到」当成「页面里没有」")
    last_title = ""
    for step in steps:
        action = step.get("action") or {}
        obs = step.get("observation") or {}
        params = action.get("params") or {}
        url = str(params.get("url") or "")
        target = action.get("target")
        head = f" {step.get('index'):>2}. {action.get('tool') or '（无动作）'}"
        if url:
            head += f"  {_short(url, 70)}"
        if target:
            head += f"  target={json.dumps(target, ensure_ascii=False)}"
        head += f"  origin={action.get('origin') or '—'}"
        add(head)
        add(f"      {_obs_line(obs)}")
        title = str(obs.get("page_title") or "")
        if title and title != last_title:
            add(f"      标题 {_short(title, 80)}")
            last_title = title
        elif title:
            add("      标题（同前）")
        preview = str(obs.get("body_preview") or "")
        if preview:
            add(f"      正文 {_short(preview, 380)}")
        elif not obs.get("degraded"):
            add("      正文 （空）")

    wanted = list(dict.fromkeys(
        [str(digest.get("rubric_ref") or "")] + list(DEFAULT_EXTRA_CLAUSES)
    ))
    add("")
    add("── 条款原文 " + _RULE[8:])
    for cid in wanted:
        if not cid:
            continue
        text = clause_text(cid, clauses)
        if not text:
            add(f"⚠ 条款 {cid} 原文不可用（rubric 文档里没有这一条）")
            continue
        for raw in text.splitlines():
            add(f"  {raw}" if raw.strip() else "")

    return "\n".join(lines)


def render_compact(digest: Mapping[str, Any], *, index: int = 0, total: int = 0) -> str:
    """一张卡的**紧凑读数**（``--all`` 用）：三到四行，人扫。"""
    site = digest.get("site") or {}
    steps = digest.get("steps") or []
    obs_list = [s.get("observation") or {} for s in steps]
    videos = [o.get("video_tag_count") for o in obs_list]
    iframes = [o.get("iframe_count") for o in obs_list]
    videos = [v for v in videos if isinstance(v, int)]
    iframes = [v for v in iframes if isinstance(v, int)]
    title = ""
    for o in obs_list:
        if o.get("page_title"):
            title = str(o["page_title"])
            break

    head = (
        f"[{index}/{total}] {digest.get('golden_id')}  {digest.get('dimension')}"
        f"  gold={digest.get('gold_value')} ← pred={digest.get('pred_value')}"
    )
    detail = (
        f"       {site.get('url')}"
        + (f"  「{_short(title, 30)}」" if title else "")
    )
    counts = ""
    if videos:
        counts += f"video={videos[0]}"
        if len(videos) > 1 and videos[-1] != videos[0]:
            counts += f"→{videos[-1]}"
        counts += "  "
    if iframes:
        counts += f"iframe={iframes[0]}"
        if len(iframes) > 1 and iframes[-1] != iframes[0]:
            counts += f"→{iframes[-1]}"
    archive = digest.get("archive") or {}
    if archive.get("found"):
        counts += f"  unit={archive.get('unit') or '—'}"
    else:
        # 读不到现场：**说读不到**，并把存档里的候选列出来（人好判断是
        # 「没访问过」还是「url 对不上」）。空读数会被读成「页面是空的」。
        counts += f"  ⚠ {archive.get('note') or '现场未读到'}"
        if archive.get("candidates"):
            counts += f"；存档里有：" + " · ".join(archive["candidates"][:4])
    evidence = str(digest.get("pred_evidence") or "")
    return "\n".join(
        [head, detail, f"       {counts}".rstrip(), f"       判据 {evidence}"]
    )


__all__ = [
    "DEFAULT_EXTRA_CLAUSES",
    "DEFAULT_PREVIEW_CHARS",
    "card_digest",
    "clause_text",
    "clauses_markdown",
    "load_clauses",
    "locate_sample",
    "observation_digest",
    "parse_clauses",
    "render_card",
    "render_compact",
    "row_digest",
    "scene_summary",
]
