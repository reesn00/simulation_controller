# GDR Agent 数据精修：MVP 单进程版落地设计方案

> 基于 [gdr-int.md](gdr-int.md) 理论框架，按以下约束做调整后形成的工程实施文档：
> - 底座主力：**Qwen3.5-9B-Instruct**
> - 推理后端：**llama-cpp-python**（不追求高并发）
> - 调度编排：**单进程顺序** + 可选多进程（MVP 不涉及分布式）
> - 监控：**标准库 logging + JSONL 文件日志 + tqdm 进度条**（MVP 不集成外部监控）

---

## 0. 调整后的技术栈

| 层 | 选型 |
|---|---|
| 底座主力 | **Qwen3.5-9B-Instruct-Q4_K_M.gguf** |
| 推理后端 | **llama-cpp-python**（llama.cpp 的官方 Python 绑定） |
| 升级模型 | Qwen3.5-32B-Instruct-Q4_K_M.gguf（同框架，按需切换路径） |
| 裁判模型 | Qwen3.5-32B 或 Qwen3.5-72B（仅按需加载） |
| Schema | Pydantic v2 |
| JSON 校验 | jsonschema |
| 嵌入模型 | sentence-transformers（BGE-M3，本地 CPU） |
| 编排 | 标准库 `concurrent.futures` / `multiprocessing` |
| 日志 | `logging`（控制台 + JSONL 文件） |
| 进度 | `tqdm` |

---

## 1. 依赖清单（pyproject.toml 摘要）

```toml
[project]
dependencies = [
  "llama-cpp-python>=0.3.2",
  "pydantic>=2.6",
  "jsonschema>=4.21",
  "sentence-transformers>=2.6",
  "tqdm>=4.66",
  "orjson>=3.9",
]

[project.optional-dependencies]
sft = ["transformers", "peft", "datasets", "trl"]
```

> GPU 加速 llama.cpp：编译时带 `CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python`，否则纯 CPU 也能用（更慢但零配置）。

---

## 2. 调整后的目录结构

```text
gdr-agent/
├── config.py                 # 全局配置（路径、阈值、模型名）
├── schema.py                 # Pydantic 数据契约
├── logger.py                 # 统一日志入口
├── router.py                 # 模块1：解析与缺陷路由
├── refiners/
│   ├── base.py               # LlamaCppClient 单例封装
│   ├── thought_refactor.py   # 模块2
│   ├── tool_fixer.py         # 模块3
│   └── obs_denoiser.py       # 模块4
├── validators/
│   ├── l1_rules.py
│   ├── l2_semantic.py
│   └── l3_judge.py           # 模块5
├── reassembler.py            # 模块6
├── evaluator/                # 模块7（可选 phase）
├── prompts/                  # yaml 形式提示词
│   ├── thought.yaml
│   ├── tool.yaml
│   ├── obs.yaml
│   └── judge.yaml
├── pipeline.py               # 单进程编排入口
├── build_sft_pairs.py        # 程序化构造 SFT 样本
└── README.md
```

无 Ray、无 vLLM、无 Prometheus，**全部本地 Python 进程**。

---

## 3. LlamaCppClient 封装（核心基础设施）

```python
# refiners/base.py
from llama_cpp import Llama, LlamaGrammar
from threading import Lock
from typing import Optional
import time, logging, json

log = logging.getLogger(__name__)

class LlamaCppClient:
    """单进程内复用同一 Llama 实例，线程安全。"""
    _instances: dict[str, "LlamaCppClient"] = {}
    _lock = Lock()

    def __init__(self, model_path: str, n_ctx: int = 8192, n_gpu_layers: int = -1):
        self.llm = Llama(
            model_path=model_path,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            n_threads=8,
            verbose=False,
        )

    @classmethod
    def get(cls, model_path: str, **kwargs) -> "LlamaCppClient":
        with cls._lock:
            if model_path not in cls._instances:
                log.info(f"loading model: {model_path}")
                cls._instances[model_path] = cls(model_path, **kwargs)
            return cls._instances[model_path]

    def generate(
        self,
        prompt: str,
        grammar_json_schema: Optional[dict] = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> tuple[str, dict]:
        t0 = time.time()
        kwargs = dict(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            stop=["</output>", ""],
        )
        if grammar_json_schema:
            kwargs["grammar"] = LlamaGrammar.from_json_schema(
                json.dumps(grammar_json_schema)
            )
        out = self.llm(**kwargs)
        text = out["choices"][0]["text"]
        meta = {
            "model": self.llm.model_path,
            "tokens_in": out["usage"]["prompt_tokens"],
            "tokens_out": out["usage"]["completion_tokens"],
            "latency_s": round(time.time() - t0, 3),
        }
        return text, meta
```

