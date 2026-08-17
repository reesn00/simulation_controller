# Phase 3 验证契约

每条 Criterion 的 verdict 为 `PASS/FAIL/INCONCLUSIVE/ERROR`。必选项聚合顺序固定为：

```text
FAIL > ERROR > INCONCLUSIVE > PASS
```

流水线顺序：响应契约检查、确定性 Validator、Claim 提取、工具证据、Semantic Judge、聚合。确定性结论不交给 Judge 重判；需要工具但没有 READY Provider 时返回 `INCONCLUSIVE`。

已实现 Validator：keyword all/any/none、JSON/list/table/card/text、真实字段/path、min_chars/min_items、排除平台语境、HTTP(S) URL 语法。

补充的 fail-closed 约束：

- 排除平台会检查每一次出现，不能用前文的否定描述掩盖后文推荐。
- Browser Evidence 的 `SUPPORTED` 只有在取得明确的 2xx/3xx 状态时才可按策略通过；4xx/5xx 为 `FAIL`，状态未知为 `INCONCLUSIVE`。
- `min_urls` 要求逐个取得证据并达到数量，不以第一个可访问候选代替整组验收。
- 播放进度只读取结构化 `progressed=true`，其他字段中的 `true` 不得造成误判。
- 外部文件操作没有 READY 的 `filesystem.inspect` Provider 时为 `INCONCLUSIVE`，远端自述不能作为执行成功证据。

Semantic Judge 每次使用独立 CAMEL Agent、最低温度、严格 JSON Schema 和不可信数据边界。解析、传输、ID 遗漏或重复失败时返回 `ERROR`，不存在 fail-open 路径。

## Catalog v2 修复契约

每条 Criterion 编译 `remediation.owner/guidance/retryable`。验证器只判断事实；是否继续对话由任务契约决定：

- `executor`：远端内容可以修复，FAIL 可进入下一轮自然追问。
- `simulator`：模拟端自身逻辑问题，不向远端追责。
- `environment`：工具、网络或运行环境问题，保持 ERROR/INCONCLUSIVE 并终止当前 Run。
- `user`：需要真实用户补充的外部信息，模拟端不得编造。

Semantic Judge 返回的 `retryable` 不再拥有最终决定权。ValidationPipeline 会按 Criterion remediation 归一化 FAIL 的重试语义，避免一个可补充的语义缺口直接结束任务。

InteractionActor 每轮只选择 `max_gaps_per_turn` 个 executor-owned 缺口，优先使用针对当前 reason code 的 Scenario guidance，再使用 Criterion guidance，最后才使用通用兜底话术。已经 PASS 的项目不会要求远端重复处理。
