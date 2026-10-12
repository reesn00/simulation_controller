# URL 视频可播放性校验 & 黄金数据集构建 · 完整方案

## 0. 背景与目标

- 场景：Agent 轨迹数据合成中，需要构建"URL 网站视频是否正常播放"的黄金数据集（golden dataset）。
- 现状：人工逐条点击网页判断，量大时不可扩展。
- 目标：**人工从"执行者"变为"审核者"**，构建"机器初筛 + VLM 复核 + 人工终审低置信"的三层流水线，支持海量 URL 的可扩展标注。

---

## 1. 任务定义（先决条件，需与业务方确认）

必须明确的 4 个定义问题，直接决定标签体系：

| # | 问题 | 影响 |
|---|------|------|
| Q1 | 付费内容算不算"不可播放"？ | 决定 paywall 是正类、负类还是独立标签 |
| Q2 | 登录后可免费看，算不算通过？ | 区分 login_wall vs paywall，避免混淆 |
| Q3 | 前贴广告、试看片段算不算"正常播放"？ | 决定 ads / partial_free 的标签归属 |
| Q4 | 地区限制/反爬/验证码如何归类？ | 区分"内容问题"与"环境问题" |

**建议默认定义**：agent 任务是"访问 URL 并确认视频能正常播放"。
- 能自动播（含广告后播放）→ `playable`
- 需登录（登录后免费）→ `login_required`（若 agent 任务不含登录态，视为不通过）
- 需付费/会员/购买 → `paywall`（独立标签，任务是否通过取决于业务定义）
- 地区限制/反爬/验证码 → `blocked`（环境问题）
- 加载失败/404/解码失败 → `broken`（内容问题）
- 无法确定 → `unknown` → 进人工

---

## 2. 标签体系

### 2.1 一级标签（final_label）

| 标签 | 含义 | 典型证据 |
|------|------|----------|
| `playable` | 视频正常播放 | timeupdate 推进、帧解码、无错误 |
| `ads_then_playable` | 有广告但广告后可播 | 广告标记后出现正常播放 |
| `paywall` | 需付费/会员/购买 | 价格文案、付费弹窗、试看结束提示 |
| `login_required` | 需登录（登录后免费） | "登录后观看"、无价格信息 |
| `partial_free` | 部分免费（试看/预览） | 试看 N 分钟弹付费、会员专享标注 |
| `blocked` | 环境性阻断 | 地区限制、验证码、反爬、robots |
| `broken` | 内容性损坏 | 404、媒体流无法解码、duration=0 |
| `empty` | 无视频内容 | 页面无 video 元素/无媒体流 |
| `unknown` | 无法确定 | 信号不足或互相矛盾 |

### 2.2 付费墙子标签（paywall_type）

| 子标签 | 含义 |
|--------|------|
| `none` | 无付费墙 |
| `paid_subscription` | 订阅制会员（连续包月/年费） |
| `rental` | 单片租赁 |
| `purchase` | 单片购买（永久） |
| `trial` | 免费试用引导（7天免费等） |
| `unknown` | 能感知付费但无法细分 |

### 2.3 元数据字段

- `paywall_evidence`：命中的原文证据（如"开通VIP · 连续包月¥25"）
- `price` / `currency`：抽取的价格
- `playable_before_paywall`：付费前是否有可播内容
- `preview_duration_sec`：试看时长

---

## 3. 整体流水线架构

```
URL 列表
  │
  ▼
[L0] 批量可达性探测（requests, 无浏览器）        ── 秒级/条
  │  过滤：404/403/超时/跳转异常 → broken/blocked
  ▼
[L1] 媒体资源解析（HTML/iframe 静态解析 + ffprobe）
  │  抽取：video/mp4, m3u8, webm, audio/mp4
  │  过滤：媒体 404、duration=0、不可解码 → broken
  ▼
[L2] 浏览器自动化（Playwright/Chromium headless）
  │  加载页面 → 识别播放器 → 触发播放 → 采集信号（见 §4.2）
  │  采集：DOM 文本、弹窗、播放事件、网络请求、定时截图
  ▼
[L3] 规则判定（关键词 + 行为信号，见 §4.3、§5）
  │  输出：初判标签 + 置信度 + needs_human
  ▼
[L4] VLM 复核（截图 + DOM 摘要 → 结构化输出）
  │  处理：规则未覆盖的变体文案、动态弹窗、UI 组合信号
  │  输出：标签 + 置信度 + 证据
  ▼
[L5] 仲裁与路由
  │  ├─ 规则 & VLM 一致且高置信 → 直接入 golden（自动通过）
  │  ├─ 规则 & VLM 一致但低置信 → 抽样 2-5% 人工复核
  │  └─ 不一致 / unknown / 边界类 → 人工终审队列
  ▼
[L6] 人工审核台（只看 needs_human 样本，秒级操作）
  │  操作：确认 / 改标 / 打备注
  │  产出：终审标签 + 推翻记录
  ▼
[L7] 回流迭代
     人工推翻 case → 关键词库/规则/VLM prompt 迭代
     阈值校准 → 自动通过率持续提升
```

