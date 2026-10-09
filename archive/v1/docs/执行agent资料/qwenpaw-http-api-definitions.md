# QwenPaw HTTP 接口请求与返回数据定义

> 基于 `docs/qwenpaw-backend-api.md` 与源码 `src/qwenpaw/schemas.py`、`src/qwenpaw/app/routers/console.py`、`src/qwenpaw/app/routers/agent_scoped.py` 梳理。

---

## 1. 整体架构

```
外部请求 → FastAPI (_app.py)
    → AgentContextMiddleware (注入 agent_id)
    → AuthMiddleware (鉴权)
    → Router (console / agent-scoped / chats / tools / …)
    → DynamicMultiAgentRunner.stream_query(AgentRequest)
    → WorkspaceRegistry.get_agent(agent_id)
    → Runtime.run() → 8 阶段编排 → Envelope → SSE 流
```

**所有接口统一前缀：`/api`**（`app/_app.py:766` 挂载 `include_router(api_router, prefix="/api")`）

---

## 2. 核心数据模型（`src/qwenpaw/schemas.py`）

### 2.1 枚举

| 枚举名 | 值 |
|--------|------|
| `Role` | `"user"`, `"assistant"`, `"system"`, `"tool"` |
| `RunStatus` | `"created"`, `"in_progress"`, `"completed"`, `"failed"`, `"cancelled"` |
| `ContentType` | `"text"`, `"image"`, `"audio"`, `"video"`, `"file"`, `"data"`, `"refusal"` |
| `MessageType` | `"message"`, `"reasoning"`, `"plugin_call"`, `"plugin_call_output"`, `"function_call"`, `"function_call_output"`, `"mcp_tool_call"`, `"mcp_tool_call_output"`, `"progress"`, `"result"` |

### 2.2 Content 块（多模态）

所有 Content 块继承自 `_ContentBase`，共享字段：

```json
{
  "type": "<ContentType>",
  "delta": false,          // 是否为流式增量
  "index": 0,              // 序列号（可选）
  "status": "<RunStatus>",
  "object": "content",
  "msg_id": "<string>"
}
```

**各类型特有字段：**

| 类型 | 类名 | 特有字段 |
|------|------|---------|
| `"text"` | `TextContent` | `text: str` |
| `"image"` | `ImageContent` | `image_url: str?` |
| `"audio"` | `AudioContent` | `data: str?, format: str?` |
| `"video"` | `VideoContent` | `video_url: str?` |
| `"file"` | `FileContent` | `filename: str?, file_url: str?` |
| `"data"` | `DataContent` | `data: Any`（含 `FunctionCall` / `FunctionCallOutput`） |
| `"refusal"` | `RefusalContent` | `refusal: str` |

### 2.3 Message

```json
{
  "id": "<uuid_hex>",            // 默认自动生成
  "type": "message",             // MessageType 枚举
  "role": "user",                // Role 枚举，可空
  "content": [ ContentBlock, ... ],  // Content 联合类型列表
  "status": "in_progress",       // RunStatus
  "metadata": { ... }            // 可选字典
}
```

### 2.4 FunctionCall / FunctionCallOutput（嵌入 DataContent.data）

```json
// FunctionCall
{
  "call_id": "<string>?",
  "name": "<string>?",
  "arguments": "<string>?"
}

// FunctionCallOutput
{
  "call_id": "<string>?",
  "name": "<string>?",
  "output": "<string>?"
}
```

### 2.5 AgentRequest（入站请求体）

```python
class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="allow")   # 兼容未知字段
    input: List[Message]            # ★ 核心：用户提示词
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    stream: bool = True             # true=SSE 流，false=非流（任务模式）
    metadata: Optional[Dict[str, Any]] = None
```

### 2.6 AgentResponse（出站非流响应）

```python
class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: Optional[str] = None
    output: List[Message] = Field(default_factory=list)
    status: RunStatus = RunStatus.Completed
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
```

### 2.7 Event（SSE 事件）

```python
class Event(BaseModel):
    model_config = ConfigDict(extra="allow")
    # 字段视具体 envelope 类型而定，详见 §3.3
```

