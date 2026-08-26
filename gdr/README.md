# GDR Agent 数据精修 MVP

对 QwenPaw 平台导出的 Agent 会话轨迹数据执行自动缺陷检测与精修，实现"脏数据入、干净数据出"的流水线处理。

LLM 调用通过 HTTP OpenAI 兼容接口（vLLM / llama.cpp server / Ollama 等），不再依赖本地 SDK 加载模型。

## 数据模型

真实数据为 QwenPaw 平台导出的单文件 JSON 格式，核心结构为 **Session → Message → Block** 三级模型：

| 层级 | 说明 |
|------|------|
| Session | 一次完整的 Agent 交互会话，含 session_id、messages、reply_context 等 |
| Message | 单条对话记录，role 为 user 或 assistant |
| Block | 原子内容单元，按 type 分为 thinking / toolcall / toolresult / text 四种 |

## 缺陷标签（13种）

| 类别 | 标签 | 检测方式 |
|------|------|---------|
| 思考链 | `THOUGHT_TOO_SHORT/LONG/BROKEN_LOGIC` | 规则层长度阈值 + LLM 复核 |
| 工具调用 | `TOOL_JSON_INVALID` | input 字段 JSON 解析 |
| 工具调用 | `TOOL_HALLUCINATED` | 工具名不在可用列表 |
| 工具调用 | `API_HALLUCINATION` | input 代码调用不存在的 API |
| 工具调用 | `TOOL_WRONG_SELECTION` | LLM 语义复核 |
| 工具调用 | `REPETITIVE_CALL` | ≥3 次同工具相似 input |
| 工具调用 | `CONTEXT_SWITCH_LOOP` | browser↔shell 切换 ≥3 次 |
| 观测 | `OBS_NOISE` | LLM 语义噪声复核 |
| 观测 | `OBS_DEBUG_LEAK` | 关键词匹配（DEBUG/Traceback/API_MISUSE 等） |
| Text 块 | `TEXT_FACT_HALLUCINATION` | 数值/平台名未在前面 toolresult 中出现 |
| 消息级 | `MESSAGE_UNHEALTHY` | 健康分 < 阈值 |

## 环境准备

### 依赖

```bash
pip install httpx pydantic pydantic-settings jsonschema sentence-transformers scikit-learn tqdm orjson PyYAML Jinja2
```

[SFT] 可选：
```bash
pip install ".[sft]"
```

### LLM 端点

启动任意 OpenAI 兼容服务（vLLM / llama.cpp server / Ollama），注册三个模型名（默认值见下表）。

例如 llama.cpp server：
```bash
./server -m qwen3.5-9b-instruct-q4_k_m.gguf --port 8000 -ngl 99
```

## 配置

所有参数支持 `GDR_` 前缀环境变量覆盖：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `GDR_LLM_BASE_URL` | `http://localhost:8000/v1` | OpenAI 兼容端点 |
| `GDR_LLM_API_KEY` | `not-needed` | API Key |
| `GDR_MAIN_MODEL` | `Qwen3.5-9B-Instruct` | 主力模型名 |
| `GDR_TOOL_MODEL` | `Qwen3.5-32B-Instruct` | 升级模型名 |
| `GDR_JUDGE_MODEL` | `Qwen3.5-32B-Instruct` | L3 裁判模型名 |
| `GDR_EMBEDDING_ENDPOINT_URL` | `http://127.0.0.1:8086/v1` | OpenAI 兼容嵌入端点 (llama.cpp `--embedding`) |
| `GDR_EMBEDDING_ENDPOINT_MODEL` | `v5-nano-retrieval` | 端点上注册的嵌入模型名 |
| `GDR_ENABLE_L1` / `GDR_ENABLE_L2` / `GDR_ENABLE_L3` | `true` | 验证层开关 |
| `GDR_INPUT_PATH` | `./data/input.json` | 输入 QwenPaw JSON |
| `GDR_OUTPUT_PATH` | `./refine_data/output.json` | 输出精修 JSON |
| `GDR_LOG_DIR` | `./logs` | 日志目录 |

