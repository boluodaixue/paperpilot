"""Project-owned provider configuration must not leak from the host environment."""
from __future__ import annotations

from pathlib import Path

import src.utils.env_config as env_config


def _reset() -> None:
    env_config._ENV_LOADED = False


def _clear_provider_config() -> None:
    for key in tuple(env_config.os.environ):
        if env_config._PROJECT_ONLY_ENV.fullmatch(key):
            env_config.os.environ.pop(key, None)


def test_provider_configuration_does_not_fall_back_to_system_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "system-key-must-not-be-used")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://system.invalid/v1")
    _reset()
    try:
        assert env_config.get_env("DEEPSEEK_API_KEY") is None
        assert env_config.get_env("DEEPSEEK_BASE_URL") is None
    finally:
        _reset()
        _clear_provider_config()


def test_worktree_loads_shared_project_env_then_local_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    shared = tmp_path / "shared"
    worktree = tmp_path / "worktree"
    git_dir = shared / ".git" / "worktrees" / "worktree"
    git_dir.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text(
        f"gitdir: {git_dir.as_posix()}\n",
        encoding="utf-8",
    )
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    (shared / ".env").write_text(
        "DEEPSEEK_API_KEY=shared-key\n"
        "DEEPSEEK_BASE_URL=https://ark.example/v3\n"
        "DEEPSEEK_MODEL=deepseek-project\n",
        encoding="utf-8",
    )
    (worktree / ".env.local").write_text(
        "DEEPSEEK_MODEL=deepseek-worktree\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(worktree)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "system-key-must-not-be-used")
    _reset()
    try:
        assert env_config.get_env("DEEPSEEK_API_KEY") == "shared-key"
        assert env_config.get_env("DEEPSEEK_BASE_URL") == "https://ark.example/v3"
        assert env_config.get_env("DEEPSEEK_MODEL") == "deepseek-worktree"
    finally:
        _reset()
        _clear_provider_config()
