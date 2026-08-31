"""Phase 5 tests for the evidence-only Lead report composer."""

from __future__ import annotations

import json

import pytest

from src.research.models import EvidenceItem, OutputStatus, ResearchStatus
from src.research.report_composer import compose_report
from src.research.v2_contracts import (
    BlueWorkerResult,
    CoreQuestion,
    EvidenceClaim,
    ResearchPlan,
    ResearchChallenge,
    ResearchChallengeLoopOutcome,
    SupervisorOutcome,
)


def _research_package(*, with_evidence: bool = True):
    question = CoreQuestion.create("What does the primary source establish?")
    plan = ResearchPlan.create(0, (question,), report_outline=("Answer", "Limitations"))
    evidence = EvidenceItem(
        "evidence-1", "The primary source reports 42%.", "web", "Official report",
        "https://example.com/report", "table:2", "The measured value was 42%.",
        limitations="Single reported measurement",
    )
    claims = (EvidenceClaim.create(
        "The official report gives a measured value of 42%.",
        (question.question_id,), (evidence.evidence_id,), evidence.source_ref,
        evidence.locator, evidence.excerpt, evidence.limitations,
    ),) if with_evidence else ()
    worker = BlueWorkerResult(
        "packet-1", ResearchStatus.COMPLETED if with_evidence else ResearchStatus.PARTIAL,
        "raw worker summary must not enter the prompt", claims=claims,
        evidence=(evidence,) if with_evidence else (), unresolved=("gap",) if not with_evidence else (),
        output_status=OutputStatus.VALID,
    )
    outcome = SupervisorOutcome(
        plan.plan_id, (worker,), (question.question_id,),
        (question.question_id,) if with_evidence else (),
        () if with_evidence else (question.question_id,), 1, 18000,
    )
    return plan, outcome


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
async def test_composer_uses_selected_claims_no_tools_and_preserves_evidence_ids() -> None:
    plan, outcome = _research_package()
    captured = {}

    async def policy(messages, *, tools=None):
        captured["messages"] = messages
        captured["tools"] = tools
        return {"content": json.dumps({
            "report_markdown": "# Answer\n\nThe measured value was 42%. [[EVIDENCE:evidence-1]]\n\n## Limitations\n\nSingle measurement.",
            "unresolved": [],
        })}

    draft = await compose_report(policy, plan, outcome)

    assert captured["tools"] == []
    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    assert "raw worker summary must not enter the prompt" not in prompt
    assert "evidence-1" in prompt
    assert draft.status == "drafted"
    assert draft.evidence_ids == ("evidence-1",)


@pytest.mark.asyncio
async def test_composer_removes_line_with_unknown_internal_evidence_marker() -> None:
    plan, outcome = _research_package()

    async def policy(messages, *, tools=None):
        return {"content": json.dumps({
            "report_markdown": "Unsupported. [[EVIDENCE:evidence-forged]]",
            "unresolved": [],
        })}

    draft = await compose_report(policy, plan, outcome)

    assert draft.status == "drafted_repaired"
    assert draft.output_status is OutputStatus.REPAIRED
    assert "evidence-forged" not in draft.markdown
    assert any("unknown Evidence" in item for item in draft.unresolved)


@pytest.mark.asyncio
async def test_composer_downgrades_table_when_unsafe_header_is_removed() -> None:
    plan, outcome = _research_package()

    async def policy(messages, *, tools=None):
        del messages
        assert tools == []
        return {"content": json.dumps({
            "report_markdown": (
                "| Forged [[EVIDENCE:evidence-forged]] | Value |\n"
                "|---|---|\n"
                "| Supported [[EVIDENCE:evidence-1]] | 42% |"
            ),
            "unresolved": [],
        })}

    draft = await compose_report(policy, plan, outcome)

    assert "evidence-forged" not in draft.markdown
    assert "|---|---|" not in draft.markdown
    assert "- Supported [[EVIDENCE:evidence-1]] — 42%" in draft.markdown


@pytest.mark.asyncio
async def test_composer_repairs_unknown_inventory_before_dropping_supported_content() -> None:
    plan, outcome = _research_package()
    calls = 0

    async def policy(messages, *, tools=None):
        nonlocal calls
        calls += 1
        assert tools == []
        if calls == 1:
            report = "# Answer\n\nThe measured value was 42%. [[EVIDENCE:invented-id]]"
        else:
            assert "allowed_evidence_ids" in messages[-1]["content"]
            report = "# Answer\n\nThe measured value was 42%. [[EVIDENCE:evidence-1]]"
        return {"content": json.dumps({
            "report_markdown": report,
            "unresolved": [],
        })}

    draft = await compose_report(policy, plan, outcome)

    assert calls == 2
    assert "The measured value was 42%" in draft.markdown
    assert "invented-id" not in draft.markdown
    assert draft.status == "drafted_repaired"
    assert draft.output_status is OutputStatus.REPAIRED


