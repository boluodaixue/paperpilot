"""Phase 6 checkpoint recovery test across all V2 workflow stages."""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
import pytest

from src.research.memory import MarkdownMemoryStore
from src.research.models import ExecutionIdentity
from src.research.v2_contracts import CitationAuditOutcome, ResearchArchitecture, SupervisorV2Config
from src.research.workflow import build_research_workflow, create_research_workflow_state, resume_research_workflow
from tests._v2_workflow_fakes import AlignmentPolicy, package


@pytest.mark.asyncio
async def test_completed_v2_checkpoint_rebuild_does_not_repeat_stage_calls(monkeypatch, tmp_path) -> None:
    saver = InMemorySaver()
    counts = {key: 0 for key in ("plan", "blue", "red", "draft", "audit")}
    plan, supervisor, challenge, draft = package()

    def stub(name, value):
        async def inner(*args, **kwargs):
            counts[name] += 1
            return value
        return inner

    import src.research.workflow as workflow_module
    monkeypatch.setattr(workflow_module, "plan_research", stub("plan", plan))
    monkeypatch.setattr(workflow_module, "run_research_supervisor", stub("blue", supervisor))
    monkeypatch.setattr(workflow_module, "run_research_challenge_loop", stub("red", challenge))
    monkeypatch.setattr(workflow_module, "compose_report", stub("draft", draft))
    monkeypatch.setattr(workflow_module, "audit_citations", stub("audit", CitationAuditOutcome(status="passed")))

    def build():
        return build_research_workflow(
            AlignmentPolicy(), (), MarkdownMemoryStore(tmp_path), checkpointer=saver,
            research_architecture=ResearchArchitecture.SUPERVISOR_V2,
            supervisor_v2_config=SupervisorV2Config(enabled=True),
        )

    identity = ExecutionIdentity("root-v2-recovery", None, "root-v2-recovery", 0)
    first_graph = build()
    await first_graph.ainvoke(
        create_research_workflow_state("Verify it", identity),
        config={"configurable": {"thread_id": identity.thread_id}},
    )
    first = await resume_research_workflow(first_graph, thread_id=identity.thread_id, action="confirm")
    rebuilt = build()
    second = await rebuilt.ainvoke(None, config={"configurable": {"thread_id": identity.thread_id}})

    assert counts == {"plan": 1, "blue": 1, "red": 1, "draft": 1, "audit": 1}
    assert second["report_markdown"] == first["report_markdown"]
    assert second["workflow_status"] == "completed"