关键设计：

- **单例模式**：同一模型只加载一次，避免重复吃显存
- **`LlamaGrammar.from_json_schema`**：llama.cpp 原生支持 JSON Schema 约束解码（对应原方案中的 guided decoding），结构层错误零复发
- **不追求并发**：单实例顺序调用即可；如要并行，**开多个进程**而非线程（llama.cpp 在 Python GIL 下多线程收益有限）

---

## 4. 日志规范

```python
# logger.py
import logging, sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

def setup(log_dir: Path, level=logging.INFO):
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt_console = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    root = logging.getLogger()
    root.setLevel(level)

    # 控制台
    h1 = logging.StreamHandler(sys.stdout)
    h1.setFormatter(logging.Formatter(fmt_console))
    root.addHandler(h1)

    # JSONL 文件（便于后续解析）
    h2 = RotatingFileHandler(
        log_dir / "gdr.jsonl", maxBytes=50_000_000, backupCount=5, encoding="utf-8"
    )
    h2.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(h2)
```

### 使用约定

| 事件 | 日志级别 | 字段 |
|---|---|---|
| 模型加载 | INFO | `event=model_load, path=...` |
| 单条精修开始/结束 | DEBUG | `event=refine_*, traj_id, turn_idx, latency_s` |
| 验证结果 | INFO | `event=validate, level=L1/L2/L3, passed, failure_modes` |
| 升级大模型 | WARNING | `event=escalate, traj_id, from_model, to_model` |
| 兜底丢弃 | ERROR | `event=discard, traj_id, reason` |
| 进度 | INFO（带 `tqdm`） | `processed/total` |

控制台可读、文件可机解，**零外部依赖**。

---

## 5. 数据契约（Pydantic Schema）

```python
# schema.py
from pydantic import BaseModel, Field
from typing import Literal, Optional
from enum import Enum

class DefectTag(str, Enum):
    THOUGHT_TOO_SHORT = "thought_too_short"
    THOUGHT_TOO_LONG = "thought_too_long"
    THOUGHT_BROKEN_LOGIC = "thought_broken_logic"
    TOOL_JSON_INVALID = "tool_json_invalid"
    TOOL_SCHEMA_MISMATCH = "tool_schema_mismatch"
    TOOL_WRONG_SELECTION = "tool_wrong_selection"
    TOOL_HALLUCINATED = "tool_hallucinated"
    OBS_NOISE = "obs_noise"
    OBS_DEBUG_LEAK = "obs_debug_leak"

class AtomicTurn(BaseModel):
    turn_idx: int
    user_query: str
    thought: str
    action: dict                       # 原始或精修后的工具调用
    observation: str
    defects: list[DefectTag] = Field(default_factory=list)
    # 精修字段（精修后填充）
    refined_thought: Optional[str] = None
    refined_action: Optional[dict] = None
    refined_observation: Optional[str] = None
    refine_log: list[dict] = Field(default_factory=list)

class Trajectory(BaseModel):
    trajectory_id: str
    system_prompt: str
    user_query: str
    turns: list[AtomicTurn]
    tools_schema: list[dict]           # 可用工具的 JSON Schema 列表
    metadata: dict = Field(default_factory=dict)
```

---

## 6. 单进程流水线编排

