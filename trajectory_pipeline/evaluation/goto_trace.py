"""goto URL 溯源断言（方案 13 §6.2，A3-2）——**批次级**幻觉 goto 检查器。

scripted 下「goto 的 URL 与候选对应」是代码编排保证的恒真式，批次断言
只需比对；agent 下 goto 是 LLM 决定的，URL 可能来自候选链接、站内链接、
**也可能幻觉编造**——断言从「比对两个代码产物」变成「核查 LLM 输出的
可溯源性」。产出是**批次质量信号**，不进 22 分支封闭集：幻觉是模型的
病，不是站点的失败原因，样本级分支记账一字不动。

判定口径（§6.2 + 2026-10-17 幻觉记账决策 A）：

- 每个 ``origin="model"`` 的 goto 动作，其 ``params.url`` 必须 ∈
  **锚点集**——候选池 ∪ 该轨迹内已观察 ``links[].href``，锚点随
  轨迹递推：第 N 步的合法 goto 集包含前 N-1 步观察到的全部链接；
- 锚点的**时序是判定的一部分**：第 N 步观察到的链接救不了第 N-1 步
  的 goto——模型做决定时还没看见它；
- 相对 href 以**产生它的观察页 URL** 解析（urljoin）——「站内相对
  URL 解析结果」天然入锚，不需要特判；
- 归一容差三档，逐级放宽，放不过才记违规：
  1. 原串精确匹配锚点；
  2. (host 小写, 去尾斜杠 path, query) 归一匹配——容忍大小写/尾斜杠；
  3. **landed 容差**：该步观察的 ``url``（goto 落地页）与某锚点
     host 相同且 path 互为前缀——容忍重定向把 URL 改写成规范化形式；
- **不进 D-2 样本级**（决策 A）：D-2 保持只核 click/fill 的 target，
  本检查器是独立的批次级信号——两个信号分开放，D-2 分数跨模式可比。

数据源是 **P1 存档 JSON 本体**（不是 P2 样本）——步骤的动作参数与
观察链接都在存档里，读档即查。与 assembler 同一条纪律：交接面是
文件格式，不 import executor。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

from trajectory_pipeline.executor.archive import DEFAULT_ROOT, select_archives

#: 检查对象的动作来源。``infrastructure`` 的 goto/new_tab 是执行器自己
#: 的会话管理，URL 来自代码不是模型，不在溯源范围（§6.2：核查的是
#: **LLM 输出的可溯源性**）。
MODEL_ORIGIN = "model"

#: 锚点递推的观察流顺序 = 真实执行顺序：搜索单元 → 中间页（B/C）→
#: 站点访问。每条的 steps 按序消费，第 N 步观察到的链接只进第 N+1 步
#: 之后的锚点。
_STREAMS = ("steps", "intermediates", "visits")


def _norm(url: str) -> tuple[str, str, str] | None:
    """归一化：``(host 小写, 去尾斜杠 path, query)``。解析不了返回 None。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return (host, path, parts.query)


def _site(url: str) -> tuple[str, str] | None:
    """归一的 (host, path)，供 landed 容差的前缀匹配用。"""
    n = _norm(url)
    return (n[0], n[1]) if n else None


def _paths_prefix_match(a: str, b: str) -> bool:
    """两条 path 互为前缀（容忍重定向补充/剥掉尾巴）。

    根 path（``/``）**不与任何东西前缀匹配**——它是一切路径的前缀，
    放它过等于同 host 的任何 goto 都自动可溯源，容差退化成没有容差。
    """
    if a == "/" or b == "/":
        return a == b
    return a.startswith(b) or b.startswith(a)


class _Anchors:
    """锚点集：原串集合 + 归一集合，双档匹配。"""

    def __init__(self) -> None:
        self._raw: set[str] = set()
        self._norm: set[tuple[str, str, str]] = set()
        self._sites: set[tuple[str, str]] = set()

    def add(self, url: str) -> None:
        url = (url or "").strip()
        if not url:
            return
        self._raw.add(url)
        n = _norm(url)
        if n:
            self._norm.add(n)
            self._sites.add((n[0], n[1]))

    def covers(self, url: str) -> bool:
        """容差三档：原串 → 归一 → （调用方另做 landed 前缀匹配）。"""
        if url in self._raw:
            return True
        n = _norm(url)
        return n is not None and n in self._norm

    def covers_site(self, site: tuple[str, str]) -> bool:
        """landed 容差：host 相同且 path 互为前缀。"""
        host, path = site
        return any(h == host and _paths_prefix_match(path, p)
                   for h, p in self._sites)


