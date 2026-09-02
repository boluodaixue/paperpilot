"""AgentGraph routes explicit control decisions without forcing Fork."""

from __future__ import annotations

import json

import pytest

from src.research.agent_graph import build_research_agent_graph, create_research_agent_state
from src.research.models import AgentLimits, ExecutionIdentity, ResearchTask
from src.research.research_blackboard import ResearchBlackboard
from src.research.research_control import HomogeneousForkConfig
from tests._research_assessment import assessment_response


def _final(text: str) -> dict:
    return {
        "content": json.dumps({
            "status": "completed",
            "summary": text,
            "findings": [text],
            "unresolved": [],
        }),
        "tool_calls": [],
    }


class ChildControlPolicy:
    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        system = str(messages[0].get("content") or "")
        if "Choose the next control action" in system:
            return {"content": json.dumps({
                "action": "complete",
                "rationale": "The scoped child task is complete.",
                "target_requirement_ids": [],
                "fork_candidates": [],
            })}
        if str(messages[-1].get("content") or "").startswith("FINAL_SYNTHESIS_SNAPSHOT"):
            return _final("Child completed its scope.")
        raise AssertionError("child research tools must not be called in this fixture")


class RootControlPolicy:
    def fork(self):
        return ChildControlPolicy()

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        system = str(messages[0].get("content") or "")
        if "Choose the next control action" in system:
            state = json.loads(messages[-1]["content"])
            if state["child_results"]:
                return {"content": json.dumps({
                    "action": "complete",
                    "rationale": "Merge completed child scopes.",
                    "target_requirement_ids": [],
                    "fork_candidates": [],
                })}
            return {"content": json.dumps({
                "action": "fork",
                "rationale": "R1 and R2 are independent parallel research directions.",
                "target_requirement_ids": ["R1", "R2"],
                "fork_candidates": [
                    {
                        "objective": "Research R1",
                        "expected_output": "Evidence for R1",
                        "requirement_ids": ["R1"],
                        "context": {},
                        "reasons": ["parallel"],
                        "estimated_tool_calls": 1,
                        "independent": True,
                    },
                    {
                        "objective": "Research R2",
                        "expected_output": "Evidence for R2",
                        "requirement_ids": ["R2"],
                        "context": {},
                        "reasons": ["parallel"],
                        "estimated_tool_calls": 1,
                        "independent": True,
                    },
                ],
            })}
        if str(messages[-1].get("content") or "").startswith("FINAL_SYNTHESIS_SNAPSHOT"):
            return _final("Root merged child scopes.")
        raise AssertionError("root must make a control decision before research")


@pytest.mark.asyncio
async def test_explicit_decision_naturally_forks_and_records_rationale(tmp_path) -> None:
    board = ResearchBlackboard(tmp_path / "board.sqlite")
    identity = ExecutionIdentity("root-control", None, "root-control", 0)
    task = ResearchTask(
        "root-task",
        "Compare two independent requirements",
        context={
            "research_requirements": [
                {"requirement_id": "R1", "description": "Direction one"},
                {"requirement_id": "R2", "description": "Direction two"},
            ]
        },
        require_evidence=False,
    )
    graph = build_research_agent_graph(
        RootControlPolicy(),
        [],
        coordination_board=board,
        homogeneous_fork_config=HomogeneousForkConfig(
            enabled=True,
            explicit_control_decision=True,
            budget_leases_enabled=True,
        ),
    )

    final = await graph.ainvoke(
        create_research_agent_state(task, identity, AgentLimits(max_children=2)),
        config={"configurable": {"thread_id": identity.thread_id}},
    )

    assert final["result"].thread_count == 3
    assert final["control_action"] == "fork"
    assert "independent parallel" in final["control_rationale"]
    metrics = board.metrics(identity.root_thread_id)
    assert metrics["event_fork_called"] == 1
    assert metrics["event_fork_candidates_evaluated"] == 1
    assert metrics["event_budget_lease_granted"] == 2
    assert metrics["event_budget_lease_released"] == 2
    assert metrics["active_budget_lease_count"] == 0


@pytest.mark.asyncio
async def test_invalid_control_decision_falls_back_to_one_local_round(tmp_path) -> None:
    class InvalidThenLocalPolicy:
        def __init__(self) -> None:
            self.control_calls = 0

        def __call__(self, messages, *, tools=None):
            assessment = assessment_response(messages)
            if assessment is not None:
                return assessment
            system = str(messages[0].get("content") or "")
            if "Choose the next control action" in system:
                self.control_calls += 1
                return {"content": "not-json"}
            return _final("Local fallback remained bounded.")

    policy = InvalidThenLocalPolicy()
    identity = ExecutionIdentity("root-invalid-control", None, "root-invalid-control", 0)
    graph = build_research_agent_graph(
        policy,
        [],
        coordination_board=ResearchBlackboard(tmp_path / "board.sqlite"),
        homogeneous_fork_config=HomogeneousForkConfig(
            enabled=True,
            explicit_control_decision=True,
        ),
    )
    final = await graph.ainvoke(
        create_research_agent_state(
            ResearchTask("local", "Stay local", require_evidence=False),
            identity,
            AgentLimits(),
        ),
        config={"configurable": {"thread_id": identity.thread_id}},
    )

    assert final["result"].thread_count == 1
    assert final["control_decision_error"] is not None
    assert final["local_rounds_since_fork"] >= 1


