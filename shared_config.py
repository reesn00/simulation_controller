"""shared_config: 根配置 ``config/config.yaml`` 的定位与读取工具。

四个模块 (simulate_serve / orchestration / gdr / etl.qwenformat) 的全部配置
统一收纳在仓库根的 ``config/config.yaml`` (全项目唯一配置文件, 模块级配置
文件已删除, 无兜底):
  - 定位: ``SIMCTL_CONFIG`` 环境变量 > 仓库根 ``config/config.yaml``
  - ``${VAR}`` 环境变量占位符展开 (未设置的环境变量展开为空串)
  - 各模块按顶层 section 取数; ``llm:`` 共享段提供端点/密钥/模型缺省

注意: gdr 是独立打包的 uv workspace 成员 (有自己的 pyproject/依赖), 不能
import 本模块; 它在 ``gdr/config/settings.py`` 内自带一份等价的精简读取逻辑。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent
ROOT_CONFIG_ENV = "SIMCTL_CONFIG"
ROOT_CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"

# 根配置的已知顶层 section (用于识别"根格式"文件)
ROOT_SECTION_KEYS = ("llm", "simulate_serve", "orchestration", "gdr", "qf")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env_placeholders(value: Any) -> Any:
    """递归展开字符串中的 ``${VAR}`` 环境变量占位符; 未设置的变量展开为空串."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: expand_env_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_placeholders(v) for v in value]
    return value


def load_yaml_file(path: str | Path) -> dict[str, Any]:
    """读取 yaml 文件为 dict (占位符已展开); 文件不存在/非 dict 返回空 dict."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return expand_env_placeholders(raw)


def find_root_config() -> Path | None:
    """定位根配置: ``SIMCTL_CONFIG`` env > 仓库根 ``config/config.yaml``.

    - env 已设置且文件存在 → 返回该文件;
    - env 已设置但文件缺失 → 返回 None (显式覆盖失败时不静默回退默认路径);
    - env 未设置 → 仓库根 ``config/config.yaml`` 存在则返回, 否则 None
      (调用方退回各自包内默认配置)。
    """
    env_path = os.environ.get(ROOT_CONFIG_ENV)
    if env_path:
        cand = Path(env_path)
        return cand if cand.is_file() else None
    if ROOT_CONFIG_PATH.is_file():
        return ROOT_CONFIG_PATH
    return None


def is_root_config(raw: dict[str, Any]) -> bool:
    """判断一个已加载的 yaml dict 是否为根配置格式 (含已知顶层 section)."""
    return isinstance(raw, dict) and any(k in raw for k in ROOT_SECTION_KEYS)


def llm_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    """取根配置的 ``llm:`` 共享段 (base_url / api_key / model)."""
    section = raw.get("llm")
    return section if isinstance(section, dict) else {}
