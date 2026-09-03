"""N1 acceptance tests for the one homogeneous Research AgentGraph."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import langfuse
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.research import run_research_agent
from src.research.agent_graph import (
    ResearchAgentState,
    _action_for_tool_call,
    _action_id,
    _delegable_token_budget,
    _delegable_tool_budget,
    _finalization_token_reserve,
    _extract_evidence,
    _fundable_child_count,
    _fork_resource_allocations,
    _remaining_child_slots,
    _remaining_finalization_seconds,
    _remaining_research_seconds,
    _runtime_exhaustion_assessment,
    _semantic_tool_error,
    _stable_evidence_id,
    _tool_artifact_id,
    build_research_agent_graph,
    create_research_agent_state,
)
from src.research.models import (
    AgentLimits,
    CriticalGap,
    EvidenceItem,
    ExecutionIdentity,
    ForkCandidate,
    NextResearchAction,
    OutputStatus,
    ResearchDecision,
    ResearchResult,
    ResearchStatus,
    ResearchTask,
    StrategyAttempt,
    TerminationReason,
)
from src.research.research_control import HomogeneousForkConfig
from src.research.research_sufficiency import active_next_actions
from src.tools.web_search import MockWebSearchTool


def _tool_call(name: str = "web_search", arguments: dict[str, Any] | None = None) -> dict:
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {"query": "transformer"}),
        },
    }


def _final(summary: str, finding: str, status: str = "completed") -> dict:
    return {
        "content": json.dumps(
            {
                "status": status,
                "summary": summary,
                "findings": [finding],
                "unresolved": [],
            }
        ),
        "tool_calls": [],
    }


def test_evidence_ids_are_stable_and_content_addressed() -> None:
    first = _stable_evidence_id("https://example.com/source", "same excerpt")
    assert first == _stable_evidence_id("https://example.com/source", "same excerpt")
    assert first != _stable_evidence_id("https://example.com/source", "different excerpt")
    assert first != _stable_evidence_id("https://example.com/other", "same excerpt")


def test_only_prioritized_active_action_receives_rewritten_tool_call() -> None:
    actions = (
        NextResearchAction("R1", "primary_document", "official report", "high", "A"),
        NextResearchAction("R2", "paper_search", "benchmark paper", "high", "B"),
    )

    active = active_next_actions(actions)
    matched = _action_for_tool_call(
        "web_search",
        {"query": "materially rewritten search terms"},
        active,
    )

    assert active == actions[:1]
    assert matched == actions[0]


def test_semantic_tool_errors_do_not_become_evidence() -> None:
    action = NextResearchAction(
        "R1",
        "official_database",
        "find the official benchmark",
        "high",
        "Support R1",
    )
    result = {
        "query": "find the official benchmark",
        "results": [],
        "error": "search quota exhausted",
    }

    assert _semantic_tool_error("web_search", result) == "search quota exhausted"
    assert (
        _extract_evidence(
            "web_search",
            {"query": action.query},
            result,
            action=action,
            artifact_id=_tool_artifact_id("web_search", {"query": action.query}, result),
        )
        == []
    )
    assert _semantic_tool_error(
        "browser",
        "[Browser Error] Network error: 403 Forbidden",
    )


def test_extracted_evidence_keeps_requirement_action_and_artifact_lineage() -> None:
    action = NextResearchAction(
        "R1",
        "primary_document",
        "Transformer attention official paper",
        "high",
        "Support the architecture requirement",
    )
    result = {
        "results": [
            {
                "title": "Attention Is All You Need",
                "url": "https://arxiv.org/abs/1706.03762",
                "snippet": "The Transformer uses attention mechanisms.",
            }
        ]
    }
    artifact_id = _tool_artifact_id("web_search", {"query": action.query}, result)

    evidence = _extract_evidence(
        "web_search",
        {"query": action.query},
        result,
        action=action,
        artifact_id=artifact_id,
    )

    assert len(evidence) == 1
    assert evidence[0].requirement_id == "R1"
    assert evidence[0].action_id == _action_id(action)
    assert evidence[0].artifact_id == artifact_id


def test_deeper_agents_stop_research_earlier_to_leave_upstream_synthesis_time() -> None:
    limits = AgentLimits(max_elapsed_seconds=300.0)
    root_identity = _root_identity("root-layered-reserve")
    child_identity = ExecutionIdentity(
        "child-layered-reserve",
        root_identity.thread_id,
        root_identity.root_thread_id,
        1,
    )
    grandchild_identity = ExecutionIdentity(
        "grandchild-layered-reserve",
        child_identity.thread_id,
        root_identity.root_thread_id,
        2,
    )
    task = ResearchTask("layered-reserve", "Reserve time at each depth.")
    root = _state(task, root_identity, limits)
    child = _state(task, child_identity, limits)
    grandchild = _state(task, grandchild_identity, limits)
    child["deadline_at"] = root["deadline_at"]
    grandchild["deadline_at"] = root["deadline_at"]

    root_remaining = _remaining_research_seconds(root)
    child_remaining = _remaining_research_seconds(child)
    grandchild_remaining = _remaining_research_seconds(grandchild)
    assert root_remaining - child_remaining == pytest.approx(30.0, abs=0.01)
    assert child_remaining - grandchild_remaining == pytest.approx(30.0, abs=0.01)

    wide_limits = AgentLimits(max_elapsed_seconds=1800.0)
    wide_root = _state(task, root_identity, wide_limits)
    wide_child = _state(task, child_identity, wide_limits)
    wide_child["deadline_at"] = wide_root["deadline_at"]
    assert _remaining_research_seconds(wide_root) - _remaining_research_seconds(wide_child) == pytest.approx(
        180.0, abs=0.01
    )


def test_root_gets_final_synthesis_time_after_the_research_deadline() -> None:
    limits = AgentLimits(
        max_elapsed_seconds=1200.0,
        root_finalization_grace_seconds=300.0,
    )
    root_identity = _root_identity("root-long-final-reserve")
    child_identity = ExecutionIdentity(
        "child-long-final-reserve",
        root_identity.thread_id,
        root_identity.root_thread_id,
        1,
    )
    task = ResearchTask("long-final-reserve", "Add 300 seconds for Root synthesis.")
    root = _state(task, root_identity, limits)
    child = _state(task, child_identity, limits)
    root["deadline_at"] = time.time() + 1200.0
    child["deadline_at"] = root["deadline_at"]

    assert _remaining_research_seconds(root) == pytest.approx(1200.0, abs=0.05)
    assert _remaining_research_seconds(child) == pytest.approx(1080.0, abs=0.05)
    assert _remaining_finalization_seconds(root) == pytest.approx(1500.0, abs=0.05)
    assert _remaining_finalization_seconds(child) == pytest.approx(1200.0, abs=0.05)


def test_fork_budget_keeps_parent_tool_and_token_capacity() -> None:
    limits = AgentLimits(
        max_tool_calls=20,
        max_total_tool_calls=100,
        max_total_tokens=120000,
    )
    state = _state(
        ResearchTask("parent-reserve", "Retain parent aggregation capacity."),
        _root_identity("root-parent-reserve"),
        limits,
    )
    state["total_tool_calls_used"] = 10
    state["estimated_tokens_used"] = 20000

    delegated_tools = _delegable_tool_budget(state, 4)
    delegated_tokens = _delegable_token_budget(state)

    assert delegated_tools == 72
    assert state["subtree_tool_budget"] - state["total_tool_calls_used"] - delegated_tools == 18
    assert delegated_tokens < (state["subtree_token_budget"] - state["estimated_tokens_used"])
    assert delegated_tokens > 0


def test_fork_resource_preflight_checks_time_tokens_and_tools() -> None:
    candidate = ForkCandidate(
        objective="Scoped direction",
        expected_output="Direction memo",
        estimated_tool_calls=2,
    )
    settings = HomogeneousForkConfig()

    low_time = _state(
        ResearchTask("low-time", "Retain time-starved work locally."),
        _root_identity("root-low-time"),
        AgentLimits(max_elapsed_seconds=300.0),
    )
    low_time["deadline_at"] = time.time() + 50.0
    low_tokens = _state(
        ResearchTask("low-tokens", "Retain token-starved work locally."),
        _root_identity("root-low-tokens"),
        AgentLimits(max_total_tokens=70000),
    )
    low_tools = _state(
        ResearchTask("low-tools", "Retain tool-starved work locally."),
        _root_identity("root-low-tools"),
        AgentLimits(max_total_tool_calls=1),
    )

    for state in (low_time, low_tokens, low_tools):
        accepted, tool_budgets, token_budgets, retained = _fork_resource_allocations(
            state,
            [candidate],
            fork_settings=settings,
            check_token_budget=True,
        )
        assert accepted == []
        assert tool_budgets == []
        assert token_budgets == []
        assert retained == [candidate]


def test_fork_resource_preflight_preserves_sufficient_equal_split() -> None:
    candidate = ForkCandidate(
        objective="Scoped direction",
        expected_output="Direction memo",
        estimated_tool_calls=2,
    )
    state = _state(
        ResearchTask("enough", "Dispatch sufficiently funded work."),
        _root_identity("root-enough"),
        AgentLimits(max_total_tokens=120000, max_total_tool_calls=4),
    )

    accepted, tool_budgets, token_budgets, retained = _fork_resource_allocations(
        state,
        [candidate],
        fork_settings=HomogeneousForkConfig(),
        check_token_budget=True,
    )

    assert accepted == [candidate]
    assert tool_budgets == [2]
    assert token_budgets == [_delegable_token_budget(state)]
    assert retained == []


def test_resource_rejections_do_not_consume_logical_child_slots() -> None:
    state = _state(
        ResearchTask("rejected", "Keep rejected scopes local."),
        _root_identity("root-rejected-slots"),
        AgentLimits(max_children=3),
    )
    state["completed_fork_fingerprints"] = ["rejected-a", "rejected-b"]

    assert _remaining_child_slots(state) == 3


def test_fundable_child_count_uses_global_lease_pool_when_enabled() -> None:
    state = _state(
        ResearchTask("lease", "Fund children from the shared pool."),
        _root_identity("root-lease-capacity"),
        AgentLimits(max_children=5, max_total_tool_calls=30),
    )
    settings = HomogeneousForkConfig(
        enabled=True,
        explicit_control_decision=True,
        budget_leases_enabled=True,
    )

    assert _fundable_child_count(
        state,
        5,
        fork_settings=settings,
        available_lease_tokens=180000,
    ) == 3
    assert _fundable_child_count(
        state,
        5,
        fork_settings=settings,
        available_lease_tokens=59999,
    ) == 0


def test_parent_token_reserve_grows_with_assessment_state_complexity() -> None:
    limits = AgentLimits(max_total_tokens=200000)
    identity = _root_identity("root-dynamic-reserve")
    simple = _state(ResearchTask("simple", "Simple reserve."), identity, limits)
    complex_state = _state(
        ResearchTask("complex", "Complex reserve."),
        _root_identity("root-dynamic-reserve-complex"),
        limits,
    )
    complex_state["observed_evidence"] = [
        EvidenceItem(
            evidence_id=f"E{index}",
            finding="finding " * 80,
            source_type="paper",
            title=f"Source {index}",
            source_ref=f"https://example.test/{index}",
            requirement_id="R1",
        )
        for index in range(40)
    ]
    complex_state["strategy_attempts"] = [
        StrategyAttempt("R1", "paper_search", f"query {index}", "no_progress") for index in range(20)
    ]

    assert _delegable_token_budget(complex_state) < _delegable_token_budget(simple)


def test_parent_token_reserve_is_not_eroded_by_sequential_fork_batches() -> None:
    limits = AgentLimits(max_total_tokens=200000)
    state = _state(
        ResearchTask("sequential-reserve", "Keep synthesis capacity."),
        _root_identity("root-sequential-reserve"),
        limits,
    )
    state["estimated_tokens_used"] = 20000

    first_delegable = _delegable_token_budget(state)
    state["estimated_tokens_used"] += first_delegable - 10000
    second_delegable = _delegable_token_budget(state)

    assert first_delegable == 140000
    assert second_delegable == 10000
    assert state["subtree_token_budget"] - state["estimated_tokens_used"] - second_delegable == 40000


def test_finalization_reserve_scales_for_root_and_child() -> None:
    limits = AgentLimits(max_total_tokens=300000)
    task = ResearchTask("reserve", "Keep enough room for synthesis.")
    root = _state(task, _root_identity("root-final-reserve"), limits)
    child = _state(
        task,
        ExecutionIdentity("child-final-reserve", "root-final-reserve", "root-final-reserve", 1),
        limits,
    )
    child["subtree_token_budget"] = 60000

    assert _finalization_token_reserve(root) == 45000
    assert _finalization_token_reserve(child) == 9000


def test_runtime_exhaustion_requires_three_distinct_strategy_families() -> None:
    state = _state(
        ResearchTask("runtime-exhaustion", "Stop exhausted paths."),
        _root_identity("root-runtime-exhaustion"),
    )
    state["critical_gaps"] = [CriticalGap("R1", "Important evidence remains.")]
    two = (
        StrategyAttempt("R1", "paper_search", "q1", "no_progress"),
        StrategyAttempt("R1", "primary_document", "q2", "no_progress"),
    )
    assert _runtime_exhaustion_assessment(state, two) is None

    exhausted = _runtime_exhaustion_assessment(
        state,
        (*two, StrategyAttempt("R1", "official_database", "q3", "no_progress")),
    )
    assert exhausted is not None
    assert exhausted.decision == ResearchDecision.STOP_RESEARCH
    assert exhausted.termination_reason == TerminationReason.EVIDENCE_EXHAUSTED
    assert exhausted.next_actions == ()


def _assessment_state(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    content = str(messages[-1].get("content") or "")
    if not content.startswith("ASSESS_RESEARCH_STATE"):
        return None
    return json.loads(content.split("STATE:\n", 1)[1])


def _assessment_response(
    state: dict[str, Any],
    *,
    decision: str = "stop_research",
    termination_reason: str | None = "coverage_complete",
    coverage_status: str = "supported",
    gap_impact: str = "high",
    strategy: str = "primary_document",
    action_requirement_index: int = 0,
) -> dict[str, Any]:
    requirements = state["requirements"]
    gaps = []
    actions = []
    if decision in {"continue", "replan"}:
        gaps = [
            {
                "requirement_id": requirements[action_requirement_index]["requirement_id"],
                "reason": "A material requirement remains unsupported.",
                "impact": gap_impact,
            }
        ]
        actions = [
            {
                "requirement_id": requirements[action_requirement_index]["requirement_id"],
                "strategy": strategy,
                "query": requirements[action_requirement_index]["description"],
                "expected_value": "high",
                "expected_improvement": "Resolve the material requirement with direct evidence.",
            }
        ]
    payload = {
        "decision": decision,
        "coverage": [
            {
                "requirement_id": requirement["requirement_id"],
                "status": coverage_status,
                "evidence_ids": (
                    [
                        item["evidence_id"]
                        for item in state["evidence"]
                        if not item.get("requirement_id") or item.get("requirement_id") == requirement["requirement_id"]
                    ]
                    if coverage_status != "unsupported"
                    else []
                ),
                "rationale": "The listed evidence directly supports the requirement.",
                "remaining_gap": None if coverage_status == "supported" else "Support is incomplete.",
            }
            for requirement in requirements
        ],
        "critical_gaps": gaps,
        "next_actions": actions,
        "termination_reason": termination_reason,
        "replan_reason": ("The current strategy has low marginal value." if decision == "replan" else None),
        "exhaustion_reason": None,
    }
    return {"content": json.dumps(payload), "tool_calls": []}


class FixedWebTool:
    name = "web_search"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def get_openai_tool_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fixed offline search",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("offline search failed")
        return {
            "results": [
                {
                    "title": "Attention Is All You Need",
                    "url": "https://arxiv.org/abs/1706.03762",
                    "snippet": "The Transformer is based solely on attention mechanisms.",
                }
            ]
        }


class GroundedPolicy:
    """Call one tool, then return final structured output."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def __call__(self, messages, *, tools=None):
        self.calls.append([dict(message) for message in messages])
        assessment = _assessment_state(messages)
        if assessment is not None:
            return _assessment_response(assessment)
        if messages[-1]["role"] == "tool" or tools == []:
            return _final(
                "The Transformer replaced recurrence with attention.",
                "The Transformer architecture is based on attention mechanisms.",
            )
        return {"content": "", "tool_calls": [_tool_call()]}


