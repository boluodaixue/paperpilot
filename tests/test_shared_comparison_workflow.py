"""End-to-end shared report path leaves orchestration as the only graph change."""

from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.research.memory import MarkdownMemoryStore
from src.research.models import AgentLimits, ExecutionIdentity
from src.research.research_blackboard import ResearchBlackboard
from src.research.v2_contracts import (
    CoreQuestion,
    ResearchArchitecture,
    ResearchPlan,
    SupervisorV2Config,
)
from src.research.workflow import (
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)
from tests._research_assessment import assessment_response


class FixedAcquireTool:
    name = "acquire_evidence"
    accepts_relevance_query = True

    def fork(self):
        return self

    def get_openai_tool_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Acquire one official source",
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
            "selected_urls": ["https://authority.gov/report"],
            "documents": [{
                "url": "https://authority.gov/report",
                "title": "Official report",
                "format": "html",
                "extractor": "fixture",
                "blocks": [{
                    "heading": "Result",
                    "locator": "section:result",
                    "text": "The official measured result is 42 percent.",
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


class SharedPolicy:
    def __init__(self) -> None:
        self.audit_evidence_counts: list[int] = []

    def __call__(self, messages, *, tools=None):
        content = str(messages[-1].get("content") or "")
        system = str(messages[0].get("content") or "")
        if "before research begins" in system:
            return {"content": json.dumps({
                "objective": "Verify the official measured result",
                "scope": ["Official result"],
                "directions": ["Verify the measured result"],
                "constraints": ["Use primary sources"],
                "expected_output": "Evidence-backed answer",
            })}
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        if content.startswith("FINAL_SYNTHESIS_SNAPSHOT"):
            return {"content": json.dumps({
                "status": "completed",
                "summary": "The official result is verified.",
                "findings": ["The official measured result is 42 percent."],
                "unresolved": [],
            })}
        if "Lead Researcher composing" in system:
            payload = json.loads(content)
            claim = payload["selected_claims"][0]
            return {"content": json.dumps({
                "sections": [{
                    "heading": "Result",
                    "assertions": [{
                        "text": claim["claim"],
                        "claim_ids": [claim["claim_id"]],
                    }],
                }],
                "unresolved": [],
            })}
        if "Audit whether each material statement" in system:
            self.audit_evidence_counts.append(len(json.loads(content)["evidence"]))
            return {"content": json.dumps({"issues": []})}
        if any(item.get("role") == "tool" for item in messages):
            return {"content": json.dumps({
                "status": "completed",
                "summary": "The official result is verified.",
                "findings": ["The official measured result is 42 percent."],
                "unresolved": [],
            })}
        return {
            "content": "",
            "tool_calls": [{
                "id": "acquire",
                "type": "function",
                "function": {
                    "name": "acquire_evidence",
                    "arguments": json.dumps({"query": "official measured result"}),
                },
            }],
        }


def _plan() -> ResearchPlan:
    return ResearchPlan.create(
        1,
        (CoreQuestion.create(
            "Verify the measured result",
            origin="fixed_comparison",
        ),),
        report_outline=("Result",),
        source_guidance=("Use primary sources",),
    )


async def _run(tmp_path, architecture: ResearchArchitecture):
    name = architecture.value
    identity = ExecutionIdentity(f"root-{name}", None, f"root-{name}", 0)
    policy = SharedPolicy()
    graph = build_research_workflow(
        policy,
        (FixedAcquireTool(),),
        MarkdownMemoryStore(tmp_path / name / "vault"),
        checkpointer=InMemorySaver(),
        research_architecture=architecture,
        supervisor_v2_config=SupervisorV2Config(
            enabled=architecture is ResearchArchitecture.SUPERVISOR_V2,
            red_review_enabled=False,
            max_research_waves=1,
        ),
        research_blackboard=ResearchBlackboard(tmp_path / name / "checkpoint.sqlite"),
        shared_comparison_plan=_plan(),
    )
    config = {"configurable": {"thread_id": identity.thread_id}}
    await graph.ainvoke(
        create_research_workflow_state(
            "What is the official measured result?",
            identity,
            AgentLimits(max_elapsed_seconds=1200.0),
        ),
        config=config,
    )
    final = await resume_research_workflow(
        graph,
        thread_id=identity.thread_id,
        action="confirm",
    )
    return final, policy


@pytest.mark.asyncio
async def test_legacy_and_supervisor_share_plan_composer_and_citation_path(tmp_path) -> None:
    (legacy, legacy_policy) = await _run(tmp_path, ResearchArchitecture.LEGACY)
    (supervisor, supervisor_policy) = await _run(
        tmp_path, ResearchArchitecture.SUPERVISOR_V2
    )

    assert legacy["v2_plan"] == supervisor["v2_plan"] == _plan()
    assert legacy["v2_report_body"] == supervisor["v2_report_body"]
    assert legacy["shared_selected_evidence_ids"] == supervisor[
        "shared_selected_evidence_ids"
    ]
    assert legacy["workflow_result"].research_architecture == "legacy"
    assert supervisor["workflow_result"].research_architecture == "supervisor_v2"
    assert 'architecture: "legacy"' in legacy["report_markdown"]
    assert 'architecture: "supervisor_v2"' in supervisor["report_markdown"]
    assert "## References" in legacy["report_markdown"]
    assert "## References" in supervisor["report_markdown"]
    assert "Unresolved Red-team issues" not in legacy["report_markdown"]
    assert "Unresolved Red-team issues" not in supervisor["report_markdown"]
    assert legacy_policy.audit_evidence_counts == [
        len(legacy["shared_selected_evidence_ids"])
    ]
    assert supervisor_policy.audit_evidence_counts == [
        len(supervisor["shared_selected_evidence_ids"])
    ]
    legacy_nodes = set(legacy.keys())
    supervisor_nodes = set(supervisor.keys())
    assert "v2_citation_audit" in legacy_nodes & supervisor_nodes