```python
# pipeline.py
from pathlib import Path
import json, logging
from tqdm import tqdm

from schema import Trajectory
from router import Router
from refiners import thought_refactor, tool_fixer, obs_denoiser
from validators import l1_rules, l2_semantic, l3_judge
from reassembler import reassemble
from config import Settings

log = logging.getLogger(__name__)

def process_one(traj: Trajectory, cfg: Settings) -> Trajectory | None:
    """处理单条轨迹，所有异常内部捕获。"""
    try:
        Router().tag(traj)

        # 1) 思考链重构（仅对缺陷标记触发）
        for turn in traj.turns:
            if turn.has_thought_defect():
                turn.refined_thought = thought_refactor.refine(turn, cfg)

        # 2) 工具调用修复
        for turn in traj.turns:
            if turn.has_action_defect():
                turn.refined_action = tool_fixer.refine(turn, cfg)

        # 3) 观测降噪
        for turn in traj.turns:
            if turn.has_obs_defect():
                turn.refined_observation = obs_denoiser.refine(turn, cfg)

        # 4) 分层验证 + 重试升级
        for turn in traj.turns:
            if not validate_with_retry(turn, cfg):
                log.warning(f"discard turn {traj.trajectory_id}:{turn.turn_idx}")
                turn.mark_invalid()

        if all(t.invalid for t in traj.turns):
            return None

        # 5) 重组
        return reassemble(traj)
    except Exception as e:
        log.exception(f"pipeline error: {traj.trajectory_id}: {e}")
        return None

def validate_with_retry(turn, cfg):
    if cfg.enable_l1 and not l1_rules.check(turn): return False
    if cfg.enable_l2 and not l2_semantic.check(turn): return False
    if cfg.enable_l3 and not l3_judge.check(turn, cfg):
        return try_retry(turn, cfg)
    return True

def run(input_path: Path, output_path: Path, cfg: Settings):
    trajs = load_trajectories(input_path)
    results = []
    for traj in tqdm(trajs, desc="GDR refining"):
        r = process_one(traj, cfg)
        if r:
            results.append(r)
    save_trajectories(results, output_path)
    log.info(f"done: {len(results)}/{len(trajs)} kept")
```

### 并行策略（可选，不强求）

- MVP **建议顺序**，更易排查问题
- CPU 验证与 GPU 推理天然异步，可在主进程里用一个轻量 `ThreadPoolExecutor(max_workers=2)` 跑两条精修流，验证同步进行
- 真要并行：开 N 个**子进程**（每个加载一份模型副本），用 `multiprocessing.Pool`

---

## 7. 关键模块实现示例

### 7.1 工具调用修复（带 JSON Schema 约束解码）

```python
# refiners/tool_fixer.py
import json
from .base import LlamaCppClient
from config import Settings

PROMPT = """[角色] Agent 工具调用修复专家...
[输入]
用户意图: {user_query}
前序 Thought: {thought}
可用工具 Schema: {tools_schema}
原始（错误）调用: {action}
[输出要求]
1. tool_name 必须严格等于列表中的某一项。
2. 参数 schema 100% 合规。
3. 输出 JSON: {{"tool_name": "...", "arguments": {{...}}}}
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "tool_name": {"type": "string"},
        "arguments": {"type": "object"},
    },
    "required": ["tool_name", "arguments"],
}

def refine(turn, cfg: Settings):
    client = LlamaCppClient.get(cfg.tool_model_path)
    prompt = PROMPT.format(
        user_query=turn.user_query,
        thought=turn.thought,
        tools_schema=json.dumps(turn.tools_schema, ensure_ascii=False),
        action=json.dumps(turn.action, ensure_ascii=False),
    )
    text, meta = client.generate(
        prompt, grammar_json_schema=OUTPUT_SCHEMA, max_tokens=512
    )
    return json.loads(text)
```

约束解码由 llama.cpp 底层完成，**结构错误零复发**。

### 7.2 思考链重构（带事实守恒校验）

```python
# refiners/thought_refactor.py
def refine(turn, cfg):
    client = LlamaCppClient.get(cfg.main_model_path)
    prompt = render("thought.yaml", turn=turn)
    text, _ = client.generate(prompt, max_tokens=600)
    refined = json.loads(text)["refined_thought"]

    # 事实守恒：精修后 thought 中的命名实体 ⊇ 原 thought 中的
    orig_ents = extract_entities(turn.thought)
    new_ents = extract_entities(refined)
    if not orig_ents.issubset(new_ents):
        log.warning(f"entity loss in {turn.turn_idx}: {orig_ents - new_ents}")
        return None  # 让上层走重试/升级
    return refined
```

