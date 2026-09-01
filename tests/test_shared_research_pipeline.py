"""Both orchestration architectures enter one bounded report package."""

from __future__ import annotations

from src.research.models import (
    EvidenceItem,
    OutputStatus,
    ResearchResult,
    ResearchStatus,
    TerminationReason,
)
from src.research.shared_research_pipeline import (
    normalize_supervisor_outcome,
    package_legacy_result,
    shared_evidence_limit,
)
from src.research.research_worker import _worker_result
from src.research.v2_contracts import (
    BlueWorkerResult,
    BlueWorkerUsage,
    CoreQuestion,
    ResearchPlan,
    SupervisorOutcome,
    WorkPacket,
)


def _plan(count: int = 3) -> ResearchPlan:
    return ResearchPlan.create(
        1,
        tuple(
            CoreQuestion.create(f"Verify official measured requirement {index}")
            for index in range(count)
        ),
        report_outline=("Comparison", "Conclusion"),
    )


def _evidence(plan: ResearchPlan) -> tuple[EvidenceItem, ...]:
    return tuple(
        EvidenceItem(
            evidence_id=f"E-{question_index}-{index}",
            finding=f"Atomic supported claim {question_index}-{index}",
            source_type="official",
            title="Official standard",
            source_ref=(
                "https://regulator.gov/standard"
                if index < 5 else f"https://authority{index}.gov/report"
            ),
            locator=f"page:{question_index * 10 + index}",
            excerpt=(
                f"The official clause {question_index}-{index} establishes the measured requirement."
            ),
            requirement_id=question.question_id,
            action_id=f"A-{question_index}-{index}",
            artifact_id=f"artifact-{question_index}-{index}",
        )
        for question_index, question in enumerate(plan.core_questions)
        for index in range(8)
    )


def _supervisor(plan: ResearchPlan, evidence: tuple[EvidenceItem, ...]) -> SupervisorOutcome:
    workers = tuple(
        BlueWorkerResult(
            packet_id=f"packet-{index}",
            status=ResearchStatus.COMPLETED,
            summary="done",
            evidence=tuple(
                item for item in evidence if item.requirement_id == question.question_id
            ),
            usage=BlueWorkerUsage(tool_calls=1, estimated_tokens=1000),
            output_status=OutputStatus.VALID,
        )
        for index, question in enumerate(plan.core_questions)
    )
    ids = tuple(item.question_id for item in plan.core_questions)
    return SupervisorOutcome(
        plan.plan_id,
        workers,
        ids,
        ids,
        (),
        1,
        50000,
        TerminationReason.COVERAGE_COMPLETE,
    )


def test_dynamic_limit_is_shared_and_capped() -> None:
    assert shared_evidence_limit(_plan(1)) == 12
    assert shared_evidence_limit(_plan(6)) == 24
    assert shared_evidence_limit(_plan(20)) == 36


def test_legacy_and_supervisor_generate_the_same_claim_inventory() -> None:
    plan = _plan()
    evidence = _evidence(plan)
    legacy = ResearchResult(
        task_id="legacy",
        status=ResearchStatus.COMPLETED,
        summary="done",
        evidence=evidence,
        termination_reason=TerminationReason.COVERAGE_COMPLETE,
        output_status=OutputStatus.VALID,
    )

    legacy_package = package_legacy_result(
        plan,
        legacy,
        finalization_token_reserve=50000,
    )
    supervisor_package = normalize_supervisor_outcome(
        plan,
        _supervisor(plan, evidence),
    )

    assert legacy_package.selected_evidence_ids == supervisor_package.selected_evidence_ids
    legacy_claims = tuple(
        claim
        for worker in legacy_package.challenge_outcome.supervisor_outcome.worker_results
        for claim in worker.claims
    )
    supervisor_claims = tuple(
        claim
        for worker in supervisor_package.challenge_outcome.supervisor_outcome.worker_results
        for claim in worker.claims
    )
    assert {item.claim_id for item in legacy_claims} == {
        item.claim_id for item in supervisor_claims
    }
    assert len(legacy_package.selected_evidence_ids) == shared_evidence_limit(plan)
    assert len(
        legacy_package.challenge_outcome.supervisor_outcome.worker_results[0].evidence
    ) == len(evidence)


def test_raw_page_shaped_claim_is_quarantined_before_both_composers() -> None:
    plan = _plan(1)
    valid = EvidenceItem(
        "valid", "Atomic supported claim", "official", "Official",
        "https://regulator.gov/rule", "section:1",
        "The official source states the applicable rule.",
        requirement_id=plan.core_questions[0].question_id,
    )
    raw = EvidenceItem(
        "raw", "| navigation | cookie |\n|---|---|", "web", "Raw page",
        "https://example.com/raw", "section:raw", "Raw page",
        requirement_id=plan.core_questions[0].question_id,
    )
    legacy = ResearchResult(
        "legacy", ResearchStatus.COMPLETED, "done", evidence=(valid, raw),
    )

    package = package_legacy_result(plan, legacy, finalization_token_reserve=12000)

    assert package.selected_evidence_ids == ("valid",)
    assert package.quarantined_evidence_count == 1


def test_shared_mode_preserves_full_worker_evidence_until_common_selector() -> None:
    plan = _plan(1)
    evidence = tuple(
        EvidenceItem(
            f"E{index}", f"Claim {index}", "official", "Official",
            f"https://authority{index}.gov/report", f"section:{index}",
            f"Official support {index}",
            requirement_id=plan.core_questions[0].question_id,
            action_id=f"A{index}", artifact_id=f"artifact-{index}",
        )
        for index in range(30)
    )
    result = ResearchResult(
        "worker", ResearchStatus.COMPLETED, "done", evidence=evidence,
    )
    packet = WorkPacket.create(
        "Research the fixed question",
        (plan.core_questions[0].question_id,),
        "Evidence claims",
        (),
        30,
        100000,
        9999999999.0,
    )

    normal = _worker_result(packet, result)
    shared = _worker_result(packet, result, preserve_full_evidence=True)

    assert len(normal.evidence) == 6
    assert len(shared.evidence) == 30
