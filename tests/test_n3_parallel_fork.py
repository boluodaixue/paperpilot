"""N3 acceptance tests for bounded homogeneous root-to-child fork."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.research import (
    AgentLimits,
    ExecutionIdentity,
    ForkReason,
    ResearchStatus,
    ResearchTask,
    build_research_agent_graph,
    create_research_agent_state,
    run_research_agent,
)
from src.research.agent_graph import _child_task
from src.research.fork_policy import evaluate_fork_candidates, fork_tool_schema
from src.research.models import ForkCandidate, ResearchRequirement
from src.research.research_sufficiency import build_research_requirements
from tests._research_assessment import assessment_response


def _fork_call(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "call-fork",
        "type": "function",
        "function": {
            "name": "fork_research",
            "arguments": json.dumps({"candidates": candidates}),
        },
    }


def _search_call(query: str) -> dict[str, Any]:
    return {
        "id": f"call-search-{query}",
        "type": "function",
        "function": {
            "name": "web_search",
            "arguments": json.dumps({"query": query}),
        },
    }


def _final(summary: str, finding: str) -> dict[str, Any]:
    return {
        "content": json.dumps(
            {
                "status": "completed",
                "summary": summary,
                "findings": [finding],
                "unresolved": [],
            }
        ),
        "tool_calls": [],
    }


def test_child_requirements_keep_the_parent_requirement_identity() -> None:
    parent = ResearchTask(
        "root",
        "Compare all model families",
        context={
            "directions": ["Chinese reasoning", "code", "long context"],
            "research_gaps": ["architecture"],
            "research_requirements": [
                {"requirement_id": "R1", "description": "Compare code quality"},
                {"requirement_id": "R2", "description": "Compare long-context quality"},
            ],
            "known_information": ["Reusable memory context"],
        },
    )
    candidate = ForkCandidate(
        objective="Research only Gemini long-context evidence",
        expected_output="A scoped evidence memo",
        requirement_ids=("R2",),
    )

    parent_requirements = build_research_requirements(parent)
    child = _child_task(parent, candidate, parent_requirements)
    requirements = build_research_requirements(child)

    assert requirements == (ResearchRequirement("R2", "Compare long-context quality"),)
    assert child.context["parent_requirement_ids"] == ["R2"]
    assert child.context["known_information"] == ["Reusable memory context"]
    assert child.context["parent_objective"] == parent.objective
    assert "directions" not in child.context
    assert "research_gaps" not in child.context


def test_multi_requirement_fork_requires_a_valid_parent_mapping() -> None:
    parent = ResearchTask("root", "Compare code and context")
    identity = _root_identity("root-lineage")
    missing = ForkCandidate(
        objective="Research code",
        expected_output="Code evidence",
        reasons=(ForkReason.CONTEXT_ISOLATION,),
    )
    unknown = ForkCandidate(
        objective="Research context",
        expected_output="Context evidence",
        requirement_ids=("R9",),
        reasons=(ForkReason.CONTEXT_ISOLATION,),
    )
    accepted, rejected = evaluate_fork_candidates(
        [missing, unknown],
        parent_task=parent,
        identity=identity,
        max_fork_depth=2,
        max_children=2,
        parent_requirement_ids=("R1", "R2"),
    )

    assert accepted == []
    assert any("missing parent requirement mapping" in item for item in rejected)
    assert any("unknown parent requirements R9" in item for item in rejected)
    candidate_schema = fork_tool_schema()["function"]["parameters"]["properties"]["candidates"]["items"]
    assert "requirement_ids" in candidate_schema["required"]


def test_fork_budget_prefers_requirement_breadth_before_finer_splits() -> None:
    parent = ResearchTask("root", "Cover four requirements")
    candidates = [
        ForkCandidate(
            objective=objective,
            expected_output=f"Evidence for {objective}",
            requirement_ids=(requirement_id,),
            reasons=(ForkReason.CONTEXT_ISOLATION,),
        )
        for objective, requirement_id in (
            ("R1 reasoning", "R1"),
            ("R1 code", "R1"),
            ("R1 context", "R1"),
            ("R2 architecture", "R2"),
            ("R3 causality", "R3"),
            ("R4 cost", "R4"),
        )
    ]

    accepted, rejected = evaluate_fork_candidates(
        candidates,
        parent_task=parent,
        identity=_root_identity("root-breadth"),
        max_fork_depth=2,
        max_children=4,
        parent_requirement_ids=("R1", "R2", "R3", "R4"),
    )

    assert [item.requirement_ids for item in accepted] == [
        ("R1",),
        ("R2",),
        ("R3",),
        ("R4",),
    ]
    assert sum("child budget exhausted" in item for item in rejected) == 2


@dataclass
class ForkTracker:
    active_tools: int = 0
    max_active_tools: int = 0
    tool_queries: list[str] = field(default_factory=list)
    tool_instance_ids: list[int] = field(default_factory=list)
    child_policy_ids: list[int] = field(default_factory=list)
    child_user_prompts: list[str] = field(default_factory=list)


class ConcurrentWebTool:
    name = "web_search"

    def __init__(self, tracker: ForkTracker) -> None:
        self.tracker = tracker

    def __deepcopy__(self, memo):
        return type(self)(self.tracker)

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fixed concurrent search",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, query: str) -> dict[str, Any]:
        self.tracker.active_tools += 1
        self.tracker.max_active_tools = max(
            self.tracker.max_active_tools,
            self.tracker.active_tools,
        )
        self.tracker.tool_queries.append(query)
        self.tracker.tool_instance_ids.append(id(self))
        await asyncio.sleep(0.03)
        self.tracker.active_tools -= 1
        slug = query.lower().replace(" ", "-")
        return {
            "results": [
                {
                    "title": f"Source for {query}",
                    "url": f"https://example.com/{slug}",
                    "snippet": f"Evidence collected for {query}.",
                }
            ]
        }


class ChildPolicy:
    def __init__(self, tracker: ForkTracker, fail_objective: str | None) -> None:
        self.tracker = tracker
        self.fail_objective = fail_objective
        self.tracker.child_policy_ids.append(id(self))

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        task_payload = json.loads(messages[1]["content"])
        objective = task_payload["objective"]
        if len(messages) == 2:
            self.tracker.child_user_prompts.append(messages[1]["content"])
        if self.fail_objective and self.fail_objective in objective:
            raise RuntimeError("child model failure")
        if messages[-1]["role"] == "tool" or tools == []:
            return _final(
                f"Completed {objective}",
                f"Evidence-backed finding for {objective}",
            )
        return {"content": "", "tool_calls": [_search_call(objective)]}


class ForkingPolicy:
    def __init__(
        self,
        candidates: list[dict[str, Any]],
        tracker: ForkTracker,
        *,
        fail_objective: str | None = None,
        repeat_once: bool = False,
    ) -> None:
        self.candidates = candidates
        self.tracker = tracker
        self.fail_objective = fail_objective
        self.repeat_once = repeat_once

    def fork(self):
        return ChildPolicy(self.tracker, self.fail_objective)

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        if tools == [] and str(messages[-1].get("content") or "").startswith("FINAL_SYNTHESIS_SNAPSHOT"):
            return _final(
                "The parent gathered and synthesized child research.",
                "The child directions produced evidence-backed findings.",
            )
        fork_responses = [
            message for message in messages if message.get("role") == "tool" and message.get("name") == "fork_research"
        ]
        if not fork_responses:
            return {
                "content": "ROOT_PRIVATE_PLAN must stay in the parent context.",
                "tool_calls": [_fork_call(self.candidates)],
            }
        if self.repeat_once and len(fork_responses) == 1:
            return {"content": "", "tool_calls": [_fork_call(self.candidates)]}
        return _final(
            "The parent gathered and synthesized child research.",
            "The child directions produced evidence-backed findings.",
        )


def _root_identity(thread_id: str = "root-fork") -> ExecutionIdentity:
    return ExecutionIdentity(thread_id, None, thread_id, 0)


def _candidate(
    objective: str,
    reason: str,
    *,
    estimated_tool_calls: int = 0,
    independent: bool = True,
    context: dict[str, Any] | None = None,
    requirement_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "objective": objective,
        "expected_output": f"Evidence for {objective}",
        "requirement_ids": requirement_ids or ["R1"],
        "context": context or {},
        "reasons": [reason],
        "estimated_tool_calls": estimated_tool_calls,
        "independent": independent,
    }


@pytest.mark.asyncio
async def test_independent_candidates_run_in_parallel_with_isolated_instances() -> None:
    tracker = ForkTracker()
    candidates = [
        _candidate("direction alpha", "parallel"),
        _candidate("direction beta", "parallel"),
    ]
    identity = _root_identity("root-parallel")
    graph = build_research_agent_graph(
        ForkingPolicy(candidates, tracker),
        [ConcurrentWebTool(tracker)],
    )

    final = await graph.ainvoke(
        create_research_agent_state(
            ResearchTask("root-task", "Compare two independent directions."),
            identity,
            AgentLimits(max_children=4),
        ),
        config={"configurable": {"thread_id": identity.thread_id}},
    )

    result = final["result"]
    assert result.status == ResearchStatus.COMPLETED
    assert len(result.child_result_refs) == 2
    assert len(result.evidence) == 2
    assert result.tool_calls_used == 2
    assert tracker.max_active_tools == 2
    assert len(set(tracker.child_policy_ids)) == 2
    assert len(set(tracker.tool_instance_ids)) == 2
    assert all(thread.startswith("root-parallel.child.") for thread in final["child_thread_ids"])


@pytest.mark.asyncio
async def test_tree_results_preserve_root_requirement_lineage_through_aggregation() -> None:
    tracker = ForkTracker()
    candidates = [
        _candidate("collect code evidence", "parallel", requirement_ids=["R1"]),
        _candidate("collect context evidence", "parallel", requirement_ids=["R2"]),
    ]
    identity = _root_identity("root-requirement-lineage")
    graph = build_research_agent_graph(
        ForkingPolicy(candidates, tracker),
        [ConcurrentWebTool(tracker)],
    )
    final = await graph.ainvoke(
        create_research_agent_state(
            ResearchTask(
                "root-lineage",
                "Compare code and long-context quality.",
                context={
                    "research_requirements": [
                        {"requirement_id": "R1", "description": "Compare code quality"},
                        {
                            "requirement_id": "R2",
                            "description": "Compare long-context quality",
                        },
                    ]
                },
            ),
            identity,
            AgentLimits(max_children=2),
        ),
        config={"configurable": {"thread_id": identity.thread_id}},
    )

    result = final["result"]
    assert result.status == ResearchStatus.COMPLETED
    assert {item.requirement_id for item in result.coverage} == {"R1", "R2"}
    assert all(item.status.value == "supported" for item in result.coverage)
    assert all(item.evidence_ids for item in result.coverage)
    child_requirement_sets = {
        tuple(item["requirement_id"] for item in json.loads(prompt)["context"]["research_requirements"])
        for prompt in tracker.child_user_prompts
    }
    assert child_requirement_sets == {("R1",), ("R2",)}


@pytest.mark.asyncio
async def test_dependent_parallel_candidates_are_rejected() -> None:
    tracker = ForkTracker()
    candidates = [
        _candidate("collect baseline", "parallel", independent=True),
        _candidate("analyze baseline output", "parallel", independent=False),
    ]

    result = await run_research_agent(
        ResearchTask(
            "root-task",
            "Run dependent work in the correct order.",
            require_evidence=False,
        ),
        ForkingPolicy(candidates, tracker),
        [ConcurrentWebTool(tracker)],
        identity=_root_identity("root-dependent"),
    )

    assert result.status == ResearchStatus.COMPLETED
    assert result.child_result_refs == ()
    assert tracker.tool_queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "expected_children"),
    [
        (_candidate("isolated corpus", "context_isolation"), 1),
        (_candidate("deep verification", "deep_tool_chain", estimated_tool_calls=3), 1),
        (_candidate("too shallow", "deep_tool_chain", estimated_tool_calls=2), 0),
    ],
)
async def test_context_isolation_and_deep_chain_conditions_are_deterministic(
    candidate: dict[str, Any],
    expected_children: int,
) -> None:
    tracker = ForkTracker()
    result = await run_research_agent(
        ResearchTask(
            "root-task",
            "Evaluate one scoped direction.",
            require_evidence=expected_children > 0,
        ),
        ForkingPolicy([candidate], tracker),
        [ConcurrentWebTool(tracker)],
        identity=_root_identity(f"root-condition-{expected_children}-{candidate['objective']}"),
        limits=AgentLimits(max_children=2),
    )

    assert len(result.child_result_refs) == expected_children
    assert len(tracker.tool_queries) == expected_children


@pytest.mark.asyncio
async def test_child_receives_scoped_task_not_parent_message_history() -> None:
    tracker = ForkTracker()
    candidate = _candidate(
        "isolated evidence extraction",
        "context_isolation",
        context={"required_source": "paper-123"},
    )

    result = await run_research_agent(
        ResearchTask(
            "root-task",
            "Parent objective",
            context={"shared_constraint": "use primary sources"},
        ),
        ForkingPolicy([candidate], tracker),
        [ConcurrentWebTool(tracker)],
        identity=_root_identity("root-context-isolation"),
    )

    assert result.status == ResearchStatus.COMPLETED
    assert len(tracker.child_user_prompts) == 1
    child_prompt = tracker.child_user_prompts[0]
    assert "required_source" in child_prompt
    assert "shared_constraint" in child_prompt
    assert "ROOT_PRIVATE_PLAN" not in child_prompt


@pytest.mark.asyncio
async def test_duplicate_fork_request_does_not_run_the_child_twice() -> None:
    tracker = ForkTracker()
    candidate = _candidate("repeat candidate", "context_isolation")

    result = await run_research_agent(
        ResearchTask("root-task", "Avoid duplicate child work."),
        ForkingPolicy([candidate], tracker, repeat_once=True),
        [ConcurrentWebTool(tracker)],
        identity=_root_identity("root-deduplicate"),
    )

    assert len(result.child_result_refs) == 1
    assert len(tracker.tool_queries) == 1


@pytest.mark.asyncio
async def test_depth_one_agent_cannot_fork_in_n3() -> None:
    tracker = ForkTracker()
    identity = ExecutionIdentity("child", "root", "root", 1)

    result = await run_research_agent(
        ResearchTask("child-task", "Handle this child task locally.", require_evidence=False),
        ForkingPolicy([_candidate("grandchild", "context_isolation")], tracker),
        [ConcurrentWebTool(tracker)],
        identity=identity,
        limits=AgentLimits(max_fork_depth=1),
    )

    assert result.status == ResearchStatus.COMPLETED
    assert result.child_result_refs == ()
    assert tracker.tool_queries == []


@pytest.mark.asyncio
async def test_partial_child_failure_keeps_successful_evidence_and_is_reported() -> None:
    tracker = ForkTracker()
    candidates = [
        _candidate("successful direction", "parallel"),
        _candidate("failing direction", "parallel"),
    ]

    result = await run_research_agent(
        ResearchTask("root-task", "Gather partial child results."),
        ForkingPolicy(candidates, tracker, fail_objective="failing"),
        [ConcurrentWebTool(tracker)],
        identity=_root_identity("root-partial-child"),
    )

    assert result.status == ResearchStatus.COMPLETED
    assert len(result.child_result_refs) == 2
    assert len(result.evidence) == 1
    assert any("returned failed" in item for item in result.unresolved)


@pytest.mark.asyncio
async def test_child_budget_limits_total_accepted_candidates() -> None:
    tracker = ForkTracker()
    candidates = [
        _candidate("isolated alpha", "context_isolation"),
        _candidate("isolated beta", "context_isolation"),
    ]

    result = await run_research_agent(
        ResearchTask("root-task", "Respect the child budget."),
        ForkingPolicy(candidates, tracker),
        [ConcurrentWebTool(tracker)],
        identity=_root_identity("root-child-budget"),
        limits=AgentLimits(max_children=1),
    )

    assert len(result.child_result_refs) == 1
    assert len(tracker.tool_queries) == 1
