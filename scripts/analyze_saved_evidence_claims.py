#!/usr/bin/env python3
"""Replay saved V2 Evidence through the Passage/Candidate layer without network calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from src.research.evidence_claim_pipeline import (
    deterministic_verify_candidate_claims,
    extract_candidate_claims,
    verified_evidence_claims,
)
from src.research.v2_contracts import ResearchPlan, SupervisorOutcome, WorkPacket


async def analyze(path: Path, thread_id: str) -> dict[str, object]:
    logging.getLogger("langgraph.checkpoint.serde.jsonplus").setLevel(logging.ERROR)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        checkpoint = await saver.aget_tuple({
            "configurable": {"thread_id": thread_id}
        })
    if checkpoint is None:
        raise ValueError(f"no checkpoint for thread {thread_id}")
    state = checkpoint.checkpoint["channel_values"]
    plan = state.get("v2_plan") or state.get("plan")
    challenge = state.get("v2_challenge_outcome")
    outcome = (
        challenge.supervisor_outcome
        if challenge is not None and hasattr(challenge, "supervisor_outcome")
        else state.get("v2_supervisor_outcome") or state.get("supervisor_outcome")
    )
    result_value = state.get("result")
    if (
        result_value is not None
        and hasattr(result_value, "supervisor_outcome")
    ):
        outcome = result_value.supervisor_outcome
    if not isinstance(plan, ResearchPlan) or not isinstance(outcome, SupervisorOutcome):
        raise ValueError("checkpoint has no V2 plan/supervisor outcome")
    if not getattr(plan, "evidence_requirements", None):
        plan = ResearchPlan.create(
            plan.brief_revision,
            tuple(plan.core_questions),
            tuple(plan.report_outline),
            tuple(plan.source_guidance),
            tuple(plan.work_hints),
            plan.fallback_reason,
        )
    totals = {
        "workers": len(outcome.worker_results),
        "evidence": 0,
        "passages": 0,
        "candidates": 0,
        "verified_claims": 0,
        "exact_quote_violations": 0,
        "old_claims": 0,
    }
    examples: list[dict[str, str]] = []
    for worker in outcome.worker_results:
        question_ids = tuple(dict.fromkeys(
            item.requirement_id
            for item in worker.evidence
            if any(
                question.question_id == item.requirement_id
                for question in plan.core_questions
            )
        ))
        if not question_ids:
            question_ids = tuple(
                question.question_id
                for question in plan.core_questions
                if any(
                    question.question_id in claim.question_ids
                    for claim in worker.claims
                )
            )
        if not question_ids:
            continue
        packet = WorkPacket.create(
            "Offline replay of saved Evidence",
            question_ids,
            "Verified Claims",
            tuple(plan.source_guidance),
            0,
            0,
            9999999999.0,
        )
        documents, passages, candidates = extract_candidate_claims(
            packet, plan, worker.evidence
        )
        assessments = deterministic_verify_candidate_claims(
            candidates,
            passages,
            plan.evidence_requirements,
        )
        claims = verified_evidence_claims(
            worker.evidence,
            documents,
            passages,
            candidates,
            assessments,
        )
        passage_by_id = {item.passage_id: item for item in passages}
        totals["evidence"] += len(worker.evidence)
        totals["passages"] += len(passages)
        totals["candidates"] += len(candidates)
        totals["verified_claims"] += len(claims)
        totals["old_claims"] += len(worker.claims)
        totals["exact_quote_violations"] += sum(
            item.exact_quote not in passage_by_id[item.passage_ids[0]].exact_text
            for item in candidates
        )
        for item in candidates:
            if len(examples) >= 8:
                break
            examples.append({
                "candidate": item.text,
                "exact_quote": item.exact_quote,
                "requirement_id": item.requirement_ids[0],
            })
    draft = state.get("v2_report_draft")
    audit = state.get("v2_citation_audit")
    audit_issues = tuple(getattr(audit, "issues", ()) or ())
    return {
        "checkpoint": str(path),
        "thread_id": thread_id,
        **totals,
        "draft_assertion_count": sum(
            len(section.assertions)
            for section in getattr(draft, "sections", ())
        ),
        "audit_status": getattr(audit, "status", None),
        "audit_issue_counts": {
            f"{category}/{status}": sum(
                item.category == category and item.status == status
                for item in audit_issues
            )
            for category, status in sorted({
                (item.category, item.status) for item in audit_issues
            })
        },
        "audit_issue_examples": [
            {
                "category": item.category,
                "status": item.status,
                "claim_text": item.claim_text[:300],
            }
            for item in audit_issues[:8]
        ],
        "examples": examples,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--thread-id", required=True)
    args = parser.parse_args()
    print(json.dumps(
        asyncio.run(analyze(args.checkpoint, args.thread_id)),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
