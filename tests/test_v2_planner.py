"""Phase 1 tests for the no-tool V2 Research Planner."""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.research.models import ResearchBrief
from src.research.research_planner import normalize_sub_queries, plan_research


def _brief() -> ResearchBrief:
    return ResearchBrief(
        question="Compare four 2024 models",
        objective="Compare model quality and technical routes",
        scope=("2024 public versions",),
        directions=(
            "Compare Chinese reasoning; compare code generation",
            "Compare long-context behavior",
        ),
        constraints=("Use public sources",),
        expected_output="A cited comparison report",
        revision=1,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["q1", "q2"], ("q1", "q2")),
        ({"queries": ["q1", {"query": "q2"}]}, ("q1", "q2")),
        ({"query": "q1"}, ("q1",)),
        ("q1", ("q1",)),
        ('{"queries": ["q1", "q2"]}', ("q1", "q2")),
        ([], ("original question",)),
        ({"unexpected": "value"}, ("original question",)),
        (None, ("original question",)),
    ],
)
def test_normalize_sub_queries_handles_defensive_input_shapes(
    raw: Any,
    expected: tuple[str, ...],
) -> None:
    assert normalize_sub_queries(raw, "original question") == expected


class RecordingPolicy:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def __call__(self, messages, *, tools=None):
        self.calls.append((messages, tools))
        value = self.responses.pop(0)
        return {
            "content": value if isinstance(value, str) else json.dumps(value),
            "tool_calls": [],
        }


@pytest.mark.asyncio
async def test_planner_uses_model_grouped_questions_as_the_dynamic_plan() -> None:
    policy = RecordingPolicy(
        [
            {
                "core_questions": [
                    "Compare Chinese reasoning and code generation benchmarks.",
                    "Compare long-context behavior and effective context length.",
                    "Compare architecture and training-data routes.",
                    "Summarize source-comparable model strengths and limitations.",
                ],
                "report_outline": ["Executive summary", "Comparison"],
                "source_guidance": ["Prefer official reports"],
                "work_hints": ["Benchmark families can run independently"],
            }
        ]
    )

    plan = await plan_research(_brief(), policy)

    assert [item.description for item in plan.core_questions] == [
        "Compare Chinese reasoning and code generation benchmarks.",
        "Compare long-context behavior and effective context length.",
        "Compare architecture and training-data routes.",
        "Summarize source-comparable model strengths and limitations.",
    ]
    assert all(item.required for item in plan.core_questions)
    assert all(item.origin == "dynamic_plan" for item in plan.core_questions)
    assert policy.calls[0][1] == []
    assert plan.fallback_reason is None


@pytest.mark.asyncio
async def test_same_brief_and_policy_output_produce_stable_plan_ids() -> None:
    response = {
        "core_questions": ["Verify source comparability"],
        "report_outline": ["Summary", "Sources"],
        "source_guidance": ["Primary sources"],
        "work_hints": [],
    }
    first = await plan_research(_brief(), RecordingPolicy([response]))
    second = await plan_research(_brief(), RecordingPolicy([response]))

    assert first == second


@pytest.mark.asyncio
async def test_dynamic_plan_caps_model_grouping_at_five_main_questions() -> None:
    response = {
        "core_questions": [f"Grouped direction {index}" for index in range(1, 8)],
        "report_outline": ["Comparison", "Conclusion"],
        "source_guidance": [],
        "work_hints": [],
    }

    plan = await plan_research(_brief(), RecordingPolicy([response]))

    assert [item.description for item in plan.core_questions] == [
        f"Grouped direction {index}" for index in range(1, 6)
    ]
    assert all(item.required for item in plan.core_questions)


@pytest.mark.asyncio
async def test_malformed_json_gets_one_no_tool_structure_repair() -> None:
    policy = RecordingPolicy(
        [
            "not-json",
            {
                "core_questions": {"queries": ["Verify benchmark versions"]},
                "report_outline": ["Comparison"],
                "source_guidance": [],
                "work_hints": [],
            },
        ]
    )

    plan = await plan_research(_brief(), policy)

    assert len(policy.calls) == 2
    assert all(tools == [] for _, tools in policy.calls)
    assert any(
        item.description == "Verify benchmark versions"
        for item in plan.core_questions
    )
    assert plan.fallback_reason is None


@pytest.mark.asyncio
async def test_failed_repair_uses_deterministic_brief_fallback() -> None:
    policy = RecordingPolicy(["not-json", "still-not-json"])

    plan = await plan_research(_brief(), policy)

    assert len(policy.calls) == 2
    assert plan.fallback_reason
    assert plan.report_outline == ("Research findings", "Limitations", "Sources")
    assert {item.description for item in plan.core_questions} == {
        "Compare Chinese reasoning",
        "compare code generation",
        "Compare long-context behavior",
    }
    assert all(item.required for item in plan.core_questions)


@pytest.mark.asyncio
async def test_empty_model_question_array_still_returns_conservative_plan() -> None:
    policy = RecordingPolicy(
        [
            {
                "core_questions": [],
                "report_outline": [],
                "source_guidance": [],
                "work_hints": [],
            }
        ]
    )

    plan = await plan_research(_brief(), policy)

    assert len(plan.core_questions) == 1
    assert plan.core_questions[0].description == _brief().question
    assert plan.report_outline == ("Research findings", "Limitations", "Sources")
    assert plan.fallback_reason is None


@pytest.mark.asyncio
async def test_planner_keeps_experimental_requirements_out_of_production_prompt() -> None:
    policy = RecordingPolicy([
        {
            "core_questions": [],
            "report_outline": ["Comparison"],
            "source_guidance": ["Use original benchmark reports"],
            "work_hints": [],
            "evidence_requirements": [{
                "question": "Compare Chinese reasoning",
                "description": "Report the same-version benchmark score and test conditions.",
                "evidence_kind": "benchmark_result",
                "minimum_verified_claims": 2,
                "minimum_independent_sources": 2,
                "primary_source_required": True,
                "required": True,
            }],
        }
    ])

    plan = await plan_research(_brief(), policy)

    assert all(
        item.minimum_verified_claims == 1
        and item.minimum_independent_sources == 1
        and item.primary_source_required is False
        for item in plan.evidence_requirements
    )
    assert {
        item.question_id for item in plan.evidence_requirements
    } == {
        item.question_id for item in plan.core_questions
    }
