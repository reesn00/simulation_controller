import json
from collections import Counter

p = r"c:/Users/klpc/workspace/code/items/simulation_controller/output/agent_trajectory/run_8ec3fd6d221c426ba3eec793e666cf88__useramulation-88f2af1877db49a78661abea02c5df30.json"
raw = open(p, "r", encoding="utf-8").read()

depth = 0
in_str = False
esc = False
start = -1
objs = []
for i, ch in enumerate(raw):
    if in_str:
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_str = False
        continue
    if ch == '"':
        in_str = True
    elif ch == "{":
        if depth == 0:
            start = i
        depth += 1
    elif ch == "}":
        if depth > 0:
            depth -= 1
        if depth == 0 and start >= 0:
            try:
                objs.append(json.loads(raw[start:i + 1]))
            except Exception:
                pass
            start = -1

print("total events:", len(objs))
et = Counter(o.get("event_type", "?") for o in objs if isinstance(o, dict))
for k, v in et.most_common():
    print(f"  {k}: {v}")

print()
print("model_request 详情:")
sys_lens = []
tool_lens = []
msg_counts = []
for idx, o in enumerate(objs):
    if o.get("event_type") == "model_request":
        payload = o.get("payload") or {}
        msgs = payload.get("messages") or []
        tools = payload.get("tools") or []
        sys_text_len = 0
        for m in msgs:
            if (m.get("role") or m.get("name")) == "system":
                content = m.get("content", [])
                text = ""
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            text += b.get("text", "")
                sys_text_len = len(text)
                break
        sys_lens.append(sys_text_len)
        tool_lens.append(len(tools))
        msg_counts.append(len(msgs))

for i, (s, t, n) in enumerate(zip(sys_lens, tool_lens, msg_counts)):
    print(f"  mr[{i}] messages={n}  system_text_chars={s}  tools_def={t}")

# 看一下 final_reply 的 content 形态
print()
print("final_reply content 形态:")
for idx, o in enumerate(objs):
    if o.get("event_type") == "final_reply":
        content = (o.get("payload") or {}).get("content", [])
        print(f"  final_reply[{idx}]: content is list with {len(content)} entries")
        for j, c in enumerate(content):
            if isinstance(c, dict):
                t = c.get("type")
                inner = c.get("content", [])
                inner_types = []
                if isinstance(inner, list):
                    inner_types = [b.get("type") if isinstance(b, dict) else "?" for b in inner]
                print(f"    [{j}] type={t} inner_types={inner_types}")
        break