---

## 4. 各模块设计

### 4.1 L0/L1：静态探测

**L0 可达性**
- 状态码、重定向链（>3 次标记）、TLS 证书、响应时间（>10s 超时）
- 输出：`landing_status`、`final_url`、`redirect_count`

**L1 媒体解析**
- 正则/解析器抽取：`video/mp4`、`application/x-mpegURL(m3u8)`、`webm`、`audio/mp4`、`iframe` 播放器
- `ffprobe` 直连媒体 URL：时长、分辨率、码率、关键帧解码
- HLS：拉取首个分片试解码
- 过滤：`媒体404`、`duration=0`、`ffprobe 失败` → `broken`

可自动解决：**60–90%** 的明显坏站。

### 4.2 L2：浏览器自动化信号采集

每个 URL 采集窗口：加载后 3s、点击播放后 10–15s。

**播放信号（判断是否真在播）：**
- `readyState >= 3`（HAVE_ENOUGH_DATA）
- `timeupdate` 事件频率、`currentTime` 是否持续增长
- `paused`、`error`（`MediaError.code`）
- 网络：媒体分片请求是否持续、码率趋势
- 帧差分：canvas 采样帧间差异 > 阈值（排除黑屏/静帧）

**付费墙信号（点击播放后重点采集）：**
- 弹窗/overlay 出现，文本含价格/会员/解锁
- `currentTime` 到达固定值（如 300s）后暂停 + 弹窗（试看结束）
- 详情页/metadata 标注（"会员专享"、"VIP 抢先看"）
- 分辨率锁定（仅 480p，高清需 VIP）

**环境阻断信号：**
- 验证码 iframe、地区提示文案、反爬挑战页

### 4.3 L3：规则判定

**付费墙关键词库（多语言，可持续迭代）：**

| 类别 | 关键词 |
|------|--------|
| 订阅制 | 开通会员、VIP、连续包月、仅会员、Subscribe、Subscription、Premium、Members only |
| 租赁/购买 | 付费、收费、解锁、购买、单点、租金、Rent、Buy、Purchase、Unlock、Pay-per-view |
| 试用 | 免费试用、首月、7天免费、Free trial、Try free |
| 试看 | 试看、预览、抢先、Trailer、Preview |
| 价格模式 | `¥\d+`、`$\d+(\.\d+)?`、`￥`、`CNY`、`USD` 等正则 |

**判定规则（示例优先级从高到低）：**
1. 播放后弹窗含价格/订阅文案 → `paywall`（置信度 0.9+）
2. currentTime 固定值截停 + 弹窗 → `paywall: rental/purchase`（0.9+）
3. "登录后观看" 且无任何价格 → `login_required`（0.85）
4. 会员专享标注 + 可播预览 → `partial_free`（0.8）
5. 验证码/地区/反爬 → `blocked`（0.9）
6. currentTime 持续增长 + 帧差分正常 → `playable`（0.9）
7. currentTime 涨但帧全黑 → `broken: black_screen`（0.85）
8. 信号矛盾或不足 → `unknown` → 进人工

### 4.4 L4：VLM 复核

**输入**：3–5 张截图（点击播放前 / 播放后 2s / 播到 10s）+ DOM 文本摘要 + 播放信号摘要。

**输出（结构化）：**
```
label: playable | ads_then_playable | paywall | login_required |
       partial_free | blocked | broken | empty | unknown
paywall_type: none | paid_subscription | rental | purchase | trial | unknown
evidence: <页面元素原文>
price: <数字或 null>  currency: <CNY/USD/...>
confidence: 0-1
```

**VLM 的价值**：动态弹窗、变体文案（"本集为会员抢先"）、图标+价格组合按钮、词表覆盖不了的语言/新 UI——这些是纯规则的盲区。

### 4.5 L5：仲裁路由

| 情况 | 处理 |
|------|------|
| 规则=VLM，置信度均 ≥ 0.9 | 自动通过，入 golden |
| 规则=VLM，置信度 0.6–0.9 | 抽样 2–5% 人工复核校准 |
| 规则≠VLM | 进人工终审 |
| 任一方 unknown / 边界类（partial_free、blocked、paywall 组合） | 进人工终审 |
| 人工队列内部再排序：分歧 > unknown > 边界类 > 高置信 | —— |

### 4.6 L6：人工审核台