@pytest.mark.asyncio
async def test_composer_excludes_unresolved_challenged_claim_and_discloses_gap() -> None:
    plan, outcome = _research_package()
    claim = outcome.worker_results[0].claims[0]
    challenge = ResearchChallenge.create(
        "weak_source", claim.question_ids, (claim.claim_id,),
        "Only one weak source supports the result", "high", status="accepted",
    )
    reviewed = ResearchChallengeLoopOutcome(outcome, challenges=(challenge,))

    async def forbidden(*args, **kwargs):
        raise AssertionError("all unresolved challenged claims must be withheld")

    draft = await compose_report(forbidden, plan, reviewed)

    assert draft.status == "abstained"
    assert challenge.challenge_id in "\n".join(draft.unresolved)
    assert claim.claim not in draft.markdown


@pytest.mark.asyncio
async def test_composer_removes_claim_that_no_red_challenges_exist() -> None:
    plan, outcome = _research_package()
    challenge = ResearchChallenge.create(
        "missing_question",
        (plan.core_questions[0].question_id,),
        (),
        "A required market example is still missing.",
        "high",
        status="unresolved_disclosed",
    )
    reviewed = ResearchChallengeLoopOutcome(outcome, challenges=(challenge,))

    async def policy(messages, *, tools=None):
        assert tools == []
        assert "unresolved_disclosed" in messages[-1]["content"]
        return {"content": json.dumps({
            "report_markdown": (
                "# Answer\n\nThe measured value was 42%. "
                "[[EVIDENCE:evidence-1]]\n\n"
                "- 在本次上下文中不存在待决的挑战信息，因此无需因挑战限制正文。"
            ),
            "unresolved": [],
        })}

    draft = await compose_report(policy, plan, reviewed)

    assert "不存在待决的挑战" not in draft.markdown
    assert "无需因挑战" not in draft.markdown
    assert "The measured value was 42%" in draft.markdown
    assert draft.output_status is OutputStatus.REPAIRED
    assert any("contradicting supplied Red challenges" in item for item in draft.unresolved)


@pytest.mark.asyncio
async def test_composer_ignores_harmless_extra_response_metadata() -> None:
    plan, outcome = _research_package()

    async def policy(messages, *, tools=None):
        del messages
        assert tools == []
        return {"content": json.dumps({
            "report_markdown": (
                "# Answer\n\nThe measured value was 42%. "
                "[[EVIDENCE:evidence-1]]"
            ),
            "unresolved": [],
            "quality_notes": ["Kept as non-authoritative metadata"],
        })}

    draft = await compose_report(policy, plan, outcome)

    assert draft.status == "drafted"
    assert draft.evidence_ids == ("evidence-1",)


@pytest.mark.asyncio
async def test_composer_programmatically_preserves_unresolved_when_model_omits_field() -> None:
    plan, outcome = _research_package()
    unresolved_outcome = outcome.__class__(
        **{
            **outcome.__dict__,
            "resolved_question_ids": (),
            "unresolved_question_ids": (plan.core_questions[0].question_id,),
        }
    )

    async def policy(messages, *, tools=None):
        del messages
        assert tools == []
        return {"content": json.dumps({
            "report_markdown": (
                "# Answer\n\nThe measured value was 42%. "
                "[[EVIDENCE:evidence-1]]"
            ),
        })}

    draft = await compose_report(policy, plan, unresolved_outcome)

    assert plan.core_questions[0].question_id in draft.unresolved


@pytest.mark.asyncio
async def test_composer_repairs_one_invalid_json_response_without_tools() -> None:
    plan, outcome = _research_package()
    calls = []

    async def policy(messages, *, tools=None):
        calls.append((messages, tools))
        if len(calls) == 1:
            return {"content": "# Answer\n\nThe measured value was 42%."}
        return {"content": json.dumps({
            "report_markdown": (
                "# Answer\n\nThe measured value was 42%. "
                "[[EVIDENCE:evidence-1]]"
            ),
            "unresolved": [],
        })}

    draft = await compose_report(policy, plan, outcome)

    assert len(calls) == 2
    assert all(tools == [] for _, tools in calls)
    assert "INVALID RESPONSE" in calls[1][0][-1]["content"]
    assert draft.evidence_ids == ("evidence-1",)
