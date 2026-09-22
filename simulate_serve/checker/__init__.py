"""simulate_serve.checker — task completeness verification.

在 trajectory 落盘后、交接给 gdr 之前判定 agent 是否完整结束任务。

判定维度（5 维正向/反向混合）:
  1. 末尾 event_type ∈ {final_reply, error, cancel}
     - 不在 → incomplete(no_terminal_event)
     - error/cancel → aborted
     - final_reply → 进入维度 2
  2. 末尾 toolcall 未配对 → incomplete
  3. toolcall/toolresult 数量不匹配（尾部缺失） → incomplete
  4. 末尾 text 被截断（启发式） → incomplete
  5. 末段仅 thinking（>= 200 字符）且无 text → incomplete

新架构 ``simulation server → gdr → etl`` 下, 本模块仅依赖 trajectory
JSONL（不依赖 etl / gdr）, 保持 simulate_serve 自包含。
"""
from simulate_serve.domain.completion import CompletionCheck

from .completion_checker import (
    check_completion,
    snapshot_partial_trajectory,
)

__all__ = ["CompletionCheck", "check_completion", "snapshot_partial_trajectory"]