- 每屏展示一条：截图 + 证据高亮 + 自动判定 + 置信度
- 快捷键操作：确认（Enter）/ 改标（数字键）/ 备注（T）
- 单条处理时间目标：**5–10 秒**（vs 现状分钟级，人工量降至 5–15%）

---

## 5. 数据结构 Schema

```json
{
  "url": "https://example.com/video/123",
  "final_url": "https://example.com/video/123",
  "landing_status": 200,
  "redirect_count": 0,

  "player_type": "html5 | hls | youtube | iframe | unknown",
  "media_urls": ["https://..."],

  "play_started": true,
  "timeupdate_progress": true,
  "current_time_sec": 12.4,
  "frames_decoded": true,
  "black_screen": false,
  "media_error": null,

  "rule_label": "paywall",
  "vlm_label": "paywall",
  "rule_confidence": 0.91,
  "vlm_confidence": 0.93,

  "final_label": "paywall",
  "paywall_type": "paid_subscription",
  "paywall_evidence": "开通VIP · 连续包月¥25",
  "price": 25,
  "currency": "CNY",
  "playable_before_paywall": true,
  "preview_duration_sec": 300,

  "needs_human": false,
  "human_label": null,
  "human_remark": null,
  "screenshots": ["s_000.png", "s_002.png", "s_010.png"],
  "captured_at": "2026-10-12T11:55:00+08:00"
}
```

---

## 6. 分层抽样与质量保证

**分层维度**：域名、视频源类型（MP4 直链/HLS/YouTube/iframe）、CDN、地区、清晰度、登录态、页面模板。

**校准机制**：
- 每轮分层抽样 3–8% 人工复核，计算机器判定与人工的一致率
- 记录 **推翻率**（override rate）= 人工推翻机器判定的比例
- 逐层调阈值：推翻率 < 2% 的层 → 提高自动通过阈值；> 10% 的层 → 收紧 + 增补规则

**核心指标**：
| 指标 | 目标 |
|------|------|
| 自动通过率 | 首轮 50–70%，迭代后 80–90% |
| 自动判定准确率（抽样复核） | ≥ 97% |
| 人工处理占比 | 首轮 10–20%，稳定后 ≤ 10% |
| 单条平均标注成本 | 从分钟级降到秒级 |

---

## 7. 迭代回流机制

```
人工推翻样本
  ├─ 词表未命中 → 关键词库（周更）
  ├─ 规则误判  → 规则优先级/条件修正
  ├─ VLM 判错 → prompt 增补 few-shot 反例
  └─ 新型 paywall 形态 → 新增 paywall_type 或信号通道
```

**边界 case 清单（重点关注）**：
- 地区限制 + 付费叠加 → 标签需支持 `primary_reason` + `secondary_reason`
- 登录墙 ≠ 付费墙 → 强校验，这是最容易污染 golden 集的混淆
- 广告误伤 → 设 `ads` 中间态，播完广告再判定
- 混合内容（部分集免费、部分付费）→ 按 URL 粒度标注 + 记录上下文
- 应用内购买/跳第三方支付页 → 标 `paywall`，证据记跳转链

---

## 8. 落地路线

| 阶段 | 内容 | 周期 | 产出 |
|------|------|------|------|
| P0 定义确认 | 对齐 Q1–Q4 任务定义，冻结标签体系 | 2–3 天 | 标签规范 v1 |
| P1 静态探测 | L0/L1 脚本 + ffprobe 批处理 | 3–5 天 | 粗筛 pipeline，先砍掉 60–90% 坏站 |
| P2 播放检测 | Playwright 信号采集 + 规则判定 | 1–2 周 | L2/L3，含关键词库 v1 |
| P3 付费识别 | 弹窗/试看/价格抽取 + paywall 子标签 | 1 周（与 P2 并行后半） | paywall 专项判定 |
| P4 VLM 复核 | 截图链路 + prompt 工程 + few-shot | 1–2 周 | L4，边界 case 覆盖率提升 |
| P5 人工审核台 | 审核 UI + 回流闭环 + 分层抽样校准 | 1 周 | L5–L7 |
| P6 迭代优化 | 按推翻率调阈值，压人工占比 | 持续 | 自动通过率 ≥ 85% |

---

## 9. 成本估算（供参考）

单条处理成本量级对比：
- 纯人工点击：1–3 分钟/条
- L0–L3 机器判定：约 5–15 秒/条（静态）+ 15–30 秒/条（浏览器）
- VLM 复核：约 2–5 秒/条（批量调用）
- 人工终审：仅 5–15% 的样本 × 5–10 秒/条

**万条量级**：全人工约需 167–500 人时；流水线 + 终审后约 20–50 人时，**提效约 5–10 倍**，且准确率更稳定。
