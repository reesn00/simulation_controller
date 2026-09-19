# Project Notes — 历史档案与待治理技术债

本文档归集暂未生效 / 历史性的项目笔记，仅作查阅依据，不作为开发规范。规范约束一律见 [CLAUDE.md](../CLAUDE.md)。

## 1. 待治理技术债

### 1.1 凭据泄漏与内置配置残留

`simulate_serve/config/config.yaml` 含真实 API key + 内网 endpoint。

**当前症状**：
- `tests/test_cli.py::test_builtin_config_does_not_package_model_credentials_or_internal_endpoint` 在 main HEAD 仍失败
- 历史 commit 与早期文档可能残留凭据/端点片段

**治理方向**：
1. 全仓 grep 真实 key 与内网 endpoint 模式，确认无残留
2. 修复 `test_cli.py` 失败项（重写断言或彻底删除内置 key/endpoint）
3. CI 加 pre-commit / pre-receive hook 拦截 key 模式
4. `CLAUDE.md` "配置和工具"段已写明凭据不得提交、打包、复制到测试/文档/日志 — 需要把约束转化为执行机制

## 2. Trajectory 格式演化历史

### 2.1 2026-09-06 AI SDK 内嵌快照路径（已废弃）

早期假设 trajectory 是 AI SDK 形态：`tool_call` / `tool_result` 不以独立事件出现，而是嵌在 `LAST model_request.payload.messages[].content` 中；工具定义在 `model_request.payload.tools`。

相关决策（均已移除，仅作历史）：
- `etl/qwenformat/load.py::parse_trajectory` 检测 LAST model_request 是否含 assistant message；若是走 **AI SDK 抽取路径**（从 messages.content 中解析 type=text / type=tool_call / type=tool_result；text 中 `<think>...</think>` 由 `_split_thinking` 拆为独立 ThinkingBlock）；否则保留旧的事件流路径（向后兼容）。
- `etl/qwenformat/load.py::_final_reply_last_text` 只取 final_reply 最后一个 message/reasoning 块（中间 plugin_call / plugin_call_output / 早期 message 被 model_request.messages 覆盖，跳过避免重复）。
- `etl/qwenformat/load.py::to_session_dict` 把 SessionRecord.tools 透传到 Session dict (`"tools": list(self.tools)`)。
- `etl/qwenformat/transform.py::trajectory_to_session_with_openai_metadata` 优先用 `trajectory["tools"]`（来自 model_request.payload.tools，含完整 description + parameters schema）；缺失时回退到从 toolcall 推导。
- `gdr/domain/schema.py::Message.role` 从 `Literal["user","assistant"]` 扩展为 `Literal["system","user","assistant"]`，接纳 qf_out 新增的 system message；schema 其余约束不动。

### 2.2 2026-09-18 独立事件流（当前）

QwenPaw 后端在两周内切换了 trajectory 输出形态，从 AI SDK 内嵌快照恢复为独立事件流。当前实现见 [CLAUDE.md "数据格式约定"](../CLAUDE.md)；唯一重放入口 `etl/qwenformat/load.py::parse_trajectory`；`_split_thinking` 与 AI SDK 快照抽取路径已全部删除。

### 2.3 后续格式变化的处理约定

若 QwenPaw 后端再次变更 trajectory 形态：
1. 在 `etl/qwenformat/load.py` 单一入口实现版本探测 / 新格式适配
2. 同步更新 [CLAUDE.md "数据格式约定"](../CLAUDE.md)
3. 在本文件追加变更条目（不复用旧段落，避免误导）