class DirectPolicy:
    def __call__(self, messages, *, tools=None):
        assessment = _assessment_state(messages)
        if assessment is not None:
            return _assessment_response(assessment)
        objective = json.loads(messages[1]["content"])["objective"]
        return _final(f"Summary for {objective}", f"Finding for {objective}")


class AlwaysToolPolicy:
    def __call__(self, messages, *, tools=None):
        assessment = _assessment_state(messages)
        if assessment is not None:
            return _assessment_response(
                assessment,
                decision="continue",
                termination_reason=None,
                coverage_status="unsupported",
            )
        return {"content": "", "tool_calls": [_tool_call()]}


class BrokenPolicy:
    def __call__(self, messages, *, tools=None):
        raise RuntimeError("model unavailable")


def _root_identity(thread_id: str) -> ExecutionIdentity:
    return ExecutionIdentity(
        thread_id=thread_id,
        parent_thread_id=None,
        root_thread_id=thread_id,
        depth=0,
    )


def _child_identity(thread_id: str, root_thread_id: str = "root") -> ExecutionIdentity:
    return ExecutionIdentity(
        thread_id=thread_id,
        parent_thread_id=root_thread_id,
        root_thread_id=root_thread_id,
        depth=1,
    )


def _state(
    task: ResearchTask,
    identity: ExecutionIdentity,
    limits: AgentLimits | None = None,
) -> ResearchAgentState:
    return create_research_agent_state(task, identity, limits or AgentLimits())


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


