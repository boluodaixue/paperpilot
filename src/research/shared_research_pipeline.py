"""Architecture-neutral research package for fair report generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .evidence_claim_pipeline import (
    deterministic_verify_candidate_claims,
    extract_candidate_claims,
    narrow_partially_entailed_candidates,
    verified_evidence_claims,
    verify_candidate_claims,
)
from .evidence_selection import select_representative_evidence
from .models import EvidenceItem, RequirementCoverage, ResearchResult, ResearchStatus
from .v2_contracts import (
    BlueWorkerResult,
    BlueWorkerUsage,
    EvidenceClaim,
    ResearchChallengeLoopOutcome,
    ResearchPlan,
    SupervisorOutcome,
    WorkPacket,
)
from .research_supervisor import _requirement_coverage


@dataclass(frozen=True)
class SharedResearchPackage:
    """One normalized package consumed by the common Composer/Citation path."""

    challenge_outcome: ResearchChallengeLoopOutcome
    selected_evidence_ids: tuple[str, ...]
    quarantined_evidence_count: int = 0


def shared_evidence_limit(plan: ResearchPlan) -> int:
    required_count = len(tuple(item for item in plan.core_questions if item.required))
    if not required_count:
        required_count = len(plan.core_questions)
    return min(40, max(24, required_count * 8))


async def _selected_claims(
    plan: ResearchPlan,
    evidence: Iterable[EvidenceItem],
    *,
    policy: object | None = None,
    selection_coverage: Iterable[RequirementCoverage] = (),
) -> tuple[
    tuple[EvidenceItem, EvidenceClaim],
    tuple,
    tuple,
    tuple,
    tuple,
]:
    selection_coverage = tuple(selection_coverage)
    allowed_questions = {item.question_id for item in plan.core_questions}
    allowed_requirements = {
        item.requirement_id for item in plan.evidence_requirements
    }
    reportable: list[EvidenceItem] = []
    for item in evidence:
        if (
            item.requirement_id in (allowed_questions | allowed_requirements)
            and item.source_ref
            and item.locator
            and item.excerpt
            and not item.limitations.startswith("Search-result snippet")
        ):
            reportable.append(item)
    if not reportable:
        return (), (), (), (), ()
    requirement_by_id = {
        item.requirement_id: item for item in plan.evidence_requirements
    }
    question_ids = tuple(dict.fromkeys(
        requirement_by_id[item.requirement_id].question_id
        if item.requirement_id in requirement_by_id else item.requirement_id
        for item in reportable
    ))
    packet = WorkPacket.create(
        "Normalize shared evidence through the verified Claim pipeline",
        question_ids,
        "Verified Claims",
        plan.source_guidance,
        0,
        0,
        9999999999.0,
    )
    # Authority and source diversity matter only after a Passage can produce a
    # reportable exact Claim.  This keeps navigation/index pages from consuming
    # the bounded shared inventory ahead of substantive lower-ranked sources.
    claimable: list[EvidenceItem] = []
    for item in reportable:
        _, _, item_candidates = extract_candidate_claims(
            packet,
            plan,
            (item,),
        )
        if item_candidates:
            claimable.append(item)
    selected = select_representative_evidence(
        claimable,
        selection_coverage,
        limit=shared_evidence_limit(plan),
        max_per_requirement=8,
        max_per_source=2,
        max_per_primary_source=4,
        requirement_descriptions={
            item.question_id: item.description for item in plan.core_questions
        },
    )
    if not selected:
        return (), (), (), (), ()
    documents, passages, candidates = extract_candidate_claims(
        packet, plan, selected
    )
    agent_ranked_evidence_ids = {
        evidence_id
        for item in selection_coverage
        for evidence_id in item.evidence_ids
    }
    passage_by_id = {item.passage_id: item for item in passages}
    trusted_candidate_ids = {
        item.candidate_id
        for item in candidates
        if any(
            passage_by_id[passage_id].evidence_id in agent_ranked_evidence_ids
            for passage_id in item.passage_ids
        )
    }
    assessments = (
        await verify_candidate_claims(
            policy,
            candidates,
            passages,
            plan.evidence_requirements,
            trusted_candidate_ids=trusted_candidate_ids,
        )
        if policy is not None
        else deterministic_verify_candidate_claims(
            candidates,
            passages,
            plan.evidence_requirements,
        )
    )
    narrowed = narrow_partially_entailed_candidates(
        candidates,
        assessments,
        passages,
    )
    trusted_narrowed_ids = {
        item.candidate_id
        for item in narrowed
        if any(
            passage_by_id[passage_id].evidence_id in agent_ranked_evidence_ids
            for passage_id in item.passage_ids
        )
    }
    narrowed_assessments = (
        await verify_candidate_claims(
            policy,
            narrowed,
            passages,
            plan.evidence_requirements,
            trusted_candidate_ids=trusted_narrowed_ids,
        )
        if policy is not None and narrowed
        else deterministic_verify_candidate_claims(
            narrowed,
            passages,
            plan.evidence_requirements,
        ) if narrowed else ()
    )
    candidates = (*candidates, *narrowed)
    assessments = (*assessments, *narrowed_assessments)
    claims = verified_evidence_claims(
        selected, documents, passages, candidates, assessments
    )
    evidence_by_id = {item.evidence_id: item for item in selected}
    return (
        tuple((evidence_by_id[claim.evidence_ids[0]], claim) for claim in claims),
        documents,
        passages,
        candidates,
        assessments,
    )


async def normalize_supervisor_outcome(
    plan: ResearchPlan,
    outcome: SupervisorOutcome,
    *,
    policy: object | None = None,
    selection_coverage: Iterable[RequirementCoverage] = (),
) -> SharedResearchPackage:
    """Regenerate bounded Claims identically, independent of orchestration."""

    full_evidence = tuple(
        dict.fromkeys(
            item.evidence_id
            for worker in outcome.worker_results
            for item in worker.evidence
        )
    )
    evidence_by_id = {
        item.evidence_id: item
        for worker in outcome.worker_results
        for item in worker.evidence
    }
    pairs, documents, passages, candidates, assessments = await _selected_claims(
        plan,
        evidence_by_id.values(),
        policy=policy,
        selection_coverage=selection_coverage,
    )
    claims_by_packet: dict[str, list[EvidenceClaim]] = {
        item.packet_id: [] for item in outcome.worker_results
    }
    evidence_owner = {
        item.evidence_id: worker.packet_id
        for worker in outcome.worker_results
        for item in worker.evidence
    }
    for evidence, claim in pairs:
        packet_id = evidence_owner.get(evidence.evidence_id)
        if packet_id is not None:
            claims_by_packet.setdefault(packet_id, []).append(claim)
    normalized_workers = tuple(
        BlueWorkerResult(
            packet_id=worker.packet_id,
            status=worker.status,
            summary=worker.summary,
            claims=tuple(claims_by_packet.get(worker.packet_id, ())),
            evidence=tuple(worker.evidence),
            unresolved=tuple(worker.unresolved),
            alerts=tuple(worker.alerts),
            usage=worker.usage,
            termination_reason=worker.termination_reason,
            output_status=worker.output_status,
            documents=tuple(
                item
                for item in documents
                if any(
                    passage.document_id == item.document_id
                    and evidence_owner.get(passage.evidence_id) == worker.packet_id
                    for passage in passages
                )
            ),
            passages=tuple(
                item for item in passages
                if evidence_owner.get(item.evidence_id) == worker.packet_id
            ),
            candidate_claims=tuple(
                item for item in candidates
                if any(
                    evidence_owner.get(passage.evidence_id) == worker.packet_id
                    for passage in passages
                    if passage.passage_id in item.passage_ids
                )
            ),
            support_assessments=tuple(
                item for item in assessments
                if any(
                    candidate.candidate_id == item.candidate_id
                    and any(
                        evidence_owner.get(passage.evidence_id) == worker.packet_id
                        for passage in passages
                        if passage.passage_id in candidate.passage_ids
                    )
                    for candidate in candidates
                )
            ),
        )
        for worker in outcome.worker_results
    )
    requirement_coverage = _requirement_coverage(plan, normalized_workers)
    supported_requirements = {
        item.requirement_id for item in requirement_coverage if item.status == "supported"
    }
    required_by_question: dict[str, set[str]] = {}
    for item in plan.evidence_requirements:
        if item.required:
            required_by_question.setdefault(item.question_id, set()).add(item.requirement_id)
    required = tuple(
        item.question_id for item in plan.core_questions if item.required
    ) or tuple(item.question_id for item in plan.core_questions)
    normalized = SupervisorOutcome(
        plan_id=plan.plan_id,
        worker_results=normalized_workers,
        assigned_question_ids=tuple(outcome.assigned_question_ids),
        resolved_question_ids=tuple(
            item for item in required
            if required_by_question.get(item, set()) <= supported_requirements
        ),
        unresolved_question_ids=tuple(
            item for item in required
            if not required_by_question.get(item, set()) <= supported_requirements
        ),
        wave_count=outcome.wave_count,
        finalization_token_reserve=outcome.finalization_token_reserve,
        termination_reason=outcome.termination_reason,
        requirement_coverage=requirement_coverage,
    )
    selected_evidence_ids = tuple(dict.fromkeys(
        item.evidence_id for item, _ in pairs
    ))
    return SharedResearchPackage(
        ResearchChallengeLoopOutcome(normalized),
        selected_evidence_ids,
        max(0, len(full_evidence) - len(selected_evidence_ids)),
    )


async def package_legacy_result(
    plan: ResearchPlan,
    result: ResearchResult,
    *,
    finalization_token_reserve: int,
    policy: object | None = None,
) -> SharedResearchPackage:
    """Adapt one homogeneous-root result without discarding its full Evidence."""

    worker = BlueWorkerResult(
        packet_id=f"legacy-{result.task_id}",
        status=result.status,
        summary=result.summary,
        evidence=tuple(result.evidence),
        unresolved=tuple(result.unresolved),
        alerts=tuple(result.tool_alerts),
        usage=BlueWorkerUsage(
            iterations=result.iterations,
            tool_calls=result.tool_calls_used,
            estimated_tokens=result.estimated_tokens_used,
            retries=result.retries_used,
            source_candidates=result.source_candidate_count,
            sources_opened=result.source_open_count,
            duplicate_sources=result.duplicate_source_count,
            acquisition_calls=result.acquisition_call_count,
        ),
        termination_reason=result.termination_reason,
        output_status=result.output_status,
    )
    required = tuple(
        item.question_id for item in plan.core_questions if item.required
    ) or tuple(item.question_id for item in plan.core_questions)
    raw = SupervisorOutcome(
        plan.plan_id,
        (worker,),
        required,
        (),
        required,
        1,
        finalization_token_reserve,
        result.termination_reason,
    )
    return await normalize_supervisor_outcome(
        plan,
        raw,
        policy=policy,
        selection_coverage=result.coverage,
    )


__all__ = [
    "SharedResearchPackage",
    "normalize_supervisor_outcome",
    "package_legacy_result",
    "shared_evidence_limit",
]
