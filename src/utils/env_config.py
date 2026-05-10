"""
环境变量配置加载器

统一封装 .env / .env.local 的加载逻辑，供所有模块使用。
设计原则：
  1. 敏感信息（API Key、Base URL）只从 .env 读取，不硬编码在源码中。
  2. 构造函数参数仅作为 .env 的覆盖，方便单元测试和特殊场景。
  3. 幂等加载：多次调用不会重复读取文件。
"""
from __future__ import annotations

import os

from dotenv import load_dotenv


__all__ = ["ensure_env_loaded", "get_env", "get_env_int", "get_env_float", "get_env_bool"]


_ENV_LOADED = False


def ensure_env_loaded() -> None:
    """确保 .env 文件已加载（幂等）。

    加载顺序（后加载的优先级更高）：
      1. .env（项目级默认配置）
      2. .env.local（用户本地自定义，被 .gitignore 忽略）
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    # 1. 加载项目级 .env
    env_path = os.path.join(os.getcwd(), ".env")
    if os.path.exists(env_path):
        load_dotenv(dotenv_path=env_path)

    # 2. 加载用户级 .env.local（优先级更高）
    local_env = os.path.join(os.getcwd(), ".env.local")
    if os.path.exists(local_env):
        load_dotenv(dotenv_path=local_env, override=True)

    _ENV_LOADED = True


def get_env(key: str, default: str | None = None) -> str | None:
    """读取环境变量，支持空字符串转 None。

    首次调用会自动触发 ensure_env_loaded()。
    """
    ensure_env_loaded()
    val = os.getenv(key, default)
    if val == "" or val is None:
        return None
    return val


def get_env_int(key: str, default: int) -> int:
    """读取环境变量并转为 int。"""
    val = get_env(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        raise ValueError(f"环境变量 {key} 的值 '{val}' 无法转为整数")


def get_env_float(key: str, default: float) -> float:
    """读取环境变量并转为 float。"""
    val = get_env(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        raise ValueError(f"环境变量 {key} 的值 '{val}' 无法转为浮点数")


def get_env_bool(key: str, default: bool = False) -> bool:
    """读取环境变量并转为 bool。

    以下值视为 True：true, True, 1, yes, YES
    以下值视为 False：false, False, 0, no, NO
    """
    val = get_env(key)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes")
