"""Phase 7 tests for report-body scoring and V2 rollout gates."""

from __future__ import annotations

from src.research.models import (
    MemoryManifest,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
    ResearchWorkflowResult,
)
from evaluation.benchmarks.research_bench import ResearchBench
from evaluation.metrics.rule_based import RuleBasedMetrics
from scripts.run_eval import v2_canary_gate, v2_structure_metrics


def test_researchbench_final_body_excludes_brief_frontmatter_and_references() -> None:
    report = """---
id: report
---

# Question mentioning GPT-4o

## Research Brief

- GPT-4o
- Claude 3.5
- Gemini 1.5
- Qwen2.5

## Findings

Only the supported result remains here.

## References

- GPT-4o reference title
"""
    body = ResearchBench.final_report_body(report)

    assert "Research Brief" not in body
    assert "GPT-4o" not in body
    assert "supported result" in body


def _workflow() -> ResearchWorkflowResult:
    report = (
        "# Result\n\n"
        "The measured benchmark result is 42 percent in the official test. [1]\n\n"
        "## References\n\n1. source"
    )
    return ResearchWorkflowResult(
        brief=ResearchBrief("Q", "O", (), ("D",), (), "R"),
        research_result=ResearchResult("r", ResearchStatus.COMPLETED, "summary"),
        report_markdown=report,
        memory_manifest=MemoryManifest("reports/r.md"),
        research_architecture="supervisor_v2",
        challenges=(
            {"status": "resolved", "category": "weak_source"},
            {"status": "rejected", "category": "uncertainty"},
        ),
        citation_issues=(),
        supplemental_wave_count=1,
        finalization_token_reserve=18000,
        core_question_count=4,
        assigned_core_question_count=4,
        worker_packet_count=4,
        unique_worker_packet_count=4,
        source_open_count=3,
        source_candidate_count=4,
        duplicate_source_count=1,
        acquisition_call_count=2,
        candidate_claim_count=3,
        verified_claim_count=3,
        support_assessment_count=3,
        entailed_assessment_count=3,
        evidence_requirement_coverage=({
            "requirement_id": "ER1",
            "question_id": "Q1",
            "status": "supported",
            "primary_source_required": False,
            "primary_source_present": False,
        },),
        composer_claim_count=3,
    )


def test_v2_structure_metrics_are_deterministic_and_source_backed() -> None:
    metrics = v2_structure_metrics(_workflow())

    assert metrics == {
        "core_question_assignment_rate": 1.0,
        "worker_duplicate_rate": 0.0,
        "source_open_ratio": 0.75,
        "search_to_open_rate": 0.75,
        "duplicate_source_rate": 0.25,
        "acquisition_call_count": 2,
        "evidence_per_acquisition": 0.0,
        "challenge_acceptance_rate": 0.5,
        "challenge_resolution_rate": 1.0,
        "unresolved_challenge_disclosure_rate": 1.0,
        "material_claim_citation_coverage": 1.0,
        "invalid_citation_count": 0,
        "raw_url_count_before_render": 0,
        "citation_issue_conflict_count": 0,
        "audit_log_leak_count": 0,
        "reportable_claim_rejection_count": 0,
        "claim_entailment_pass_rate": 1.0,
        "verified_claim_yield": 1.0,
        "evidence_requirement_coverage_rate": 1.0,
        "weighted_evidence_requirement_coverage_rate": 1.0,
        "primary_source_requirement_coverage_rate": 1.0,
        "composer_claim_survival_rate": 1.0,
        "citation_audit_removal_rate": 0.0,
        "verified_claims_in_report": 3,
        "finalization_reserve_tokens": 18000,
        "supplemental_wave_count": 1,
        "repair_applied": False,
        "repair_actions": [],
    }


def test_canary_gate_blocks_expansion_on_any_documented_failure() -> None:
    detail = {
        "output_status": "valid",
        "termination_reason": "coverage_complete",
        "judge_average": 5.8,
        "v2": v2_structure_metrics(_workflow()),
    }
    assert v2_canary_gate(detail) == (True, ())

    failed = {**detail, "termination_reason": "budget_forced", "judge_average": 4.9}
    failed["v2"] = {**detail["v2"], "invalid_citation_count": 1}
    allowed, reasons = v2_canary_gate(failed)
    assert not allowed
    assert set(reasons) == {
        "budget_forced", "invalid_citations_present", "judge_average_below_5"
    }


def test_canary_gate_blocks_report_boundary_regressions() -> None:
    detail = {
        "output_status": "valid",
        "termination_reason": "coverage_complete",
        "judge_average": 6.0,
        "v2": {
            **v2_structure_metrics(_workflow()),
            "raw_url_count_before_render": 1,
            "citation_issue_conflict_count": 1,
            "audit_log_leak_count": 1,
        },
    }

    allowed, reasons = v2_canary_gate(detail)

    assert not allowed
    assert set(reasons) == {
        "raw_urls_present",
        "citation_issue_conflicts_present",
        "citation_audit_log_leaked",
    }


def test_canary_gate_blocks_citation_valid_but_incomplete_research() -> None:
    detail = {
        "research_status": "partial",
        "output_status": "valid",
        "termination_reason": "coverage_complete",
        "judge_average": 6.0,
        "v2": v2_structure_metrics(_workflow()),
    }

    assert v2_canary_gate(detail) == (False, ("research_status_not_completed",))


def test_legacy_v2_gate_does_not_use_experimental_requirement_metrics() -> None:
    detail = {
        "research_status": "completed",
        "output_status": "valid",
        "termination_reason": "coverage_complete",
        "judge_average": 6.0,
        "v2": {
            **v2_structure_metrics(_workflow()),
            "evidence_requirement_coverage_rate": 0.75,
            "primary_source_requirement_coverage_rate": 0.5,
            "citation_audit_removal_rate": 0.2,
        },
    }

    allowed, reasons = v2_canary_gate(detail)

    assert allowed
    assert reasons == ()


def test_legacy_v2_gate_does_not_require_experimental_verified_claim_count() -> None:
    detail = {
        "research_status": "completed",
        "output_status": "valid",
        "termination_reason": "coverage_complete",
        "judge_average": 6.0,
        "v2": {
            **v2_structure_metrics(_workflow()),
            "verified_claims_in_report": 2,
        },
    }

    assert v2_canary_gate(detail) == (True, ())


def test_material_claim_metric_ignores_unresolved_and_reference_inventory() -> None:
    report = (
        "# Result\n\nA sufficiently long factual result paragraph has no citation marker.\n\n"
        "## Unresolved\n\nA long unresolved statement does not require a citation marker.\n\n"
        "## References\n\nA long reference title does not count as a material claim."
    )
    assert RuleBasedMetrics.material_claim_citation_coverage(report) == 0.0
