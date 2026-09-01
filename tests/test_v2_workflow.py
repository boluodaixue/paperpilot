"""Phase 6 acceptance tests for the selectable V2 product workflow."""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from src.research.memory import MarkdownMemoryStore
from src.research.models import (
    AgentLimits,
    EvidenceItem,
    ExecutionIdentity,
    OutputStatus,
    ResearchStatus,
)
from src.research.v2_contracts import (
    BlueWorkerResult,
    CitationAuditOutcome,
    CitationIssue,
    EvidenceClaim,
    ResearchArchitecture,
    SupervisorV2Config,
)
from src.research.workflow import (
    _safe_citation_outcome,
    _v2_research_result,
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)
from tests._v2_workflow_fakes import AlignmentPolicy, package


def _config(thread_id):
    return {"configurable": {"thread_id": thread_id}}


def test_deterministic_citation_fallback_removes_only_unsafe_line() -> None:
    _, supervisor, _, _ = package()
    evidence = supervisor.worker_results[0].evidence
    issue = CitationIssue.create(
        "Uncited comparison heading",
        "Result",
        (),
        "missing",
        "medium",
        "add_citation",
    )
    markdown = (
        "# Result\n\nUncited comparison heading\n\n"
        "The official result is 42%. [[EVIDENCE:evidence-v2]]"
    )

    body, audit = _safe_citation_outcome(
        supervisor,
        markdown,
        evidence,
        (issue,),
        (issue.claim_text,),
    )

    assert "Uncited comparison heading" not in body
    assert "The official result is 42%" in body
    assert audit.status == "repaired"
    assert len(audit.issues) == 1
    assert audit.issues[0].status == "removed"
    assert "Citation audit removed or downgraded" not in body
    assert "structured audit ledger" not in audit.issues[0].claim_text


def test_safe_citation_fallback_never_leaves_a_broken_table() -> None:
    _, supervisor, _, _ = package()
    evidence = supervisor.worker_results[0].evidence
    issue = CitationIssue.create(
        "Unsafe header", "Comparison", (), "missing", "medium", "delete"
    )
    markdown = (
        "# Comparison\n\n"
        "| Unsafe header | Value |\n"
        "|---|---|\n"
        "| Safe supported row [[EVIDENCE:evidence-v2]] | 42% |"
    )

    body, audit = _safe_citation_outcome(
        supervisor, markdown, evidence, (issue,), (issue.claim_text,)
    )

    assert "|---|---|" not in body
    assert "- Safe supported row [[EVIDENCE:evidence-v2]] — 42%" in body
    assert audit.status == "repaired"


def test_successful_repairs_are_valid_and_recorded_separately() -> None:
    _, _, challenge, draft = package()
    repaired_draft = draft.__class__(
        **{**draft.__dict__, "output_status": OutputStatus.REPAIRED}
    )
    audit = CitationAuditOutcome(status="repaired", repaired_markdown=draft.markdown)

    result = _v2_research_result(challenge, repaired_draft, audit)

    assert result.status is ResearchStatus.COMPLETED
    assert result.output_status is OutputStatus.VALID
    assert result.repair_applied is True
    assert result.repair_actions == ("composer_safety_repair", "citation_repair")


@pytest.mark.asyncio
async def test_v2_workflow_runs_citation_gate_before_single_persist(monkeypatch, tmp_path) -> None:
    calls = []
    plan, supervisor, challenge, draft = package()

    async def fake_plan(*args, **kwargs):
        calls.append("planning")
        return plan

    async def fake_supervisor(*args, **kwargs):
        calls.append("blue_research")
        return supervisor

    async def fake_challenge(*args, **kwargs):
        calls.append("red_review")
        return challenge

    async def fake_compose(*args, **kwargs):
        calls.append("drafting")
        return draft

    async def fake_audit(*args, **kwargs):
        calls.append("citation_audit")
        return CitationAuditOutcome(status="passed")

    import src.research.workflow as workflow_module
    monkeypatch.setattr(workflow_module, "plan_research", fake_plan)
    monkeypatch.setattr(workflow_module, "run_research_supervisor", fake_supervisor)
    monkeypatch.setattr(workflow_module, "run_research_challenge_loop", fake_challenge)
    monkeypatch.setattr(workflow_module, "compose_report", fake_compose)
    monkeypatch.setattr(workflow_module, "audit_citations", fake_audit)

    graph = build_research_workflow(
        AlignmentPolicy(), (), MarkdownMemoryStore(tmp_path),
        checkpointer=InMemorySaver(),
        research_architecture=ResearchArchitecture.SUPERVISOR_V2,
        supervisor_v2_config=SupervisorV2Config(enabled=True),
    )
    identity = ExecutionIdentity("root-v2-flow", None, "root-v2-flow", 0)
    await graph.ainvoke(
        create_research_workflow_state("Verify it", identity, AgentLimits()),
        config=_config(identity.thread_id),
    )
    final = await resume_research_workflow(graph, thread_id=identity.thread_id, action="confirm")

    assert calls == ["planning", "blue_research", "red_review", "drafting", "citation_audit"]
    assert final["workflow_status"] == "completed"
    assert final["workflow_result"].research_architecture == "supervisor_v2"
    assert "[[EVIDENCE:" not in final["report_markdown"]
    assert "## References" in final["report_markdown"]
    stages = [item["kind"] for item in final["execution_events"]]
    assert stages == ["planning", "blue_research", "red_review", "drafting", "citation_audit", "persisting"]


