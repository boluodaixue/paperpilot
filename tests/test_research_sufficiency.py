"""Acceptance tests for research sufficiency contracts and deterministic routing."""
from __future__ import annotations

import json

import pytest

from src.research.models import (
    EvidenceItem,
    OutputStatus,
    ResearchDecision,
    ResearchRequirement,
    ResearchTask,
    StrategyAttempt,
    TerminationReason,
)
from src.research.research_sufficiency import (
    AssessmentValidationError,
    build_research_requirements,
    hard_termination_reason,
    parse_research_assessment,
)


REQUIREMENTS = (
    ResearchRequirement("R1", "Answer the first necessary question."),
    ResearchRequirement("R2", "Answer the second necessary question."),
)
EVIDENCE = (
    EvidenceItem(
        evidence_id="E1",
        finding="One primary source supports both scoped requirements.",
        source_type="paper",
        title="Primary source",
        source_ref="https://example.com/primary",
    ),
)


def test_confirmed_brief_requirements_are_stable_ordered_and_deduplicated() -> None:
    task = ResearchTask(
        "requirements",
        "Fallback objective",
        context={
            "directions": ["First", "Second", "First"],
            "research_gaps": ["Second", "Third"],
            "scope": ["Boundary only"],
        },
    )
    first = build_research_requirements(task)
    second = build_research_requirements(task)
    assert first == second
    assert [(item.requirement_id, item.description) for item in first] == [
        ("R1", "First"),
        ("R2", "Second"),
        ("R3", "Third"),
    ]


def _payload(
    *,
    decision: str,
    statuses: tuple[str, str] = ("supported", "supported"),
    termination_reason: str | None = None,
    gaps: list[dict] | None = None,
    actions: list[dict] | None = None,
    replan_reason: str | None = None,
    exhaustion_reason: str | None = None,
) -> str:
    return json.dumps(
        {
            "decision": decision,
            "coverage": [
                {
                    "requirement_id": requirement.requirement_id,
                    "status": status,
                    "evidence_ids": ["E1"] if status != "unsupported" else [],
                    "rationale": "Evidence was assessed against this requirement.",
                    "remaining_gap": None if status == "supported" else "A gap remains.",
                }
                for requirement, status in zip(REQUIREMENTS, statuses)
            ],
            "critical_gaps": gaps or [],
            "next_actions": actions or [],
            "termination_reason": termination_reason,
            "replan_reason": replan_reason,
            "exhaustion_reason": exhaustion_reason,
        }
    )


def _gap(requirement_id: str = "R2", impact: str = "high") -> dict:
    return {
        "requirement_id": requirement_id,
        "reason": "The missing evidence could change the final answer.",
        "impact": impact,
    }


def _action(requirement_id: str = "R2", strategy: str = "primary_document") -> dict:
    return {
        "requirement_id": requirement_id,
        "strategy": strategy,
        "query": "Find the original primary document",
        "expected_value": "high",
        "expected_improvement": "Resolve the material uncertainty.",
    }


def test_one_source_can_complete_all_requirements_when_semantically_supported() -> None:
    assessment = parse_research_assessment(
        _payload(decision="stop_research", termination_reason="coverage_complete"),
        requirements=REQUIREMENTS,
        evidence=EVIDENCE,
        attempts=(),
    )
    assert assessment.decision == ResearchDecision.STOP_RESEARCH
    assert assessment.termination_reason == TerminationReason.COVERAGE_COMPLETE
    assert {item.evidence_ids for item in assessment.coverage} == {("E1",)}


def test_coverage_complete_rejects_weak_or_uncovered_requirements() -> None:
    with pytest.raises(AssessmentValidationError, match="coverage_complete"):
        parse_research_assessment(
            _payload(
                decision="stop_research",
                statuses=("supported", "weak"),
                termination_reason="coverage_complete",
            ),
            requirements=REQUIREMENTS,
            evidence=EVIDENCE,
            attempts=(),
        )


def test_continue_requires_a_requirement_scoped_high_value_action() -> None:
    assessment = parse_research_assessment(
        _payload(
            decision="continue",
            statuses=("supported", "unsupported"),
            gaps=[_gap()],
            actions=[_action()],
        ),
        requirements=REQUIREMENTS,
        evidence=EVIDENCE,
        attempts=(),
    )
    assert assessment.decision == ResearchDecision.CONTINUE
    with pytest.raises(AssessmentValidationError, match="executable action"):
        parse_research_assessment(
            _payload(
                decision="continue",
                statuses=("supported", "unsupported"),
                gaps=[_gap()],
            ),
            requirements=REQUIREMENTS,
            evidence=EVIDENCE,
            attempts=(),
        )


