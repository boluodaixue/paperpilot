"""Phase 2 acceptance tests for the one-layer Blue Research Worker."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any

import pytest

from src.research.models import AgentLimits, EvidenceItem, ExecutionIdentity
from src.research.research_worker import (
    ResearchWorkerState,
    create_research_worker_state,
    run_research_worker,
    _strong_worker_evidence,
)
from src.research.v2_contracts import CoreQuestion, ResearchPlan, WorkPacket
from tests._research_assessment import assessment_response


def _plan_packet(*, max_tool_calls: int = 2, token_budget: int = 80000):
    question = CoreQuestion.create("Verify the model benchmark claim")
    plan = ResearchPlan.create(
        0,
        (question,),
        source_guidance=("Open the source; search snippets are leads only",),
    )
    packet = WorkPacket.create(
        objective="Verify the model benchmark claim",
        question_ids=(question.question_id,),
        expected_output="Source-locatable Evidence Claims",
        source_guidance=plan.source_guidance,
        max_tool_calls=max_tool_calls,
        token_budget=token_budget,
        deadline_at=time.time() + 120,
        wave="initial",
    )
    return plan, packet


def _identity(name: str) -> ExecutionIdentity:
    return ExecutionIdentity(
        thread_id=f"root.worker.{name}",
        parent_thread_id="root",
        root_thread_id="root",
        depth=1,
    )


class WorkerPolicy:
    def __init__(self, *, parent: "WorkerPolicy | None" = None) -> None:
        self.parent = parent
        self.schemas: list[tuple[str, ...]] = []

    def fork(self):
        child = WorkerPolicy(parent=self)
        if not hasattr(self, "children"):
            self.children: list[WorkerPolicy] = []
        self.children.append(child)
        return child

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        names = tuple(
            item.get("function", {}).get("name", "")
            for item in (tools or [])
        )
        self.schemas.append(names)
        if tools == []:
            return {
                "content": json.dumps(
                    {
                        "status": "completed",
                        "summary": "The opened source supports the scoped claim.",
                        "findings": ["The benchmark claim is source-locatable."],
                        "unresolved": [],
                    }
                ),
                "tool_calls": [],
            }
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "browser-call",
                    "type": "function",
                    "function": {
                        "name": "browser",
                        "arguments": json.dumps({"url": "https://example.com/report"}),
                    },
                }
            ],
        }


class BrowserTool:
    name = "browser"
    executed_instance_ids: list[int] = []

    def __deepcopy__(self, memo):
        del memo
        return BrowserTool()

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Open one source",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        }

    async def execute(self, **kwargs):
        type(self).executed_instance_ids.append(id(self))
        return "Official benchmark report section 2: the scoped result is 87.5. " + (
            "supporting context " * 300
        )


class ArtifactStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def persist_tool_artifact(self, artifact_id: str, **kwargs):
        self.calls.append({"artifact_id": artifact_id, **kwargs})
        raw = json.dumps(kwargs["result"], ensure_ascii=False, default=str).encode()
        return {
            "artifact_id": artifact_id,
            "artifact_path": f"Artifacts/root/{artifact_id}.json",
            "content_hash": "a" * 64,
            "size_bytes": len(raw),
        }


def test_worker_state_has_no_child_or_fork_channels() -> None:
    plan, packet = _plan_packet()
    state = create_research_worker_state(
        packet,
        plan,
        _identity("shape"),
        AgentLimits(),
    )

    forbidden = {
        "pending_fork_calls",
        "completed_fork_fingerprints",
        "child_thread_ids",
        "child_results",
    }
    assert forbidden.isdisjoint(ResearchWorkerState.__annotations__)
    assert forbidden.isdisjoint(state)
    assert state["limits"].max_children == 0
    assert state["limits"].max_fork_depth == 1
    assert state["subtree_thread_budget"] == 1


def test_worker_filters_tool_errors_and_irrelevant_generic_title_matches() -> None:
    plan, packet = _plan_packet()
    packet = WorkPacket.create(
        packet.objective,
        packet.question_ids,
        packet.expected_output,
        ("Original question: Compare GPT-4o and Qwen2.5",),
        packet.max_tool_calls,
        packet.token_budget,
        packet.deadline_at,
        packet.wave,
    )
    common = dict(
        source_type="web", locator="section:1", excerpt="opened", requirement_id=packet.question_ids[0],
        action_id="action", artifact_id="artifact",
    )
    evidence = (
        EvidenceItem("bad", "[Browser Error] 403", title="GPT-4o", source_ref="https://bad", **common),
        EvidenceItem("noise", "A generic networking report", title="Technical Report", source_ref="https://noise", **common),
        EvidenceItem("good", "GPT-4o official benchmark result", title="GPT-4o System Card", source_ref="https://good", **common),
    )

    assert tuple(item.evidence_id for item in _strong_worker_evidence(evidence, packet)) == ("good",)


def test_worker_bounds_full_content_blocks_per_source_and_requirement() -> None:
    _, packet = _plan_packet()
    evidence = tuple(
        EvidenceItem(
            f"evidence-{index}",
            f"Official benchmark evidence block {index}",
            "web",
            "Official benchmark report",
            f"https://example.com/source-{index // 4}",
            f"section:{index}",
            f"Opened benchmark evidence block {index}",
            requirement_id=packet.question_ids[0],
            action_id="action",
            artifact_id="artifact",
        )
        for index in range(20)
    )

    selected = _strong_worker_evidence(evidence, packet)

    assert len(selected) == 6
    assert all(
        sum(item.source_ref == candidate.source_ref for item in selected) <= 2
        for candidate in selected
    )


@pytest.mark.asyncio
async def test_worker_has_no_fork_tool_and_returns_claims_from_opened_sources() -> None:
    BrowserTool.executed_instance_ids.clear()
    plan, packet = _plan_packet()
    policy = WorkerPolicy()
    store = ArtifactStore()

    result = await run_research_worker(
        packet,
        plan,
        policy,
        [BrowserTool()],
        identity=_identity("grounded"),
        limits=AgentLimits(),
        tool_artifact_store=store,
    )

    assert len(policy.children) == 1
    assert all("fork_research" not in names for names in policy.children[0].schemas)
    assert result.claims and result.evidence
    assert result.claims[0].question_ids == packet.question_ids
    assert result.claims[0].evidence_ids == (result.evidence[0].evidence_id,)
    assert result.evidence[0].artifact_id == store.calls[0]["artifact_id"]
    assert result.usage.tool_calls == 1
    assert len(str(store.calls[0]["result"])) > 4000


class SearchTool:
    name = "web_search"

    def get_openai_tool_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Search",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **kwargs):
        return {
            "results": [
                {
                    "title": "Search lead",
                    "url": "https://example.com/lead",
                    "snippet": "A search snippet that has not been opened.",
                }
            ]
        }


class SearchPolicy(WorkerPolicy):
    def fork(self):
        return self

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        if tools == []:
            return {
                "content": json.dumps(
                    {
                        "status": "partial",
                        "summary": "Only an unopened lead was found.",
                        "findings": [],
                        "unresolved": ["Open the source"],
                    }
                ),
                "tool_calls": [],
            }
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "search",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        }


@pytest.mark.asyncio
async def test_search_snippets_remain_leads_not_worker_evidence_claims() -> None:
    plan, packet = _plan_packet(max_tool_calls=1)
    result = await run_research_worker(
        packet,
        plan,
        SearchPolicy(),
        [SearchTool()],
        identity=_identity("lead"),
        limits=AgentLimits(max_iterations=1),
    )

    assert result.claims == ()
    assert result.evidence == ()


@pytest.mark.asyncio
async def test_parallel_workers_receive_isolated_policy_and_tool_instances() -> None:
    BrowserTool.executed_instance_ids.clear()
    policy = WorkerPolicy()
    plan, first_packet = _plan_packet()
    second_packet = WorkPacket.create(
        objective=first_packet.objective + " independently",
        question_ids=first_packet.question_ids,
        expected_output=first_packet.expected_output,
        source_guidance=first_packet.source_guidance,
        max_tool_calls=first_packet.max_tool_calls,
        token_budget=first_packet.token_budget,
        deadline_at=first_packet.deadline_at,
        wave="initial",
    )

    await asyncio.gather(
        run_research_worker(
            first_packet,
            plan,
            policy,
            [BrowserTool()],
            identity=_identity("parallel-a"),
            limits=AgentLimits(),
            tool_artifact_store=ArtifactStore(),
        ),
        run_research_worker(
            second_packet,
            plan,
            policy,
            [BrowserTool()],
            identity=_identity("parallel-b"),
            limits=AgentLimits(),
            tool_artifact_store=ArtifactStore(),
        ),
    )

    assert len(policy.children) == 2
    assert policy.children[0] is not policy.children[1]
    assert len(set(BrowserTool.executed_instance_ids)) == 2
