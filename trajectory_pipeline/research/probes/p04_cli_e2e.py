"""P0-4 端到端：跑一遍真实 CLI 的两条命令，看两个文件是否真的分开。

不联网、不开浏览器：用一份最小的假 P1 存档。

跑法（在 trajectory_pipeline/ 下）：
    uv run python research/probes/p04_cli_e2e.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ARCH = {
    "task_id": "T001",
    "title": "功夫",
    "query": "功夫 在线观看",
    "outcomes": [
        {"site": "a.com", "url": "https://a.com/x", "landed_url": "https://a.com/x",
         "branch": "unresolved", "evidence": "看不清"},
    ],
    "visits": [
        {"url": "https://a.com/x", "final_url": "https://a.com/x",
         "success": False, "steps": [],
         "obs": {"url": "https://a.com/x", "page_title": "功夫",
                 "body_text": "x" * 50, "video_tag_count": 1,
                 "interactive_elements": []}},
    ],
}


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "trajectory_pipeline.executor.cli", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "T001__aaaa1111.json").write_text(
            json.dumps(ARCH, ensure_ascii=False), encoding="utf-8")
        q = root / "review_queue.jsonl"

        r = run("review", "--out", str(root), "--write", str(q))
        print("== review --write ==")
        print(r.stdout.strip())
        assert q.exists(), "队列没落盘"
        rows = [json.loads(x) for x in q.read_text(encoding="utf-8").splitlines()]
        assert rows and "verdict" not in rows[0], "队列档不该带裁定栏"

        # 复核员填裁定（模拟人工编辑）
        for row in rows:
            row["verdict"] = "confirm_blocked"
            row["reviewed_by"] = "reesn"
            row["note"] = "被验证码挡住"
        q.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows),
                     encoding="utf-8")

        r = run("review-queue", str(q), "--settle")
        print("\n== review-queue --settle ==")
        print(r.stdout.strip())
        v = root / "review_queue.verdicts.jsonl"
        assert v.exists(), "裁定档没落盘"
        vrows = [json.loads(x) for x in v.read_text(encoding="utf-8").splitlines()]
        assert vrows[0]["verdict"] == "confirm_blocked"
        assert "body_preview" not in vrows[0], "裁定档不该带证据摘要"

        # ⚠️ 关键一步：重跑 review --write（这就是原 bug 的触发动作）
        r = run("review", "--out", str(root), "--write", str(q))
        print("\n== 重跑 review --write（原 bug 的触发动作）==")
        assert r.returncode == 0
        left = [json.loads(x) for x in q.read_text(encoding="utf-8").splitlines()]
        assert "verdict" not in left[0], "队列被重建（预期）"
        after = [json.loads(x) for x in v.read_text(encoding="utf-8").splitlines()]
        assert after[0]["verdict"] == "confirm_blocked", "裁定档被擦了！"
        print(f"  队列已重建（无裁定栏）；裁定档仍在：{after[0]['verdict']}")

        # apply：给队列路径也应读裁定档
        r = run("review", "--out", str(root), "--verdicts", str(q), "--apply")
        print("\n== review --verdicts <队列> --apply（自动切到裁定档）==")
        print(r.stdout.strip().splitlines()[0] if r.stdout.strip() else "(无输出)")
        reviewed = root / "T001__aaaa1111.reviewed.json"
        assert reviewed.exists(), "复核档没写"
        doc = json.loads(reviewed.read_text(encoding="utf-8"))
        print(f"  复核档 outcomes[0].branch = {doc['outcomes'][0]['branch']}")
        assert doc["outcomes"][0]["branch"] == "blocked"

    print("\n全部通过：队列可安全重跑，裁定档不受影响，apply 能读到裁定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
