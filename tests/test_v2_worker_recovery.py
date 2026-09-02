"""Recovery and reliability tests for the V2 Blue Worker."""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver

import pytest

from src.research.models import AgentLimits
from src.research.research_worker import run_research_worker
from tests.test_v2_worker import (
    ArtifactStore,
    BrowserTool,
    WorkerPolicy,
    _identity,
    _plan_packet,
)


@pytest.mark.asyncio
async def test_completed_worker_checkpoint_does_not_repeat_tool_or_artifact_write() -> None:
    BrowserTool.executed_instance_ids.clear()
    plan, packet = _plan_packet()
    saver = InMemorySaver()
    store = ArtifactStore()
    identity = _identity("recovery")

    first = await run_research_worker(
        packet,
        plan,
        WorkerPolicy(),
        [BrowserTool()],
        identity=identity,
        limits=AgentLimits(),
        checkpointer=saver,
        tool_artifact_store=store,
    )
    second = await run_research_worker(
        packet,
        plan,
        WorkerPolicy(),
        [BrowserTool()],
        identity=identity,
        limits=AgentLimits(),
        checkpointer=saver,
        tool_artifact_store=store,
    )

    assert second == first
    assert len(BrowserTool.executed_instance_ids) == 1
    assert len(store.calls) == 1
    assert first.evidence[0].artifact_id == store.calls[0]["artifact_id"]


class QuotaTool:
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
        raise RuntimeError("quota exhausted: no credits remaining")


class QuotaPolicy(WorkerPolicy):
    def fork(self):
        return self

    def __call__(self, messages, *, tools=None):
        from tests._research_assessment import assessment_response

        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        if tools == []:
            return {
                "content": '{"status":"partial","summary":"Search unavailable","findings":[],"unresolved":[]}',
                "tool_calls": [],
            }
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "quota",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        }


@pytest.mark.asyncio
async def test_worker_preserves_service_unavailable_alerts_and_tool_budget() -> None:
    plan, packet = _plan_packet(max_tool_calls=1)
    result = await run_research_worker(
        packet,
        plan,
        QuotaPolicy(),
        [QuotaTool()],
        identity=_identity("quota"),
        limits=AgentLimits(max_iterations=1, max_retries_per_action=0),
    )

    assert result.usage.tool_calls == 1
    assert result.alerts
    assert result.alerts[0].circuit_open is True
    assert result.claims == ()
