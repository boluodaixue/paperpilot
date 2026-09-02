from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from src.research.agent_graph import (
    _fallback_assessment,
    build_research_agent_graph,
    create_research_agent_state,
)
from src.research.agent_scheduler import (
    FairAgentScheduler,
    ScheduledAgent,
    ScheduledAgentCancelled,
)
from src.research.fork_policy import evaluate_fork_candidates
from src.research.models import (
    AgentLimits,
    CriticalGap,
    EvidenceItem,
    ExecutionIdentity,
    ForkCandidate,
    ForkReason,
    ResearchTask,
    ResearchResult,
    ResearchStatus,
    TerminationReason,
)
from src.research.research_blackboard import ResearchBlackboard
from src.research.research_sufficiency import (
    build_research_requirements,
    initial_coverage,
)
from src.research.runtime import shared_comparison_plan_from_config
from src.research.research_control import HomogeneousForkConfig
from tests._research_assessment import assessment_response


def _candidate(objective: str, scope: str) -> ForkCandidate:
    return ForkCandidate(
        objective=objective,
        expected_output="Scoped evidence",
        requirement_ids=("R1",),
        scope_signature=scope,
        reasons=(ForkReason.CONTEXT_ISOLATION,),
    )


def test_parent_semantic_decision_is_not_replaced_by_fuzzy_similarity() -> None:
    identity = ExecutionIdentity("root", None, "root", 0)
    near_a = _candidate("Inspect disclosure timing", "disclosure-timing-annual")
    near_b = _candidate("Inspect disclosure timing details", "disclosure-timing-event")
    exact = _candidate("  INSPECT disclosure timing ", " disclosure-timing-annual ")

    accepted, rejected = evaluate_fork_candidates(
        (near_a, near_b, exact),
        parent_task=ResearchTask("root", "Compare disclosure"),
        identity=identity,
        max_fork_depth=2,
        max_children=5,
        parent_requirement_ids=("R1",),
    )

    assert accepted == [near_a, near_b]
    assert any("duplicate task" in item for item in rejected)


def test_simple_agent_limits_and_legacy_aliases_are_normalized() -> None:
    limits = AgentLimits()
    assert limits.effective_max_concurrent_agents == 10
    assert limits.effective_max_total_agents == 24
    assert limits.effective_max_children_per_agent == 5
    assert limits.max_fork_depth == 2

    legacy = AgentLimits(max_children=3, max_total_threads=7)
    assert legacy.effective_max_children_per_agent == 3
    assert legacy.effective_max_total_agents == 7
    assert legacy.effective_max_concurrent_agents == 7


