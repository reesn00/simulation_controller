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
│  ├─ outcomes.jsonl           **当前有效轮**（INC43 轮，75 对直采命中 79 行）
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
│  ├─ probe_new_tab_click.py          P5 点击超时·第一轮（**含一处已证伪的判读**：当时以为与「新开 tab」有关）
│  ├─ probe_click_delivery.py         第二轮：事件到底送到没有（`data-pd` 计数器 + rAF 帧率）
│  ├─ probe_click_latency.py          第三轮：把 timeout 放到 60s → **不是挂死，是慢过超时**
│  ├─ probe_click_repeat.py           第四轮：重复采样，headful+humanize 出现 7→61s 与 rAF 停滞
│  ├─ probe_click_distance.py         第五轮：对角按钮交替点 → 位移不是主因
│  ├─ probe_click_occluded.py         第六轮：**人为最小化窗口** → 5–13s，因果定案
│  ├─ probe_click_occlusion_prefs.py  第七轮：Firefox 偏好能不能治住（答：不能）
│  └─ click_*.json / new_tab_click.json   上述各轮的原始读数
├─ ab_baseline/plan-ab.json    A/B 两批共用的执行计划输入（`cli gen` 产物，挪而非删）
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

# A/B 与专项分析（全部离线）
uv run python trajectory_pipeline/research/probes/inc43_ab_compare.py
uv run python trajectory_pipeline/research/probes/inc44_ab_compare.py
uv run python trajectory_pipeline/research/probes/inc44_unresolved_analysis.py
uv run python trajectory_pipeline/research/probes/cluster_c_landing_check.py

# 重采 / 探针（联网 + 真浏览器；对照 .env 里的 OBSCURA_EXE / TRAJECTORY_LLM_*）
uv run python trajectory_pipeline/research/recollect/visit.py
uv run python trajectory_pipeline/research/probes/inc45_probe_sohu.py
```

**哪些脚本必须联网**：`recollect/visit.py`、`probes/inc43_probe_body.py`、
`probes/inc45_probe_sohu.py`、`probes/inc45_probe2_sohu.py`。其余全部离线。

**哪些脚本会起真浏览器**：`probes/probe_new_tab_click.py`、`probe_click_delivery.py`、
`probe_click_latency.py`、`probe_click_repeat.py`、`probe_click_distance.py`、
`probe_click_occluded.py`、`probe_click_occlusion_prefs.py`（camoufox，**不开网络**，
只有第一轮与 distance 轮用到 `jijikb.com`）。后两个会**最小化它自己起的那个窗口**
再恢复（按标题 `PROBE-MARK-*` 认窗口，不碰别的窗口）——**跑的时候别看屏幕**。

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

## 迁移带来的两个顺带修复

- **`inc40_prep_retest_worksheet.py` 写错位置的 bug 已修**：原写
  `Path("tmp_inc40_retest_worksheet_data.jsonl")`（根级），而数据实际在 `tmp/` 下。
  现在写同目录。
- **根级临时名已进 `.gitignore`**（`/tmp/`、`/tmp_*/`、`/.tmp_*/`）。
  ⚠️ ignore 对**已跟踪**文件无效——真正的清理是这次 `git mv`，规则只防将来复发。

> `probes/cluster_c_landing_check.py` 原本标注「一次性工具，不入库」，但已被提交
> （2026-10-19）。这里按研究产物保留；**要不要退库由 owner 决定**，本目录不擅自删证据。