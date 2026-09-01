"""Strict opt-in configuration for homogeneous Fork control and leases."""

from __future__ import annotations

import pytest

from src.research.research_control import HomogeneousForkConfig
from src.research.runtime import homogeneous_fork_config_from_config


def test_homogeneous_fork_control_defaults_off() -> None:
    assert homogeneous_fork_config_from_config({"research": {}}) == HomogeneousForkConfig()


def test_homogeneous_fork_control_strictly_parses_leases() -> None:
    settings = homogeneous_fork_config_from_config({
        "research": {
            "homogeneous_fork": {
                "enabled": True,
                "explicit_control_decision": True,
                "budget_leases_enabled": True,
                "reconsider_after_local_rounds": 2,
                "parent_merge_reserve_tokens": 50000,
                "initial_child_lease_tokens": 60000,
                "child_topup_tokens": 25000,
                "max_child_lease_tokens": 125000,
            }
        }
    })

    assert settings.enabled is True
    assert settings.budget_leases_enabled is True
    assert settings.initial_child_lease_tokens == 60000
    assert settings.child_topup_tokens == 25000


def test_enabled_control_requires_explicit_decision() -> None:
    with pytest.raises(ValueError, match="explicit_control_decision"):
        homogeneous_fork_config_from_config({
            "research": {"homogeneous_fork": {"enabled": True}}
        })


def test_explicit_control_can_run_without_budget_leases() -> None:
    settings = homogeneous_fork_config_from_config({
        "research": {
            "homogeneous_fork": {
                "enabled": True,
                "explicit_control_decision": True,
                "budget_leases_enabled": False,
            }
        }
    })

    assert settings.enabled is True
    assert settings.explicit_control_decision is True
    assert settings.budget_leases_enabled is False


def test_budget_leases_require_enabled_control() -> None:
    with pytest.raises(ValueError, match="budget_leases_enabled"):
        homogeneous_fork_config_from_config({
            "research": {
                "homogeneous_fork": {"budget_leases_enabled": True}
            }
        })


def test_unknown_homogeneous_fork_setting_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown research.homogeneous_fork"):
        homogeneous_fork_config_from_config({
            "research": {"homogeneous_fork": {"fork_everything": True}}
        })
