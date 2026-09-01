"""Phase 4 tests for the tool-free Red research review boundary."""

from __future__ import annotations

import json

import pytest

from src.research.models import EvidenceItem, OutputStatus, ResearchStatus
from src.research.research_challenge import (
    _apply_red_constraints,
    adjudicate_research_challenges,
    review_research_package,
)
from src.research.v2_contracts import (
    BlueWorkerResult,
    CoreQuestion,
    EvidenceClaim,
    ResearchChallenge,
    ResearchPlan,
    SupervisorOutcome,
)


def _package(*, evidence_id: str = "evidence-1"):
    question = CoreQuestion.create("Verify the primary claim")
    plan = ResearchPlan.create(0, (question,))
    claim = EvidenceClaim.create(
        claim="The opened source supports the primary claim.",
        question_ids=(question.question_id,),
        evidence_ids=(evidence_id,),
        source_ref="https://example.com/source",
        locator="section:1",
        excerpt="Primary source excerpt",
    )
    evidence = EvidenceItem(
        evidence_id="evidence-1",
        finding="Primary source finding",
        source_type="web",
        title="Primary source",
        source_ref="https://example.com/source",
        locator="section:1",
        excerpt="Primary source excerpt",
    )
    worker = BlueWorkerResult(
        packet_id="packet-1",
        status=ResearchStatus.COMPLETED,
        summary="done",
        claims=(claim,),
        evidence=(evidence,),
        output_status=OutputStatus.VALID,
    )
    outcome = SupervisorOutcome(
        plan_id=plan.plan_id,
        worker_results=(worker,),
        assigned_question_ids=(question.question_id,),
        resolved_question_ids=(question.question_id,),
        unresolved_question_ids=(),
        wave_count=1,
        finalization_token_reserve=18000,
    )
    return plan, outcome, question, claim


class QueuePolicy:
    def __init__(self, responses):
        self.responses = list(responses)
        self.tools = []

    async def __call__(self, messages, *, tools=None):
        self.tools.append(tools)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return {"content": json.dumps(value)}


@pytest.mark.asyncio
async def test_red_accepts_only_six_categories_and_has_no_tools() -> None:
    plan, outcome, question, claim = _package()
    categories = (
        "missing_question",
        "unsupported_claim",
        "weak_source",
        "conflict",
        "non_comparable",
        "uncertainty",
    )
    policy = QueuePolicy(
        [
            {
                "challenges": [
                    {
                        "category": category,
                        "target_question_ids": [question.question_id],
                        "target_claim_ids": [claim.claim_id],
                        "reason": f"Detected {category}",
                        "severity": "high",
                        "requested_evidence": "Primary corroboration",
                        "suggested_query": "official corroboration",
                    }
                    for category in categories
                ]
            }
        ]
    )

    challenges, alerts = await review_research_package(policy, plan, outcome)

    assert tuple(item.category for item in challenges) == categories
    assert alerts == ()
    assert policy.tools == [[]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "style"),
        ("target_question_ids", ["question-unknown"]),
        ("target_claim_ids", ["claim-unknown"]),
    ],
)
async def test_red_rejects_unknown_category_question_or_claim(field, value) -> None:
    plan, outcome, question, claim = _package()
    item = {
        "category": "weak_source",
        "target_question_ids": [question.question_id],
        "target_claim_ids": [claim.claim_id],
        "reason": "Needs stronger support",
        "severity": "high",
        "requested_evidence": "Primary source",
        "suggested_query": "official source",
    }
    item[field] = value
    policy = QueuePolicy([{"challenges": [item]}])

    with pytest.raises(ValueError):
        await review_research_package(policy, plan, outcome, fallback_on_error=False)


@pytest.mark.asyncio
async def test_research_package_rejects_claim_with_unknown_evidence_id() -> None:
    plan, outcome, *_ = _package(evidence_id="evidence-unknown")
    policy = QueuePolicy([{"challenges": []}])

    with pytest.raises(ValueError, match="unknown Evidence"):
        await review_research_package(policy, plan, outcome, fallback_on_error=False)
    assert policy.tools == []


