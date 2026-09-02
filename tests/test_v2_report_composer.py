"""Structured, evidence-only Lead report composition tests."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.research.models import EvidenceItem, OutputStatus, ResearchStatus
from src.research.report_composer import _bounded_claims, compose_report
from src.research.v2_contracts import (
    BlueWorkerResult,
    CoreQuestion,
    EvidenceClaim,
    EvidenceRequirement,
    ResearchChallenge,
    ResearchChallengeLoopOutcome,
    ResearchPlan,
    SupervisorOutcome,
)


def _research_package(*, with_evidence: bool = True, claim_text: str | None = None):
    question = CoreQuestion.create("What does the primary source establish?")
    plan = ResearchPlan.create(0, (question,), report_outline=("Answer", "Limitations"))
    evidence = EvidenceItem(
        "evidence-1", "The primary source reports 42%.", "web", "Official report",
        "https://example.com/report", "table:2", "The measured value was 42%.",
        limitations="Single reported measurement",
    )
    claims = (EvidenceClaim.create(
        claim_text or "The official report gives a measured value of 42%.",
        (question.question_id,), (evidence.evidence_id,), evidence.source_ref,
        evidence.locator, evidence.excerpt, evidence.limitations,
    ),) if with_evidence else ()
    worker = BlueWorkerResult(
        "packet-1", ResearchStatus.COMPLETED if with_evidence else ResearchStatus.PARTIAL,
        "raw worker summary must not enter the prompt", claims=claims,
        evidence=(evidence,) if with_evidence else (),
        unresolved=("gap",) if not with_evidence else (),
        output_status=OutputStatus.VALID,
    )
    outcome = SupervisorOutcome(
        plan.plan_id, (worker,), (question.question_id,),
        (question.question_id,) if with_evidence else (),
        () if with_evidence else (question.question_id,), 1, 18000,
    )
    return plan, outcome


def _payload(outcome: SupervisorOutcome, text: str, *, unresolved=None, claim_ids=None):
    claim = outcome.worker_results[0].claims[0]
    return {
        "sections": [{
            "heading": "Answer",
            "assertions": [{
                "text": text,
                "claim_ids": claim_ids if claim_ids is not None else [claim.claim_id],
            }],
        }],
        "unresolved": [] if unresolved is None else unresolved,
    }


@pytest.mark.asyncio
async def test_composer_abstains_without_valid_evidence_and_does_not_call_policy() -> None:
    plan, outcome = _research_package(with_evidence=False)

    async def forbidden(*args, **kwargs):
        raise AssertionError("composer must not hallucinate without evidence")

    draft = await compose_report(forbidden, plan, outcome)

    assert draft.status == "abstained"
    assert draft.output_status is OutputStatus.FALLBACK
    assert "No source-locatable evidence" in draft.markdown


@pytest.mark.asyncio
async def test_composer_renders_citations_from_claim_lineage() -> None:
    plan, outcome = _research_package()
    captured = {}

    async def policy(messages, *, tools=None):
        captured["messages"] = messages
        captured["tools"] = tools
        return {"content": json.dumps(_payload(outcome, "The measured value was 42%."))}

    draft = await compose_report(policy, plan, outcome)

    assert captured["tools"] == []
    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    assert "raw worker summary must not enter the prompt" not in prompt
    assert "https://example.com/report" not in prompt
    assert "The measured value was 42%." not in prompt
    assert "claim_ids" in prompt
    assert draft.status == "drafted"
    assert draft.evidence_ids == ("evidence-1",)
    assert "[[EVIDENCE:evidence-1]]" in draft.markdown
    assert draft.sections[0].assertions[0].claim_ids


@pytest.mark.asyncio
async def test_synthesis_requirement_needs_its_cited_report_structure() -> None:
    base_plan, base_outcome = _research_package()
    research_question = base_plan.core_questions[0]
    synthesis_question = CoreQuestion.create(
        "Form the comparison and disclose evidence gaps",
        requires_external_evidence=False,
    )
    plan = ResearchPlan.create(
        0,
        (research_question, synthesis_question),
        report_outline=("Answer", "Comparison conclusion"),
    )
    outcome = replace(base_outcome, plan_id=plan.plan_id)
    claim = outcome.worker_results[0].claims[0]

    async def complete_policy(messages, *, tools=None):
        return {"content": json.dumps({
            "sections": [
                {
                    "heading": "Answer",
                    "assertions": [{
                        "text": "The measured value was 42%.",
                        "claim_ids": [claim.claim_id],
                    }],
                },
                {
                    "heading": "Comparison conclusion",
                    "assertions": [{
                        "text": "The available comparison is limited to the verified measurement.",
                        "claim_ids": [claim.claim_id],
                    }],
                },
            ],
            "unresolved": ["No second comparable measurement was verified."],
        })}

    complete = await compose_report(complete_policy, plan, outcome)
    assert complete.incomplete_synthesis_requirement_ids == ()
    assert "[[EVIDENCE:evidence-1]]" in complete.markdown

    async def missing_policy(messages, *, tools=None):
        return {"content": json.dumps(_payload(
            outcome,
            "The measured value was 42%.",
        ))}

    missing = await compose_report(missing_policy, plan, outcome)
    assert missing.incomplete_synthesis_requirement_ids == (
        synthesis_question.question_id,
    )
    assert any("lacks its report structure" in item for item in missing.unresolved)


@pytest.mark.asyncio
async def test_composer_repairs_unknown_claim_id_before_rendering() -> None:
    plan, outcome = _research_package()
    calls = 0

    async def policy(messages, *, tools=None):
        nonlocal calls
        calls += 1
        claim_ids = ["claim-forged"] if calls == 1 else None
        return {"content": json.dumps(_payload(
            outcome, "The measured value was 42%.", claim_ids=claim_ids
        ))}

    draft = await compose_report(policy, plan, outcome)

    assert calls == 2
    assert "claim-forged" not in draft.markdown
    assert draft.output_status is OutputStatus.REPAIRED
    assert any("malformed Lead composer structure" in item for item in draft.unresolved)


@pytest.mark.asyncio
async def test_composer_repairs_model_prose_with_raw_urls_and_markdown_images() -> None:
    plan, outcome = _research_package()
    calls = 0

    async def policy(messages, *, tools=None):
        nonlocal calls
        calls += 1
        text = (
            "![chart](https://example.com/chart.png) See https://example.com/report"
            if calls == 1 else "The measured value was 42%."
        )
        return {"content": json.dumps(_payload(outcome, text))}

    draft = await compose_report(policy, plan, outcome)

    assert calls == 2
    assert "http" not in draft.markdown
    assert "![" not in draft.markdown
    assert draft.output_status is OutputStatus.REPAIRED


@pytest.mark.asyncio
async def test_composer_excludes_explicitly_conflicted_claim_and_discloses_gap() -> None:
    plan, outcome = _research_package()
    claim = outcome.worker_results[0].claims[0]
    challenge = ResearchChallenge.create(
        "conflict", claim.question_ids, (claim.claim_id,),
        "A directly contradictory Passage remains unresolved", "high", status="accepted",
    )
    reviewed = ResearchChallengeLoopOutcome(outcome, challenges=(challenge,))

    async def forbidden(*args, **kwargs):
        raise AssertionError("all unresolved challenged claims must be withheld")

    draft = await compose_report(forbidden, plan, reviewed)

    assert draft.status == "abstained"
    assert challenge.challenge_id in "\n".join(draft.unresolved)
    assert claim.claim not in draft.markdown


@pytest.mark.asyncio
async def test_composer_withholds_medium_non_comparable_target_in_legacy_v2() -> None:
    plan, outcome = _research_package()
    claim = outcome.worker_results[0].claims[0]
    challenge = ResearchChallenge.create(
        "non_comparable",
        claim.question_ids,
        (claim.claim_id,),
        "Do not infer direct equivalence with a different mechanism.",
        "medium",
        status="unresolved_disclosed",
    )
    reviewed = ResearchChallengeLoopOutcome(outcome, challenges=(challenge,))

    async def policy(*args, **kwargs):
        raise AssertionError("targeted unresolved Claim must be withheld")

    draft = await compose_report(policy, plan, reviewed)

    assert draft.status == "abstained"
    assert claim.claim not in draft.markdown


@pytest.mark.asyncio
async def test_composer_removes_assertion_that_no_red_challenges_exist() -> None:
    plan, outcome = _research_package()
    challenge = ResearchChallenge.create(
        "missing_question", (plan.core_questions[0].question_id,), (),
        "A required market example is still missing.", "high",
        status="unresolved_disclosed",
    )
    reviewed = ResearchChallengeLoopOutcome(outcome, challenges=(challenge,))

    async def policy(messages, *, tools=None):
        assert "unresolved_disclosed" in messages[-1]["content"]
        return {"content": json.dumps(_payload(
            outcome, "There are no unresolved Red-team challenges."
        ))}

    draft = await compose_report(policy, plan, reviewed)

    assert "no unresolved Red-team challenges" not in draft.markdown
    assert "The official report gives a measured value" in draft.markdown
    assert draft.output_status is OutputStatus.REPAIRED


@pytest.mark.asyncio
async def test_composer_programmatically_preserves_workflow_unresolved() -> None:
    plan, outcome = _research_package()
    unresolved_outcome = outcome.__class__(
        **{
            **outcome.__dict__,
            "resolved_question_ids": (),
            "unresolved_question_ids": (plan.core_questions[0].question_id,),
        }
    )

    async def policy(messages, *, tools=None):
        return {"content": json.dumps(_payload(
            unresolved_outcome, "The measured value was 42%."
        ))}

    draft = await compose_report(policy, plan, unresolved_outcome)

    assert plan.core_questions[0].question_id in draft.unresolved


@pytest.mark.asyncio
async def test_composer_repairs_one_invalid_json_response_without_tools() -> None:
    plan, outcome = _research_package()
    calls = []

    async def policy(messages, *, tools=None):
        calls.append((messages, tools))
        if len(calls) == 1:
            return {"content": "# invalid"}
        return {"content": json.dumps(_payload(outcome, "The measured value was 42%."))}

    draft = await compose_report(policy, plan, outcome)

    assert len(calls) == 2
    assert all(tools == [] for _, tools in calls)
    assert "INVALID RESPONSE" in calls[1][0][-1]["content"]
    assert draft.evidence_ids == ("evidence-1",)
    assert draft.output_status is OutputStatus.REPAIRED


@pytest.mark.asyncio
async def test_composer_falls_back_to_clean_claims_when_repair_is_invalid() -> None:
    plan, outcome = _research_package()
    calls = []

    async def policy(messages, *, tools=None):
        calls.append((messages, tools))
        return {"content": "not valid JSON"}

    draft = await compose_report(policy, plan, outcome)

    assert len(calls) == 2
    assert "The official report gives a measured value of 42%." in draft.markdown
    assert "[[EVIDENCE:evidence-1]]" in draft.markdown
    assert draft.output_status is OutputStatus.REPAIRED
    assert any("deterministic Evidence Claim report" in item for item in draft.unresolved)


@pytest.mark.asyncio
async def test_deterministic_fallback_preserves_plan_sections_and_synthesis_gap() -> None:
    base_plan, base_outcome = _research_package()
    first = base_plan.core_questions[0]
    second = CoreQuestion.create("Verify disclosure requirements")
    synthesis = CoreQuestion.create(
        "Form the structured comparison and disclose gaps",
        requires_external_evidence=False,
    )
    plan = ResearchPlan.create(
        0,
        (first, second, synthesis),
        report_outline=("Answer", "Disclosure", "Comparison conclusion"),
    )
    outcome = replace(
        base_outcome,
        plan_id=plan.plan_id,
        assigned_question_ids=(first.question_id, second.question_id),
        resolved_question_ids=(first.question_id,),
        unresolved_question_ids=(second.question_id,),
    )

    async def invalid_policy(messages, *, tools=None):
        return {"content": "not valid JSON"}

    draft = await compose_report(invalid_policy, plan, outcome)

    assert [section.heading for section in draft.sections[:3]] == [
        "Answer",
        "Disclosure",
        "Comparison conclusion",
    ]
    assert "The official report gives a measured value of 42%." in draft.markdown
    assert "该部分缺少可报告的已验证 Claim" in draft.markdown
    assert "未形成经验证的综合结论" in draft.markdown
    assert draft.incomplete_synthesis_requirement_ids == ()
    assert draft.output_status is OutputStatus.REPAIRED


@pytest.mark.asyncio
async def test_raw_page_shaped_claim_is_quarantined_before_policy_call() -> None:
    raw = "| Navigation | Value |\n|---|---|\n| Privacy policy | https://example.com |"
    plan, outcome = _research_package(claim_text=raw)

    async def forbidden(*args, **kwargs):
        raise AssertionError("raw page-shaped Claim must not reach the composer")

    draft = await compose_report(forbidden, plan, outcome)

    assert draft.status == "abstained"
    assert "Privacy policy" not in draft.markdown
    assert draft.uncovered_question_ids == (plan.core_questions[0].question_id,)
    assert draft.quarantined_claim_count == 1


def test_bounded_claims_covers_each_question_before_second_claims() -> None:
    questions = tuple(CoreQuestion.create(f"Question {index}") for index in range(7))
    plan = ResearchPlan.create(0, questions)
    claims = tuple(
        EvidenceClaim.create(
            f"Supported claim {question_index}-{claim_index}.",
            (question.question_id,),
            (f"evidence-{question_index}-{claim_index}",),
            f"https://example.com/{question_index}/{claim_index}",
            "section:1",
            "A grounded excerpt.",
        )
        for question_index, question in enumerate(questions)
        for claim_index in range(2)
    )

    selected = _bounded_claims(plan, claims, total_limit=12)

    covered = {item for claim in selected for item in claim.question_ids}
    assert covered == {item.question_id for item in questions}
    assert len(selected) == 12


def test_default_claim_budget_scales_beyond_twelve_for_multi_question_report() -> None:
    questions = tuple(CoreQuestion.create(f"Question {index}") for index in range(4))
    plan = ResearchPlan.create(0, questions)
    claims = tuple(
        EvidenceClaim.create(
            f"Supported concise claim {question_index}-{claim_index}.",
            (question.question_id,),
            (f"evidence-{question_index}-{claim_index}",),
            f"https://authority.gov/{question_index}/{claim_index}",
            "section:1",
            "A grounded excerpt.",
        )
        for question_index, question in enumerate(questions)
        for claim_index in range(8)
    )

    selected = _bounded_claims(plan, claims)

    assert 12 < len(selected) <= 32
    assert {item for claim in selected for item in claim.question_ids} == {
        item.question_id for item in questions
    }


def test_bounded_claims_covers_each_evidence_requirement_before_duplicates() -> None:
    question = CoreQuestion.create("Compare the regulated mechanisms")
    requirements = tuple(
        EvidenceRequirement.create(
            question.question_id,
            f"Prove mechanism {index}",
        )
        for index in range(3)
    )
    plan = ResearchPlan.create(
        0,
        (question,),
        evidence_requirements=requirements,
    )
    claims = tuple(
        EvidenceClaim.create(
            f"Verified mechanism {index}.",
            (question.question_id,),
            (f"E{index}",),
            f"https://authority.gov/{index}",
            f"section:{index}",
            f"Verified mechanism {index}.",
            requirement_ids=(requirement.requirement_id,),
        )
        for index, requirement in enumerate(requirements)
    )

    selected = _bounded_claims(plan, claims, total_limit=2)

    covered = {item for claim in selected for item in claim.requirement_ids}
    assert covered == {item.requirement_id for item in requirements}
