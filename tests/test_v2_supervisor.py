"""Phase 3 tests for bounded Supervisor decisions and checkpoint recovery."""

from __future__ import annotations

from dataclasses import replace

from langgraph.checkpoint.memory import InMemorySaver

import pytest

from src.research.models import (
    AgentLimits,
    ExecutionIdentity,
    OutputStatus,
    ResearchStatus,
    TerminationReason,
)
from src.research.research_supervisor import (
    SupervisorBudget,
    plan_initial_work_packets,
    run_research_supervisor,
)
from src.research.v2_contracts import (
    BlueWorkerResult,
    BlueWorkerUsage,
    CoreQuestion,
    EvidenceClaim,
    ResearchPlan,
    SupervisorV2Config,
)


def _plan(count: int = 3) -> ResearchPlan:
    return ResearchPlan.create(
        0,
        tuple(CoreQuestion.create(f"Required question {index}") for index in range(count)),
        source_guidance=("Use primary sources",),
    )


def _identity(name: str = "root-supervisor") -> ExecutionIdentity:
    return ExecutionIdentity(name, None, name, 0)


def _budget() -> SupervisorBudget:
    return SupervisorBudget(
        total_tool_calls=12,
        total_tokens=120000,
        deadline_at=9999999999.0,
    )


def test_initial_packets_cover_unassigned_required_questions_before_repeats() -> None:
    plan = _plan(5)
    packets, reserve = plan_initial_work_packets(
        plan,
        SupervisorV2Config(enabled=True, max_initial_workers=2),
        _budget(),
    )

    assigned = [question_id for packet in packets for question_id in packet.question_ids]
    required = [item.question_id for item in plan.core_questions if item.required]
    assert sorted(assigned) == sorted(required)
    assert len(assigned) == len(set(assigned))
    assert len(packets) == 2
    assert reserve >= 18000


def _claim_result(packet, question_ids) -> BlueWorkerResult:
    claims = tuple(
        EvidenceClaim.create(
            claim=f"Supported {question_id}",
            question_ids=(question_id,),
            evidence_ids=(f"evidence-{question_id}",),
            source_ref=f"https://example.com/{question_id}",
            locator="section:1",
            excerpt="Source-locatable support",
        )
        for question_id in question_ids
    )
    return BlueWorkerResult(
        packet_id=packet.packet_id,
        status=ResearchStatus.COMPLETED,
        summary="Worker completed",
        claims=claims,
        usage=BlueWorkerUsage(tool_calls=1, estimated_tokens=1000),
        output_status=OutputStatus.VALID,
    )


@pytest.mark.asyncio
async def test_supervisor_workers_are_depth_one_and_completed_checkpoint_is_reused() -> None:
    calls: list[tuple[str, ExecutionIdentity]] = []

    async def runner(packet, plan, policy, tools, **kwargs):
        del plan, policy, tools
        calls.append((packet.packet_id, kwargs["identity"]))
        return _claim_result(packet, packet.question_ids)

    saver = InMemorySaver()
    plan = _plan(3)
    first = await run_research_supervisor(
        plan,
        policy=object(),
        tools=(),
        identity=_identity(),
        limits=AgentLimits(),
        settings=SupervisorV2Config(enabled=True, max_initial_workers=3),
        budget=_budget(),
        checkpointer=saver,
        worker_runner=runner,
    )
    second = await run_research_supervisor(
        plan,
        policy=object(),
        tools=(),
        identity=_identity(),
        limits=AgentLimits(),
        settings=SupervisorV2Config(enabled=True, max_initial_workers=3),
        budget=_budget(),
        checkpointer=saver,
        worker_runner=runner,
    )

    assert second == first
    assert len(calls) == 3
    assert all(item.depth == 1 and item.parent_thread_id == "root-supervisor" for _, item in calls)
    assert first.termination_reason is TerminationReason.COVERAGE_COMPLETE
    assert first.assigned_question_ids == first.resolved_question_ids


@pytest.mark.asyncio
async def test_supervisor_records_unresolved_after_bounded_wave() -> None:
    async def runner(packet, *args, **kwargs):
        del args, kwargs
        return BlueWorkerResult(
            packet_id=packet.packet_id,
            status=ResearchStatus.PARTIAL,
            summary="No usable evidence",
            unresolved=("Source unavailable",),
        )

    plan = _plan(2)
    outcome = await run_research_supervisor(
        plan,
        policy=object(),
        tools=(),
        identity=_identity("root-unresolved"),
        limits=AgentLimits(),
        settings=SupervisorV2Config(
            enabled=True,
            max_initial_workers=2,
            max_research_waves=1,
        ),
        budget=_budget(),
        worker_runner=runner,
    )

    assert set(outcome.unresolved_question_ids) == {
        item.question_id for item in plan.core_questions
    }
    assert outcome.termination_reason is TerminationReason.EVIDENCE_EXHAUSTED


@pytest.mark.asyncio
async def test_runtime_cancellation_cannot_be_overridden_by_supervisor() -> None:
    async def forbidden_runner(*args, **kwargs):
        raise AssertionError("cancelled Supervisor must not run Workers")

    outcome = await run_research_supervisor(
        _plan(1),
        policy=object(),
        tools=(),
        identity=_identity("root-cancelled"),
        limits=AgentLimits(),
        settings=SupervisorV2Config(enabled=True),
        budget=_budget(),
        worker_runner=forbidden_runner,
        user_cancelled=True,
        requested_termination_reason=TerminationReason.COVERAGE_COMPLETE,
    )

    assert outcome.termination_reason is TerminationReason.USER_CANCELLED
    assert outcome.worker_results == ()
