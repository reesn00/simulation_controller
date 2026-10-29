# research/ · 研究产物与复算脚本

> **这里放证据，不放结论，也不放产品代码。**
> 结论在 [`../docs/`](../docs/)（现状/编年史/避坑），产物在 `trajectory_pipeline/output/`。
> 本目录是第三段：**「当初那批数字是怎么算出来的」**——脚本 + 它读的落盘数据。
>
> 2026-10-20 由仓库根的 `tmp/`、`tmp_*`、`.tmp_inc46_probe/`、`plan-ab.json`、
> `_test_short.mp4` 迁入（35 个文件）。迁移前的来龙去脉见
> [09-门禁与纪律域 §迁移](../docs/计划进展/09-门禁与纪律域.md)。

---

## 是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 一次性分析脚本 + 它们读写的 `.jsonl` 证据 | 生产代码（`../executor/`、`../perception/`…） |
| 可重跑的复算入口（多数离线） | pytest 测试（门禁树只有 `tests/`，本目录**不被收集**） |
| 某批结论的**取证现场** | 结论本身（结论在 docs，改结论不改这里） |

**为什么没放 `tests/fixtures/`**：`tests/` 是门禁树（`pyproject.toml` 的 `testpaths`），
而 `visit.py` 要联网 + 真浏览器，放进去会让「门禁覆盖范围」与「树内容」不再一一对应
（gdr/tests 那个教训的同款）。且 `.jsonl` 是**运行产物/证据**，而 fixtures 语义是
「被测对象的输入」，方向相反。

---

## 目录

