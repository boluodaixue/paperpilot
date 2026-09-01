"""AgentGraph integration for the passive research blackboard."""

from __future__ import annotations

import json

import pytest

from src.research.agent_graph import (
    _stable_evidence_id,
    build_research_agent_graph,
    create_research_agent_state,
)
from src.research.models import (
    AgentLimits,
    ExecutionIdentity,
    OutputStatus,
    ResearchStatus,
    ResearchTask,
)
from src.research.research_blackboard import ResearchBlackboard
from src.research.research_supervisor import SupervisorBudget, run_research_supervisor
from src.research.v2_contracts import (
    BlueWorkerResult,
    BlueWorkerUsage,
    CoreQuestion,
    EvidenceClaim,
    ResearchPlan,
    SupervisorV2Config,
)
from tests._research_assessment import assessment_response


def _final(summary: str) -> dict:
    return {
        "content": json.dumps({
            "status": "completed",
            "summary": summary,
            "findings": [summary],
            "unresolved": [],
        }),
        "tool_calls": [],
    }


def _fork_call() -> dict:
    return {
        "id": "fork-once",
        "type": "function",
        "function": {
            "name": "fork_research",
            "arguments": json.dumps({
                "candidates": [
                    {
                        "objective": "Research use of proceeds",
                        "expected_output": "Scoped proceeds memo",
                        "requirement_ids": ["R1"],
                        "reasons": ["parallel"],
                        "independent": True,
                    },
                    {
                        "objective": "Research disclosure",
                        "expected_output": "Scoped disclosure memo",
                        "requirement_ids": ["R2"],
                        "reasons": ["parallel"],
                        "independent": True,
                    },
                ]
            }),
        },
    }


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class ChildPolicy:
    def __init__(self, board_views: list[dict]) -> None:
        self.board_views = board_views

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        content = str(messages[-1].get("content") or "")
        if content.startswith("FINAL_SYNTHESIS_SNAPSHOT"):
            return _final("Child completed its assigned requirement.")
        assert content.startswith("RESEARCH_COORDINATION_BOARD")
        self.board_views.append(json.loads(content.split("\n\n", 1)[1]))
        return _final("Child completed its assigned requirement.")


class RootPolicy:
    def __init__(self) -> None:
        self.board_views: list[dict] = []
        self.forked = False

    def fork(self):
        return ChildPolicy(self.board_views)

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        content = str(messages[-1].get("content") or "")
        if content.startswith("FINAL_SYNTHESIS_SNAPSHOT"):
            return _final("Root synthesized homogeneous child results.")
        if not self.forked:
            self.forked = True
            return {"content": "", "tool_calls": [_fork_call()]}
        return _final("Root synthesized homogeneous child results.")


@pytest.mark.asyncio
async def test_natural_fork_children_receive_live_own_and_sibling_scope(tmp_path) -> None:
    board = ResearchBlackboard(tmp_path / "checkpoint.sqlite")
    policy = RootPolicy()
    identity = ExecutionIdentity("root-board", None, "root-board", 0)
    task = ResearchTask(
        "root-task",
        "Compare green bonds and sustainability-linked bonds",
        context={
            "research_plan_id": "fixed-plan",
            "report_outline": ["Use of proceeds", "Disclosure"],
            "research_requirements": [
                {"requirement_id": "R1", "description": "Use of proceeds"},
                {"requirement_id": "R2", "description": "Disclosure"},
            ],
        },
        require_evidence=False,
    )
    graph = build_research_agent_graph(
        policy,
        [],
        coordination_board=board,
    )

    final = await graph.ainvoke(
        create_research_agent_state(task, identity, AgentLimits(max_children=2)),
        config={"configurable": {"thread_id": identity.thread_id}},
    )

    assert len(final["child_thread_ids"]) == 2
    assert len(policy.board_views) == 2
    assert {
        tuple(item["requirement_id"] for item in view["own_assignments"])
        for view in policy.board_views
    } == {("R1",), ("R2",)}
    assert all(len(view["sibling_assignments"]) == 1 for view in policy.board_views)
    root_view = board.snapshot("root-board", viewer_thread_id="root-board")
    assert root_view["requirement_status"] == {"R1": "supported", "R2": "supported"}
    metrics = board.metrics("root-board")
    assert metrics["event_fork_called"] == 1
    assert metrics["event_fork_candidates_evaluated"] == 1