def test_replan_requires_a_materially_new_strategy() -> None:
    attempted = (
        StrategyAttempt("R2", "primary_document", "old query", "no_progress"),
    )
    assessment = parse_research_assessment(
        _payload(
            decision="replan",
            statuses=("supported", "unsupported"),
            gaps=[_gap()],
            actions=[_action(strategy="official_database")],
            replan_reason="The primary-document query path has low marginal value.",
        ),
        requirements=REQUIREMENTS,
        evidence=EVIDENCE,
        attempts=attempted,
    )
    assert assessment.decision == ResearchDecision.REPLAN
    with pytest.raises(AssessmentValidationError, match="materially new strategy"):
        parse_research_assessment(
            _payload(
                decision="replan",
                statuses=("supported", "unsupported"),
                gaps=[_gap()],
                actions=[_action(strategy="primary_document")],
                replan_reason="The old query has low value.",
            ),
            requirements=REQUIREMENTS,
            evidence=EVIDENCE,
            attempts=attempted,
        )


def test_saturated_allows_only_low_impact_remaining_details() -> None:
    assessment = parse_research_assessment(
        _payload(
            decision="stop_research",
            statuses=("supported", "weak"),
            termination_reason="saturated",
            gaps=[_gap(impact="low")],
        ),
        requirements=REQUIREMENTS,
        evidence=EVIDENCE,
        attempts=(),
    )
    assert assessment.termination_reason == TerminationReason.SATURATED
    with pytest.raises(AssessmentValidationError, match="low-impact"):
        parse_research_assessment(
            _payload(
                decision="stop_research",
                statuses=("supported", "weak"),
                termination_reason="saturated",
                gaps=[_gap(impact="high")],
            ),
            requirements=REQUIREMENTS,
            evidence=EVIDENCE,
            attempts=(),
        )


def test_evidence_exhausted_requires_multiple_distinct_attempted_strategies() -> None:
    attempts = (
        StrategyAttempt("R2", "primary_document", "query one", "no_progress"),
        StrategyAttempt("R2", "official_database", "query two", "no_progress"),
    )
    assessment = parse_research_assessment(
        _payload(
            decision="stop_research",
            statuses=("supported", "unsupported"),
            termination_reason="evidence_exhausted",
            gaps=[_gap()],
            exhaustion_reason="Two independent evidence paths produced no usable source.",
        ),
        requirements=REQUIREMENTS,
        evidence=EVIDENCE,
        attempts=attempts,
    )
    assert assessment.termination_reason == TerminationReason.EVIDENCE_EXHAUSTED
    with pytest.raises(AssessmentValidationError, match="multiple distinct"):
        parse_research_assessment(
            _payload(
                decision="stop_research",
                statuses=("supported", "unsupported"),
                termination_reason="evidence_exhausted",
                gaps=[_gap()],
                exhaustion_reason="One path failed.",
            ),
            requirements=REQUIREMENTS,
            evidence=EVIDENCE,
            attempts=attempts[:1],
        )
    with pytest.raises(AssessmentValidationError, match="no-progress"):
        parse_research_assessment(
            _payload(
                decision="stop_research",
                statuses=("supported", "unsupported"),
                termination_reason="evidence_exhausted",
                gaps=[_gap()],
                exhaustion_reason="The model claimed exhaustion despite useful results.",
            ),
            requirements=REQUIREMENTS,
            evidence=EVIDENCE,
            attempts=(
                StrategyAttempt("R2", "primary_document", "query one", "evidence_found"),
                StrategyAttempt("R2", "official_database", "query two", "evidence_found"),
            ),
        )


@pytest.mark.parametrize("reason", ["budget_forced", "tool_failure", "user_cancelled"])
def test_semantic_assessment_cannot_fabricate_runtime_only_stop_reasons(reason: str) -> None:
    with pytest.raises(AssessmentValidationError, match="runtime state"):
        parse_research_assessment(
            _payload(decision="stop_research", termination_reason=reason),
            requirements=REQUIREMENTS,
            evidence=EVIDENCE,
            attempts=(),
        )


def test_unknown_evidence_id_is_rejected_before_routing() -> None:
    payload = json.loads(
        _payload(decision="stop_research", termination_reason="coverage_complete")
    )
    payload["coverage"][0]["evidence_ids"] = ["MISSING"]
    with pytest.raises(AssessmentValidationError, match="unknown Evidence IDs"):
        parse_research_assessment(
            json.dumps(payload),
            requirements=REQUIREMENTS,
            evidence=EVIDENCE,
            attempts=(),
        )


@pytest.mark.parametrize(
    ("detail", "reason"),
    [
        ("max_iterations_exhausted", TerminationReason.BUDGET_FORCED),
        ("token_budget_exhausted", TerminationReason.BUDGET_FORCED),
        ("policy_error: unavailable", TerminationReason.TOOL_FAILURE),
        ("user_cancelled", TerminationReason.USER_CANCELLED),
    ],
)
def test_hard_bound_and_failure_details_map_to_stable_reasons(detail, reason) -> None:
    assert hard_termination_reason(detail) == reason


def test_assessment_output_status_can_record_one_successful_repair() -> None:
    assessment = parse_research_assessment(
        _payload(decision="stop_research", termination_reason="coverage_complete"),
        requirements=REQUIREMENTS,
        evidence=EVIDENCE,
        attempts=(),
        output_status=OutputStatus.REPAIRED,
    )
    assert assessment.output_status == OutputStatus.REPAIRED