---

## 3. Agent 选择机制

### 3.1 三种方式（优先级从高到低）

| 优先级 | 方式 | 位置 | 示例 |
|--------|------|------|------|
| 1 | **URL 路径参数** | `/api/agents/{agentId}/...` | `POST /api/agents/my-agent-001/console/chat` |
| 2 | **HTTP Header** | `X-Agent-Id` | `X-Agent-Id: my-agent-001` |
| 3 | **配置文件默认值** | `config.agents.profiles.active_agent` | 回退 `"default"` |

> ⚠️ **Body 里传 `agent_id` 不生效**：`AgentRequest` 无此字段（`extra="allow"` 下传了也会被忽略）。

### 3.2 路由绑定

- **普通路由**：`/api/console/chat` — 通过 Header `X-Agent-Id` 指定 agent
- **Agent-scoped 路由**：`/api/agents/{agentId}/...` — 路径内嵌 agentId，语义最清晰
  - 由 `AgentContextMiddleware` 从路径中提取 `agentId` → 写入 `request.state.agent_id`
  - 子路由覆盖：`agent-status`, `chats`, `config`, `cron`, `mcp`, `mcp_oauth`, `skills`, `tools`, `workspace`, `console`, `plugins`, `checkpoints`

### 3.3 额外 Header

| Header | 说明 |
|--------|------|
| `X-Agent-Id` | 指定 agent 身份 |
| `X-Root-Session-Id` | 跨会话审批路由（用于 HITL 审批场景） |

---

## 4. 核心 API 端点

### 4.1 流式聊天接口

```
POST /api/console/chat
Content-Type: application/json
```

**请求体：**

```json
{
  "input": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "你好，帮我写一段代码" }
      ]
    }
  ],
  "session_id": "sess-2026-07-26-001",
  "user_id": "external-user-123",
  "stream": true,
  "metadata": { "source": "external-api" }
}
```

**返回：** `text/event-stream` (SSE 流)，Envelope 状态机详见 §4.3。

**关键行为：**
- `session_id` 不传时回退为 `"default"`，同一 agent 默认复用历史
- 显式传入唯一 `session_id` 才能创建隔离新会话
- `reconnect=true` 可挂载到正在运行的流（断线重连）

### 4.2 后台任务接口

```
提交任务:  POST /api/console/chat/task
查询结果:  GET /api/console/chat/task/{task_id}
```

**提交请求体：**

```json
{
  "input": [{ "role": "user", "content": [{ "type": "text", "text": "你好" }] }],
  "session_id": "sess-generated-by-caller",
  "user_id": "external-user-123",
  "timeout": 120
}
```

> 注意：`timeout` 是任务专用字段，不出现在 `AgentRequest` 中，因此 `post_console_chat_task` 接收 raw `dict` 而非 `AgentRequest`。

**提交响应（立即返回）：**

```json
{
  "task_id": "task-a1b2c3d4e5f6",
  "timeout": 120
}
```

**轮询响应（`finished` 时）：**

```json
{
  "status": "finished",
  "started_at": 1710000000.0,
  "result": {
    "status": "completed",
    "session_id": "sess-generated-by-caller",
    "content": [{ "type": "text", "text": "..." }]
  }
}
```

**异常响应：**

```json
{
  "status": "finished",
  "result": {
    "status": "failed",
    "error": { "message": "执行超时 / 错误信息" }
  }
}
```

> 执行异常和超时取消统一使用 `status: "finished"` + `result.status: "failed"`。

### 4.3 SSE Envelope 协议（`runtime/envelope.py`）

所有 SSE 事件携带单调递增 `_seq` 字段，与前端 `Builder.tsx` 严格对齐。

**生命周期序列：**

```
1. response.created    → { object: "response", status: "created", ... }
2. response.started    → 隐含在首个消息事件中
3. 流式块（反复发送）:
   text_start / text_delta / text_end
   reasoning_start / reasoning_delta / reasoning_end
   tool_call_start / tool_call_delta / tool_call_end
   data_start / data_delta / data_end
4. message.completed   → { status: "completed" }
5. 终止（三选一）:
   - 正常结束: envelope 自动 finalize
   - 取消:     cancel_envelope()
   - 错误:     error_envelope(text)
   - 保活:     heartbeat()  (长 idle 期间 keep-alive)
```

