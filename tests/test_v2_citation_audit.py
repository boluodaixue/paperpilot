"""Phase 5 tests for deterministic plus semantic citation auditing."""

from __future__ import annotations

import json

import pytest

from src.research.citation_audit import (
    audit_citations,
    citation_followup_directives,
    deterministic_citation_issues,
)
from src.research.models import EvidenceItem, ResearchStatus
from src.research.v2_contracts import (
    BlueWorkerResult,
    CitationIssue,
    CoreQuestion,
    EvidenceClaim,
    ResearchPlan,
    SupervisorOutcome,
)


def _evidence():
    return (EvidenceItem(
        "evidence-1", "Official result is 42%.", "web", "Official report",
        "https://example.com/report", "table:2", "The result was 42%.",
    ),)


def test_deterministic_audit_rejects_unknown_ids_and_unknown_urls() -> None:
    markdown = (
        "# Findings\n\nClaim. [[EVIDENCE:evidence-forged]]\n\n"
        "More at https://forged.example/path"
    )
    issues = deterministic_citation_issues(markdown, _evidence())

    assert {item.category for item in issues} == {"invalid"}
    assert any("evidence-forged" in item.claim_text for item in issues)
    assert any("forged.example" in item.claim_text for item in issues)


def test_deterministic_audit_rejects_even_known_raw_url_without_double_missing() -> None:
    line = "The official result is described at https://example.com/report"

    issues = deterministic_citation_issues(f"# Findings\n\n{line}", _evidence())

    assert [(item.category, item.claim_text) for item in issues] == [
        ("invalid", "https://example.com/report")
    ]


def test_deterministic_audit_never_reports_invalid_and_missing_for_same_line() -> None:
    line = "A long unsupported assertion uses [[EVIDENCE:forged-id]] in this report."

    issues = deterministic_citation_issues(f"# Findings\n\n{line}", _evidence())

    assert {item.category for item in issues} == {"invalid"}


@pytest.mark.asyncio
async def test_semantic_audit_is_tool_free_and_rejects_unknown_evidence() -> None:
    calls = []

    async def policy(messages, *, tools=None):
        calls.append(tools)
        return {"content": json.dumps({"issues": [{
            "claim_text": "The result was 42%.",
            "section": "Findings",
            "evidence_ids": ["evidence-forged"],
            "category": "overclaim",
            "severity": "high",
            "repair_action": "qualify",
        }]})}

    with pytest.raises(ValueError, match="unknown Evidence"):
        await audit_citations(
            policy,
            "# Findings\n\nThe result was 42%. [[EVIDENCE:evidence-1]]",
            _evidence(),
        )
    assert calls == [[]]


@pytest.mark.asyncio
async def test_semantic_audit_reports_unsupported_scope() -> None:
    async def policy(messages, *, tools=None):
        return {"content": json.dumps({"issues": [{
            "claim_text": "The result applies universally.",
            "section": "Findings",
            "evidence_ids": ["evidence-1"],
            "category": "overclaim",
            "severity": "high",
            "repair_action": "qualify",
        }]})}

    outcome = await audit_citations(
        policy,
        "# Findings\n\nThe result applies universally. [[EVIDENCE:evidence-1]]",
        _evidence(),
    )

    assert outcome.status == "issues"
    assert outcome.issues[0].repair_action == "qualify"


def test_critical_citation_gap_maps_only_through_claim_lineage() -> None:
    question = CoreQuestion.create("Verify the reported result")
    plan = ResearchPlan.create(0, (question,))
    evidence = _evidence()[0]
    claim = EvidenceClaim.create(
        "Official result is 42%.", (question.question_id,),
        (evidence.evidence_id,), evidence.source_ref, evidence.locator,
        evidence.excerpt,
    )
    outcome = SupervisorOutcome(
        plan.plan_id,
        (BlueWorkerResult(
            "packet-1", ResearchStatus.COMPLETED, "done",
            claims=(claim,), evidence=(evidence,),
        ),),
        (question.question_id,), (question.question_id,), (), 1, 18000,
    )
    issue = CitationIssue.create(
        claim.claim, "Findings", (evidence.evidence_id,),
        "missing", "high", "add_citation",
    )
    unmapped = CitationIssue.create(
        "An unrelated sentence", "Findings", (),
        "missing", "high", "add_citation",
    )

    question_ids, guidance = citation_followup_directives(
        (issue, unmapped), outcome
    )

    assert question_ids == (question.question_id,)
    assert issue.issue_id in "\n".join(guidance[question.question_id])
    assert unmapped.issue_id not in "\n".join(guidance[question.question_id])