def test_legacy_and_v2_graphs_are_explicitly_distinct(tmp_path) -> None:
    legacy = build_research_workflow(AlignmentPolicy(), (), MarkdownMemoryStore(tmp_path / "legacy"))
    v2 = build_research_workflow(
        AlignmentPolicy(), (), MarkdownMemoryStore(tmp_path / "v2"),
        research_architecture=ResearchArchitecture.SUPERVISOR_V2,
        supervisor_v2_config=SupervisorV2Config(enabled=True),
    )
    legacy_nodes = set(legacy.get_graph().nodes)
    v2_nodes = set(v2.get_graph().nodes)
    assert "research_agent" in legacy_nodes
    assert "research_agent" not in v2_nodes
    assert {"planning", "blue_research", "red_review", "drafting", "citation_audit"} <= v2_nodes


@pytest.mark.asyncio
async def test_citation_gap_uses_final_insurance_repair_without_new_research(monkeypatch, tmp_path) -> None:
    calls = []
    plan, supervisor, challenge, draft = package()
    original_claim = supervisor.worker_results[0].claims[0]
    original_evidence = supervisor.worker_results[0].evidence[0]

    async def fake_plan(*args, **kwargs):
        return plan

    async def fake_supervisor(*args, **kwargs):
        return supervisor

    async def fake_challenge(*args, **kwargs):
        return challenge

    async def fake_compose(*args, **kwargs):
        calls.append("draft")
        return draft

    audit_calls = 0

    async def fake_audit(*args, **kwargs):
        nonlocal audit_calls
        audit_calls += 1
        calls.append("audit")
        if audit_calls == 1:
            return CitationAuditOutcome(status="issues", issues=(
                CitationIssue.create(
                    original_claim.claim,
                    "Result",
                    (original_evidence.evidence_id,),
                    "missing",
                    "high",
                    "add_citation",
                ),
            ))
        return CitationAuditOutcome(status="passed")

    async def fake_repair(*args, **kwargs):
        calls.append("repair")
        return CitationAuditOutcome(
            status="repaired",
            repaired_markdown=draft.markdown,
        )

    import src.research.workflow as workflow_module
    monkeypatch.setattr(workflow_module, "plan_research", fake_plan)
    monkeypatch.setattr(workflow_module, "run_research_supervisor", fake_supervisor)
    monkeypatch.setattr(workflow_module, "run_research_challenge_loop", fake_challenge)
    monkeypatch.setattr(workflow_module, "compose_report", fake_compose)
    monkeypatch.setattr(workflow_module, "audit_citations", fake_audit)
    monkeypatch.setattr(workflow_module, "repair_citations", fake_repair)

    graph = build_research_workflow(
        AlignmentPolicy(), (), MarkdownMemoryStore(tmp_path),
        checkpointer=InMemorySaver(),
        research_architecture=ResearchArchitecture.SUPERVISOR_V2,
        supervisor_v2_config=SupervisorV2Config(enabled=True),
    )
    identity = ExecutionIdentity("root-citation-followup", None, "root-citation-followup", 0)
    await graph.ainvoke(
        create_research_workflow_state("Verify it", identity, AgentLimits()),
        config=_config(identity.thread_id),
    )
    final = await resume_research_workflow(
        graph, thread_id=identity.thread_id, action="confirm"
    )

    assert calls == ["draft", "audit", "repair"]
    assert final["v2_citation_followup_used"] is False
    assert final["v2_supervisor_outcome"].wave_count == 1
    assert final["workflow_status"] == "completed"