```
research/
├─ recollect/                  直采重采主线（INC42 起；INC43–INC46 复用同一份 outcomes）
│  ├─ manifest.json            90 行并集的 task → title / URL 清单（直采输入）
│  ├─ visit.py                 直访 90 行并集，产出 outcomes.jsonl（**联网 + 真浏览器 + LLM**）
│  ├─ measure.py               离线复算：按 (task_id, url) 回填、重算 D-10 投影
│  ├─ apply_code_writeback.py    离线：把重采 outcomes 的新判定写回 candidates.jsonl
│  │                            的 code（v12→v13 一次性；跑完由 freeze --refresh-code 挂链）
│  ├─ outcomes.jsonl           **当前有效轮**（INC43 轮，75 对直采命中 79 行）
│  ├─ plan_from_corpus.py      离线：**从存量存档反推「重跑计划」**（M-44 后那批 28 条）
│  │                            同一任务实例重跑（不是 `cli gen` 重采样新任务）
│  ├─ plan_hardfail_rerun.py   离线：**从批次存档反推「硬故障重跑计划」**（增量九十六那 26 条）
│  │                            ⇒ 只收「同参数重跑就会不同」的那族（B1/B2/B4a），
│  │                            B4b（token 上限撞顶）与 B3/B5/B6 归「先调参」
│  ├─ compare_hardfail_rerun.py  离线：**新旧两批对照**——那批硬故障现在有没有变成真结论
│  │                              （对齐 = **两边各算 title + host，任一相等即配上**：
│  │                              旧行多是跳转链⇒只有标题，新行多是解析后地址⇒只有 host，
│  │                              各取一个键**永远对不上**；证据档分强弱，**软证据不与强证据混算**）
│  └─ rounds/                  历史轮次快照，**已被 outcomes.jsonl 取代**，仅留痕
│     ├─ inc42_backup.jsonl       INC43 修复前的基线
│     ├─ inc43_backup.jsonl       INC44 A/B 的「旧轮」
│     ├─ inc43_current.jsonl      当时的轮内中间态
│     └─ inc44_falsified.jsonl    INC44 被证伪方案那一轮的留存（簇 C 分析用它）
├─ retest/                     人审 / judge 重测线（INC37–INC42）
│  ├─ inc37_retest_input.jsonl  round=2 输入
│  ├─ inc40_prep_retest_worksheet.py   从存档抽 (observation, rationale) 对 → 工作表数据
│  ├─ inc40_retest_worksheet_data.jsonl
│  ├─ inc41_dump_anchors.py / inc41_dump_two.py   导出锚点判据面（盲打用）
│  ├─ inc41_judge_retest.py + .jsonl   强制触发 D-9 judge，连跑两轮量 A/B 稳定性
│  ├─ inc41_write_retest.py / inc41_ws_compact.py
│  ├─ inc42_analyze_retest.py / inc42_classify_shortfall.py
│  └─ …
├─ probes/                     探针与 A/B 分析（INC43–INC46、INC57–INC59）
│  ├─ inc43_probe_body.py + inc43_probe_bodies.jsonl   18 行失败簇直采正文，验「②③漏付费墙」假设
│  ├─ inc43_ab_compare.py      INC42 轮 vs INC43 轮
│  ├─ inc44_ab_compare.py / inc44_ab_golden_check.py / inc44_check_t011.py
│  ├─ inc44_unresolved_analysis.py   unresolved 36 行构成分层
│  ├─ inc45_find_mojibake.py / inc45_probe_sohu.py / inc45_probe2_sohu.py
│  ├─ inc46_E005_before.json   INC46 动作前动机口径的单档前后对照（原 `.tmp_inc46_probe/`）
│  ├─ cluster_c_landing_check.py     簇 C 词表覆盖率前置核对（INC59）
│  ├─ click_*.json / new_tab_click.json   P5「点击超时」七轮探针的**原始读数**
│  │                                     （脚本已退役 2026-10-26，见 [§退役配方](#退役配方-2026-10-26-执行层换-camofox)）
│  ├─ inc72_paired_compare.py         双跑**配对复算**（按落地 URL 配对；分母只取两面都测到的页面）
│  ├─ inc72_ladder_identity.py        元素阶梯**逐观察恒等式**核对（`candidates == hidden + visible`）
│  ├─ inc73_human_review.py + .json   ★**判据⑤ 人判工作表**（负样本成因封闭集 17 项 + 动作可移植性）；
│  │                                 留池列由 `cause` 派生，冲突在 `keep_conflicts` 点名
│  ├─ inc73_human_review.filled-original.json   首轮**人填原件**（留池列还在手填时：yes 28 / no 11），审计对照
│  ├─ inc73_field_check.py            判据⑤ 的**现场取证**（重采一遍与存档并排；联网）
│  ├─ corpus_inventory.py             离线：盘上 P1 存档**按 task 汇总**（份数/面/形态/分支/耗时）
│  ├─ camofox_nav_failure_codes.py    **M-44 机器码词表实测**（camofox 面：DNS 失败/拒连/超时/打断）
│  ├─ obscura_nav_failure_codes.py    同上（obscura 面；**绕过驱动的 200 字截断**直打 MCP）
│  └─ inc73_action_replay.py          判据⑥ 的**动作重放**（从存档 steps[] 定位重放；联网）
│  ├─ （文档代号 / 动作落定两个探针**已退役** 2026-10-26，
│  │     配方与读数落点见 [§退役配方](#退役配方-2026-10-26-执行层换-camofox)）
│  ├─ probe_settle_readings.py        存档里**有多少观察是「读到一半」的**（离线；`--exclude` 排掉批次子目录）
│  │  ├─ settle_readings_prefix.txt   **修复前**那一代的读数（24 份 / 388 观察 / 过渡态 59（15%）/ 打架 6）
│  │  └─ settle_readings_after.txt    **修复后**（第二批 12 份 / 218 观察 / 过渡态 8（3%）/ 打架 1 / nav_unsettled 0）
│  ├─ probe_batch_rates.py            一批存档的**判据①（真·反爬拦截率）与判据②（`body_text`）**（离线）
│  │  ├─ batch_rates_prefix.json      **修复前**那一代的读数（真·反爬 9.03% / body 778.0）
│  │  └─ batch_rates_two_generations.json  两代对照（**修复后 8.55% / 800.5**；⚠️ 修复前那代已整代回滚 ⇒ 不可重算）
│  ├─ compare_generations.py          **两代存档逐 URL 对照**：同一页两代结论互相矛盾的条目（离线）
│  │  └─ generation_collision.json    两代对撞读数（112 个共享 (task,url) 对 / 17 处结论矛盾）
│  ├─ merge_batch_pool.py             批次后处理：把子目录里的 **P1 存档展平 + 负样本池并进根目录**（默认 dry-run）
│  ├─ camofox_*.py                    第 5 轮的**执行层取证探针**（导航时序 / 销毁页自愈 / 建页与导航两条路由
│  │                                  分开 / 归因 / 离线扫存档五验收判据 / 采集侧账）——见下面「怎么跑」
│  ├─ camofox_r5_uh_recheck_*.json    **并发批里 6 条 `unreachable_hard` 的逐条单独复核**（3 轮重采）
│  │  ├─ camofox_r5_uh_recheck_targets.txt   5 条 URL（从存档**程序化取**，不手抄）
│  │  ├─ camofox_r5_uh_recheck_r1.json       第 1 轮（3 条**打得开** + 2 条**跳转链活着**）
│  │  ├─ camofox_r5_uh_recheck_r2.json       第 2 轮（看两条链接**交替**报两种传输层错误）
│  │  └─ camofox_r5_uh_recheck_r3.json       第 3 轮
│  ├─ camofox_r5_step6_live_check.json      停点第 ⑥ 步那 **4 条 agent 负样本**的现场重采
│  │  ├─ camofox_r5_step6_live_check_targets.txt   4 条 + M-43 那条
│  │  └─ camofox_r5_step6_archive_vs_live.py + .json  **存档正文（当场）vs 现场正文（今天）**并排，
│  │                                     各自跑一遍仓库自己的 `paywall_evidence` ⇒ 用来分「标签当时对不对」
│  │                                     与「判据抓不抓得住」（iqiyi 那条：标签对，判据抓不住）
│  ├─ paywall_widening_measure.py + .json  **Q-20 ② 的实测**：`paywall_evidence` 的「同句」放宽到多宽
│  │                                     （离线；**1160 段存档正文 + 22 行金标候选**）
│  │                                     ⇒ 三个候选（换行不切句 / 邻窗 ±1 / 邻窗 ±3）各量「新增命中 / 丢失命中」
│  │                                     ＋ iqiyi·youku·v.qq 三个验收样例 ＋ 金标回归
│  │                                     ⚠️ **只改粒度、不动词表**（动词表 = 口径变更 ⇒ 按 D-C7 连带改 prompt + 重跑 A/B）
│  ├─ probe_m29_d5_two.py + m29_d5_two.json   **D-5 判 2 分的样本逐条摊开**：正文原话（`body_text`）
│  │                                     + 命中的形态词 + 形态组合（`defect_only` / `paywall_only` /
│  │                                     `both` / `neither`）+ 裸 CTA 的同句语境词（离线；M-29 收口用）
│  └─ m30_live_check.py + m30_live_check.json + m30_live_check_run2.json
│                                     **M-30 现场重采**：把 7 个待核 URL **真的重新打开看**
│                                     （camofox；**联网**），现场读数与 P1 存档读数**并排**输出
│                                     （body 字数 / video / iframe / 关键词命中）——
│                                     用来判「现场≠存档」是**站点漂移**还是**观察时机太早**
│                                     ⚠️ **每站读两次**（「导航一返回就观察」＋「等 4 秒再观察」，
│                                       `--settle` 可调）；**两读之差就是「渲染时机」的证据**
│                                     ⚠️ 两个 json 是**两轮独立重采**（独立证据，不是同一份复制）
│                                     ⚠️ 2026-10-26 已随执行层换 camofox（`--headful` 撤）
│  ├─ camofox_q23_uh_recheck.py + .json    **Q-23 第一轮**：池里存量 54 条 `unreachable_hard` 的
│  │                                        **29 个 URL** 逐条真浏览器复核（**联网**；每 URL 最多 3 次）
│  ├─ camofox_q23_uh_settle.py + .json      **Q-23 补测**：给**第一轮没打开的全部 12 个 URL** 统一加试 6 次
│  │                                        ⇒ 取证量一致 ⇒ 判据可统一套用（**联网**；125 次尝试的另一半）
│  ├─ q23_dispose_uh_rows.py + .json        ⚠️ **改生产池**：把裁定「不是真·站点不可达」的
│  │                                        **26 个 URL / 40 行** 逐条**降级 + 出池**
│  │                                        （`cli downgrade` → `cli pool-drop`；每条 dry-run，命中数必须 1:1）
│  └─ q23_annotate_kept_rows.py + .json     ⚠️ **改生产池**：给**留池的 14 行 + 对应存档记录**补机器码读数
│                                           （原 evidence 是 obscura 旧文案 + 200 字截断 ⇒ 机器码没落进去）
│  ├─ gold_candidate_drift.py        **冻结的金标候选集 ↔ 负样本池的漂移读数**（离线；
│  │                                  去重键 / 按行两个口径 + `--pool-before` 做「本轮新增」对照）
│  ├─ failure_mode_profile.py        **批次失败模式画像**（离线；按证据首段把每条 outcome
│  │                                  归到 B1/B2/B4a/B4b/遍历超时/环境级阻断/结论类；
│  │                                  `--tasks` 做**同任务集**对照——重跑批是专挑有硬故障的
│  │                                  任务跑的 ⇒ 整批比整批是**条件不同**的比法）
│  ├─ h01_unresolved_rebucket.py + .json  **H-01 重测**：`unresolved` 剩余桶里有多少是
│  │                                  「机器本可以判出来」的（**离线**；两个子族各用感知层
│  │                                  **现成判据**——`infer_access_defect` 查页面正文 /
│  │                                  `classify_paywall` 查 agent 叙述；只算正文真抓到了的）
│  ├─ q24_q25_guard_rerun_plan.py      **两条护栏真跑批次的执行计划**：把 19 个「有 give-up 命中」的任务
│  │                                   汇成一份可跑计划（**16 个按 task_id 从 `plan_rerun_prod_m44.json`
│  │                                   过滤** + **3 个取 `ab_baseline/plan-camoufox-prod1-rest.json`**）；
│  │                                   **实例不重新反推**（缺一条就报错，不拿别的顶）⇒ 校验走**生产侧
│  │                                   同一个 loader**（不自己另写一套判据）⚠️ **只拼计划不开浏览器**
│  ├─ q24_q25_guard_replay.py          **两条护栏在存档上的回放读数**（**离线**）：拿**已落盘的存档** +
│  │                                   **同一把新判据**再判一遍 ⇒ 量的是「护栏会动哪些行」
│  │                                   ⚠️ **不是真跑读数**（读的是旧存档、用的是新判据）；
│  │                                   `--batch` / `--tasks` 可过滤
│  └─ q24_q25_guard_realrun_reading.py **护栏真跑读数**（**离线**）：读**存档里实际记下的分支**
│                                      （`outcomes[].branch` / `evidence`），分 §A Q-25 甲（终局形态）/
│                                      §B Q-24 甲（归因对象）/ §C 上线前后读数差（同 task_id + 同 url）
│                                      ⚠️ §C 的差值**混着站点漂移** ⇒ **不是干净的护栏效应**，
│                                      要连 evidence 一起读
│  ├─ q24_q25_guard_host403_probe.txt  **E021 那行的现场取证**（`cli probe` 的原始读数，**联网**）：
│  │                                   `m.xacjs.com` 的 **5 个路径**（根 / `/index.html` / `/weihu/` /
│  │                                   `/weihu/1416804.html` / `/voddetail/1.html`）**全部**返回
│  │                                   「403 Forbidden\nnginx」⇒ **host 级 403**，不是「单条视频死了」
│  │                                   ⚠️ **这是事实不是结论**——贴什么分支仍需人工裁定
│  ├─ q24_q25_guard_host403_recheck_2026-10-29.txt
│  │                                   **E021 的复测**（`cli probe` 原始读数，**联网**，生产面 camofox）：
│  │                                   5 个路径**仍全**是「403 Forbidden\nnginx」、
│  │                                   `元素=0` · `video=0` · `降级=[]`（⇒ **前置事实成立**）
│  ├─ q24_q25_guard_facecheck.py       **面依赖检查**（**联网**）：同一台机器、同一出口 IP，
│  │                                   **只换请求头**（`chrome` UA / `bare` 无 UA / `referer`）
│  │                                   打同一批 URL ⇒ **换面会不会变**，是判「这是站点的事实
│  │                                   还是我们这个面的事实」的那把尺子
│  │                                   ⚠️ **只记事实不下结论**；**`set-cookie` 一律不记**（红线）
│  ├─ q24_q25_guard_facecheck_xacjs_2026-10-29.txt
│  │                                   **E021 的换面读数**（**24** 次请求 = 7 个 URL × 3 客户端）：
│  │                                   `m.xacjs.com` 的 5 个路径 + `www` + apex ⇒ **21/21 HTTP 403
│  │                                   Forbidden（server=nginx）**；对照站 `m.dinggetv.com` **同刻 200 OK**
│  ├─ q24_q25_guard_facecheck_pool_broken_2026-10-29.txt
│  │                                   **池里全部 10 个 `broken` 唯一 URL 的换面读数**
│  │                                   （**30** 次请求）⇒ 换面**结论变了**的有 **3** 个
│  │                                   （`m.dinggetv.com` 电影页 · `104.143.94.74` 播放页 ·
│  │                                   `bilibili/bangumi` 外壳）⇒ 逐条判读见
│  │                                   [编年史 §八](../docs/计划进展/90-增量编年史.md)
│  └─ q24_q25_guard_kmslxsh_probe.txt  **T011 改判的现场取证**（`cli probe` 的原始读数，**联网**）：
│                                      `kmslxsh.com` 的 **3 个路径**（`/v/31445/6a6568e66c316.html` /
│                                      `/v/17807/6a6553bbc6bfd.html` / 站点根）**全部**返回
│                                      「404 Not Found\nnginx」，`元素=0` · `video=0` · `降级=[]`
│                                      ⇒ **前置事实成立**（当初那条观察若没带 `press_unsupported`，
│                                      护栏本就会落 `broken`）⇒ owner 据此裁定 T011 改判成 `broken`
│                                      ⚠️ **这是事实不是结论**——贴什么分支仍需人工裁定
├─ ab_baseline/plan-ab.json    A/B 两批共用的执行计划输入（`cli gen` 产物，挪而非删）
│  └─ plan-camoufox-prod1.json  **第一批 camoufox 生产批次**的 12 条计划（seed 20261022；第二批复用同一份⇒前后可比）
└─ smoke/_test_short.mp4       ffprobe 烟测物料（05/07 文档引为凭证）
```