@pytest.mark.asyncio
async def test_fixed_tool_path_returns_structured_source_locatable_result() -> None:
    policy = GroundedPolicy()
    tool = FixedWebTool()
    graph = build_research_agent_graph(policy, [tool])
    identity = _root_identity("root-grounded")
    updates: list[str] = []

    async for update in graph.astream(
        _state(ResearchTask("task-1", "Research the Transformer architecture."), identity),
        config=_config(identity.thread_id),
        stream_mode="updates",
    ):
        updates.extend(update)

    snapshot = await graph.aget_state(_config(identity.thread_id))
    result = snapshot.values["result"]
    assert isinstance(result, ResearchResult)
    assert result.status == ResearchStatus.COMPLETED
    assert result.summary.startswith("The Transformer")
    assert result.tool_calls_used == 1
    assert len(result.evidence) == 1
    evidence = result.evidence[0]
    assert evidence.source_ref == "https://arxiv.org/abs/1706.03762"
    assert evidence.locator == evidence.source_ref
    assert evidence.excerpt
    assert evidence.requirement_id == "R1"
    assert evidence.action_id
    assert evidence.artifact_id
    assert result.strategy_attempts[0].action_id == evidence.action_id
    assert updates == [
        "prepare",
        "think_and_plan",
        "use_tools",
        "assess_research_state",
        "think_and_plan",
        "finalize_output",
        "synthesize",
    ]


@pytest.mark.asyncio
async def test_unbound_research_call_is_rejected_before_tool_execution() -> None:
    class RequiredQueryTool(FixedWebTool):
        def get_openai_tool_schema(self) -> dict:
            schema = super().get_openai_tool_schema()
            schema["function"]["parameters"] = {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            }
            return schema

    class EmptyArgumentsPolicy:
        def __call__(self, messages, *, tools=None):
            assessment = _assessment_state(messages)
            if assessment is not None:
                return _assessment_response(
                    assessment,
                    decision="continue",
                    termination_reason=None,
                    coverage_status="unsupported",
                )
            if tools == []:
                return _final("Bounded fallback.", "No unbound tool was executed.", "partial")
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-unbound",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": "{}",
                        },
                    }
                ],
            }

    tool = RequiredQueryTool()
    identity = _root_identity("root-unbound-tool")
    final = await build_research_agent_graph(EmptyArgumentsPolicy(), [tool]).ainvoke(
        _state(
            ResearchTask("unbound", "Reject an unbound research call."),
            identity,
            AgentLimits(max_iterations=1, max_tool_calls=5),
        ),
        config=_config(identity.thread_id),
    )

    assert tool.calls == []
    assert final["result"].tool_calls_used == 0
    assert any(event["kind"] == "tool_rejected_unbound" for event in final["execution_events"])


