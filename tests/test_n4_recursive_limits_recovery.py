"""N4 acceptance tests for bounded recursion, hard limits, and recovery.

The policies and tools in this module are deterministic and offline.  Tests
exercise only the public Research Agent graph contracts; CLI and Web entry
points are intentionally out of scope.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.research import (
    AgentLimits,
    ExecutionIdentity,
    ResearchResult,
    ResearchStatus,
    ResearchTask,
    build_research_agent_graph,
    create_research_agent_state,
    run_research_agent,
)
from tests._research_assessment import assessment_response


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _root_identity(thread_id: str) -> ExecutionIdentity:
    return ExecutionIdentity(thread_id, None, thread_id, 0)


def _fork_call(*objectives: str) -> dict[str, Any]:
    candidates = [
        {
            "objective": objective,
            "expected_output": f"Evidence for {objective}",
            "context": {},
            "reasons": ["context_isolation"],
            "estimated_tool_calls": 0,
            "independent": True,
        }
        for objective in objectives
    ]
    return {
        "id": f"fork-{'-'.join(objectives)}",
        "type": "function",
        "function": {
            "name": "fork_research",
            "arguments": json.dumps({"candidates": candidates}),
        },
    }


def _search_call(query: str) -> dict[str, Any]:
    return {
        "id": f"search-{query}",
        "type": "function",
        "function": {
            "name": "web_search",
            "arguments": json.dumps({"query": query}),
        },
    }


def _final(objective: str, *, status: str = "completed") -> dict[str, Any]:
    return {
        "content": json.dumps(
            {
                "status": status,
                "summary": f"Completed {objective}",
                "findings": [f"Finding for {objective}"],
                "unresolved": [],
            }
        ),
        "tool_calls": [],
    }


def _continue_assessment(messages, *, query: str = "repeat") -> dict[str, Any] | None:
    content = str(messages[-1].get("content") or "")
    if not content.startswith("ASSESS_RESEARCH_STATE"):
        return None
    state = json.loads(content.split("STATE:\n", 1)[1])
    coverage = [
        {
            "requirement_id": item["requirement_id"],
            "status": "unsupported",
            "evidence_ids": [],
            "rationale": "More evidence is required.",
            "remaining_gap": "A material requirement remains open.",
        }
        for item in state["requirements"]
    ]
    requirement_id = state["requirements"][0]["requirement_id"]
    return {
        "content": json.dumps(
            {
                "decision": "continue",
                "coverage": coverage,
                "critical_gaps": [
                    {
                        "requirement_id": requirement_id,
                        "reason": "A material requirement remains open.",
                        "impact": "high",
                    }
                ],
                "next_actions": [
                    {
                        "requirement_id": requirement_id,
                        "strategy": "query_rewrite",
                        "query": query,
                        "expected_value": "high",
                        "expected_improvement": "Collect the missing direct evidence.",
                    }
                ],
                "termination_reason": None,
                "replan_reason": None,
                "exhaustion_reason": None,
            }
        ),
        "tool_calls": [],
    }


@dataclass
class _HierarchyTracker:
    depths_by_objective: dict[str, int] = field(default_factory=dict)
    fork_responses: dict[str, str] = field(default_factory=dict)
    policy_calls: Counter = field(default_factory=Counter)
    tool_calls: Counter = field(default_factory=Counter)


class _FixedWebTool:
    name = "web_search"

    def __init__(self, tracker: _HierarchyTracker) -> None:
        self.tracker = tracker

    def __deepcopy__(self, memo):
        return type(self)(self.tracker)

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fixed offline evidence lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, query: str) -> dict[str, Any]:
        self.tracker.tool_calls[query] += 1
        return {
            "results": [
                {
                    "title": f"Source for {query}",
                    "url": f"https://example.com/{query}",
                    "snippet": f"Fixed source-locatable evidence for {query}.",
                }
            ]
        }


class _HierarchyPolicy:
    """The same policy implementation is cloned for every recursion level."""

    def __init__(
        self,
        tracker: _HierarchyTracker,
        *,
        forks: dict[str, tuple[str, ...]],
        tool_objectives: tuple[str, ...] = (),
        failing_objectives: tuple[str, ...] = (),
    ) -> None:
        self.tracker = tracker
        self.forks = forks
        self.tool_objectives = set(tool_objectives)
        self.failing_objectives = set(failing_objectives)

    def fork(self):
        return type(self)(
            self.tracker,
            forks=self.forks,
            tool_objectives=tuple(self.tool_objectives),
            failing_objectives=tuple(self.failing_objectives),
        )

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        task_payload = json.loads(messages[1]["content"])
        objective = task_payload["objective"]
        depth_match = re.search(r"depth (\d+)", messages[0]["content"])
        assert depth_match is not None
        self.tracker.depths_by_objective.setdefault(
            objective,
            int(depth_match.group(1)),
        )
        self.tracker.policy_calls[objective] += 1

        if objective in self.failing_objectives:
            raise RuntimeError(f"fixed failure for {objective}")
        if tools == [] and str(messages[-1].get("content") or "").startswith(
            "FINAL_SYNTHESIS_SNAPSHOT"
        ):
            snapshot = json.loads(
                str(messages[-1]["content"]).split("STATE:\n", 1)[1]
            )
            fork_outcomes = [
                item["content"]
                for item in snapshot.get("tool_outcomes", [])
                if item.get("name") == "fork_research"
            ]
            if fork_outcomes:
                self.tracker.fork_responses[objective] = fork_outcomes[-1]
            return _final(objective)

        fork_messages = [
            message
            for message in messages
            if message.get("role") == "tool"
            and message.get("name") == "fork_research"
        ]
        if objective in self.forks and not fork_messages:
            return {
                "content": "",
                "tool_calls": [_fork_call(*self.forks[objective])],
            }
        if fork_messages:
            self.tracker.fork_responses[objective] = fork_messages[-1]["content"]
            return _final(objective)

        tool_messages = [
            message
            for message in messages
            if message.get("role") == "tool"
            and message.get("name") == "web_search"
        ]
        if objective in self.tool_objectives and not tool_messages:
            return {"content": "", "tool_calls": [_search_call(objective)]}
        return _final(objective)


@pytest.mark.asyncio
async def test_default_depth_runs_root_child_grandchild_and_records_lineage() -> None:
    limits = AgentLimits()
    assert limits.max_fork_depth == 2

    tracker = _HierarchyTracker()
    policy = _HierarchyPolicy(
        tracker,
        forks={
            "root objective": ("child objective",),
            "child objective": ("grandchild objective",),
            "grandchild objective": ("forbidden great-grandchild",),
        },
    )
    identity = _root_identity("root-recursive")
    graph = build_research_agent_graph(policy, [])
    final = await graph.ainvoke(
        create_research_agent_state(
            ResearchTask(
                "root-task",
                "root objective",
                require_evidence=False,
            ),
            identity,
            limits,
        ),
        config=_config(identity.thread_id),
    )

    result = final["result"]
    assert isinstance(result, ResearchResult)
    assert result.status == ResearchStatus.COMPLETED
    assert result.thread_count == 3
    assert result.estimated_tokens_used > 0
    assert result.retries_used == 0
    assert tracker.depths_by_objective == {
        "root objective": 0,
        "child objective": 1,
        "grandchild objective": 2,
    }
    assert "forbidden great-grandchild" not in tracker.depths_by_objective
    assert "fork depth limit reached" in tracker.fork_responses["grandchild objective"]

    starts = [
        event for event in final["execution_events"]
        if event["kind"] == "agent_started"
    ]
    assert {event["depth"] for event in starts} == {0, 1, 2}
    by_depth = {event["depth"]: event for event in starts}
    assert by_depth[0]["thread_id"] == identity.thread_id
    assert by_depth[0]["parent_thread_id"] is None
    assert by_depth[1]["parent_thread_id"] == by_depth[0]["thread_id"]
    assert by_depth[2]["parent_thread_id"] == by_depth[1]["thread_id"]
    assert all(event["root_thread_id"] == identity.thread_id for event in starts)


@pytest.mark.asyncio
async def test_total_thread_limit_stops_recursion_without_exceeding_budget() -> None:
    tracker = _HierarchyTracker()
    policy = _HierarchyPolicy(
        tracker,
        forks={
            "root objective": ("child objective",),
            "child objective": ("grandchild objective",),
        },
    )
    limits = AgentLimits(max_total_threads=2)

    result = await run_research_agent(
        ResearchTask("root-task", "root objective", require_evidence=False),
        policy,
        [],
        identity=_root_identity("root-thread-limit"),
        limits=limits,
    )

    assert result.thread_count == limits.max_total_threads
    assert set(tracker.depths_by_objective.values()) == {0, 1}
    assert "grandchild objective" not in tracker.depths_by_objective
    assert "child budget exhausted" in tracker.fork_responses["child objective"]


@pytest.mark.asyncio
async def test_child_cannot_fork_an_ancestor_objective() -> None:
    tracker = _HierarchyTracker()
    policy = _HierarchyPolicy(
        tracker,
        forks={
            "root objective": ("child objective",),
            "child objective": ("root objective",),
        },
    )

    result = await run_research_agent(
        ResearchTask("ancestor-dedup", "root objective", require_evidence=False),
        policy,
        [],
        identity=_root_identity("root-ancestor-dedup"),
        limits=AgentLimits(),
    )

    assert result.status == ResearchStatus.COMPLETED
    assert result.thread_count == 2
    assert tracker.policy_calls["root objective"] == 2
    assert "duplicates an ancestor task" in tracker.fork_responses["child objective"]


class _AlwaysToolPolicy:
    def __call__(self, messages, *, tools=None):
        assessment = _continue_assessment(messages)
        if assessment is not None:
            return assessment
        return {"content": "", "tool_calls": [_search_call("repeat")]}


@pytest.mark.asyncio
async def test_total_tool_call_limit_has_a_clear_stop_reason() -> None:
    tracker = _HierarchyTracker()
    limits = AgentLimits(
        max_iterations=5,
        max_tool_calls=5,
        max_total_tool_calls=1,
    )

    result = await run_research_agent(
        ResearchTask("tool-budget", "repeat tools"),
        _AlwaysToolPolicy(),
        [_FixedWebTool(tracker)],
        identity=_root_identity("root-tool-budget"),
        limits=limits,
    )

    assert tracker.tool_calls["repeat"] == 1
    assert result.tool_calls_used <= limits.max_total_tool_calls
    assert result.stop_reason == "max_tool_calls_exhausted"
    assert result.termination_reason.value == "budget_forced"
    assert result.status in {ResearchStatus.PARTIAL, ResearchStatus.FAILED}


class _SlowPolicy:
    async def __call__(self, messages, *, tools=None):
        await asyncio.sleep(0.05)
        return _final("slow objective")


@pytest.mark.asyncio
async def test_elapsed_time_limit_has_a_clear_stop_reason() -> None:
    limits = AgentLimits(max_elapsed_seconds=0.01)
    result = await run_research_agent(
        ResearchTask("time-budget", "slow objective", require_evidence=False),
        _SlowPolicy(),
        [],
        identity=_root_identity("root-time-budget"),
        limits=limits,
    )

    assert result.stop_reason == "time_budget_exhausted"
    assert result.termination_reason.value == "budget_forced"
    assert result.status == ResearchStatus.FAILED


class _TokenPolicy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        self.calls += 1
        return {
            **_final("token objective"),
            "usage": {"total_tokens": 10},
        }


@pytest.mark.asyncio
async def test_total_token_limit_is_reported_in_result_metrics() -> None:
    limits = AgentLimits(max_total_tokens=10)
    policy = _TokenPolicy()
    result = await run_research_agent(
        ResearchTask("token-budget", "token objective", require_evidence=False),
        policy,
        [],
        identity=_root_identity("root-token-budget"),
        limits=limits,
    )

    assert result.estimated_tokens_used <= limits.max_total_tokens
    assert result.stop_reason == "token_budget_exhausted"
    assert result.termination_reason.value == "budget_forced"
    assert result.status == ResearchStatus.FAILED
    assert policy.calls == 0


class _FinalAfterToolPolicy:
    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        if any(message.get("role") == "tool" for message in messages):
            return _final("retry objective")
        return {"content": "", "tool_calls": [_search_call("retry")]}


class _FlakyWebTool(_FixedWebTool):
    def __init__(self, tracker: _HierarchyTracker, failures: int) -> None:
        super().__init__(tracker)
        self.failures = failures

    async def execute(self, query: str) -> dict[str, Any]:
        self.tracker.tool_calls[query] += 1
        if self.tracker.tool_calls[query] <= self.failures:
            raise RuntimeError("fixed transient tool failure")
        return await super().execute(query)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("per_action", "total", "expected_retries"),
    [
        (1, 5, 1),
        (5, 2, 2),
    ],
)
async def test_retry_limits_bound_each_action_and_the_whole_run(
    per_action: int,
    total: int,
    expected_retries: int,
) -> None:
    tracker = _HierarchyTracker()
    limits = AgentLimits(
        max_tool_calls=10,
        max_total_tool_calls=10,
        max_retries_per_action=per_action,
        max_total_retries=total,
    )
    result = await run_research_agent(
        ResearchTask("retry-budget", "retry objective"),
        _FinalAfterToolPolicy(),
        [_FlakyWebTool(tracker, failures=10)],
        identity=_root_identity(f"root-retry-{per_action}-{total}"),
        limits=limits,
    )

    assert result.retries_used == expected_retries
    assert result.retries_used <= limits.max_total_retries
    assert result.retries_used <= limits.max_retries_per_action
    assert tracker.tool_calls["retry"] == expected_retries + 1


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("max_total_threads", 0),
        ("max_total_tool_calls", -1),
        ("max_elapsed_seconds", 0),
        ("max_total_tokens", -1),
        ("max_retries_per_action", -1),
        ("max_total_retries", -1),
    ],
)
def test_new_hard_limits_reject_invalid_values(
    field_name: str,
    invalid_value: int,
) -> None:
    limits = replace(AgentLimits(), **{field_name: invalid_value})
    with pytest.raises(ValueError):
        limits.validate()


@dataclass
class _RecoveryTracker:
    calls: Counter = field(default_factory=Counter)
    slow_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_slow: asyncio.Event = field(default_factory=asyncio.Event)


class _RecoveryPolicy:
    def __call__(self, messages, *, tools=None):
        content = str(messages[-1].get("content") or "")
        if content.startswith("ASSESS_RESEARCH_STATE"):
            state = json.loads(content.split("STATE:\n", 1)[1])
            if len(state["evidence"]) < 2:
                return _continue_assessment(messages, query="slow")
            return assessment_response(messages)
        completed_tools = [
            message for message in messages if message.get("role") == "tool"
        ]
        if not completed_tools:
            return {"content": "", "tool_calls": [_search_call("fast")]}
        if len(completed_tools) == 1:
            return {"content": "", "tool_calls": [_search_call("slow")]}
        return _final("recovered objective")


class _RecoveryTool:
    name = "web_search"

    def __init__(self, tracker: _RecoveryTracker) -> None:
        self.tracker = tracker

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return _FixedWebTool(_HierarchyTracker()).get_openai_tool_schema()

    async def execute(self, query: str) -> dict[str, Any]:
        self.tracker.calls[query] += 1
        if query == "slow" and self.tracker.calls[query] == 1:
            self.tracker.slow_started.set()
            await self.tracker.release_slow.wait()
        return {
            "results": [
                {
                    "title": f"Source for {query}",
                    "url": f"https://example.com/{query}",
                    "snippet": f"Recovered evidence for {query}.",
                }
            ]
        }


@pytest.mark.asyncio
async def test_external_cancellation_resumes_without_repeating_completed_tool() -> None:
    tracker = _RecoveryTracker()
    saver = InMemorySaver()
    identity = _root_identity("root-cancel-resume")
    graph = build_research_agent_graph(
        _RecoveryPolicy(),
        [_RecoveryTool(tracker)],
        checkpointer=saver,
    )
    invocation = asyncio.create_task(
        graph.ainvoke(
            create_research_agent_state(
                ResearchTask("recovery", "recovered objective"),
                identity,
                AgentLimits(),
            ),
            config=_config(identity.thread_id),
        )
    )
    await asyncio.wait_for(tracker.slow_started.wait(), timeout=2)
    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert tracker.calls["fast"] == 1
    tracker.release_slow.set()
    final = await graph.ainvoke(None, config=_config(identity.thread_id))

    assert final["result"].status == ResearchStatus.COMPLETED
    assert tracker.calls["fast"] == 1
    assert tracker.calls["slow"] == 2


@dataclass
class _ChildRecoveryTracker:
    calls: Counter = field(default_factory=Counter)
    fast_final_response: asyncio.Event = field(default_factory=asyncio.Event)
    slow_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_slow: asyncio.Event = field(default_factory=asyncio.Event)


class _ChildRecoveryPolicy:
    def __init__(self, tracker: _ChildRecoveryTracker) -> None:
        self.tracker = tracker

    def fork(self):
        return type(self)(self.tracker)

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        objective = json.loads(messages[1]["content"])["objective"]
        if tools == [] and str(messages[-1].get("content") or "").startswith(
            "FINAL_SYNTHESIS_SNAPSHOT"
        ):
            if objective == "fast child":
                self.tracker.fast_final_response.set()
            return _final(objective)
        fork_messages = [
            message
            for message in messages
            if message.get("role") == "tool"
            and message.get("name") == "fork_research"
        ]
        if objective == "root recovery" and not fork_messages:
            return {
                "content": "",
                "tool_calls": [_fork_call("fast child", "slow child")],
            }
        if fork_messages:
            return _final(objective)

        tool_messages = [
            message
            for message in messages
            if message.get("role") == "tool"
            and message.get("name") == "web_search"
        ]
        if not tool_messages:
            return {"content": "", "tool_calls": [_search_call(objective)]}
        if objective == "fast child":
            self.tracker.fast_final_response.set()
        return _final(objective)


class _BlockingChildTool:
    name = "web_search"

    def __init__(self, tracker: _ChildRecoveryTracker) -> None:
        self.tracker = tracker

    def __deepcopy__(self, memo):
        return type(self)(self.tracker)

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return _FixedWebTool(_HierarchyTracker()).get_openai_tool_schema()

    async def execute(self, query: str) -> dict[str, Any]:
        self.tracker.calls[query] += 1
        if query == "slow child" and self.tracker.calls[query] == 1:
            await self.tracker.fast_final_response.wait()
            # The fast child can now complete its synthesize node and terminal
            # checkpoint before cancellation is exposed to the parent.
            await asyncio.sleep(0.05)
            self.tracker.slow_started.set()
            await self.tracker.release_slow.wait()
        return {
            "results": [
                {
                    "title": f"Source for {query}",
                    "url": f"https://example.com/{query}",
                    "snippet": f"Recovered child evidence for {query}.",
                }
            ]
        }


@pytest.mark.asyncio
async def test_cancelled_parent_reuses_completed_child_checkpoint() -> None:
    tracker = _ChildRecoveryTracker()
    saver = InMemorySaver()
    identity = _root_identity("root-child-checkpoint-recovery")
    graph = build_research_agent_graph(
        _ChildRecoveryPolicy(tracker),
        [_BlockingChildTool(tracker)],
        checkpointer=saver,
    )
    invocation = asyncio.create_task(
        graph.ainvoke(
            create_research_agent_state(
                ResearchTask("child-recovery", "root recovery"),
                identity,
                AgentLimits(),
            ),
            config=_config(identity.thread_id),
        )
    )
    await asyncio.wait_for(tracker.slow_started.wait(), timeout=2)
    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert tracker.calls["fast child"] == 1
    tracker.release_slow.set()
    final = await graph.ainvoke(None, config=_config(identity.thread_id))

    result = final["result"]
    assert result.status == ResearchStatus.COMPLETED
    assert result.thread_count == 3
    assert tracker.calls["fast child"] == 1
    assert tracker.calls["slow child"] == 2
    assert {item.source_ref for item in result.evidence} == {
        "https://example.com/fast child",
        "https://example.com/slow child",
    }


@pytest.mark.asyncio
async def test_failed_grandchild_keeps_successful_sibling_evidence() -> None:
    tracker = _HierarchyTracker()
    policy = _HierarchyPolicy(
        tracker,
        forks={
            "root objective": ("child objective",),
            "child objective": ("successful grandchild", "failing grandchild"),
        },
        tool_objectives=("successful grandchild",),
        failing_objectives=("failing grandchild",),
    )

    result = await run_research_agent(
        ResearchTask("partial-tree", "root objective"),
        policy,
        [_FixedWebTool(tracker)],
        identity=_root_identity("root-partial-grandchild"),
        limits=AgentLimits(),
    )

    assert result.status == ResearchStatus.COMPLETED
    assert result.thread_count == 4
    assert len(result.evidence) == 1
    assert result.evidence[0].source_ref.endswith("/successful grandchild")
    assert tracker.tool_calls["successful grandchild"] == 1
    assert any("failed" in unresolved.lower() for unresolved in result.unresolved)
