"""Bounded Lead Researcher Supervisor for Research Agent V2.

The two-node ``supervisor_decide`` / ``supervisor_execute`` boundary and the
``ConductResearch`` / ``ResearchComplete`` control semantics are adapted from
LangChain Deep Research From Scratch at commit
``93f35e5d2a51590f9542207a9ff66a01901da5bc``. Packet identity, budgets,
checkpointing, cancellation, Evidence, and deterministic merging are
PaperPilot-specific. See ``THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .models import (
    AgentLimits,
    ExecutionIdentity,
    OutputStatus,
    ResearchStatus,
    TerminationReason,
)
from .research_worker import run_research_worker
from .v2_contracts import (
    BlueWorkerResult,
    ConductResearch,
    EvidenceClaim,
    ResearchComplete,
    ResearchPlan,
    SupervisorOutcome,
    SupervisorV2Config,
    WorkPacket,
)


@dataclass(frozen=True)
class SupervisorBudget:
    """Global research allocation before Lead drafting/citation reserves."""

    total_tool_calls: int
    total_tokens: int
    deadline_at: float

    def validate(self) -> None:
        if self.total_tool_calls < 0 or self.total_tokens < 0:
            raise ValueError("Supervisor budgets cannot be negative")
        if self.deadline_at <= 0:
            raise ValueError("Supervisor deadline must be positive")


class SupervisorState(TypedDict, total=False):
    plan: ResearchPlan
    identity: ExecutionIdentity
    limits: AgentLimits
    settings: SupervisorV2Config
    budget: SupervisorBudget
    worker_results: list[BlueWorkerResult]
    pending_action: ConductResearch | ResearchComplete | None
    assigned_question_ids: list[str]
    supplemental_question_ids: list[str]
    wave_count: int
    finalization_token_reserve: int
    user_cancelled: bool
    requested_termination_reason: TerminationReason | None
    outcome: SupervisorOutcome | None


WorkerRunner = Callable[..., Awaitable[BlueWorkerResult]]


def _split_even(total: int, count: int) -> list[int]:
    if count <= 0:
        return []
    base, extra = divmod(max(0, total), count)
    return [base + (1 if index < extra else 0) for index in range(count)]


def lead_finalization_reserve(total_tokens: int, question_count: int) -> int:
    """Keep the Lead's 15% floor plus a small complexity-aware minimum."""
    if total_tokens <= 0:
        return 0
    return min(
        total_tokens,
        max(
            12000,
            (total_tokens * 15 + 99) // 100,
            2000 * max(1, question_count),
        ),
    )


def _question_groups(question_ids: list[str], worker_count: int) -> list[list[str]]:
    groups = [[] for _ in range(worker_count)]
    for index, question_id in enumerate(question_ids):
        groups[index % worker_count].append(question_id)
    return [group for group in groups if group]


def _packets_for_questions(
    plan: ResearchPlan,
    settings: SupervisorV2Config,
    budget: SupervisorBudget,
    question_ids: Iterable[str],
    *,
    wave: str,
    available_tool_calls: int,
    available_tokens: int,
    guidance_by_question: Mapping[str, Iterable[str]] | None = None,
) -> tuple[WorkPacket, ...]:
    questions = {item.question_id: item for item in plan.core_questions}
    unique_ids = tuple(
        dict.fromkeys(item for item in question_ids if item in questions)
    )
    if not unique_ids:
        return ()
    worker_count = min(settings.max_initial_workers, len(unique_ids))
    groups = _question_groups(list(unique_ids), worker_count)
    tool_shares = _split_even(available_tool_calls, len(groups))
    token_shares = _split_even(available_tokens, len(groups))
    packets: list[WorkPacket] = []
    targeted_guidance = guidance_by_question or {}
    for index, group in enumerate(groups):
        descriptions = [questions[item].description for item in group]
        objective_prefix = "Resolve Red-reviewed gaps" if wave == "supplemental" else "Research"
        targeted_objectives = tuple(
            str(text).strip()
            for question_id in group
            for text in targeted_guidance.get(question_id, ())
            if str(text).strip()
        )
        objective = f"{objective_prefix}: " + " | ".join(descriptions)
        if targeted_objectives:
            objective += " | Targeted Red requirements: " + " | ".join(targeted_objectives)
        packets.append(
            WorkPacket.create(
                objective=objective,
                question_ids=tuple(group),
                expected_output="Source-locatable Evidence Claims and unresolved gaps",
                source_guidance=tuple(dict.fromkeys((*plan.source_guidance, *targeted_objectives))),
                max_tool_calls=tool_shares[index],
                token_budget=token_shares[index],
                deadline_at=budget.deadline_at,
                wave=wave,
            )
        )
    return tuple(packets)


