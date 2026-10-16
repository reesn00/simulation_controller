"""把 E005 的 P3 视图渲染成一份可读 HTML。

只做**展示**，不做任何判定——轨迹内容全部来自 P3 产物，
这里不新增也不改写一个字。
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

ROOT = Path("trajectory_pipeline/output/pipeline")

ROLE_LABEL = {
    "system": "系统提示",
    "user": "用户",
    "assistant": "助手",
    "tool": "观察（工具返回）",
}


def esc(s: str) -> str:
    return html.escape(str(s or ""))


def _nl(s: str) -> str:
    """正文换行 → <br>，并转义。"""
    return esc(s).replace("\n", "<br>")


def render_tool_calls(tcs: list[dict]) -> str:
    if not tcs:
        return ""
    rows = []
    for tc in tcs:
        fn = tc.get("function") or {}
        name = fn.get("name") or "?"
        raw = fn.get("arguments")
        if isinstance(raw, str):
            try:
                args = json.loads(raw)
            except ValueError:
                args = {"_raw": raw}
        else:
            args = raw or {}
        frag = "  ".join(f'<span class="k">{esc(k)}</span>='
                         f'<span class="v">{esc(str(v)[:120])}</span>'
                         for k, v in args.items())
        rows.append(
            f'<div class="call"><span class="fname">{esc(name)}</span>'
            f'<span class="args">{frag}</span></div>'
        )
    return "".join(rows)


def render_msg(m: dict) -> str:
    role = m.get("role") or "?"
    content = str(m.get("content") or "")
    tcs = m.get("tool_calls") or []
    cls = f"msg {role}"

    # 标签：assistant 带 tool_calls 时，它的 content 就是「动作前动机」
    badge = ROLE_LABEL.get(role, role)
    extra = ""
    if role == "assistant" and tcs and content.strip():
        badge = "助手 · 动作前动机（rationale）"
        extra = " motive"
    elif role == "assistant" and not tcs:
        badge = "助手 · 收尾总结（reflection）"
        extra = " closing"

    parts = [f'<div class="{cls}{extra}">']
    parts.append(f'<div class="badge">{esc(badge)}</div>')
    if content.strip():
        long = len(content) > 600
        body = content if not long else content[:600] + "\n…（正文已截断显示）"
        parts.append(f'<div class="body">{_nl(body)}</div>')
    if tcs:
        parts.append(f'<div class="calls">{render_tool_calls(tcs)}</div>')
    if role == "tool":
        long = len(content) > 400
        body = content if not long else content[:400] + "\n…（观察已折叠，全文见 P3 JSON）"
        parts.append(f'<div class="obs">{_nl(body)}</div>')
    parts.append("</div>")
    return "".join(parts)


def main() -> int:
    samples = sorted((ROOT / "samples").glob("E005__e35b7455__*.json"))
    if not samples:
        print("没有 E005 样本")
        return 1

    blocks = []
    for sp in samples:
        sj = json.loads(sp.read_text(encoding="utf-8"))
        vj = json.loads((ROOT / "views" / f"{sp.stem}_openai.json").read_text(
            encoding="utf-8"))
        msgs = vj.get("messages") or []
        gate = sj.get("gate") or {}
        sid = sj.get("sample_id") or sp.stem
        unit = sj.get("unit") or ""
        n_steps = len((sj.get("six") or {}).get("actions") or ())
        has_ref = bool(sj.get("reflection"))
        gstatus = str(gate.get("status") or "?")
        greason = "; ".join(str(x) for x in (gate.get("reasons") or ()))[:120]

        head = (
            f'<h2 id="{esc(sid)}">{esc(sid)}'
            f'<span class="tag">{esc(unit)}</span>'
            f'<span class="tag step">{n_steps} 步</span>'
            f'<span class="tag gate-{esc(gstatus)}">闸门 {esc(gstatus)}</span>'
            f'<span class="tag">reflection {"有" if has_ref else "无"}</span>'
            f'</h2>'
            f'<p class="reason">{esc(greason)}</p>'
        )
        body = "".join(render_msg(m) for m in msgs)
        blocks.append(f'<section>{head}<div class="flow">{body}</div></section>')

    first = json.loads(samples[0].read_text(encoding="utf-8"))
    title = first.get("title") or ""
    up = str((first.get("six") or {}).get("user_prompt") or "")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>E005 轨迹查看 · 中国好声音</title>
<style>
:root {{
  --bg:#f7f8fa; --card:#fff; --ink:#1f2328; --dim:#6b7280; --line:#e3e6ea;
  --motive:#eef2ff; --motive-l:#4f46e5;
  --closing:#ecfdf5; --closing-l:#047857;
  --tool:#f9fafb; --tool-l:#6b7280;
  --user:#fff7ed; --user-l:#b45309;
  --sys:#f3f4f6; --sys-l:#4b5563;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:32px 20px 80px; background:var(--bg); color:var(--ink);
  font:15px/1.7 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
.wrap {{ max-width:920px; margin:0 auto; }}
h1 {{ font-size:22px; margin:0 0 6px; }}
.sub {{ color:var(--dim); font-size:13px; margin-bottom:4px; }}
.lead {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; margin:16px 0 26px; font-size:14px; }}
.lead b {{ color:var(--motive-l); }}
section {{ margin-bottom:34px; }}
h2 {{ font-size:16px; margin:0 0 6px; display:flex; align-items:center;
  gap:8px; flex-wrap:wrap; }}
.tag {{ font-size:11px; font-weight:500; padding:2px 8px; border-radius:20px;
  background:var(--sys); color:var(--sys-l); }}
.tag.step {{ background:#e0f2fe; color:#0369a1; }}
.tag.gate-passed {{ background:#dcfce7; color:#15803d; }}
.tag.gate-failed {{ background:#fee2e2; color:#b91c1c; }}
.reason {{ color:var(--dim); font-size:12px; margin:0 0 12px; }}
.flow {{ background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:6px 16px 16px; }}
.msg {{ padding:12px 0; border-bottom:1px dashed var(--line); }}
.msg:last-child {{ border-bottom:none; }}
.badge {{ font-size:11px; font-weight:600; letter-spacing:.02em;
  text-transform:none; color:var(--dim); margin-bottom:6px; }}
.body {{ color:var(--ink); }}
.msg.motive {{ background:var(--motive); border-left:3px solid var(--motive-l);
  margin:10px -16px; padding:12px 16px; }}
.msg.motive .badge {{ color:var(--motive-l); }}
.msg.closing {{ background:var(--closing); border-left:3px solid var(--closing-l);
  margin:10px -16px; padding:12px 16px; }}
.msg.closing .badge {{ color:var(--closing-l); }}
.msg.user {{ background:var(--user); border-left:3px solid var(--user-l);
  margin:10px -16px; padding:12px 16px; }}
.msg.user .badge {{ color:var(--user-l); }}
.msg.system .badge {{ color:var(--sys-l); }}
.calls {{ margin-top:8px; }}
.call {{ font:12px/1.8 ui-monospace,SFMono-Regular,Menlo,monospace; }}
.fname {{ color:var(--motive-l); font-weight:700; }}
.args {{ color:var(--dim); margin-left:8px; }}
.k {{ color:#0369a1; }}
.v {{ color:#047857; }}
.obs {{ margin-top:6px; color:var(--dim); font-size:12.5px;
  background:var(--tool); border-radius:6px; padding:8px 10px;
  max-height:230px; overflow:auto; }}
.foot {{ color:var(--dim); font-size:12px; margin-top:30px;
  border-top:1px solid var(--line); padding-top:14px; }}
code {{ background:var(--sys); padding:1px 5px; border-radius:4px; font-size:12px; }}
</style></head><body><div class="wrap">
<h1>E005__e35b7455 · 轨迹查看</h1>
<div class="sub">任务：{esc(title)} · 共 {len(samples)} 条样本（1 搜索 + 6 站点）</div>
<div class="lead">
<b>怎么读</b>：紫色块是<b>动作前动机</b>（rationale，写在动作<b>之前</b>），
紧跟其下的是它驱动的那个 <b>tool_calls</b>，再下面是浏览器返回的<b>观察</b>。
INC46 之前这三者的顺序是颠倒的——rationale 描述的是「刚看到的页面」，
却排在动作前面。绿色块是 <b>reflection</b> 驱动的收尾总结。<br>
用户诉求：{esc(up)}
</div>
{"".join(blocks)}
<div class="foot">
源：<code>trajectory_pipeline/output/pipeline/E005__e35b7455.json</code>（P1）
→ <code>samples/E005__e35b7455__*.json</code>（P2）
→ <code>views/E005__e35b7455__*_openai.json</code>（P3）。
本页内容全部来自 P3，未作任何改写。
</div>
</div></body></html>"""

    out = Path("output/E005_轨迹查看.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    print(str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
