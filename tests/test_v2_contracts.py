"""Phase 1 tests for serializable Research Agent V2 contracts."""

from __future__ import annotations

import json
from dataclasses import asdict

from src.research.v2_contracts import (
    ChallengeDecision,
    CitationAuditOutcome,
    CitationIssue,
    CoreQuestion,
    EvidenceClaim,
    ResearchChallenge,
    ResearchPlan,
    WorkPacket,
    stable_content_id,
)


def test_stable_content_id_is_canonical_and_not_mapping_order_dependent() -> None:
    left = stable_content_id("plan", {"b": [2, 1], "a": {"x": True}})
    right = stable_content_id("plan", {"a": {"x": True}, "b": [2, 1]})

    assert left == right
    assert left.startswith("plan-")
    assert len(left) == len("plan-") + 16


def test_same_contract_content_produces_stable_ids() -> None:
    question_a = CoreQuestion.create(
        description="Compare Chinese reasoning quality",
        required=True,
        priority="high",
        origin="brief_direction",
    )
    question_b = CoreQuestion.create(
        description="Compare Chinese reasoning quality",
        required=True,
        priority="high",
        origin="brief_direction",
    )
    assert question_a == question_b

    plan_a = ResearchPlan.create(
        brief_revision=2,
        core_questions=(question_a,),
        report_outline=("Summary", "Evidence"),
        source_guidance=("Prefer primary sources",),
        work_hints=("Parallelize independent benchmarks",),
    )
    plan_b = ResearchPlan.create(
        brief_revision=2,
        core_questions=(question_b,),
        report_outline=("Summary", "Evidence"),
        source_guidance=("Prefer primary sources",),
        work_hints=("Parallelize independent benchmarks",),
    )
    assert plan_a == plan_b

    packet_a = WorkPacket.create(
        objective="Collect benchmark evidence",
        question_ids=(question_a.question_id,),
        expected_output="Evidence claims",
        source_guidance=("Primary sources",),
        max_tool_calls=4,
        token_budget=2000,
        deadline_at=123.0,
        wave="initial",
    )
    packet_b = WorkPacket.create(
        objective="Collect benchmark evidence",
        question_ids=(question_a.question_id,),
        expected_output="Evidence claims",
        source_guidance=("Primary sources",),
        max_tool_calls=4,
        token_budget=2000,
        deadline_at=123.0,
        wave="initial",
    )
    assert packet_a == packet_b


def test_all_phase_one_contracts_are_json_serializable() -> None:
    question = CoreQuestion.create("Verify the key claim")
    plan = ResearchPlan.create(0, (question,))
    claim = EvidenceClaim.create(
        claim="The source supports the key claim.",
        question_ids=(question.question_id,),
        evidence_ids=("evidence-1",),
        source_ref="https://example.com/source",
        locator="section:1",
        excerpt="Source-locatable excerpt",
        limitations="Single source",
        confidence="medium",
        comparability_notes="Same test conditions",
    )
    challenge = ResearchChallenge.create(
        category="weak_source",
        target_question_ids=(question.question_id,),
        target_claim_ids=(claim.claim_id,),
        reason="Only one source supports the claim.",
        severity="high",
        requested_evidence="Independent primary source",
        suggested_query="official source key claim",
    )
    issue = CitationIssue.create(
        claim_text="The key claim is true.",
        section="Findings",
        evidence_ids=("evidence-1",),
        category="overclaim",
        severity="high",
        repair_action="qualify",
    )
    outcome = CitationAuditOutcome(
        status="repaired",
        issues=(issue,),
        repaired_markdown="Qualified claim [[EVIDENCE:evidence-1]]",
        unresolved=("Independent confirmation unavailable",),
    )

    encoded = json.dumps(
        {
            "plan": asdict(plan),
            "claim": asdict(claim),
            "challenge": asdict(challenge),
            "decision": ChallengeDecision.ACCEPT,
            "audit": asdict(outcome),
        },
        sort_keys=True,
    )

    assert question.question_id in encoded
    assert challenge.challenge_id in encoded
    assert issue.issue_id in encoded