---

## 怎么跑

**一律在仓库根跑**（`cd` 到 `simulation_controller/` 再执行）。原因不是习惯：

- 脚本里 `sys.path.insert(0, ".")` 依赖 CWD；
- 读黄金集与存档用的是**仓库根相对**路径
  （`trajectory_pipeline/evaluation/golden/data/v3/`、`trajectory_pipeline/output/pipeline/`）；
- 同目录兄弟文件已改成 `Path(__file__).resolve().parent / ...`——**迁移时改的**，
  这样至少不会再因为换目录而读不到；跨目录的（`probes/` 读 `recollect/`）走 `parent.parent`。

```powershell
# 离线复算 D-10 投影（不需要浏览器 / key）
uv run python trajectory_pipeline/research/recollect/measure.py

# 把重采新判定写回 candidates.jsonl 的 code（离线；v12→v13 一次性）
# ⚠️ 跑前先确认 candidates.jsonl 处于**写回前**状态（否则按 changed != 预期 fail-closed）；
#    跑完由 freeze --version N --from-version M --refresh-code 挂链，再用 agreement 出正式读数。
uv run python trajectory_pipeline/research/recollect/apply_code_writeback.py

# A/B 与专项分析（全部离线）
uv run python trajectory_pipeline/research/probes/inc43_ab_compare.py
uv run python trajectory_pipeline/research/probes/inc44_ab_compare.py
uv run python trajectory_pipeline/research/probes/inc44_unresolved_analysis.py
uv run python trajectory_pipeline/research/probes/cluster_c_landing_check.py

# 判据⑤ 人判工作表（离线；重生成沿用旧表已填格子——人填的判断只在 JSON 里）
# ⚠️ --a/--b 可给多次：**B 面两批存档都得列上**（少列一批 ⇒ 那批的行整批消失）
uv run python -m trajectory_pipeline.research.probes.inc73_human_review \
  --a trajectory_pipeline/output/pipeline/dualrun2/obscura \
  --b trajectory_pipeline/output/pipeline/dualrun2/camoufox \
  --b trajectory_pipeline/output/pipeline/dualrun/camoufox \
  --json trajectory_pipeline/research/probes/inc73_human_review.json

# 重采 / 探针（联网 + 真浏览器；对照 .env 里的 OBSCURA_EXE / TRAJECTORY_LLM_*）
uv run python trajectory_pipeline/research/recollect/visit.py
uv run python trajectory_pipeline/research/probes/inc45_probe_sohu.py

# 「读到一半」这一族（2026-10-22）：存档里的半读读数
# ⚠️ 探针是 rglob 递归扫的 ⇒ **必须把批次子目录排掉**，否则两代混进一张表
#    （现在的子目录：camoufox / camoufox_smoke / dualrun* / proberun / mediaprobe /
#      mediabrowse / staticprobe / review_auto）。**批次存档已摊平在顶层**。
uv run python trajectory_pipeline/research/probes/probe_settle_readings.py trajectory_pipeline/output/pipeline camoufox --exclude camoufox --exclude camoufox_smoke --exclude dualrun --exclude dualrun2 --exclude dualrun_camoufox --exclude dualrun2_camoufox --exclude proberun --exclude mediabrowse --exclude mediaprobe --exclude staticprobe --exclude review_auto   # 离线
uv run python trajectory_pipeline/research/probes/probe_batch_rates.py --label 修复后=trajectory_pipeline/output/pipeline --surface camoufox-1   # 离线

# 两代对撞：修复前那代 vs 修复后那代，同一 (task, url) 上结论是否相反
# ⚠️ **两侧都要点名 run_id**（--before-run-ids / --after-run-ids）：根目录里还躺着存量批次，
#    不筛就变成「两代执行层 + 存量 obscura」混在一张表里。
# ⚠️ **这一对已经跑不动了**（2026-10-22）：**修复前那一代已整代回滚** ⇒
#    存档不在盘上 ⇒ 读数只剩 `probes/generation_collision.json` 那份**历史快照**
#    （112 个共享 (task,url) 对 / 17 处结论矛盾）。下面是**下一次**两代对撞的写法。
# ⚠️ `--driver` 在这个**离线**脚本里筛的是**存档里记的 driver 字段**
#    （不选执行层、不联网）⇒ 想对撞 2026-10-22 那两代就点名 `camoufox`（它已退役、
#    只读存量）；**下一次**两代（现行执行层 = camofox）点名 `camofox`。
#    脚本的 `--driver` 缺省值仍是 `camoufox`（对齐那一次已完成的取证）。
uv run python trajectory_pipeline/research/probes/compare_generations.py \
  --before trajectory_pipeline/output/pipeline --after trajectory_pipeline/output/pipeline \
  --driver camofox --before-run-ids <前一代 run_id 列表> --after-run-ids 15e4b195,f48c4d6c,023b8837,b441d5aa,8f1aec03,102d3554,ae6553b1,a78dc57e,dcdf616c,0680dc4a,93f110b6,c39e54c7   # 离线

# D-5 判 2 分的样本逐条摊开（离线；M-29 收口的「逐条读正文」那一步）
# ⚠️ 读的是**存档原文**（observations[].body_text），不是临场重采 ⇒ 站点可能已漂移。
uv run python trajectory_pipeline/research/probes/probe_m29_d5_two.py

# M-30 现场重采：7 个待核 URL 真的重新打开看（**联网 + 真浏览器**；camofox）
# ⚠️ 必须在**仓库根**跑（读的是仓库根相对路径的存档目录）。
# ⚠️ 有的站点 `wait_until="load"` 会把导航打断（NS_BINDING_ABORTED）⇒ 脚本自动退回
#    `domcontentloaded` 重试一次（两轮实测都靠这条回落拿到 ixigua 的正文）。
# ⚠️ **每站读两次**：「导航一返回就观察」＋「等 --settle 秒再观察」（默认 4.0s）——
#    两次读数之差 = 「页面还没渲染完就观察」的量化证据（如 iqiyi 6 字 → 471 字）。
uv run python -m trajectory_pipeline.research.probes.m30_live_check \
  --json trajectory_pipeline/research/probes/m30_live_check.json
# 第二轮（独立重采，另存一份）
uv run python -m trajectory_pipeline.research.probes.m30_live_check \
  --json trajectory_pipeline/research/probes/m30_live_check_run2.json
# 换一批 URL 采（--targets-file 一行一个；--url 可重复叠加）
uv run python -m trajectory_pipeline.research.probes.m30_live_check \
  --targets-file <清单文件> --all-archives --json <落盘路径>
# ⚠️ --all-archives：并排比较**全部** P1 存档（顶层 `{task_id}__{run_id}.json`，
#    当前 78 个），不只默认那三个 run ⇒ 换 URL 批次时用这个。

# camofox 面「读到一半」（nav_unsettled）现场取证（**联网 + 真浏览器**；camofox）
# ⚠️ 必须在**仓库根**跑（`python -m`，包内相对路径）。
# 重演 `observe()` 的读序（snapshot → links → interactive），**每一步之后取一次
# 文档代号**（`performance.timeOrigin`）⇒ 打印「读到一半」是**时序问题**
# （页面在换文档）还是**能力问题**（代号读不到）——两者在 P1 存档里同形。
uv run python -m trajectory_pipeline.research.probes.camofox_nav_probe \
  --url "https://www.baidu.com/s?wd=功夫 在线观看"

# 上游销毁页之后驱动能不能自愈（**联网 + 真浏览器**；camofox）
# ⚠️ 必须在**仓库根**跑（`python -m`，包内相对路径）。
# 在**上游那一侧**把当前页关掉（本地栈照旧攥着它——这正是上游销毁页之后的
# 真实状态），再导航同一个 URL ⇒ 打印「换了一张新页、同一个 URL 重来、
# 页面真的加载出来了」还是「自愈失败」。**只读，不改任何产物**。
uv run python -m trajectory_pipeline.research.probes.camofox_tab_recovery_probe

# 上游「建页」与「导航」两个路由的失败形状**能不能分开**（**联网 + 真浏览器**；camofox）
# ⚠️ 必须在**仓库根**跑（`python -m`，包内相对路径）。
# 测三件事：① `camofox_create_tab` **不给 url** 能不能建出**空白页**
#    （上游 `server.js` 的 `if (url)`）；② 空白页 + `camofox_navigate` 打
#    DNS 不存在的域名 → 错误形状；③ 建页（**带 url**）打同一个域名 → 错误形状
#    （**对照**）。⇒ 实测结论：**两种失败在错误体里字面同形**
#    （500 + `{"error":…,"retryable":false}`、**无** `code`/`recovery`）
#    ⇒ **光看错误体分不开，只有「走哪个路由」能分开**（M-43）。
uv run python -m trajectory_pipeline.research.probes.camofox_route_split_probe \
  --url "http://this-domain-should-not-exist-zzq7x9.invalid/"

# 拆开「建页」与「导航」之后，**导航还正不正常**（**联网 + 真浏览器**；camofox）
# ⚠️ 必须在**仓库根**跑（`python -m`，包内相对路径）。
# 上游的 `navigate` 路由要求那一页**已注册在会话里** ⇒ 测「空白页 → 导航」
# 这条链路上报回来的东西（`httpStatus` / `navigationOk` / 落地 URL）与
# 原来「建页带 URL」**一样**。**只读，不改任何产物**。
uv run python -m trajectory_pipeline.research.probes.camofox_split_nav_probe \
  --url "https://www.baidu.com/s?wd=功夫 在线观看"

# **归因**现场取证：修好之后，**失败落在哪个路由 ⇒ 那就是归属判据**
# （**联网 + 真浏览器**；camofox）
# ⚠️ 必须在**仓库根**跑（`python -m`，包内相对路径）。
# 用**生产驱动**（`CamofoxDriver.goto`）跑三条真实路径，打印每一条抛出来的
# **异常类型**——因为「归因」这件事**没有门禁**，只能现场看：
# ① 正常站点 ⇒ 不抛，且**观察读得到正文**（证明拆开没把采集链打断）；
# ② DNS 不存在的域名 ⇒ **普通** `DriverError`（站点事实）；
# ③ 挂住不可路由的 IP ⇒ **普通** `DriverError`（等满 35 秒导航预算）。
# ⚠️ **「建页失败 ⇒ 执行层」现场触发不到**（建空白页几乎不会失败）
#    ⇒ 该条由单测覆盖，报告里标「未实测」——**不拿单测冒充现场读数**。
uv run python -m trajectory_pipeline.research.probes.camofox_attribution_probe

# **离线**扫 P1 存档：把方案 21 §R5 的验收标准**机械核一遍**（**不联网**）
# ⚠️ 必须在**仓库根**跑（`python -m`，包内相对路径）。
# 五条判据全是**结构**，不是猜文本：C1 指纹非空（≥11 键）· C2 建页路由的失败
# **不得**记 `unreachable_hard` · C3 `unreachable_hard` 的失败**必须**落在导航
# 路由上 · C4 **同一个 URL** 上同一个失败原因出现 ≥2 次（死页级联）·
# C5 `degraded` 含 `press_unsupported` 而那一步的动作不是 press（假降级）。
# ⚠️ **它不替人判「结论对不对」**——它只把**形状不对**的行点名。
# ⚠️ **批次正在写盘时读到的可能是不完整的一档** ⇒ 要判批次请**跑完之后**再扫。
uv run python -m trajectory_pipeline.research.probes.camofox_route_attribution_scan \
  --root trajectory_pipeline/output/pipeline

# **离线**算**采集侧账**读数（方案 20 §R3）：从存档的 `gatekeeper_events[]`
# 读 `blocked_surface` / `attempt`（**面只从 `RunRecord.driver` 折算，不手填**）
# + 从黄金集读**金标侧的面分布** ⇒ 印出「跨面账为什么是 0」。
# ⚠️ **不联网**、**只读**；⚠️ **必须在仓库根**跑（`python -m`）。
# ⚠️ 账的来源是**存档的闸门账本**，**不是负样本池**——方案 20 §R1 之后
# `blocked` **不进池** ⇒ 池里看不到被拦的笔数。
uv run python -m trajectory_pipeline.research.probes.camofox_collection_account \
  --root trajectory_pipeline/output/pipeline \
  --golden-root trajectory_pipeline/evaluation/golden/data

# 第 5 轮的**现场重采**（**联网 + 真浏览器**；camofox）——两张证据表
# ⚠️ **站点会漂** ⇒ 这两份 `.json` **不可重建**（重采拿到的是另一个页面）
#    ⇒ **留在这里当证据**，结论在 [编年史 §十一](../docs/计划进展/90-增量编年史.md)。
# ① `camofox_r5_uh_recheck_*`：**并发批里那 6 条 `unreachable_hard` 的逐条单独复核**
#    （铁律：不许因为「它是站点打不开」就放过）。3 条**打得开**（豆瓣片单 / 爱奇艺
#    《功夫（粤语）》/ 西瓜视频《功夫》），2 条**跳转链活着**（E028 的 baidu 链接——
#    打断消息里点名落点 `www.ctwlpt.com/voddetail/38550.html` 与 `…/vodplay/38550-1-1.html`
#    ⇒ 重定向确实发生了），1 条**失败在建页路由**（M-43，导航根本没发出去）。
#    **三轮**（r1/r2/r3）是为了看出两条链接**交替**报 `NS_ERROR_NET_RESET` 与
#    `Navigation … is interrupted by another navigation to …`。
# ② `camofox_r5_step6_live_check.*`：停点第 ⑥ 步那 **4 条 agent 负样本**的现场重采
#    （bilibili / v.qq / iqiyi / youku）+ M-43 那条 iqiyi。
uv run python -m trajectory_pipeline.research.probes.m30_live_check \
  --targets-file trajectory_pipeline/research/probes/camofox_r5_uh_recheck_targets.txt \
  --json trajectory_pipeline/research/probes/camofox_r5_uh_recheck_r1.json \
  --settle 4

# ③ `camofox_r5_step6_archive_vs_live.py`：**存档正文 vs 现场正文**并排（**离线，不联网**）
#    ——把 4 条 agent 负样本的**当场** `body_text`（从 `output/pipeline/samples/*.json` 取）
#    与今天的现场正文放在一起，各自跑仓库自己的 `paywall_evidence`。
#    ⚠️ 读的是 `output/pipeline/samples/`（**不进库**）⇒ **存档不在本机就跳过**，
#    但已生成的 `.json` **在库里** ⇒ 证据不丢。
uv run python trajectory_pipeline/research/probes/camofox_r5_step6_archive_vs_live.py

# D-3 / D-9 提示词 A/B（方案 16 C-10 补验证）：三臂 —— `old`（现版**去掉锚点块**
# 重建，**指纹须逐位等于** v8 manifest 的 `evaluator_version`，对不上当场停）/
# `new`（现版）/ `new2`（同版再跑一遍 = **噪声地板**）。
# ⚠️ **必须在仓库根跑**（读仓库根相对路径的存档目录）。
# ⚠️ **联网**（调 LLM 裁判）；凭据只从 TRAJECTORY_LLM_* 读，**不打印不落盘**。
# ⚠️ 慢：约 15s/行 ⇒ 247 行 D-3 + 8 行 D-9 ≈ **65 分钟**。
# ⚠️ `--d3-limit 247 --d9-limit 8` 就是**候选全体**（不是抽样）——切小会变成前 N 条。
uv run python trajectory_pipeline/research/probes/prompt_ab_anchor.py \
  --d3-limit 247 --d9-limit 8

# 批次后处理：展平 P1 存档 + 并池（**默认 dry-run**，确认后加 --apply）
# ⚠️ **顺序不能反**：先回滚要作废的那一代（cli purge-run --apply），**再**并池。
#    两代共用去重键 (task_id,url,branch) ⇒ 先并后回会把新批次那行**静默吞掉**
#    （既没进池、也没了存档）。实测两代 12 个 task 有 **39 个重叠键**。
uv run python trajectory_pipeline/research/probes/merge_batch_pool.py \
  --batch trajectory_pipeline/output/pipeline/<批次目录> \
  --root trajectory_pipeline/output/pipeline
# 冻结的金标候选集 ↔ 负样本池 的**漂移读数**（离线；每次动池之后量一次）
# ⚠️ 候选行的分支在 `code.branch`（**不在顶层**）⇒ 按顶层取会得到「322 个空分支键」，
#    看着像全漂了。
# ⚠️ `--pool-before` 给**动池之前**的池文件 ⇒ 算得出「本轮新增漂移」有多少
#    （Q-23 那轮：撤池前 183 键 / 204 行 ⇒ 撤池后 207 键 / 230 行，★新增 24 键 / 26 行）。
uv run python trajectory_pipeline/research/probes/gold_candidate_drift.py \
  --pool-before trajectory_pipeline/output/q23_backup_2026-10-28/negative.jsonl

# Q-23：池里存量 54 条 `unreachable_hard` 的逐条真浏览器复核（**联网 + 真浏览器**；camofox）
# ⚠️ 必须在**仓库根**跑。① 轮：池里的 29 个 URL，每个最多 3 次；② 轮：给**全部没打开的** URL
#    统一加试 6 次（**按「第一轮打不开」筛，不按「读数难解释」筛**——后者就是「按结果决定给谁加试」）。
# ⚠️ 这两支**只写 .json 取证**，**不碰池**；裁定交给处置脚本。
uv run python trajectory_pipeline/research/probes/camofox_q23_uh_recheck.py
uv run python trajectory_pipeline/research/probes/camofox_q23_uh_settle.py --attempts 6

# Q-23 处置（⚠️ **改生产池**：降级 + 撤池行；**默认 dry-run**，看清清单再加 --apply）
# ⚠️ **每条先 dry-run，dry-run 与 apply 的命中数必须 1:1**，对不上就中止
#    （池是 append-only 资产的唯一安全带）。
# ⚠️ **降级必须两层都落**（原档 + 复核档）——门禁与报表读**复核档**，
#    只改原档会当场报 `negative_not_pooled`（避坑指南 §8.36）。
# ⚠️ **判据不另写**：处置脚本 import 复核脚本的 `_verdict` ⇒ 一份判据两处用。
uv run python trajectory_pipeline/research/probes/q23_dispose_uh_rows.py
uv run python trajectory_pipeline/research/probes/q23_dispose_uh_rows.py --apply

# Q-23 留池行的证据补注（⚠️ **改生产池**：写存档的 evidence + 重写池文件；**默认 dry-run**）
# ⚠️ `--replace`：**换掉**尾部已写过的补注段（计数修正后用）——否则「已补过就跳过」
#    会把错的文本永远冻住。
uv run python trajectory_pipeline/research/probes/q23_annotate_kept_rows.py
uv run python trajectory_pipeline/research/probes/q23_annotate_kept_rows.py --replace --apply

```

```powershell
# 硬故障重跑：先出计划，再起批（**必须串行**——上游 lock_queue 会毁掉别人的页）
uv run python trajectory_pipeline/research/recollect/plan_hardfail_rerun.py `
    --out trajectory_pipeline/output/pipeline/plan_rerun_hardfail.json `
    --batch-id rerun-hardfail-2026-10-28

uv run python -m trajectory_pipeline.executor.cli run `
    --plan trajectory_pipeline/output/pipeline/plan_rerun_hardfail.json `
    --executor-mode agent --driver camofox --perceptor llm --log-level info

# 跑完：新旧对照（读存档，按「title + host 任一相等」对齐，证据档分开报）
uv run python trajectory_pipeline/research/recollect/compare_hardfail_rerun.py

# 跑完：失败模式画像（**同任务集**对照——分母一致才可比）
uv run python trajectory_pipeline/research/probes/failure_mode_profile.py `
    --batch rerun-prod-m44-2026-10-27 `
    --batch rerun-hardfail-2026-10-28 `
    --tasks E002,E017,E021,T018,T024,T025,T031,T042,T054 --compare

# H-01 重测：`unresolved` 剩余桶里「机器本可以判出来」的条数（**离线**；读全库存档）
# ⚠️ **判据不手抄词表**——两个子族各调感知层现成函数（`infer_access_defect` / `classify_paywall`）。
# ⚠️ 子族②（agent 叙述）命中是「**线索**」不是「结论」——叙述是模型自述，口径见台账 Q-25。
uv run python trajectory_pipeline/research/probes/h01_unresolved_rebucket.py

# v14 键迁移：重切前量四组事实（新 id 唯一吗 / 旧→新映射 1:1 吗 / 标注挂得上吗 /
# 哪些历史版本含旧 id）——**离线**，只读
uv run python trajectory_pipeline/research/probes/gold_candidate_rekey.py

# v14 若给 `--refresh-code` 会怎样：头条 D10 读数 + 逐行改好/改坏分类（**离线**，只读）
uv run python trajectory_pipeline/research/probes/gold_refresh_code_impact.py

# Q-24 甲 / Q-25 甲两条护栏在**全库存档**上的作用面（**离线**，只读）
uv run python trajectory_pipeline/research/probes/q24_q25_guard_replay.py
```

**哪些脚本必须联网**：`recollect/visit.py`、`probes/inc43_probe_body.py`、
`probes/inc45_probe_sohu.py`、`probes/inc45_probe2_sohu.py`、
`probes/m30_live_check.py`、`probes/cf_access_boundary.py`、
`probes/inc73_field_check.py`、`probes/inc73_action_replay.py`、
`probes/camofox_nav_probe.py`、`probes/camofox_tab_recovery_probe.py`、
`probes/camofox_route_split_probe.py`、`probes/camofox_split_nav_probe.py`、
`probes/camofox_attribution_probe.py`、
`probes/camofox_nav_failure_codes.py`、`probes/obscura_nav_failure_codes.py`
（**M-44 的机器码词表实测**：两面各打 DNS 失败 / 连接被拒 / 超时 / 打断四类用例
⇒ 判据的码**是量出来的，不是抄的**）、
`probes/camofox_q23_uh_recheck.py`、`probes/camofox_q23_uh_settle.py`
（**Q-23 逐条复核**：真浏览器重开池里的 `unreachable_hard` URL）、
`probes/prompt_ab_anchor.py`（调 LLM 裁判）。
其余全部离线。

**离线但会读存档的七支**：
`probes/corpus_inventory.py`（盘上 P1 存档按 task 汇总：份数 / 面 / 形态 / 分支分布 /
耗时——**跑之前先看清「现在有哪些任务」**）·
`probes/gold_candidate_drift.py`（冻结的金标候选集 ↔ 负样本池的**漂移读数**——每次动池
之后量一次；`--pool-before` 算「本轮新增漂移」）·
`recollect/plan_from_corpus.py`（**从存量存档反推重跑计划**：同一任务实例重跑，
不是 `cli gen` 重采样新任务；挑实例时**persona 维度齐全优先于站点数**
——站点最多的那份常常是 p0 校准跑、provenance 已被覆盖）·
`recollect/plan_hardfail_rerun.py`（**从批次存档反推硬故障重跑计划**：判据只收
「同参数重跑就会不同」的那族；实例来源必须是**批次存档**本身 ⇒ 新旧两份才能逐行对照）·
`recollect/compare_hardfail_rerun.py`（**新旧两批对照**：硬故障现在有没有变成真结论；
对齐 = **两边各算 title + host，任一相等即配上**——旧行多是跳转链⇒只有标题，
新行多是解析后地址⇒只有 host，各取一个键**永远对不上**；证据档分强弱，
**软证据不与强证据混算**）·
`probes/failure_mode_profile.py`（**批次失败模式画像**：按证据首段归族；
`--tasks` 做同任务集对照——**分母一致才可比**，整批比整批会被「挑的是有硬故障的任务」带偏）·
`probes/h01_unresolved_rebucket.py`（**H-01 重测**：`unresolved` 里「机器本可以判出来」的
条数——两个子族各调感知层现成判据，**不手抄词表**）·
`probes/gold_candidate_rekey.py`（**v14 键迁移前的四组事实**：新 id 唯一吗 / 旧→新映射 1:1 吗 /
重建后标注挂得上吗 / 哪些历史版本含旧 id）·
`probes/gold_refresh_code_impact.py`（**「若给 `--refresh-code` 会怎样」**：头条 D10 两态读数 +
逐行「改好 / 改坏」分类，**改坏的三族成因**分得出人核已写明分支名走错 / 金标陈旧 / 口径漂移）·
`probes/q24_q25_guard_replay.py`（**Q-24 甲 / Q-25 甲两条护栏在全库存档上的作用面**：
Q-24 量「`unreachable_hard` 里 URL 是中转链的条数」，Q-25 量「agent 放弃收口的 visit 里
护栏命中 / 归因对象不明 / 前置事实不成立各多少」）。

**哪些脚本会起真浏览器**：`probes/m30_live_check.py`、`probes/cf_access_boundary.py`、
`probes/inc73_field_check.py`、`probes/inc73_action_replay.py`、
`probes/camofox_nav_probe.py`、`probes/camofox_tab_recovery_probe.py`、
`probes/camofox_route_split_probe.py`、`probes/camofox_split_nav_probe.py`、
`probes/camofox_attribution_probe.py`、
`probes/camofox_nav_failure_codes.py`、`probes/obscura_nav_failure_codes.py`
`probes/camofox_q23_uh_recheck.py`、`probes/camofox_q23_uh_settle.py`
（**Q-23 逐条复核**：起真浏览器重开 URL）、
（**camofox**：MCP 适配器 + 上游 REST 服务 ⇒ **还要上游服务起着**）。
⚠️ 2026-10-26 起**没有 `--headful`**：camofox 的浏览器由上游 REST 服务持有，
有头/无头**不由本仓库决定**（旧 camoufox 那批探针里「最小化自己起的那个窗口」
那一族随之退役，见 [§退役配方](#退役配方-2026-10-26-执行层换-camofox)）。

> ⚠️ **Windows 控制台先设 `PYTHONIOENCODING=utf-8`。**
> 这些脚本会往 stdout 打 `✅` / `❌` / 中文，默认 GBK 控制台会在
> `UnicodeEncodeError` 处崩掉（**崩在最后一行打印，看起来像「脚本跑完了但报错」**，
> 其实计算已经做完）。PowerShell：`$env:PYTHONIOENCODING="utf-8"`。

---

## 三条必须知道的规矩

1. **`outcomes.jsonl` 不可平凡重建。** 重跑 `visit.py` 需联网 + 真浏览器，
   而**站点已漂移**：重采结果 ≠ 当轮结果。删了就是丢证据，不是「再跑一遍」。
   `rounds/` 同理——它们最像残留，其实是轮次留痕。
2. **不要拿本目录的 `.jsonl` 当「当前事实」。** 它们是**当时**的取证；
   当前状态看 [`../docs/计划进展/`](../docs/计划进展/) 与 `check-archives`。
3. **改写历史文档时只加注记。** 编年史里出现的旧 `tmp/` 路径是当时发生的事实，
   不要回改成新路径（那等于篡改它当时记的事）。迁移注记已加在两份编年史顶部。

---

## 退役配方（2026-10-26 · 执行层换 camofox）

**为什么退役**：这 12 支探针是**旧 camoufox 执行层**（playwright 直连、仓库自己
起浏览器窗口）的取证脚本。它们要么 import 已删掉的
`executor/browser/camoufox_{driver,session}.py`，要么 import 已卸载的
`camoufox` 包 ⇒ **跑不起来**。而且**不是「缺个依赖」**：camofox 的浏览器由
**上游 REST 服务**持有，仓库**不再起窗口**⇒「自己起窗口、自己最小化它、
量 rAF 帧率」这一整套玩法**在新执行层下不存在**。

⚠️ **退役的是脚本，不是证据**：读数（`.json`）与结论（docs）**全部保留**。
下表是「配方索引」——**要重测时照着这张表重建**，别从 git 历史里刨。

| 退役的脚本 | 它量了什么 | 读数落在哪 | 结论落在哪 | 要重测的话怎么重建 |
|---|---|---|---|---|
| `probe_new_tab_click.py` | 新开 tab 上点击挂死，定位到哪个环节不返回（第一轮；**含一处已证伪的判读**） | `probes/new_tab_click.json` | 方案 04 §4 | 新执行层**没有「自己起窗口」这件事** ⇒ 该问题（窗口遮挡 / 合成器节流）**不复存在**；要问的是「上游服务里的浏览器会不会同样慢」 |
| `probe_click_delivery.py` | 事件到底送到没有（`data-pd` 计数器 + rAF 帧率） | `probes/click_delivery.json` | 方案 04 §4 | 同上：DOM 计数器改用 `evaluate` 取（camofox 有 `camofox_evaluate`） |
| `probe_click_latency.py` | 挂死还是慢过超时（timeout 放到 60s 量真实完成时间） | `probes/click_latency.json` | 方案 04 §4 | 量「上游 tool 往返时延」：`camofox_click` 的往返时间 |
| `probe_click_repeat.py` | 同一动作重复采样，摊平「慢」的分布 | `probes/click_repeat.json` | 方案 04 §4 / 避坑指南 | 同上 |
| `probe_click_distance.py` | 位移是不是主因（对角按钮交替点） | `probes/click_distance.json` | 方案 04 §4 | 位移由**上游**做（`camofox_type`）⇒ 本仓库量不到 |
| `probe_click_occluded.py` | **人为最小化窗口** → 5–13s，因果定案 | `probes/click_occluded.json` | 避坑指南 | **不可重建**：窗口归上游所有，本仓库无权最小化它 |
| `probe_click_occlusion_prefs.py` | Firefox 偏好能不能治住窗口不可见导致的点击变慢（答：不能） | `probes/click_occlusion_prefs.json` | 避坑指南 | 同上（偏好也在上游） |
| `probe_doc_stamp.py` | **文档代号**（`performance.timeOrigin`）能不能分「换了文档」与「只换了 URL」 | 无 json（stdout 读数） | 避坑指南 §文档代号 | **机制仍在**：`camofox_driver._doc_stamp()` 用同一判据；要重新量就在 camofox 下重跑一遍 |
| `probe_nav_commit.py` | 动作之后页面**落没落定**的现场读数 | 无 json（stdout 读数） | 避坑指南 / 方案 04 | **机制仍在**：`camofox_driver._await_commit()`；同上 |
| `probe_camoufox_launch.py` | 最小 launch 分步探针：定位 launch 卡在哪一步 | 无 json（stdout 读数） | 增量九十二的停点 | **换成 camofox 的两步**：MCP 通道起来没有（`CamofoxSession.launch` 的预检）· 上游 REST 服务通不通 |
| `probe_bili_ep313572.py` | B 站《功夫》播放页当场复采（播放器报错 vs 会员内容） | `probes/bili_ep313572.json` | D-10 分歧卡 | 用 `m30_live_check.py --url <该 URL>` 重采（同口径：每站两读 + 关键词命中） |
| `probe_calib_rows14_32.py` | v7 校准 14–32 行的 13 个 URL 当场复采（19 条 D-10 分歧） | `probes/calib_rows14_32.json` | D-10 分歧卡 | 用 `m30_live_check.py --targets-file <清单>` 重采 |

⚠️ **为什么留着这张表而不是留脚本**：留着脚本，下一个人会去跑它 ⇒ 撞
ImportError ⇒ 然后**去把 camoufox 装回来**（owner 已裁定「禁止把原有的
camoufox 接回来」）。⇒ **脚本退役 + 配方留档**是这里唯一不会把人带回去的做法。

---

## 迁移带来的两个顺带修复

- **`inc40_prep_retest_worksheet.py` 写错位置的 bug 已修**：原写
  `Path("tmp_inc40_retest_worksheet_data.jsonl")`（根级），而数据实际在 `tmp/` 下。
  现在写同目录。
- **根级临时名已进 `.gitignore`**（`/tmp/`、`/tmp_*/`、`/.tmp_*/`）。
  ⚠️ ignore 对**已跟踪**文件无效——真正的清理是这次 `git mv`，规则只防将来复发。

> `probes/cluster_c_landing_check.py` 原本标注「一次性工具，不入库」，但已被提交
> （2026-10-19）。这里按研究产物保留；**要不要退库由 owner 决定**，本目录不擅自删证据。