@pytest.mark.asyncio
async def test_large_tool_result_is_trimmed_only_after_verified_artifact_write() -> None:
    class CapturingStore:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def persist_tool_artifact(self, artifact_id: str, **kwargs) -> dict[str, object]:
            self.calls.append({"artifact_id": artifact_id, **kwargs})
            return {
                "artifact_id": artifact_id,
                "artifact_path": f"Artifacts/root/{artifact_id}.json",
                "content_hash": "a" * 64,
                "size_bytes": 20000,
            }

    class LargeTool(FixedWebTool):
        async def execute(self, **kwargs) -> dict:
            self.calls.append(kwargs)
            return {
                "results": [
                    {
                        "title": "Transformer source",
                        "url": "https://example.test/transformer",
                        "snippet": "Transformer evidence " + ("x" * 20000),
                    }
                ]
            }

    store = CapturingStore()
    policy = GroundedPolicy()
    identity = _root_identity("root-artifact-trim")
    result = await build_research_agent_graph(
        policy,
        [LargeTool()],
        tool_artifact_store=store,
    ).ainvoke(
        _state(ResearchTask("artifact-trim", "Research Transformer evidence."), identity),
        config=_config(identity.thread_id),
    )

    assert store.calls and len(str(store.calls[0]["result"])) > 20000
    tool_message = next(
        str(message.get("content") or "")
        for message in result["messages"]
        if message.get("role") == "tool" and "artifact_path" in str(message.get("content"))
    )
    assert len(tool_message) < 4000
    assert "Artifacts/root/" in tool_message
    assert result["result"].evidence


@pytest.mark.asyncio
async def test_failed_artifact_write_keeps_the_complete_tool_result_in_context() -> None:
    class FailingStore:
        def persist_tool_artifact(self, artifact_id: str, **kwargs) -> dict[str, object]:
            del artifact_id, kwargs
            raise OSError("injected artifact failure")

    class LargeTool(FixedWebTool):
        async def execute(self, **kwargs) -> dict:
            self.calls.append(kwargs)
            return {
                "results": [
                    {
                        "title": "Transformer source",
                        "url": "https://example.test/transformer",
                        "snippet": "BEGIN-FULL " + ("x" * 14000) + " END-FULL",
                    }
                ]
            }

    policy = GroundedPolicy()
    identity = _root_identity("root-artifact-failure")
    final = await build_research_agent_graph(
        policy,
        [LargeTool()],
        tool_artifact_store=FailingStore(),
    ).ainvoke(
        _state(ResearchTask("artifact-failure", "Research Transformer evidence."), identity),
        config=_config(identity.thread_id),
    )

    assert any(
        "BEGIN-FULL" in str(message.get("content") or "") and "END-FULL" in str(message.get("content") or "")
        for message in final["messages"]
        if message.get("role") == "tool"
    )


@pytest.mark.asyncio
async def test_public_runner_reuses_the_existing_web_tool_protocol() -> None:
    identity = _root_identity("root-existing-tool")

    result = await run_research_agent(
        ResearchTask("existing-tool", "Research the transformer architecture."),
        GroundedPolicy(),
        [MockWebSearchTool(delay_ms=(0, 0))],
        identity=identity,
    )

    assert result.status == ResearchStatus.COMPLETED
    assert result.evidence
    assert result.evidence[0].source_ref.startswith("https://arxiv.org/")


@pytest.mark.asyncio
async def test_root_and_child_execute_the_exact_same_compiled_graph() -> None:
    graph = build_research_agent_graph(GroundedPolicy(), [FixedWebTool()])
    identities = [_root_identity("root-shared"), _child_identity("child-shared", "root-shared")]

    for index, identity in enumerate(identities):
        final = await graph.ainvoke(
            _state(ResearchTask(f"task-{index}", "Research attention."), identity),
            config=_config(identity.thread_id),
        )
        assert final["result"].status == ResearchStatus.COMPLETED
        assert final["identity"] == identity


@pytest.mark.asyncio
async def test_checkpoint_state_and_messages_are_isolated_between_threads() -> None:
    checkpointer = InMemorySaver()
    graph = build_research_agent_graph(DirectPolicy(), [], checkpointer=checkpointer)
    tasks = {
        "root-alpha": ResearchTask("alpha", "alpha-only", require_evidence=False),
        "root-beta": ResearchTask("beta", "beta-only", require_evidence=False),
    }

    for thread_id, task in tasks.items():
        final = await graph.ainvoke(
            _state(task, _root_identity(thread_id)),
            config=_config(thread_id),
        )
        assert final["result"].status == ResearchStatus.COMPLETED

    alpha = (await graph.aget_state(_config("root-alpha"))).values
    beta = (await graph.aget_state(_config("root-beta"))).values
    assert alpha["task"].objective == "alpha-only"
    assert beta["task"].objective == "beta-only"
    assert "beta-only" not in json.dumps(alpha["messages"])
    assert "alpha-only" not in json.dumps(beta["messages"])


@pytest.mark.asyncio
async def test_tool_failure_degrades_to_partial_without_fabricating_evidence() -> None:
    graph = build_research_agent_graph(GroundedPolicy(), [FixedWebTool(fail=True)])
    identity = _root_identity("root-tool-failure")

    final = await graph.ainvoke(
        _state(ResearchTask("tool-failure", "Research with a failing tool."), identity),
        config=_config(identity.thread_id),
    )

    result = final["result"]
    assert result.status == ResearchStatus.PARTIAL
    assert result.termination_reason == TerminationReason.TOOL_FAILURE
    assert result.evidence == ()
    assert any("No source-locatable evidence" in item for item in result.unresolved)


@pytest.mark.asyncio
async def test_service_unavailable_alert_is_checkpointed_and_opens_circuit() -> None:
    class QuotaTool(FixedWebTool):
        async def execute(self, **kwargs) -> dict:
            self.calls.append(kwargs)
            raise RuntimeError(
                "BochaAI error: You do not have enough money or package quota"
            )

    class TwoCallsPolicy(GroundedPolicy):
        def __call__(self, messages, *, tools=None):
            assessment = _assessment_state(messages)
            if assessment is not None:
                return _assessment_response(assessment)
            if messages[-1]["role"] == "tool" or tools == []:
                return _final("Search service unavailable.", "No evidence was fabricated.")
            return {
                "content": "",
                "tool_calls": [
                    _tool_call(arguments={"query": "first query"}),
                    {
                        **_tool_call(arguments={"query": "second query"}),
                        "id": "call-web-search-second",
                    },
                ],
            }

    tool = QuotaTool()
    identity = _root_identity("root-tool-unavailable")
    final = await build_research_agent_graph(TwoCallsPolicy(), [tool]).ainvoke(
        _state(
            ResearchTask("tool-unavailable", "Research with an unavailable search service."),
            identity,
        ),
        config=_config(identity.thread_id),
    )

    result = final["result"]
    assert len(tool.calls) == 1
    assert result.termination_reason == TerminationReason.TOOL_FAILURE
    assert len(result.tool_alerts) == 1
    assert result.tool_alerts[0].category == "service_unavailable"
    assert result.tool_alerts[0].circuit_open is True
    alert_event = next(
        event for event in final["execution_events"] if event["kind"] == "tool_unavailable"
    )
    assert alert_event["tool"] == "web_search"
    assert any(
        event["kind"] == "tool_call_skipped_unavailable"
        for event in final["execution_events"]
    )
    assert any("External information alert" in item for item in result.unresolved)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (AgentLimits(max_iterations=5, max_tool_calls=1), "max_tool_calls_exhausted"),
        (AgentLimits(max_iterations=1, max_tool_calls=5), "max_iterations_exhausted"),
    ],
)
async def test_hard_limits_stop_the_loop_deterministically(
    limits: AgentLimits,
    reason: str,
) -> None:
    graph = build_research_agent_graph(AlwaysToolPolicy(), [FixedWebTool()])
    identity = _root_identity(f"root-{reason}")

    final = await graph.ainvoke(
        _state(ResearchTask("bounded", "Bound the research loop."), identity, limits),
        config=_config(identity.thread_id),
    )

    result = final["result"]
    assert result.status == ResearchStatus.PARTIAL
    assert result.stop_reason == reason
    assert result.termination_reason == TerminationReason.BUDGET_FORCED
    assert reason in result.unresolved


