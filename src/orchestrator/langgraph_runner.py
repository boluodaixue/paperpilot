"""Phase 1 minimal LangGraph entry for the legacy research workflow.

The graph deliberately keeps the existing :class:`Orchestrator` as the owner
of Manager and Planner behaviour.  LangGraph only provides serial state
transitions, execution identity, checkpointing, and result hand-off here.
"""
from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .orchestrator import Orchestrator
from .schemas import ResearchReport, RunConfig
from ..utils.tracing import trace_block, trace_context


__all__ = [
    "ResearchGraphState",
    "build_research_graph",
    "run_research_graph",
]


class ResearchGraphState(TypedDict):
    """The fields read or written by the Phase 1 single-thread graph."""

    query: str
    thread_id: str
    parent_thread_id: str | None
    root_thread_id: str
    report: ResearchReport | None


def _validate_root_identity(
    state: ResearchGraphState,
    config: RunnableConfig,
) -> None:
    """Require one canonical root identity in state and checkpoint config."""
    thread_id = state.get("thread_id")
    root_thread_id = state.get("root_thread_id")
    parent_thread_id = state.get("parent_thread_id")
    checkpoint_thread_id = config.get("configurable", {}).get("thread_id")

    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("thread_id must be a non-empty string")
    if root_thread_id != thread_id:
        raise ValueError("Phase 1 root execution requires root_thread_id == thread_id")
    if parent_thread_id is not None:
        raise ValueError("Phase 1 root execution requires parent_thread_id is None")
    if checkpoint_thread_id != thread_id:
        raise ValueError(
            "LangGraph configurable.thread_id must match the state thread_id"
        )


def _identity_metadata(state: ResearchGraphState) -> dict[str, Any]:
    return {
        "thread_id": state["thread_id"],
        "parent_thread_id": state["parent_thread_id"],
        "root_thread_id": state["root_thread_id"],
    }


def build_research_graph(
    orchestrator: Orchestrator,
    *,
    run_config: RunConfig | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> Any:
    """Build the Phase 1 serial graph around the existing Orchestrator.

    ``InMemorySaver`` is intentionally the default for this minimal phase.
    Callers that need to inspect snapshots may inject and retain their own
    checkpointer instance.
    """
    effective_run_config = run_config or RunConfig()

    def manager_prepare(
        state: ResearchGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_root_identity(state, config)
        metadata = _identity_metadata(state)
        with trace_context(
            session_id=state["root_thread_id"],
            trace_name="paperpilot.research.graph",
            tags=["paperpilot", "langgraph", "manager"],
            metadata=metadata,
        ):
            with trace_block(
                "langgraph.manager_prepare",
                run_type="chain",
                inputs=metadata,
                tags=["paperpilot", "langgraph", "manager"],
            ) as observation:
                observation.add_output({"identity_valid": True})
        return {}

    async def legacy_research(
        state: ResearchGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_root_identity(state, config)
        metadata = _identity_metadata(state)
        with trace_context(
            session_id=state["root_thread_id"],
            trace_name="paperpilot.research.graph",
            tags=["paperpilot", "langgraph", "legacy-research"],
            metadata=metadata,
        ):
            with trace_block(
                "langgraph.legacy_research",
                run_type="chain",
                inputs={"query": state["query"], **metadata},
                tags=["paperpilot", "langgraph", "legacy-research"],
            ) as observation:
                report = await orchestrator.run(
                    state["query"],
                    config=effective_run_config,
                )
                observation.add_output(
                    {
                        "report_type": type(report).__name__,
                        "confidence": report.confidence,
                    }
                )
        return {"report": report}

    def manager_complete(
        state: ResearchGraphState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        _validate_root_identity(state, config)
        report = state.get("report")
        if not isinstance(report, ResearchReport):
            raise TypeError("legacy_research must return an existing ResearchReport")

        metadata = _identity_metadata(state)
        with trace_context(
            session_id=state["root_thread_id"],
            trace_name="paperpilot.research.graph",
            tags=["paperpilot", "langgraph", "manager"],
            metadata=metadata,
        ):
            with trace_block(
                "langgraph.manager_complete",
                run_type="chain",
                inputs=metadata,
                tags=["paperpilot", "langgraph", "manager"],
            ) as observation:
                observation.add_output(
                    {
                        "report_type": type(report).__name__,
                        "confidence": report.confidence,
                    }
                )
        return {"report": report}

    builder = StateGraph(ResearchGraphState)
    builder.add_node("manager_prepare", manager_prepare)
    builder.add_node("legacy_research", legacy_research)
    builder.add_node("manager_complete", manager_complete)
    builder.add_edge(START, "manager_prepare")
    builder.add_edge("manager_prepare", "legacy_research")
    builder.add_edge("legacy_research", "manager_complete")
    builder.add_edge("manager_complete", END)

    effective_checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
    return builder.compile(checkpointer=effective_checkpointer)


async def run_research_graph(
    query: str,
    orchestrator: Orchestrator,
    *,
    thread_id: str,
    parent_thread_id: str | None = None,
    root_thread_id: str | None = None,
    run_config: RunConfig | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> ResearchReport:
    """Run one Phase 1 root research thread and return ``ResearchReport``."""
    canonical_root_thread_id = root_thread_id or thread_id
    graph = build_research_graph(
        orchestrator,
        run_config=run_config,
        checkpointer=checkpointer,
    )
    invocation_config = {"configurable": {"thread_id": thread_id}}
    final_state = await graph.ainvoke(
        ResearchGraphState(
            query=query,
            thread_id=thread_id,
            parent_thread_id=parent_thread_id,
            root_thread_id=canonical_root_thread_id,
            report=None,
        ),
        config=invocation_config,
    )
    report = final_state.get("report")
    if not isinstance(report, ResearchReport):
        raise TypeError("LangGraph research completed without a ResearchReport")
    return report
