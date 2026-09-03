"""N2 acceptance tests for user confirmation and Markdown Memory Store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.research import (
    AgentLimits,
    ExecutionIdentity,
    MarkdownMemoryStore,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
    ResearchTask,
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)
from src.research.models import EvidenceItem
from tests._research_assessment import assessment_response


def _tool_call() -> dict[str, Any]:
    return {
        "id": "call-web-search",
        "type": "function",
        "function": {
            "name": "web_search",
            "arguments": json.dumps({"query": "transformer"}),
        },
    }


class FixedWebTool:
    name = "web_search"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fixed offline web search",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "results": [
                {
                    "title": "Attention Is All You Need",
                    "url": "https://arxiv.org/abs/1706.03762",
                    "snippet": "The Transformer relies on attention rather than recurrence.",
                }
            ]
        }


class WorkflowPolicy:
    """One policy serves both root alignment and the homogeneous research graph."""

    def __init__(self) -> None:
        self.alignment_calls = 0
        self.research_calls = 0

    def __call__(self, messages, *, tools=None):
        content = str(messages[-1].get("content") or "")
        if content.startswith("ASSESS_RESEARCH_STATE"):
            state = json.loads(content.split("STATE:\n", 1)[1])
            evidence_by_requirement = {
                requirement["requirement_id"]: [
                    item["evidence_id"]
                    for item in state["evidence"]
                    if not item.get("requirement_id") or item.get("requirement_id") == requirement["requirement_id"]
                ]
                for requirement in state["requirements"]
            }
            missing = [
                requirement
                for requirement in state["requirements"]
                if not evidence_by_requirement[requirement["requirement_id"]]
            ]
            if missing:
                target = missing[0]
                return {
                    "content": json.dumps(
                        {
                            "decision": "continue",
                            "coverage": [
                                {
                                    "requirement_id": requirement["requirement_id"],
                                    "status": (
                                        "supported"
                                        if evidence_by_requirement[requirement["requirement_id"]]
                                        else "unsupported"
                                    ),
                                    "evidence_ids": evidence_by_requirement[requirement["requirement_id"]],
                                    "rationale": "Scoped fixture coverage.",
                                    "remaining_gap": (
                                        None
                                        if evidence_by_requirement[requirement["requirement_id"]]
                                        else "Evidence is missing."
                                    ),
                                }
                                for requirement in state["requirements"]
                            ],
                            "critical_gaps": [
                                {
                                    "requirement_id": target["requirement_id"],
                                    "reason": "Evidence is missing.",
                                    "impact": "high",
                                }
                            ],
                            "next_actions": [
                                {
                                    "requirement_id": target["requirement_id"],
                                    "strategy": "primary_document",
                                    "query": target["description"],
                                    "expected_value": "high",
                                    "expected_improvement": "Support the missing requirement.",
                                }
                            ],
                            "termination_reason": None,
                            "replan_reason": None,
                            "exhaustion_reason": None,
                        }
                    ),
                    "tool_calls": [],
                }
            assessment = assessment_response(messages)
            assert assessment is not None
            return assessment
        system = str(messages[0].get("content", ""))
        if "before research begins" in system:
            self.alignment_calls += 1
            conversation = "\n".join(str(message.get("content", "")) for message in messages)
            revised = "peer-reviewed papers" in conversation
            payload = {
                "objective": (
                    "Compare Transformer evidence using peer-reviewed papers"
                    if revised
                    else "Explain the evidence behind the Transformer architecture"
                ),
                "scope": ["architecture", "empirical evidence"],
                "directions": (
                    ["peer-reviewed papers", "compare reported results"]
                    if revised
                    else ["original paper", "subsequent evaluations"]
                ),
                "constraints": ["cite source locations"],
                "expected_output": "A concise evidence-backed Markdown report",
            }
            return {"content": json.dumps(payload), "tool_calls": []}

        self.research_calls += 1
        if messages[-1]["role"] == "tool" or tools == []:
            return {
                "content": json.dumps(
                    {
                        "status": "completed",
                        "summary": "Attention replaced recurrence in the Transformer.",
                        "findings": ["The original Transformer architecture is attention-based."],
                        "unresolved": [],
                    }
                ),
                "tool_calls": [],
            }
        return {"content": "", "tool_calls": [_tool_call()]}


class FlakyMemoryStore(MarkdownMemoryStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.attempts = 0

    def persist_research(self, brief, result, identity):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("disk temporarily unavailable")
        return super().persist_research(brief, result, identity)


def _identity(thread_id: str) -> ExecutionIdentity:
    return ExecutionIdentity(thread_id, None, thread_id, 0)


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _interrupt_value(state: dict[str, Any]) -> dict[str, Any]:
    interrupts = state.get("__interrupt__")
    assert interrupts and len(interrupts) == 1
    return interrupts[0].value


@pytest.mark.asyncio
async def test_alignment_repairs_a_non_json_transport_once(tmp_path) -> None:
    class RepairingPolicy:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, messages, *, tools=None):
            self.calls += 1
            if self.calls == 1:
                return {"content": "I will prepare the research plan.", "tool_calls": []}
            assert "ALIGNMENT_FORMAT_REPAIR" in str(messages[-1].get("content") or "")
            return {
                "content": json.dumps({
                    "objective": "Trace Transformer development",
                    "scope": ["architecture history"],
                    "directions": ["foundational paper", "later variants"],
                    "constraints": ["cite primary sources"],
                    "expected_output": "An evidence-backed report",
                }),
                "tool_calls": [],
            }

    policy = RepairingPolicy()
    identity = _identity("root-alignment-repair")
    graph = build_research_workflow(
        policy,
        [],
        MarkdownMemoryStore(tmp_path),
        checkpointer=InMemorySaver(),
    )

    paused = await graph.ainvoke(
        create_research_workflow_state("Research Transformer development", identity),
        config=_config(identity.thread_id),
    )

    assert _interrupt_value(paused)["brief"]["objective"] == "Trace Transformer development"
    assert policy.calls == 2


@pytest.mark.asyncio
async def test_first_run_stops_for_user_without_running_research_tools(tmp_path) -> None:
    policy = WorkflowPolicy()
    tool = FixedWebTool()
    graph = build_research_workflow(policy, [tool], MarkdownMemoryStore(tmp_path))
    identity = _identity("root-wait")

    paused = await graph.ainvoke(
        create_research_workflow_state("How do Transformers work?", identity),
        config=_config(identity.thread_id),
    )

    payload = _interrupt_value(paused)
    assert payload["kind"] == "research_brief_confirmation"
    assert payload["brief"]["revision"] == 0
    assert tool.calls == []
    assert policy.research_calls == 0
    snapshot = await graph.aget_state(_config(identity.thread_id))
    assert snapshot.next == ("review_brief",)


@pytest.mark.asyncio
async def test_user_can_modify_then_confirm_the_same_root_workflow(tmp_path) -> None:
    policy = WorkflowPolicy()
    tool = FixedWebTool()
    graph = build_research_workflow(policy, [tool], MarkdownMemoryStore(tmp_path))
    identity = _identity("root-revise")
    await graph.ainvoke(
        create_research_workflow_state("How do Transformers work?", identity),
        config=_config(identity.thread_id),
    )

    revised_pause = await resume_research_workflow(
        graph,
        thread_id=identity.thread_id,
        action="modify",
        feedback="Focus only on peer-reviewed papers",
    )

    revised = _interrupt_value(revised_pause)["brief"]
    assert revised["revision"] == 1
    assert "peer-reviewed papers" in revised["directions"]
    assert tool.calls == []

    second_pause = await resume_research_workflow(
        graph,
        thread_id=identity.thread_id,
        action="modify",
        feedback="Also emphasize the comparison of reported results",
    )
    second_revision = _interrupt_value(second_pause)["brief"]
    assert second_revision["revision"] == 2
    assert "peer-reviewed papers" in second_revision["directions"]
    assert tool.calls == []

    final = await resume_research_workflow(
        graph,
        thread_id=identity.thread_id,
        action="confirm",
    )

    workflow_result = final["workflow_result"]
    assert workflow_result.brief.revision == 2
    assert workflow_result.research_result.status == ResearchStatus.COMPLETED
    assert len(tool.calls) == 2
    assert policy.alignment_calls == 3


@pytest.mark.asyncio
async def test_confirmation_writes_report_evidence_and_source_wikilinks(tmp_path) -> None:
    graph = build_research_workflow(
        WorkflowPolicy(),
        [FixedWebTool()],
        MarkdownMemoryStore(tmp_path),
    )
    identity = _identity("root-memory")
    await graph.ainvoke(
        create_research_workflow_state("How do Transformers work?", identity),
        config=_config(identity.thread_id),
    )
    final = await resume_research_workflow(
        graph,
        thread_id=identity.thread_id,
        action="confirm",
    )

    manifest = final["memory_manifest"]
    report_path = tmp_path / manifest.report_path
    evidence_path = tmp_path / manifest.evidence_paths[0]
    source_path = tmp_path / manifest.source_paths[0]
    assert report_path.exists() and evidence_path.exists() and source_path.exists()

    report = report_path.read_text(encoding="utf-8")
    evidence = evidence_path.read_text(encoding="utf-8")
    assert f"[[evidence/{evidence_path.stem}|Evidence]]" in report
    assert f"[[sources/{source_path.stem}|" in evidence
    assert "https://arxiv.org/abs/1706.03762" in source_path.read_text(encoding="utf-8")


def test_memory_store_repeated_commit_is_idempotent(tmp_path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    identity = _identity("root-idempotent")
    brief = ResearchBrief(
        question="Question",
        objective="Objective",
        scope=("scope",),
        directions=("direction",),
        constraints=(),
        expected_output="report",
    )
    evidence = EvidenceItem(
        evidence_id="evidence-fixed",
        finding="Fixed finding",
        source_type="web",
        title="Fixed source",
        source_ref="https://example.com/source",
        locator="section-1",
        excerpt="Fixed excerpt",
        excerpt_type="quote",
    )
    result = ResearchResult(
        task_id="task",
        status=ResearchStatus.COMPLETED,
        summary="Summary",
        findings=("Fixed finding",),
        evidence=(evidence,),
    )

    first_report, first_manifest = store.persist_research(brief, result, identity)
    first_files = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*.md"))
    second_report, second_manifest = store.persist_research(brief, result, identity)
    second_files = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*.md"))

    assert second_manifest == first_manifest
    assert second_report == first_report
    assert second_files == first_files
    assert len(second_files) == 3


@pytest.mark.asyncio
async def test_confirmation_can_resume_from_a_rebuilt_graph(tmp_path) -> None:
    checkpointer = InMemorySaver()
    policy = WorkflowPolicy()
    tool = FixedWebTool()
    identity = _identity("root-rebuilt")
    first_graph = build_research_workflow(
        policy,
        [tool],
        MarkdownMemoryStore(tmp_path),
        checkpointer=checkpointer,
    )
    await first_graph.ainvoke(
        create_research_workflow_state("How do Transformers work?", identity),
        config=_config(identity.thread_id),
    )

    rebuilt_graph = build_research_workflow(
        policy,
        [tool],
        MarkdownMemoryStore(tmp_path),
        checkpointer=checkpointer,
    )
    final = await resume_research_workflow(
        rebuilt_graph,
        thread_id=identity.thread_id,
        action="confirm",
    )

    assert final["workflow_result"].research_result.status == ResearchStatus.COMPLETED
    assert len(tool.calls) == 2


@pytest.mark.asyncio
async def test_retry_after_persist_failure_does_not_repeat_research(tmp_path) -> None:
    policy = WorkflowPolicy()
    tool = FixedWebTool()
    store = FlakyMemoryStore(tmp_path)
    graph = build_research_workflow(policy, [tool], store)
    identity = _identity("root-persist-retry")
    await graph.ainvoke(
        create_research_workflow_state("How do Transformers work?", identity),
        config=_config(identity.thread_id),
    )

    with pytest.raises(RuntimeError, match="disk temporarily unavailable"):
        await resume_research_workflow(
            graph,
            thread_id=identity.thread_id,
            action="confirm",
        )
    assert len(tool.calls) == 2

    final = await graph.ainvoke(None, config=_config(identity.thread_id))
    assert final["workflow_result"].research_result.status == ResearchStatus.COMPLETED
    assert len(tool.calls) == 2
    assert store.attempts == 2


@pytest.mark.asyncio
async def test_two_root_workflows_keep_checkpoints_and_reports_isolated(tmp_path) -> None:
    graph = build_research_workflow(
        WorkflowPolicy(),
        [FixedWebTool()],
        MarkdownMemoryStore(tmp_path),
    )
    manifests = []
    for thread_id, question in (
        ("root-alpha", "Alpha transformer question"),
        ("root-beta", "Beta transformer question"),
    ):
        await graph.ainvoke(
            create_research_workflow_state(question, _identity(thread_id)),
            config=_config(thread_id),
        )
        final = await resume_research_workflow(
            graph,
            thread_id=thread_id,
            action="confirm",
        )
        manifests.append(final["memory_manifest"])

    assert manifests[0].report_path != manifests[1].report_path
    assert manifests[0].evidence_paths == manifests[1].evidence_paths
    assert manifests[0].source_paths == manifests[1].source_paths
    alpha = (await graph.aget_state(_config("root-alpha"))).values
    beta = (await graph.aget_state(_config("root-beta"))).values
    assert alpha["question"].startswith("Alpha")
    assert beta["question"].startswith("Beta")


@pytest.mark.asyncio
async def test_outer_workflow_rejects_non_root_identity(tmp_path) -> None:
    graph = build_research_workflow(
        WorkflowPolicy(),
        [],
        MarkdownMemoryStore(tmp_path),
    )
    identity = ExecutionIdentity("child", "root", "root", 1)
    with pytest.raises(ValueError, match="requires a root identity"):
        await graph.ainvoke(
            create_research_workflow_state("Question", identity, AgentLimits()),
            config=_config(identity.thread_id),
        )
