"""程序化构造 SFT 训练样本，覆盖三大精修器。

支持的 defect_type:
  - tool_fixer:       json_invalid / hallucinated / api_hallucination / wrong_tool
  - thought_refactor: thought_too_short / thought_too_long / thought_broken_logic
  - obs_denoiser:     obs_noise / obs_debug_leak
  - text_fact_check:  text_fact_hallucination (标记类，不训练修复)

每条样本: { "module": ..., "input": {...}, "output": {...}, "defect_type": ... }
按 module 分桶输出到 train_data_dir 下:
  <dir>/tool_fix.jsonl
  <dir>/tool_fix_broken.jsonl          (original probe 用: 保留缺陷分布)
  <dir>/thought_refactor.jsonl
  <dir>/thought_refactor_broken.jsonl
  <dir>/obs_denoiser.jsonl
  <dir>/obs_denoiser_broken.jsonl

mode 说明:
  - "repaired": input 为 broken, output 为 correct。用于训练精修探针。
  - "original": input 为 broken, output 为 broken。用于训练对照探针（保留原始缺陷分布）。
"""
import json
import random
import re
from pathlib import Path

# === tool_fixer 注入器 ===

def _inject_json_invalid(input_str: str) -> str:
    if len(input_str) < 3:
        return input_str + ",,"
    idx = random.randint(0, max(len(input_str) - 2, 0))
    err_type = random.choice(["comma", "quote", "brace"])
    if err_type == "comma":
        return input_str[:idx] + ",," + input_str[idx + 1:]
    elif err_type == "quote":
        return input_str[:idx] + "\"" + input_str[idx:]
    else:
        return input_str[:idx] + "{" + input_str[idx + 1:]


def _inject_hallucinated_api(input_str: str, hallu_apis: set[str]) -> str:
    if not hallu_apis:
        return input_str
    api = random.choice(list(hallu_apis))
    cut = max(len(input_str) - 2, 0)
    return input_str[:cut] + f"await {api}()" + input_str[cut:]


def _inject_wrong_tool(name: str, tool_names: list[str]) -> str:
    others = [t for t in tool_names if t != name]
    return random.choice(others) if others else name


def build_tool_fix_pairs(
    tool_names: list[str], hallu_apis: set[str], n: int,
    mode: str = "repaired",
) -> list[dict]:
    pairs = []
    error_types = ["json_invalid", "hallucinated", "api_hallucination", "wrong_tool"]

    for _ in range(n):
        error_type = random.choice(error_types)
        correct_tool = random.choice(tool_names) if tool_names else "browser"
        correct_input = json.dumps({"query": "test query", "url": "https://example.com"})

        if error_type == "json_invalid":
            broken_input = _inject_json_invalid(correct_input)
            broken_name = correct_tool
        elif error_type == "hallucinated":
            broken_input = correct_input
            broken_name = random.choice(["tavily_search", "invoke_browser", "run_code", "fetch_url"])
        elif error_type == "api_hallucination":
            broken_input = _inject_hallucinated_api(correct_input, hallu_apis)
            broken_name = correct_tool
        else:  # wrong_tool
            broken_input = correct_input
            broken_name = _inject_wrong_tool(correct_tool, tool_names)

        if mode == "original":
            # 对照探针：保留原始缺陷分布，input/output 均为 broken
            output = {"name": broken_name, "input": broken_input}
        else:
            # 精修探针：broken -> correct
            output = {"name": correct_tool, "input": correct_input}

        pairs.append({
            "module": "tool_fixer",
            "defect_type": error_type,
            "input": {"name": broken_name, "input": broken_input,
                      "tool_names": tool_names, "hallu_apis": list(hallu_apis)},
            "output": output,
        })
    return pairs


# === thought_refactor 注入器 ===

