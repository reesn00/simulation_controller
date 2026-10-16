# INC46 · rationale「动作前动机」口径改造方案

## Context

P1 存档 `steps[i].rationale` 目前是**事后反思**：writer 生成时喂了 `action[i]+observation[i]`，
文本描述刚看到的页面。P3 训练视图把 rationale[i] 放在 `tool_call(action[i])` **之前**，
造成时序倒置——模型还没导航就先评价页面内容，训练会教「未卜先知」
（E005__e35b7455__site-4 实测暴露，owner 确认顺序反了）。

**owner 拍板（2026-10-15）**：改采集层 writer 口径，把 rationale 变成**动作前动机**——
生成时不喂当前步观察，只喂历史（之前的动作+观察）与当前动作。P3 渲染顺序不动
（rationale[i] 配 action[i] 在新口径下因果自然成立）。

四项子决策（已拍板）：
1. 终局反思：writer 加终端模式 `write_reflection`，落 **unit 级新键 `reflection`**（与 steps 平级），收尾消息继续带自然语言结论
2. `write_one` 的 `error` 参数**彻底删除**（动作未执行谈不上失败；error 本就在 P1 `steps[i].error` 落盘）
3. D-9 few-shot 六例整体移除，**零样本出分**并在 verdict reason 标注 `fewshot=none`，示例池留待重生成后人工从 manual-review 桶重选
4. 存量 480 条 rationale 重生成后**原地改写** P1 存档，原档备份到 `output/pipeline/_backup_pre_motivation/` 子目录（不会被顶层 `*.json` glob 误拾）

## 改动清单

### 1. writer 新口径 — `rationale/writer.py`

- **SYSTEM（199-213 行）重写**：「每一步你会拿到：用户想要什么、此前各步的动作与页面返回、本步即将执行的动作。写执行这个动作**之前**的动机与预期」。硬约束：只根据历史与任务写、不得声称已看到本步结果；最近历史观察不完整时明说不确定；禁止事后口吻；200 字以内。输出 JSON `{thinking, conclusion}` 形状不变。
- **`write_one` 签名**：`(client, *, action, user_prompt="", target_title="", history=())`——**删 `observation` 与 `error`**；`history` 改为 `(action_json, obs_json)` 对序列。
- **用户模板变量位（340-357）**：删 `observation`、`this_action_failed`；`observation_completeness` 改指**最后一条历史观察**的 shortfall（无历史不出该键）。
- **`render_history`（293-300）扩成动作+观察**：历史观察用**瘦身形状**——只留 `url`/`page_title`/`body_text`（截 `HISTORY_BODY_CHARS≈500`）/`body_truncated`/`elements_total`，不带 elements/links 明细；**只最近 5 步带观察，更早只留 action**（控输入量防超时纪律不变）。
- **新增 `write_reflection`**：`(client, *, history, outcome, user_prompt="", target_title="")` → `WriteResult`；SYSTEM 变体（终局总结口径：看完全部历史与最终判定，写一段事后总结）。`WriteResult.to_json` 形状不变。

### 2. 调用点 — `executor/orchestrator.py`

- `_write_rationales`（796-816）：history 改 `[(s.action.to_json(), _obs_json(s.observation)) for s in steps[:index]]`；删 `observation=`/`error=` 实参。
- 逐 unit 结束后追加一次 `write_reflection`，落 **unit 级 `reflection` 键**（搜索单元 record / 站点单元 result 各自级别，与 steps 平级）。失败记账纪律同款：异常不抛，落 `status=unavailable`。

### 3. 闸门窗口迁移 — `rationale/gates.py`

- `gate()`（408-409）：`visible = observations[:step_index]`，`later = observations[step_index:]`——本步观察从「当时可见」挪到「后见」。
- `check_entities`（161）/`check_temporal_at`（280）：窗口同步缩到 `[:step_index]`，docstring「本步及前置步」改「前置步」。
- **step 0 空窗口显式早退**：窗口为空时实体/时序核查返回 undeterminable（「首步无历史观察，核查不适用」），不依赖隐式路径——goto 首步动机合法引用只有 user_prompt/target_title，绝不能判幻觉。
- `assembler/schema.py gate_of`（257-263）：调用签名不变，docstring 口径同步。

### 4. D-9 迁移 — `evaluation/scorers/d9_scorer.py`

- `_render_user`（562-608）：rationale step i 的锚定观察窗口从 obs[0..i] 缩到 **obs[0..i-1]**；step 0 渲染「（无前置观察，只有任务与动作）」。
- 系统提示（471-474）改「动作前动机的忠实翻译，锚定此前观察」。
- **few-shot 六例（356-466）整体移除**：留空列表常量 + docstring 注明「待存量重生成后从 manual-review 桶人工重选（铁律：不挑 ai-assisted-* 桶）」；judge 零样本出分，verdict reason 标注 `fewshot=none`。

