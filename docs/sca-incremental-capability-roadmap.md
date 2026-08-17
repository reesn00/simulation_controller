# SCA 增量能力路线图

> 状态：已确认，作为现有架构的增量演进路线，不替换当前三层主链路
> 来源：`C:\Users\shenr\Downloads\参考方案.md` 与 2026-08-17 架构评审结论

## 1. 定位与边界

当前生产主链路保持不变：

```text
CatalogLoader / TaskCompiler
  -> TaskRuntime / RunStateMachine
  -> InteractionActor + ExecutorGateway
  -> ValidationPipeline
  -> JsonRunRepository
```

参考方案只作为增量能力路线图。新增能力必须复用现有领域契约、显式 Python 状态机、四态验证和 v2 审计记录，不引入第二套 SCA 主控框架。

明确不在当前阶段引入：

- LangGraph 或由 LLM 控制的状态迁移。
- Zvec、嵌入模型或新的向量数据库依赖。
- Docker 代码沙箱；当前任务以检索和内容验证为主，尚无稳定 Artifact 传输契约。
- 自由文本思维链采集、持久化或训练集导出。
- 运行时任意修改验收标准。

## 2. 已确认优先级

| 阶段 | 能力 | 实施原则 | 验收 |
|---|---|---|---|
| P0 | 响应与数据清洁边界 | 优先消费可见文本块，忽略结构化 reasoning/tool 块；无法可靠区分时不进入蒸馏集 | 推理块不进入 `ExecutorResponse.text`；疑似内部推理的成功 Run 不进入 distill |
| P1 | 分阶段验证 | 先完成所有廉价确定性验证；存在必选硬失败时延后工具和 Judge | Judge/Provider 不为明显格式或数量失败付费；报告仍覆盖全部 Criterion |
| P1 | 运行熔断 | 使用现有 `GUIDE_EXHAUSTED` 终态表达重复响应、重复失败和时间预算耗尽 | 触发原因、阈值和签名进入结构化事件与 FailureRecord |
| P1 | 分级引导 | 根据已完成追问轮次确定 L2/L3/L4，引导等级只影响表达策略和审计，不改变验收权 | `FOLLOWUP_CREATED` 记录 `guidance_level` |
| P1 | 结构化决策 | 将 PASS/FAIL/ERROR/INCONCLUSIVE/重试/熔断选择记录为结构化 action/reason | 不保存自由文本思维链；状态行为与现有契约兼容 |
| P2 | 多样性与负样本 | 建立独立 synthesis 层，所有变体带 provenance，不污染正式 Catalog | 可复现、可关闭、可按变体统计质量 |
| P3 | 经验策略 | 先按 reason code、task type、model version 统计规则效果；有足够数据后再评估向量检索 | 至少数百条有效 Run，并通过对照实验证明收益后才引入向量依赖 |

## 3. 当前实施拆分

### T1 - 响应清洁门禁

- 范围：QwenPaw 文本块提取、蒸馏清洁检查、合约与仓储测试。
- 契约：只把可见 `text` 块交给 Validator；`reasoning`、`thinking`、`analysis`、工具块不进入可见回复。
- 兼容：纯字符串响应和现有 `<think>...</think>` 响应继续支持。
- 回滚：移除共享内容策略并恢复原有 `_THINK_RE`。

### T2 - 分阶段验证

- 范围：`ValidationPipeline` 与验证单测。
- 契约：执行全部确定性规则；必选确定性 FAIL 时，将尚未执行的昂贵准则标记为 `DEFERRED_AFTER_HARD_FAIL` / `INCONCLUSIVE`。
- 回滚：恢复单循环依次执行所有 Validator/Provider/Judge。

### T3 - 运行熔断与结构化决策

- 范围：`TaskRuntime`、内部运行策略、状态事件和运行单测。
- 契约：不增加终态、不修改 Run Schema；重复响应、重复失败和可选时间预算使用 `GUIDE_EXHAUSTED`，但写入独立 failure code。
- 回滚：移除可选 guard policy，保留原 `max_guide_rounds`。

### T4 - 分级引导

- 范围：InteractionContext/UserUtterance、Actor、Prompt 与测试。
- 契约：首轮失败 L2、第二次 L3、第三次及以后 L4；等级进入事件，不暴露给远端为内部 ID。
- 回滚：UserUtterance 恢复无等级字段，Actor 使用原提示。

## 4. 延后项与触发条件

- Token 预算：等待远端 Executor 提供可靠 usage 元数据；不以字符数伪装 Token。
- 模糊结果“接受并适配”：等待任务契约为可接受替代路径定义可机器判断的条件；当前只记录不确定性，不动态改 Criterion。
- 经验库：真实成功/失败轨迹数量达到统计门槛后，以离线实验决定实现。
- 沙箱验证：执行端提供代码或 Artifact 的稳定消息协议后再设计。

## 5. 验证矩阵

- 聚焦：Executor 合约、Repository 蒸馏门禁、Validation Pipeline、Runtime、Guidance。
- 全量：`python -m pytest -q`。
- 配置：`python -m simulate_serve --validate-config`。
- 就绪度：`python -m simulate_serve --readiness`；外部 Provider 缺失可记录为环境限制，不得伪造通过。
