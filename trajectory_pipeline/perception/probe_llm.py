"""W3 一次性取证探针 v2——**不是生产代码**。

v1 的三个实测发现（全部是「不照实测就写不出来」的那类）：

1. **模型默认输出散文，不输出 JSON。** v1 探针只喂了 ``Question.prompt``，
   没把 ``schema_hint`` 变成可执行的输出契约，于是 IS_REACHABLE 与
   PLAYER_OK 全返回自然语言/markdown 列表。这条决定了
   ``schema_parse`` 必须做「散文里抠 JSON」而不能只做 ``json.loads``。
2. **大输入会 None。** v1 的 SELECT_PLAY_SITES（1200 字正文 + 40 元素
   + 30 链接）直接 None，reason 当时不可见。
3. **模型会把「采集不全」读成「页面不行」。** iqiyi 的 PLAYER_OK 判否，
   理由是「body 为空、元素为 0，说明未渲染」——它给了一个**自信的错答案**
   而不是 fail-closed。规则版早就防住这条（见 ``test_零个交互元素不判无播放控件``），
   所以 LLMPerceptor **必须在代码层预检采集充分性，不能指望模型自己判**。

**它为什么在包内**：后端行为会漂（哪个模型返内联 ``<think>``、哪个吐散文、
哪个写未转义引号），而这三条一旦漂掉就只能重新踩一遍——上表每一条都是花掉
一轮真实批次换来的。放产物目录会被 ``.gitignore`` 挡掉，换台机器就没了，
所以它与 ``executor/browser/probe_*.py`` 同惯例：探针与被探模块同目录。

跑法（凭据从环境变量，不进文件）::

    uv run python -m trajectory_pipeline.perception.probe_llm [题目id]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from trajectory_pipeline.llm.client import LLMClient, LLMConfig
from trajectory_pipeline.perception import questions
from trajectory_pipeline.common.elements import (
    element_json,
    vocabulary_of_archive_elements,
)
from trajectory_pipeline.perception.base import (
    Q,
)

BODY_CHARS = int(os.environ.get("PROBE_BODY_CHARS", "600"))
#: ``perception/`` 的上一级就是包根，产物仍在 ``trajectory_pipeline/output/``
#: ——CLAUDE.md 决策 D8 定的落点，不许改到仓库根 ``output/``（那是 v1 的）。
OUT_DIR = Path(__file__).resolve().parents[1] / "output" / "pipeline"
ARCHIVES = sorted(OUT_DIR.glob("T*.json"))

#: 逐题输出契约。用 Question.schema_hint 生成，但**显式写死键名**——
#: 后端不强制 json_schema，模型会漂字段名，而「漂了」这件事只有在
#: 输出契约里把键名列全时才拦得住。
CONTRACTS = {
    Q.SELECT_PLAY_SITES: (
        '{"selected":[{"url":"...","title":"...","why":"..."}],'
        '"rejected":[{"url":"...","reason":"..."}]}'
    ),
    Q.IS_REACHABLE: '{"answer":true|false,"evidence":"一句话，指出页面上的具体证据"}',
    Q.FIND_PLAY_CONTROL: (
        '{"ref":"控件ref或null","trailer_only":true|false,'
        '"trailer_suspect":["标签文本"],"why":"一句话"}'
    ),
    Q.PLAYER_OK: '{"answer":true|false,"evidence":"一句话，指出页面上的具体证据"}',
}


def _obs(raw: dict) -> dict:
    raw_elements = raw.get("interactive_elements") or []
    vocabulary = vocabulary_of_archive_elements(raw_elements)
    return {
        "url": raw.get("url", ""),
        "page_title": raw.get("page_title", ""),
        "body_text": (raw.get("body_text") or "")[:BODY_CHARS],
        "interactive_elements": [
            element_json(e, vocabulary) for e in raw_elements[:25]
        ],
        "links": [{"text": l.get("text", "")[:60], "href": l.get("href", "")}
                  for l in (raw.get("links") or [])[:20]],
        "video_tag_count": raw.get("video_tag_count", 0),
        "iframe_count": raw.get("iframe_count", 0),
        "body_chars": len(raw.get("body_text") or ""),
    }


def run_case(client: LLMClient, qid: str, obs: dict, title: str, **extra) -> None:
    spec = questions.get(qid)
    system = (
        spec.prompt
        + "\n\n只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。"
        + "\n输出格式必须严格是：" + CONTRACTS[qid]
    )
    user = json.dumps({"observation": obs, **extra}, ensure_ascii=False)
    raw = client.chat(system, user, max_tokens=600)
    print("=" * 70)
    print(f"[{qid}] {title}")
    print(f"  url={obs['url'][:64]}")
    print(f"  title={obs['page_title'][:40]!r} body={obs['body_chars']}字 "
          f"elems={len(obs['interactive_elements'])} video={obs['video_tag_count']} "
          f"iframe={obs['iframe_count']}")
    if raw is None:
        print(f"  >>> None  reason={client.last_error!r}")
    else:
        print(f"  >>> {len(raw)}字符:")
        print("  " + raw[:900].replace("\n", "\n  "))
    print()


def main() -> int:
    cfg = LLMConfig.from_env()
    client = LLMClient(cfg)
    print(f"# 模型={cfg.model} @ {cfg.base_url} 正文上限={BODY_CHARS}\n")
    if not ARCHIVES:
        print("没有真实存档")
        return 1

    only = sys.argv[1] if len(sys.argv) > 1 else ""
    arc = json.loads(ARCHIVES[0].read_text(encoding="utf-8"))

    if not only or only == Q.SELECT_PLAY_SITES:
        run_case(client, Q.SELECT_PLAY_SITES, _obs(arc["search_observation"]),
                 "真实搜索页", target_title=arc.get("title", ""))

    if not only or only == Q.IS_REACHABLE:
        for v in (arc.get("visits") or [])[:2]:
            run_case(client, Q.IS_REACHABLE, _obs(v["site_obs"]),
                     f"真实站点页 {v.get('landed_url','')[:40]}")

    if not only or only == Q.FIND_PLAY_CONTROL:
        for v in (arc.get("visits") or [])[:3]:
            run_case(client, Q.FIND_PLAY_CONTROL, _obs(v["site_obs"]),
                     f"真实站点页 {v.get('landed_url','')[:40]}")

    if not only or only == Q.PLAYER_OK:
        for p in ARCHIVES:
            a = json.loads(p.read_text(encoding="utf-8"))
            for v in a.get("visits") or []:
                po = v.get("player_obs") or v.get("site_obs")
                if po and (v.get("success") or po.get("video_tag_count")):
                    run_case(client, Q.PLAYER_OK, _obs(po),
                             f"{v.get('landed_url','')[:50]} success={v.get('success')}")
                    break

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())