def plan_initial_work_packets(
    plan: ResearchPlan,
    settings: SupervisorV2Config,
    budget: SupervisorBudget,
) -> tuple[tuple[WorkPacket, ...], int]:
    """Assign every required question once before any optional/repeated work."""
    settings.validate()
    budget.validate()
    required_ids = [
        item.question_id for item in plan.core_questions if item.required
    ]
    if not required_ids:
        required_ids = [item.question_id for item in plan.core_questions]
    reserve = lead_finalization_reserve(budget.total_tokens, len(required_ids))
    packets = _packets_for_questions(
        plan,
        settings,
        budget,
        required_ids,
        wave="initial",
        available_tool_calls=budget.total_tool_calls,
        available_tokens=max(0, budget.total_tokens - reserve),
    )
    return packets, reserve


def plan_supplemental_work_packets(
    plan: ResearchPlan,
    settings: SupervisorV2Config,
    budget: SupervisorBudget,
    outcome: SupervisorOutcome,
    question_ids: Iterable[str],
    *,
    guidance_by_question: Mapping[str, Iterable[str]] | None = None,
) -> tuple[WorkPacket, ...]:
    """Allocate one targeted wave without touching the Lead reserve."""
    if outcome.wave_count >= settings.max_research_waves:
        return ()
    used_tools = sum(item.usage.tool_calls for item in outcome.worker_results)
    used_tokens = sum(item.usage.estimated_tokens for item in outcome.worker_results)
    return _packets_for_questions(
        plan,
        settings,
        budget,
        question_ids,
        wave="supplemental",
        available_tool_calls=max(0, budget.total_tool_calls - used_tools),
        available_tokens=max(
            0,
            budget.total_tokens - outcome.finalization_token_reserve - used_tokens,
        ),
        guidance_by_question=guidance_by_question,
    )


def _worker_identity(root: ExecutionIdentity, packet: WorkPacket) -> ExecutionIdentity:
    return ExecutionIdentity(
        thread_id=f"{root.thread_id}.worker.{packet.packet_id.split('-', 1)[-1]}",
        parent_thread_id=root.thread_id,
        root_thread_id=root.root_thread_id,
        depth=1,
    )


worker_identity_for_packet = _worker_identity


def _resolved_question_ids(results: Iterable[BlueWorkerResult]) -> set[str]:
    return {
        question_id
        for result in results
        for claim in result.claims
        for question_id in claim.question_ids
    }


def _normalized_worker_result(result: BlueWorkerResult) -> BlueWorkerResult:
    claims = tuple(
        EvidenceClaim.create(
            claim=item.claim,
            question_ids=tuple(item.question_ids),
            evidence_ids=tuple(item.evidence_ids),
            source_ref=item.source_ref,
            locator=item.locator,
            excerpt=item.excerpt,
            limitations=item.limitations,
            confidence=item.confidence,
            comparability_notes=item.comparability_notes,
        )
        for item in result.claims
    )
    return BlueWorkerResult(
        packet_id=result.packet_id,
        status=result.status,
        summary=result.summary,
        claims=claims,
        evidence=tuple(result.evidence),
        unresolved=tuple(result.unresolved),
        alerts=tuple(result.alerts),
        usage=result.usage,
        termination_reason=result.termination_reason,
        output_status=result.output_status,
    )