**Envelope 状态机：**

```
response.created → response.started → [text|reasoning|tool_call|data]_* → message.completed
                                                                        ↓
                                                              cancel / error / heartbeat
```

---

## 5. 完整请求示例

### 5.1 流式聊天（Header 指定 agent）

```http
POST /api/console/chat HTTP/1.1
Host: <qwenpaw-host>
X-Agent-Id: my-agent-001
Content-Type: application/json

{
  "input": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "你好，帮我写一段代码" }
      ]
    }
  ],
  "session_id": "sess-2026-07-26-001",
  "user_id": "external-user-123",
  "stream": true,
  "metadata": { "source": "external-api" }
}
```

### 5.2 流式聊天（URL 路径指定 agent）

```http
POST /api/agents/my-agent-001/console/chat HTTP/1.1
Host: <qwenpaw-host>
Content-Type: application/json

{
  "input": [{ "role": "user", "content": [{ "type": "text", "text": "你好" }] }],
  "session_id": "sess-2026-07-26-001",
  "stream": true
}
```

### 5.3 后台异步任务

```http
POST /api/console/chat/task HTTP/1.1
Host: <qwenpaw-host>
X-Agent-Id: my-agent-001
Content-Type: application/json

{
  "input": [{ "role": "user", "content": [{ "type": "text", "text": "帮我分析这段代码" }] }],
  "session_id": "sess-async-001",
  "timeout": 180
}
```

---

## 6. 会话与 Agent 切换速查

| 目标 | 操作 |
|------|------|
| 同 agent，继续上次对话 | 同一 `session_id`，`X-Agent-Id` 不变 |
| 同 agent，新开对话 | 新的唯一 `session_id`，`X-Agent-Id` 不变 |
| 换 agent，看同一份历史 | 同一 `session_id`，换 `X-Agent-Id` |
| 换 agent，全新开始 | 新 `session_id` + 新 `X-Agent-Id` |

---

## 7. 幂等性与重试契约

| 场景 | 策略 |
|------|------|
| POST 提交前明确失败（网络未送达） | 客户端可**显式重试** |
| POST 服务端已接收但响应不明（无幂等键） | 客户端**不得**自动重复 POST |
| poll 临时网络错误 | 允许在整体时限内继续重试 |

---

## 8. 其他路由清单（`app/routers/`）

| 路由前缀 | 说明 |
|----------|------|
| `/api/agents` | Agent 管理 |
| `/api/agents/{agentId}/...` | Agent-scoped 路由 |
| `/api/chats` | 会话 CRUD |
| `/api/config` | 配置（channels, heartbeat） |
| `/api/providers` | LLM Provider 管理 |
| `/api/skills` / `/api/skills_stream` | 技能 |
| `/api/tools` / `/api/tool_calls` | 工具与调用 |
| `/api/mcp` / `/api/mcp_oauth` | MCP 协议 |
| `/api/cron` | 定时任务 |
| `/api/console` | 控制台（聊天、任务） |
| `/api/auth` | 认证 |
| `/api/files` | 文件 |
| `/api/settings` | 设置 |
| `/api/backup` | 备份恢复 |
| `/api/git` | Git 操作 |
| `/api/coding-mode` | 编码模式 |
| `/api/local_models` | 本地模型 |
| `/api/loops` | 循环/门控 |
| `/api/market` | Skills Hub 市场 |
| `/api/token_usage` | Token 用量 |
| `/api/envs` | 环境变量 |
| `/api/fork` | Fork 项目 |
| `/api/voice` | 语音 |
| `/api/agent_stats` / `/api/agent_status` | Agent 统计与状态 |
| `/api/plugins` | 插件 |
| `/api/provider_oauth` | Provider OAuth |
| `/api/approval` | 人工审批（HITL） |
| `/api/frontend_plugin` | 前端插件 |
