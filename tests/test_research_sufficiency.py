"""Acceptance tests for research sufficiency contracts and deterministic routing."""
from __future__ import annotations

import json

import pytest

from src.research.models import (
    CriticalGap,
    EvidenceItem,
    NextResearchAction,
    OutputStatus,
    ResearchDecision,
    ResearchResult,
    ResearchStatus,
    RequirementCoverage,
    RequirementStatus,
    ResearchRequirement,
    ResearchTask,
    StrategyAttempt,
    TerminationReason,
)
from src.research.research_sufficiency import (
    AssessmentValidationError,
    ResearchAssessment,
    active_next_actions,
    build_assessment_projection,
    build_research_requirements,
    hard_termination_reason,
    merge_next_action_queue,
    merge_child_coverage_evidence,
    parse_research_assessment,
    reconcile_strategy_attempt_outcomes,
    unattempted_actions,
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


def test_assessment_rejects_free_form_strategy_family() -> None:
    payload = _payload(
        decision="continue",
        statuses=("supported", "unsupported"),
        gaps=[_gap()],
        actions=[_action(strategy="search many different official pages")],
    )

    with pytest.raises(AssessmentValidationError, match="strategy is not canonical"):
        parse_research_assessment(
            payload,
            requirements=REQUIREMENTS,
            evidence=EVIDENCE,
            attempts=(),
        )


def test_continue_rejects_an_action_that_was_already_executed() -> None:
    action = _action(strategy="paper_search")
    payload = _payload(
        decision="continue",
        statuses=("supported", "unsupported"),
        gaps=[_gap()],
        actions=[action],
    )
    attempts = (
        StrategyAttempt(
            "R2",
            "paper_search",
            action["query"],
            "no_progress",
        ),
    )

    with pytest.raises(AssessmentValidationError, match="must not repeat"):
        parse_research_assessment(
            payload,
            requirements=REQUIREMENTS,
            evidence=EVIDENCE,
            attempts=attempts,
        )
    assert unattempted_actions(
        (NextResearchAction("R2", "paper_search", action["query"], "high", "x"),),
        attempts,
    ) == ()


def test_long_history_projection_is_bounded_and_preserves_attempt_outcomes() -> None:
    strategies = (
        "official_database",
        "primary_document",
        "query_rewrite",
        "paper_search",
        "other",
    )
    evidence = tuple(
        EvidenceItem(
            evidence_id=f"E{index}",
            finding=(f"Finding {index} " * 100),
            source_type="paper",
            title=f"Source {index}",
            source_ref=f"https://example.com/{index}",
        )
        for index in range(200)
    )
    attempts = tuple(
        StrategyAttempt(
            "R1" if index % 2 == 0 else "R2",
            strategies[index % len(strategies)],
            f"distinct query {index}",
            "evidence_found" if index % 3 == 0 else "no_progress",
            (f"E{index}",),
        )
        for index in range(200)
    )

    projection = build_assessment_projection(
        task=ResearchTask("projection", "Bound a long research history"),
        requirements=REQUIREMENTS,
        coverage=(
            RequirementCoverage("R1", RequirementStatus.WEAK, ("E0",)),
            RequirementCoverage("R2", RequirementStatus.UNSUPPORTED),
        ),
        evidence=evidence,
        critical_gaps=(CriticalGap("R2", "Still unresolved", "high"),),
        attempts=attempts,
        child_results=(),
        candidate_final="",
        recent_tool_failures=(),
        recent_tool_outcomes=(),
    )

    assert len(json.dumps(projection, ensure_ascii=False)) <= 30500
    assert "strategy_attempts" not in projection
    assert sum(
        item["attempt_count"] for item in projection["strategy_attempt_summary"]
    ) == 200
    assert len(projection["strategy_attempt_summary"]) <= len(strategies) * 2
    assert projection["evidence_inventory"]["included_count"] < 200
    assert projection["evidence_inventory"]["omitted_candidate_count"] > 0


def test_strategy_progress_requires_validated_coverage_citation() -> None:
    attempts = (
        StrategyAttempt("R1", "paper_search", "relevant", "evidence_found", ("E1",)),
        StrategyAttempt("R1", "query_rewrite", "noise", "evidence_found", ("E2",)),
        StrategyAttempt("R2", "primary_document", "other", "no_progress", ("E3",)),
    )
    coverage = (
        RequirementCoverage("R1", RequirementStatus.WEAK, ("E1",)),
        RequirementCoverage("R2", RequirementStatus.UNSUPPORTED),
    )

    reconciled = reconcile_strategy_attempt_outcomes(attempts, coverage)

    assert [item.outcome for item in reconciled] == [
        "evidence_found",
        "no_progress",
        "no_progress",
    ]
    assert reconciled[1].evidence_ids == ("E2",)


def test_child_evidence_is_linked_without_inheriting_child_coverage_status() -> None:
    parent = (
        RequirementCoverage("R1", RequirementStatus.UNSUPPORTED),
        RequirementCoverage("R2", RequirementStatus.WEAK, ("E0",)),
    )
    child = ResearchResult(
        task_id="child-r1",
        status=ResearchStatus.PARTIAL,
        summary="Scoped R1 evidence",
        evidence=EVIDENCE,
        coverage=(
            RequirementCoverage("R1", RequirementStatus.SUPPORTED, ("E1",)),
            RequirementCoverage("R9", RequirementStatus.SUPPORTED, ("E1",)),
        ),
    )

    merged = merge_child_coverage_evidence(parent, (child,))

    assert merged[0].status == RequirementStatus.UNSUPPORTED
    assert merged[0].evidence_ids == ("E1",)
    assert merged[1] == parent[1]


def test_active_next_actions_preserves_model_priority_without_fixed_stop_gate() -> None:
    actions = (
        NextResearchAction("R2", "paper_search", "first", "high", "resolve R2"),
        NextResearchAction("R1", "query_rewrite", "second", "medium", "resolve R1"),
    )

    assert active_next_actions(actions) == actions[:1]


def test_next_action_queue_consumes_active_and_preserves_unexecuted_tail() -> None:
    previous = (
        NextResearchAction("R1", "primary_document", "done", "high", "A"),
        NextResearchAction("R2", "paper_search", "pending", "high", "B"),
        NextResearchAction("R3", "official_database", "also pending", "medium", "C"),
    )
    assessment = parse_research_assessment(
        _payload(
            decision="continue",
            statuses=("weak", "weak"),
            gaps=[_gap("R1"), _gap("R2")],
            actions=[_action("R1", "query_rewrite")],
        ),
        requirements=REQUIREMENTS,
        evidence=EVIDENCE,
        attempts=(),
    )

    queue = merge_next_action_queue(
        previous,
        assessment,
        active_consumed=True,
    )

    assert [(item.requirement_id, item.query) for item in queue] == [
        ("R2", "pending"),
        ("R1", "Find the original primary document"),
    ]


def test_replan_replaces_only_same_requirement_and_stop_clears_queue() -> None:
    previous = (
        NextResearchAction("R1", "primary_document", "old R1", "high", "A"),
        NextResearchAction("R2", "paper_search", "pending R2", "high", "B"),
    )
    replanned = parse_research_assessment(
        _payload(
            decision="replan",
            statuses=("weak", "weak"),
            gaps=[_gap("R1"), _gap("R2")],
            actions=[_action("R1", "query_rewrite")],
            replan_reason="The old R1 path is low value.",
        ),
        requirements=REQUIREMENTS,
        evidence=EVIDENCE,
        attempts=(
            StrategyAttempt("R1", "primary_document", "old R1", "no_progress"),
        ),
    )
    queue = merge_next_action_queue(
        previous,
        replanned,
        active_consumed=False,
    )
    assert [(item.requirement_id, item.query) for item in queue] == [
        ("R2", "pending R2"),
        ("R1", "Find the original primary document"),
    ]

    stopped = parse_research_assessment(
        _payload(decision="stop_research", termination_reason="coverage_complete"),
        requirements=REQUIREMENTS,
        evidence=EVIDENCE,
        attempts=(),
    )
    assert merge_next_action_queue(previous, stopped, active_consumed=False) == ()


def test_requirement_keyed_queue_stays_bounded_and_rotates_across_assessments() -> None:
    coverage = (
        RequirementCoverage("R1", RequirementStatus.WEAK),
        RequirementCoverage("R2", RequirementStatus.WEAK),
    )
    gaps = (
        CriticalGap("R1", "R1 remains weak", "high"),
        CriticalGap("R2", "R2 remains weak", "high"),
    )

    def assessment(round_index: int) -> ResearchAssessment:
        return ResearchAssessment(
            decision=ResearchDecision.CONTINUE,
            coverage=coverage,
            critical_gaps=gaps,
            next_actions=(
                NextResearchAction(
                    "R1",
                    "query_rewrite",
                    f"R1 query {round_index}",
                    "high",
                    "Improve R1",
                ),
                NextResearchAction(
                    "R1",
                    "paper_search",
                    f"duplicate R1 {round_index}",
                    "high",
                    "Also improve R1",
                ),
                NextResearchAction(
                    "R2",
                    "official_database",
                    f"R2 query {round_index}",
                    "high",
                    "Improve R2",
                ),
            ),
        )

    queue: tuple[NextResearchAction, ...] = ()
    active_requirements: list[str] = []
    for round_index in range(10):
        queue = merge_next_action_queue(
            queue,
            assessment(round_index),
            active_consumed=bool(queue),
        )
        assert len(queue) == 2
        assert len({item.requirement_id for item in queue}) == len(queue)
        active_requirements.append(queue[0].requirement_id)

    assert active_requirements == ["R1", "R2"] * 5
