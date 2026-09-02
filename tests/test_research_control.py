"""Explicit homogeneous control decisions preserve the three Fork strategies."""

from __future__ import annotations

import json

import pytest

from src.research.models import ForkReason, ResearchRequirement
from src.research.research_control import (
    ResearchControlAction,
    decision_as_fork_tool_call,
    initial_root_requirement_decision,
    parse_research_control_decision,
    research_control_prompt,
    should_scout_before_fork,
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


def test_control_prompt_does_not_treat_one_requirement_as_indivisible() -> None:
    messages = research_control_prompt(
        objective="Compare investor protections",
        requirements=(ResearchRequirement("R1", "Investor protection"),),
        coverage=(),
        child_summaries=(),
        board_view=None,
        depth=1,
        max_fork_depth=2,
        max_children=5,
        local_rounds_since_fork=1,
        remaining_total_agent_slots=15,
        delegable_token_budget=90000,
    )

    prompt = messages[0]["content"]
    assert "coverage unit, not the smallest execution unit" in prompt
    assert "'single requirement', 'focused requirement'" in prompt
    assert "not a sufficient reason to avoid Fork" in prompt
    assert "observed research basis" in prompt
    assert "must not combine independent external-evidence Requirements" in prompt
    payload = json.loads(messages[-1]["content"])
    assert payload["remaining_total_agent_slots"] == 15
    assert payload["delegable_token_budget"] == 90000


def test_initial_root_wave_assigns_each_external_requirement_to_one_child() -> None:
    requirements = tuple(
        ResearchRequirement(f"R{index}", f"Research requirement {index}")
        for index in range(1, 5)
    ) + (
        ResearchRequirement(
            "S1",
            "Synthesize the report",
            requires_external_evidence=False,
        ),
    )

    decision = initial_root_requirement_decision(
        requirements,
        max_children=5,
        remaining_total_agent_slots=9,
    )

    assert decision is not None
    assert decision.action is ResearchControlAction.FORK
    assert decision.target_requirement_ids == ("R1", "R2", "R3", "R4")
    assert [item.requirement_ids for item in decision.fork_candidates] == [
        ("R1",),
        ("R2",),
        ("R3",),
        ("R4",),
    ]
    assert all("S1" not in item.requirement_ids for item in decision.fork_candidates)


def test_initial_root_wave_waits_when_all_requirements_do_not_fit() -> None:
    requirements = tuple(
        ResearchRequirement(f"R{index}", f"Research requirement {index}")
        for index in range(1, 5)
    )

    assert initial_root_requirement_decision(
        requirements,
        max_children=3,
        remaining_total_agent_slots=9,
    ) is None


def test_child_parallel_fork_requires_research_basis_but_deep_chain_does_not() -> None:
    parallel = parse_research_control_decision(
        json.dumps({
            "action": "fork",
            "rationale": "parallel targets",
            "target_requirement_ids": ["R1"],
            "fork_candidates": [
                {
                    "objective": f"scope {index}",
                    "expected_output": "evidence",
                    "requirement_ids": ["R1"],
                    "reasons": ["parallel"],
                }
                for index in range(2)
            ],
        }),
        valid_requirement_ids=("R1",),
    )
    deep_chain = parse_research_control_decision(
        json.dumps({
            "action": "fork",
            "rationale": "three-step retrieval chain",
            "target_requirement_ids": ["R1"],
            "fork_candidates": [{
                "objective": "retrieve and inspect the primary document",
                "expected_output": "evidence",
                "requirement_ids": ["R1"],
                "reasons": ["deep_tool_chain"],
                "estimated_tool_calls": 3,
            }],
        }),
        valid_requirement_ids=("R1",),
    )

    assert should_scout_before_fork(parallel, depth=1, has_research_basis=False)
    assert not should_scout_before_fork(parallel, depth=1, has_research_basis=True)
    assert not should_scout_before_fork(deep_chain, depth=1, has_research_basis=False)