@pytest.mark.asyncio
async def test_rejected_challenge_requires_known_evidence_or_explicit_reason() -> None:
    plan, outcome, question, claim = _package()
    red = QueuePolicy(
        [{"challenges": [{
            "category": "weak_source",
            "target_question_ids": [question.question_id],
            "target_claim_ids": [claim.claim_id],
            "reason": "Only one source",
            "severity": "high",
            "requested_evidence": "Independent source",
            "suggested_query": "official independent source",
        }]}]
    )
    challenges, _ = await review_research_package(red, plan, outcome)
    lead = QueuePolicy(
        [{"decisions": [{
            "challenge_id": challenges[0].challenge_id,
            "decision": "reject",
            "evidence_ids": [],
            "reason": "",
        }]}]
    )

    decisions = await adjudicate_research_challenges(lead, challenges, outcome)

    assert decisions[0].decision.value == "accept"
    assert "Program guard" in decisions[0].reason
    assert lead.tools == [[]]


@pytest.mark.asyncio
async def test_red_failure_emits_quality_alert_and_gap_fallback() -> None:
    plan, outcome, *_ = _package()
    unresolved = outcome.__class__(
        **{**outcome.__dict__, "resolved_question_ids": (),
           "unresolved_question_ids": outcome.assigned_question_ids}
    )
    policy = QueuePolicy([RuntimeError("review service unavailable")])

    challenges, alerts = await review_research_package(policy, plan, unresolved)

    assert alerts and alerts[0].category == "red_review_unavailable"
    assert challenges and challenges[0].category == "missing_question"


@pytest.mark.asyncio
async def test_lead_cannot_reject_high_gap_with_a_reason_that_concedes_it() -> None:
    plan, outcome, question, claim = _package()
    challenge = __import__(
        "src.research.v2_contracts", fromlist=["ResearchChallenge"]
    ).ResearchChallenge.create(
        "weak_source", (question.question_id,), (claim.claim_id,),
        "Only a secondary source is available", "high",
    )
    lead = QueuePolicy([{"decisions": [{
        "challenge_id": challenge.challenge_id,
        "decision": "reject",
        "evidence_ids": [],
        "reason": "Only one weak source exists and no evidence can support the comparison",
    }]}])

    decisions = await adjudicate_research_challenges(lead, (challenge,), outcome)

    assert decisions[0].decision.value == "accept"
    assert "Program guard" in decisions[0].reason


@pytest.mark.asyncio
async def test_high_missing_question_always_gets_bounded_verification_before_closure() -> None:
    plan, outcome, question, claim = _package()
    from src.research.v2_contracts import ResearchChallenge
    challenge = ResearchChallenge.create(
        "missing_question", (question.question_id,), (claim.claim_id,),
        "A required comparison dimension is missing", "high",
    )
    lead = QueuePolicy([{"decisions": [{
        "challenge_id": challenge.challenge_id,
        "decision": "reject",
        "evidence_ids": ["evidence-1"],
        "reason": "The current source appears related",
    }]}])

    decisions = await adjudicate_research_challenges(lead, (challenge,), outcome)

    assert decisions[0].decision.value == "accept"
    assert "bounded supplemental verification" in decisions[0].reason


@pytest.mark.asyncio
async def test_lead_adjudication_receives_related_evidence_content() -> None:
    plan, outcome, question, claim = _package()
    from src.research.v2_contracts import ResearchChallenge
    challenge = ResearchChallenge.create(
        "weak_source", (question.question_id,), (claim.claim_id,),
        "The claim may be weak", "medium",
    )
    captured = {}

    async def lead(messages, *, tools=None):
        captured["messages"] = messages
        assert tools == []
        return {"content": json.dumps({"decisions": [{
            "challenge_id": challenge.challenge_id,
            "decision": "reject",
            "evidence_ids": ["evidence-1"],
            "reason": "The supplied primary excerpt directly supports the claim",
        }]})}

    decisions = await adjudicate_research_challenges(lead, (challenge,), outcome)

    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    assert "Primary source excerpt" in prompt
    assert decisions[0].decision.value == "reject"