@pytest.mark.asyncio
async def test_blackboard_does_not_force_a_policy_to_fork(tmp_path) -> None:
    board = ResearchBlackboard(tmp_path / "checkpoint.sqlite")

    class LocalPolicy:
        def __call__(self, messages, *, tools=None):
            assessment = assessment_response(messages)
            if assessment is not None:
                return assessment
            return _final("The root deliberately stayed local.")

    identity = ExecutionIdentity("root-local", None, "root-local", 0)
    graph = build_research_agent_graph(
        LocalPolicy(),
        [],
        coordination_board=board,
    )
    final = await graph.ainvoke(
        create_research_agent_state(
            ResearchTask("local", "Stay local", require_evidence=False),
            identity,
            AgentLimits(max_children=2),
        ),
        config={"configurable": {"thread_id": identity.thread_id}},
    )

    assert final["result"].thread_count == 1
    metrics = board.metrics("root-local")
    assert metrics.get("event_fork_called", 0) == 0
    assert metrics["event_fork_not_called"] >= 1


@pytest.mark.asyncio
async def test_child_publishes_incidental_cross_scope_evidence_without_expanding(tmp_path) -> None:
    board = ResearchBlackboard(tmp_path / "checkpoint.sqlite")
    source_ref = "https://authority.gov/combined-standard"
    excerpt = "The official standard also contains a disclosure clause."
    evidence_id = _stable_evidence_id(f"{source_ref}#R1", excerpt)

    class AcquireTool:
        name = "acquire_evidence"
        accepts_relevance_query = True

        def fork(self):
            return self

        def get_openai_tool_schema(self):
            return {
                "type": "function",
                "function": {
                    "name": self.name,
                    "description": "Acquire evidence",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }

        async def execute(self, query: str):
            return {
                "status": "ok",
                "query": query,
                "search_backend": "fixture",
                "selected_urls": [source_ref],
                "documents": [{
                    "url": source_ref,
                    "title": "Combined official standard",
                    "format": "html",
                    "extractor": "fixture",
                    "blocks": [{
                        "heading": "Disclosure",
                        "locator": "section:disclosure",
                        "text": excerpt,
                    }],
                }],
                "fetch_errors": [],
                "metrics": {
                    "candidate_count": 1,
                    "selected_count": 1,
                    "opened_count": 1,
                    "duplicate_candidate_count": 0,
                    "cache_hit_count": 0,
                },
            }

    class SignalingChildPolicy(ChildPolicy):
        def __call__(self, messages, *, tools=None):
            assessment = assessment_response(messages)
            if assessment is not None:
                return assessment
            content = str(messages[-1].get("content") or "")
            if content.startswith("FINAL_SYNTHESIS_SNAPSHOT"):
                return _final("Child completed its assigned requirement.")
            task_payload = next(
                json.loads(str(item["content"]))
                for item in messages
                if item.get("role") == "user"
                and str(item.get("content") or "").startswith("{")
                and "task_id" in str(item.get("content") or "")
            )
            if "proceeds" in task_payload["objective"].casefold():
                return {
                    "content": "",
                    "tool_calls": [
                        _tool_call("acquire", "acquire_evidence", {"query": "official proceeds"}),
                        _tool_call(
                            "signal",
                            "signal_cross_scope",
                            {
                                "evidence_id": evidence_id,
                                "target_requirement_id": "R2",
                                "message": "Incidental disclosure evidence; sibling should reuse it.",
                            },
                        ),
                    ],
                }
            return _final("Disclosure child stayed inside its own scope.")

    class SignalingRootPolicy(RootPolicy):
        def fork(self):
            return SignalingChildPolicy(self.board_views)

    policy = SignalingRootPolicy()
    identity = ExecutionIdentity("root-signal", None, "root-signal", 0)
    task = ResearchTask(
        "root-task",
        "Compare green bonds and sustainability-linked bonds",
        context={
            "research_plan_id": "fixed-plan",
            "research_requirements": [
                {"requirement_id": "R1", "description": "Use of proceeds"},
                {"requirement_id": "R2", "description": "Disclosure"},
            ],
        },
        require_evidence=False,
    )
    graph = build_research_agent_graph(
        policy,
        [AcquireTool()],
        coordination_board=board,
    )

    final = await graph.ainvoke(
        create_research_agent_state(task, identity, AgentLimits(max_children=2)),
        config={"configurable": {"thread_id": identity.thread_id}},
    )

    assert final["result"].thread_count == 3
    root_view = board.snapshot("root-signal", viewer_thread_id="root-signal")
    assert root_view["cross_scope_signals"] == [{
        "signal_id": root_view["cross_scope_signals"][0]["signal_id"],
        "evidence_id": evidence_id,
        "discovered_by": root_view["cross_scope_signals"][0]["discovered_by"],
        "target_requirement_id": "R2",
        "parent_thread_id": "root-signal",
        "message": "Incidental disclosure evidence; sibling should reuse it.",
        "status": "open",
    }]
    query = root_view["recent_queries"][0]
    assert query["requirement_id"] == "R1"
    assert query["query"] == "official proceeds"


@pytest.mark.asyncio
async def test_supervisor_workers_use_the_same_blackboard_contract(tmp_path) -> None:
    board = ResearchBlackboard(tmp_path / "checkpoint.sqlite")
    questions = (
        CoreQuestion.create("Use of proceeds"),
        CoreQuestion.create("Disclosure"),
    )
    plan = ResearchPlan.create(
        0,
        questions,
        report_outline=("Use of proceeds", "Disclosure"),
    )
    worker_views: list[dict] = []

    async def runner(packet, plan_arg, policy, tools, **kwargs):
        del plan_arg, policy, tools
        identity = kwargs["identity"]
        worker_views.append(board.snapshot(
            identity.root_thread_id,
            viewer_thread_id=identity.thread_id,
            own_requirement_ids=packet.question_ids,
        ))
        claims = tuple(
            EvidenceClaim.create(
                f"Supported {question_id}",
                (question_id,),
                (f"evidence-{question_id}",),
                f"https://authority.gov/{question_id}",
                "section:1",
                "Official support",
            )
            for question_id in packet.question_ids
        )
        return BlueWorkerResult(
            packet.packet_id,
            ResearchStatus.COMPLETED,
            "Worker complete",
            claims=claims,
            usage=BlueWorkerUsage(tool_calls=1, estimated_tokens=1000),
            output_status=OutputStatus.VALID,
        )

    identity = ExecutionIdentity("root-supervisor-board", None, "root-supervisor-board", 0)
    outcome = await run_research_supervisor(
        plan,
        policy=object(),
        tools=(),
        identity=identity,
        limits=AgentLimits(),
        settings=SupervisorV2Config(enabled=True, max_initial_workers=2),
        budget=SupervisorBudget(12, 120000, 9999999999.0),
        worker_runner=runner,
        coordination_board=board,
    )

    assert len(worker_views) == 2
    assert all(len(view["own_assignments"]) == 1 for view in worker_views)
    assert all(len(view["sibling_assignments"]) == 1 for view in worker_views)
    assert set(outcome.resolved_question_ids) == {item.question_id for item in questions}
    root_view = board.snapshot(identity.root_thread_id, viewer_thread_id=identity.thread_id)
    assert set(root_view["requirement_status"].values()) == {"completed"}
    assert board.metrics(identity.root_thread_id)["event_supervisor_packets_assigned"] == 1