完整字段见 [config/settings.py](config/settings.py) 和 [.env.example](.env.example)。

## 使用

```bash
# 单文件 (默认输出到 ./refine_data/output.json)
python -m pipeline --input origindata/session.json

# 单文件自定义输出
python -m pipeline --input origindata/session.json --output refine_data/session_refined.json

# 批量 + 多进程 (默认输出到 <batch-input-dir>/refine_data/)
python -m pipeline --batch-input-dir origindata/ --workers 4

# 也可使用 pyproject 提供的脚本入口
# gdr-pipeline --input origindata/session.json

# 带环境变量
set GDR_MAIN_MODEL=qwen3.5-9b-instruct
set GDR_LLM_BASE_URL=http://192.168.1.10:8000/v1
python -m pipeline --input origindata/session.json
```

## 输出

精修后的 JSON 保留原始结构，在 `metadata` 中附加精修历史：

```json
{
  "session_id": "...",
  "metadata": {
    "refine_history": [...],
    "validation_summary": {
      "total_blocks": 42,
      "modified_blocks": 15,
      "passed_L1": 15,
      "passed_L2": 12,
      "passed_L3": 10
    },
    "modified_blocks": ["block_id_1", "block_id_2"]
  }
}
```

## 日志

运行日志输出到 `./logs/gdr.jsonl`，每行一条结构化 JSON：

```json
{"timestamp":"2026-08-24T...","level":"INFO","name":"root","event":"creating HTTP LLM client: base_url=http://localhost:8000/v1 model=Qwen3.5-9B-Instruct"}
{"timestamp":"...","level":"DEBUG","name":"root","event":"session 0c1750... processed in 45.2s","session_id":"0c1750...","latency_s":45.2}
```

## 架构

```
gdr/
├── README.md
├── pyproject.toml                # 项目配置 + CLI 入口 (gdr-pipeline / gdr-evaluator)
├── .env.example                  # 环境变量模板
├── config/
│   ├── settings.py               # Settings (pydantic-settings) + load_tools()
│   └── tools.yaml                # 工具白名单 + 幻觉 API 黑名单
├── domain/
│   └── schema.py                 # Pydantic 数据契约 (Session/Message/Block*)
├── infrastructure/
│   ├── logging.py                # 双通道日志 (console + JSONL RotatingFile)
│   └── llm_client.py             # HTTP OpenAI LLM client 单例池 (历史名 LlamaCppClient)
├── routing/
│   └── router.py                 # 缺陷路由：规则层 + LLM 3-票投票层
├── pipeline/
│   ├── runner.py                 # 主编排 (单 session 处理 + 多文件批量 + 多进程 Pool)
│   └── cli.py                    # argparse CLI 入口
├── reassembly/
│   └── reassembler.py            # 一致性终检 + 重写 blocks + 元数据落盘
├── refiners/
│   ├── thought_refactor.py       # 思考链重构 + 事实守恒
│   ├── tool_fixer.py             # 工具调用修复 + 约束解码
│   └── obs_denoiser.py           # 观测降噪 + 压缩率
├── validators/
│   ├── l1_rules.py               # L1 规则验证
│   ├── l2_semantic.py            # L2 语义验证 BGE-M3
│   └── l3_judge.py               # L3 裁判验证
├── data/
│   └── sft_pairs.py              # 程序化 SFT 样本构造
├── evaluator/                    # 效用评估闭环 (探针 + 双维评测 + 反馈回路)
├── prompts/                      # YAML 提示词模板
└── docs/                         # 设计文档
```

各 Python 包通过 `__init__.py` 重新导出顶层符号, 因此原 `from config import Settings`、`from schema import Session`、`from router import Router` 等写法仍然兼容; `refiners.base` 已并入 `infrastructure.llm_client`, 旧 `from refiners.base import LlamaCppClient` 也仍可用。

设计细节见 [docs/gdr-mvp-design.md](docs/gdr-mvp-design.md)。

## 许可证

MIT