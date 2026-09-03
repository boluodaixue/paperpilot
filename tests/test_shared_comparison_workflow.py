"""End-to-end shared report path leaves orchestration as the only graph change."""

from __future__ import annotations

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.research.memory import MarkdownMemoryStore
from src.research.memory_dialogue import answer_memory as answer_from_memory
from src.research.models import (
    AgentLimits,
    EvidenceItem,
    ExecutionIdentity,
    OutputStatus,
    ResearchResult,
    ResearchStatus,
    TerminationReason,
)
from src.research.obsidian import build_obsidian_open_uri
from src.research.research_blackboard import ResearchBlackboard
from src.research.retrieval import MarkdownMemoryIndex
from src.research.v2_contracts import (
    CoreQuestion,
    ResearchArchitecture,
    ResearchPlan,
    SupervisorV2Config,
)
from src.research.workflow import (
    _drop_unknown_root_evidence_markers,
    _preserve_legacy_research_state,
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
        self.composer_calls = 0

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
        if "PaperPilot Research Planner" in system:
            return {"content": json.dumps({
                "core_questions": ["Verify the official measured result"],
                "report_outline": ["Result"],
                "source_guidance": ["Use primary sources"],
                "work_hints": [],
            })}
        if "Answer only from the supplied selected-Memory notes" in system:
            context = json.loads(content.split("MEMORY_CONTEXT_JSON:\n", 1)[1])
            return {"content": json.dumps({
                "claims": [{
                    "text": "The stored research reports a measured result of 42 percent.",
                    "source_paths": [context["hits"][0]["path"]],
                }],
                "insufficient_evidence": [],
            })}
        if "independent evidence-support verifier" in system:
            payload = json.loads(content)
            return {"content": json.dumps({
                "assessments": [
                    {
                        "candidate_id": item["candidate_id"],
                        "verdict": "entailed",
                        "confidence": 0.99,
                        "supported_scope": item["text"],
                        "unsupported_scope": "",
                        "reason": "The official Passage directly supports the Claim.",
                    }
                    for item in payload["candidates"]
                ]
            })}
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        if content.startswith("FINAL_SYNTHESIS_SNAPSHOT"):
            state = json.loads(content.split("STATE:\n", 1)[1])
            marker = (
                f" [[EVIDENCE:{state['evidence'][0]['evidence_id']}]]"
                if state.get("evidence") else ""
            )
            return {"content": json.dumps({
                "status": "completed",
                "summary": "The official result is verified.",
                "findings": ["The official measured result is 42 percent."],
                "unresolved": [],
                "research_memo": "",
                "report_markdown": (
                    "# Root report\n\n## Result\n\n"
                    "The official measured result is 42 percent."
                    + marker
                ),
            })}
        if "Lead Researcher composing" in system:
            self.composer_calls += 1
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


@pytest.mark.asyncio
async def test_dynamic_legacy_plan_uses_root_agent_report_path(tmp_path) -> None:
    identity = ExecutionIdentity("root-dynamic-structured", None, "root-dynamic-structured", 0)
    policy = SharedPolicy()
    memory_id = "M-current-baseline"
    store = MarkdownMemoryStore(tmp_path / "dynamic" / "vault")
    store.create_memory("Current baseline", memory_id)
    graph = build_research_workflow(
        policy,
        (FixedAcquireTool(),),
        store,
        checkpointer=InMemorySaver(),
        research_architecture=ResearchArchitecture.LEGACY,
        research_blackboard=ResearchBlackboard(
            tmp_path / "dynamic" / "checkpoint.sqlite"
        ),
        structured_report_enabled=True,
    )
    config = {"configurable": {"thread_id": identity.thread_id}}
    await graph.ainvoke(
        create_research_workflow_state(
            "What is the official measured result?",
            identity,
            AgentLimits(max_elapsed_seconds=1200.0),
            memory_id=memory_id,
        ),
        config=config,
    )
    final = await resume_research_workflow(
        graph,
        thread_id=identity.thread_id,
        action="confirm",
    )

    assert len(final["v2_plan"].core_questions) == 1
    assert final["v2_plan"].core_questions[0].origin == "dynamic_plan"
    assert final["workflow_result"].structured_report is True
    assert final["workflow_result"].root_agent_report is True
    assert final["workflow_result"].shared_comparison is False
    assert final["workflow_result"].memory_id == memory_id
    assert final["result"].report_markdown
    assert "# Root report" in final["result"].report_markdown
    assert "## References" in final["report_markdown"]
    assert "## Evidence-backed Details" not in final["report_markdown"]
    assert "shared_selected_evidence_ids" not in final
    assert final.get("v2_citation_audit") is None
    assert policy.audit_evidence_counts == []
    assert policy.composer_calls == 0
    assert final["workflow_result"].coordination_metrics[
        "evidence_lineage_count"
    ] == 0

    manifest = final["workflow_result"].memory_manifest
    assert manifest.report_path.startswith(f"Memories/{memory_id}/reports/")
    assert len(manifest.evidence_paths) == len(manifest.source_paths) == 1
    assert f"[[{manifest.evidence_paths[0][:-3]}|Evidence 1]]" in final["report_markdown"]
    evidence_markdown = store.read_text(manifest.evidence_paths[0])
    assert f"[[{manifest.source_paths[0][:-3]}|" in evidence_markdown
    assert build_obsidian_open_uri(store.root, manifest.report_path).startswith(
        "obsidian://open?"
    )

    hits = MarkdownMemoryIndex(store).search(memory_id, "measured result 42 percent")
    assert hits
    answer = await answer_from_memory(
        store,
        policy,
        memory_id,
        "What measured result is stored?",
    )
    assert answer.citations
    assert answer.citations[0].relative_path.startswith(f"Memories/{memory_id}/")

    continued_identity = ExecutionIdentity(
        "root-dynamic-continued",
        None,
        "root-dynamic-continued",
        0,
    )
    continued_config = {
        "configurable": {"thread_id": continued_identity.thread_id}
    }
    continued = await graph.ainvoke(
        create_research_workflow_state(
            "Continue researching the official measured result.",
            continued_identity,
            AgentLimits(max_elapsed_seconds=1200.0),
            memory_id=memory_id,
        ),
        config=continued_config,
    )
    assert continued["retrieved_memory"]
    continued_final = await resume_research_workflow(
        graph,
        thread_id=continued_identity.thread_id,
        action="confirm",
    )
    assert continued_final["workflow_result"].memory_id == memory_id
    assert continued_final["workflow_result"].memory_manifest.report_path != manifest.report_path


def test_root_report_drops_only_unknown_evidence_markers() -> None:
    item = EvidenceItem(
        evidence_id="evidence-known-id",
        finding="The official measured result is 42 percent.",
        source_type="primary",
        title="Official report",
        source_ref="https://authority.gov/report",
        locator="section:result",
    )
    markdown = (
        "Supported [[EVIDENCE:evidence-known-id]]. "
        "Bare hash [[EVIDENCE:known-id]]. "
        "Uncited statement [[EVIDENCE:missing-id]]."
    )

    cleaned = _drop_unknown_root_evidence_markers(markdown, (item,))

    assert cleaned.count("[[EVIDENCE:evidence-known-id]]") == 2
    assert "[[EVIDENCE:known-id]]" not in cleaned
    assert "missing-id" not in cleaned
    assert "Uncited statement" in cleaned


def test_structured_report_cannot_upgrade_budget_forced_legacy_result() -> None:
    structured = ResearchResult(
        "structured",
        ResearchStatus.COMPLETED,
        "Valid cited report",
        termination_reason=TerminationReason.BUDGET_FORCED,
        output_status=OutputStatus.VALID,
    )
    legacy = ResearchResult(
        "legacy",
        ResearchStatus.PARTIAL,
        "Useful partial research",
        stop_reason="token_budget_exhausted",
        termination_reason=TerminationReason.BUDGET_FORCED,
        thread_count=6,
    )

    preserved = _preserve_legacy_research_state(structured, legacy)

    assert preserved.status is ResearchStatus.PARTIAL
    assert preserved.stop_reason == "token_budget_exhausted"
    assert preserved.termination_reason is TerminationReason.BUDGET_FORCED
    assert preserved.thread_count == 6