### 7.3 L3 裁判（仅在升级路径触发）

```python
# validators/l3_judge.py
JUDGE_PROMPT = """[角色] 精修质量裁判。
[正例] {positive_example}
[负例] {negative_example}
[评估]
原始: {original}
精修: {refined}
[输出] JSON: {"verdict":"pass|fail","score":0-10,"reason":"..."}"""

def check(turn, cfg):
    client = LlamaCppClient.get(cfg.judge_model_path)  # 32B
    prompt = JUDGE_PROMPT.format(
        positive_example=load("prompts/judge_pos.json"),
        negative_example=load("prompts/judge_neg.json"),
        original=turn.original_dump(),
        refined=turn.refined_dump(),
    )
    text, _ = client.generate(prompt, max_tokens=300)
    result = json.loads(text)
    return result.get("verdict") == "pass"
```

### 7.4 思考链提示词（prompts/thought.yaml）

```yaml
role: |
  [角色] 你是一名 Agent 推理链修复专家。
task: |
  [任务] 基于真实条件，修正思考链的【过短/过长/逻辑断裂】缺陷，禁止注入原始内容中没有的新事实。
inputs:
  - 用户意图: {user_query}
  - 当前 Observation: {observation}
  - 原始 Thought: {thought}
  - 目标长度区间: [{min_len}, {max_len}] 字
  - 缺陷类型: {defect_type}
output_requirements:
  - 保留原始 Thought 中的全部事实断言、决策结论、关键实体。
  - 仅调整表述结构、补充因果链或剥离冗余。
  - 输出 JSON: {"refined_thought": "...", "preserved_facts": [...], "removed_facts": []}
  - 回答末尾列出"我新增了哪些原始 Thought 中没有的内容"。
```

### 7.5 观测降噪提示词（prompts/obs.yaml）

```yaml
role: |
  [角色] Agent 观测降噪专家。
task: |
  [任务] 从原始 Observation 中仅保留对解决 user_query 有直接因果贡献的信息，剔除调试日志/HTML 结构/重复状态码/无关栈信息。
inputs:
  - user_query: {user_query}
  - 前序 Thought: {thought}
  - 后续 Thought 列表: {next_thoughts}
  - 原始 Observation: {observation}
output_requirements:
  - 输出自然语言段落，禁止 markdown/JSON。
  - 字数 ≤ 原文 50%。
  - 末尾列出"被保留的核心事实"列表。
```

---

## 8. Router 规则层（毫秒级，零成本）

```python
# router.py
class Router:
    def tag(self, traj: Trajectory) -> Trajectory:
        for turn in traj.turns:
            turn.defects.extend(self._rule_layer(turn))
            if needs_llm_review(turn.defects):
                turn.defects.extend(self._llm_layer(turn))
        return traj

    def _rule_layer(self, turn):
        tags = []
        # JSON 解析
        try:
            json.loads(json.dumps(turn.action))
        except Exception:
            tags.append(DefectTag.TOOL_JSON_INVALID)
        # Schema 校验（仅在 JSON 合法时）
        try:
            jsonschema.validate(turn.action, _match_tool_schema(turn))
        except Exception:
            tags.append(DefectTag.TOOL_SCHEMA_MISMATCH)
        # 长度分诊
        if len(turn.thought) < 20:
            tags.append(DefectTag.THOUGHT_TOO_SHORT)
        elif len(turn.thought) > 500:
            tags.append(DefectTag.THOUGHT_TOO_LONG)
        # 噪声关键词
        if any(k in turn.observation for k in ["DEBUG", "Traceback", "status: 500"]):
            tags.append(DefectTag.OBS_DEBUG_LEAK)
        # 幻觉工具
        if turn.action.get("tool_name") not in _tool_names(turn):
            tags.append(DefectTag.TOOL_HALLUCINATED)
        return tags

    def _llm_layer(self, turn):
        # 仅对规则未覆盖项做 LLM 复核（THOUGHT_BROKEN_LOGIC / TOOL_WRONG_SELECTION / OBS_NOISE）
        # 多次抽样一致性 ≥ 2/3 才采纳
        ...
```

