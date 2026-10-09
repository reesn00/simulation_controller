"""gdr/refiners/retry_loop_ground_truth: LLM 重试循环判定的 ground truth 样本集.

构造 20+ 样本, 覆盖 4 类场景:
    - clear_retry  (≥8): 同意图反复重试, LLM 应判 is_retry_loop=True
    - not_retry    (≥5): 不同意图, LLM 应判 is_retry_loop=False
    - edge_case    (≥3): 段长/状态边界, LLM 应判 False 或拒判
    - real_world   (≥2): 从 output/refine_data 真实样本抽取 (Tavily web_search)

每条样本 schema:
    GroundTruthSample(
        id="rl_001",
        category="clear_retry",
        description="...",
        calls=[
            {"function": "web_search", "input": "...", "error": "429 ...", "state": "error"},
            ...  # 必须 ≥ 5 条
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 4],
    )

调优用法:
    from gdr.refiners.retry_loop_ground_truth import GROUND_TRUTH_SAMPLES
    from gdr.refiners.retry_loop_prompt_eval import evaluate_prompt, format_report

    client = LlamaCppClient.get("Qwen3.5-9B-Instruct", cfg=cfg)
    result = evaluate_prompt(GROUND_TRUTH_SAMPLES, client)
    print(format_report(result))
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GroundTruthSample:
    id: str
    category: str  # "clear_retry" | "not_retry" | "edge_case" | "real_world"
    description: str
    calls: list[dict[str, Any]]
    expected_is_retry_loop: bool
    expected_keep_indices: list[int] | None = None  # None 表示 not_retry / edge


def _q(query: str) -> str:
    """构造一个 Tavily 风格的 input JSON 字符串."""
    return '{"search_term": "' + query + '"}'


def _err() -> str:
    """构造 Tavily 真实 429 错误文本."""
    return "Client error '429 Too Many Requests' for url 'https://api.tavily.com/search'"


def _ok(marker: str = "") -> str:
    """构造一个 success response 文本."""
    return "search results: [" + marker + "]"


def _tc(idx: int, query: str) -> dict[str, Any]:
    """构造一条 web_search tool call + 配对 429 toolresult."""
    return {
        "function": "web_search",
        "input": _q(query),
        "error": _err(),
        "state": "error",
        # 冗余字段, 仅供人工阅读
        "_query": query,
        "_idx": idx,
    }


def _tc_other_state(idx: int, query: str, state: str, output: str) -> dict[str, Any]:
    """构造一条非 error state 的 toolresult (用于 edge case)."""
    return {
        "function": "web_search",
        "input": _q(query),
        "error": output,
        "state": state,
        "_query": query,
        "_idx": idx,
    }


# ---------------------------------------------------------------------------
# A. clear_retry: LLM 应判 is_retry_loop=True
# ---------------------------------------------------------------------------

_CLEAR_RETRY_SAMPLES: list[GroundTruthSample] = [
    # 1. 完全相同的 query
    GroundTruthSample(
        id="rl_001",
        category="clear_retry",
        description="完全相同的 query 重复 5 次",
        calls=[_tc(i, "功夫 Hustle") for i in range(5)],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 4],
    ),
    # 2. 完全相同的 query 重复 8 次
    GroundTruthSample(
        id="rl_002",
        category="clear_retry",
        description="完全相同的 query 重复 8 次 (长循环)",
        calls=[_tc(i, "功夫 Hustle") for i in range(8)],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 7],
    ),
    # 3. 标点/空格变体
    GroundTruthSample(
        id="rl_003",
        category="clear_retry",
        description="query 加空格/标点变化",
        calls=[
            _tc(0, "功夫 Hustle"),
            _tc(1, "功夫Hustle"),
            _tc(2, "功夫.Hustle"),
            _tc(3, "功夫 Hustle!"),
            _tc(4, "功夫Hustle？"),
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 4],
    ),
    # 4. 大小写变化
    GroundTruthSample(
        id="rl_004",
        category="clear_retry",
        description="英文 query 大小写变化",
        calls=[
            _tc(0, "Kung Fu Hustle"),
            _tc(1, "kung fu hustle"),
            _tc(2, "KUNG FU HUSTLE"),
            _tc(3, "Kung fu Hustle"),
            _tc(4, "Kung Fu hustle"),
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 4],
    ),
    # 5. 限定词增加
    GroundTruthSample(
        id="rl_005",
        category="clear_retry",
        description="中文 query 加限定词 (中文/正版/2024/免费)",
        calls=[
            _tc(0, "功夫 Hustle"),
            _tc(1, "功夫 Hustle 中文"),
            _tc(2, "功夫 Hustle 正版"),
            _tc(3, "功夫 Hustle 2024"),
            _tc(4, "功夫 Hustle 免费"),
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 4],
    ),
    # 6. 同义词改写 (英文)
    GroundTruthSample(
        id="rl_006",
        category="clear_retry",
        description="英文同义词改写 (tutorial/guide/how-to/walkthrough)",
        calls=[
            _tc(0, "python tutorial"),
            _tc(1, "python guide"),
            _tc(2, "python how-to"),
            _tc(3, "python walkthrough"),
            _tc(4, "python intro"),
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 4],
    ),
    # 7. 关键词顺序调整
    GroundTruthSample(
        id="rl_007",
        category="clear_retry",
        description="关键词顺序调整 (machine learning basics 系列)",
        calls=[
            _tc(0, "machine learning basics"),
            _tc(1, "basics of machine learning"),
            _tc(2, "learn machine basics"),
            _tc(3, "machine basics learning"),
            _tc(4, "learning machine basics"),
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 4],
    ),
    # 8. 重复字符 / 空格填充
    GroundTruthSample(
        id="rl_008",
        category="clear_retry",
        description="重复字符 / 空格填充变体",
        calls=[
            _tc(0, "功夫 Hustle"),
            _tc(1, "功夫  Hustle"),
            _tc(2, "功夫   Hustle"),
            _tc(3, "功夫 Hustle "),
            _tc(4, " 功夫 Hustle"),
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 4],
    ),
    # 9. URL/同义标识符变体 (代码搜索场景)
    GroundTruthSample(
        id="rl_009",
        category="clear_retry",
        description="GitHub repo 标识符变体 (org/repo vs org_repo)",
        calls=[
            _tc(0, "github.com/owner/repo"),
            _tc(1, "owner/repo github"),
            _tc(2, "owner_repo github"),
            _tc(3, "owner repo github"),
            _tc(4, "github owner repo"),
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 4],
    ),
    # 10. 数字轻微变体
    GroundTruthSample(
        id="rl_010",
        category="clear_retry",
        description="数字轻微变体 (年份 / 价格)",
        calls=[
            _tc(0, "iPhone 15 价格"),
            _tc(1, "iPhone 15 多少钱"),
            _tc(2, "iPhone 15 售价"),
            _tc(3, "iPhone 15 报价"),
            _tc(4, "iPhone 15 price"),
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 4],
    ),
]


# ---------------------------------------------------------------------------
# B. not_retry: LLM 应判 is_retry_loop=False
# ---------------------------------------------------------------------------

_NOT_RETRY_SAMPLES: list[GroundTruthSample] = [
    # 1. 完全不同搜索方向
    GroundTruthSample(
        id="nr_001",
        category="not_retry",
        description="完全不同的搜索方向 (电影→演员→导演→片尾曲→奖项)",
        calls=[
            _tc(0, "功夫 Hustle"),
            _tc(1, "周星驰 电影"),
            _tc(2, "功夫 片尾曲"),
            _tc(3, "Stephen Chow"),
            _tc(4, "功夫 获奖"),
        ],
        expected_is_retry_loop=False,
    ),
    # 2. 跨语言切换 + 主题变化
    GroundTruthSample(
        id="nr_002",
        category="not_retry",
        description="跨语言 + 主题变 (功夫→Kung Fu→Kung Fu cast→Hustle actors)",
        calls=[
            _tc(0, "功夫 Hustle"),
            _tc(1, "Kung Fu Hustle"),
            _tc(2, "Kung Fu Hustle cast"),
            _tc(3, "Hustle actors"),
            _tc(4, "Stephen Chow filmography"),
        ],
        expected_is_retry_loop=False,
    ),
    # 3. 完全不同 schema 字段名 (虽然搜索工具相同)
    GroundTruthSample(
        id="nr_003",
        category="not_retry",
        description="input schema 字段从 search_term 改为 topic, 参数结构变化",
        calls=[
            {"function": "web_search",
             "input": '{"search_term": "功夫 Hustle"}',
             "error": _err(), "state": "error"},
            {"function": "web_search",
             "input": '{"topic": "功夫"}',
             "error": _err(), "state": "error"},
            {"function": "web_search",
             "input": '{"topic": "周星驰"}',
             "error": _err(), "state": "error"},
            {"function": "web_search",
             "input": '{"topic": "Stephen Chow"}',
             "error": _err(), "state": "error"},
            {"function": "web_search",
             "input": '{"topic": "喜剧电影"}',
             "error": _err(), "state": "error"},
        ],
        expected_is_retry_loop=False,
    ),
    # 4. 多次相似但实际是不同的细分查询
    GroundTruthSample(
        id="nr_004",
        category="not_retry",
        description="看似相似但实际是不同的细分查询 (BUG 类型)",
        calls=[
            _tc(0, "Python TypeError"),
            _tc(1, "Python ValueError"),
            _tc(2, "Python KeyError"),
            _tc(3, "Python AttributeError"),
            _tc(4, "Python IndexError"),
        ],
        expected_is_retry_loop=False,
    ),
    # 5. 时间范围 / 不同维度
    GroundTruthSample(
        id="nr_005",
        category="not_retry",
        description="同一主题但不同时间维度 (周/月/季/年报)",
        calls=[
            _tc(0, "Tesla weekly report"),
            _tc(1, "Tesla monthly report"),
            _tc(2, "Tesla quarterly report"),
            _tc(3, "Tesla annual report"),
            _tc(4, "Tesla 10-K filing"),
        ],
        expected_is_retry_loop=False,
    ),
    # 6. 同一接口不同实质内容
    GroundTruthSample(
        id="nr_006",
        category="not_retry",
        description="看似相似但目标实体完全不同 (Apple 公司 / 苹果 水果)",
        calls=[
            _tc(0, "Apple stock price"),
            _tc(1, "Apple revenue 2024"),
            _tc(2, "apple nutrition facts"),
            _tc(3, "apple recipes"),
            _tc(4, "Apple vs Samsung comparison"),
        ],
        expected_is_retry_loop=False,
    ),
]


# ---------------------------------------------------------------------------
# C. edge_case: 段长/状态边界, LLM 应保守判 False 或拒判
# ---------------------------------------------------------------------------

_EDGE_CASE_SAMPLES: list[GroundTruthSample] = [
    # 1. 段长 4 (< 5) - 不进入 LLM, 但 LLM 单独跑应判 False (因段短)
    GroundTruthSample(
        id="ec_001",
        category="edge_case",
        description="段长 4 < 5, rule 预筛不触发; LLM 单跑应保守判 False",
        calls=[_tc(i, "功夫 Hustle") for i in range(4)],
        expected_is_retry_loop=False,
    ),
    # 2. 段长 5 但含 success (rule 预筛拒; LLM 单跑应判 False 因结果混合)
    GroundTruthSample(
        id="ec_002",
        category="edge_case",
        description="5 次中含 4 success 1 error, rule 预筛不通过; LLM 单跑应判 False",
        calls=[
            _tc_other_state(0, "功夫 Hustle", "success", _ok("hit 1")),
            _tc_other_state(1, "功夫 Hustle", "success", _ok("hit 2")),
            _tc_other_state(2, "功夫 Hustle 中文", "success", _ok("hit 3")),
            _tc_other_state(3, "功夫 Hustle", "success", _ok("hit 4")),
            _tc_other_state(4, "功夫 Hustle", "error", _err()),
        ],
        expected_is_retry_loop=False,
    ),
    # 3. 段长 5 但 error 文本中不全是 429 (混合 error 类型, rule 拒)
    GroundTruthSample(
        id="ec_003",
        category="edge_case",
        description="5 次 error 但只有 3 个 429 + 2 个 timeout/500, rule 拒",
        calls=[
            _tc_other_state(0, "功夫 Hustle", "error", "Error 429: rate limited"),
            _tc_other_state(1, "功夫 Hustle", "error", "Error 500: server error"),
            _tc_other_state(2, "功夫 Hustle", "error", "Timeout after 30s"),
            _tc_other_state(3, "功夫 Hustle", "error", "Error 429: rate limited"),
            _tc_other_state(4, "功夫 Hustle 中文", "error", "Timeout after 30s"),
        ],
        expected_is_retry_loop=False,
    ),
    # 4. 段长 6 含 2 个不同 query 主体 (语义跳变)
    GroundTruthSample(
        id="ec_004",
        category="edge_case",
        description="5 次中有 1 次 query 主体完全变化 (虽然字段同名)",
        calls=[
            _tc(0, "功夫 Hustle"),
            _tc(1, "功夫 Hustle"),
            _tc(2, "功夫 Hustle"),
            _tc(3, "完全不同的主题 ABC XYZ 123"),  # 异常跳变
            _tc(4, "功夫 Hustle"),
        ],
        expected_is_retry_loop=False,
    ),
]


# ---------------------------------------------------------------------------
# D. real_world: 从 output/refine_data 真实样本抽取 (Tavily web_search)
# ---------------------------------------------------------------------------

_REAL_WORLD_SAMPLES: list[GroundTruthSample] = [
    # 1. Tavily web_search 5 次连续 429, query 同主题
    GroundTruthSample(
        id="rw_001",
        category="real_world",
        description="Tavily web_search 真实 5 次 429 (同 query 微调)",
        calls=[
            {"function": "web_search",
             "input": '{"search_term": "功夫 Hustle"}',
             "error": "web_search failed: Client error '429 Too Many Requests' for url 'https://api.tavily.com/search'",
             "state": "error", "_idx": 0},
            {"function": "web_search",
             "input": '{"search_term": "功夫 Hustle 中文"}',
             "error": "web_search failed: Client error '429 Too Many Requests' for url 'https://api.tavily.com/search'",
             "state": "error", "_idx": 1},
            {"function": "web_search",
             "input": '{"search_term": "功夫 Hustle 正版"}',
             "error": "web_search failed: Client error '429 Too Many Requests' for url 'https://api.tavily.com/search'",
             "state": "error", "_idx": 2},
            {"function": "web_search",
             "input": '{"search_term": "Kung Fu Hustle 2024"}',
             "error": "web_search failed: Client error '429 Too Many Requests' for url 'https://api.tavily.com/search'",
             "state": "error", "_idx": 3},
            {"function": "web_search",
             "input": '{"search_term": "功夫 Hustle 免费观看"}',
             "error": "web_search failed: Client error '429 Too Many Requests' for url 'https://api.tavily.com/search'",
             "state": "error", "_idx": 4},
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 4],
    ),
    # 2. Tavily web_search 6 次连续 429, 全部超时类错误
    GroundTruthSample(
        id="rw_002",
        category="real_world",
        description="Tavily web_search 真实 6 次 429 (timeout 类)",
        calls=[
            {"function": "web_search",
             "input": '{"search_term": "python web scraping tutorial"}',
             "error": "Request timeout: 30s exceeded for url 'https://api.tavily.com/search'",
             "state": "timeout", "_idx": 0},
            {"function": "web_search",
             "input": '{"search_term": "python web scraping guide"}',
             "error": "Request timeout: 30s exceeded for url 'https://api.tavily.com/search'",
             "state": "timeout", "_idx": 1},
            {"function": "web_search",
             "input": '{"search_term": "python scrape web pages"}',
             "error": "Request timeout: 30s exceeded for url 'https://api.tavily.com/search'",
             "state": "timeout", "_idx": 2},
            {"function": "web_search",
             "input": '{"search_term": "scrape python"}',
             "error": "Request timeout: 30s exceeded for url 'https://api.tavily.com/search'",
             "state": "timeout", "_idx": 3},
            {"function": "web_search",
             "input": '{"search_term": "how to scrape websites python"}',
             "error": "Request timeout: 30s exceeded for url 'https://api.tavily.com/search'",
             "state": "timeout", "_idx": 4},
            {"function": "web_search",
             "input": '{"search_term": "python scraping example"}',
             "error": "Request timeout: 30s exceeded for url 'https://api.tavily.com/search'",
             "state": "timeout", "_idx": 5},
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 5],
    ),
    # 3. E010 中观察到 Tavily 8 次 429, query 中含限定词但核心主题一致
    GroundTruthSample(
        id="rw_003",
        category="real_world",
        description="Tavily 8 次 429 (Python async, 限定词递增)",
        calls=[
            {
                "function": "web_search",
                "input": '{"search_term": "' + q + '"}',
                "error": "web_search failed: Client error '429 Too Many Requests'",
                "state": "error",
                "_idx": i,
            }
            for i, q in enumerate([
                "Python async await tutorial",
                "Python async tutorial",
                "async await Python",
                "Python coroutine tutorial",
                "asyncio tutorial",
                "Python await guide",
                "async programming Python",
                "Python async how-to",
            ])
        ],
        expected_is_retry_loop=True,
        expected_keep_indices=[0, 7],
    ),
]


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------

GROUND_TRUTH_SAMPLES: list[GroundTruthSample] = (
    _CLEAR_RETRY_SAMPLES + _NOT_RETRY_SAMPLES + _EDGE_CASE_SAMPLES + _REAL_WORLD_SAMPLES
)


def samples_by_category(category: str) -> list[GroundTruthSample]:
    """按 category 过滤样本."""
    return [s for s in GROUND_TRUTH_SAMPLES if s.category == category]
