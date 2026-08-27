"""Phase 1 LangGraph single-thread orchestration acceptance tests.

All research inputs are deterministic and offline.  The fixture keeps the
existing Planner, ResearcherAgent, AgentPool, SummarizerAgent, and
ResearchReport contracts in the exercised path while limiting the plan to one
ANALYZE task so this suite does not cover Phase 2 fan-out behaviour.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import langfuse
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.orchestrator.agent_pool import AgentPool
from src.orchestrator.langgraph_runner import (
    ResearchGraphState,
    build_research_graph,
    run_research_graph,
)
from src.orchestrator.orchestrator import Orchestrator
from src.orchestrator.schemas import ResearchReport, RunConfig
from src.planner.planner import Planner


QUERY = "Compare the fixed offline finding without using external tools."
FINDING = "The fixed offline finding is reproducible. Confidence: 0.75"
REPORT_CONTENT = (
    "# Fixed Offline Report\n\n"
    "The fixed finding is reproducible and uses no external service.\n\n"
    "Overall Confidence: 0.82"
)


def _thread_config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


class _FixedPolicy:
    """Minimal policy compatible with Planner, Researcher, and Summarizer."""

    tools = None

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"messages": messages, "tools": tools})
        return {"content": self.content, "tool_calls": []}


def _offline_orchestrator() -> Orchestrator:
    plan = {
        "sub_tasks": [
            {
                "task_id": "analysis-1",
                "task_type": "analyze",
                "description": "Analyze the supplied fixed offline finding.",
                "dependencies": [],
                "timeout_seconds": 5,
            }
        ]
    }
    planner = Planner(_FixedPolicy(json.dumps(plan)))
    pool = AgentPool(
        policy_factory=lambda: _FixedPolicy(FINDING),
        tools_factory=lambda: [],
        max_idle=1,
    )
    return Orchestrator(
        planner=planner,
        agent_pool=pool,
        summarizer_policy=_FixedPolicy(REPORT_CONTENT),
    )


def _run_config() -> RunConfig:
    return RunConfig(
        max_concurrent=1,
        global_timeout_seconds=30,
        synthesis_timeout_seconds=10,
        max_replan_rounds=0,
        enable_research_loop=False,
        enable_adversarial=False,
    )


def _root_state(query: str, thread_id: str) -> ResearchGraphState:
    return ResearchGraphState(
        query=query,
        thread_id=thread_id,
        parent_thread_id=None,
        root_thread_id=thread_id,
        report=None,
    )


@pytest.mark.asyncio
async def test_legacy_and_langgraph_return_equivalent_research_reports() -> None:
    legacy_report = await _offline_orchestrator().run(QUERY, config=_run_config())
    graph_report = await run_research_graph(
        QUERY,
        _offline_orchestrator(),
        thread_id="root-equivalence",
        run_config=_run_config(),
    )

    assert isinstance(legacy_report, ResearchReport)
    assert isinstance(graph_report, ResearchReport)
    assert asdict(graph_report) == asdict(legacy_report)


@pytest.mark.asyncio
async def test_graph_streams_exact_serial_node_order_without_fan_out() -> None:
    graph = build_research_graph(
        _offline_orchestrator(),
        run_config=_run_config(),
    )
    node_updates: list[str] = []

    async for update in graph.astream(
        _root_state(QUERY, "root-flow"),
        config=_thread_config("root-flow"),
        stream_mode="updates",
    ):
        node_updates.extend(update)

    assert node_updates == [
        "manager_prepare",
        "legacy_research",
        "manager_complete",
    ]


@pytest.mark.asyncio
async def test_in_memory_snapshots_are_readable_and_isolated_by_thread_id() -> None:
    checkpointer = InMemorySaver()
    graph = build_research_graph(
        _offline_orchestrator(),
        run_config=_run_config(),
        checkpointer=checkpointer,
    )
    threads = {
        "root-alpha": "offline query alpha",
        "root-beta": "offline query beta",
    }

    for thread_id, query in threads.items():
        final_state = await graph.ainvoke(
            _root_state(query, thread_id),
            config=_thread_config(thread_id),
        )
        assert isinstance(final_state["report"], ResearchReport)

    for thread_id, query in threads.items():
        snapshot = await graph.aget_state(_thread_config(thread_id))
        values = snapshot.values

        assert snapshot.next == ()
        assert set(values) == {
            "query",
            "thread_id",
            "parent_thread_id",
            "root_thread_id",
            "report",
        }
        assert values["query"] == query
        assert values["thread_id"] == thread_id
        assert values["parent_thread_id"] is None
        assert values["root_thread_id"] == thread_id
        assert isinstance(values["report"], ResearchReport)
        assert values["report"].query == query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thread_id", "parent_thread_id", "root_thread_id", "message"),
    [
        ("", None, None, "thread_id must be a non-empty string"),
        ("root-a", "parent-a", "root-a", "parent_thread_id is None"),
        ("root-a", None, "different-root", "root_thread_id == thread_id"),
    ],
)
async def test_graph_entry_rejects_invalid_phase1_root_identity(
    thread_id: str,
    parent_thread_id: str | None,
    root_thread_id: str | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        await run_research_graph(
            QUERY,
            _offline_orchestrator(),
            thread_id=thread_id,
            parent_thread_id=parent_thread_id,
            root_thread_id=root_thread_id,
            run_config=_run_config(),
        )


@pytest.mark.asyncio
async def test_graph_rejects_checkpoint_thread_id_mismatch() -> None:
    graph = build_research_graph(
        _offline_orchestrator(),
        run_config=_run_config(),
    )

    with pytest.raises(ValueError, match="configurable.thread_id must match"):
        await graph.ainvoke(
            _root_state(QUERY, "root-state"),
            config=_thread_config("root-checkpoint"),
        )


@pytest.mark.asyncio
async def test_disabled_tracing_does_not_change_graph_result(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_TRACING", "false")

    report = await run_research_graph(
        QUERY,
        _offline_orchestrator(),
        thread_id="root-tracing-disabled",
        run_config=_run_config(),
    )

    assert isinstance(report, ResearchReport)
    assert report.query == QUERY
    assert report.content == REPORT_CONTENT
    assert report.confidence == 0.82


@pytest.mark.asyncio
async def test_tracing_sdk_failure_does_not_change_graph_result(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")

    def telemetry_failure(**_kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(langfuse, "propagate_attributes", telemetry_failure)
    monkeypatch.setattr(langfuse, "get_client", telemetry_failure)

    report = await run_research_graph(
        QUERY,
        _offline_orchestrator(),
        thread_id="root-tracing-failure",
        run_config=_run_config(),
    )

    assert isinstance(report, ResearchReport)
    assert report.query == QUERY
    assert report.content == REPORT_CONTENT
    assert report.confidence == 0.82
