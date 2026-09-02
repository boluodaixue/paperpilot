"""Phase-3 assembly boundary for the V2 Planner and Supervisor core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from langgraph.checkpoint.base import BaseCheckpointSaver

from .models import AgentLimits, ExecutionIdentity, ResearchBrief
from .research_planner import plan_research
from .research_supervisor import SupervisorBudget, run_research_supervisor
from .v2_contracts import ResearchPlan, SupervisorOutcome, SupervisorV2Config


@dataclass(frozen=True)
class ResearchV2CoreResult:
    plan: ResearchPlan
    supervisor: SupervisorOutcome


async def run_research_v2_core(
    brief: ResearchBrief,
    *,
    policy: Any,
    tools: Iterable[Any],
    identity: ExecutionIdentity,
    limits: AgentLimits,
    settings: SupervisorV2Config,
    budget: SupervisorBudget,
    checkpointer: BaseCheckpointSaver | None = None,
    tool_artifact_store: Any | None = None,
) -> ResearchV2CoreResult:
    """Run the no-tool Planner then the bounded Supervisor research stage."""
    plan = await plan_research(brief, policy)
    supervisor = await run_research_supervisor(
        plan,
        policy=policy,
        tools=tools,
        identity=identity,
        limits=limits,
        settings=settings,
        budget=budget,
        checkpointer=checkpointer,
        tool_artifact_store=tool_artifact_store,
    )
    return ResearchV2CoreResult(plan, supervisor)
