# 18 · 金标修正：14 条 gold_defect 逐条判定与改判方案

**上游**：[16-评估器与rubric双向校准回路方案.md](16-评估器与rubric双向校准回路方案.md)（六桶分诊 · 金标修正窗口）· [在线视频场景rubric.md](在线视频场景rubric.md)（D-5 决策树 · v1.2 付费页边界）
**状态**：✅ 已执行 · 2026-10-24 · v10 已冻结（改 label 用 `--refresh-label`，非 `--refresh-gold`）

---

## 0. 一句话

分诊档（`triage.jsonl`）判定 calibration 档 33 条 D-10 分歧里 **14 条是金标错**（`gold_defect`，42%）。本文件把这 14 条逐条判清"该改判成什么"，并给出**改判收益的诚实预期**。

**⚠️ 最重要的结论先放前面**：核掉这 14 条金标错，D-10 一致率**只从 74.2% → 78.1%**（calibration 档，+4pp），**到不了 90%**。因为 14 条里只有 5 条是"评估器本来就给对、纯金标拖累"，其余 9 条的金标改对了，pred 侧（评估器给的 `unresolved` / `login_wall_or_blocked`）**仍然不对**——那暴露的是评估器侧的问题（11 条 `evaluator_logic`），得靠下一步改判据才能救。

**金标与评估器是纠缠的，必须两侧一起修**。本文件只处理金标这一侧。

---

## 1. 判定依据（三条交叉印证）

每条改判都同时满足：

1. **rubric 明写条款**：D-5 决策树（见下）；
2. **等价类**（`alignment.EQUIV_CLASS`）；
3. **分诊证据原文**（`triage.jsonl` 的 `triage_evidence`，由 owner `reesn` 填写）。

### D-5 决策树（rubric v1.2 明写）

```
非播放站/无关站    → not_play_site
传输层失败（没打开）→ unreachable_hard      ← 采集失败，不是站点性质
地区/DRM/验证码/反爬 → blocked
是播放站，无播放入口 → no_play_control
播放页无 video/组件  → component_unverified
播放页但需订阅/付费  → subscription_wall
```

v1.2 付费页边界：**导航栏「开通会员」不是付费墙**。

### 等价类（关键映射）

| label | 等价类 |
|---|---|
| `subscription_wall` | PAYWALL |
| `login_wall_or_blocked` / `blocked` | ACCESS_BLOCKED |
| `no_play_control` / `component_unverified` | NO_PLAY_RESOURCE |
| `unreachable_hard` | REACH_FAIL |
| `not_play_site` | NOT_PLAY_SITE |
| `success` | SUCCESS |
| `unresolved` | UNKNOWN |

---

## 2. 14 条逐条判定表

| # | golden_id | 现金标 | pred | 改判为 | 依据 | 改判后 |
|---|---|---|---|---|---|---|
| 1 | E005__e35b7455__feb5257a | not_play_site | component_unverified | **success** | 爱奇艺综艺频道列表页是播放站，**owner 裁定** | ❌ 仍不一致（pred 错）|
| 2 | E017__a2878b23__95f0a368 | login_wall_or_blocked | unreachable_hard | **unreachable_hard** | mytvsuper 访问 500 = 传输层失败 | ✅ AGREE |
| 3 | E023__4d175424__a7f0eaaf | login_wall_or_blocked | unresolved | **unreachable_hard** | bilibili 不可达 = 传输层失败 | ❌（pred 是 unresolved）|
| 4 | E014__f50809fb__7493f4e0 | login_wall_or_blocked | unresolved | **unreachable_hard** | ixigua 访问失败 = 传输层失败 | ❌（pred 是 unresolved）|
| 5 | E023__4d175424__9d49aca1 | subscription_wall | unresolved | **component_unverified** | 无付费证据，D-5 第三行 | ❌（pred 是 unresolved）|
| 6 | E014__f50809fb__dec0b09f | subscription_wall | unresolved | **component_unverified** | 无付费证据（仅导航级会员）| ❌（pred 是 unresolved）|
| 7 | E021__94a1392f__2ab12498 | subscription_wall | component_unverified | **component_unverified** | 评估器对，金标错 | ✅ AGREE |
| 8 | E017__a2878b23__9a9dc9bf | subscription_wall | component_unverified | **component_unverified** | 评估器对，金标错 | ✅ AGREE |
| 9 | E021__94a1392f__2b9c7618 | subscription_wall | no_play_control | **no_play_control** | 评估器对，金标错 | ✅ AGREE |
| 10 | T014__acaa3ef1__22e0fb36 | component_unverified | login_wall_or_blocked | **subscription_wall** | 正文「正片 会员」付费证据 | ❌（pred 错）|
| 11 | T029__0a800a3f__22e0fb36 | component_unverified | login_wall_or_blocked | **subscription_wall** | 有付费证据 | ❌（pred 错）|
| 12 | T031__3af4e21b__f9895792 | component_unverified | login_wall_or_blocked | **blocked** | 错误码 70017001.1 = 地区限制 | ✅ AGREE（同 ACCESS_BLOCKED）|
| 13 | T031__de0c1df5__22e0fb36 | component_unverified | login_wall_or_blocked | **subscription_wall** | 有付费证据 | ❌（pred 错）|
| 14 | T040__c9982a1c__22e0fb36 | component_unverified | login_wall_or_blocked | **subscription_wall** | 有付费证据 | ❌（pred 错）|

