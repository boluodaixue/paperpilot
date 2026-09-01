"""Passage-first Candidate Claim extraction and support verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.research.evidence_claim_pipeline import (
    claim_has_verified_lineage,
    extract_candidate_claims,
    verified_evidence_claims,
    verify_candidate_claims,
)
from src.research.models import EvidenceItem, ResearchResult, ResearchStatus
from src.research.research_supervisor import _requirement_coverage
from src.research.research_worker import _verified_worker_result
from src.research.v2_contracts import (
    CoreQuestion,
    BlueWorkerResult,
    EvidenceRequirement,
    ResearchPlan,
    SupportAssessment,
    WorkPacket,
)


def _package():
    question = CoreQuestion.create("What efficacy did the pivotal trial establish?")
    requirement = EvidenceRequirement.create(
        question.question_id,
        "Report the pivotal endpoint with numerator, denominator, and follow-up.",
        evidence_kind="clinical_endpoint",
        primary_source_required=True,
    )
    plan = ResearchPlan.create(
        0,
        (question,),
        evidence_requirements=(requirement,),
    )
    packet = WorkPacket.create(
        question.description,
        (question.question_id,),
        "Verified Claim",
        (),
        10,
        10000,
        9999999999.0,
    )
    evidence = EvidenceItem(
        "E1",
        "RAW FINDING claims a lifelong cure and unrelated navigation.",
        "paper",
        "Pivotal trial",
        "https://journal.example/trial",
        "Results, paragraph 2",
        "The pivotal trial reported transfusion independence in 32 of 35 participants.",
        requirement_id=requirement.requirement_id,
        action_id="A1",
        artifact_id="artifact-1",
    )
    return plan, packet, evidence


def test_candidate_is_extracted_from_exact_passage_not_generated_finding() -> None:
    plan, packet, evidence = _package()

    documents, passages, candidates = extract_candidate_claims(
        packet, plan, (evidence,)
    )

    assert len(documents) == len(passages) == len(candidates) == 1
    assert candidates[0].text == evidence.excerpt
    assert candidates[0].exact_quote in passages[0].exact_text
    assert "lifelong cure" not in candidates[0].text
    assert candidates[0].requirement_ids == (
        plan.evidence_requirements[0].requirement_id,
    )


def test_passage_can_emit_two_bounded_atomic_candidates() -> None:
    plan, packet, evidence = _package()
    first = "The pivotal trial reported transfusion independence in 32 of 35 participants."
    second = "Median follow-up was 20.4 months at the data cutoff."
    evidence = EvidenceItem(**{
        **evidence.__dict__,
        "excerpt": f"{first} {second} A navigation footer without evidence",
    })

    _, passages, candidates = extract_candidate_claims(packet, plan, (evidence,))

    assert len(passages) == 1
    assert len(candidates) == 2
    assert {item.text for item in candidates} == {first, second}


@pytest.mark.asyncio
async def test_independent_verifier_creates_report_claim_only_after_entailment() -> None:
    plan, packet, evidence = _package()
    documents, passages, candidates = extract_candidate_claims(packet, plan, (evidence,))

    async def policy(messages, *, tools=None):
        assert tools == []
        assert "independent evidence-support verifier" in messages[0]["content"]
        return {"content": json.dumps({
            "assessments": [{
                "candidate_id": candidates[0].candidate_id,
                "verdict": "entailed",
                "confidence": 0.98,
                "supported_scope": candidates[0].text,
                "unsupported_scope": "",
                "reason": "The exact Passage states the complete endpoint.",
            }]
        })}

    assessments = await verify_candidate_claims(policy, candidates, passages)
    claims = verified_evidence_claims(
        (evidence,), documents, passages, candidates, assessments
    )

    assert assessments[0].method == "semantic_verifier"
    assert len(claims) == 1
    assert claims[0].verification_status == "verified"
    assert claims[0].support_assessment_ids == (assessments[0].assessment_id,)
    assert claims[0].passage_ids == (passages[0].passage_id,)


def test_rejected_candidate_cannot_enter_verified_claim_inventory() -> None:
    plan, packet, evidence = _package()
    documents, passages, candidates = extract_candidate_claims(packet, plan, (evidence,))
    rejected = SupportAssessment.create(
        candidates[0].candidate_id,
        candidates[0].passage_ids,
        "irrelevant",
        0.99,
        reason="The Passage does not answer the requirement.",
    )

    claims = verified_evidence_claims(
        (evidence,), documents, passages, candidates, (rejected,)
    )

    assert claims == ()


@pytest.mark.asyncio
async def test_invalid_verifier_output_falls_back_only_for_exact_quote_claim() -> None:
    plan, packet, evidence = _package()
    _, passages, candidates = extract_candidate_claims(packet, plan, (evidence,))

    async def policy(messages, *, tools=None):
        return {"content": "not json"}

    assessments = await verify_candidate_claims(policy, candidates, passages)

    assert assessments[0].verdict == "entailed"
    assert assessments[0].method == "deterministic_exact_quote"


def test_requirement_coverage_requires_verified_claim_and_primary_source() -> None:
    plan, packet, evidence = _package()
    documents, passages, candidates = extract_candidate_claims(packet, plan, (evidence,))
    assessments = (
        SupportAssessment.create(
            candidates[0].candidate_id,
            candidates[0].passage_ids,
            "entailed",
            0.99,
        ),
    )
    claims = verified_evidence_claims(
        (evidence,), documents, passages, candidates, assessments
    )
    worker = BlueWorkerResult(
        packet.packet_id,
        ResearchStatus.COMPLETED,
        "verified",
        claims=claims,
        evidence=(evidence,),
        documents=documents,
        passages=passages,
        candidate_claims=candidates,
        support_assessments=assessments,
    )

    coverage = _requirement_coverage(plan, (worker,))

    assert coverage[0].status == "supported"
    assert coverage[0].primary_source_required is True
    assert coverage[0].primary_source_present is True
    assert claim_has_verified_lineage(
        claims[0], candidates, assessments, passages
    )


def test_tampered_claim_text_breaks_verified_lineage() -> None:
    plan, packet, evidence = _package()
    documents, passages, candidates = extract_candidate_claims(packet, plan, (evidence,))
    assessments = (
        SupportAssessment.create(
            candidates[0].candidate_id,
            candidates[0].passage_ids,
            "entailed",
            0.99,
        ),
    )
    claim = verified_evidence_claims(
        (evidence,), documents, passages, candidates, assessments
    )[0]
    tampered = claim.__class__(
        **{**claim.__dict__, "claim": "The treatment is a proven lifelong cure."}
    )

    assert not claim_has_verified_lineage(
        tampered, candidates, assessments, passages
    )


def test_requirement_coverage_rejects_unverified_direct_claim() -> None:
    plan, packet, evidence = _package()
    documents, passages, candidates = extract_candidate_claims(packet, plan, (evidence,))
    assessments = (
        SupportAssessment.create(
            candidates[0].candidate_id,
            candidates[0].passage_ids,
            "irrelevant",
            0.99,
        ),
    )
    worker = BlueWorkerResult(
        packet.packet_id,
        ResearchStatus.PARTIAL,
        "rejected",
        claims=(),
        evidence=(evidence,),
        documents=documents,
        passages=passages,
        candidate_claims=candidates,
        support_assessments=assessments,
    )

    coverage = _requirement_coverage(plan, (worker,))

    assert coverage[0].status == "uncovered"
    assert "verified Claim" in coverage[0].reason


@pytest.mark.parametrize(
    "case",
    json.loads(
        (Path(__file__).parent / "fixtures" / "evidence_claim_gold.json").read_text(
            encoding="utf-8"
        )
    ),
    ids=lambda item: item["id"],
)
def test_saved_domain_gold_passages_extract_only_expected_atomic_claim(case) -> None:
    question = CoreQuestion.create(case["question"])
    requirement = EvidenceRequirement.create(
        question.question_id,
        case["requirement"],
        primary_source_required=True,
    )
    plan = ResearchPlan.create(
        0,
        (question,),
        evidence_requirements=(requirement,),
    )
    packet = WorkPacket.create(
        question.description,
        (question.question_id,),
        "Verified Claim",
        (),
        1,
        1000,
        9999999999.0,
    )
    evidence = EvidenceItem(
        case["id"],
        "A generated finding must not control extraction.",
        case["source_type"],
        case["id"],
        case["source_ref"],
        "section:gold",
        case["excerpt"],
        requirement_id=requirement.requirement_id,
        action_id="A-gold",
        artifact_id="artifact-gold",
    )

    _, passages, candidates = extract_candidate_claims(packet, plan, (evidence,))

    if case["expected_text"] is None:
        assert candidates == ()
    else:
        assert candidates[0].text == case["expected_text"]
        assert candidates[0].exact_quote in passages[0].exact_text


@pytest.mark.asyncio
async def test_production_worker_cannot_publish_semantically_rejected_candidate() -> None:
    plan, packet, evidence = _package()
    result = ResearchResult(
        packet.packet_id,
        ResearchStatus.COMPLETED,
        "candidate collected",
        evidence=(evidence,),
    )

    async def verifier(messages, *, tools=None):
        candidate_id = json.loads(messages[-1]["content"])["candidates"][0][
            "candidate_id"
        ]
        return {"content": json.dumps({
            "assessments": [{
                "candidate_id": candidate_id,
                "verdict": "irrelevant",
                "confidence": 0.99,
                "supported_scope": "",
                "unsupported_scope": "The Passage does not answer the proof obligation.",
                "reason": "Wrong endpoint.",
            }]
        })}

    worker = await _verified_worker_result(
        packet,
        plan,
        result,
        verifier,
        preserve_full_evidence=False,
    )

    assert worker.candidate_claims
    assert worker.support_assessments[0].verdict == "irrelevant"
    assert worker.claims == ()
    assert worker.status is ResearchStatus.PARTIAL


@pytest.mark.asyncio
async def test_partial_support_is_narrowed_extractively_and_reverified() -> None:
    plan, packet, evidence = _package()
    supported_scope = "The pivotal trial reported transfusion independence in 32 of 35 participants"
    evidence = EvidenceItem(
        **{
            **evidence.__dict__,
            "excerpt": (
                supported_scope
                + "; the treatment therefore guarantees a lifelong cure."
            ),
        }
    )
    result = ResearchResult(
        packet.packet_id,
        ResearchStatus.COMPLETED,
        "candidate collected",
        evidence=(evidence,),
    )
    calls = 0

    async def verifier(messages, *, tools=None):
        nonlocal calls
        calls += 1
        candidate = json.loads(messages[-1]["content"])["candidates"][0]
        if calls == 1:
            verdict = "partially_entailed"
            scope = supported_scope
        else:
            verdict = "entailed"
            scope = candidate["text"]
        return {"content": json.dumps({
            "assessments": [{
                "candidate_id": candidate["candidate_id"],
                "verdict": verdict,
                "confidence": 0.98,
                "supported_scope": scope,
                "unsupported_scope": "lifelong cure" if calls == 1 else "",
                "reason": "Narrow to the observed endpoint." if calls == 1 else "Exact support.",
            }]
        })}

    worker = await _verified_worker_result(
        packet,
        plan,
        result,
        verifier,
        preserve_full_evidence=False,
    )

    assert calls == 2
    assert len(worker.candidate_claims) == 2
    assert worker.claims[0].claim == supported_scope
    assert all("lifelong cure" not in item.claim for item in worker.claims)
