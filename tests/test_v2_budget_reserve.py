"""Phase 3 budget allocation tests for Lead finalization safety."""

from __future__ import annotations

from src.research.research_supervisor import SupervisorBudget, plan_initial_work_packets
from src.research.v2_contracts import CoreQuestion, ResearchPlan, SupervisorV2Config


def test_worker_allocation_preserves_lead_finalization_tokens_and_is_fair() -> None:
    plan = ResearchPlan.create(
        0,
        tuple(CoreQuestion.create(f"Question {index}") for index in range(4)),
    )
    budget = SupervisorBudget(
        total_tool_calls=11,
        total_tokens=100000,
        deadline_at=9999999999.0,
    )

    packets, reserve = plan_initial_work_packets(
        plan,
        SupervisorV2Config(enabled=True, max_initial_workers=4),
        budget,
    )

    assert reserve >= 15000
    assert sum(item.token_budget for item in packets) <= budget.total_tokens - reserve
    assert sum(item.max_tool_calls for item in packets) <= budget.total_tool_calls
    assert max(item.token_budget for item in packets) - min(
        item.token_budget for item in packets
    ) <= 1
    assert max(item.max_tool_calls for item in packets) - min(
        item.max_tool_calls for item in packets
    ) <= 1
