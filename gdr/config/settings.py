from pathlib import Path
from typing import Any, Literal, Optional
import json
import os
import re

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from pydantic import Field, model_validator
import logging
import yaml

log = logging.getLogger(__name__)

# gdr 包根目录: 相对路径配置一律锚定到这里, 不依赖调用方 CWD。
GDR_ROOT = Path(__file__).resolve().parent.parent

# 统一根配置 (仓库根 config/config.yaml) 的定位:
#   1. GDR_CONFIG_FILE 环境变量 (显式覆盖)
#   2. 仓库根 config/config.yaml
# 不存在时抛 FileNotFoundError (无包内兜底)。
ROOT_CONFIG_ENV = "GDR_CONFIG_FILE"
REPO_ROOT_CONFIG = GDR_ROOT.parent / "config" / "config.yaml"

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 根配置 gdr: 段字段 → llm: 共享段字段的缺省映射
_LLM_FIELD_FALLBACK = {
    "llm_base_url": "base_url",
    "llm_api_key": "api_key",
    "main_model": "model",
    "tool_model": "model",
    "judge_model": "model",
}


def _expand_env_placeholders(value: Any) -> Any:
    """递归展开 ${VAR} 占位符 (与根级 shared_config 等价的精简实现;
    gdr 独立安装时不能 import 根级模块, 故自带一份)。"""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_placeholders(v) for v in value]
    return value


def _load_root_gdr_section() -> dict[str, Any]:
    """读统一根配置的 ``gdr:`` 段 (含 ``llm:`` 共享段缺省合并)。

    根配置不存在或不含 ``gdr:`` 段时抛 FileNotFoundError (无包内兜底)。
    GDR_CONFIG_FILE 已设置但文件缺失时不静默回退仓库根默认路径。
    """
    env_path = os.environ.get(ROOT_CONFIG_ENV)
    candidates = [Path(env_path)] if env_path else [REPO_ROOT_CONFIG]
    for cand in candidates:
        if not cand.is_file():
            break
        try:
            raw = yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001 - 配置损坏直接报错, 不静默吞
            raise FileNotFoundError(f"root config {cand} unreadable: {e}") from e
        if not isinstance(raw, dict):
            raw = {}
        raw = _expand_env_placeholders(raw)
        section = raw.get("gdr") if isinstance(raw.get("gdr"), dict) else {}
        out = dict(section)
        llm = raw.get("llm") if isinstance(raw.get("llm"), dict) else {}

        # PR 3 (Commit 10): 把 ``langfuse.stages.gdr`` 嵌套段铺平到 gdr Settings
        # 字段 (langfuse_gdr_per_step_span / langfuse_gdr_per_llm_span /
        # langfuse_gdr_per_refine_span). 根级 langfuse: 段的通用字段
        # (enabled / public_key / secret_key / base_url / environment / ...)
        # 保持由 simulate_serve.observability.langfuse_client 工厂鸭子类型
        # 解析 (它通过 ``getattr(cfg, "langfuse_<key>")`` 直接读 gdr 字段,
        # 但 ``langfuse.*`` 全局字段由 ``cfg.langfuse_*`` 映射)。这里只负责把
        # ``stages.gdr.*`` 三档 stage 专用布尔值落到 ``langfuse_gdr_*`` 字段。
        langfuse_root = raw.get("langfuse") if isinstance(raw.get("langfuse"), dict) else {}
        langfuse_stages = (
            langfuse_root.get("stages") if isinstance(langfuse_root.get("stages"), dict) else {}
        )
        gdr_stage = langfuse_stages.get("gdr") if isinstance(langfuse_stages.get("gdr"), dict) else {}
        if gdr_stage:
            # ``enabled`` 阶段开关 → ``langfuse_enabled`` 兜底 (Factory 读 langfuse_enabled)
            if "enabled" in gdr_stage and "langfuse_enabled" not in out:
                out["langfuse_enabled"] = bool(gdr_stage["enabled"])
            if "per_step_span" in gdr_stage and "langfuse_gdr_per_step_span" not in out:
                out["langfuse_gdr_per_step_span"] = bool(gdr_stage["per_step_span"])
            if "per_llm_span" in gdr_stage and "langfuse_gdr_per_llm_span" not in out:
                out["langfuse_gdr_per_llm_span"] = bool(gdr_stage["per_llm_span"])
            if "per_refine_span" in gdr_stage and "langfuse_gdr_per_refine_span" not in out:
                out["langfuse_gdr_per_refine_span"] = bool(gdr_stage["per_refine_span"])
            # upload_payload / max_payload_bytes 也下沉到 gdr (Factory 读 langfuse_*)。
            if "upload_payload" in gdr_stage and "langfuse_upload_payload" not in out:
                out["langfuse_upload_payload"] = gdr_stage["upload_payload"]
            if "max_payload_bytes" in gdr_stage and "langfuse_max_payload_bytes" not in out:
                out["langfuse_max_payload_bytes"] = gdr_stage["max_payload_bytes"]
            if "max_block_payload_bytes" in gdr_stage and "langfuse_max_block_payload_bytes" not in out:
                out["langfuse_max_block_payload_bytes"] = gdr_stage["max_block_payload_bytes"]
        for field_name, llm_key in _LLM_FIELD_FALLBACK.items():
            if not out.get(field_name) and llm.get(llm_key):
                out[field_name] = llm[llm_key]
        log.info("gdr settings: root config %s gdr: section loaded (%d keys)", cand, len(out))
        return out
    raise FileNotFoundError(
        f"Unified root config not found: {candidates[0]} "
        f"(or set {ROOT_CONFIG_ENV}); no fallback config exists"
    )


