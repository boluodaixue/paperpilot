"""Phase 0 safety-switch tests for the Research Agent V2 rollout."""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.research import MarkdownMemoryStore
from src.research.runtime import (
    build_research_runtime,
    research_architecture_settings_from_config,
)
from src.research.v2_contracts import ResearchArchitecture, SupervisorV2Config


def test_v2_is_disabled_and_legacy_is_selected_by_default(tmp_path) -> None:
    settings = research_architecture_settings_from_config({})

    assert settings.architecture is ResearchArchitecture.LEGACY
    assert settings.supervisor_v2 == SupervisorV2Config()

    runtime = build_research_runtime(
        {},
        policy=lambda *args, **kwargs: None,
        tools=[],
        memory_store=MarkdownMemoryStore(tmp_path),
        checkpointer=InMemorySaver(),
    )
    assert runtime.research_architecture is ResearchArchitecture.LEGACY
    assert runtime.supervisor_v2_config.enabled is False


def test_valid_supervisor_v2_settings_are_strictly_parsed() -> None:
    settings = research_architecture_settings_from_config(
        {
            "research": {
                "architecture": "supervisor_v2",
                "supervisor_v2": {
                    "enabled": True,
                    "max_initial_workers": 3,
                    "max_research_waves": 1,
                    "red_review_enabled": False,
                    "max_red_review_rounds": 0,
                    "max_citation_repair_rounds": 0,
                },
            }
        }
    )

    assert settings.architecture is ResearchArchitecture.SUPERVISOR_V2
    assert settings.supervisor_v2.max_initial_workers == 3
    assert settings.supervisor_v2.max_research_waves == 1
    assert settings.supervisor_v2.red_review_enabled is False


@pytest.mark.parametrize("value", [None, True, 1, [], {}, "v2", "recursive"])
def test_unknown_or_non_string_architecture_is_rejected(value: Any) -> None:
    with pytest.raises(ValueError, match="research.architecture"):
        research_architecture_settings_from_config(
            {"research": {"architecture": value}}
        )


@pytest.mark.parametrize("value", [None, True, 1, [], "enabled"])
def test_non_mapping_supervisor_v2_config_is_rejected(value: Any) -> None:
    with pytest.raises(ValueError, match="research.supervisor_v2 must be a mapping"):
        research_architecture_settings_from_config(
            {"research": {"supervisor_v2": value}}
        )


def test_unknown_supervisor_v2_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown research.supervisor_v2 settings"):
        research_architecture_settings_from_config(
            {"research": {"supervisor_v2": {"worker_limit": 4}}}
        )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("enabled", "true", "must be a boolean"),
        ("red_review_enabled", 1, "must be a boolean"),
        ("max_initial_workers", True, "must be an integer"),
        ("max_initial_workers", 0, "must be at least 1"),
        ("max_research_waves", 4, "must be between 1 and 3"),
        ("max_red_review_rounds", -1, "must be between 0 and 1"),
        ("max_citation_repair_rounds", 2, "must be between 0 and 1"),
    ],
)
def test_invalid_supervisor_v2_values_are_rejected(
    key: str,
    value: Any,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        research_architecture_settings_from_config(
            {"research": {"supervisor_v2": {key: value}}}
        )


def test_requesting_disabled_supervisor_v2_fails_before_runtime_construction(
    tmp_path,
) -> None:
    with pytest.raises(ValueError, match="requires research.supervisor_v2.enabled=true"):
        build_research_runtime(
            {"research": {"architecture": "supervisor_v2"}},
            policy=lambda *args, **kwargs: None,
            tools=[],
            memory_store=MarkdownMemoryStore(tmp_path),
            checkpointer=InMemorySaver(),
        )


def test_enabled_v2_routes_to_the_v2_workflow_not_legacy(tmp_path) -> None:
    runtime = build_research_runtime(
        {
            "research": {
                "architecture": "supervisor_v2",
                "supervisor_v2": {"enabled": True},
            }
        },
        policy=lambda *args, **kwargs: None,
        tools=[],
        memory_store=MarkdownMemoryStore(tmp_path),
        checkpointer=InMemorySaver(),
    )

    nodes = set(runtime.graph.get_graph().nodes)
    assert {"planning", "blue_research", "red_review", "drafting", "citation_audit"} <= nodes
    assert "research_agent" not in nodes