**改判后 5 条 AGREE**（#2 / #7 / #8 / #9 / #12），其余 9 条 pred 侧仍不对。

---

## 3. 改判收益（诚实预期）

| 口径 | 改判前 | 改判后 |
|---|---|---|
| calibration 档 | 95/128 = 74.2% | **100/128 = 78.1%** |
| 全站点级 | 215/289 = 74.4% | **220/289 = 76.1%** |

**只 +4pp，到不了 90%。** 剩下的 9 条（金标改对了 pred 仍错）+ 11 条 `evaluator_logic`，是评估器侧的账，需改判据。

---

## 4. 三条观察（改了这 14 条之后才看得见）

1. **「无付费证据 → component_unverified」是一组系统性金标错**：#5 #6 #7 #8 四条 `subscription_wall` 都被判成"无付费证据"，根因是 **v1.2 付费页边界（导航栏会员≠付费墙）在金标标注时没被遵守**。这是标注流程问题，不是单条偶然。
2. **「auto-validator 按 DOM video=0 贴 component_unverified」是另一组**：#10 #11 #13 #14 四条 T 系列，全是 `auto-validator-l3l4-v1-no-text-signal` 自动贴的，正文明明有「会员」付费文案却贴成了 component_unverified。**同一 URL 下 8 个 hash 段 label 分散**（M-30 金标不自洽的实证）。
3. **pred 侧 `unresolved` 是评估器「记账降级」的产物**：#3 #4 #5 #6 四条 pred 都是 `unresolved`，对应分诊证据里写的「评估器落 unresolved 是它自己『点击前后同页 ⇒ 前置事实不成立』的记账降级规则」——这是评估器侧的另一笔账（属 evaluator_logic）。

---

## 5. 执行步骤（已执行 · 2026-10-24）

```bash
# 1. 确认 annotate 文件（已生成，14 条）
#    evaluation/golden/data/gold_defect_fix_14.jsonl

# 2. 合并修正（round=1 已有 label，不一致需 --force）
python -m trajectory_pipeline.evaluation.golden.builder annotate \
    --input evaluation/golden/data/gold_defect_fix_14.jsonl --force

# 3. 重冻结新基线（继承式 + 只重挂 label）
#    ⚠️ 注意：改 label 不能用 --refresh-gold（那只重挂软维度分数，label 一字不碰）
#    必须用 --refresh-label（本次新增的冻结出口，只重挂 golden.label 等，回归集原样冻结）
python -m trajectory_pipeline.evaluation.golden.builder freeze \
    --version 10 --from-version 9 --refresh-label

# 4. 重出快照 + 分歧卡，看新一致率
python -m trajectory_pipeline.evaluation.golden.builder agreement \
    --version 10 --emit-disagreements
```

**⚠️ 改金标 = 开新版本，不是就地改**（方案 16 铁律 2）。旧版本 v9 留档不动，v10 是新基线。

**执行结果（2026-10-24）**：
- annotate 14 条 label 全改对（success/unreachable_hard/component_unverified/no_play_control/subscription_wall/blocked）。
- v10 冻结成功，**回归集指纹 `937a45f25eea` 与 v9 一致**（C-17「回归集一经冻结不得重切」守住），`label_refresh.changed_rows=14`。
- **D-10 一致率 74.2% → 78.1%**（calibration 100/128），与 §3 预测的 +4pp 完全一致。

---

## 6. 待办

- [x] owner 逐条复核 §2 的 14 条判定（尤其 #1 E005 的 success、#12 的 blocked）
- [x] 确认后执行 §5 的 annotate + freeze
- [ ] 评估器侧 11 条 evaluator_logic + 9 条「金标改对但 pred 仍错」的判据修复（下一步）——78.1% 仍离 90% 差约 12pp，剩余账在评估器侧
