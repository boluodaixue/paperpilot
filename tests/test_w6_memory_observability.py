"""W6 observability coverage for selected-Memory retrieval and research writes."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import src.research.retrieval as retrieval_module
import src.research.runtime as runtime_module
import src.research.workflow as workflow_module
from src.research import (
    AgentLimits,
    ExecutionIdentity,
    MarkdownMemoryIndex,
    MarkdownMemoryStore,
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)
from src.research.runtime import ResearchRuntime


def _write_note(root: Path, body: str) -> str:
    relative_path = "Memories/M-trace/notes/Trace-source.md"
    (root / relative_path).write_text(
        "# Trace source\n\n" + body,
        encoding="utf-8",
    )
    return relative_path


class _Policy:
    def __call__(self, messages, *, tools=None):
        system = str(messages[0].get("content") or "")
        if "before research begins" in system:
            return {
                "content": json.dumps(
                    {
                        "objective": "Trace selected Memory research",
                        "scope": ["trace"],
                        "directions": ["find fixed evidence"],
                        "constraints": [],
                        "expected_output": "Markdown",
                    }
                ),
                "tool_calls": [],
            }
        if messages[-1].get("role") == "tool":
            return {
                "content": json.dumps(
                    {
                        "status": "completed",
                        "summary": "Fixed trace result",
                        "findings": ["Fixed trace evidence"],
                        "unresolved": [],
                    }
                ),
                "tool_calls": [],
            }
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "trace-tool",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        }


class _Tool:
    name = "web_search"

    def get_openai_tool_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fixed offline source",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **kwargs: Any):
        return {
            "results": [
                {
                    "title": "Fixed trace source",
                    "url": "https://example.invalid/trace",
                    "snippet": "Fixed trace evidence",
                }
            ]
        }


@pytest.mark.asyncio
async def test_memory_traces_include_identity_paths_and_no_note_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []

    @contextmanager
    def fake_context(**kwargs):
        contexts.append(kwargs)
        yield

    @contextmanager
    def fake_block(name, run_type="chain", inputs=None, tags=None):
        record = {
            "name": name,
            "run_type": run_type,
            "inputs": inputs,
            "tags": tags,
            "outputs": [],
        }

        class Run:
            def add_output(self, output):
                record["outputs"].append(output)

            def add_metadata(self, metadata):
                return None

            def set_error(self, message):
                return None

        blocks.append(record)
        yield Run()

    monkeypatch.setattr(retrieval_module, "trace_context", fake_context)
    monkeypatch.setattr(retrieval_module, "trace_block", fake_block)
    monkeypatch.setattr(workflow_module, "trace_context", fake_context)
    monkeypatch.setattr(workflow_module, "trace_block", fake_block)

    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Trace", memory_id="M-trace")
    secret = "Needletrace SECRET-NOTE-BODY"
    source_path = _write_note(tmp_path, secret)

    direct_hits = MarkdownMemoryIndex(store).search("M-trace", "needletrace")
    assert [hit.relative_path for hit in direct_hits] == [source_path]

    thread_id = "trace-root"
    graph = build_research_workflow(_Policy(), [_Tool()], store)
    await graph.ainvoke(
        create_research_workflow_state(
            "Continue needletrace research",
            ExecutionIdentity(thread_id, None, thread_id, 0),
            memory_id="M-trace",
        ),
        config={"configurable": {"thread_id": thread_id}},
    )
    final = await resume_research_workflow(
        graph,
        thread_id=thread_id,
        action="confirm",
    )

    retrieval_blocks = [record for record in blocks if record["name"] == "memory.search"]
    assert retrieval_blocks
    retrieval_output = retrieval_blocks[0]["outputs"][-1]
    assert retrieval_blocks[0]["run_type"] == "retriever"
    assert retrieval_blocks[0]["inputs"] == {"memory_id": "M-trace", "limit": 5}
    assert retrieval_output == {
        "memory_id": "M-trace",
        "query_term_count": 1,
        "hit_count": 1,
        "retrieved_files": [{"path": source_path, "score": direct_hits[0].score}],
    }

    workflow_contexts = [
        item
        for item in contexts
        if item.get("trace_name") == "paperpilot.research.workflow"
    ]
    assert workflow_contexts
    assert all(item["metadata"]["memory_id"] == "M-trace" for item in workflow_contexts)

    persist = next(
        record for record in blocks if record["name"] == "research_workflow.persist"
    )
    persist_output = persist["outputs"][-1]
    manifest = final["memory_manifest"]
    assert persist_output == {
        "memory_id": "M-trace",
        "report_path": manifest.report_path,
        "evidence_paths": list(manifest.evidence_paths),
        "source_paths": list(manifest.source_paths),
        "evidence_count": len(manifest.evidence_paths),
        "source_count": len(manifest.source_paths),
    }

    serialized_trace = json.dumps(
        {"contexts": contexts, "blocks": blocks},
        ensure_ascii=False,
        default=str,
    )
    assert "SECRET-NOTE-BODY" not in serialized_trace
    assert source_path in serialized_trace


def test_empty_retrieval_is_traced_without_query_or_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs: list[dict[str, Any]] = []

    @contextmanager
    def fake_context(**kwargs):
        yield

    @contextmanager
    def fake_block(*args, **kwargs):
        class Run:
            def add_output(self, output):
                outputs.append(output)

        yield Run()

    monkeypatch.setattr(retrieval_module, "trace_context", fake_context)
    monkeypatch.setattr(retrieval_module, "trace_block", fake_block)
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Empty", memory_id="M-empty")

    assert MarkdownMemoryIndex(store).search("M-empty", "SECRET-QUERY") == ()
    assert outputs == [
        {
            "memory_id": "M-empty",
            "query_term_count": 2,
            "hit_count": 0,
            "retrieved_files": [],
        }
    ]
    assert "SECRET-QUERY" not in json.dumps(outputs)


def test_legacy_memory_rebuilds_safe_root_markdown_with_the_same_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []

    @contextmanager
    def fake_context(**kwargs):
        contexts.append(kwargs)
        yield

    @contextmanager
    def fake_block(*args, **kwargs):
        class Run:
            def add_output(self, output):
                outputs.append(output)

        yield Run()

    monkeypatch.setattr(retrieval_module, "trace_context", fake_context)
    monkeypatch.setattr(retrieval_module, "trace_block", fake_block)
    report = tmp_path / "reports" / "Legacy-report.md"
    evidence = tmp_path / "evidence" / "Legacy-evidence.md"
    report.parent.mkdir()
    evidence.parent.mkdir()
    report.write_text(
        "# Legacy report\n\nLegacyneedle [[evidence/Legacy-evidence]].",
        encoding="utf-8",
    )
    evidence.write_text("# Legacy evidence\n\nSupporting material.", encoding="utf-8")
    managed = tmp_path / "Memories" / "M-other" / "notes" / "Private.md"
    managed.parent.mkdir(parents=True)
    managed.write_text("# Private\n\nLegacyneedle secret.", encoding="utf-8")
    index = MarkdownMemoryIndex(MarkdownMemoryStore(tmp_path))

    first = index.search("M-legacy", "legacyneedle", limit=5)
    report.write_text("# Legacy report\n\nChangedneedle.", encoding="utf-8")
    second = index.search("M-legacy", "changedneedle", limit=5)

    assert {hit.relative_path for hit in first} == {
        "reports/Legacy-report.md",
        "evidence/Legacy-evidence.md",
    }
    assert [hit.relative_path for hit in second] == ["reports/Legacy-report.md"]
    assert all("Memories/M-other" not in hit.relative_path for hit in first + second)
    assert contexts[0]["metadata"] == {"memory_id": "M-legacy", "limit": 5}
    assert outputs[0]["memory_id"] == "M-legacy"
    assert {item["path"] for item in outputs[0]["retrieved_files"]} == {
        "reports/Legacy-report.md",
        "evidence/Legacy-evidence.md",
    }


@pytest.mark.asyncio
async def test_runtime_start_and_review_propagate_memory_and_write_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contexts: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []

    @contextmanager
    def fake_context(**kwargs):
        contexts.append(kwargs)
        yield

    @contextmanager
    def fake_block(name, run_type="chain", inputs=None, tags=None):
        record = {"name": name, "metadata": [], "outputs": []}

        class Run:
            def add_output(self, output):
                record["outputs"].append(output)

            def add_metadata(self, metadata):
                record["metadata"].append(metadata)

        blocks.append(record)
        yield Run()

    monkeypatch.setattr(runtime_module, "trace_context", fake_context)
    monkeypatch.setattr(runtime_module, "trace_block", fake_block)

    class Graph:
        async def ainvoke(self, state, config):
            return {
                "brief": SimpleNamespace(
                    memory_paths=("Memories/M-runtime/notes/Source.md",)
                )
            }

        async def aget_state(self, config):
            return SimpleNamespace(values={"memory_id": "M-runtime"})

    async def fake_resume(graph, *, thread_id, action, feedback=None):
        manifest = SimpleNamespace(
            report_path="Memories/M-runtime/reports/Report.md",
            evidence_paths=("Memories/M-runtime/evidence/Evidence.md",),
            source_paths=("Memories/M-runtime/sources/Source.md",),
        )
        return {"workflow_result": SimpleNamespace(memory_manifest=manifest)}

    monkeypatch.setattr(runtime_module, "resume_research_workflow", fake_resume)
    runtime = object.__new__(ResearchRuntime)
    runtime.graph = Graph()
    runtime.limits = AgentLimits()

    await runtime.start(
        "Continue research",
        thread_id="runtime-trace",
        memory_id="M-runtime",
    )
    await runtime.review("runtime-trace", "confirm")

    assert [context["metadata"] for context in contexts] == [
        {"memory_id": "M-runtime"},
        {"memory_id": "M-runtime"},
    ]
    assert [block["name"] for block in blocks] == [
        "paperpilot.research.start",
        "paperpilot.research.review",
    ]
    assert blocks[0]["outputs"][-1]["memory_paths"] == [
        "Memories/M-runtime/notes/Source.md"
    ]
    assert blocks[1]["outputs"][-1]["write_paths"] == [
        "Memories/M-runtime/reports/Report.md",
        "Memories/M-runtime/evidence/Evidence.md",
        "Memories/M-runtime/sources/Source.md",
    ]


@pytest.mark.asyncio
async def test_runtime_memory_operations_trace_only_bounded_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs: dict[str, dict[str, Any]] = {}

    @contextmanager
    def fake_context(**kwargs):
        yield

    @contextmanager
    def fake_block(name, run_type="chain", inputs=None, tags=None):
        class Run:
            def add_output(self, output):
                outputs[name] = output

            def add_metadata(self, metadata):
                return None

        yield Run()

    answer = SimpleNamespace(
        answer_id="Answer-runtime",
        memory_id="M-runtime",
        citations=(
            SimpleNamespace(relative_path="Memories/M-runtime/notes/Source.md"),
        ),
        insufficient_evidence=(),
    )
    proposal = SimpleNamespace(
        proposal_id="Proposal-runtime",
        memory_id="M-runtime",
        target_path="Memories/M-runtime/notes/Note.md",
        home_path="Memories/M-runtime/Home.md",
        source_paths=("Memories/M-runtime/notes/Source.md",),
    )
    import_proposal = SimpleNamespace(
        proposal_id="ImportProposal-runtime",
        memory_id="M-runtime",
        attachment_path="Memories/M-runtime/attachments/Asset.txt",
        import_path="Memories/M-runtime/imports/Import.md",
        note_path="Memories/M-runtime/notes/Import-note.md",
    )

    async def fake_answer(*args, **kwargs):
        return answer

    async def fake_note(*args, **kwargs):
        return proposal

    async def fake_import(*args, **kwargs):
        return import_proposal

    store = SimpleNamespace(
        commit_memory_note=lambda value: {
            "memory_id": value.memory_id,
            "target_path": value.target_path,
            "home_path": value.home_path,
            "wikilink": "[[Memories/M-runtime/notes/Note]]",
        },
        commit_memory_import=lambda value: {
            "status": "committed",
            "memory_id": value.memory_id,
            "attachment_path": value.attachment_path,
            "import_path": value.import_path,
            "note_path": value.note_path,
            "home_path": "Memories/M-runtime/Home.md",
            "private_markdown": "SECRET BODY",
        },
    )
    monkeypatch.setattr(runtime_module, "trace_context", fake_context)
    monkeypatch.setattr(runtime_module, "trace_block", fake_block)
    monkeypatch.setattr(runtime_module, "answer_from_memory", fake_answer)
    monkeypatch.setattr(runtime_module, "propose_note_from_memory", fake_note)
    monkeypatch.setattr(runtime_module, "prepare_file_import", fake_import)
    runtime = object.__new__(ResearchRuntime)
    runtime.memory_store = store
    runtime.policy = object()

    assert await runtime.answer_memory("M-runtime", "SECRET QUESTION") is answer
    assert await runtime.propose_memory_note(answer) is proposal
    runtime.commit_memory_note(proposal)
    assert (
        await runtime.prepare_memory_file_import(
            "M-runtime", "source.txt", b"SECRET BYTES"
        )
        is import_proposal
    )
    runtime.commit_memory_import(import_proposal)

    serialized = json.dumps(outputs, ensure_ascii=False)
    assert "SECRET QUESTION" not in serialized
    assert "SECRET BODY" not in serialized
    assert "SECRET BYTES" not in serialized
    assert outputs["paperpilot.memory.answer"]["retrieved_files"] == [
        "Memories/M-runtime/notes/Source.md"
    ]
    assert outputs["paperpilot.memory.note.commit"]["target_path"].endswith(
        "/Note.md"
    )
    assert outputs["paperpilot.memory.import.commit"] == {
        "status": "committed",
        "memory_id": "M-runtime",
        "attachment_path": "Memories/M-runtime/attachments/Asset.txt",
        "import_path": "Memories/M-runtime/imports/Import.md",
        "note_path": "Memories/M-runtime/notes/Import-note.md",
        "home_path": "Memories/M-runtime/Home.md",
    }