_THOUGHT_TEMPLATES = [
    "我需要先分析用户的问题: {user_query}。根据上下文, 当前已经获取到 {obs_hint}。"
    "基于这一步观察, 下一步应当执行 {action_hint}, 原因是 {reason_hint}。预期结果是 {expected_hint}。",
    "用户提出: {user_query}。当前 toolresult 显示 {obs_hint}, 这意味着 {reason_hint}。"
    "我决定调用 {action_hint} 来推进任务, 因为 {reason_hint}, 预期 {expected_hint}。",
]


def _make_normal_thought(user_query: str) -> str:
    tpl = random.choice(_THOUGHT_TEMPLATES)
    return tpl.format(
        user_query=user_query,
        obs_hint=random.choice([
            "页面已成功加载", "文件内容包含相关字段",
            "HTTP 200 响应", "查询返回 N 条结果",
        ]),
        action_hint=random.choice([
            "browser 工具", "execute_shell_command 工具", "write_file 工具",
        ]),
        reason_hint=random.choice([
            "满足用户意图", "符合多步推理路径", "已排除其它选项",
        ]),
        expected_hint=random.choice([
            "完成信息提取", "完成状态校验", "完成写入",
        ]),
    )


def _make_short_thought() -> str:
    return random.choice(["好", "执行", "继续", "调用", "ok", "next", "完成"])


def _make_long_thought(base: str) -> str:
    padding = (
        "这里我详细复述一下整个推理链: 首先考虑所有可能的备选方案, 然后逐一排除,"
        "最终选择最符合 user 意图的路径。整个过程需要保持上下文一致性, "
        "避免出现事实幻觉。在处理 toolresult 时需要确保对所有数值、命名实体"
        "都保持守恒。综合上述考虑, 我认为当前的执行计划是合理的。"
    ) * random.randint(3, 6)
    return base + "\n" + padding