@pytest.mark.asyncio
async def test_lead_cannot_reject_with_unrelated_known_evidence() -> None:
    plan, outcome, question, claim = _package()
    other_question = CoreQuestion.create("Unrelated question")
    other_claim = EvidenceClaim.create(
        "Unrelated claim", (other_question.question_id,), ("evidence-other",),
        "https://example.com/other", "section:9", "Other excerpt",
    )
    other_evidence = EvidenceItem(
        "evidence-other", "Other", "web", "Other source",
        "https://example.com/other", "section:9", "Other excerpt",
    )
    worker = outcome.worker_results[0].__class__(
        **{
            **outcome.worker_results[0].__dict__,
            "claims": (*outcome.worker_results[0].claims, other_claim),
            "evidence": (*outcome.worker_results[0].evidence, other_evidence),
        }
    )
    plan = ResearchPlan.create(0, (*plan.core_questions, other_question))
    outcome = outcome.__class__(
        **{**outcome.__dict__, "plan_id": plan.plan_id, "worker_results": (worker,)}
    )
    from src.research.v2_contracts import ResearchChallenge
    challenge = ResearchChallenge.create(
        "weak_source", (question.question_id,), (claim.claim_id,),
        "Needs relevant support", "medium",
    )
    lead = QueuePolicy([{"decisions": [{
        "challenge_id": challenge.challenge_id,
        "decision": "reject",
        "evidence_ids": ["evidence-other"],
        "reason": "Use another source",
    }]}])

    decisions = await adjudicate_research_challenges(
        lead, (challenge,), outcome
    )

    assert decisions[0].decision.value == "defer"
    assert "unrelated" in decisions[0].reason


@pytest.mark.asyncio
async def test_adjudication_batches_and_conservatively_fills_omitted_decisions() -> None:
    plan, outcome, question, claim = _package()
    from src.research.v2_contracts import ResearchChallenge
    challenges = tuple(
        ResearchChallenge.create(
            "uncertainty",
            (question.question_id,),
            (claim.claim_id,),
            f"Challenge {index}",
            "high" if index == 3 else "medium",
        )
        for index in range(4)
    )
    policy = QueuePolicy([
        {"decisions": [
            {
                "challenge_id": challenge.challenge_id,
                "decision": "accept",
                "evidence_ids": [],
                "reason": "Needs review",
            }
            for challenge in challenges[:3]
        ]},
        {"decisions": []},
    ])

    decisions = await adjudicate_research_challenges(
        policy, challenges, outcome
    )

    assert len(decisions) == 4
    assert len(policy.tools) == 2
    assert decisions[-1].decision.value == "accept"
    assert "omitted" in decisions[-1].reason


def test_hard_red_constraint_removes_claim_and_reopens_coverage() -> None:
    plan, outcome, question, claim = _package()
    challenge = ResearchChallenge.create(
        "conflict",
        (question.question_id,),
        (claim.claim_id,),
        "A directly contradictory Passage remains unresolved.",
        "medium",
        status="unresolved_disclosed",
    )

    constrained = _apply_red_constraints(plan, outcome, (challenge,))

    assert constrained.worker_results[0].claims == ()
    assert constrained.resolved_question_ids == ()
    assert constrained.unresolved_question_ids == (question.question_id,)


def test_legacy_red_constraint_withholds_any_unresolved_target_claim() -> None:
    plan, outcome, question, claim = _package()
    challenge = ResearchChallenge.create(
        "non_comparable",
        (question.question_id,),
        (claim.claim_id,),
        "The narrow fact is valid but cannot support a direct comparison.",
        "medium",
        status="unresolved_disclosed",
    )

    constrained = _apply_red_constraints(plan, outcome, (challenge,))

    assert constrained.worker_results[0].claims == ()
    assert constrained.resolved_question_ids == ()
    assert constrained.unresolved_question_ids == (question.question_id,)
