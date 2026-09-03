"""
环境变量配置加载器

统一封装 .env / .env.local 的加载逻辑，供所有模块使用。
设计原则：
  1. 敏感信息（API Key、Base URL）只从项目 .env 读取，不使用系统同名变量。
  2. 构造函数参数仅作为 .env 的覆盖，方便单元测试和特殊场景。
  3. 幂等加载：多次调用不会重复读取文件。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv


__all__ = ["ensure_env_loaded", "get_env", "get_env_int", "get_env_float", "get_env_bool"]


_ENV_LOADED = False
_PROJECT_ONLY_ENV = re.compile(
    r"^(?:DEFAULT_LLM_BACKEND|[A-Z0-9_]+_(?:API_KEY|BASE_URL|MODEL))$"
)


def _project_env_roots() -> tuple[Path, ...]:
    """Return shared-checkout then worktree roots, in override order."""

    worktree_root = Path.cwd().resolve(strict=False)
    roots: list[Path] = []
    dot_git = worktree_root / ".git"
    if dot_git.is_file():
        try:
            line = dot_git.read_text(encoding="utf-8").strip()
            if line.lower().startswith("gitdir:"):
                git_dir = Path(line.split(":", 1)[1].strip())
                if not git_dir.is_absolute():
                    git_dir = (worktree_root / git_dir).resolve(strict=False)
                common_file = git_dir / "commondir"
                if common_file.is_file():
                    common_dir = Path(
                        common_file.read_text(encoding="utf-8").strip()
                    )
                    if not common_dir.is_absolute():
                        common_dir = (git_dir / common_dir).resolve(strict=False)
                    shared_root = common_dir.parent
                    if shared_root != worktree_root:
                        roots.append(shared_root)
        except (OSError, ValueError):
            pass
    roots.append(worktree_root)
    return tuple(dict.fromkeys(roots))


def ensure_env_loaded() -> None:
    """确保 .env 文件已加载（幂等）。

    加载顺序（后加载的优先级更高）：
      1. Git 主检出目录的 .env / .env.local（worktree 共享配置）
      2. 当前 worktree 的 .env / .env.local（本地覆盖）
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return

    # Provider credentials and model endpoints are project configuration. Remove
    # inherited machine/user values first so a missing worktree .env cannot silently
    # select an unrelated account.
    for key in tuple(os.environ):
        if _PROJECT_ONLY_ENV.fullmatch(key):
            os.environ.pop(key, None)

    # A Git worktree does not contain ignored .env files. Load the shared checkout
    # first, then let worktree-local files override it when present.
    for root in _project_env_roots():
        for name in (".env", ".env.local"):
            env_path = root / name
            if env_path.is_file():
                load_dotenv(dotenv_path=env_path, override=True)

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
