"""Phase 4 tests for one checkpointed, targeted supplemental wave."""

from __future__ import annotations

import json

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from src.research.models import (
    AgentLimits,
    EvidenceItem,
    ExecutionIdentity,
    OutputStatus,
    ResearchStatus,
)
from src.research.research_challenge import (
    _recheck_challenges,
    run_research_challenge_loop,
)
from src.research.research_supervisor import SupervisorBudget
from src.research.v2_contracts import (
    BlueWorkerResult,
    CoreQuestion,
    EvidenceClaim,
    ResearchPlan,
    ResearchChallenge,
    SupervisorOutcome,
    SupervisorV2Config,
)


class ChallengePolicy:
    def __init__(self, question_id: str) -> None:
        self.question_id = question_id
        self.calls = 0
        self.tools = []
        self.challenge_id = ""

    async def __call__(self, messages, *, tools=None):
        self.calls += 1
        self.tools.append(tools)
        prompt = messages[0]["content"]
        if "Lead Researcher" in prompt:
            return {"content": json.dumps({"decisions": [{
                "challenge_id": self.challenge_id,
                "decision": "accept",
                "evidence_ids": [],
                "reason": "Targeted evidence is warranted",
            }]})}
        if "recheck" in prompt.lower():
            return {"content": json.dumps({"statuses": [{
                "challenge_id": self.challenge_id,
                "status": "resolved",
                "evidence_ids": ["evidence-supplemental"],
                "reason": "Supplemental evidence now supports the question",
            }]})}
        payload = {
            "category": "missing_question",
            "target_question_ids": [self.question_id],
            "target_claim_ids": [],
            "reason": "The required question is unresolved",
            "severity": "high",
            "requested_evidence": "One opened primary source",
            "suggested_query": "official primary source",
        }
        from src.research.v2_contracts import ResearchChallenge
        self.challenge_id = ResearchChallenge.create(**payload).challenge_id
        return {"content": json.dumps({"challenges": [payload]})}


def _initial():
    question = CoreQuestion.create("Resolve the missing primary claim")
    plan = ResearchPlan.create(0, (question,))
    outcome = SupervisorOutcome(
        plan_id=plan.plan_id,
        worker_results=(),
        assigned_question_ids=(question.question_id,),
        resolved_question_ids=(),
        unresolved_question_ids=(question.question_id,),
        wave_count=1,
        finalization_token_reserve=18000,
    )
    return question, plan, outcome


@pytest.mark.asyncio
async def test_high_accepted_challenge_runs_one_supplemental_and_rechecks_only_it() -> None:
    question, plan, initial = _initial()
    policy = ChallengePolicy(question.question_id)
    worker_calls = []

    async def worker(packet, plan_arg, policy_arg, tools, **kwargs):
        del plan_arg, policy_arg, tools, kwargs
        worker_calls.append(packet)
        return BlueWorkerResult(
            packet_id=packet.packet_id,
            status=ResearchStatus.COMPLETED,
            summary="Supplement complete",
            claims=(EvidenceClaim.create(
                claim="Supplemental source resolves the required question.",
                question_ids=(question.question_id,),
                evidence_ids=("evidence-supplemental",),
                source_ref="https://example.com/supplemental",
                locator="section:2",
                excerpt="Opened primary-source support",
            ),),
            evidence=(EvidenceItem(
                "evidence-supplemental",
                "Supplemental source resolves the required question.",
                "web",
                "Supplemental primary source",
                "https://example.com/supplemental",
                "section:2",
                "Opened primary-source support",
            ),),
            output_status=OutputStatus.VALID,
        )

    saver = InMemorySaver()
    kwargs = dict(
        policy=policy,
        tools=(),
        identity=ExecutionIdentity("root-challenge", None, "root-challenge", 0),
        limits=AgentLimits(),
        settings=SupervisorV2Config(enabled=True),
        budget=SupervisorBudget(8, 80000, 9999999999.0),
        checkpointer=saver,
        worker_runner=worker,
    )
    first = await run_research_challenge_loop(plan, initial, **kwargs)
    second = await run_research_challenge_loop(plan, initial, **kwargs)

    assert second == first
    assert len(worker_calls) == 1
    assert worker_calls[0].wave == "supplemental"
    assert worker_calls[0].question_ids == (question.question_id,)
    assert "Suggested query: official primary source" in worker_calls[0].source_guidance
    assert "Requested evidence: One opened primary source" in worker_calls[0].source_guidance
    assert first.supervisor_outcome.wave_count == 2
    assert first.challenges[0].status == "resolved"
    assert first.challenges[0].resolution_evidence_ids == ("evidence-supplemental",)
    assert first.supplemental_packet_ids == (worker_calls[0].packet_id,)
    assert policy.calls == 3
    assert policy.tools == [[], [], []]


@pytest.mark.asyncio
async def test_low_severity_challenge_never_starts_supplemental_worker() -> None:
    question, plan, initial = _initial()

    class LowPolicy(ChallengePolicy):
        async def __call__(self, messages, *, tools=None):
            response = await super().__call__(messages, tools=tools)
            if self.calls == 1:
                payload = json.loads(response["content"])
                payload["challenges"][0]["severity"] = "low"
                from src.research.v2_contracts import ResearchChallenge
                self.challenge_id = ResearchChallenge.create(**payload["challenges"][0]).challenge_id
                response["content"] = json.dumps(payload)
            return response

    policy = LowPolicy(question.question_id)

    async def forbidden_worker(*args, **kwargs):
        raise AssertionError("low severity challenge cannot trigger research")

    result = await run_research_challenge_loop(
        plan,
        initial,
        policy=policy,
        tools=(),
        identity=ExecutionIdentity("root-low-red", None, "root-low-red", 0),
        limits=AgentLimits(),
        settings=SupervisorV2Config(enabled=True),
        budget=SupervisorBudget(8, 80000, 9999999999.0),
        worker_runner=forbidden_worker,
    )

    assert result.supervisor_outcome.wave_count == 1
    assert result.supplemental_question_ids == ()
    assert result.challenges[0].status == "unresolved_disclosed"
    assert policy.calls == 2


