"""W3 acceptance tests for continuing research from one selected Memory."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests._research_assessment import assessment_response

import src.research.workflow as workflow_module
from src.research import (
    AgentLimits,
    ExecutionIdentity,
    MarkdownMemoryStore,
    ResearchBrief,
    ResearchStatus,
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)
from src.research.retrieval import MemorySearchHit
from src.research.workflow import _bounded_memory_hits


def _identity(thread_id: str) -> ExecutionIdentity:
    return ExecutionIdentity(thread_id, None, thread_id, 0)


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _interrupt_brief(state: dict[str, Any]) -> dict[str, Any]:
    interrupts = state.get("__interrupt__")
    assert interrupts and len(interrupts) == 1
    return interrupts[0].value["brief"]


def _write_note(
    root: Path,
    memory_id: str,
    *,
    name: str,
    title: str,
    body: str,
    wikilink: str | None = None,
) -> str:
    relative_path = f"Memories/{memory_id}/notes/{name}.md"
    path = root / relative_path
    link_text = f"\n\n[[{wikilink}]]" if wikilink is not None else ""
    path.write_text(
        (
            "---\n"
            f'id: "Note-{name}"\n'
            'type: "note"\n'
            f'memory_id: "{memory_id}"\n'
            f'title: "{title}"\n'
            'created_at: "2026-08-28T00:00:00+08:00"\n'
            'updated_at: "2026-08-28T00:00:00+08:00"\n'
            'origin: "user"\n'
            'status: "confirmed"\n'
            "tags:\n  - research\n"
            "---\n\n"
            f"# {title}\n\n{body}{link_text}\n"
        ),
        encoding="utf-8",
    )
    return relative_path


class _Tool:
    name = "web_search"

    def __init__(self) -> None:
        self.calls = 0

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fixed offline W3 source",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "results": [
                {
                    "title": "New transformer evidence",
                    "url": "https://example.com/new-transformer-evidence",
                    "snippet": "The new source fills a prior evidence gap.",
                }
            ]
        }


class _Policy:
    def __init__(self) -> None:
        self.alignment_messages: list[list[dict[str, Any]]] = []
        self.research_messages: list[list[dict[str, Any]]] = []

    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        if "before research begins" in str(messages[0].get("content", "")):
            self.alignment_messages.append(messages)
            revised = "Revise the research brief" in str(messages[-1].get("content", ""))
            payload: dict[str, Any] = {
                "objective": "Extend the selected Memory with new transformer evidence",
                "scope": ["transformer evidence"],
                "directions": ["Find the missing primary evidence"],
                "constraints": ["Keep prior notes source-locatable"],
                "expected_output": "An evidence-backed Markdown report",
                "memory_id": "M-forged",
                "memory_paths": ["Memories/M-forged/notes/Forged.md"],
            }
            if revised:
                payload["known_information"] = ["User-confirmed known information"]
                payload["research_gaps"] = ["User-refined evidence gap"]
            elif "Zygomorphic spectroheliograph" in str(messages[-1].get("content", "")):
                payload["known_information"] = ["Forged prior Memory knowledge"]
            return {"content": json.dumps(payload), "tool_calls": []}

        self.research_messages.append(messages)
        if messages[-1]["role"] == "tool" or tools == []:
            return {
                "content": json.dumps(
                    {
                        "status": "completed",
                        "summary": "New evidence extends the prior Memory.",
                        "findings": ["The prior evidence gap now has a new source."],
                        "unresolved": [],
                    }
                ),
                "tool_calls": [],
            }
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "w3-search",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"query": "new transformer evidence"}),
                    },
                }
            ],
        }


@pytest.mark.asyncio
async def test_memory_hits_are_fixed_in_brief_revision_and_root_task_context(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Current", memory_id="M-current")
    store.create_memory("Other", memory_id="M-other")
    current_path = _write_note(
        tmp_path,
        "M-current",
        name="Transformer-baseline",
        title="Transformer baseline",
        body="Prior transformer attention evidence established the baseline.",
        wikilink="Memories/M-current/notes/Supporting",
    )
    _write_note(
        tmp_path,
        "M-other",
        name="Transformer-private",
        title="Other transformer material",
        body="This other Memory must never enter the selected context.",
    )
    policy = _Policy()
    tool = _Tool()
    limits = AgentLimits(max_fork_depth=1, max_children=2)
    graph = build_research_workflow(policy, [tool], store)
    thread_id = "w3-continued"

    paused = await graph.ainvoke(
        create_research_workflow_state(
            "What new transformer evidence fills the prior gap?",
            _identity(thread_id),
            limits,
            memory_id="M-current",
        ),
        config=_config(thread_id),
    )
    first_brief = _interrupt_brief(paused)
    prompt = "\n".join(
        str(message.get("content", ""))
        for message in policy.alignment_messages[0]
    )

    assert "MEMORY_CONTEXT_JSON" in prompt
    assert current_path in prompt
    assert "Transformer baseline" in prompt
    assert "Prior transformer attention evidence" in prompt
    assert "Memories/M-current/notes/Supporting" in prompt
    assert "M-other" not in prompt
    assert "Other transformer material" not in prompt
    assert first_brief["memory_id"] == "M-current"
    assert first_brief["memory_paths"] == (current_path,)
    assert "M-forged" not in json.dumps(first_brief)
    assert any(
        "Prior transformer attention evidence" in item
        for item in first_brief["known_information"]
    )
    assert first_brief["research_gaps"] == ("Find the missing primary evidence",)
    assert tool.calls == 0

    revised_pause = await resume_research_workflow(
        graph,
        thread_id=thread_id,
        action="modify",
        feedback="Narrow the remaining gap",
    )
    revised = _interrupt_brief(revised_pause)
    assert revised["revision"] == 1
    assert revised["memory_id"] == "M-current"
    assert revised["memory_paths"] == (current_path,)
    assert revised["known_information"] == ("User-confirmed known information",)
    assert revised["research_gaps"] == ("User-refined evidence gap",)

    final = await resume_research_workflow(
        graph,
        thread_id=thread_id,
        action="confirm",
    )
    brief = final["brief"]
    task = final["task"]

    assert isinstance(brief, ResearchBrief)
    assert brief.memory_id == "M-current"
    assert final["workflow_result"].memory_id == "M-current"
    assert final["workflow_result"].research_result.status == ResearchStatus.COMPLETED
    assert task.context["known_information"] == ["User-confirmed known information"]
    assert task.context["research_gaps"] == ["User-refined evidence gap"]
    assert task.context["retrieved_memory"][0]["path"] == current_path
    assert "memory_id" not in task.context
    assert final["identity"] == _identity(thread_id)
    assert final["limits"] == limits
    assert final["identity"].depth == 0
    assert final["limits"].max_fork_depth == 1
    assert tool.calls == 1

    report = final["report_markdown"]
    assert "## Memory Context" in report
    assert "**Memory ID:** M-current" in report
    assert f"`{current_path}`" in report
    assert "User-confirmed known information" in report
    assert "User-refined evidence gap" in report


@pytest.mark.asyncio
async def test_no_hits_are_explicit_and_model_cannot_invent_paths(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Empty", memory_id="M-empty")
    policy = _Policy()
    graph = build_research_workflow(policy, [_Tool()], store)
    thread_id = "w3-no-hits"

    paused = await graph.ainvoke(
        create_research_workflow_state(
            "Zygomorphic spectroheliograph",
            _identity(thread_id),
            memory_id="M-empty",
        ),
        config=_config(thread_id),
    )
    brief = _interrupt_brief(paused)
    prompt = "\n".join(
        str(message.get("content", ""))
        for message in policy.alignment_messages[0]
    )

    assert "No relevant notes were found in the selected Memory" in prompt
    assert '"hits": []' in prompt
    assert brief["memory_id"] == "M-empty"
    assert brief["memory_paths"] == ()
    assert brief["known_information"] == ()
    assert brief["research_gaps"] == ("Find the missing primary evidence",)
    assert "M-forged" not in json.dumps(brief)
    assert "Forged prior Memory knowledge" not in json.dumps(brief)


def test_bounded_memory_context_caps_hits_and_wikilinks() -> None:
    long_link = "Memories/M-bounded/notes/" + ("x" * 400)
    hits = tuple(
        MemorySearchHit(
            relative_path=f"Memories/M-bounded/notes/Note-{index}.md",
            title="t" * 400,
            summary="s" * 1400,
            wikilinks=tuple(f"{long_link}-{link}" for link in range(12)),
            score=10,
            modified_ns=index,
            content_hash=str(index),
        )
        for index in range(7)
    )

    bounded = _bounded_memory_hits(hits)

    assert len(bounded) == 5
    assert all(set(hit) == {"path", "title", "summary", "wikilinks"} for hit in bounded)
    assert all(len(hit["title"]) == 300 for hit in bounded)
    assert all(len(hit["summary"]) == 1200 for hit in bounded)
    assert all(len(hit["wikilinks"]) == 8 for hit in bounded)
    assert all(
        len(link) == 300
        for hit in bounded
        for link in hit["wikilinks"]
    )


@pytest.mark.asyncio
async def test_workflow_reuses_one_rebuildable_process_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[Any] = []
    searches: list[tuple[str, str, int]] = []

    class CountingIndex:
        def __init__(self, memory_store) -> None:
            instances.append(memory_store)

        def search(self, memory_id: str, query: str, limit: int = 5):
            searches.append((memory_id, query, limit))
            return ()

    monkeypatch.setattr(workflow_module, "MarkdownMemoryIndex", CountingIndex)
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("Index", memory_id="M-index")
    graph = build_research_workflow(_Policy(), [_Tool()], store)

    for thread_id in ("w3-index-first", "w3-index-second"):
        await graph.ainvoke(
            create_research_workflow_state(
                "Zygomorphic spectroheliograph",
                _identity(thread_id),
                memory_id="M-index",
            ),
            config=_config(thread_id),
        )

    assert instances == [store]
    assert searches == [
        ("M-index", "Zygomorphic spectroheliograph", 5),
        ("M-index", "Zygomorphic spectroheliograph", 5),
    ]


@pytest.mark.asyncio
async def test_two_continued_runs_keep_both_reports_in_the_same_memory(
    tmp_path: Path,
) -> None:
    store = MarkdownMemoryStore(tmp_path)
    store.create_memory("History", memory_id="M-history")
    _write_note(
        tmp_path,
        "M-history",
        name="Prior-transformer",
        title="Prior transformer research",
        body="Prior transformer evidence and an unresolved gap.",
    )
    graph = build_research_workflow(_Policy(), [_Tool()], store)
    report_paths: list[str] = []

    for thread_id in ("w3-history-first", "w3-history-second"):
        await graph.ainvoke(
            create_research_workflow_state(
                "Continue transformer evidence research",
                _identity(thread_id),
                memory_id="M-history",
            ),
            config=_config(thread_id),
        )
        final = await resume_research_workflow(
            graph,
            thread_id=thread_id,
            action="confirm",
        )
        report_paths.append(final["memory_manifest"].report_path)

    assert report_paths[0] != report_paths[1]
    assert all(path.startswith("Memories/M-history/reports/") for path in report_paths)
    assert all((tmp_path / path).is_file() for path in report_paths)


def test_legacy_brief_construction_and_fields_remain_compatible() -> None:
    brief = ResearchBrief(
        question="Legacy question",
        objective="Legacy objective",
        scope=(),
        directions=("Legacy direction",),
        constraints=(),
        expected_output="Legacy Markdown",
    )

    assert brief.memory_id is None
    assert brief.memory_paths == ()
    assert brief.known_information == ()
    assert brief.research_gaps == ()