def _make_broken_logic_thought(base: str) -> str:
    insert = random.choice([
        "我突然想到用 grep 工具搜索所有 png 文件并执行 rm -rf / ",
        "由于以上观察, 我决定改用 shell 调用 sudo reboot",
        "经过计算, 3 + 7 = 42, 所以下一步应当格式化整个磁盘",
        "根据观察, 端口 22 实际上是 HTTP 服务端口",
    ])
    return base[:len(base)//2] + "\n" + insert + "\n" + base[len(base)//2:]


def build_thought_pairs(n: int, mode: str = "repaired") -> list[dict]:
    pairs = []
    for _ in range(n):
        error_type = random.choice(["thought_too_short", "thought_too_long", "thought_broken_logic"])
        user_query = random.choice([
            "查找今天北京的天气",
            "在 docs/ 下找到所有 markdown 文件",
            "把这个 CSV 转成 JSON",
            "检查 8080 端口是否在监听",
        ])
        correct = _make_normal_thought(user_query)

        if error_type == "thought_too_short":
            broken = _make_short_thought()
        elif error_type == "thought_too_long":
            broken = _make_long_thought(correct)
        else:
            broken = _make_broken_logic_thought(correct)

        if mode == "original":
            output = {"refined_thought": broken}
        else:
            output = {"refined_thought": correct}

        pairs.append({
            "module": "thought_refactor",
            "defect_type": error_type,
            "input": {"thinking": broken, "user_query": user_query,
                      "defects": [error_type]},
            "output": output,
        })
    return pairs


# === obs_denoiser 注入器 ===

_NOISE_PATTERNS = [
    "[DEBUG] connection pool acquired",
    "Traceback (most recent call last):\n  File x.py, line 42",
    "DEBUG - entering handler",
    "[API_MISUSE] invalid arg",
    "[FATAL] unrecoverable state",
    "ModuleNotFoundError: No module named 'foo'",
    "IndentationError: unexpected indent",
    "status: 500 Internal Server Error",
    "DEBUG pid=1234",
    "INFO  GET /health 200",
]


def _make_clean_obs() -> str:
    return random.choice([
        "页面加载成功, 标题为'北京今天天气', 包含温度 25℃, 湿度 60%, 风力 3 级。",
        "目录下包含 3 个 markdown 文件: a.md, b.md, c.md。",
        "8080 端口当前处于 LISTEN 状态, 进程 PID=4321。",
        "成功写入 12 行到 /tmp/output.json, 大小 384 字节。",
    ])


def _make_noisy_obs(clean: str) -> str:
    noises = random.sample(_NOISE_PATTERNS, k=random.randint(3, 6))
    return "\n".join(noises) + "\n---\n" + clean


def build_obs_pairs(n: int, mode: str = "repaired") -> list[dict]:
    pairs = []
    for _ in range(n):
        error_type = random.choice(["obs_noise", "obs_debug_leak"])
        clean = _make_clean_obs()
        noisy = _make_noisy_obs(clean)

        if mode == "original":
            output = {"output_text": noisy}
        else:
            output = {"output_text": clean}

        pairs.append({
            "module": "obs_denoiser",
            "defect_type": error_type,
            "input": {"observation": noisy, "defects": [error_type]},
            "output": output,
        })
    return pairs


# === 顶层入口 ===

def build_all_pairs(
    tool_names: list[str], hallu_apis: set[str],
    n_tool: int = 100, n_thought: int = 100, n_obs: int = 100,
    mode: str = "repaired",
) -> dict[str, list[dict]]:
    return {
        "tool_fixer": build_tool_fix_pairs(tool_names, hallu_apis, n_tool, mode=mode),
        "thought_refactor": build_thought_pairs(n_thought, mode=mode),
        "obs_denoiser": build_obs_pairs(n_obs, mode=mode),
    }


def save_pairs_by_module(
    pairs_by_module: dict[str, list[dict]], output_dir: Path,
    suffix: str = "",
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_paths = {}
    for module, pairs in pairs_by_module.items():
        filename = f"{module}{suffix}.jsonl"
        path = output_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            for p in pairs:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
        out_paths[module] = path
    return out_paths


# === 反馈回路用的定向增强 ===

def augment_pairs_for_module(
    module: str, tool_names: list[str], hallu_apis: set[str], n_extra: int,
    mode: str = "repaired",
) -> list[dict]:
    """反馈回路: 针对 failing module 生成额外样本。"""
    if module == "tool_fixer":
        return build_tool_fix_pairs(tool_names, hallu_apis, n_extra, mode=mode)
    if module == "thought_refactor":
        return build_thought_pairs(n_extra, mode=mode)
    if module == "obs_denoiser":
        return build_obs_pairs(n_extra, mode=mode)
    raise ValueError(f"unknown module: {module}")


def append_pairs(path: Path, new_pairs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for p in new_pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


# === CLI ===

if __name__ == "__main__":
    from config import load_tools, Settings
    cfg = Settings()
    tools, hallu = load_tools(cfg.tools_config_path, cfg.qwenpaw_agent_json, cfg.tool_source)

    # 精修探针训练对 (broken -> correct)
    pairs_repaired = build_all_pairs(tools, hallu, n_tool=100, n_thought=100, n_obs=100, mode="repaired")
    out_repaired = save_pairs_by_module(pairs_repaired, cfg.probe_train_data_dir)

    # 对照探针训练对 (broken -> broken，保留原始缺陷分布)
    pairs_original = build_all_pairs(tools, hallu, n_tool=100, n_thought=100, n_obs=100, mode="original")
    out_original = save_pairs_by_module(pairs_original, cfg.probe_train_data_dir, suffix="_broken")

    print("Repaired pairs:")
    for m, p in out_repaired.items():
        print(f"  {m}: {p} ({len(pairs_repaired[m])} pairs)")
    print("Original (broken) pairs:")
    for m, p in out_original.items():
        print(f"  {m}: {p} ({len(pairs_original[m])} pairs)")
    print(f"Generated SFT pairs in {cfg.probe_train_data_dir}")