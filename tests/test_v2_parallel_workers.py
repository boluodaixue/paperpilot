"""Phase 3 parallel execution and deterministic merge tests."""

from __future__ import annotations

import asyncio

import pytest

from src.research.models import AgentLimits, ExecutionIdentity, ResearchStatus
from src.research.research_supervisor import SupervisorBudget, run_research_supervisor
from src.research.v2_contracts import (
    BlueWorkerResult,
    CoreQuestion,
    ResearchPlan,
    SupervisorV2Config,
)


@pytest.mark.asyncio
async def test_workers_run_concurrently_and_merge_by_packet_id() -> None:
    plan = ResearchPlan.create(
        0,
        tuple(CoreQuestion.create(f"Question {index}") for index in range(4)),
    )
    active = 0
    max_active = 0

    async def runner(packet, *args, **kwargs):
        nonlocal active, max_active
        del args, kwargs
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02 if packet.question_ids[0].endswith("0") else 0.001)
        active -= 1
        return BlueWorkerResult(
            packet_id=packet.packet_id,
            status=ResearchStatus.PARTIAL,
            summary="bounded result",
            unresolved=("unresolved",),
        )

    outcome = await run_research_supervisor(
        plan,
        policy=object(),
        tools=(),
        identity=ExecutionIdentity("root-parallel", None, "root-parallel", 0),
        limits=AgentLimits(),
        settings=SupervisorV2Config(
            enabled=True,
            max_initial_workers=4,
            max_research_waves=1,
        ),
        budget=SupervisorBudget(20, 200000, 9999999999.0),
        worker_runner=runner,
    )

    assert max_active >= 2
    assert [item.packet_id for item in outcome.worker_results] == sorted(
        item.packet_id for item in outcome.worker_results
    )
