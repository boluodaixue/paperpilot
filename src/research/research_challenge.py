"""Tool-free Red research review and one bounded supplemental wave.

The strict parser/fallback pattern is shared with PaperPilot's legacy
``report_review`` module. The bounded reviewer/revision routing is informed by
GPT Researcher at commit ``6f998324006fd8e30d6e98e8815641da158d583c``;
this implementation uses PaperPilot's typed challenges, Evidence IDs, budgets,
and checkpoints. See ``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from typing import Any, Awaitable, Callable, Iterable, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .models import (
    AgentLimits,
    ExecutionIdentity,
    OutputStatus,
    ResearchStatus,
    TerminationReason,
    ToolAvailabilityAlert,
)
from .policy import call_policy
from .report_review import parse_json_object
from .research_supervisor import (
    SupervisorBudget,
    plan_supplemental_work_packets,
    worker_identity_for_packet,
)
from .research_worker import run_research_worker
from .v2_contracts import (
    BlueWorkerResult,
    BlueWorkerUsage,
    ChallengeAdjudication,
    ChallengeDecision,
    ResearchChallenge,
    ResearchChallengeLoopOutcome,
    ResearchPlan,
    SupervisorOutcome,
    SupervisorV2Config,
    WorkPacket,
    stable_content_id,
)


_CHALLENGE_FIELDS = {
    "category",
    "target_question_ids",
    "target_claim_ids",
    "reason",
    "severity",
    "requested_evidence",
    "suggested_query",
}
_DECISION_FIELDS = {"challenge_id", "decision", "evidence_ids", "reason"}
_STATUS_FIELDS = {"challenge_id", "status", "evidence_ids", "reason"}
_ADJUDICATION_BATCH_SIZE = 3
_MAX_ADJUDICATION_CLAIMS = 6
_MAX_ADJUDICATION_EVIDENCE = 8


class ResearchChallengeState(TypedDict, total=False):
    plan: ResearchPlan
    supervisor_outcome: SupervisorOutcome
    challenges: list[ResearchChallenge]
    adjudications: list[ChallengeAdjudication]
    quality_alerts: list[ToolAvailabilityAlert]
    supplemental_question_ids: list[str]
    supplemental_guidance: dict[str, list[str]]
    supplemental_packet_ids: list[str]
    red_complete: bool
    adjudication_complete: bool
    supplemental_complete: bool
    recheck_complete: bool
    result: ResearchChallengeLoopOutcome | None


WorkerRunner = Callable[..., Awaitable[BlueWorkerResult]]


def _inventory(outcome: SupervisorOutcome) -> tuple[dict[str, Any], set[str]]:
    claims = {
        claim.claim_id: claim
        for result in outcome.worker_results
        for claim in result.claims
    }
    evidence_ids = {
        evidence.evidence_id
        for result in outcome.worker_results
        for evidence in result.evidence
    }
    return claims, evidence_ids


def _evidence_inventory(outcome: SupervisorOutcome) -> dict[str, Any]:
    return {
        evidence.evidence_id: evidence
        for result in outcome.worker_results
        for evidence in result.evidence
    }


def _challenge_question_ids(
    challenge: ResearchChallenge,
    claims: dict[str, Any],
) -> tuple[str, ...]:
    """Resolve explicit questions plus the lineage of targeted claims."""
    return tuple(dict.fromkeys((
        *challenge.target_question_ids,
        *(
            question_id
            for claim_id in challenge.target_claim_ids
            if claim_id in claims
            for question_id in claims[claim_id].question_ids
        ),
    )))


def _challenge_claims(
    challenge: ResearchChallenge,
    claims: dict[str, Any],
) -> tuple[Any, ...]:
    if challenge.target_claim_ids:
        return tuple(
            claims[claim_id]
            for claim_id in challenge.target_claim_ids
            if claim_id in claims
        )
    question_ids = set(_challenge_question_ids(challenge, claims))
    return tuple(
        claim
        for claim in claims.values()
        if bool(question_ids.intersection(claim.question_ids))
    )


def _validate_package(plan: ResearchPlan, outcome: SupervisorOutcome) -> None:
    if outcome.plan_id != plan.plan_id:
        raise ValueError("Supervisor outcome belongs to another ResearchPlan")
    question_ids = {item.question_id for item in plan.core_questions}
    claims, evidence_ids = _inventory(outcome)
    for claim in claims.values():
        unknown_questions = set(claim.question_ids) - question_ids
        if unknown_questions:
            raise ValueError(f"claim references unknown Core Question: {sorted(unknown_questions)}")
        unknown_evidence = set(claim.evidence_ids) - evidence_ids
        if unknown_evidence:
            raise ValueError(f"claim references unknown Evidence ID: {sorted(unknown_evidence)}")


def _red_prompt(plan: ResearchPlan, outcome: SupervisorOutcome) -> list[dict[str, Any]]:
    package = {
        "plan": asdict(plan),
        "claims": [
            asdict(claim)
            for result in outcome.worker_results
            for claim in result.claims
        ],
        "sources": [
            {
                "evidence_id": item.evidence_id,
                "source_type": item.source_type,
                "title": item.title,
                "source_ref": item.source_ref,
                "locator": item.locator,
                "excerpt": item.excerpt,
                "limitations": item.limitations,
            }
            for result in outcome.worker_results
            for item in result.evidence
        ],
        "unresolved_question_ids": list(outcome.unresolved_question_ids),
        "worker_unresolved": [
            text for result in outcome.worker_results for text in result.unresolved
        ],
    }
    return [
        {
            "role": "system",
            "content": """You are the Red Research Reviewer. Review only the supplied
