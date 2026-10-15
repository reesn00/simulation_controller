# -*- coding: utf-8 -*-
"""INC41：写 round=2 输入 JSONL 并执行 builder annotate + consistency。

口径（诚实记录，不伪造 test-retest）：
  - r2 判定者 = agent（基于存档证据面：outcome 行 + visit 观察 + 记账降级注记）
  - r1 判定者 = manual-review（人工语境）
  - 间隔 3 天（owner 2026-10-15 解除 14 天法定间隔，MVP 提速）
"""
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

# (golden_id, label, 证据摘要)——判定顺序按队列 golden_id 排序
ROWS = [
    ("E005__e35b7455__3bd50dfb", "no_play_control",
     "腾讯专题页 200 交互元素无非预告播放控件，标题/正文锚定"),
    ("E005__e35b7455__b8513a71", "component_unverified",
     "bangumi 播放页播放器报'发生了错误，请稍后再试'，video=0（记账降级注记同判）"),
    ("E005__e35b7455__de84d997", "player_unverified",
     "cztv vod URL + video_tag=1，body 采集降级无法核实可用性"),
    ("E001__57bbf995__site-2", "subscription_wall",
     "vip.1905.com 播放页，正文提示需 Flash 并登录会员观看，无 video/iframe"),
    ("E014__f50809fb__dec0b09f", "component_unverified",
     "乐视详情页+立即播放，点击前后同页，video=0（记账降级注记：本该记 component_unverified）"),
    ("E021__94a1392f__2ab12498", "component_unverified",
     "芒果TV 到达播放页，仅'立即播放'入口，video=0 iframe=0"),
    ("E021__94a1392f__2b9c7618", "no_play_control",
     "优酷用户上传页，136 交互元素无非预告播放控件"),
    ("E023__4d175424__a7f0eaaf", "unresolved",
     "bilibili visit 0 步、站点遍历超时>90s，存档无观察证据面 → fail-closed"),
    ("TPIN02__1a5e526d__bf4b6ae1", "unreachable_hard",
     "page_title='网页无法访问'，body 采集降级、无交互元素"),
    ("TPIN02__1cf5deca__3dcdbc14", "subscription_wall",
     "腾讯播放页+'开通会员'付费墙信号+设备不支持报错（2026-10-10 口径：有播放页需付费不算成功）"),
    ("TPIN02__1cf5deca__910a8b86", "not_play_site",
     "hao123 首页导航站，path='/'，结构上不可能是作品播放页"),
    ("TPIN02__1cf5deca__ad5971d1", "player_unverified",
     "爱奇艺播放页 URL + video_tag=1，body 采集降级无法核实是否《功夫》及可播性"),
]

PREFIX = ("INC41 MVP early retest (owner 2026-10-15, 14d gate lifted, interval 3d): "
          "r2=agent from archive evidence face; r1=manual-review human contextual; "
          "NOT a same-rater test-retest. 证据：")

inp = pathlib.Path("tmp/inc37_retest_input.jsonl")
inp.write_text(
    "\n".join(
        json.dumps({
            "golden_id": gid, "round": 2, "label": label,
            "reviewed_by": "agent-inc41", "reviewed_at": NOW,
            "note": PREFIX + ev, "interval_days": 3,
        }, ensure_ascii=False)
        for gid, label, ev in ROWS
    ),
    encoding="utf-8",
)
print(f"已写 {inp}（{len(ROWS)} 条，reviewed_at={NOW}）")

for cmd in (
    [sys.executable, "-m", "trajectory_pipeline.evaluation.golden.builder",
     "annotate", "--input", str(inp), "--force"],
    [sys.executable, "-m", "trajectory_pipeline.evaluation.golden.builder",
     "consistency"],
):
    print("\n$>", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    print(r.stdout)
    if r.returncode != 0:
        print("STDERR:", r.stderr[-2000:])
        sys.exit(r.returncode)