@pytest.mark.asyncio
async def test_scheduler_queues_overflow_and_waiting_parent_uses_no_slot() -> None:
    scheduler = FairAgentScheduler(
        run_id="run",
        max_concurrent_agents=2,
        max_total_agents=24,
    )
    await scheduler.activate_root("root")
    release = asyncio.Event()
    active = 0
    peak = 0

    async def runner(index: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return index

    task = asyncio.create_task(scheduler.run_children(
        parent_thread_id="root",
        parent_assignment_id="assignment-root",
        children=[
            ScheduledAgent(
                assignment_id=f"assignment-{index}",
                thread_id=f"child-{index}",
                parent_assignment_id="assignment-root",
                runner=lambda index=index: runner(index),
                can_start=lambda: True,
            )
            for index in range(5)
        ],
    ))
    await asyncio.sleep(0.05)
    metrics = scheduler.metrics()
    assert active == 2
    assert metrics["active_peak"] == 2
    assert metrics["queued_peak"] >= 3
    assert metrics["waiting_peak"] == 1
    release.set()
    assert await task == [0, 1, 2, 3, 4]
    assert peak == 2


@pytest.mark.asyncio
async def test_scheduler_is_round_robin_fair_across_parent_assignments() -> None:
    scheduler = FairAgentScheduler(
        run_id="run-fair",
        max_concurrent_agents=2,
        max_total_agents=24,
    )
    await scheduler.activate_root("root")
    starts: list[str] = []

    async def grandchild(label: str) -> str:
        starts.append(label)
        await asyncio.sleep(0.02)
        return label

    async def parent(label: str) -> str:
        await scheduler.run_children(
            parent_thread_id=f"parent-{label}",
            parent_assignment_id=f"assignment-{label}",
            children=[
                ScheduledAgent(
                    assignment_id=f"assignment-{label}-{index}",
                    thread_id=f"grandchild-{label}-{index}",
                    parent_assignment_id=f"assignment-{label}",
                    runner=lambda label=f"{label}{index}": grandchild(label),
                    can_start=lambda: True,
                )
                for index in range(2)
            ],
        )
        return label

    await scheduler.run_children(
        parent_thread_id="root",
        parent_assignment_id="assignment-root",
        children=[
            ScheduledAgent(
                assignment_id=f"assignment-{label}",
                thread_id=f"parent-{label}",
                parent_assignment_id="assignment-root",
                runner=lambda label=label: parent(label),
                can_start=lambda: True,
            )
            for label in ("A", "B")
        ],
    )

    assert set(starts[:2]) == {"A0", "B0"}
    assert starts.index("A1") > starts.index("B0")
    assert starts.index("B1") > starts.index("A0")


def _board(path: Path) -> ResearchBlackboard:
    board = ResearchBlackboard(path)
    board.ensure_plan(
        "run",
        plan_id="plan",
        objective="objective",
        requirements=({"requirement_id": "R1", "description": "Evidence"},),
    )
    board.ensure_root_assignment(
        "run",
        assignment_id="assignment-root",
        owner_thread_id="root",
        objective="objective",
        requirement_ids=("R1",),
    )
    return board


@pytest.mark.asyncio
async def test_budget_boundary_cancels_only_never_started_queue_entries(tmp_path) -> None:
    board = _board(tmp_path / "board.sqlite")
    rows = [
        {
            "assignment_id": f"assignment-{index}",
            "parent_assignment_id": "assignment-root",
            "owner_thread_id": f"child-{index}",
            "requirement_ids": ("R1",),
            "objective": f"scope {index}",
            "scope_signature": f"scope-{index}",
            "status": "queued",
        }
        for index in range(2)
    ]
    board.register_assignment_nodes(
        "run",
        rows,
        actor_thread_id="root",
        lease_seconds=60,
        max_total_assignments=24,
        max_children_per_parent=5,
        max_depth=2,
    )
    scheduler = FairAgentScheduler(
        run_id="run",
        max_concurrent_agents=1,
        max_total_agents=24,
        board=board,
    )
    await scheduler.activate_root("root")
    release = asyncio.Event()
    budget_open = True

    async def first() -> str:
        await release.wait()
        board.update_assignment_node(
            "run", "assignment-0", owner_thread_id="child-0", status="completed"
        )
        return "first"

    task = asyncio.create_task(scheduler.run_children(
        parent_thread_id="root",
        parent_assignment_id="assignment-root",
        children=[
            ScheduledAgent(
                "assignment-0", "child-0", "assignment-root", first,
                lambda: True,
            ),
            ScheduledAgent(
                "assignment-1", "child-1", "assignment-root",
                lambda: asyncio.sleep(0, result="second"),
                lambda: budget_open,
            ),
        ],
    ))
    await asyncio.sleep(0.05)
    budget_open = False
    release.set()
    results = await task

    assert results[0] == "first"
    assert isinstance(results[1], ScheduledAgentCancelled)
    assert board.assignment_node("run", "assignment-1")["status"] == "cancelled_due_to_budget"
    assert board.assignment_node("run", "assignment-root")["status"] == "researching"


def test_queue_and_waiting_state_survive_blackboard_reopen(tmp_path) -> None:
    path = tmp_path / "board.sqlite"
    board = _board(path)
    outcome = board.register_assignment_nodes(
        "run",
        ({
            "assignment_id": "assignment-child",
            "parent_assignment_id": "assignment-root",
            "owner_thread_id": "child",
            "requirement_ids": ("R1",),
            "objective": "scope",
            "scope_signature": "scope",
            "status": "queued",
        },),
        actor_thread_id="root",
        lease_seconds=60,
        max_total_assignments=24,
        max_children_per_parent=5,
        max_depth=2,
    )
    board.update_assignment_node(
        "run",
        "assignment-root",
        owner_thread_id="root",
        status="waiting_children",
    )
    assert outcome["assignment-child"].acquired is True

    reopened = ResearchBlackboard(path)
    snapshot = reopened.snapshot("run", viewer_thread_id="root")
    statuses = {item["assignment_id"]: item["status"] for item in snapshot["assignment_tree"]}
    assert statuses == {
        "assignment-root": "waiting_children",
        "assignment-child": "queued",
    }


def test_blackboard_enforces_five_children_depth_two_and_total_twenty_four(tmp_path) -> None:
    board = _board(tmp_path / "limits.sqlite")
    limits = AgentLimits()
    children = [
        {
            "assignment_id": f"child-{index}",
            "parent_assignment_id": "assignment-root",
            "owner_thread_id": f"child-thread-{index}",
            "requirement_ids": ("R1",),
            "objective": f"child scope {index}",
            "scope_signature": f"child-{index}",
            "status": "queued",
        }
        for index in range(5)
    ]
    child_outcomes = board.register_assignment_nodes(
        "run",
        children,
        actor_thread_id="root",
        lease_seconds=60,
        max_total_assignments=limits.effective_max_total_agents,
        max_children_per_parent=limits.effective_max_children_per_agent,
        max_depth=limits.max_fork_depth,
    )
    assert all(item.acquired for item in child_outcomes.values())

    sixth = board.register_assignment_nodes(
        "run",
        ({
            "assignment_id": "child-5",
            "parent_assignment_id": "assignment-root",
            "owner_thread_id": "child-thread-5",
            "requirement_ids": ("R1",),
            "objective": "sixth child",
            "scope_signature": "child-5",
        },),
        actor_thread_id="root",
        lease_seconds=60,
        max_total_assignments=24,
        max_children_per_parent=5,
        max_depth=2,
    )
    assert sixth["child-5"].reason == "parent_child_limit_reached"

    for child_index in range(4):
        grandchildren = [
            {
                "assignment_id": f"grandchild-{child_index}-{index}",
                "parent_assignment_id": f"child-{child_index}",
                "owner_thread_id": f"grandchild-thread-{child_index}-{index}",
                "requirement_ids": ("R1",),
                "objective": f"grandchild scope {child_index}-{index}",
                "scope_signature": f"grandchild-{child_index}-{index}",
                "status": "queued",
            }
            for index in range(5)
        ]
        outcomes = board.register_assignment_nodes(
            "run",
            grandchildren,
            actor_thread_id=f"child-thread-{child_index}",
            lease_seconds=60,
            max_total_assignments=24,
            max_children_per_parent=5,
            max_depth=2,
        )
        if child_index < 3:
            assert all(item.acquired for item in outcomes.values())
        else:
            assert sum(item.acquired for item in outcomes.values()) == 3
            assert sum(
                item.reason == "global_thread_limit_reached"
                for item in outcomes.values()
            ) == 2

    too_deep = board.register_assignment_nodes(
        "run",
        ({
            "assignment_id": "great-grandchild",
            "parent_assignment_id": "grandchild-0-0",
            "owner_thread_id": "great-grandchild-thread",
            "requirement_ids": ("R1",),
            "objective": "too deep",
            "scope_signature": "too-deep",
        },),
        actor_thread_id="grandchild-thread-0-0",
        lease_seconds=60,
        max_total_assignments=25,
        max_children_per_parent=5,
        max_depth=2,
    )
    assert too_deep["great-grandchild"].reason == "fork_depth_limit_reached"
    assert board.metrics("run")["assignment_count"] == 24


def test_synthesis_requirement_is_excluded_from_evidence_coverage_and_fin006() -> None:
    task = ResearchTask(
        "task",
        "compare",
        context={"research_requirements": [
            {"requirement_id": "R1", "description": "Find facts"},
            {
                "requirement_id": "R2",
                "description": "Synthesize comparison",
                "requires_external_evidence": False,
            },
        ]},
    )
    requirements = build_research_requirements(task)
    assert [item.requirement_id for item in initial_coverage(requirements)] == ["R1"]

    config = yaml.safe_load(Path("configs/researchbench-shared-fin006-legacy.yaml").read_text(encoding="utf-8"))
    plan = shared_comparison_plan_from_config(config)
    assert plan is not None
    assert plan.core_questions[-1].requires_external_evidence is False
    assert len(plan.evidence_requirements) == 4


def test_failed_assessment_after_bounded_children_is_evidence_exhausted() -> None:
    identity = ExecutionIdentity("root-taxonomy", None, "root-taxonomy", 0)
    state = create_research_agent_state(
        ResearchTask("root", "Research requirement"),
        identity,
        AgentLimits(),
    )
    state["observed_evidence"] = [
        EvidenceItem(
            "E1",
            "Verified partial finding.",
            "web",
            "Primary source",
            "https://example.com/source",
            "section:1",
            "Verified partial finding.",
            requirement_id="R1",
        )
    ]
    state["critical_gaps"] = [CriticalGap("R1", "A material gap remains.")]
    state["child_results"] = [
        ResearchResult(
            task_id="child",
            status=ResearchStatus.PARTIAL,
            summary="Partial child result.",
            termination_reason=TerminationReason.BUDGET_FORCED,
            stop_reason="token_budget_exhausted",
        )
    ]

    assessment = _fallback_assessment(
        state,
        attempts=(),
        has_research_tools=True,
        assessment_error=(
            "AssessmentValidationError: continue/replan must change a strategy family"
        ),
    )

    assert assessment.termination_reason is TerminationReason.EVIDENCE_EXHAUSTED
    assert assessment.exhaustion_reason is not None


@pytest.mark.asyncio
async def test_root_initial_wave_keeps_four_external_requirements_separate(tmp_path) -> None:
    def final(label: str) -> dict:
        return {
            "content": json.dumps({
                "status": "completed",
                "summary": label,
                "findings": [label],
                "unresolved": [],
            }),
            "tool_calls": [],
        }

    class ChildPolicy:
        def __call__(self, messages, *, tools=None):
            assessment = assessment_response(messages)
            if assessment is not None:
                return assessment
            if "Choose the next control action" in str(messages[0].get("content") or ""):
                return {"content": json.dumps({
                    "action": "complete",
                    "rationale": "scoped child complete",
                    "target_requirement_ids": [],
                    "fork_candidates": [],
                })}
            return final("child")

    class RootPolicy:
        def fork(self):
            return ChildPolicy()

        def __call__(self, messages, *, tools=None):
            assessment = assessment_response(messages)
            if assessment is not None:
                return assessment
            if "Choose the next control action" in str(messages[0].get("content") or ""):
                payload = json.loads(messages[-1]["content"])
                if not payload["child_results"]:
                    return {"content": json.dumps({
                        "action": "fork",
                        "rationale": "The model bundled four requirements into two tasks.",
                        "target_requirement_ids": ["R1", "R2", "R3", "R4"],
                        "fork_candidates": [
                            {
                                "objective": "bundled requirements 1 and 2",
                                "expected_output": "evidence",
                                "requirement_ids": ["R1", "R2"],
                                "reasons": ["parallel"],
                            },
                            {
                                "objective": "bundled requirements 3 and 4",
                                "expected_output": "evidence",
                                "requirement_ids": ["R3", "R4"],
                                "reasons": ["parallel"],
                            },
                        ],
                    })}
                return {"content": json.dumps({
                    "action": "complete",
                    "rationale": "merge isolated requirement owners",
                    "target_requirement_ids": [],
                    "fork_candidates": [],
                })}
            return final("root")

    board = ResearchBlackboard(tmp_path / "root-wave.sqlite")
    identity = ExecutionIdentity("root-wave", None, "root-wave", 0)
    graph = build_research_agent_graph(
        RootPolicy(),
        [],
        coordination_board=board,
        homogeneous_fork_config=HomogeneousForkConfig(
            enabled=True,
            explicit_control_decision=True,
        ),
    )
    final_state = await graph.ainvoke(
        create_research_agent_state(
            ResearchTask(
                "root",
                "compare four requirements",
                context={"research_requirements": [
                    {"requirement_id": f"R{index}", "description": f"Requirement {index}"}
                    for index in range(1, 5)
                ]},
                require_evidence=False,
            ),
            identity,
            AgentLimits(max_total_agents=10),
        ),
        config={"configurable": {"thread_id": identity.thread_id}},
    )

    nodes = board.snapshot(
        identity.root_thread_id,
        viewer_thread_id=identity.thread_id,
    )["assignment_tree"]
    children = [item for item in nodes if item["depth"] == 1]
    assert len(children) == 4
    assert sorted(item["requirement_ids"] for item in children) == [
        ["R1"], ["R2"], ["R3"], ["R4"]
    ]
    assert final_state["result"].thread_count == 5
    assert final_state["result"].report_markdown.startswith("# Research result")
    assert final_state["child_results"]
    assert all(item.research_memo for item in final_state["child_results"])


@pytest.mark.asyncio
async def test_child_may_fork_first_turn_for_a_concrete_deep_tool_chain(tmp_path) -> None:
    def final(label: str) -> dict:
        return {
            "content": (
                '{"status":"completed","summary":"'
                + label
                + '","findings":["'
                + label
                + '"],"unresolved":[]}'
            ),
            "tool_calls": [],
        }

    class ForbiddenTool:
        name = "web_search"

        def get_openai_tool_schema(self):
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": "must remain unused",
                    "parameters": {"type": "object", "properties": {}},
                },
            }

        async def execute(self, **_):
            raise AssertionError("first-turn child Fork must not require scouting")

    class GrandchildPolicy:
        def __call__(self, messages, *, tools=None):
            assessment = assessment_response(messages)
            if assessment is not None:
                return assessment
            if "Choose the next control action" in str(messages[0].get("content") or ""):
                return {"content": '{"action":"complete","rationale":"done",'
                    '"target_requirement_ids":[],"fork_candidates":[]}'}
            return final("grandchild")

    class ChildPolicy:
        def fork(self):
            return GrandchildPolicy()

        def __call__(self, messages, *, tools=None):
            assessment = assessment_response(messages)
            if assessment is not None:
                return assessment
            if "Choose the next control action" in str(messages[0].get("content") or ""):
                state = json.loads(messages[-1]["content"])
                if state["child_results"]:
                    return {"content": '{"action":"complete","rationale":"merge",'
                        '"target_requirement_ids":[],"fork_candidates":[]}'}
                return {"content": json.dumps({
                    "action": "fork",
                    "rationale": "Concrete three-step source chains can start immediately.",
                    "target_requirement_ids": ["R1"],
                    "fork_candidates": [
                        {
                            "objective": f"grandchild {index}",
                            "expected_output": "evidence",
                            "requirement_ids": ["R1"],
                            "scope_signature": f"scope-{index}",
                            "context": {},
                            "reasons": ["deep_tool_chain"],
                            "estimated_tool_calls": 3,
                            "independent": True,
                        }
                        for index in range(2)
                    ],
                }, ensure_ascii=False)}
            return final("child")

    class RootPolicy:
        def fork(self):
            return ChildPolicy()

        def __call__(self, messages, *, tools=None):
            assessment = assessment_response(messages)
            if assessment is not None:
                return assessment
            if "Choose the next control action" in str(messages[0].get("content") or ""):
                state = json.loads(messages[-1]["content"])
                if state["child_results"]:
                    return {"content": '{"action":"complete","rationale":"merge",'
                        '"target_requirement_ids":[],"fork_candidates":[]}'}
                return {"content": json.dumps({
                    "action": "fork",
                    "rationale": "Isolate the child scope.",
                    "target_requirement_ids": ["R1"],
                    "fork_candidates": [{
                        "objective": "child",
                        "expected_output": "evidence",
                        "requirement_ids": ["R1"],
                        "scope_signature": "child-scope",
                        "context": {},
                        "reasons": ["context_isolation"],
                        "estimated_tool_calls": 0,
                        "independent": True,
                    }],
                }, ensure_ascii=False)}
            return final("root")

    board = ResearchBlackboard(tmp_path / "first-turn.sqlite")
    identity = ExecutionIdentity("root-first-turn", None, "root-first-turn", 0)
    graph = build_research_agent_graph(
        RootPolicy(),
        [ForbiddenTool()],
        coordination_board=board,
        homogeneous_fork_config=HomogeneousForkConfig(
            enabled=True,
            explicit_control_decision=True,
            recursive_fork_min_local_tool_calls=99,
        ),
    )
    final_state = await graph.ainvoke(
        create_research_agent_state(
            ResearchTask(
                "root",
                "objective",
                context={"research_requirements": [
                    {"requirement_id": "R1", "description": "Research fact"},
                ]},
                require_evidence=False,
            ),
            identity,
            AgentLimits(),
        ),
        config={"configurable": {"thread_id": identity.thread_id}},
    )

    assert final_state["result"].thread_count == 4
    assert final_state["result"].tool_calls_used == 0
    assert board.metrics(identity.root_thread_id)["event_fork_called"] == 2