### 5. reflection 进 P2/P3 — `assembler/schema.py` + `assembler/views.py`

- `split_archive`：抽 unit 级 `reflection` 进 Sample（新可选字段，形状同 WriteResult）；Sample dataclass + `to_json` 加键；P2 契约键集同步。
- `views._closing_text`（365-389）：结论来源从「末步 rationale.conclusion」改读 **`sample["reflection"].rationale.conclusion`**；无 reflection 时退回纯 disposition 话术。`to_meta` 的 `closing_summary` 逻辑不变。

### 6. 存量重生成 CLI — `executor/cli.py`

新子命令 **`regenerate-rationales`**：
- 遍历 `select_archives(root)`，逐 unit 重建 `steps[i].rationale`（history 从 `steps[:i]` 现构）+ unit 级 `reflection`；复用 `_make_rationale_writer()`（178-216）的 client 构造。
- **备份**：首次写回前把原档复制到 `output/pipeline/_backup_pre_motivation/<name>`（子目录，避开顶层 `*.json` glob；已存在即跳过）。
- **原子写回**：archive.py 的 `.tmp`+`os.replace` 同款。
- 单步失败落 `status="unavailable"+reason` 不中断整批；每档一行 summary（written/unavailable 计数）。
- 支持 `--only <archive>` 与 `--dry-run`。
- 规模：64 档 × 480 step + 约 100+ 次 reflection ≈ **600 次 LLM 调用**（需 `TRAJECTORY_LLM_*` 环境变量）。

### 7. 测试

- `test_writer.py`：`TestPromptContent` 反转（当前 obs **不**进 payload、历史步含瘦观察、早步只留 action）；删/改 `Test动作失败进提示词`；`TestShortfall` 改指最后一条历史观察；新增 `TestStep0EmptyHistory`、`TestHistoryObservationSlim`、`TestWriteReflection`。
- `test_rationale_gates.py`：`Test实体核查`/`Test时序核查` 窗口断言全改 `[:i]`；新增 step0 空窗口不判幻觉、本步观察实体被判 temporal 泄漏的正例。
- `test_d9_scorer.py`：`TestFewShot注入点` 改空示例池断言；新增 `_render_user` 窗口对位测试。
- `test_assembler.py`/`test_views.py`：reflection 进 Sample、收尾读 reflection（有/无两路）。
- `test_visit.py:2199-2202`、`tests/mutation_check.py:163`：P1 rationale 形状不变，确认即可；mutation_check 白名单加 unit 级 `reflection` 键。
- 新增 `test_cli_regenerate.py`：备份只建一次且在子目录、单步失败不中断、原子写、dry-run 不写盘。
- `test_p2_contract.py`：Sample 键集加 `reflection`。

### 8. 文档

- `writer.py` 模块 docstring 七条口径重写（动作前动机、不喂当前观察、历史瘦身、error 移除理由、终端反思）。
- `gates.py` docstring 窗口语义；`contracts/P3-views.md` 八条判断中 rationale 条目改「动作前动机」（渲染顺序不变）+ 收尾来源改 reflection；P2 契约加 `reflection` 键。
- `docs/计划进展/05-rationale与闸门域.md` 新增 INC46 节；`90-增量编年史.md` 追加「增量四十六」；域索引表同步；项目记忆更新。

## 风险点

- **step0 空窗口**是全链路最容易误伤的位置（gates 判幻觉 / D-9 判无锚），必须有显式早退+测试钉住。
- D-9 空 few-shot 期间分数口径变化（零样本），报表侧需知情。
- 重生成后旧 rationale 来源的派生物（INC40 few-shot 六例出处、INC41 κ=0.176 口径）失效，文档注明。
- reflection 是 LLM 产物，落 unit 级独立键而非 outcome 内——outcome 保持采集事实层纯洁。

## 验证

1. 全量门禁绿（基线 2328 passed，预期净增 ~25 条）。
2. `regenerate-rationales --dry-run` 打印将重写步数；`--only E005__e35b7455.json` 单档真跑。
3. 重建 E005 的 P2/P3，核验 site-4 消息流因果链：msg2=「打开候选链接验证」类动机+goto → tool(obs0) → msg4=「页面列出8期链接，点第一期确认」类动机+click → tool(obs2) → msg6=reflection 结论+处置。** assistant 不再出现「评价未见页面」**。
4. 全量 64 档重跑（~600 次 LLM 调用）→ P2 304/P3 912 重建 → scout 重跑对比分流变化。
5. check-archives 不红（integrity 不校验 rationale 内容，只扫 U+FFFD/凭据）。