def _feed_observation(anchors: _Anchors, obs: Mapping[str, Any] | None) -> None:
    """把一次观察的 url 与其 links 喂进锚点。相对 href 以观察页 URL 解析。"""
    if not obs:
        return
    base = str(obs.get("url") or "")
    anchors.add(base)
    for link in obs.get("links") or ():
        if not isinstance(link, Mapping):
            continue
        href = str(link.get("href") or "")
        if not href:
            continue
        # 相对 / 协议相对 href 以产生它的观察页为基解析——「站内相对
        # URL 解析结果」由此天然入锚（§6.2 例外口径的一般化）。
        anchors.add(urljoin(base, href) if base else href)


def _initial_anchors(doc: Mapping[str, Any]) -> _Anchors:
    """递推起点：候选池 ∪ 中间页 URL ∪ search_url ∪ 搜索观察。"""
    anchors = _Anchors()
    for c in doc.get("candidates") or ():
        if isinstance(c, Mapping):
            anchors.add(str(c.get("url") or ""))
    for ir in doc.get("intermediates") or ():
        if isinstance(ir, Mapping):
            anchors.add(str(ir.get("url") or ""))
    anchors.add(str(doc.get("search_url") or ""))
    _feed_observation(anchors, doc.get("search_observation"))
    return anchors


def _steps_of(doc: Mapping[str, Any], stream: str) -> Iterable[Mapping[str, Any]]:
    if stream == "steps":
        # 顶层搜索单元的 steps 是**扁平**的 [step, ...]；中间页/站点是
        # [{..., "steps": [step, ...]}, ...] 按单元嵌套。两层形状不同源，
        # 在这里分派而不是让调用方记。
        for step in doc.get("steps") or ():
            if isinstance(step, Mapping):
                yield step
        return
    for unit in doc.get(stream) or ():
        if not isinstance(unit, Mapping):
            continue
        for step in unit.get("steps") or ():
            if isinstance(step, Mapping):
                yield step


def check_record(doc: Mapping[str, Any]) -> dict[str, Any]:
    """核一份 P1 存档，返回 ``{task_id, executor_mode, checked, traced,
    skipped, violations}``。

    ``violations`` 每项：``{stream, index, url, landed}``——明细足够
    人工复核复现（哪条链第几步、 goto 去哪、实际落在哪）。
    """
    anchors = _initial_anchors(doc)
    checked = traced = skipped = 0
    violations: list[dict[str, Any]] = []
    for stream in _STREAMS:
        for index, step in enumerate(_steps_of(doc, stream)):
            action = step.get("action")
            is_model_goto = (
                isinstance(action, Mapping)
                and action.get("tool") == "goto"
                and action.get("origin") == MODEL_ORIGIN)
            if is_model_goto:
                params = action.get("params") or {}
                url = (str(params.get("url") or "")
                       if isinstance(params, Mapping) else "")
                if not url:
                    # 没有 URL 的 goto 不是「溯源通过/不通过」能回答的
                    # 问题——动作本身 malformed，单独计数让它可见。
                    skipped += 1
                else:
                    checked += 1
                    if anchors.covers(url):
                        traced += 1
                    else:
                        obs = step.get("observation")
                        landed = (str(obs.get("url") or "")
                                  if isinstance(obs, Mapping) else "")
                        site = _site(landed) if landed else None
                        if site and anchors.covers_site(site):
                            # landed 容差：重定向改写，落地页可溯源
                            traced += 1
                        else:
                            violations.append({
                                "stream": stream, "index": index,
                                "url": url, "landed": landed,
                            })
            # 该步观察喂锚点——时序刻意放在判定**之后**：本步观察到的
            # 链接救不了本步的 goto（模型做决定时还没看见它）。非 goto
            # 步骤同样喂：click 之后的观察正是下一个 goto 的判据来源。
            _feed_observation(
                anchors, step.get("observation")
                if isinstance(step.get("observation"), Mapping) else None)
    return {
        "task_id": str(doc.get("task_id") or ""),
        "executor_mode": str(doc.get("executor_mode") or "") or "unknown",
        "checked": checked, "traced": traced, "skipped": skipped,
        "violations": violations,
    }


