from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.research import (
    ExecutionIdentity,
    MarkdownMemoryStore,
    MemoryWriteConflictError,
    build_research_workflow,
    create_research_workflow_state,
    resume_research_workflow,
)


class _Policy:
    def __call__(self, messages, *, tools=None):
        if "before research begins" in str(messages[0].get("content", "")):
            return {
                "content": json.dumps(
                    {
                        "objective": "Verify one stable research commit",
                        "scope": ["persistence"],
                        "directions": ["use the fixed source"],
                        "constraints": ["cite evidence"],
                        "expected_output": "Markdown report",
                    }
                ),
                "tool_calls": [],
            }
        if messages[-1]["role"] == "tool":
            return {
                "content": json.dumps(
                    {
                        "status": "completed",
                        "summary": "The stable finding was verified.",
                        "findings": ["The stable finding is supported."],
                        "unresolved": [],
                    }
                ),
                "tool_calls": [],
            }
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "fixed-call",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"query": "stable finding"}),
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
                "description": "fixed offline search",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **kwargs) -> dict[str, Any]:
        self.calls += 1
        return {
            "results": [
                {
                    "title": "Stable Source",
                    "url": "https://example.com/stable",
                    "snippet": "The stable finding is supported.",
                }
            ]
        }


class _CrashAfterCommitStore(MarkdownMemoryStore):
    def __init__(
        self,
        root: Path,
        mutate: Callable[[Path, str], None] | None = None,
    ) -> None:
        super().__init__(root)
        self.mutate = mutate
        self.calls = 0

    def persist_research(self, brief, result, identity, *, memory_id=None):
        self.calls += 1
        committed = super().persist_research(
            brief,
            result,
            identity,
            memory_id=memory_id,
        )
        if self.mutate is not None:
            self.mutate(self.root, committed[1].report_path)
        raise RuntimeError("process stopped after the Vault commit")


class _CountingStore(MarkdownMemoryStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls = 0

    def persist_research(self, brief, result, identity, *, memory_id=None):
        self.calls += 1
        return super().persist_research(
            brief,
            result,
            identity,
            memory_id=memory_id,
        )


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


async def _stop_after_commit(
    root: Path,
    *,
    thread_id: str,
    memory_id: str | None = None,
    mutate: Callable[[Path, str], None] | None = None,
):
    checkpointer = InMemorySaver()
    tool = _Tool()
    store = _CrashAfterCommitStore(root, mutate)
    graph = build_research_workflow(
        _Policy(),
        [tool],
        store,
        checkpointer=checkpointer,
    )
    identity = ExecutionIdentity(thread_id, None, thread_id, 0)
    await graph.ainvoke(
        create_research_workflow_state(
            "Can the stable finding be verified?",
            identity,
            memory_id=memory_id,
        ),
        config=_config(thread_id),
    )
    with pytest.raises(RuntimeError, match="stopped after the Vault commit"):
        await resume_research_workflow(
            graph,
            thread_id=thread_id,
            action="confirm",
        )
    assert store.calls == 1
    assert tool.calls == 1
    return checkpointer, tool


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_persist_replay_reuses_an_exact_stable_bundle(
    tmp_path: Path,
    managed: bool,
) -> None:
    memory_id = "M-replay" if managed else None
    if memory_id is not None:
        MarkdownMemoryStore(tmp_path).create_memory("Replay", memory_id)
    checkpointer, tool = await _stop_after_commit(
        tmp_path,
        thread_id=f"research-replay-{managed}",
        memory_id=memory_id,
    )

    rebuilt_store = _CountingStore(tmp_path)
    rebuilt = build_research_workflow(
        _Policy(),
        [tool],
        rebuilt_store,
        checkpointer=checkpointer,
    )
    final = await rebuilt.ainvoke(
        None,
        config=_config(f"research-replay-{managed}"),
    )

    assert final["workflow_status"] == "completed"
    assert rebuilt_store.calls == 0
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_persist_replay_rejects_changed_content_without_overwriting(
    tmp_path: Path,
) -> None:
    changed_report: list[Path] = []

    def change_report(root: Path, report_path: str) -> None:
        path = root / report_path
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "The stable finding was verified.",
                "Obsidian edit must survive.",
            ),
            encoding="utf-8",
        )
        changed_report.append(path)

    thread_id = "research-content-conflict"
    checkpointer, tool = await _stop_after_commit(
        tmp_path,
        thread_id=thread_id,
        mutate=change_report,
    )
    assert len(changed_report) == 1
    changed = changed_report[0].read_text(encoding="utf-8")
    rebuilt_store = _CountingStore(tmp_path)
    rebuilt = build_research_workflow(
        _Policy(),
        [tool],
        rebuilt_store,
        checkpointer=checkpointer,
    )

    with pytest.raises(MemoryWriteConflictError, match="content does not match"):
        await rebuilt.ainvoke(None, config=_config(thread_id))

    assert rebuilt_store.calls == 0
    assert changed_report[0].read_text(encoding="utf-8") == changed


@pytest.mark.asyncio
async def test_persist_replay_rejects_managed_report_identity_conflict(
    tmp_path: Path,
) -> None:
    memory_id = "M-identity"
    MarkdownMemoryStore(tmp_path).create_memory("Identity", memory_id)

    def change_identity(root: Path, report_path: str) -> None:
        path = root / report_path
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'root_thread_id: "research-identity-conflict"',
                'root_thread_id: "different-thread"',
            ),
            encoding="utf-8",
        )

    thread_id = "research-identity-conflict"
    checkpointer, tool = await _stop_after_commit(
        tmp_path,
        thread_id=thread_id,
        memory_id=memory_id,
        mutate=change_identity,
    )
    rebuilt_store = _CountingStore(tmp_path)
    rebuilt = build_research_workflow(
        _Policy(),
        [tool],
        rebuilt_store,
        checkpointer=checkpointer,
    )

    with pytest.raises(MemoryWriteConflictError, match="identity does not match"):
        await rebuilt.ainvoke(None, config=_config(thread_id))

    assert rebuilt_store.calls == 0
