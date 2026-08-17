# Phase 2 异步运行与状态机

主链路为 `BatchRunner -> TaskRuntime -> InteractionActor/ExecutorGateway/ValidationPort/RunRepository`。

终态包括：`SUCCESS`、`GUIDE_EXHAUSTED`、`INCONCLUSIVE`、`VALIDATION_ERROR`、`EXECUTOR_ERROR`、`ACTOR_ERROR`、`CANCELLED`、`INTERRUPTED`。所有迁移由普通 Python 显式迁移表控制，非法跳转抛出 `InvalidStateTransition`。

计数语义：

- `executor_turns`：收到的远端执行回复数。
- `guide_rounds`：成功完成的追问提交/回复轮数，首轮不计入。
- 每轮 ValidationReport 独立保存。

HTTP 使用单一生命周期 `httpx.AsyncClient`。submit 和 poll 继承同一 execution agent ID；POST 可能已被服务端接收但响应不明时，没有幂等键就不重试。
