"""One-layer, non-recursive Blue Research Worker for Research Agent V2.

The node boundary follows the no-fork worker shape in LangChain's
``deep_research_from_scratch`` at commit
``93f35e5d2a51590f9542207a9ff66a01901da5bc``. Execution reuses PaperPilot's
existing AgentGraph nodes so tool availability, action binding, artifact
offload, Evidence extraction, budgets, checkpoint recovery, and context
compaction remain one implementation. See ``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import replace
from typing import Any, Iterable, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver

from .agent_graph import (
    ResearchAgentState,
    build_research_agent_graph,
    create_research_agent_state,
)
from .research_blackboard import ResearchBlackboard
from .claim_hygiene import derive_atomic_claim
from .evidence_claim_pipeline import (
    deterministic_verify_candidate_claims,
    extract_candidate_claims,
    narrow_partially_entailed_candidates,
    verified_evidence_claims,
    verify_candidate_claims,
)
from .models import (
    AgentLimits,
    EvidenceItem,
    ExecutionIdentity,
    ResearchResult,
    ResearchStatus,
    ResearchTask,
)
from .v2_contracts import (
    BlueWorkerResult,
    BlueWorkerUsage,
    EvidenceClaim,
    ResearchPlan,
    WorkPacket,
)

_FORBIDDEN_WORKER_STATE_FIELDS = {
    "pending_fork_calls",
    "completed_fork_fingerprints",
    "child_thread_ids",
    "child_results",
}
ResearchWorkerState = TypedDict(
    "ResearchWorkerState",
    {
        key: value
        for key, value in ResearchAgentState.__annotations__.items()
        if key not in _FORBIDDEN_WORKER_STATE_FIELDS
    },
    total=False,
)

__all__ = [
    "ResearchWorkerState",
    "build_research_worker_graph",
    "create_research_worker_state",
    "run_research_worker",
]


def _worker_limits(packet: WorkPacket, limits: AgentLimits) -> AgentLimits:
    remaining_seconds = max(0.001, packet.deadline_at - time.time())
    worker = replace(
        limits,
        max_tool_calls=min(limits.max_tool_calls, packet.max_tool_calls),
        max_children=0,
        max_fork_depth=1,
        max_total_threads=1,
        max_total_tool_calls=min(limits.max_total_tool_calls, packet.max_tool_calls),
        max_elapsed_seconds=min(limits.max_elapsed_seconds, remaining_seconds),
        max_total_tokens=min(limits.max_total_tokens, packet.token_budget),
    )
    worker.validate()
    return worker


def _validate_assignment(
    packet: WorkPacket,
    plan: ResearchPlan,
    identity: ExecutionIdentity,
) -> dict[str, Any]:
    identity.validate()
    if identity.depth != 1:
        raise ValueError("Blue Worker identity must have depth=1")
    questions = {item.question_id: item for item in plan.core_questions}
    if any(item not in questions for item in packet.question_ids):
        raise ValueError("WorkPacket references unknown Core Question IDs")
    return questions


def create_research_worker_state(
    packet: WorkPacket,
    plan: ResearchPlan,
    identity: ExecutionIdentity,
    limits: AgentLimits,
) -> ResearchWorkerState:
    """Adapt one WorkPacket to the shared loop without recursive state fields."""
    questions = _validate_assignment(packet, plan, identity)
    worker_limits = _worker_limits(packet, limits)
    task = ResearchTask(
        task_id=packet.packet_id,
        objective=packet.objective,
        context={
            "research_plan_id": plan.plan_id,
            "work_packet_id": packet.packet_id,
            "wave": packet.wave,
            "parent_requirement_ids": list(packet.question_ids),
            "coordination_requirements": [
                {
                    "requirement_id": question_id,
                    "description": questions[question_id].description,
                    "required": questions[question_id].required,
                }
                for question_id in packet.question_ids
            ],
            "research_requirements": [
                {
                    "requirement_id": question_id,
                    "description": questions[question_id].description,
                    "required": questions[question_id].required,
                }
                for question_id in packet.question_ids
            ],
            "directions": [
                questions[question_id].description for question_id in packet.question_ids
            ],
        },
        expected_output=packet.expected_output,
        constraints=packet.source_guidance,
        require_evidence=True,
    )
    shared_state = dict(
        create_research_agent_state(
            task,
            identity,
            worker_limits,
            deadline_at=packet.deadline_at,
            subtree_thread_budget=1,
            subtree_tool_budget=worker_limits.max_total_tool_calls,
            subtree_token_budget=worker_limits.max_total_tokens,
            subtree_retry_budget=worker_limits.max_total_retries,
            lineage_objectives=[packet.objective],
        )
    )
    for field in _FORBIDDEN_WORKER_STATE_FIELDS:
        shared_state.pop(field, None)
    return ResearchWorkerState(**shared_state)


def _isolated_policy(policy: Any) -> Any:
    factory = getattr(policy, "fork", None)
    if callable(factory):
        isolated = factory()
        if isolated is not policy:
            return isolated
    try:
        return copy.deepcopy(policy)
    except Exception:
        # Stateless function policies are safe to share; stateful production
        # adapters expose ``fork`` and are handled above.
        return policy


def _isolated_tools(tools: Iterable[Any]) -> list[Any]:
    isolated: list[Any] = []
    for tool in tools:
        factory = getattr(tool, "fork", None) or getattr(tool, "clone", None)
        if callable(factory):
            candidate = factory()
        else:
            try:
                candidate = copy.deepcopy(tool)
            except Exception as exc:
                raise TypeError(
                    f"research tool {getattr(tool, 'name', tool)!r} cannot be isolated"
                ) from exc
        isolated.append(candidate)
    return isolated


def build_research_worker_graph(
    policy: Any,
    tools: Iterable[Any] = (),
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    tool_artifact_store: Any | None = None,
    coordination_board: ResearchBlackboard | None = None,
) -> Any:
    """Compile the shared research loop with Worker state and no fork node."""
    return build_research_agent_graph(
        policy,
        tools,
        checkpointer=checkpointer,
        child_checkpointer=checkpointer,
        tool_artifact_store=tool_artifact_store,
        coordination_board=coordination_board,
        state_schema=ResearchWorkerState,
        allow_fork_tool=False,
    )


def _strong_worker_evidence(
    evidence: Iterable[EvidenceItem],
    packet: WorkPacket,
    plan: ResearchPlan | None = None,
) -> tuple[EvidenceItem, ...]:
    allowed_requirements = set(packet.question_ids)
    anchor_text = " ".join((packet.objective, *packet.source_guidance))
    anchors = {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}", anchor_text)
        if (
            any(character.isdigit() or character in ".-+" for character in token)
            or (token.isupper() and len(token) >= 3)
        )
        if token.casefold() not in {
            "research", "objective", "original", "question", "compare", "analysis",
            "analyze", "model", "models", "performance", "technical", "report",
            "source", "sources", "official", "benchmark", "benchmarks", "generation",
            "context", "task", "tasks", "evidence", "directly", "assigned", "topic",
            "generic", "title", "matches", "noise", "with", "from", "that", "this",
            "use", "prefer", "reject", "primary", "opened", "paperpilot", "deliverable",
        }
    }

    def relevant(item: EvidenceItem) -> bool:
        content = " ".join((
            item.finding, item.title, item.source_ref, item.excerpt,
        )).casefold()
        if content.startswith(("[browser error]", "[browser warning]", "[tool error]")):
            return False
        return not anchors or any(anchor in content for anchor in anchors)

    eligible = tuple(
        item
        for item in evidence
        if item.requirement_id in allowed_requirements
        and item.action_id
        and item.artifact_id
        and item.source_ref
        and item.locator
        and item.excerpt
        and not item.limitations.startswith("Search-result snippet")
        and relevant(item)
    )
    selected: list[EvidenceItem] = []
    per_requirement: dict[str, int] = {}
    per_source: dict[str, int] = {}
    for item in eligible:
        if per_requirement.get(item.requirement_id, 0) >= 6:
            continue
        if per_source.get(item.source_ref, 0) >= 2:
            continue
        selected.append(item)
        per_requirement[item.requirement_id] = per_requirement.get(item.requirement_id, 0) + 1
        per_source[item.source_ref] = per_source.get(item.source_ref, 0) + 1
        if len(selected) >= 18:
            break
    return tuple(selected)


def _worker_result(
    packet: WorkPacket,
    result: ResearchResult,
    *,
    plan: ResearchPlan | None = None,
    preserve_full_evidence: bool = False,
) -> BlueWorkerResult:
    evidence = (
        tuple(result.evidence)
        if preserve_full_evidence
        else _strong_worker_evidence(result.evidence, packet, plan)
    )
    claims = tuple(
        EvidenceClaim.create(
            claim=clean,
            question_ids=(item.requirement_id,),
            evidence_ids=(item.evidence_id,),
            source_ref=item.source_ref,
            locator=item.locator,
            excerpt=item.excerpt,
            limitations=item.limitations,
            confidence="medium",
            comparability_notes="",
            verification_status="unverified",
        )
        for item in evidence
        if (clean := derive_atomic_claim(
            item.finding,
            question_context=packet.objective,
            limit=800,
        ))
    )
    status = result.status
    unresolved = list(result.unresolved)
    if not evidence and status is ResearchStatus.COMPLETED:
        status = ResearchStatus.PARTIAL
        unresolved.append("No opened, source-locatable Evidence Claim was collected.")
    return BlueWorkerResult(
        packet_id=packet.packet_id,
        status=status,
        summary=result.summary,
        claims=claims,
        evidence=evidence,
        unresolved=tuple(dict.fromkeys(unresolved)),
        alerts=tuple(result.tool_alerts),
        usage=BlueWorkerUsage(
            iterations=result.iterations,
            tool_calls=result.tool_calls_used,
            estimated_tokens=result.estimated_tokens_used,
            retries=result.retries_used,
            source_candidates=result.source_candidate_count,
            sources_opened=result.source_open_count,
            duplicate_sources=result.duplicate_source_count,
            acquisition_calls=result.acquisition_call_count,
        ),
        termination_reason=result.termination_reason,
        output_status=result.output_status,
    )


async def _verified_worker_result(
    packet: WorkPacket,
    plan: ResearchPlan,
    result: ResearchResult,
    policy: Any,
    *,
    preserve_full_evidence: bool,
) -> BlueWorkerResult:
    packaged = _worker_result(
        packet,
        result,
        plan=plan,
        preserve_full_evidence=True,
    )
    documents, passages, candidates = extract_candidate_claims(
        packet,
        plan,
        packaged.evidence,
    )
    packaged = replace(
        packaged,
        documents=documents,
        passages=passages,
        candidate_claims=candidates,
        support_assessments=(),
    )
    assessments = await verify_candidate_claims(
        policy,
        packaged.candidate_claims,
        packaged.passages,
        plan.evidence_requirements,
    )
    narrowed = narrow_partially_entailed_candidates(
        packaged.candidate_claims,
        assessments,
        packaged.passages,
    )
    narrowed_assessments = (
        await verify_candidate_claims(
            policy,
            narrowed,
            packaged.passages,
            plan.evidence_requirements,
        )
        if narrowed else ()
    )
    candidate_claims = (*packaged.candidate_claims, *narrowed)
    assessments = (*assessments, *narrowed_assessments)
    claims = verified_evidence_claims(
        packaged.evidence,
        packaged.documents,
        packaged.passages,
        candidate_claims,
        assessments,
    )
    unresolved = [
        item
        for item in packaged.unresolved
        if not (
            claims
            and item == "No independently verifiable Passage-to-Claim support was collected."
        )
    ]
    rejected = len(candidate_claims) - len(claims)
    if rejected:
        unresolved.append(
            f"Support verification rejected or limited {rejected} Candidate Claim(s)."
        )
    status = result.status
    if not claims and status is ResearchStatus.COMPLETED:
        status = ResearchStatus.PARTIAL
    return replace(
        packaged,
        claims=claims,
        candidate_claims=candidate_claims,
        support_assessments=assessments,
        unresolved=tuple(dict.fromkeys(unresolved)),
        status=status,
    )


async def run_research_worker(
    packet: WorkPacket,
    plan: ResearchPlan,
    policy: Any,
    tools: Iterable[Any] = (),
    *,
    identity: ExecutionIdentity,
    limits: AgentLimits,
    checkpointer: BaseCheckpointSaver | None = None,
    tool_artifact_store: Any | None = None,
    coordination_board: ResearchBlackboard | None = None,
    preserve_full_evidence: bool = False,
) -> BlueWorkerResult:
    """Start or resume one isolated Worker without repeating completed actions."""
    isolated_policy = _isolated_policy(policy)
    isolated_tools = _isolated_tools(tools)
    graph = build_research_worker_graph(
        isolated_policy,
        isolated_tools,
        checkpointer=checkpointer,
        tool_artifact_store=tool_artifact_store,
        coordination_board=coordination_board,
    )
    config = {"configurable": {"thread_id": identity.thread_id}}
    snapshot = await graph.aget_state(config)
    initial = create_research_worker_state(packet, plan, identity, limits)
    if snapshot.values:
        saved_task = snapshot.values.get("task")
        if (
            not isinstance(saved_task, ResearchTask)
            or saved_task.task_id != packet.packet_id
            or saved_task.context.get("research_plan_id") != plan.plan_id
            or snapshot.values.get("identity") != identity
        ):
            raise ValueError("checkpoint thread belongs to another WorkPacket")
        saved_result = snapshot.values.get("result")
        if isinstance(saved_result, ResearchResult):
            return _worker_result(
                packet,
                saved_result,
                plan=plan,
                preserve_full_evidence=preserve_full_evidence,
            )
        final_state = await graph.ainvoke(None, config=config)
    else:
        final_state = await graph.ainvoke(initial, config=config)
    result = final_state.get("result")
    if not isinstance(result, ResearchResult):
        raise RuntimeError("Blue Worker finished without a ResearchResult")
    return _worker_result(
        packet,
        result,
        plan=plan,
        preserve_full_evidence=preserve_full_evidence,
    )
