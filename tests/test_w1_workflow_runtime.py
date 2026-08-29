"""W1 acceptance tests for selecting a long-lived Memory in the workflow."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from tests._research_assessment import assessment_response

from src.research import (
    ExecutionIdentity,
    MarkdownMemoryStore,
    MemoryDescriptor,
    MemoryManifest,
    ResearchBrief,
    ResearchResult,
    ResearchStatus,
    ResearchWorkflowResult,
    build_research_runtime,
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)


class _Policy:
    def __init__(self) -> None:
        self.alignment_calls = 0
        self.research_calls = 0

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        if "before research begins" in str(messages[0].get("content", "")):
            self.alignment_calls += 1
            return {
                "content": json.dumps(
                    {
                        "objective": "Verify selected Memory routing",
                        "scope": ["workflow"],
                        "directions": ["collect one source"],
                        "constraints": ["persist to the selected Memory"],
                        "expected_output": "Markdown report",
                    }
                ),
                "tool_calls": [],
            }

        self.research_calls += 1
        if messages[-1]["role"] == "tool" or tools == []:
            return {
                "content": json.dumps(
                    {
                        "status": "completed",
                        "summary": "The selected Memory received the result.",
                        "findings": ["Memory routing was preserved."],
                        "unresolved": [],
                    }
                ),
                "tool_calls": [],
            }
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "w1-tool-call",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"query": "memory routing"}),
                    },
                }
            ],
        }


class _Tool:
    name = "web_search"

    def __init__(self) -> None:
        self.calls = 0

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fixed offline result",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "results": [
                {
                    "title": "Memory routing source",
                    "url": "https://example.com/memory-routing",
                    "snippet": "A fixed source used by the W1 workflow test.",
                }
            ]
        }


class _SelectionStore(MarkdownMemoryStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.memories: dict[str, MemoryDescriptor] = {}
        self.persisted_memory_ids: list[str | None] = []

    def create_memory(
        self,
        title: str,
        memory_id: str | None = None,
    ) -> MemoryDescriptor:
        selected_id = memory_id or "M-generated"
        now = datetime.now(timezone.utc).isoformat()
        descriptor = MemoryDescriptor(
            memory_id=selected_id,
            title=title,
            relative_path=f"Memories/{selected_id}/",
            created_at=now,
            updated_at=now,
        )
        self.memories[selected_id] = descriptor
        return descriptor

    def get_memory(self, memory_id: str) -> MemoryDescriptor:
        try:
            return self.memories[memory_id]
        except KeyError as exc:
            raise FileNotFoundError(f"Memory does not exist: {memory_id}") from exc

    def list_memories(self) -> tuple[MemoryDescriptor, ...]:
        return tuple(self.memories.values())

    def persist_research(
        self,
        brief,
        result,
        identity,
        *,
        memory_id: str | None = None,
    ):
        self.persisted_memory_ids.append(memory_id)
        prefix = f"Memories/{memory_id}/" if memory_id is not None else ""
        manifest = MemoryManifest(
            report_path=f"{prefix}reports/Report-{identity.root_thread_id}.md",
            evidence_paths=(f"{prefix}evidence/Evidence-fixed.md",),
            source_paths=(f"{prefix}sources/Source-fixed.md",),
        )
        return "# Selected Memory report\n", manifest


def _identity(thread_id: str) -> ExecutionIdentity:
    return ExecutionIdentity(thread_id, None, thread_id, 0)


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def test_workflow_state_rejects_invalid_memory_id() -> None:
    with pytest.raises(ValueError, match="memory_id"):
        create_research_workflow_state(
            "Question",
            _identity("w1-invalid"),
            memory_id="../escape",
        )


@pytest.mark.asyncio
async def test_missing_memory_is_rejected_before_policy_or_tool(tmp_path: Path) -> None:
    policy = _Policy()
    tool = _Tool()
    graph = build_research_workflow(policy, [tool], _SelectionStore(tmp_path))

    with pytest.raises(FileNotFoundError, match="Memory does not exist"):
        await graph.ainvoke(
            create_research_workflow_state(
                "Question",
                _identity("w1-missing"),
                memory_id="M-missing",
            ),
            config=_config("w1-missing"),
        )

    assert policy.alignment_calls == policy.research_calls == 0
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_checkpoint_and_result_preserve_selected_memory(tmp_path: Path) -> None:
    store = _SelectionStore(tmp_path)
    store.create_memory("Selected", memory_id="M-selected")
    tool = _Tool()
    graph = build_research_workflow(_Policy(), [tool], store)
    thread_id = "w1-checkpoint"

    await graph.ainvoke(
        create_research_workflow_state(
            "Question",
            _identity(thread_id),
            memory_id="M-selected",
        ),
        config=_config(thread_id),
    )
    snapshot = await graph.aget_state(_config(thread_id))
    assert snapshot.values["memory_id"] == "M-selected"

    final = await resume_research_workflow(
        graph,
        thread_id=thread_id,
        action="confirm",
    )
    result = final["workflow_result"]

    assert isinstance(result, ResearchWorkflowResult)
    assert final["memory_id"] == result.memory_id == "M-selected"
    assert store.persisted_memory_ids == ["M-selected"]
    assert result.memory_manifest.report_path.startswith(
        "Memories/M-selected/reports/"
    )
    assert all(
        path.startswith("Memories/M-selected/")
        for path in (
            *result.memory_manifest.evidence_paths,
            *result.memory_manifest.source_paths,
        )
    )
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_runtime_creates_lists_gets_and_selects_memory(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    runtime = build_research_runtime(
        {},
        policy=_Policy(),
        tools=[_Tool()],
        memory_store=store,
    )

    created = runtime.create_memory("Runtime Memory", memory_id="M-runtime")
    assert runtime.get_memory("M-runtime") == created
    assert runtime.list_memories() == (created,)

    result = await runtime.run_auto_confirmed(
        "Question",
        thread_id="w1-runtime",
        memory_id="M-runtime",
    )
    assert result.memory_id == "M-runtime"
    assert result.memory_manifest.report_path.startswith("Memories/M-runtime/")
    assert (tmp_path / result.memory_manifest.report_path).is_file()


def test_legacy_result_construction_keeps_memory_optional() -> None:
    result = ResearchWorkflowResult(
        brief=ResearchBrief(
            question="Legacy",
            objective="Keep the legacy constructor",
            scope=(),
            directions=("compatibility",),
            constraints=(),
            expected_output="Markdown",
        ),
        research_result=ResearchResult(
            task_id="legacy-task",
            status=ResearchStatus.COMPLETED,
            summary="Legacy construction remains valid.",
        ),
        report_markdown="# Legacy\n",
        memory_manifest=MemoryManifest(report_path="reports/Report-legacy.md"),
    )

    assert list(ResearchWorkflowResult.__dataclass_fields__)[-1] == "memory_id"
    assert result.memory_id is None
