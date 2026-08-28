"""N1 acceptance tests for the one homogeneous Research AgentGraph."""
from __future__ import annotations

import json
from typing import Any

import langfuse
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.research.agent_graph import (
    ResearchAgentState,
    build_research_agent_graph,
    create_research_agent_state,
)
from src.research.models import (
    AgentLimits,
    ExecutionIdentity,
    ResearchResult,
    ResearchStatus,
    ResearchTask,
)
from src.research import run_research_agent
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
        if messages[-1]["role"] == "tool":
            return _final(
                "The Transformer replaced recurrence with attention.",
                "The Transformer architecture is based on attention mechanisms.",
            )
        return {"content": "", "tool_calls": [_tool_call()]}


class DirectPolicy:
    def __call__(self, messages, *, tools=None):
        objective = json.loads(messages[1]["content"])["objective"]
        return _final(f"Summary for {objective}", f"Finding for {objective}")


class AlwaysToolPolicy:
    def __call__(self, messages, *, tools=None):
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
    assert updates == [
        "prepare",
        "think_and_plan",
        "use_tools",
        "assess_completion",
        "think_and_plan",
        "assess_completion",
        "synthesize",
    ]


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
    assert result.evidence == ()
    assert any("No source-locatable evidence" in item for item in result.unresolved)


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
    assert reason in result.unresolved


@pytest.mark.asyncio
async def test_completion_gate_finalizes_after_two_evidence_ready_rounds() -> None:
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

    class GateAwarePolicy:
        def __init__(self) -> None:
            self.finalized_without_tools = False

        def __call__(self, messages, *, tools=None):
            if tools == []:
                self.finalized_without_tools = True
                return _final("Enough independent evidence was collected.", "Grounded finding.")
            return {"content": "", "tool_calls": [_tool_call()]}

    policy = GateAwarePolicy()
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
    assert result.tool_calls_used == 3
    assert policy.finalized_without_tools is True
    assessments = [
        event
        for event in final["execution_events"]
        if event["kind"] == "completion_assessed"
    ]
    assert assessments[-2]["outcome"] == "finalize"
    assert assessments[-1]["outcome"] == "synthesize"


@pytest.mark.asyncio
async def test_completion_gate_stops_two_rounds_without_new_evidence() -> None:
    class FinalizeAwareAlwaysToolPolicy:
        def __call__(self, messages, *, tools=None):
            if tools == []:
                return _final(
                    "Research saturated without additional independent evidence.",
                    "Only one stable source was found.",
                    status="partial",
                )
            return {"content": "", "tool_calls": [_tool_call()]}

    identity = _root_identity("root-evidence-saturated")
    graph = build_research_agent_graph(
        FinalizeAwareAlwaysToolPolicy(),
        [FixedWebTool()],
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
    assert result.status == ResearchStatus.PARTIAL
    assert result.stop_reason == "evidence_saturated"
    assert result.tool_calls_used == 3
    assert result.iterations == 4


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
async def test_unstructured_final_response_is_partial_but_still_typed() -> None:
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
    assert result.status == ResearchStatus.PARTIAL
    assert "unstructured" in result.unresolved[0].lower()


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