@pytest.mark.asyncio
async def test_wall_clock_fuse_reserves_one_final_synthesis_call() -> None:
    class SlowResearchPolicy:
        def __init__(self) -> None:
            self.final_calls = 0

        async def __call__(self, messages, *, tools=None):
            if tools == []:
                self.final_calls += 1
                return _final("Budget-forced synthesis.", "Available finding.")
            await asyncio.sleep(2)
            return _final("Too late.", "Too late.")

    policy = SlowResearchPolicy()
    identity = _root_identity("root-time-final-reserve")
    result = await run_research_agent(
        ResearchTask(
            "time-final-reserve",
            "Reserve time for final output.",
            require_evidence=False,
        ),
        policy,
        [],
        identity=identity,
        limits=AgentLimits(max_elapsed_seconds=1.0),
    )

    assert result.termination_reason == TerminationReason.BUDGET_FORCED
    assert result.stop_reason == "time_budget_exhausted"
    assert result.output_status == OutputStatus.VALID
    assert policy.final_calls == 1


@pytest.mark.asyncio
async def test_final_synthesis_uses_a_bounded_checkpoint_snapshot_not_full_history() -> None:
    class SnapshotPolicy:
        def __init__(self) -> None:
            self.prompt_chars = 0

        def __call__(self, messages, *, tools=None):
            assert tools == []
            content = str(messages[-1].get("content") or "")
            assert content.startswith("FINAL_SYNTHESIS_SNAPSHOT")
            self.prompt_chars = sum(len(str(item.get("content") or "")) for item in messages)
            return _final("Bounded synthesis.", "Checkpointed finding.")

    policy = SnapshotPolicy()
    identity = _root_identity("root-bounded-final-snapshot")
    state = _state(
        ResearchTask("bounded-final", "Synthesize a long execution."),
        identity,
    )
    state["messages"] = [
        {"role": "system", "content": "history" * 100_000},
        {"role": "user", "content": "more history" * 100_000},
    ]
    state["observed_evidence"] = [
        EvidenceItem(
            evidence_id=f"E{index}",
            finding=(f"Finding {index}. " * 100),
            source_type="web",
            title=f"Source {index}",
            source_ref=f"https://example.com/{index}",
        )
        for index in range(100)
    ]
    state["stop_reason"] = "time_budget_exhausted"
    state["termination_reason"] = TerminationReason.BUDGET_FORCED

    final = await build_research_agent_graph(policy, []).ainvoke(
        state,
        config=_config(identity.thread_id),
    )
    assert final["result"].output_status == OutputStatus.VALID
    assert policy.prompt_chars < 100_000


@pytest.mark.asyncio
async def test_root_final_synthesis_returns_markdown_and_continues_on_length() -> None:
    class MarkdownPolicy:
        def __init__(self) -> None:
            self.calls = 0
            self.max_tokens = 4096

        def __call__(self, messages, *, tools=None):
            assert tools == []
            self.calls += 1
            if self.calls == 1:
                assert "Return only the complete Markdown report" in messages[-1]["content"]
                assert self.max_tokens == 32768
                return {
                    "content": "# Report\n\nFirst section.",
                    "tool_calls": [],
                    "finish_reason": "length",
                }
            assert "Continue immediately" in messages[-1]["content"]
            return {
                "content": "## Final section\n\nComplete.",
                "tool_calls": [],
                "finish_reason": "stop",
            }

    policy = MarkdownPolicy()
    identity = _root_identity("root-direct-markdown-continuation")
    state = _state(
        ResearchTask(
            "root-direct-markdown-continuation",
            "Write the final report.",
            require_evidence=False,
        ),
        identity,
    )
    state["finalization_requested"] = True
    state["stop_reason"] = "token_budget_exhausted"
    state["termination_reason"] = TerminationReason.BUDGET_FORCED

    final = await build_research_agent_graph(policy, []).ainvoke(
        state,
        config=_config(identity.thread_id),
    )

    assert policy.calls == 2
    assert "First section" in final["result"].report_markdown
    assert "Final section" in final["result"].report_markdown
    assert final["result"].output_status == OutputStatus.VALID
    assert any(
        event.get("kind") == "root_report_continued"
        for event in final["execution_events"]
    )


@pytest.mark.asyncio
async def test_hard_stop_replaces_an_invalid_candidate_with_fresh_final_synthesis() -> None:
    class FreshFinalPolicy:
        def __init__(self) -> None:
            self.final_calls = 0

        def __call__(self, messages, *, tools=None):
            assert tools == []
            self.final_calls += 1
            return _final("Fresh structured synthesis.", "Recovered finding.")

    policy = FreshFinalPolicy()
    identity = _root_identity("root-invalid-candidate-hard-stop")
    state = _state(
        ResearchTask(
            "invalid-candidate-hard-stop",
            "Replace an invalid candidate after the research budget stops.",
            require_evidence=False,
        ),
        identity,
    )
    state["messages"] = [
        {"role": "system", "content": "saved history"},
        {"role": "user", "content": "saved task"},
    ]
    state["draft_raw"] = "plain, invalid candidate output"
    state["draft"] = None
    state["stop_reason"] = "time_budget_exhausted"
    state["termination_reason"] = TerminationReason.BUDGET_FORCED

    final = await build_research_agent_graph(policy, []).ainvoke(
        state,
        config=_config(identity.thread_id),
    )
    assert final["result"].output_status == OutputStatus.VALID
    assert final["draft_raw"] != "plain, invalid candidate output"
    assert policy.final_calls == 1


