"""Deterministic Phase 6 fixtures; not collected as tests."""

from __future__ import annotations

import json

from src.research.models import EvidenceItem, OutputStatus, ResearchStatus
from src.research.v2_contracts import (
    BlueWorkerResult,
    CoreQuestion,
    EvidenceClaim,
    ReportDraft,
    ResearchChallengeLoopOutcome,
    ResearchPlan,
    SupervisorOutcome,
)


class AlignmentPolicy:
    async def __call__(self, messages, *, tools=None):
        return {"content": json.dumps({
            "objective": "Verify the result",
            "scope": ["Official source"],
            "directions": ["Verify the measured result"],
            "constraints": ["Use opened sources"],
            "expected_output": "Evidence-backed answer",
        })}


def package(brief_revision: int = 0):
    question = CoreQuestion.create("Verify the measured result")
    plan = ResearchPlan.create(brief_revision, (question,))
    evidence = EvidenceItem(
        "evidence-v2", "The official result is 42%.", "web", "Official report",
        "https://example.com/report", "table:2", "The measured result was 42%.",
    )
    claim = EvidenceClaim.create(
        "The official result is 42%.", (question.question_id,), (evidence.evidence_id,),
        evidence.source_ref, evidence.locator, evidence.excerpt,
    )
    worker = BlueWorkerResult(
        "packet-v2", ResearchStatus.COMPLETED, "done", claims=(claim,),
        evidence=(evidence,), output_status=OutputStatus.VALID,
    )
    supervisor = SupervisorOutcome(
        plan.plan_id, (worker,), (question.question_id,), (question.question_id,),
        (), 1, 18000,
    )
    challenge = ResearchChallengeLoopOutcome(supervisor)
    draft = ReportDraft(
        plan.plan_id,
        "# Result\n\nThe official result is 42%. [[EVIDENCE:evidence-v2]]",
        "drafted",
        (evidence.evidence_id,),
    )
    return plan, supervisor, challenge, draft
