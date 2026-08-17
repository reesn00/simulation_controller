# Phase 6 最终验证报告

日期：2026-08-15

## 已通过

- 58 Task / 11 Scenario 严格编译，0 diagnostics。
- 89 个单元、合约和离线功能测试通过。
- 生产包实测语句覆盖率为 85%；该指标用于记录基线，不设置为了追数字而扭曲工具/模型边界的硬门槛。
- CLI help、validate-config、check-tools 通过。
- Python compileall 通过。
- Wheel 构建通过，三份正式 YAML 存在，重复配置不存在。
- Wheel 独立 target 安装后 `--validate-config` 通过，且包内 Playwright package/lock 路径可定位。
- Playwright MCP 0.0.78 package-lock 已生成。
- Camoufox 0.6.0 optional dependency 已进入 uv.lock。
- 浏览器证据已按 fail-closed 语义区分工具错误、HTTP 失败状态、访问门槛、媒体缺失、播放未确认、证据数量不足和置信度不足；`video.playable` 必须精确观察到 `progressed=true`。
- T026 的链接验证要求至少 3 个候选分别取得证据；不能由第一个可访问链接提前判过。
- 排除平台 Validator 检查全部出现位置，覆盖“先否定、后推荐”的绕过场景。
- v2 与 legacy 蒸馏都要求最终 ValidationReport 为 PASS 且其中准则全部 PASS；没有验证报告的 SUCCESS 不再导出。
- append-only Event/Validation 与 checkpoint 在加载时按稳定 ID 对账，覆盖“已 append、未 checkpoint”崩溃窗口。
- submit 已成功但 poll 失败时保留远端 task/session/agent ID，避免审计记录丢失已接受任务。
- F001 文件操作要求 `filesystem.inspect` 证据；当前未配置该 Provider 时按设计返回 INCONCLUSIVE，不接受纯文本自述为成功。
- CAMEL Actor 的无工具边界、思维文本剥离，以及 Semantic Judge 的严格 ID/JSON 和 fail-closed 路径已加入离线替身测试。
- QwenPaw 后台任务路由已通过真实 HTTP 与服务端源码确认：提交为 `POST /api/console/chat/task`，轮询为 `GET /api/console/chat/task/{task_id}`；提交只返回 `task_id`，最终 `session_id` 位于 `result`。
- `open_session()` 现在显式生成 `useramulation-<uuid>` 会话 ID。QwenPaw 缺省会回退到固定 `default` 会话，显式生成可避免不同模拟任务之间的历史污染。
- Catalog v2 的 `initial_request` 现在原样作为首轮消息，不再由本地 Actor 改写；隐藏 fixture 仍不会进入远端请求。
- 追问只选择可重试的 executor-owned FAIL，并强制包含“保留已满足内容、返回完整修订结果”的请求；触发原因和 Criterion ID 进入 `FOLLOWUP_CREATED` 事件。
- 启动装配会汇总当前不能达到 PASS 的任务及缺失的 Judge/Provider 能力，但不会伪造远端证据。
- `--readiness` 可在不连接 QwenPaw、不创建 Run 日志的前提下输出任务级可验证性；当前无模型配置时准确报告 58 个任务缺少 semantic Judge，另列 22 个浏览器任务和 F001 文件取证缺口。
- Runtime 会识别此前 PASS、本轮再次非 PASS 的 Criterion，在追问中明确要求合并前后结果，并把回退准则写入事件明细。
- 内置配置及重建 Wheel 中的模型凭据和内部 endpoint 均为空，支持通过标准环境变量注入。

## 非阻断未执行

- QwenPaw HTTP 服务已启动，但 LLM 能力未启用。2026-08-15 仅提交一次契约探针，服务返回 `task-9cc9d93834c4`；前 10 秒持续为 `running`，约 80 秒后终态为顶层 `finished`、内层 `failed`，错误码 `MODEL_EXECUTION_ERROR`，原因包含上游 HTTP 502。真实失败包络与客户端离线合约测试一致。服务端没有公开取消接口，因此调用方仍必须携带有限 `timeout`。
- Playwright/Camoufox 浏览器 smoke：默认不自动安装浏览器；启用 Provider 后按 setup 文档执行。
- 需要真实模型输出的完整会话、多轮追问、语义判定和远端超时终态未执行；必须待远端 Agent 开启完整 LLM 能力后验证。本轮按约束不再提交第二个任务，也未调用流式聊天接口。
- “完整影片而非预告片/剪辑版”仍不能仅凭播放进度完全证明，需真实浏览器联调后增加媒体时长和内容身份级证据。

以上未执行项不属于当前离线门禁，不能据此把未验证外部行为宣称为已通过。

## 2026-08-17 远端恢复后补充验证

### 已验证

- 离线基线与修复后全量回归通过：`90 passed`；配置编译仍为 58 Task / 11 Scenario / 0 diagnostics，`compileall` 通过。
- 临时注入 MiniMax 模型配置后，`--readiness` 从 58 个任务全部缺少 Judge 改为 35 个任务可完整判定，剩余 22 个浏览器任务和 F001 仍缺外部取证 Provider；联调结束后内置配置已恢复为空。
- T006 在无本地 Judge 的真实运行 `run_898d7b8e056541fba7dbb2dd3a7d8e16` 中完成首轮加 3 次追问：同一远端 session、4 个独立 remote task、4 个 executor turn、3 个 guide round，约 202 秒后按 fail-closed 规则结束为 INCONCLUSIVE。
- MiniMax Semantic Judge 的最小真实探针成功返回严格结构化 verdict；对已保存的真实长回复重放时，3 条语义准则返回 `PASS / INCONCLUSIVE / PASS`，证明真实模型判定链路可用且证据不足不会放行。
- 修复后再次运行 T006 时，远端 task `task-164db61109a8` 在 poll 阶段返回 `Task cancelled`；Run `run_53ff5891ea8b46dc867abc537ede17ac` 正确保存为 EXECUTOR_ERROR，并保留 run/session/task 标识及 `rerun_of` 关系。

### 本次发现并修复

- `CamelSemanticJudge` 过去只在 Prompt 中描述 JSON，未向 CAMEL 传递 `response_format=JudgeResponse`；MiniMax 会返回自有结构，现已显式启用 Pydantic 结构化输出。
- CAMEL 已解析的 Pydantic 对象位于 `message.parsed`，旧实现却继续解析含 `<think>`/代码围栏的原始 content；现改为优先使用 parsed，缺失时才回退到严格 JSON 文本解析。
- Judge 超时过去也会重试一次，使 60 秒预算扩大到约 120 秒；现超时直接 fail-closed，解析/Schema 失败仍可二次尝试，并新增不重试超时的回归测试。

### 仍未完成

- Playwright/Camoufox 本地浏览器 smoke 仍未执行；22 个任务缺 `browser.navigate` / `browser.snapshot`，媒体时长和内容身份级证据仍待真实浏览器联调。
- F001 仍缺 `filesystem.inspect` Provider，按设计只能 INCONCLUSIVE。
- 真实远端多轮与真实 MiniMax Judge 已分别验证，但尚未得到一次稳定的、同一 Run 内最终 SUCCESS 的完整组合链路；本次远端存在成功多轮与取消终态两种结果。
- 未主动制造远端 poll timeout：服务没有取消接口，故不为测试超时而遗留孤立远端任务；timeout 终态继续由离线合约测试覆盖。

本节取代 2026-08-15 报告中“真实模型完整会话、追问和语义判定均未执行”的当前状态描述；原段落保留为当日历史记录。