@pytest.mark.asyncio
async def test_claim_only_challenge_maps_back_to_question_and_drives_targeted_packet() -> None:
    question, plan, initial = _initial()
    original_claim = EvidenceClaim.create(
        "A weak claim",
        (question.question_id,),
        ("evidence-old",),
        "https://example.com/old",
        "section:1",
        "Weak support",
    )
    old_evidence = EvidenceItem(
        "evidence-old", "Weak support", "web", "Old source",
        "https://example.com/old", "section:1", "Weak support",
    )
    initial = initial.__class__(
        **{
            **initial.__dict__,
            "worker_results": (BlueWorkerResult(
                "packet-initial", ResearchStatus.PARTIAL, "weak",
                claims=(original_claim,), evidence=(old_evidence,),
            ),),
        }
    )

    class ClaimOnlyPolicy(ChallengePolicy):
        async def __call__(self, messages, *, tools=None):
            self.calls += 1
            self.tools.append(tools)
            prompt = messages[0]["content"]
            if "Lead Researcher" in prompt:
                return {"content": json.dumps({"decisions": [{
                    "challenge_id": self.challenge_id,
                    "decision": "accept",
                    "evidence_ids": [],
                    "reason": "Replace weak support",
                }]})}
            if "recheck" in prompt.lower():
                return {"content": json.dumps({"statuses": [{
                    "challenge_id": self.challenge_id,
                    "status": "resolved",
                    "evidence_ids": ["evidence-supplemental"],
                    "reason": "New primary evidence resolves it",
                }]})}
            payload = {
                "category": "weak_source",
                "target_question_ids": [],
                "target_claim_ids": [original_claim.claim_id],
                "reason": "The claim uses weak support",
                "severity": "high",
                "requested_evidence": "An opened primary source",
                "suggested_query": "official primary evidence",
            }
            from src.research.v2_contracts import ResearchChallenge
            self.challenge_id = ResearchChallenge.create(**payload).challenge_id
            return {"content": json.dumps({"challenges": [payload]})}

    policy = ClaimOnlyPolicy(question.question_id)
    packets = []

    async def worker(packet, *args, **kwargs):
        del args, kwargs
        packets.append(packet)
        evidence = EvidenceItem(
            "evidence-supplemental", "Strong support", "web", "Official",
            "https://example.com/new", "section:2", "Strong support",
        )
        claim = EvidenceClaim.create(
            "A strongly supported replacement claim", (question.question_id,),
            (evidence.evidence_id,), evidence.source_ref, evidence.locator,
            evidence.excerpt,
        )
        return BlueWorkerResult(
            packet.packet_id, ResearchStatus.COMPLETED, "done",
            claims=(claim,), evidence=(evidence,),
        )

    result = await run_research_challenge_loop(
        plan, initial, policy=policy, tools=(),
        identity=ExecutionIdentity("root-claim-map", None, "root-claim-map", 0),
        limits=AgentLimits(), settings=SupervisorV2Config(enabled=True),
        budget=SupervisorBudget(8, 80000, 9999999999.0), worker_runner=worker,
    )

    assert len(packets) == 1
    assert packets[0].question_ids == (question.question_id,)
    assert any(original_claim.claim_id in item for item in packets[0].source_guidance)
    assert result.challenges[0].status == "resolved"


@pytest.mark.asyncio
async def test_recheck_cannot_resolve_with_only_initial_wave_evidence() -> None:
    question = CoreQuestion.create("Verify the claim")
    plan = ResearchPlan.create(0, (question,))
    old = EvidenceItem(
        "evidence-old", "Old", "web", "Old source",
        "https://example.com/old", "section:1", "Old evidence",
    )
    new = EvidenceItem(
        "evidence-new", "New", "web", "New source",
        "https://example.com/new", "section:2", "New evidence",
    )
    old_claim = EvidenceClaim.create(
        "The claim", (question.question_id,), (old.evidence_id,),
        old.source_ref, old.locator, old.excerpt,
    )
    new_claim = EvidenceClaim.create(
        "The new claim", (question.question_id,), (new.evidence_id,),
        new.source_ref, new.locator, new.excerpt,
    )
    outcome = SupervisorOutcome(
        plan.plan_id,
        (
            BlueWorkerResult(
                "packet-initial", ResearchStatus.COMPLETED, "old",
                claims=(old_claim,), evidence=(old,),
            ),
            BlueWorkerResult(
                "packet-supplemental", ResearchStatus.COMPLETED, "new",
                claims=(new_claim,), evidence=(new,),
            ),
        ),
        (question.question_id,), (question.question_id,), (), 2, 18000,
    )
    challenge = ResearchChallenge.create(
        "weak_source", (question.question_id,), (old_claim.claim_id,),
        "Old evidence was weak", "high", status="accepted",
    )

    async def policy(messages, *, tools=None):
        del messages
        assert tools == []
        return {"content": json.dumps({"statuses": [{
            "challenge_id": challenge.challenge_id,
            "status": "resolved",
            "evidence_ids": [old.evidence_id],
            "reason": "Incorrectly reused old evidence",
        }]})}

    challenges, alerts = await _recheck_challenges(
        policy, (challenge,), outcome, ("packet-supplemental",)
    )

    assert challenges[0].status == "accepted"
    assert alerts and alerts[0].category == "red_recheck_unavailable"
