from pathlib import Path
from typing import Optional
import json

from pydantic_settings import BaseSettings, SettingsConfigDict, PydanticBaseSettingsSource, YamlConfigSettingsSource
from pydantic import model_validator
import logging
import yaml

log = logging.getLogger(__name__)

# 主 yaml 配置文件的绝对路径 (基于本文件位置定位, 不依赖 CWD)
YAML_FILE = Path(__file__).resolve().parent / "gdr_config.yaml"

# gdr 包根目录: 相对路径配置一律锚定到这里, 不依赖调用方 CWD。
# (orchestration master 从仓库根运行时, './config/tools.yaml' 在 CWD 下
#  解析不到 → 空白名单 → 所有 toolcall 被级联误判 TOOL_HALLUCINATED 并丢弃。)
GDR_ROOT = Path(__file__).resolve().parent.parent

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

    input_path: Path = Path("./data/input.json")
    output_path: Path = Path("./refine_data/output.json")
    log_dir: Path = Path("./logs")

    llm_timeout_s: int = 120
    l3_timeout_s: int = 60

    max_compression_ratio: float = 1.50
    thought_min_len: int = 20
    thought_max_len: int = 500
    thought_max_len_l1: int = 2000

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
    consistency_max_llm_calls: int = 40           # 一致性校验状态重算 LLM 调用预算 (含重试), 超出标记 needs_review

    # === 训练质量评分（方案 §5.2） ===
    enable_quality_scorer: bool = True           # 训练质量维度评分开关

    # === 人工审核队列（方案 §5.5） ===
    deferred_output_path: Path = Path("./refine_data/deferred.jsonl")  # 人工审核队列输出路径

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
    # judge 低分 session 不丢: 完整精修结果另存审核通道, 供人工检查/后期修改后手动并回。
    # 真正硬丢弃只发生在结构严重不可用时 (见 pipeline/runner._session_structurally_unusable)。
    judge_low_export_enabled: bool = True
    judge_low_output_path: Path = Path("./refine_data/judge_low.jsonl")

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

    # 主配置源: gdr/config/gdr_config.yaml; 环境变量 (GDR_*) 与 .env 有更高优先级的覆盖权
    # 注意: 类体内带下划线前缀的属性会被 pydantic 视为私有属性 (ModelPrivateAttr),
    #       所以 yaml 路径必须以模块级常量存在, 不能放进类体。
    model_config = SettingsConfigDict(
        env_prefix="GDR_",   # 进程环境变量 (GDR_*) 临时覆盖 yaml
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
        # 优先级: init(kwargs) > env(GDR_*) > yaml > secrets > 字段默认值
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(
                settings_cls, yaml_file=YAML_FILE, yaml_file_encoding="utf-8",
            ),
            file_secret_settings,
        )


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
) -> tuple[list[str], set[str]]:
    """解析工具白名单 + 幻觉 API 黑名单。

    白名单三级合并 (tool_source=auto, 默认):
      agent.json enabled builtin_tools (权威源) ∪ tools.yaml extra_tools (动态工具补充);
      agent.json 读不到 → 退回纯 tools.yaml 名单 (旧行为); 两者皆空 → 空白名单,
      router / tool_fixer / L1 sanity 全部跳过名称校验, 避免级联误杀真实数据。

    hallucinated_apis 是已知坏字符串黑名单, 始终手工维护, 不参与自动来源。
    """
    manual_tools: list[str] = []
    hallucinated_apis: set[str] = set()
    try:
        with open(tools_config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # extra_tools 为补充名单 (旧键 tools 兼容)
        manual_tools = list(data.get("extra_tools") or data.get("tools") or [])
        hallucinated_apis = set(data.get("hallucinated_apis") or [])
    except Exception as e:
        log.error(
            "failed to load tools config from %s (%s): manual whitelist unavailable, "
            "relying on auto source only; check GDR_TOOLS_CONFIG_PATH or gdr_config.yaml",
            tools_config_path, e,
        )

    if tool_source == "off":
        log.info("tool_source=off: 工具白名单置空, router/tool_fixer/L1 跳过名称校验")
        return [], hallucinated_apis

    if tool_source == "manual":
        if not manual_tools:
            log.error(
                "tool_source=manual but %s has no whitelist: tool-name hallucination "
                "checks are skipped downstream (router / tool_fixer / L1 sanity)",
                tools_config_path,
            )
        return manual_tools, hallucinated_apis

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
        return manual_tools, hallucinated_apis

    merged = sorted(set(auto_tools) | set(manual_tools))
    log.info(
        "tool whitelist: %d enabled builtin tool(s) from %s ∪ %d extra_tool(s) "
        "from %s → %d tools",
        len(auto_tools), agent_path, len(manual_tools), tools_config_path, len(merged),
    )
    return merged, hallucinated_apis