---

## 9. 验证层

### 9.1 L1 规则层

```python
# validators/l1_rules.py
def check(turn) -> bool:
    try:
        json.loads(json.dumps(turn.refined_action or turn.action))
        jsonschema.validate(turn.refined_action or turn.action, _match_tool_schema(turn))
        assert 0 < len(turn.refined_thought or turn.thought) <= 2000
        # 字段非空、长度区间达标
        # refined_thought 包含原 thought 的所有命名实体（NER 比对）
        return True
    except Exception:
        return False
```

### 9.2 L2 语义层（CPU + 嵌入）

```python
# validators/l2_semantic.py
def check(turn) -> bool:
    sim_thought = cosine_sim(
        embed(turn.thought),
        embed(turn.refined_thought or turn.thought),
    )
    sim_action = cosine_sim(
        embed(json.dumps(turn.action)),
        embed(json.dumps(turn.refined_action or turn.action)),
    )
    return sim_thought > 0.85 and sim_action > 0.90
```

### 9.3 L3 裁判层（GPU，慢路径，仅对 L2 边界值触发）

详见 §7.3。

---

## 10. 配置示例

```python
# config.py
from pydantic_settings import BaseSettings
from pathlib import Path

class Settings(BaseSettings):
    # 模型路径
    main_model_path: Path = Path("./models/Qwen3.5-9B-Instruct-Q4_K_M.gguf")
    tool_model_path: Path = Path("./models/Qwen3.5-9B-Instruct-Q4_K_M.gguf")
    judge_model_path: Path = Path("./models/Qwen3.5-32B-Instruct-Q4_K_M.gguf")

    # 上下文
    n_ctx: int = 8192
    n_gpu_layers: int = -1              # 全 GPU；CPU 模式设 0

    # 验证开关
    enable_l1: bool = True
    enable_l2: bool = True
    enable_l3: bool = True

    # 升级阈值
    max_retries_9b: int = 2
    escalate_to_32b_after: int = 2

    # IO
    input_path: Path = Path("./data/raw.jsonl")
    output_path: Path = Path("./data/refined.jsonl")
    log_dir: Path = Path("./logs")

    class Config:
        env_prefix = "GDR_"
```

支持 `GDR_MAIN_MODEL_PATH=...` 环境变量覆盖。

---

## 11. SFT 数据构造（关键技术路径）

```python
# build_sft_pairs.py
from datasets import Dataset
import random

def build_pairs(tools_schema, n_pairs=10000):
    pairs = []
    for _ in range(n_pairs):
        tool = random.choice(tools_schema)
        correct_call = sample_valid_call(tool)
        broken_call = inject_error(correct_call, error_type=random.choice([
            "json_invalid", "schema_mismatch", "wrong_tool", "hallucinated"
        ]))
        pairs.append({
            "instruction": REPAIR_PROMPT,
            "input": {
                "user_query": ...,
                "thought": ...,
                "broken_call": broken_call,
                "tools_schema": tools_schema,
            },
            "output": correct_call,
        })
    return Dataset.from_list(pairs)
```

- 用 **Qwen3.5-9B-Instruct + LoRA SFT 3 epoch**
- 评估目标：精修后通过率 ≥ 95%，事实一致率 ≥ 98%

---

## 12. 端到端一致性终检与重组

```python
# reassembler.py
def reassemble(traj: Trajectory) -> Trajectory:
    for turn in traj.turns:
        turn.thought = turn.refined_thought or turn.thought
        turn.action = turn.refined_action or turn.action
        turn.observation = turn.refined_observation or turn.observation
    assert end_to_end_consistency(traj)
    return traj

def end_to_end_consistency(traj):
    # 每轮 Thought 引用的事实 ⊆ 同轮 Observation
    # Action 与 Thought 决策逻辑自洽（用 32B 打分 ≥ 7）
    ...
```

### 元数据落盘

```json
{
  "trajectory_id": "...",
  "original_version": "...",
  "refined_version": "...",
  "modified_components": ["turn_2.thought", "turn_3.action"],
  "validation_levels_passed": ["L1", "L2", "L3"],
  "refine_history": [
    {"module": "tool_fixer", "attempt": 1, "result": "fail"}
  ]
}
```