# QwenPaw agent.json 默认位置: builtin_tools 是远端 agent 真实工具集的权威来源。
# agent_id 不同的部署用 qwenpaw_agent_json 配置 / GDR_QWENPAW_AGENT_JSON 覆盖。
DEFAULT_QWENPAW_AGENT_JSON = Path("~/.qwenpaw/workspaces/default/agent.json")


class Settings(BaseSettings):
    # HTTP OpenAI-compatible LLM endpoint
    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "not-needed"

    # Model names registered at the endpoint
    main_model: str = "Qwen3.5-9B-Instruct"
    tool_model: str = "Qwen3.5-32B-Instruct"
    judge_model: str = "Qwen3.5-32B-Instruct"

    # === Embedding endpoint (OpenAI-compatible /v1/embeddings) ===
    # Llama.cpp server with --embedding: e.g. http://127.0.0.1:8086/v1
    embedding_endpoint_url: str = "http://127.0.0.1:8086/v1"
    embedding_endpoint_model: str = "v5-nano-retrieval"
    embedding_expected_dim: Optional[int] = None  # None=不校验; 设了则首次响应维度不符立即报错
    embedding_timeout_s: float = 30.0
    embedding_max_batch: int = 32
    embedding_max_input_chars: int = 6000  # 单条输入字符上限 (超服务端 n_ctx 会 400 exceed_context_size)

    # Kept for backwards compatibility in prompts / context sizing; not used to load GGUF files.
    n_ctx: int = 8192
    n_gpu_layers: int = -1

    enable_l1: bool = True
    enable_l2: bool = True
    enable_l3: bool = True
    enable_llm_layer: bool = True

    # === LLM 投票层 - 3 次请求各自的上下文策略 ===
    # 每条策略控制一次 LLM 投票能看到的相邻 block 范围, 让 3 次投票输入不同以提升独立性。
    # 可选值: "none" / "±1" / "±2" / "pre1_post2" / "pre2_post1"
    # 默认: 裸看 / 局部窗口 / 偏前文, 覆盖三种独立判断依据。
    llm_vote_context_strategies: list[str] = ["none", "±1", "pre2_post1"]
    # LLM 投票每次请求最大字符预算 (含 surrounding), 超过会被截断
    llm_vote_max_context_chars: int = 4000

    # 是否使用 ContextUnderstanding 替代旧的 ±N surrounding context 注入 LLM 投票 prompt
    llm_vote_use_cu: bool = True
    # 语义标签不会改变决策结果的块跳过 LLM 投票 (省调用)。
    # 依据 core/policy.py 决策表: 追加语义标签 (BROKEN_LOGIC/WRONG_SELECTION/OBS_NOISE)
    # 仅对 THOUGHT_TOO_LONG 的 thinking 会翻转 PRUNE 决策, 其余分支先于/等价于语义分支。
    llm_vote_skip_rule_decidable: bool = True
    # CU 注入 prompt 的最大字符预算
    cu_prompt_max_chars: int = 4000
    # CU archive 子集策略: "full" 使用完整 archive; "referenced" 仅使用 referenced_by/depends_on 相关条目
    cu_prompt_archive_strategy: str = "referenced"
    # 折叠失败 toolresult / 重复 thinking 时是否使用 CU 保护被引用 block
    fold_use_cu: bool = True
    # fold 失败重试时, 引用保护是否只看 active window 中 thinking/text 类型
    # (默认 True)。设为 False 时退回旧行为, 任何 referenced_by 都算保护。
    fold_protect_active_text_only: bool = True

    # === P0 方案 ②: 重试循环 LLM 判剪枝 ===
    # 启用: 对连续 ≥min_consecutive 次同函数调用且结果全 429 的段, 走 LLM
    # 判定"是否同一意图反复重试"; LLM 同意则保留 1-3 条 (含至少 1 失败),
    # 其余删除. LLM 任何异常 → 保守 fallback 保留原状.
    retry_loop_clip_enabled: bool = True
    retry_loop_clip_min_consecutive: int = 5
    retry_loop_clip_max_keep: int = 3
    # LLM 调用超时; 超时/失败 → 整段保留.
    retry_loop_clip_llm_timeout_s: int = 60

    max_retries_9b: int = 2

    tools_config_path: Path = Path("./config/tools.yaml")

    # === 工具白名单来源 ===
    # auto: QwenPaw agent.json 的 enabled builtin_tools (权威源) ∪ tools.yaml 补充名单
    #       (extra_tools, 放 Skill 等运行时动态工具); agent.json 读不到时退回纯
    #       tools.yaml 名单 (旧行为)。
    # manual: 仅 tools.yaml 名单。off: 空白名单 (router / tool_fixer / L1 sanity
    #         全部跳过名称校验)。
    # 名单会漂移的兜底: pipeline/runner.process_one 会把会话中出现但不在白名单里的
    # 工具名写入 session.metadata["unknown_tool_names"] 并告警, 漂移自动浮出。
    tool_source: str = "auto"
    qwenpaw_agent_json: Path = DEFAULT_QWENPAW_AGENT_JSON

    input_path: Path = Path("../output/agent_trajectory")  # 默认单文件入口路径；批量模式用 batch_input_dir
    output_path: Path = Path("./refine_data/output.json")
    log_dir: Path = Path("./logs")

    llm_timeout_s: int = 120
    l3_timeout_s: int = 60

    max_compression_ratio: float = 1.50
    thought_min_len: int = 20
    thought_max_len: int = 500
    thought_max_len_l1: int = 2000
    # 修复 P1.2: 9B/32B 重写输出经常以单字符之差超过 thought_max_len (如
    # 501 vs 500), 直接 ValueError 丢弃整 block 太刚性. 允许在上限基础上
    # 额外加 ``thought_max_len_grace_pct`` 的余量 (默认 10%) 才判 length
    # out of range. 余量仅作用于长度校验, 不影响 judge 终评等其他流程.
    thought_max_len_grace_pct: int = 10

    context_switch_threshold: int = 3
    repetitive_call_threshold: int = 3

    message_health_min_ratio: float = 0.3
    max_failures_before_success: int = 8

    enable_text_fact_check: bool = True

    # === 上下文理解 - 近期窗口 ===
    context_active_window_size: int = 4          # 近期窗口消息数 (3~5 推荐)
    context_relevance_threshold: float = 0.6     # 相关性阈值
    context_redundancy_threshold: float = 0.85   # 判定"窗口内已存在等价版本"的语义相似度阈值
    enable_context_understanding: bool = True    # 是否启用 context_understanding 模块 (False 则退化为旧 ±2 上下文)
    enable_jieba_entity_extraction: bool = True  # 实体抽取时是否启用 jieba.analyse.extract_tags (False 则回退到旧 1~4 字 CJK 窗口; jieba 未安装时自动降级)

    # === 上下文理解 - 分级压缩 ===
    context_max_archive_chars: int = 80000       # archive 总字符上限 (默认 80k, 留 4 倍余量)
    context_max_t0_entries: int = 200            # T0 全文条目上限, 超出触发 T0 合并摘要
    context_compression_strategy: str = "hybrid" # rule / llm / hybrid (P0 阶段仅实现 rule)
    context_max_llm_compressions: int = 3        # 单 session LLM 压缩调用上限 (防爆量)

    # === Tier 阈值 (重要性分数 → 级别) ===
    context_tier0_threshold: float = 0.7         # ≥ 0.7 → T0 全文
    context_tier1_threshold: float = 0.5         # 0.5~0.7 → T1 详述
    context_tier2_threshold: float = 0.3         # 0.3~0.5 → T2 简述
    context_tier3_threshold: float = 0.1         # 0.1~0.3 → T3 指针
    # < 0.1 → T4 丢弃

    # === 重要性评分子项权重 ===
    context_importance_w_error:    float = 0.30  # 错误/失败信号
    context_importance_w_transit:  float = 0.25  # 转折点
    context_importance_w_refs:     float = 0.20  # 被引用次数
    context_importance_w_finality: float = 0.15  # 唯一/最终成功尝试
    context_importance_w_novelty:  float = 0.10  # 新实体占比

    # === 决策层 ===
    enable_policy_layer: bool = True             # 是否启用 policy 决策层 (False 则全部 REPAIR_IN_PLACE)
    policy_defer_on_exhausted: bool = True       # REPAIR 失败耗尽是否转为 DEFER (而不是丢弃)
    policy_prune_with_pair_enabled: bool = False # 是否启用"连带 user 删除" (默认关闭，保守)
    policy_min_redundancy_for_prune: int = 1     # 窗口内至少 N 个等价版本才允许 PRUNE (默认 1: 任意已有等价版本即触发)

    # === 增量状态追踪（方案 §3） ===
    context_state_tracker_enabled: bool = True   # 是否启用增量状态追踪 (默认开启, 无回退开关)
    context_chunk_max_tool_pairs: int = 3        # 每 Chunk 最大 toolcall-toolresult 对数
    context_max_state_llm_calls: int = 20        # 单 session 状态追踪 LLM 调用上限
    context_state_max_retries: int = 1           # 单 chunk 状态更新失败重试次数
    state_escalate_to_tool_model: bool = False   # 复杂歧义场景是否升级 32B
    # context_state_model 默认使用 main_model (9B 小模型优先)

    # === Session 级硬过滤（方案 §5.1） ===
    session_hard_filter_enabled: bool = True     # 是否启用 session 级硬过滤
    session_max_blocks: int = 500                # 单 session 最大 block 数（覆盖 1ce... 类长 session；用户主旨：数据完整即处理并导出）

    # === 失败调用处理模式（方案 §5.3） ===
    failure_handling_mode: str = "clean"         # clean / robust / drop
    robust_max_failure_streak: int = 3           # robust 模式允许的最大连续失败次数

    # === 一致性校验（方案 §5.4） ===
    enable_edit_consistency_check: bool = True    # 编辑前后状态快照校验开关
    consistency_rollback_on_entity_loss: bool = True  # 关键字段丢失时自动回滚
    consistency_max_llm_calls: int = 40           # 一致性校验状态重算 LLM 调用预算 (含重试与复核), 超出标记 needs_review
    # 前后状态均为 LLM 压缩摘要, 精确比较差集误报率高: 低于该相似度才算真丢失
    consistency_constraint_similarity: float = 0.6
    # 回滚前由 LLM 复核"丢失"是真丢失还是摘要改写漂移; 复核失败=不回滚 (数据保全优先)
    consistency_semantic_confirm: bool = True

    # === 训练质量评分（方案 §5.2） ===
    enable_quality_scorer: bool = True           # 训练质量维度评分开关
    # P0-1.2: quality_scorer 子权重与分桶阈值。组合 health / judge /
    # intent_fulfillment / modified_blocks / tool_diversity / noise /
    # depth 七维信号, 输出 [0,1] 的 training_value_score + complexity_tier.
    # 权重和阈值仅生效在 enable_quality_scorer=True 且 _attach_metadata
    # 之前被 compute_quality_score 调用的场景, 默认值在常规批次上
    # 训练价值分布大致均匀 (easy/medium/hard 各 ~33%).
    quality_scorer_weight_health: float = 0.25
    quality_scorer_weight_judge: float = 0.25
    quality_scorer_weight_intent: float = 0.20
    quality_scorer_weight_modified: float = 0.10
    quality_scorer_weight_diversity: float = 0.10
    quality_scorer_weight_noise: float = 0.05
    quality_scorer_weight_depth: float = 0.05
    # complexity_tier 分桶阈值: score >= easy_max → easy;
    #   score >= medium_max → medium; 否则 hard.
    quality_scorer_tier_easy_max: float = 0.70
    quality_scorer_tier_medium_max: float = 0.40
    # === P0-1.1: user_intent 抽取（1 次轻量 LLM 调用）===
    enable_user_intent_extraction: bool = True   # 是否抽取 user_intent
    user_intent_max_chars: int = 1500            # 送 LLM 的首条 user 原文上限
    user_intent_min_chars_for_extract: int = 20  # 首条 user 太短则跳过抽取
    user_intent_model: Optional[str] = None      # None=走 main_model (9B)
    user_intent_max_tokens: int = 1024           # 抽取步骤 LLM 输出预算
    # === P0-1.3: 工具与意图无关 (TOOL_OFF_TOPIC) 检测 ===
    enable_tool_off_topic_detection: bool = True
    tool_off_topic_use_blacklist: bool = True    # 启用 tools.yaml off_topic_blacklist
    tool_off_topic_use_embedding: bool = True    # 启用 user_intent × tool_desc 嵌入相似度
    tool_off_topic_embed_threshold: float = 0.30 # 余弦相似度低于此值判 off-topic
    # 同名工具的 description 在单 session 内缓存一次 (避免重复嵌入)

    # === P0-R fix: 随机保留 unused 工具作为 SFT 噪声 ===
    # 真实部署中 agent 面对完整工具菜单, SFT 训练样本若只展示被调工具, 会
    # 让模型学到"工具列表短 = 该调用"的错误相关. 该组配置控制从 unused
    # 池随机保留若干个未用工具, 让模型学到"长工具列表 ≠ 该全调".
    # - strategy="none": 仅保留 called (回滚逃生口, 不推荐用于生产)
    # - strategy="deterministic": 按 session_id 种子从 unused 池采样
    #   (跨 session 多样, session 内确定, 可复跑)
    tools_prune_strategy: Literal["none", "deterministic"] = "deterministic"
    tools_prune_keep_unused_min: int = 4     # 至少保留 N 个未用工具
    tools_prune_keep_unused_max: int = 12    # 最多保留 N 个未用工具
    tools_prune_keep_unused_ratio: float = 0.3  # 按 unused 池比例 (与 max 取 min)

    # === 人工审核队列（方案 §5.5） ===
    deferred_output_path: Path = Path("./refine_data/deferred.jsonl")  # 人工审核队列输出路径

    # === Usage Prune 前移到 gdr (方案 etl-prune-frontload.md) ===
    # gdr 末尾 step 22 调用 prune_session_in_place, 让写出的 C2 refined
    # Session 天然是已精简 + 已脱敏形态 (CLAUDE.md 隐私红线级别).
    # 关闭后回退到 etl 阶段裁剪 (旧行为, 仅存量重跑兼容).
    usage_prune_enabled: bool = True
    # 独立式评分 reject 旁路 (C2 不落盘, 转 audit/scoring_reject.jsonl)
    scoring_reject_audit_enabled: bool = True
    scoring_reject_output_path: Path = Path("./audit/scoring_reject.jsonl")

    # === 批量 + 并行 ===
    batch_input_dir: Optional[Path] = None
    batch_output_dir: Optional[Path] = None
    workers: int = 2           # 1 = 单进程顺序; >1 = multiprocessing.Pool (批量模式进程数)
    llm_concurrency: int = 4   # 单进程内 LLM 请求并发上限 (投票/refiner 线程池 + 生成信号量)
    max_files: Optional[int] = None  # 限制本次处理的输入文件数 (None = 全部)
    session_timeout_s: int = 1200  # 单条 session 处理超时（CU 构建 ~300s + 路由 + refine + reassemble 余量）

    # === 严格性 ===
    strict_consistency: bool = True  # 一致性终检异常时是否丢弃
    judge_min_score: int = 7        # 终检 judge 进主输出的最低分 (0-10), 0 = 关闭
    # 终检 judge 阈值豁免: 当 refiner 修改的 block 很少时, L3 judge 实际判的是
    # 原 trajectory 内部自洽度, 跟"精修质量"已解耦. search-heavy 类任务经常
    # 因为远端本身反复重设 query / 自我怀疑, 被 L3 一致性严打; 而 refiner
    # 几乎不动原内容. 此时允许放宽阈值, 让合格样本进主输出, 但仍走
    # judge_low 旁路, 留下完整审计. 仅当 modified_blocks <= N 时生效.
    judge_min_score_relaxed: int = 3     # 放宽后的最低分 (0-10), 0 = 关闭豁免
    judge_min_modified_for_relaxation: int = 5  # modified_blocks <= 此值才走放宽
    # L3 judge 输出预算 (max_tokens). reasoning 模型 (Qwen3.5 / DeepSeek 系 / o1
    # 类) 把思考链计入 max_tokens, 2048 经常被思考吃掉, JSON 只剩半截 → 解析
    # 失败 → score=0 → 误判 discard. 默认给到 36k 留足空间, 不在代码侧设上限:
    # 后端 (llama.cpp / vLLM / Ollama) 会按自己的 n_ctx / max-model-len 自然
    # 截断, 服务端可控. 该字段同时供 validators/l3_judge.py (per-block L3)
    # 和 reassembler.py (end-to-end L3) 复用, 统一口径避免一处调一处忘。
    judge_max_tokens: int = 36000
    # Router LLM 投票输出预算 (max_tokens). 与 judge_max_tokens 同根问题
    # (reasoning 模型思考计入 max_tokens, 1024 思考就吃光, parse_json_object 拿不到
    # {"has_defect": ...} → router 弃权, 落到 routing_low.jsonl 旁路)。默认 36k,
    # 不在代码侧硬截; 后端按自己的 n_ctx / max-model-len 自然截断。投票场景下
    # 答案本应很短 ("has_defect: true/false"), 实际收到 36k 上限也只触发一次
    # finish_reason=stop, 不会浪费推理预算 — reasoning 模型思考 + 短答案 8k~16k
    # 通常足够, 36k 仅给极端长思考链留缓冲。
    llm_vote_max_tokens: int = 36000
    # judge 低分 session 不丢: 完整精修结果另存审核通道, 供人工检查/后期修改后手动并回。
    # 真正硬丢弃只发生在结构严重不可用时 (见 pipeline/runner._session_structurally_unusable)。
    judge_low_export_enabled: bool = True
    judge_low_output_path: Path = Path("./refine_data/judge_low.jsonl")
    # 修复 P1.3: LLM 投票层弃权 (解析失败/请求异常) 的 block 单独落 audit
    # jsonl, 与 judge_low 同级但独立; 操作者可按需复核. 失败 block 不再让
    # session 被丢, 也不让 session 静默"语义标签不全" — 至少看得到丢了谁。
    routing_abstain_audit_enabled: bool = True
    routing_abstain_audit_path: Path = Path("./refine_data/routing_low.jsonl")
    # 未闭合 session 防呆 (修复方向 #完整性检测): 远端 sim 端把仍在跑
    # (尾巴 toolcall 没 toolresult / text 没构成完整回复) 的 trajectory 当
    # "已完成"提交时, gdr 侧把整 session 路由到 incomplete.jsonl 旁路, 不写
    # refine_data. 避免:
    #   1. SFT 用"agent 在工具调用中途被打断"作为正例, 训练出截断响应模式
    #   2. judge 在不完整证据上判分, 噪声被吸收进训练集
    # 触发条件 (见 pipeline/runner._detect_incomplete_session):
    #   - 最后一条 assistant 的最后一个 block 是 toolcall (无对应 toolresult)
    #   - toolcall 计数 > toolresult 计数 (尾部配对缺失)
    #   - 最后一条 assistant 的最后 text block 不构成结论 (启发式, 低强度)
    # 关闭: incomplete_detection_enabled=False 退回旧行为 (仍写 refine_data,
    # 仅在 metadata 标 incomplete=True 供后续过滤)。默认 True。
    incomplete_detection_enabled: bool = True
    incomplete_output_path: Path = Path("./refine_data/incomplete.jsonl")
    # F2 fix: 末尾 assistant 仅含 thinking (无 final text) 时的字符阈值;
    # thinking_chars ≥ 该值且无未配对 toolcall 时判 incomplete.
    incomplete_thinking_only_min_chars: int = 200

    # === F1 fix: tools 字段透传到 refine_data 视图 ===
    # 控制 messages.json / openai.json 顶层是否写入 metadata["tools"].
    # qwenjina.txt 已是 qf_text 渲染产物 (渲染时已传 tools), meta.json
    # 一直含 tools. 默认开启; 关闭后恢复旧行为, 便于回滚与对比.
    include_tools_in_payloads: bool = True
    # 单 session 写入的 schema 上限, 防止 SFT 训练样本被超长 schema 拖慢.
    # 0 = 不截断.
    tools_payload_max: int = 64

    # === Fix A: judge_low.jsonl 字段展开 ===
    # 把 session.metadata["judge_discard"] 中的 reason / relaxed_kind /
    # modified_blocks 等字段扁平化到 judge_low.jsonl 的顶层 judge 对象,
    # 便于审计/grep. 关闭则仅写 {score, min_score} (旧行为, 兼容旧 reader).
    judge_low_include_reason: bool = True

    # === Fix B: judge_min_score 三段阶梯阈值 ===
    # L3 judge 给分是轨迹整体自洽度 (含原始 trajectory 风格), refiner 编辑
    # 质量会显著拉低该分. 按 modified_blocks 数量分档, 编辑越少 → 阈值越低,
    # 让 search-heavy 任务或 system 清洗类样本不被一票打死. 阶梯顺序:
    #   passthrough (modified<=passthrough_threshold) → judge_min_score_passthrough
    #   low_edit    (modified<=low_edit_threshold)    → judge_min_score_low_edit
    #   relaxed     (modified<=relaxed_threshold)     → judge_min_score_relaxed
    # 否则用 judge_min_score 严格阈值. 关闭阶梯: 把对应 threshold 设为 0.
    judge_min_modified_passthrough: int = 1   # ≤1 modified blocks 视为基本未改
    judge_min_score_passthrough: int = 2     # passthrough 档最低分门槛
    judge_min_modified_low_edit: int = 3     # ≤3 modified blocks 视为轻编辑
    judge_min_score_low_edit: int = 5        # low_edit 档最低分门槛
    # 既有 relaxed 阈值保留 (≤5 modified blocks 时放宽到 3 分)
    # judge_min_modified_for_relaxation: int = 5
    # judge_min_score_relaxed: int = 3
    # 阶梯触发后, 是否在 metadata 留 judge_relaxation.note 字段.
    judge_relaxed_audit_note: bool = True

    # === 两层评分系统 (方案 trajectory-scoring-two-layer.md) ===
    # 第一层: 对比式评分 (Reference-based, 原始轨迹 vs 修改后轨迹)
    enable_trajectory_compare: bool = True
    compare_fidelity_llm: bool = True           # fidelity 三要素抽取用 LLM
    compare_diff_classifier: str = "rule_first"  # rule_first / llm_only / hybrid
    compare_coherence_reuse_reassembly: bool = True  # 复用 reassembly 配对扫描
    compare_max_retries: int = 2                # 对比式 fail 回精修的最大重试轮数

    # 第二层: 独立式评分 (Reference-free, 只看修改后轨迹)
    enable_free_quality: bool = True
    enable_redline: bool = True
    # 红线合规: 规则层黑名单 (正则模式); 空列表 = 该类红线不检查
    redline_piracy_url_patterns: list[str] = Field(default_factory=list)
    redline_privacy_patterns: list[str] = Field(default_factory=list)
    redline_prompt_injection_patterns: list[str] = Field(default_factory=list)
    redline_tos_violation_selectors: list[str] = Field(default_factory=list)
    redline_llm_review_suspicious: bool = True  # 可疑项 LLM 复核
    # 绝对质量分 (1-5 分制) 放行阈值
    absolute_quality_min_score: int = 4         # 总分 ≥ 此值才放行
    absolute_quality_min_subscore_executability: int = 4  # 子分门槛
    absolute_quality_min_subscore_action_obs: int = 4
    absolute_quality_score_mapping: str = "linear_tier"   # [0,1]→1-5 映射策略

    # 金标集 / 漂移监控
    golden_set_enabled: bool = False
    golden_set_path: Path = Path("./data/golden_trajectories")
    golden_set_anchor_count: int = 50           # 锚点轨迹数
    golden_set_drift_threshold: float = 0.3     # 漂移告警阈值
    golden_set_drift_action: str = "alert"      # alert / rollback_model / recalibrate
    golden_set_probe_pairs_per_batch: int = 20  # 每批混入探针对数

    # === 评估器 (utility eval, 设计文档 §13) ===
    evaluator_output_dir: Path = Path("./evaluator_output")
    probe_base_model_name: str = "Qwen/Qwen3.5-9B-Instruct"  # HF repo 名（探测训练基座）
    probe_train_data_dir: Path = Path("./data/sft_pairs")
    probe_lora_r: int = 16
    probe_lora_alpha: int = 32
    probe_epochs: int = 3
    probe_batch_size: int = 4
    probe_learning_rate: float = 2e-4

    eval_set_path: Path = Path("./data/eval_set.jsonl")
    eval_held_out_size: int = 200  # 从 D 中随机保留的探针评测集大小
    retention_threshold: float = 0.97  # 探针相对原 D 训练模型保留性 ≥ 阈值
    removal_threshold: float = 0.50    # 探针相对原 D 训练模型剔除性 ≥ 阈值
    max_feedback_iterations: int = 3   # 反馈回路最大重试轮数

    # === Langfuse 可观测性 (PR 3, 扁平字段) ===
    # gdr 端 Settings 字段扁平 (与现有 llm_* / context_* 风格一致), 不破坏
    # env_prefix="GDR_" 的扁平覆盖语义。工厂 _extract_langfuse_fields 鸭子
    # 类型兼容嵌套 / 扁平两种形态, 这里只负责"扁平字段 + 兜底"。
    langfuse_enabled: bool = False
    # 21 步骤逐个起 step_span (默认 true; 上传成本可控时强烈推荐)
    langfuse_gdr_per_step_span: bool = True
    # LlamaCppClient.chat 级 generation span (默认 false; 100+ spans/session 配额爆炸)
    langfuse_gdr_per_llm_span: bool = False
    # 每条 repair_item 一个 span (高级; 默认 false, 默认外层 gdr.refine.run_repairs 一个)
    langfuse_gdr_per_refine_span: bool = False
    langfuse_upload_payload: Literal["full", "summary", "none"] | None = None
    langfuse_max_payload_bytes: int = 0
    langfuse_max_block_payload_bytes: int = 0

    @model_validator(mode="after")
    def _anchor_relative_paths(self):
        """相对路径配置锚定到 gdr 根 (不依赖调用方 CWD)。

        绝对路径原样保留, 允许显式覆盖 (env GDR_TOOLS_CONFIG_PATH / yaml)。
        """
        p = Path(self.tools_config_path)
        if not p.is_absolute():
            p = (GDR_ROOT / p).resolve()
            log.info("tools_config_path anchored to gdr root: %s", p)
        self.tools_config_path = p
        return self

    @model_validator(mode="after")
    def _warn_non_strict_consistency(self):
        if not self.strict_consistency:
            log.warning(
                "strict_consistency is disabled: end-to-end consistency failures "
                "will not cause session discard. Not recommended for production."
            )
        return self

    # 配置源优先级 (高 → 低): init(kwargs) > 环境变量 (GDR_*) > 统一根配置
    # (仓库根 config/config.yaml 的 gdr: 段, 含 llm: 共享段缺省) > 字段默认值
    # 注意: 类体内带下划线前缀的属性会被 pydantic 视为私有属性 (ModelPrivateAttr),
    #       所以常量必须以模块级形式存在, 不能放进类体。
    model_config = SettingsConfigDict(
        env_prefix="GDR_",   # 进程环境变量 (GDR_*) 临时覆盖根配置
        extra="ignore",      # 未知 env 键静默忽略 (如旧的 GDR_MAX_RETRIES)
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            _RootConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


class _RootConfigSettingsSource(PydanticBaseSettingsSource):
    """统一根配置 ``gdr:`` 段的 pydantic-settings source。

    位置在 env (GDR_*) 之后、字段默认值之前: 根配置只覆盖其中出现的
    字段, 其余走代码默认值。根配置缺失时抛 FileNotFoundError。
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data = _load_root_gdr_section()

    def get_field_value(self, field, field_name: str):  # type: ignore[override]
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return {
            k: v for k, v in self._data.items()
            if v is not None and k in self.settings_cls.model_fields
        }


def load_agent_tools(agent_json_path: Path) -> list[str] | None:
    """从 QwenPaw agent.json 解析 enabled 的 builtin 工具名; 读不到/为空返回 None。

    agent.json 由 QwenPaw 维护, 是远端 agent 真实工具集的权威来源 (随工具集变化
    自动更新, 不需要手工同步); 技能运行时注入的动态工具 (如 Skill) 不在其中,
    由 tools.yaml extra_tools 补充。
    """
    try:
        with open(Path(agent_json_path).expanduser(), "r", encoding="utf-8") as f:
            data = json.load(f)
        builtin = (data.get("tools") or {}).get("builtin_tools") or {}
        names = sorted(
            name for name, spec in builtin.items()
            if isinstance(spec, dict) and spec.get("enabled", True)
        )
        if not names:
            log.warning("agent.json %s 中没有 enabled 的 builtin_tools", agent_json_path)
            return None
        return names
    except Exception as e:
        log.warning("failed to load agent tool list from %s (%s)", agent_json_path, e)
        return None


def load_tools(
    tools_config_path: Path,
    agent_json_path: Path | None = None,
    tool_source: str = "auto",
) -> tuple[list[str], set[str], dict[str, str], set[str]]:
    """解析工具白名单 + 幻觉 API 黑名单 + 工具语义描述 + off-topic 黑名单。

    返回 (tool_names, hallucinated_apis, tool_descriptions, off_topic_blacklist):
      tool_names           白名单 (列表, 经过去重+排序)
      hallucinated_apis    已知坏字符串黑名单 (路由/tool_fixer/L1 用)
      tool_descriptions    工具名 → 一句话描述 (P0-1.3 TOOL_OFF_TOPIC 嵌入层用)
      off_topic_blacklist  TOOL_OFF_TOPIC 规则层黑名单 (P0-1.3)

    白名单三级合并 (tool_source=auto, 默认):
      agent.json enabled builtin_tools (权威源) ∪ tools.yaml extra_tools (动态工具补充);
      agent.json 读不到 → 退回纯 tools.yaml 名单 (旧行为); 两者皆空 → 空白名单,
      router / tool_fixer / L1 sanity 全部跳过名称校验, 避免级联误杀真实数据。

    tool_descriptions / off_topic_blacklist 仅从 tools.yaml 读取, 不与 agent.json
    合并 (描述是手工语义, agent.json 没有这层信息)。未在 yaml 列出的工具, 嵌入
    层无法判定, router 走跳过 (不算 off-topic, 也不算 hit)。
    """
    manual_tools: list[str] = []
    hallucinated_apis: set[str] = set()
    tool_descriptions: dict[str, str] = {}
    off_topic_blacklist: set[str] = set()
    try:
        with open(tools_config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # extra_tools 为补充名单 (旧键 tools 兼容)
        manual_tools = list(data.get("extra_tools") or data.get("tools") or [])
        hallucinated_apis = set(data.get("hallucinated_apis") or [])
        # P0-1.3: 工具描述 + off-topic 黑名单 (仅 yaml, 不与 agent.json 合并)
        desc_raw = data.get("tool_descriptions") or {}
        if isinstance(desc_raw, dict):
            tool_descriptions = {
                str(k): str(v).strip()
                for k, v in desc_raw.items()
                if v and str(v).strip()
            }
        ot_raw = data.get("off_topic_blacklist") or []
        if isinstance(ot_raw, list):
            off_topic_blacklist = {str(x).strip() for x in ot_raw if str(x).strip()}
    except Exception as e:
        log.error(
            "failed to load tools config from %s (%s): manual whitelist unavailable, "
            "relying on auto source only; check GDR_TOOLS_CONFIG_PATH or config/config.yaml",
            tools_config_path, e,
        )

    if tool_source == "off":
        log.info("tool_source=off: 工具白名单置空, router/tool_fixer/L1 跳过名称校验")
        return [], hallucinated_apis, tool_descriptions, off_topic_blacklist

    if tool_source == "manual":
        if not manual_tools:
            log.error(
                "tool_source=manual but %s has no whitelist: tool-name hallucination "
                "checks are skipped downstream (router / tool_fixer / L1 sanity)",
                tools_config_path,
            )
        return manual_tools, hallucinated_apis, tool_descriptions, off_topic_blacklist

    # auto: agent.json 权威源 ∪ 手工补充名单
    agent_path = Path(agent_json_path) if agent_json_path else DEFAULT_QWENPAW_AGENT_JSON
    auto_tools = load_agent_tools(agent_path)
    if auto_tools is None:
        log.warning(
            "agent tool list unavailable (%s): falling back to manual tools.yaml "
            "whitelist only — fix qwenpaw_agent_json or keep extra_tools in sync",
            agent_path,
        )
        if not manual_tools:
            log.error(
                "tool whitelist resolved EMPTY (agent.json unreadable and %s empty): "
                "tool-name hallucination checks are skipped downstream (router / "
                "tool_fixer / L1 sanity) to avoid mass false discards",
                tools_config_path,
            )
        return manual_tools, hallucinated_apis, tool_descriptions, off_topic_blacklist

    merged = sorted(set(auto_tools) | set(manual_tools))
    log.info(
        "tool whitelist: %d enabled builtin tool(s) from %s ∪ %d extra_tool(s) "
        "from %s → %d tools (descriptions=%d, off_topic_blacklist=%d)",
        len(auto_tools), agent_path, len(manual_tools), tools_config_path,
        len(merged), len(tool_descriptions), len(off_topic_blacklist),
    )
    return merged, hallucinated_apis, tool_descriptions, off_topic_blacklist