@pytest.mark.asyncio
async def test_many_sources_with_a_critical_gap_continues_until_the_gap_is_supported() -> None:
    class GrowingWebTool(FixedWebTool):
        def __init__(self) -> None:
            super().__init__()
            self.round = 0

        async def execute(self, **kwargs) -> dict:
            self.round += 1
            return {
                "results": [
                    {
                        "title": f"Source {self.round}-{index}",
                        "url": f"https://example.com/{self.round}/{index}",
                        "snippet": f"Evidence from round {self.round}, item {index}.",
                    }
                    for index in range(2)
                ]
            }

    class SufficiencyAwarePolicy:
        def __init__(self) -> None:
            self.finalized_without_tools = False
            self.assessments = 0

        def __call__(self, messages, *, tools=None):
            assessment = _assessment_state(messages)
            if assessment is not None:
                self.assessments += 1
                if self.assessments == 1:
                    return _assessment_response(
                        assessment,
                        decision="continue",
                        termination_reason=None,
                        coverage_status="unsupported",
                        action_requirement_index=1,
                    )
                return _assessment_response(assessment)
            if tools == []:
                self.finalized_without_tools = True
                return _final("Enough independent evidence was collected.", "Grounded finding.")
            return {"content": "", "tool_calls": [_tool_call()]}

    policy = SufficiencyAwarePolicy()
    tool = GrowingWebTool()
    identity = _root_identity("root-completion-gate")
    graph = build_research_agent_graph(policy, [tool])

    final = await graph.ainvoke(
        _state(
            ResearchTask(
                "completion-gate",
                "Assess a bounded topic.",
                context={"directions": ["direction one", "direction two"]},
            ),
            identity,
            AgentLimits(max_iterations=8, max_tool_calls=20, max_total_tool_calls=20),
        ),
        config=_config(identity.thread_id),
    )

    result = final["result"]
    assert result.status == ResearchStatus.COMPLETED
    assert result.stop_reason is None
    assert result.tool_calls_used == 2
    assert result.termination_reason == TerminationReason.COVERAGE_COMPLETE
    assert policy.finalized_without_tools is True
    assessments = [event for event in final["execution_events"] if event["kind"] == "research_state_assessed"]
    assert [event["decision"] for event in assessments] == ["continue", "stop_research"]


@pytest.mark.asyncio
async def test_no_global_zero_increment_rule_forces_stop_when_an_action_remains() -> None:
    class DistinctWebTool(FixedWebTool):
        async def execute(self, **kwargs) -> dict:
            self.calls.append(kwargs)
            index = len(self.calls)
            return {
                "results": [
                    {
                        "title": f"Primary source {index}",
                        "url": f"https://example.com/primary/{index}",
                        "snippet": f"Independent evidence {index}.",
                    }
                ]
            }

    class ReplanningPolicy:
        def __init__(self) -> None:
            self.assessments = 0

        def __call__(self, messages, *, tools=None):
            assessment = _assessment_state(messages)
            if assessment is not None:
                self.assessments += 1
                if self.assessments == 1:
                    return _assessment_response(
                        assessment,
                        decision="replan",
                        termination_reason=None,
                        coverage_status="unsupported",
                        strategy="official_database",
                    )
                return _assessment_response(assessment)
            if tools == []:
                return _final(
                    "The alternative strategy resolved the requirement.",
                    "The root requirement is supported.",
                )
            return {"content": "", "tool_calls": [_tool_call()]}

    identity = _root_identity("root-evidence-saturated")
    policy = ReplanningPolicy()
    graph = build_research_agent_graph(
        policy,
        [DistinctWebTool()],
    )
    final = await graph.ainvoke(
        _state(
            ResearchTask("evidence-saturated", "Find repeatable evidence."),
            identity,
            AgentLimits(max_iterations=8, max_tool_calls=20, max_total_tool_calls=20),
        ),
        config=_config(identity.thread_id),
    )

    result = final["result"]
    assert result.status == ResearchStatus.COMPLETED
    assert result.termination_reason == TerminationReason.COVERAGE_COMPLETE
    assert result.tool_calls_used == 2
    assert policy.assessments == 2


@pytest.mark.asyncio
async def test_saturated_stops_with_partial_when_only_low_impact_detail_remains() -> None:
    class SaturatedPolicy:
        def __call__(self, messages, *, tools=None):
            assessment = _assessment_state(messages)
            if assessment is not None:
                requirement = assessment["requirements"][0]
                evidence_ids = [item["evidence_id"] for item in assessment["evidence"]]
                return {
                    "content": json.dumps(
                        {
                            "decision": "stop_research",
                            "coverage": [
                                {
                                    "requirement_id": requirement["requirement_id"],
                                    "status": "weak",
                                    "evidence_ids": evidence_ids,
                                    "rationale": "The core answer is usable but a minor detail is weak.",
                                    "remaining_gap": "A low-impact detail remains.",
                                }
                            ],
                            "critical_gaps": [
                                {
                                    "requirement_id": requirement["requirement_id"],
                                    "reason": "Only a low-impact detail remains.",
                                    "impact": "low",
                                }
                            ],
                            "next_actions": [],
                            "termination_reason": "saturated",
                            "replan_reason": None,
                            "exhaustion_reason": None,
                        }
                    ),
                    "tool_calls": [],
                }
            if tools == []:
                return _final("Saturated synthesis.", "Useful but qualified finding.")
            return {"content": "", "tool_calls": [_tool_call()]}

    identity = _root_identity("root-saturated")
    result = await run_research_agent(
        ResearchTask("saturated", "Research until only minor detail remains."),
        SaturatedPolicy(),
        [FixedWebTool()],
        identity=identity,
    )
    assert result.status == ResearchStatus.PARTIAL
    assert result.termination_reason == TerminationReason.SATURATED


@pytest.mark.asyncio
async def test_evidence_exhausted_requires_two_executed_no_progress_strategies() -> None:
    class ExhaustedPolicy:
        def __call__(self, messages, *, tools=None):
            assessment = _assessment_state(messages)
            if assessment is not None:
                attempt_count = sum(item["attempt_count"] for item in assessment["strategy_attempt_summary"])
                if attempt_count == 0:
                    return _assessment_response(
                        assessment,
                        decision="continue",
                        termination_reason=None,
                        coverage_status="unsupported",
                        strategy="primary_document",
                    )
                if attempt_count == 1:
                    return _assessment_response(
                        assessment,
                        decision="replan",
                        termination_reason=None,
                        coverage_status="unsupported",
                        strategy="official_database",
                    )
                requirement = assessment["requirements"][0]
                return {
                    "content": json.dumps(
                        {
                            "decision": "stop_research",
                            "coverage": [
                                {
                                    "requirement_id": requirement["requirement_id"],
                                    "status": "unsupported",
                                    "evidence_ids": [],
                                    "rationale": "The available evidence does not resolve the gap.",
                                    "remaining_gap": "An important gap remains.",
                                }
                            ],
                            "critical_gaps": [
                                {
                                    "requirement_id": requirement["requirement_id"],
                                    "reason": "Two distinct evidence paths made no progress.",
                                    "impact": "high",
                                }
                            ],
                            "next_actions": [],
                            "termination_reason": "evidence_exhausted",
                            "replan_reason": None,
                            "exhaustion_reason": "Primary-document and official-database paths were exhausted.",
                        }
                    ),
                    "tool_calls": [],
                }
            if tools == []:
                return _final("Exhausted synthesis.", "Available evidence remains inconclusive.")
            return {"content": "", "tool_calls": [_tool_call()]}

    identity = _root_identity("root-evidence-exhausted")
    result = await run_research_agent(
        ResearchTask("exhausted", "Try distinct strategies for the key gap."),
        ExhaustedPolicy(),
        [FixedWebTool()],
        identity=identity,
    )
    assert result.status == ResearchStatus.PARTIAL
    assert result.termination_reason == TerminationReason.EVIDENCE_EXHAUSTED
    assert [item.outcome for item in result.strategy_attempts] == [
        "no_progress",
        "no_progress",
    ]