def _build_outcome(
    state: SupervisorState,
    *,
    termination_reason: TerminationReason | None,
) -> SupervisorOutcome:
    plan = state["plan"]
    results = tuple(
        sorted(
            (_normalized_worker_result(item) for item in state.get("worker_results", [])),
            key=lambda item: item.packet_id,
        )
    )
    required = tuple(
        item.question_id for item in plan.core_questions if item.required
    ) or tuple(item.question_id for item in plan.core_questions)
    resolved_set = _resolved_question_ids(results)
    assigned_set = set(state.get("assigned_question_ids", []))
    return SupervisorOutcome(
        plan_id=plan.plan_id,
        worker_results=results,
        assigned_question_ids=tuple(item for item in required if item in assigned_set),
        resolved_question_ids=tuple(item for item in required if item in resolved_set),
        unresolved_question_ids=tuple(item for item in required if item not in resolved_set),
        wave_count=int(state.get("wave_count", 0)),
        finalization_token_reserve=int(state.get("finalization_token_reserve", 0)),
        termination_reason=termination_reason,
    )


def build_research_supervisor_graph(
    *,
    policy: Any,
    tools: Iterable[Any],
    checkpointer: BaseCheckpointSaver | None = None,
    worker_runner: WorkerRunner = run_research_worker,
    worker_checkpointer: BaseCheckpointSaver | None = None,
    tool_artifact_store: Any | None = None,
) -> Any:
    """Build the checkpointed two-node Supervisor graph."""
    effective_checkpointer = checkpointer or InMemorySaver()
    effective_worker_checkpointer = worker_checkpointer or effective_checkpointer
    tool_list = tuple(tools)

    def supervisor_decide(state: SupervisorState) -> dict[str, Any]:
        if state.get("user_cancelled"):
            outcome = _build_outcome(
                state,
                termination_reason=TerminationReason.USER_CANCELLED,
            )
            return {
                "pending_action": ResearchComplete(
                    "user_cancelled",
                    TerminationReason.USER_CANCELLED,
                ),
                "outcome": outcome,
            }
        if state.get("outcome") is not None:
            return {}

        results = tuple(state.get("worker_results", []))
        if not results and state.get("wave_count", 0) == 0:
            packets, reserve = plan_initial_work_packets(
                state["plan"], state["settings"], state["budget"]
            )
            if not packets:
                outcome = _build_outcome(
                    {**state, "finalization_token_reserve": reserve},
                    termination_reason=TerminationReason.BUDGET_FORCED,
                )
                return {
                    "finalization_token_reserve": reserve,
                    "pending_action": ResearchComplete(
                        "budget_forced", TerminationReason.BUDGET_FORCED
                    ),
                    "outcome": outcome,
                }
            return {
                "finalization_token_reserve": reserve,
                "pending_action": ConductResearch(packets, "initial"),
            }

        supplemental = tuple(state.get("supplemental_question_ids", []))
        if supplemental and state.get("wave_count", 0) < state["settings"].max_research_waves:
            used_tools = sum(item.usage.tool_calls for item in results)
            used_tokens = sum(item.usage.estimated_tokens for item in results)
            reserve = int(state.get("finalization_token_reserve", 0))
            packets = _packets_for_questions(
                state["plan"],
                state["settings"],
                state["budget"],
                supplemental,
                wave="supplemental",
                available_tool_calls=max(0, state["budget"].total_tool_calls - used_tools),
                available_tokens=max(
                    0,
                    state["budget"].total_tokens - reserve - used_tokens,
                ),
            )
            existing_ids = {item.packet_id for item in results}
            packets = tuple(item for item in packets if item.packet_id not in existing_ids)
            if packets:
                return {
                    "supplemental_question_ids": [],
                    "pending_action": ConductResearch(packets, "supplemental"),
                }

        provisional = _build_outcome(state, termination_reason=None)
        if not provisional.unresolved_question_ids:
            reason = TerminationReason.COVERAGE_COMPLETE
        elif state.get("wave_count", 0) >= state["settings"].max_research_waves:
            reason = TerminationReason.EVIDENCE_EXHAUSTED
        else:
            reason = None
        outcome = _build_outcome(state, termination_reason=reason)
        return {
            "pending_action": ResearchComplete("ready_for_red_review", reason),
            "outcome": outcome,
        }

    async def supervisor_execute(state: SupervisorState) -> dict[str, Any]:
        action = state.get("pending_action")
        if not isinstance(action, ConductResearch):
            return {}
        completed_ids = {item.packet_id for item in state.get("worker_results", [])}
        packets = tuple(item for item in action.packets if item.packet_id not in completed_ids)

        async def run_one(packet: WorkPacket) -> BlueWorkerResult:
            try:
                return await worker_runner(
                    packet,
                    state["plan"],
                    policy,
                    tool_list,
                    identity=_worker_identity(state["identity"], packet),
                    limits=state["limits"],
                    checkpointer=effective_worker_checkpointer,
                    tool_artifact_store=tool_artifact_store,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return BlueWorkerResult(
                    packet_id=packet.packet_id,
                    status=ResearchStatus.FAILED,
                    summary="Blue Worker failed before producing usable evidence.",
                    unresolved=(f"{type(exc).__name__}: {exc}",),
                    output_status=OutputStatus.FALLBACK,
                )

        new_results = await asyncio.gather(*(run_one(packet) for packet in packets))
        merged = sorted(
            [*state.get("worker_results", []), *new_results],
            key=lambda item: item.packet_id,
        )
        assigned = list(state.get("assigned_question_ids", []))
        for packet in packets:
            assigned.extend(item for item in packet.question_ids if item not in assigned)
        return {
            "worker_results": merged,
            "assigned_question_ids": assigned,
            "wave_count": state.get("wave_count", 0) + (1 if packets else 0),
            "pending_action": None,
        }

    def route_after_decide(state: SupervisorState) -> str:
        return (
            "supervisor_execute"
            if isinstance(state.get("pending_action"), ConductResearch)
            else "done"
        )

    builder = StateGraph(SupervisorState)
    builder.add_node("supervisor_decide", supervisor_decide)
    builder.add_node("supervisor_execute", supervisor_execute)
    builder.add_edge(START, "supervisor_decide")
    builder.add_conditional_edges(
        "supervisor_decide",
        route_after_decide,
        {"supervisor_execute": "supervisor_execute", "done": END},
    )
    builder.add_edge("supervisor_execute", "supervisor_decide")
    return builder.compile(checkpointer=effective_checkpointer)


async def run_research_supervisor(
    plan: ResearchPlan,
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
    supplemental_question_ids: Iterable[str] = (),
    user_cancelled: bool = False,
    requested_termination_reason: TerminationReason | None = None,
    checkpoint_thread_id: str | None = None,
) -> SupervisorOutcome:
    """Start/resume the Supervisor and return its deterministic merged outcome."""
    identity.validate()
    if identity.depth != 0:
        raise ValueError("Supervisor requires a root identity")
    settings.validate()
    budget.validate()
    graph = build_research_supervisor_graph(
        policy=policy,
        tools=tools,
        checkpointer=checkpointer,
        worker_runner=worker_runner,
        worker_checkpointer=checkpointer,
        tool_artifact_store=tool_artifact_store,
    )
    checkpoint_key = str(checkpoint_thread_id or identity.thread_id).strip()
    if not checkpoint_key:
        raise ValueError("Supervisor checkpoint thread ID cannot be empty")
    config = {"configurable": {"thread_id": checkpoint_key}}
    snapshot = await graph.aget_state(config)
    if snapshot.values:
        saved_plan = snapshot.values.get("plan")
        if not isinstance(saved_plan, ResearchPlan) or saved_plan.plan_id != plan.plan_id:
            raise ValueError("Supervisor checkpoint belongs to another ResearchPlan")
        saved = snapshot.values.get("outcome")
        if isinstance(saved, SupervisorOutcome):
            return _build_outcome(
                snapshot.values,
                termination_reason=saved.termination_reason,
            )
        final = await graph.ainvoke(None, config=config)
    else:
        reserve = lead_finalization_reserve(
            budget.total_tokens,
            len(tuple(item for item in plan.core_questions if item.required)),
        )
        final = await graph.ainvoke(
            SupervisorState(
                plan=plan,
                identity=identity,
                limits=limits,
                settings=settings,
                budget=budget,
                worker_results=[],
                pending_action=None,
                assigned_question_ids=[],
                supplemental_question_ids=list(dict.fromkeys(supplemental_question_ids)),
                wave_count=0,
                finalization_token_reserve=reserve,
                user_cancelled=user_cancelled,
                requested_termination_reason=requested_termination_reason,
                outcome=None,
            ),
            config=config,
        )
    outcome = final.get("outcome")
    if not isinstance(outcome, SupervisorOutcome):
        raise RuntimeError("Supervisor finished without an outcome")
    return _build_outcome(final, termination_reason=outcome.termination_reason)