---

## 13. 效用评估闭环（模块 7）

```bash
# 训练两个对照模型
torchrun --nproc_per_node=1 sft.py \
  --base_model Qwen3.5-9B-Instruct \
  --train_data refined_D_prime.jsonl \
  --output_dir probe_refined

torchrun --nproc_per_node=1 sft.py \
  --base_model Qwen3.5-9B-Instruct \
  --train_data original_D.jsonl \
  --output_dir probe_original
```

### 双维评测

| 维度 | 指标 | 通过阈值 |
|---|---|---|
| 保留性（应学会） | 任务完成率、工具选择正确率、Thought-事实一致性 | 与原 D 训练模型差异 ≤ 3% |
| 剔除性（应避免） | 错误调用复现率、噪声 Observation 干扰率 | 比原 D 训练模型降低 ≥ 50% |

### 反馈回路

```python
if eval.retention < threshold or eval.removal < threshold:
    failing_module = identify_failing_module(probe_outputs)
    if failing_module == "tool_fixer":
        augment_sft_pairs(more_pairs_for_tool_fixer)
    elif failing_module == "thought_refactor":
        tune_prompt(failing_pattern)
    rerun_pipeline()
```

---

## 14. 调整后的成本与性能（单卡 RTX 4090 / A10 参考）

| 项 | 数值（10 万条轨迹估算） |
|---|---|
| 9B Q4_K_M 显存 | ~6 GB（GPU）/ ~6 GB RAM（CPU） |
| 端到端吞吐 | **30–80 条/小时**（CPU），**200–400 条/小时**（GPU） |
| 32B 升级占比 | < 5% |
| L3 占比 | < 10% |
| 全量耗时（10 万条） | 约 **10–25 个 GPU·小时** |
| 探针训练 | 一次性，~2 GPU·小时 |

> 不追求高并发，**单实例顺序推理**即可；如要加速，多开 N 进程即可，模型权重按需懒加载。

---

## 15. 调整后的里程碑

| 阶段 | 天 | 交付物 |
|---|---|---|
| M1: 跑通最小链路 | 1-2 | `schema.py` + `LlamaCppClient` + 单条轨迹 Router + Thought 重构 + L1 验证 |
| M2: 三大精修器 | 3-4 | Tool Fixer（带约束解码）+ Obs Denoiser + L2 嵌入验证 |
| M3: 验证闭环 | 5-6 | L3 裁判 + 重试/升级 + 全量 1 千条小批跑通 |
| M4: 效用评估 | 7-8 | 探针模型训练 + 双维评测 + 第一版 D′ |
| M5: 收敛迭代 | 9-10 | 效用达标，可重复运行的 CLI |

---

## 16. 快速启动

```bash
# 安装
pip install llama-cpp-python pydantic jsonschema sentence-transformers tqdm orjson

# 下载模型
huggingface-cli download Qwen/Qwen3.5-9B-Instruct-GGUF \
  qwen3.5-9b-instruct-q4_k_m.gguf --local-dir ./models

# 跑
python pipeline.py

# 或带环境变量
GDR_MAIN_MODEL_PATH=./models/qwen3.5-9b-instruct-q4_k_m.gguf \
GDR_ENABLE_L3=false \
python pipeline.py
```

---

## 17. 与原方案的核心差异（对照速查）

| 项 | 原方案 | 调整后 |
|---|---|---|
| 底座主力 | Qwen2.5-7B / Llama-3.1-8B | **Qwen3.5-9B** |
| 推理后端 | vLLM | **llama-cpp-python** |
| 调度 | Ray DAG | **单进程 + 可选多进程** |
| 约束解码 | vLLM GuidedDecoding | **llama.cpp `LlamaGrammar.from_json_schema`** |
| 并发 | 高吞吐 | **顺序优先，可选多进程** |
| 监控 | Prometheus + Grafana | **logging + JSONL + tqdm** |
| 存储中间产物 | Parquet | **JSONL**（MVP 阶段） |
| 升级模型 | 集中服务调用 | **同进程按需加载 GGUF** |