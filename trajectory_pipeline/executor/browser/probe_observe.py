"""端到端真实采集探针——验证 :class:`ObscuraDriver` 的**接线**，不是格式知识。

与 ``probe_returns.py`` 的分工：
    ``probe_returns.py``   取证 obscura 的 tool 返回格式（喂 ``dom.py`` 的单测）
    ``probe_observe.py``   跑一次真实的 ``goto + observe``，把 :class:`Observation`
                           落盘并断言契约——喂的是「driver 能不能用」这个问题

不跑网络单测的原因：mcp server 起来要几秒且依赖 ``OBSCURA_EXE``，
属集成探针；契约本身由 ``tests/unit/test_dom.py``（离线）与
``tests/contract/``（七不变式）守。这个脚本是三者的补充，不是替代。

用法::

    export OBSCURA_EXE=/path/to/obscura.exe
    uv run python -m trajectory_pipeline.executor.browser.probe_observe
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from trajectory_pipeline.executor.browser.mcp_client import McpClient
from trajectory_pipeline.executor.browser.obscura_driver import ObscuraDriver
from trajectory_pipeline.executor.dom import DEFAULT_MAX_CHARS
from trajectory_pipeline.perception.base import Observation

OUT_DIR = Path(__file__).resolve().parents[2] / "output" / "pipeline"

#: 采集目标。前两个是**格式基线**（已知干净/已知脏），第三个才是真实业务页。
TARGETS: tuple[tuple[str, str], ...] = (
    ("example", "https://example.com/"),
    ("baidu", "https://www.baidu.com/"),
)


def _jsonable(obj: Any) -> Any:
    """把 Observation 递归转成 JSON 友好结构。

    同时充当一条契约断言：转不过去说明 Observation 里混进了句柄 / 不可序列化对象。
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    return obj


def check(obs: Observation, *, expect_video: bool) -> list[str]:
    """对单次采集断言可机械验证的契约。返回**问题列表**（空 = 通过）。

    不断言「页面该有什么」——那是感知层的事，探针不该替 LLM 做判断。
    """
    problems: list[str] = []

    # I：snapshot 是唯一必需项，故 url / body 缺失一定是 bug
    if not obs.url:
        problems.append("url 为空（snapshot 解析失败）")
    if not obs.body_text:
        problems.append("body_text 为空（CSS 剥离过度或 snapshot 失败）")

    # ref 必须唯一且非空——click(ref) 直接依赖它
    refs = [e.ref for e in obs.interactive_elements]
    if len(refs) != len(set(refs)):
        dupes = sorted({r for r in refs if refs.count(r) > 1})
        problems.append(f"ref 重复: {dupes}")
    if any(not r for r in refs):
        problems.append("存在空 ref")

    # 采集必须无降级——降级会让「没采到」与「确实为空」无法区分
    if obs.degraded:
        problems.append(f"采集降级: {list(obs.degraded)}（规则版会因此 fail-closed）")
    if obs.body_source != "inner_text":
        problems.append(f"正文来源非 inner_text: {obs.body_source}")

    # 计数不应为负；video 计数缺失要能看出来（降级到 0 时不代表真的没有）
    if obs.video_tag_count < 0 or obs.iframe_count < 0:
        problems.append("计数为负")

    # 截断与污染状态必须自洽
    if obs.max_chars and obs.raw_len and obs.raw_len > obs.max_chars * 1.2:
        problems.append(
            f"raw_len={obs.raw_len} 远超 max_chars={obs.max_chars}——"
            f"obscura 未按 max_chars 截断，truncated={obs.truncated} 的推断可能失效"
        )
    if not 0.0 <= obs.stripped_ratio <= 1.0:
        problems.append(f"stripped_ratio 越界: {obs.stripped_ratio}")

    # 隐私红线：查的是**字段名**，不是内容。
    # 内容里出现 "cookie" 是正常的（几乎每个站点的隐私政策都写 cookie），
    # 按内容扫会天天误报，探针一旦天天误报就等于没有。
    # 真正的红线是「不把 cookie / 凭据当字段存下来」——见 obscura_driver.FORBIDDEN_TOOLS。
    for field_name in obs.__slots__:
        if any(t in field_name.lower() for t in ("cookie", "auth", "token", "credential")):
            problems.append(f"Observation 出现疑似凭据字段: {field_name}")

    # 诊断用，不算问题
    if expect_video and obs.video_tag_count == 0:
        print("  [提示] 目标期望有 video 标签但计数为 0——确认 selectors 是否匹配")
    return problems


async def main() -> int:
    try:
        client = McpClient.from_env()
    except RuntimeError as exc:
        print(f"跳过：{exc}")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"server": None, "observations": {}, "problems": {}}

    async with client:
        driver = ObscuraDriver(client)
        report["server"] = asdict(client.info)
        print(f"server = {client.info.name} {client.info.version}, {client.info.tool_count} tools")

        for name, url in TARGETS:
            print(f"\n[{name}] goto {url}")
            try:
                await driver.goto(url)
                obs = await driver.observe(max_chars=DEFAULT_MAX_CHARS)
            except Exception as exc:
                # 单站失败不该带崩整批——记下来继续
                report["observations"][name] = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
                report["problems"][name] = [f"采集失败: {exc}"]
                print(f"  FAIL {type(exc).__name__}: {exc}")
                continue

            issues = check(obs, expect_video=False)
            report["observations"][name] = {"url": url, "observation": _jsonable(obs)}
            report["problems"][name] = issues
            print(
                f"  url={obs.url!r} title={obs.page_title!r}\n"
                f"  body={len(obs.body_text)}ch (raw={obs.raw_len}, "
                f"stripped={obs.stripped_ratio:.2f}, truncated={obs.truncated})\n"
                f"  links={len(obs.links)} elements={len(obs.interactive_elements)} "
                f"video={obs.video_tag_count} iframe={obs.iframe_count}"
            )
            print(f"  {'OK' if not issues else 'ISSUES: ' + '; '.join(issues)}")

    out = OUT_DIR / "probe_observe.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n落盘: {out}")

    total = sum(len(v) for v in report["problems"].values())
    if total:
        print(f"共 {total} 项问题")
        return 1
    print("全部目标通过")
    return 0


if __name__ == "__main__":
    # Windows 控制台默认 GBK，✅/⚠️ 会抛 UnicodeEncodeError 把探针带崩。
    # 只放宽 errors 不改 encoding——PowerShell 下当前编码本就正常，不该被改掉。
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    sys.exit(asyncio.run(main()))