@pytest.mark.asyncio
async def test_checkpoint_resume_preserves_requirements_gaps_actions_attempts_and_decision() -> None:
    class DistinctWebTool(FixedWebTool):
        async def execute(self, **kwargs) -> dict:
            self.calls.append(kwargs)
            index = len(self.calls)
            return {
                "results": [
                    {
                        "title": f"Checkpoint source {index}",
                        "url": f"https://example.com/checkpoint/{index}",
                        "snippet": f"Checkpoint evidence {index}.",
                    }
                ]
            }

    class CheckpointPolicy:
        def __init__(self, *, pause: bool) -> None:
            self.pause = pause
            self.normal_calls = 0
            self.pause_started = asyncio.Event()

        async def __call__(self, messages, *, tools=None):
            assessment = _assessment_state(messages)
            if assessment is not None:
                attempt_count = sum(item["attempt_count"] for item in assessment["strategy_attempt_summary"])
                if attempt_count == 1:
                    return _assessment_response(
                        assessment,
                        decision="continue",
                        termination_reason=None,
                        coverage_status="unsupported",
                    )
                return _assessment_response(assessment)
            if tools == []:
                return _final("Checkpointed research completed.", "Checkpointed finding.")
            self.normal_calls += 1
            if self.pause and self.normal_calls == 2:
                self.pause_started.set()
                await asyncio.Event().wait()
            return {"content": "", "tool_calls": [_tool_call()]}

    saver = InMemorySaver()
    identity = _root_identity("root-sufficiency-checkpoint")
    first_policy = CheckpointPolicy(pause=True)
    tool = DistinctWebTool()
    graph = build_research_agent_graph(
        first_policy,
        [tool],
        checkpointer=saver,
    )
    invocation = asyncio.create_task(
        graph.ainvoke(
            _state(ResearchTask("checkpoint", "Preserve sufficiency state."), identity),
            config=_config(identity.thread_id),
        )
    )
    await asyncio.wait_for(first_policy.pause_started.wait(), timeout=2)
    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation

    paused = (await graph.aget_state(_config(identity.thread_id))).values
    assert paused["assessment_decision"].value == "continue"
    assert paused["research_requirements"]
    assert paused["coverage"]
    assert paused["critical_gaps"]
    assert paused["next_actions"]
    assert len(paused["strategy_attempts"]) == 1
    assert paused["strategy_attempts"][0].action_id

    rebuilt = build_research_agent_graph(
        CheckpointPolicy(pause=False),
        [tool],
        checkpointer=saver,
    )
    restored = (await rebuilt.aget_state(_config(identity.thread_id))).values
    for field in (
        "research_requirements",
        "coverage",
        "critical_gaps",
        "next_actions",
        "strategy_attempts",
        "assessment_decision",
    ):
        assert restored[field] == paused[field]

    final = await rebuilt.ainvoke(None, config=_config(identity.thread_id))
    assert final["result"].status == ResearchStatus.COMPLETED
    assert final["strategy_attempts"]


@pytest.mark.asyncio
async def test_policy_failure_returns_a_structured_failed_result() -> None:
    graph = build_research_agent_graph(BrokenPolicy(), [])
    identity = _root_identity("root-policy-failure")

    final = await graph.ainvoke(
        _state(ResearchTask("broken", "Research unavailable model."), identity),
        config=_config(identity.thread_id),
    )

    result = final["result"]
    assert result.status == ResearchStatus.FAILED
    assert result.stop_reason == "policy_error: model unavailable"
    assert result.termination_reason == TerminationReason.TOOL_FAILURE


@pytest.mark.asyncio
async def test_user_cancelled_state_has_priority_and_never_runs_research_tools() -> None:
    class CancellationPolicy:
        def __call__(self, messages, *, tools=None):
            assert tools == []
            return _final("Cancelled synthesis.", "Previously available finding.")

    identity = _root_identity("root-user-cancelled")
    state = _state(ResearchTask("cancelled", "Do not continue after cancellation."), identity)
    state["stop_reason"] = "user_cancelled"
    state["termination_reason"] = TerminationReason.USER_CANCELLED
    final = await build_research_agent_graph(
        CancellationPolicy(),
        [FixedWebTool()],
    ).ainvoke(state, config=_config(identity.thread_id))

    result = final["result"]
    assert result.status == ResearchStatus.PARTIAL
    assert result.termination_reason == TerminationReason.USER_CANCELLED
    assert result.tool_calls_used == 0
    assert result.tool_calls_used == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "checkpoint_thread", "message"),
    [
        (
            ExecutionIdentity("root", "parent", "root", 0),
            "root",
            "parent_thread_id is None",
        ),
        (
            ExecutionIdentity("child", None, "root", 1),
            "child",
            "requires a parent_thread_id",
        ),
        (
            _root_identity("root-state"),
            "root-checkpoint",
            "must match identity.thread_id",
        ),
    ],
)
async def test_invalid_execution_identity_is_rejected(
    identity: ExecutionIdentity,
    checkpoint_thread: str,
    message: str,
) -> None:
    graph = build_research_agent_graph(DirectPolicy(), [])
    with pytest.raises(ValueError, match=message):
        await graph.ainvoke(
            _state(ResearchTask("identity", "Check identity.", require_evidence=False), identity),
            config=_config(checkpoint_thread),
        )


@pytest.mark.asyncio
async def test_root_markdown_final_response_is_valid_without_json_repair() -> None:
    class PlainPolicy:
        def __call__(self, messages, *, tools=None):
            return {"content": "A plain final answer.", "tool_calls": []}

    graph = build_research_agent_graph(PlainPolicy(), [])
    identity = _root_identity("root-unstructured")
    final = await graph.ainvoke(
        _state(ResearchTask("plain", "Analyze supplied context.", require_evidence=False), identity),
        config=_config(identity.thread_id),
    )

    result = final["result"]
    assert isinstance(result, ResearchResult)
    assert result.status == ResearchStatus.COMPLETED
    assert result.termination_reason == TerminationReason.COVERAGE_COMPLETE
    assert result.output_status == OutputStatus.VALID
    assert result.report_markdown == "A plain final answer."


