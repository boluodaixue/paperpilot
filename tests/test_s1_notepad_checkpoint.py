"""S1 acceptance tests for checkpoint-owned Notepad scratchpads."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from src.research import MarkdownMemoryStore
from src.research.agent_graph import (
    build_research_agent_graph,
    create_research_agent_state,
)
from src.research.models import AgentLimits, ExecutionIdentity, ResearchTask
from src.research.workflow import (
    build_research_workflow,
    create_research_workflow_state,
)
from src.tools import NotepadTool


def _tool_call(action: str, **arguments: Any) -> dict[str, Any]:
    return {
        "id": f"notepad-{action}",
        "type": "function",
        "function": {
            "name": "notepad",
            "arguments": json.dumps({"action": action, **arguments}),
        },
    }


def _final(content: str) -> dict[str, Any]:
    return {
        "content": json.dumps(
            {
                "status": "completed",
                "summary": content,
                "findings": [content],
                "unresolved": [],
            }
        ),
        "tool_calls": [],
    }


class _WriteThenReadPolicy:
    """Write one task-specific note, read it, and finish without external evidence."""

    def __call__(self, messages, *, tools=None):
        tool_messages = [message for message in messages if message["role"] == "tool"]
        if not tool_messages:
            task = json.loads(messages[1]["content"])
            return {
                "content": "",
                "tool_calls": [
                    _tool_call(
                        "write",
                        content=task["objective"],
                        category="strategy",
                    )
                ],
            }
        if len(tool_messages) == 1:
            return {"content": "", "tool_calls": [_tool_call("read")]}
        return _final(tool_messages[-1]["content"])


class _CountingNotepad(NotepadTool):
    def __init__(self, calls: Counter[str]) -> None:
        super().__init__()
        self.calls = calls

    async def execute(self, action: str, **kwargs) -> str:
        self.calls[action] += 1
        return await super().execute(action, **kwargs)


class _ResearchWorkflowNotepadPolicy(_WriteThenReadPolicy):
    """Add the outer brief response to the same homogeneous research policy."""

    def __init__(self, calls: Counter[str]) -> None:
        self.calls = calls

    def __call__(self, messages, *, tools=None):
        if "before research begins" in str(messages[0].get("content", "")):
            self.calls["alignment"] += 1
            return {
                "content": json.dumps(
                    {
                        "objective": "checkpoint the production notepad",
                        "scope": ["checkpoint recovery"],
                        "directions": ["record the recovery marker"],
                        "constraints": [],
                        "expected_output": "one short report",
                    }
                ),
                "tool_calls": [],
            }
        return super().__call__(messages, tools=tools)


def _identity(
    thread_id: str,
    *,
    parent_thread_id: str | None = None,
    root_thread_id: str | None = None,
) -> ExecutionIdentity:
    return ExecutionIdentity(
        thread_id=thread_id,
        parent_thread_id=parent_thread_id,
        root_thread_id=root_thread_id or thread_id,
        depth=0 if parent_thread_id is None else 1,
    )


def _state(objective: str, identity: ExecutionIdentity) -> dict[str, Any]:
    return create_research_agent_state(
        ResearchTask(
            task_id=f"task-{identity.thread_id}",
            objective=objective,
            require_evidence=False,
        ),
        identity,
        AgentLimits(max_iterations=5, max_tool_calls=4),
    )


def _config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


@pytest.mark.asyncio
async def test_bound_snapshots_are_context_local_and_preserve_instance_api() -> None:
    notepad = NotepadTool()
    schema_before = notepad.get_openai_tool_schema()
    await notepad.write("instance-only", category="conclusion")

    async def scoped(content: str) -> list[dict[str, Any]]:
        with notepad.bind_snapshot([]):
            await notepad.write(content, category="strategy")
            await asyncio.sleep(0)
            return notepad.to_dict()

    alpha, beta = await asyncio.gather(scoped("alpha-only"), scoped("beta-only"))

    assert [item["content"] for item in alpha] == ["alpha-only"]
    assert [item["content"] for item in beta] == ["beta-only"]
    assert [item["content"] for item in notepad.to_dict()] == ["instance-only"]
    assert notepad.get_openai_tool_schema() == schema_before


@pytest.mark.asyncio
async def test_agent_state_isolates_root_and_child_notepad_snapshots() -> None:
    notepad = NotepadTool()
    graph = build_research_agent_graph(
        _WriteThenReadPolicy(),
        [notepad],
        checkpointer=InMemorySaver(),
    )
    root = _identity("root-notepad")
    child = _identity(
        "root-notepad.child.one",
        parent_thread_id=root.thread_id,
        root_thread_id=root.thread_id,
    )

    root_result, child_result = await asyncio.gather(
        graph.ainvoke(_state("root-only", root), config=_config(root.thread_id)),
        graph.ainvoke(_state("child-only", child), config=_config(child.thread_id)),
    )

    assert [item["content"] for item in root_result["notepad_entries"]] == [
        "root-only"
    ]
    assert [item["content"] for item in child_result["notepad_entries"]] == [
        "child-only"
    ]
    assert "child-only" not in root_result["result"].summary
    assert "root-only" not in child_result["result"].summary
    assert notepad.to_dict() == []


@pytest.mark.asyncio
async def test_sqlite_rebuilt_graph_restores_notes_without_repeating_write(
    tmp_path: Path,
) -> None:
    database = tmp_path / "notepad-checkpoints.sqlite"
    thread_id = "sqlite-notepad-recovery"
    calls: Counter[str] = Counter()

    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        first_graph = build_research_agent_graph(
            _WriteThenReadPolicy(),
            [_CountingNotepad(calls)],
            checkpointer=saver,
        )
        stream = first_graph.astream(
            _state("survives-restart", _identity(thread_id)),
            config=_config(thread_id),
            stream_mode="updates",
        )
        async for update in stream:
            if "use_tools" in update:
                break
        await stream.aclose()

        paused = await first_graph.aget_state(_config(thread_id))
        assert paused.next == ("assess_completion",)
        assert [item["content"] for item in paused.values["notepad_entries"]] == [
            "survives-restart"
        ]
        assert calls == Counter(write=1)

    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        rebuilt_graph = build_research_agent_graph(
            _WriteThenReadPolicy(),
            [_CountingNotepad(calls)],
            checkpointer=saver,
        )
        final = await rebuilt_graph.ainvoke(None, config=_config(thread_id))

    assert [item["content"] for item in final["notepad_entries"]] == [
        "survives-restart"
    ]
    assert "survives-restart" in final["result"].summary
    assert calls == Counter(write=1, read=1)


@pytest.mark.asyncio
async def test_research_workflow_parent_checkpoint_restores_nested_notepad(
    tmp_path: Path,
) -> None:
    """The production outer Workflow must own the nested Agent scratchpad field."""
    database = tmp_path / "research-workflow-notepad.sqlite"
    vault = tmp_path / "Vault"
    thread_id = "research-workflow-notepad"
    calls: Counter[str] = Counter()
    policy = _ResearchWorkflowNotepadPolicy(calls)
    identity = _identity(thread_id)
    config = _config(thread_id)

    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        first_graph = build_research_workflow(
            policy,
            [_CountingNotepad(calls)],
            MarkdownMemoryStore(vault),
            checkpointer=saver,
        )
        paused = await first_graph.ainvoke(
            create_research_workflow_state(
                "Will the production Research checkpoint retain its scratchpad?",
                identity,
                AgentLimits(max_iterations=5, max_tool_calls=4),
            ),
            config=config,
        )
        assert paused["workflow_status"] == "waiting_confirmation"

        stream = first_graph.astream(
            Command(resume={"action": "confirm"}),
            config=config,
            stream_mode="updates",
            subgraphs=True,
        )
        stopped_after_notepad = False
        async for namespace, update in stream:
            if namespace and "use_tools" in update:
                stopped_after_notepad = True
                break
        await stream.aclose()
        assert stopped_after_notepad is True
        assert calls["write"] == 1

    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
        rebuilt_graph = build_research_workflow(
            policy,
            [_CountingNotepad(calls)],
            MarkdownMemoryStore(vault),
            checkpointer=saver,
        )
        final = await rebuilt_graph.ainvoke(None, config=config)
        checkpoint = await rebuilt_graph.aget_state(config)

    assert final["workflow_status"] == "completed"
    assert [entry["content"] for entry in final["notepad_entries"]] == [
        "checkpoint the production notepad"
    ]
    assert checkpoint.values["notepad_entries"] == final["notepad_entries"]
    assert calls["write"] == 1
