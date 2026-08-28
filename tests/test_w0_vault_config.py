"""W0 Vault-root configuration contract tests."""
from __future__ import annotations

import pytest

from src.research.runtime import PROJECT_ROOT, load_config, vault_root_from_config


def test_default_config_uses_vault_root_at_the_existing_memory_path() -> None:
    config = load_config()

    assert config["research"]["vault_root"] == "memory"
    assert "memory_root" not in config["research"]
    assert vault_root_from_config(config) == PROJECT_ROOT / "memory"


def test_vault_root_defaults_to_the_existing_project_memory_path() -> None:
    assert vault_root_from_config({}) == PROJECT_ROOT / "memory"


def test_legacy_memory_root_remains_readable() -> None:
    assert vault_root_from_config(
        {"research": {"memory_root": "legacy-memory"}}
    ) == PROJECT_ROOT / "legacy-memory"


def test_vault_root_takes_precedence_over_legacy_memory_root() -> None:
    assert vault_root_from_config(
        {
            "research": {
                "vault_root": "paperpilot-vault",
                "memory_root": "legacy-memory",
            }
        }
    ) == PROJECT_ROOT / "paperpilot-vault"


def test_absolute_path_like_vault_root_is_preserved() -> None:
    absolute_root = PROJECT_ROOT / "custom-vault"

    assert vault_root_from_config(
        {"research": {"vault_root": absolute_root}}
    ) == absolute_root


@pytest.mark.parametrize("research", [None, [], "memory"])
def test_research_configuration_must_be_a_mapping(research: object) -> None:
    with pytest.raises(ValueError, match="research configuration must be a mapping"):
        vault_root_from_config({"research": research})


@pytest.mark.parametrize("value", [None, "", "   ", 123, b"memory"])
@pytest.mark.parametrize("key", ["vault_root", "memory_root"])
def test_configured_root_must_be_a_non_empty_path_like_string(
    key: str,
    value: object,
) -> None:
    with pytest.raises(
        ValueError,
        match=rf"research\.{key} must be a non-empty path-like string",
    ):
        vault_root_from_config({"research": {key: value}})


def test_invalid_vault_root_is_not_masked_by_valid_legacy_root() -> None:
    with pytest.raises(
        ValueError,
        match=r"research\.vault_root must be a non-empty path-like string",
    ):
        vault_root_from_config(
            {"research": {"vault_root": "", "memory_root": "legacy-memory"}}
        )
