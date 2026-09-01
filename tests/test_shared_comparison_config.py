"""Strict opt-in configuration for fair architecture comparisons."""

from __future__ import annotations

import pytest

from src.research.runtime import (
    _research_blackboard_path,
    shared_comparison_plan_from_config,
)


def _config(architecture: str = "legacy") -> dict:
    return {
        "research": {
            "architecture": architecture,
            "shared_comparison": {
                "enabled": True,
                "fixed_plan": {
                    "brief_revision": 1,
                    "core_questions": [
                        {"description": "Use of proceeds"},
                        {"description": "Disclosure and verification"},
                        {"description": "Investor protection"},
                    ],
                    "report_outline": ["Comparison", "Conclusion"],
                    "source_guidance": ["Prefer primary sources"],
                },
            },
        }
    }


def test_fixed_plan_is_identical_across_architecture_switch() -> None:
    legacy = shared_comparison_plan_from_config(_config("legacy"))
    supervisor = shared_comparison_plan_from_config(_config("supervisor_v2"))

    assert legacy is not None
    assert supervisor is not None
    assert legacy == supervisor


def test_shared_comparison_is_opt_in() -> None:
    assert shared_comparison_plan_from_config({"research": {}}) is None
    assert shared_comparison_plan_from_config({
        "research": {"shared_comparison": {"enabled": False}}
    }) is None


def test_blackboard_uses_a_separate_sqlite_file_from_checkpoints(tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint.sqlite"
    blackboard = _research_blackboard_path(checkpoint)

    assert blackboard != checkpoint
    assert blackboard.name == "checkpoint.blackboard.sqlite"


@pytest.mark.parametrize(
    "shared, pattern",
    [
        ({"enabled": True}, "fixed_plan"),
        (
            {"enabled": True, "fixed_plan": {"core_questions": []}},
            "core_questions",
        ),
        (
            {
                "enabled": True,
                "fixed_plan": {
                    "core_questions": [{"description": "Question", "extra": 1}]
                },
            },
            "unknown fixed core question",
        ),
    ],
)
def test_invalid_fixed_plan_is_rejected(shared, pattern) -> None:
    with pytest.raises(ValueError, match=pattern):
        shared_comparison_plan_from_config({"research": {"shared_comparison": shared}})
