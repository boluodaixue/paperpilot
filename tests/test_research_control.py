"""Explicit homogeneous control decisions preserve the three Fork strategies."""

from __future__ import annotations

import json

import pytest

from src.research.models import ForkReason, ResearchRequirement
from src.research.research_control import (
    ResearchControlAction,
    decision_as_fork_tool_call,
    parse_research_control_decision,
    research_control_prompt,
)


@pytest.mark.parametrize(
    "reason, estimated_tool_calls",
    [
        (ForkReason.PARALLEL.value, 1),
        (ForkReason.CONTEXT_ISOLATION.value, 1),
        (ForkReason.DEEP_TOOL_CHAIN.value, 3),
    ],
)
def test_control_contract_preserves_every_existing_fork_reason(
    reason: str,
    estimated_tool_calls: int,
) -> None:
    decision = parse_research_control_decision(
        json.dumps({
            "action": "fork",
            "rationale": "The scoped task satisfies an approved Fork strategy.",
            "target_requirement_ids": ["R1"],
            "fork_candidates": [{
                "objective": "Research R1",
                "expected_output": "Evidence for R1",
                "requirement_ids": ["R1"],
                "context": {},
                "reasons": [reason],
                "estimated_tool_calls": estimated_tool_calls,
                "independent": True,
            }],
        }),
        valid_requirement_ids=("R1",),
    )

    assert decision.action is ResearchControlAction.FORK
    assert decision.fork_candidates[0].reasons[0].value == reason
    call = decision_as_fork_tool_call(decision, call_id="control-fork")
    assert call["function"]["name"] == "fork_research"


def test_local_research_requires_an_explicit_reason_without_candidates() -> None:
    decision = parse_research_control_decision(
        json.dumps({
            "action": "local_research",
            "rationale": "Only one active requirement remains.",
            "target_requirement_ids": ["R1"],
            "fork_candidates": [],
        }),
        valid_requirement_ids=("R1", "R2"),
    )

    assert decision.action is ResearchControlAction.LOCAL_RESEARCH
    assert decision.rationale == "Only one active requirement remains."


def test_fork_targets_must_match_candidate_assignments() -> None:
    with pytest.raises(ValueError, match="exactly match"):
        parse_research_control_decision(
            json.dumps({
                "action": "fork",
                "rationale": "Parallel work",
                "target_requirement_ids": ["R1", "R2"],
                "fork_candidates": [{
                    "objective": "Research R1",
                    "expected_output": "Evidence",
                    "requirement_ids": ["R1"],
                    "reasons": ["parallel"],
                }],
            }),
            valid_requirement_ids=("R1", "R2"),
        )


def test_control_prompt_names_all_three_existing_strategies() -> None:
    messages = research_control_prompt(
        objective="Compare instruments",
        requirements=(ResearchRequirement("R1", "Use of proceeds"),),
        coverage=(),
        child_summaries=(),
        board_view=None,
        depth=0,
        max_fork_depth=2,
        max_children=4,
        local_rounds_since_fork=0,
    )
    prompt = messages[0]["content"]

    assert ForkReason.PARALLEL.value in prompt
    assert ForkReason.CONTEXT_ISOLATION.value in prompt
    assert ForkReason.DEEP_TOOL_CHAIN.value in prompt
