"""Headless Research Core contracts and entry point.

This module intentionally knows nothing about product sessions, Memory IDs,
Markdown Vaults, Obsidian, Web routes, or Rubric judges.  Product and evaluation
adapters may construct a request and consume the result, but dependencies never
point back out from this module.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from langgraph.checkpoint.base import BaseCheckpointSaver

from .agent_graph import run_research_agent
from .models import (
    AgentLimits,
    EvidenceItem,
    ExecutionIdentity,
    OutputStatus,
    ResearchResult,
    ResearchStatus,
    ResearchTask,
    TerminationReason,
)
from .research_blackboard import ResearchBlackboard
from .research_control import HomogeneousForkConfig


@dataclass(frozen=True)
class PriorEvidence:
    """Generic prior Evidence accepted by Research Core from any adapter."""

    evidence_id: str
    finding: str
    source_ref: str
    title: str = ""
    source_type: str = "prior"
    locator: str = ""
    excerpt: str = ""
    limitations: str = ""
    requirement_id: str = ""
    provenance: str = "external_context"

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("PriorEvidence.evidence_id cannot be empty")
        if not self.finding.strip():
            raise ValueError("PriorEvidence.finding cannot be empty")
        if not self.source_ref.strip():
            raise ValueError("PriorEvidence.source_ref cannot be empty")

    def as_evidence_item(self) -> EvidenceItem:
        return EvidenceItem(
            evidence_id=self.evidence_id.strip(),
            finding=self.finding.strip(),
            source_type=self.source_type.strip() or "prior",
            title=self.title.strip(),
            source_ref=self.source_ref.strip(),
            locator=self.locator.strip(),
            excerpt=self.excerpt.strip(),
            limitations=self.limitations.strip(),
            requirement_id=self.requirement_id.strip(),
        )

    def prompt_view(self) -> dict[str, str]:
        """Return bounded provenance without any product implementation identity."""

        return {
            "evidence_id": self.evidence_id.strip(),
            "finding": self.finding.strip(),
            "source_ref": self.source_ref.strip(),
            "title": self.title.strip(),
            "requirement_id": self.requirement_id.strip(),
            "provenance": self.provenance.strip() or "external_context",
        }


@dataclass(frozen=True)
class PriorEvidenceBundle:
    """Immutable adapter-neutral Evidence supplied before research starts."""

    items: tuple[PriorEvidence, ...] = ()

    def __post_init__(self) -> None:
        ids = [item.evidence_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("PriorEvidenceBundle contains duplicate evidence IDs")


@dataclass(frozen=True)
class CoreResearchRequest:
    """The complete public input contract for one headless research run."""

    objective: str
    scope: tuple[str, ...] = ()
    directions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    expected_output: str = "Evidence-backed Markdown research report"
    prior_evidence: PriorEvidenceBundle = PriorEvidenceBundle()
    require_evidence: bool = True
    run_id: str = ""

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("CoreResearchRequest.objective cannot be empty")
        if not self.expected_output.strip():
            raise ValueError("CoreResearchRequest.expected_output cannot be empty")
        for name in ("scope", "directions", "constraints"):
            values = getattr(self, name)
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError(f"CoreResearchRequest.{name} contains an empty item")


@dataclass(frozen=True)
class CoreResearchResult:
    """Persistence-neutral result returned to Web, CLI, or evaluation adapters."""

    run_id: str
    report_markdown: str
    status: ResearchStatus
    termination_reason: TerminationReason | None
    output_status: OutputStatus
    evidence: tuple[EvidenceItem, ...]
    unresolved: tuple[str, ...]
    thread_count: int
    tool_calls_used: int
    estimated_tokens_used: int
    research_result: ResearchResult


def _clean_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in values if item.strip()))


def _core_task(request: CoreResearchRequest, run_id: str) -> ResearchTask:
    directions = _clean_unique((*request.directions, *request.scope))
    requirement_rows = [
        {
            "requirement_id": f"R{index}",
            "description": description,
            "required": True,
            "requires_external_evidence": request.require_evidence,
        }
        for index, description in enumerate(directions or (request.objective,), 1)
    ]
    context = {
        "original_question": request.objective,
        "scope": list(request.scope),
        "directions": list(request.directions),
        "research_requirements": requirement_rows,
        "prior_evidence": [item.prompt_view() for item in request.prior_evidence.items],
    }
    return ResearchTask(
        task_id=f"core-task-{run_id}",
        objective=request.objective.strip(),
        context=context,
        expected_output=request.expected_output.strip(),
        constraints=_clean_unique(request.constraints),
        require_evidence=request.require_evidence,
    )


async def run_core_research(
    request: CoreResearchRequest,
    policy: Any,
    tools: Iterable[Any] = (),
    *,
    limits: AgentLimits | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    coordination_board: ResearchBlackboard | None = None,
    homogeneous_fork_config: HomogeneousForkConfig | None = None,
    tool_artifact_store: Any | None = None,
) -> CoreResearchResult:
    """Run Research Core without alignment, Memory retrieval, or persistence."""

    run_id = request.run_id.strip() or f"core-{uuid.uuid4().hex}"
    identity = ExecutionIdentity(run_id, None, run_id, 0)
    result = await run_research_agent(
        _core_task(request, run_id),
        policy,
        tools,
        identity=identity,
        limits=limits,
        checkpointer=checkpointer,
        tool_artifact_store=tool_artifact_store,
        coordination_board=coordination_board,
        homogeneous_fork_config=homogeneous_fork_config,
        initial_evidence=(
            item.as_evidence_item() for item in request.prior_evidence.items
        ),
    )
    report = result.report_markdown.strip()
    if not report:
        report = result.research_memo.strip() or result.summary.strip()
    return CoreResearchResult(
        run_id=run_id,
        report_markdown=report,
        status=result.status,
        termination_reason=result.termination_reason,
        output_status=result.output_status,
        evidence=result.evidence,
        unresolved=result.unresolved,
        thread_count=result.thread_count,
        tool_calls_used=result.tool_calls_used,
        estimated_tokens_used=result.estimated_tokens_used,
        research_result=result,
    )


__all__ = [
    "CoreResearchRequest",
    "CoreResearchResult",
    "PriorEvidence",
    "PriorEvidenceBundle",
    "run_core_research",
]