Research Plan and evidence package. Do not use tools, research, write a report,
or expose hidden reasoning. Return exactly {\"challenges\": [...]} where every
item has exactly: category, target_question_ids, target_claim_ids, reason,
severity, requested_evidence, suggested_query. category must be one of
missing_question, unsupported_claim, weak_source, conflict, non_comparable,
uncertainty. severity must be low, medium, or high. Use only supplied IDs.""",
        },
        {"role": "user", "content": json.dumps(package, ensure_ascii=False)},
    ]


def _parse_challenges(
    response: dict[str, Any],
    plan: ResearchPlan,
    outcome: SupervisorOutcome,
) -> tuple[ResearchChallenge, ...]:
    payload = parse_json_object(response, role="Red research")
    if set(payload) != {"challenges"} or not isinstance(payload["challenges"], list):
        raise ValueError("Red research review must contain only a challenges list")
    known_questions = {item.question_id for item in plan.core_questions}
    known_claims, _ = _inventory(outcome)
    challenges: list[ResearchChallenge] = []
    for item in payload["challenges"]:
        if not isinstance(item, dict) or set(item) != _CHALLENGE_FIELDS:
            raise ValueError("each ResearchChallenge must match the required schema")
        question_ids = tuple(dict.fromkeys(str(value) for value in item["target_question_ids"]))
        claim_ids = tuple(dict.fromkeys(str(value) for value in item["target_claim_ids"]))
        if set(question_ids) - known_questions:
            raise ValueError("ResearchChallenge references unknown question ID")
        if set(claim_ids) - set(known_claims):
            raise ValueError("ResearchChallenge references unknown claim ID")
        severity = str(item["severity"]).strip().lower()
        if severity not in {"low", "medium", "high"}:
            raise ValueError("ResearchChallenge severity must be low, medium, or high")
        challenges.append(
            ResearchChallenge.create(
                category=item["category"],
                target_question_ids=question_ids,
                target_claim_ids=claim_ids,
                reason=item["reason"],
                severity=severity,
                requested_evidence=item["requested_evidence"],
                suggested_query=item["suggested_query"],
            )
        )
    return tuple(challenges)


def _quality_alert(category: str, message: str) -> ToolAvailabilityAlert:
    payload = {"category": category, "message": message}
    return ToolAvailabilityAlert(
        alert_id=stable_content_id("quality-alert", payload),
        tool="red_reviewer",
        category=category,
        scope="research_v2",
        target="research_package",
        message=message,
        action_required="Review the disclosed unresolved questions and evidence limitations.",
        error=message,
    )


def _gap_fallback(
    plan: ResearchPlan,
    outcome: SupervisorOutcome,
) -> tuple[ResearchChallenge, ...]:
    questions = {item.question_id: item for item in plan.core_questions}
    return tuple(
        ResearchChallenge.create(
            category="missing_question",
            target_question_ids=(question_id,),
            target_claim_ids=(),
            reason=f"Supervisor gap check found unresolved question: {questions[question_id].description}",
            severity="high",
            requested_evidence="Source-locatable evidence for the required question",
            suggested_query=questions[question_id].description,
        )
        for question_id in outcome.unresolved_question_ids
        if question_id in questions
    )


async def review_research_package(
    policy: Any,
    plan: ResearchPlan,
    outcome: SupervisorOutcome,
    *,
    fallback_on_error: bool = True,
) -> tuple[tuple[ResearchChallenge, ...], tuple[ToolAvailabilityAlert, ...]]:
    """Run one full Red review with no tool schemas."""
    _validate_package(plan, outcome)
    try:
        response = await call_policy(policy, _red_prompt(plan, outcome), [])
        return _parse_challenges(response, plan, outcome), ()
    except Exception as exc:
        if not fallback_on_error:
            raise
        message = f"Red research review unavailable or invalid: {type(exc).__name__}: {exc}"
        return _gap_fallback(plan, outcome), (_quality_alert("red_review_unavailable", message),)


def _adjudication_prompt(challenges: Iterable[ResearchChallenge], outcome: SupervisorOutcome):
    challenge_items = tuple(challenges)
    claims, _ = _inventory(outcome)
    evidence = _evidence_inventory(outcome)
    contexts = []
    for challenge in challenge_items:
        related_claims = _challenge_claims(challenge, claims)[
            :_MAX_ADJUDICATION_CLAIMS
        ]
        related_evidence_ids = tuple(dict.fromkeys(
            evidence_id
            for claim in related_claims
            for evidence_id in claim.evidence_ids
            if evidence_id in evidence
        ))
        related_evidence_ids = related_evidence_ids[:_MAX_ADJUDICATION_EVIDENCE]
        contexts.append({
            "challenge": asdict(challenge),
            "related_claims": [
                {
                    "claim_id": item.claim_id,
                    "claim": item.claim[:1600],
                    "question_ids": list(item.question_ids),
                    "evidence_ids": list(item.evidence_ids),
                    "source_ref": item.source_ref,
                    "locator": item.locator,
                    "limitations": item.limitations[:800],
                    "comparability_notes": item.comparability_notes[:800],
                }
                for item in related_claims
            ],
            "related_evidence": [
                {
                    "evidence_id": evidence[item].evidence_id,
                    "title": evidence[item].title,
                    "source_type": evidence[item].source_type,
                    "source_ref": evidence[item].source_ref,
                    "locator": evidence[item].locator,
                    "excerpt": evidence[item].excerpt[:1600],
                    "limitations": evidence[item].limitations[:800],
                }
                for item in related_evidence_ids
            ],
        })
    return [
        {
            "role": "system",
            "content": """You are the Lead Researcher. Adjudicate each supplied Red
challenge once without tools. Return exactly {\"decisions\": [...]} with fields
challenge_id, decision, evidence_ids, reason. decision is accept, reject, or
defer. Reject only when the supplied related Evidence directly defeats the
challenge, and cite those related evidence_ids. IDs alone are not support.""",
        },
        {"role": "user", "content": json.dumps({
            "challenge_contexts": contexts,
            "known_evidence_ids": sorted({
                item["evidence_id"]
                for context in contexts
                for item in context["related_evidence"]
            }),
        }, ensure_ascii=False)},
    ]


def _fallback_adjudication(
    challenge: ResearchChallenge,
    reason: str,
) -> ChallengeAdjudication:
    decision = (
        ChallengeDecision.ACCEPT
        if challenge.severity == "high"
        else ChallengeDecision.DEFER
    )
    return ChallengeAdjudication(
        challenge.challenge_id,
        decision,
        (),
        f"Program guard used a conservative decision because {reason}.",
    )


def _normalize_adjudication(
    item: dict[str, Any],
    challenge: ResearchChallenge,
    outcome: SupervisorOutcome,
) -> ChallengeAdjudication:
    try:
        decision = ChallengeDecision(str(item["decision"]).lower())
    except ValueError:
        return _fallback_adjudication(challenge, "the Lead returned an unknown decision")
    _, known_evidence = _inventory(outcome)
    raw_evidence = item.get("evidence_ids", [])
    evidence_ids = tuple(dict.fromkeys(
        str(value) for value in raw_evidence
    )) if isinstance(raw_evidence, list) else ()
    if set(evidence_ids) - known_evidence:
        if decision is ChallengeDecision.REJECT:
            return _fallback_adjudication(
                challenge, "the rejection cited unknown Evidence"
            )
        evidence_ids = tuple(item for item in evidence_ids if item in known_evidence)
    reason = str(item.get("reason") or "").strip()
    conceded = any(
        marker in reason.casefold()
        for marker in (
            "missing", "lack", "no evidence", "only", "insufficient", "unavailable",
            "not included", "cannot support", "缺失", "不足", "没有", "仅有", "无法支撑",
        )
    )
    if (
        decision is ChallengeDecision.REJECT
        and challenge.category == "missing_question"
        and challenge.severity == "high"
    ):
        return _fallback_adjudication(
            challenge,
            "a high-severity missing-question challenge requires one bounded "
            "supplemental verification before it can be closed",
        )
    if (
        decision is ChallengeDecision.REJECT
        and challenge.severity == "high"
        and not evidence_ids
        and conceded
    ):
        return _fallback_adjudication(
            challenge, "the rejection reason conceded the evidence gap"
        )
    if decision is ChallengeDecision.REJECT:
        related_ids = {
            evidence_id
            for claim in _challenge_claims(challenge, _inventory(outcome)[0])
            for evidence_id in claim.evidence_ids
        }
        rejection_error = (
            "no related Evidence was cited"
            if not evidence_ids
            else "cited Evidence was unrelated to the challenge"
            if set(evidence_ids) - related_ids
            else "no grounded reason was provided"
            if not reason
            else ""
        )
        if rejection_error:
            return _fallback_adjudication(challenge, rejection_error)
    return ChallengeAdjudication(
        challenge.challenge_id,
        decision,
        evidence_ids,
        reason,
    )


async def adjudicate_research_challenges(
    policy: Any,
    challenges: Iterable[ResearchChallenge],
    outcome: SupervisorOutcome,
) -> tuple[ChallengeAdjudication, ...]:
    items = tuple(challenges)
    if not items:
        return ()
    decisions: list[ChallengeAdjudication] = []
    for offset in range(0, len(items), _ADJUDICATION_BATCH_SIZE):
        batch = items[offset:offset + _ADJUDICATION_BATCH_SIZE]
        batch_by_id = {item.challenge_id: item for item in batch}
        parsed: dict[str, ChallengeAdjudication] = {}
        try:
            response = await call_policy(
                policy,
                _adjudication_prompt(batch, outcome),
                [],
            )
            payload = parse_json_object(response, role="Lead challenge adjudication")
            raw_decisions = payload.get("decisions")
            if not isinstance(raw_decisions, list):
                raise ValueError("Lead adjudication requires a decisions list")
            for raw in raw_decisions:
                if not isinstance(raw, dict) or not _DECISION_FIELDS.issubset(raw):
                    continue
                challenge_id = str(raw.get("challenge_id", ""))
                if challenge_id not in batch_by_id or challenge_id in parsed:
                    continue
                parsed[challenge_id] = _normalize_adjudication(
                    raw,
                    batch_by_id[challenge_id],
                    outcome,
                )
        except Exception as exc:
            failure_reason = f"Lead adjudication was unavailable or invalid ({type(exc).__name__})"
        else:
            failure_reason = "the Lead omitted this challenge from its batch response"
        decisions.extend(
            parsed.get(
                challenge.challenge_id,
                _fallback_adjudication(challenge, failure_reason),
            )
            for challenge in batch
        )
    return tuple(decisions)


def _apply_decisions(
    challenges: Iterable[ResearchChallenge],
    decisions: Iterable[ChallengeAdjudication],
) -> tuple[ResearchChallenge, ...]:
    statuses = {item.challenge_id: item.decision.value + "ed" for item in decisions}
    statuses = {key: ("deferred" if value == "defered" else value) for key, value in statuses.items()}
    return tuple(
        replace(
            item,
            status=statuses.get(item.challenge_id, item.status),
            resolution_evidence_ids=(),
            resolution_reason="",
        )
        for item in challenges
    )


def _supplemental_targets(
    challenges: Iterable[ResearchChallenge],
    outcome: SupervisorOutcome,
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Map accepted high challenges to questions and targeted Worker guidance."""
    claims, _ = _inventory(outcome)
    evidence = _evidence_inventory(outcome)
    guidance: dict[str, list[str]] = {}
    for challenge in challenges:
        if challenge.status != "accepted" or challenge.severity != "high":
            continue
        question_ids = _challenge_question_ids(challenge, claims)
        related_claims = _challenge_claims(challenge, claims)
        opened_sources = tuple(dict.fromkeys(
            evidence[evidence_id].source_ref
            for claim in related_claims
            for evidence_id in claim.evidence_ids
            if evidence_id in evidence and evidence[evidence_id].source_ref
        ))
        directives = tuple(item for item in (
            f"Red challenge {challenge.challenge_id} ({challenge.category}): {challenge.reason}",
            f"Requested evidence: {challenge.requested_evidence}"
            if challenge.requested_evidence else "",
            f"Suggested query: {challenge.suggested_query}"
            if challenge.suggested_query else "",
            "Target claims: " + " | ".join(
                f"{claim.claim_id}: {claim.claim}" for claim in related_claims
            ) if related_claims else "",
            "Do not duplicate already opened sources: " + " | ".join(opened_sources)
            if opened_sources else "",
        ) if item)
        for question_id in question_ids:
            guidance.setdefault(question_id, []).extend(directives)
    stable = {
        question_id: tuple(dict.fromkeys(items))
        for question_id, items in guidance.items()
    }
    return tuple(stable), stable


async def execute_supplemental_work_packets(
    packets: tuple[WorkPacket, ...],
    *,
    plan: ResearchPlan,
    policy: Any,
    tools: tuple[Any, ...],
    identity: ExecutionIdentity,
    limits: AgentLimits,
    worker_runner: WorkerRunner,
    checkpointer: BaseCheckpointSaver | None,
    tool_artifact_store: Any | None,
) -> tuple[BlueWorkerResult, ...]:
    async def one(packet: WorkPacket) -> BlueWorkerResult:
        try:
            return await worker_runner(
                packet,
                plan,
                policy,
                tools,
                identity=worker_identity_for_packet(identity, packet),
                limits=limits,
                checkpointer=checkpointer,
                tool_artifact_store=tool_artifact_store,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return BlueWorkerResult(
                packet_id=packet.packet_id,
                status=ResearchStatus.FAILED,
                summary="Supplemental Blue Worker failed.",
                unresolved=(f"{type(exc).__name__}: {exc}",),
                output_status=OutputStatus.FALLBACK,
            )
    return tuple(sorted(await asyncio.gather(*(one(item) for item in packets)), key=lambda item: item.packet_id))


def merge_supervisor_outcome(
    plan: ResearchPlan,
    previous: SupervisorOutcome,
    supplemental: tuple[BlueWorkerResult, ...],
) -> SupervisorOutcome:
    results = tuple(sorted((*previous.worker_results, *supplemental), key=lambda item: item.packet_id))
    resolved = {
        question_id
        for result in results
        for claim in result.claims
        for question_id in claim.question_ids
    }
    required = tuple(item.question_id for item in plan.core_questions if item.required) or tuple(
        item.question_id for item in plan.core_questions
    )
    unresolved = tuple(item for item in required if item not in resolved)
    return SupervisorOutcome(
        plan_id=plan.plan_id,
        worker_results=results,
        assigned_question_ids=tuple(dict.fromkeys((*previous.assigned_question_ids, *(q for r in supplemental for c in r.claims for q in c.question_ids)))),
        resolved_question_ids=tuple(item for item in required if item in resolved),
        unresolved_question_ids=unresolved,
        wave_count=previous.wave_count + (1 if supplemental else 0),
        finalization_token_reserve=previous.finalization_token_reserve,
        termination_reason=(
            TerminationReason.COVERAGE_COMPLETE if not unresolved
            else TerminationReason.EVIDENCE_EXHAUSTED
        ),
    )


def _normalize_loop_outcome(value: ResearchChallengeLoopOutcome) -> ResearchChallengeLoopOutcome:
    """Restore tuple fields that generic checkpoint serializers may decode as lists."""
    workers: list[BlueWorkerResult] = []
    for result in value.supervisor_outcome.worker_results:
        claims = tuple(
            replace(
                claim,
                question_ids=tuple(claim.question_ids),
                evidence_ids=tuple(claim.evidence_ids),
            )
            for claim in result.claims
        )
        usage = result.usage
        if not isinstance(usage, BlueWorkerUsage):
            usage = BlueWorkerUsage(**dict(usage))
        workers.append(
            replace(
                result,
                claims=claims,
                evidence=tuple(result.evidence),
                unresolved=tuple(result.unresolved),
                alerts=tuple(result.alerts),
                usage=usage,
            )
        )
    supervisor = replace(
        value.supervisor_outcome,
        worker_results=tuple(workers),
        assigned_question_ids=tuple(value.supervisor_outcome.assigned_question_ids),
        resolved_question_ids=tuple(value.supervisor_outcome.resolved_question_ids),
        unresolved_question_ids=tuple(value.supervisor_outcome.unresolved_question_ids),
    )
    challenges = tuple(
        replace(
            item,
            target_question_ids=tuple(item.target_question_ids),
            target_claim_ids=tuple(item.target_claim_ids),
            resolution_evidence_ids=tuple(
                getattr(item, "resolution_evidence_ids", ())
            ),
        )
        for item in value.challenges
    )
    adjudications = tuple(
        replace(item, evidence_ids=tuple(item.evidence_ids))
        for item in value.adjudications
    )
    return ResearchChallengeLoopOutcome(
        supervisor_outcome=supervisor,
        challenges=challenges,
        adjudications=adjudications,
        quality_alerts=tuple(value.quality_alerts),
        supplemental_question_ids=tuple(value.supplemental_question_ids),
        supplemental_packet_ids=tuple(
            getattr(value, "supplemental_packet_ids", ())
        ),
    )


def _recheck_prompt(
    challenges: Iterable[ResearchChallenge],
    outcome: SupervisorOutcome,
    supplemental_packet_ids: Iterable[str],
):
    packet_ids = set(supplemental_packet_ids)
    claims = [
        asdict(claim)
        for result in outcome.worker_results
        if result.packet_id in packet_ids
        for claim in result.claims
    ]
    evidence = [
        asdict(item)
        for result in outcome.worker_results
        if result.packet_id in packet_ids
        for item in result.evidence
    ]
    return [
        {
            "role": "system",
            "content": """Recheck only the supplied previously accepted high-severity
challenges against the supplemental Evidence Claims. Do not perform a new full
Red review and do not use tools. Return exactly {\"statuses\": [...]} with
challenge_id, status, evidence_ids, reason. status is resolved, accepted, or
deferred. resolved requires directly relevant supplemental evidence_ids.""",
        },
        {"role": "user", "content": json.dumps({
            "challenges": [asdict(item) for item in challenges],
            "supplemental_claims": claims,
            "supplemental_evidence": evidence,
        }, ensure_ascii=False)},
    ]


async def _recheck_challenges(
    policy: Any,
    challenges,
    outcome,
    supplemental_packet_ids,
):
    selected = tuple(item for item in challenges if item.status == "accepted" and item.severity == "high")
    if not selected:
        return tuple(challenges), ()
    try:
        response = await call_policy(
            policy,
            _recheck_prompt(selected, outcome, supplemental_packet_ids),
            [],
        )
        payload = parse_json_object(response, role="Red challenge recheck")
        if set(payload) != {"statuses"} or not isinstance(payload["statuses"], list):
            raise ValueError("challenge recheck must contain only a statuses list")
        allowed = {item.challenge_id for item in selected}
        updates: dict[str, tuple[str, tuple[str, ...], str]] = {}
        claims, _ = _inventory(outcome)
        packet_ids = set(supplemental_packet_ids)
        supplemental_evidence = {
            evidence.evidence_id
            for result in outcome.worker_results
            if result.packet_id in packet_ids
            for evidence in result.evidence
        }
        for item in payload["statuses"]:
            if not isinstance(item, dict) or set(item) != _STATUS_FIELDS:
                raise ValueError("each recheck status must match the required schema")
            challenge_id = str(item["challenge_id"])
            status = str(item["status"]).lower()
            if challenge_id not in allowed or challenge_id in updates:
                raise ValueError("recheck references unknown or duplicate challenge")
            if status not in {"resolved", "accepted", "deferred"}:
                raise ValueError("invalid recheck status")
            evidence_ids = tuple(dict.fromkeys(
                str(value) for value in item["evidence_ids"]
            ))
            if set(evidence_ids) - supplemental_evidence:
                raise ValueError("recheck references non-supplemental Evidence")
            challenge = next(
                value for value in selected if value.challenge_id == challenge_id
            )
            target_questions = set(_challenge_question_ids(challenge, claims))
            relevant_ids = {
                evidence_id
                for claim in claims.values()
                if target_questions.intersection(claim.question_ids)
                for evidence_id in claim.evidence_ids
                if evidence_id in supplemental_evidence
            }
            if status == "resolved" and (
                not evidence_ids or set(evidence_ids) - relevant_ids
            ):
                raise ValueError(
                    "resolved challenge requires relevant supplemental Evidence"
                )
            updates[challenge_id] = (
                status,
                evidence_ids,
                str(item["reason"]).strip(),
            )
        if set(updates) != allowed:
            raise ValueError("recheck must update every selected challenge")
        return tuple(
            replace(
                item,
                status=updates[item.challenge_id][0],
                resolution_evidence_ids=updates[item.challenge_id][1],
                resolution_reason=updates[item.challenge_id][2],
            ) if item.challenge_id in updates else item
            for item in challenges
        ), ()
    except Exception as exc:
        message = f"Red challenge recheck unavailable or invalid: {type(exc).__name__}: {exc}"
        return tuple(challenges), (_quality_alert("red_recheck_unavailable", message),)


def _finalize_unresolved_challenges(
    challenges: Iterable[ResearchChallenge],
) -> tuple[ResearchChallenge, ...]:
    """Close bounded Red work with an explicit, safe disclosure state."""
    return tuple(
        replace(
            item,
            status="unresolved_disclosed",
            resolution_reason=(
                item.resolution_reason
                or "Bounded review/research did not fully resolve this issue; targeted "
                "claims remain withheld or qualified and the issue is disclosed."
            ),
        )
        if item.status in {"pending", "accepted", "deferred"}
        else item
        for item in challenges
    )


def build_research_challenge_graph(
    *,
    policy: Any,
    tools: Iterable[Any],
    identity: ExecutionIdentity,
    limits: AgentLimits,
    settings: SupervisorV2Config,
    budget: SupervisorBudget,
    checkpointer: BaseCheckpointSaver | None = None,
    worker_runner: WorkerRunner = run_research_worker,
    tool_artifact_store: Any | None = None,
):
    effective_checkpointer = checkpointer or InMemorySaver()
    tool_list = tuple(tools)

    async def red_review(state: ResearchChallengeState):
        if state.get("red_complete"):
            return {}
        if not settings.red_review_enabled or settings.max_red_review_rounds == 0:
            return {"challenges": [], "red_complete": True}
        challenges, alerts = await review_research_package(policy, state["plan"], state["supervisor_outcome"])
        return {"challenges": list(challenges), "quality_alerts": list(alerts), "red_complete": True}

    async def adjudicate(state: ResearchChallengeState):
        if state.get("adjudication_complete"):
            return {}
        decisions = await adjudicate_research_challenges(policy, state.get("challenges", []), state["supervisor_outcome"])
        challenges = _apply_decisions(state.get("challenges", []), decisions)
        supplemental_ids, supplemental_guidance = _supplemental_targets(
            challenges,
            state["supervisor_outcome"],
        )
        return {
            "challenges": list(challenges),
            "adjudications": list(decisions),
            "supplemental_question_ids": list(supplemental_ids),
            "supplemental_guidance": {
                key: list(value) for key, value in supplemental_guidance.items()
            },
            "adjudication_complete": True,
        }

    async def supplemental(state: ResearchChallengeState):
        if state.get("supplemental_complete"):
            return {}
        packets = plan_supplemental_work_packets(
            state["plan"], settings, budget, state["supervisor_outcome"],
            state.get("supplemental_question_ids", []),
            guidance_by_question=state.get("supplemental_guidance", {}),
        )
        results = await execute_supplemental_work_packets(
            packets,
            plan=state["plan"], policy=policy, tools=tool_list, identity=identity,
            limits=limits, worker_runner=worker_runner, checkpointer=effective_checkpointer,
            tool_artifact_store=tool_artifact_store,
        ) if packets else ()
        if results:
            outcome = merge_supervisor_outcome(
                state["plan"], state["supervisor_outcome"], results
            )
        else:
            current = state["supervisor_outcome"]
            outcome = replace(
                current,
                termination_reason=(
                    TerminationReason.EVIDENCE_EXHAUSTED
                    if current.unresolved_question_ids
                    else TerminationReason.COVERAGE_COMPLETE
                ),
            )
        return {
            "supervisor_outcome": outcome,
            "supplemental_packet_ids": [item.packet_id for item in packets],
            "supplemental_complete": True,
        }

    async def recheck(state: ResearchChallengeState):
        if state.get("recheck_complete"):
            return {}
        challenges = tuple(state.get("challenges", []))
        if state["supervisor_outcome"].wave_count > 1:
            challenges, alerts = await _recheck_challenges(
                policy,
                challenges,
                state["supervisor_outcome"],
                state.get("supplemental_packet_ids", []),
            )
        else:
            alerts = ()
        challenges = _finalize_unresolved_challenges(challenges)
        quality = [*state.get("quality_alerts", []), *alerts]
        result = ResearchChallengeLoopOutcome(
            supervisor_outcome=state["supervisor_outcome"],
            challenges=challenges,
            adjudications=tuple(state.get("adjudications", [])),
            quality_alerts=tuple(quality),
            supplemental_question_ids=tuple(state.get("supplemental_question_ids", [])),
            supplemental_packet_ids=tuple(state.get("supplemental_packet_ids", [])),
        )
        return {"challenges": list(challenges), "quality_alerts": quality, "recheck_complete": True, "result": result}

    builder = StateGraph(ResearchChallengeState)
    builder.add_node("red_review", red_review)
    builder.add_node("adjudicate", adjudicate)
    builder.add_node("supplemental", supplemental)
    builder.add_node("recheck", recheck)
    builder.add_edge(START, "red_review")
    builder.add_edge("red_review", "adjudicate")
    builder.add_edge("adjudicate", "supplemental")
    builder.add_edge("supplemental", "recheck")
    builder.add_edge("recheck", END)
    return builder.compile(checkpointer=effective_checkpointer)


async def run_research_challenge_loop(
    plan: ResearchPlan,
    supervisor_outcome: SupervisorOutcome,
    *,
    policy: Any,
    tools: Iterable[Any],
    identity: ExecutionIdentity,
    limits: AgentLimits,
    settings: SupervisorV2Config,
    budget: SupervisorBudget,
    checkpointer: BaseCheckpointSaver | None = None,
    worker_runner: WorkerRunner = run_research_worker,
    tool_artifact_store: Any | None = None,
    checkpoint_thread_id: str | None = None,
) -> ResearchChallengeLoopOutcome:
    identity.validate()
    _validate_package(plan, supervisor_outcome)
    graph = build_research_challenge_graph(
        policy=policy, tools=tools, identity=identity, limits=limits, settings=settings,
        budget=budget, checkpointer=checkpointer, worker_runner=worker_runner,
        tool_artifact_store=tool_artifact_store,
    )
    checkpoint_key = str(checkpoint_thread_id or identity.thread_id).strip()
    if not checkpoint_key:
        raise ValueError("Research challenge checkpoint thread ID cannot be empty")
    config = {"configurable": {"thread_id": f"{checkpoint_key}.red-review"}}
    snapshot = await graph.aget_state(config)
    if snapshot.values:
        saved = snapshot.values.get("result")
        if isinstance(saved, ResearchChallengeLoopOutcome):
            return _normalize_loop_outcome(saved)
        final = await graph.ainvoke(None, config=config)
    else:
        final = await graph.ainvoke(ResearchChallengeState(
            plan=plan,
            supervisor_outcome=supervisor_outcome,
            challenges=[], adjudications=[], quality_alerts=[], supplemental_question_ids=[],
            supplemental_guidance={}, supplemental_packet_ids=[],
            red_complete=False, adjudication_complete=False, supplemental_complete=False,
            recheck_complete=False, result=None,
        ), config=config)
    result = final.get("result")
    if not isinstance(result, ResearchChallengeLoopOutcome):
        raise RuntimeError("Research challenge loop finished without an outcome")
    return _normalize_loop_outcome(result)