@pytest.mark.asyncio
async def test_assessment_json_is_repaired_once_without_polluting_research_state() -> None:
    class RepairableAssessmentPolicy:
        def __init__(self) -> None:
            self.evidence_id = ""

        def __call__(self, messages, *, tools=None):
            content = str(messages[-1].get("content") or "")
            assessment = _assessment_state(messages)
            if assessment is not None:
                self.evidence_id = assessment["evidence"][0]["evidence_id"]
                return {"content": "not-json", "tool_calls": []}
            if content.startswith("REPAIR_ASSESSMENT_JSON"):
                invalid = content.split("Invalid response:\n", 1)[0]
                del invalid
                # The repair prompt omits STATE, so use the known single requirement
                # and deterministic Evidence ID produced by FixedWebTool.
                payload = {
                    "decision": "stop_research",
                    "coverage": [
                        {
                            "requirement_id": "R1",
                            "status": "supported",
                            "evidence_ids": [self.evidence_id],
                            "rationale": "The fixed source supports the requirement.",
                            "remaining_gap": None,
                        }
                    ],
                    "critical_gaps": [],
                    "next_actions": [],
                    "termination_reason": "coverage_complete",
                    "replan_reason": None,
                    "exhaustion_reason": None,
                }
                return {"content": json.dumps(payload), "tool_calls": []}
            if tools == []:
                return _final("Repaired assessment completed.", "Grounded finding.")
            return {"content": "", "tool_calls": [_tool_call()]}

    identity = _root_identity("root-assessment-repair")
    graph = build_research_agent_graph(
        RepairableAssessmentPolicy(),
        [FixedWebTool()],
    )
    final = await graph.ainvoke(
        _state(ResearchTask("repair", "Repair assessment JSON."), identity),
        config=_config(identity.thread_id),
    )
    result = final["result"]
    assert result.status == ResearchStatus.COMPLETED
    assessed = next(event for event in final["execution_events"] if event["kind"] == "research_state_assessed")
    assert assessed["assessment_output_status"] == "repaired"
    assert assessed["assessment_error"]


@pytest.mark.asyncio
async def test_failed_assessment_repair_does_not_invent_a_research_action() -> None:
    class InvalidThenGroundedPolicy:
        def __init__(self) -> None:
            self.research_round = 0

        def __call__(self, messages, *, tools=None):
            content = str(messages[-1].get("content") or "")
            assessment = _assessment_state(messages)
            if assessment is not None:
                if assessment["evidence"]:
                    return _assessment_response(assessment)
                return {"content": "not-json", "tool_calls": []}
            if content.startswith("REPAIR_ASSESSMENT_JSON"):
                return {"content": "still-not-json", "tool_calls": []}
            if tools == []:
                return _final("Grounded after conservative continuation.", "Grounded finding.")
            self.research_round += 1
            if self.research_round == 1:
                return _final("Premature candidate.", "Unsupported candidate.")
            return {"content": "", "tool_calls": [_tool_call()]}

    identity = _root_identity("root-assessment-fallback-continue")
    result = await run_research_agent(
        ResearchTask("fallback-continue", "Keep researching an actionable gap."),
        InvalidThenGroundedPolicy(),
        [FixedWebTool()],
        identity=identity,
    )

    assert result.status == ResearchStatus.PARTIAL
    assert result.termination_reason == TerminationReason.TOOL_FAILURE
    assert result.tool_calls_used == 0
    assert result.coverage[0].status.value == "unsupported"


@pytest.mark.asyncio
async def test_repeated_assessment_contract_failure_stops_without_a_synthetic_loop() -> None:
    class AlwaysInvalidAssessmentPolicy:
        def __call__(self, messages, *, tools=None):
            content = str(messages[-1].get("content") or "")
            if _assessment_state(messages) is not None:
                return {"content": "not-json", "tool_calls": []}
            if content.startswith("REPAIR_ASSESSMENT_JSON"):
                return {"content": "still-not-json", "tool_calls": []}
            if tools == []:
                return _final(
                    "Assessment routing failed after evidence collection.",
                    "Collected evidence remains available.",
                )
            return {"content": "", "tool_calls": [_tool_call()]}

    identity = _root_identity("root-assessment-contract-failure")
    result = await run_research_agent(
        ResearchTask("assessment-failure", "Keep researching after structural failure."),
        AlwaysInvalidAssessmentPolicy(),
        [FixedWebTool()],
        identity=identity,
        limits=AgentLimits(max_iterations=2, max_tool_calls=10),
    )

    assert result.status == ResearchStatus.PARTIAL
    assert result.termination_reason == TerminationReason.TOOL_FAILURE
    assert result.stop_reason is None
    assert result.tool_calls_used == 1


@pytest.mark.asyncio
async def test_root_plain_markdown_does_not_require_json_repair() -> None:
    class RepairableFinalPolicy:
        def __call__(self, messages, *, tools=None):
            content = str(messages[-1].get("content") or "")
            assessment = _assessment_state(messages)
            if assessment is not None:
                return _assessment_response(assessment)
            if content.startswith("REPAIR_FINAL_JSON"):
                return _final("Repaired final output.", "Grounded repaired finding.")
            if tools == []:
                return {"content": "plain final", "tool_calls": []}
            return {"content": "", "tool_calls": [_tool_call()]}

    identity = _root_identity("root-final-repair")
    result = await run_research_agent(
        ResearchTask("final-repair", "Repair final JSON."),
        RepairableFinalPolicy(),
        [FixedWebTool()],
        identity=identity,
    )
    assert result.status == ResearchStatus.COMPLETED
    assert result.termination_reason == TerminationReason.COVERAGE_COMPLETE
    assert result.output_status == OutputStatus.VALID
    assert result.report_markdown == "plain final"


@pytest.mark.asyncio
async def test_empty_final_content_retries_from_bounded_state_snapshot() -> None:
    class EmptyThenFinalPolicy:
        def __call__(self, messages, *, tools=None):
            content = str(messages[-1].get("content") or "")
            assessment = _assessment_state(messages)
            if assessment is not None:
                return _assessment_response(assessment)
            if content.startswith("FINAL_OUTPUT_RETRY"):
                return _final("Recovered final output.", "Grounded recovered finding.")
            if tools == []:
                return {
                    "content": "",
                    "reasoning_content": "I should provide the final answer next.",
                    "tool_calls": [],
                }
            return {"content": "", "tool_calls": [_tool_call()]}

    identity = _root_identity("root-empty-final-retry")
    result = await run_research_agent(
        ResearchTask("empty-final", "Recover a reasoning-only final response."),
        EmptyThenFinalPolicy(),
        [FixedWebTool()],
        identity=identity,
    )

    assert result.status == ResearchStatus.COMPLETED
    assert result.termination_reason == TerminationReason.COVERAGE_COMPLETE
    assert result.output_status == OutputStatus.REPAIRED
    assert result.summary == "Recovered final output."


@pytest.mark.asyncio
async def test_langfuse_sdk_failure_does_not_change_agent_result(monkeypatch) -> None:
    monkeypatch.setenv("LANGFUSE_TRACING", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")

    def telemetry_failure(**_kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(langfuse, "propagate_attributes", telemetry_failure)
    monkeypatch.setattr(langfuse, "get_client", telemetry_failure)
    graph = build_research_agent_graph(GroundedPolicy(), [FixedWebTool()])
    identity = _root_identity("root-tracing-failure")

    final = await graph.ainvoke(
        _state(ResearchTask("tracing", "Research tracing isolation."), identity),
        config=_config(identity.thread_id),
    )

    assert final["result"].status == ResearchStatus.COMPLETED