@pytest.mark.asyncio
async def test_child_can_top_up_soft_lease_before_finalization(tmp_path) -> None:
    class HeavyChildPolicy:
        def __call__(self, messages, *, tools=None):
            assessment = assessment_response(messages)
            if assessment is not None:
                return assessment
            system = str(messages[0].get("content") or "")
            if "Choose the next control action" in system:
                return {
                    "content": json.dumps({
                        "action": "local_research",
                        "rationale": "Use one bounded local synthesis round.",
                        "target_requirement_ids": [],
                        "fork_candidates": [],
                    }),
                    "usage": {"total_tokens": 35000},
                }
            if str(messages[-1].get("content") or "").startswith(
                "FINAL_SYNTHESIS_SNAPSHOT"
            ):
                response = _final("Heavy child completed after a top-up.")
                response["usage"] = {"total_tokens": 1000}
                return response
            response = _final("Heavy child gathered its scoped result.")
            response["usage"] = {"total_tokens": 10000}
            return response

    class HeavyRootPolicy(RootControlPolicy):
        def fork(self):
            return HeavyChildPolicy()

    board = ResearchBlackboard(tmp_path / "board.sqlite")
    identity = ExecutionIdentity("root-topup", None, "root-topup", 0)
    task = ResearchTask(
        "root-task",
        "Compare two independent requirements",
        context={
            "research_requirements": [
                {"requirement_id": "R1", "description": "Direction one"},
                {"requirement_id": "R2", "description": "Direction two"},
            ]
        },
        require_evidence=False,
    )
    graph = build_research_agent_graph(
        HeavyRootPolicy(),
        [],
        coordination_board=board,
        homogeneous_fork_config=HomogeneousForkConfig(
            enabled=True,
            explicit_control_decision=True,
            budget_leases_enabled=True,
            initial_child_lease_tokens=60000,
            child_topup_tokens=25000,
            max_child_lease_tokens=125000,
        ),
    )

    final = await graph.ainvoke(
        create_research_agent_state(task, identity, AgentLimits(max_children=2)),
        config={"configurable": {"thread_id": identity.thread_id}},
    )

    assert final["result"].thread_count == 3
    metrics = board.metrics(identity.root_thread_id)
    assert metrics["event_budget_lease_topped_up"] == 2
    assert metrics["event_budget_lease_released"] == 2
    assert metrics["active_budget_lease_count"] == 0


@pytest.mark.asyncio
async def test_control_structure_repair_preserves_original_fork_intent(tmp_path) -> None:
    class RepairingRootPolicy(RootControlPolicy):
        def __init__(self) -> None:
            self.control_calls = 0

        def __call__(self, messages, *, tools=None):
            assessment = assessment_response(messages)
            if assessment is not None:
                return assessment
            system = str(messages[0].get("content") or "")
            if "Choose the next control action" in system:
                self.control_calls += 1
                payload = {
                    "action": "fork",
                    "rationale": "The same two independent scopes should run in parallel.",
                    "target_requirement_ids": ["R1", "R2"],
                    "fork_candidates": [
                        {
                            "objective": "Research R1",
                            "expected_output": "Evidence for R1",
                            "requirement_ids": ["R1"],
                            "reasons": ["parallel"],
                            "estimated_tool_calls": 1,
                            "independent": True,
                        },
                        {
                            "objective": "Research R2",
                            "expected_output": "Evidence for R2",
                            "requirement_ids": ["R2"],
                            "reasons": ["parallel"],
                            "estimated_tool_calls": 1,
                            "independent": True,
                        },
                    ],
                }
                if self.control_calls == 1:
                    payload["fork_candidates"][0]["reasons"] = ["parallel_work"]
                    payload["fork_candidates"][1]["reasons"] = ["parallel_work"]
                return {"content": json.dumps(payload)}
            if str(messages[-1].get("content") or "").startswith(
                "FINAL_SYNTHESIS_SNAPSHOT"
            ):
                return _final("Root merged repaired Fork children.")
            raise AssertionError("unexpected root call")

    board = ResearchBlackboard(tmp_path / "board.sqlite")
    identity = ExecutionIdentity("root-repair", None, "root-repair", 0)
    task = ResearchTask(
        "root-task",
        "Compare two independent requirements",
        context={
            "research_requirements": [
                {"requirement_id": "R1", "description": "Direction one"},
                {"requirement_id": "R2", "description": "Direction two"},
            ]
        },
        require_evidence=False,
    )
    graph = build_research_agent_graph(
        RepairingRootPolicy(),
        [],
        coordination_board=board,
        homogeneous_fork_config=HomogeneousForkConfig(
            enabled=True,
            explicit_control_decision=True,
        ),
    )

    final = await graph.ainvoke(
        create_research_agent_state(task, identity, AgentLimits(max_children=2)),
        config={"configurable": {"thread_id": identity.thread_id}},
    )

    assert final["result"].thread_count == 3
    assert final["control_action"] == "fork"
    assert final["control_repair_applied"] is True
    assert board.metrics(identity.root_thread_id)["event_fork_called"] == 1