def check_batch(
    docs: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """批次级汇总——验收⑤的口径出处（幻觉记账决策 A：独立批次级信号）。"""
    records = [check_record(d) for d in docs]
    by_mode: dict[str, dict[str, int]] = {}
    for r in records:
        slot = by_mode.setdefault(r["executor_mode"],
                                  {"records": 0, "checked": 0, "traced": 0,
                                   "violations": 0})
        slot["records"] += 1
        slot["checked"] += r["checked"]
        slot["traced"] += r["traced"]
        slot["violations"] += len(r["violations"])
    return {
        "records": len(records),
        "checked": sum(r["checked"] for r in records),
        "traced": sum(r["traced"] for r in records),
        "violations": sum(len(r["violations"]) for r in records),
        "by_mode": by_mode,
        "details": records,
    }


def _load_archives(root: pathlib.Path, limit: int | None
                   ) -> list[Mapping[str, Any]]:
    """挑档读 JSON。坏一份不拖垮整批——批次报告要的是「这批怎样」，
    一份坏档的正确处置是(skip)留痕，不是让报告消失。"""
    docs: list[Mapping[str, Any]] = []
    sel = select_archives(root)
    files = [p for p, _reviewed in sel]
    for path in (files[:limit] if limit else files):
        try:
            docs.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:                       # noqa: BLE001
            print(f"  [skip] {path.name} 读档失败："
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
    return docs


def _print(report: Mapping[str, Any]) -> None:
    print(f"== goto 溯源 · {report['records']} 份存档 · "
          f"model-goto {report['checked']} 个 · 不可溯源 "
          f"{report['violations']} 个 ==")
    for mode, slot in sorted((report.get("by_mode") or {}).items()):
        total = slot["checked"]
        pct = slot["violations"] / total if total else 0.0
        print(f"  [{mode}] {slot['records']} 份   goto {total}   "
              f"可溯源 {slot['traced']}   不可溯源 {slot['violations']}"
              f" ({pct:.0%})")
    details = [r for r in report.get("details") or () if r["violations"]]
    if not details:
        print("\n  无违规——全部 model-goto 可溯源")
        return
    print(f"\n  违规明细 {sum(len(r['violations']) for r in details)} 条"
          f"（前 10 条；--json 看全量）：")
    shown = 0
    for r in details:
        for v in r["violations"]:
            if shown >= 10:
                return
            shown += 1
            print(f"    [{r['executor_mode']}] {r['task_id']} "
                  f"{v['stream']}[{v['index']}] goto {v['url']!r}"
                  f"（落地 {v['landed'] or '—'}）")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="python -m trajectory_pipeline.evaluation.goto_trace",
        description="批次级 goto URL 溯源断言（方案 13 §6.2）")
    default_root = pathlib.Path(__file__).resolve().parents[1] / DEFAULT_ROOT
    ap.add_argument("--out", default=str(default_root),
                    help="P1 存档目录（默认 output/pipeline，优先取 .reviewed.json）")
    ap.add_argument("--limit", type=int, default=None, help="只查前 N 份存档")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="把报告落成 JSON")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.out)
    if not root.is_dir():
        print(f"{root} 不是目录", file=sys.stderr)
        return 2
    report = check_batch(_load_archives(root, args.limit))
    _print(report)
    if args.json_out:
        p = pathlib.Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                     encoding="utf-8")
        print(f"\n→ 已写 {p}")
    # 有违规不非零退出：这是**质量信号**不是闸门——批次报告给人看，
    # 退出码给自动化用，混用会让「agent 批有幻觉」在 CI 里变成失败，
    # 而它正是 A/B 对比要收集的数据。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["check_record", "check_batch", "main"]
