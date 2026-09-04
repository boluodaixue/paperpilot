"""S0 acceptance tests for FileReader evidence and failure isolation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.research.agent_graph import build_research_agent_graph, create_research_agent_state
from src.research.models import AgentLimits, ExecutionIdentity, ResearchResult, ResearchTask
from src.tools.file_reader import FileReaderError, FileReaderTool, ScopedFileRoot, file_reader_scope


def _tool_call(path: str) -> dict[str, Any]:
    return {
        "id": "call-file-reader",
        "type": "function",
        "function": {
            "name": "file_reader",
            "arguments": json.dumps({"root": "memory", "path": path}),
        },
    }


def _final() -> dict[str, Any]:
    return {
        "content": json.dumps(
            {
                "status": "completed",
                "summary": "The supplied file was inspected.",
                "findings": ["The supplied file contains grounded material."],
                "unresolved": [],
            }
        ),
        "tool_calls": [],
    }


class FilePolicy:
    def __init__(self, path: str) -> None:
        self.path = path
        self.offered_tool_names: list[list[str]] = []

    def __call__(self, messages, *, tools=None):
        self.offered_tool_names.append(
            [
                str(schema.get("function", {}).get("name", ""))
                for schema in (tools or [])
            ]
        )
        if messages[-1]["role"] == "tool":
            return _final()
        return {"content": "", "tool_calls": [_tool_call(self.path)]}


class SuccessfulFileTool:
    name = "file_reader"

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "read one authorized file",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "root": {"type": "string", "enum": ["memory", "upload"]},
                        "path": {"type": "string"},
                    },
                    "required": ["root", "path"],
                },
            },
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return dict(self.result)


class UnavailableDeniedFileTool(SuccessfulFileTool):
    def __init__(self) -> None:
        super().__init__({})

    def is_available(self) -> bool:
        return False

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        raise FileReaderError("file access is not authorized for this research task")


def _identity(thread_id: str) -> ExecutionIdentity:
    return ExecutionIdentity(
        thread_id=thread_id,
        parent_thread_id=None,
        root_thread_id=thread_id,
        depth=0,
    )


async def _run(policy: FilePolicy, tool: Any, thread_id: str) -> dict[str, Any]:
    identity = _identity(thread_id)
    graph = build_research_agent_graph(policy, [tool])
    return await graph.ainvoke(
        create_research_agent_state(
            ResearchTask("file-task", "Inspect the supplied file."),
            identity,
            AgentLimits(max_retries_per_action=1, max_total_retries=1),
        ),
        config={"configurable": {"thread_id": thread_id}},
    )


@pytest.mark.asyncio
async def test_file_evidence_uses_only_the_validated_result_path_and_content() -> None:
    forged_argument = "C:/private/host-secret.md"
    policy = FilePolicy(forged_argument)
    tool = SuccessfulFileTool(
        {
            "path": "memory/notes/N-approved.md",
            "format": "markdown",
            "content": "Approved scoped evidence.",
            "truncated": True,
        }
    )

    final = await _run(policy, tool, "s0-file-success")

    result = final["result"]
    assert isinstance(result, ResearchResult)
    assert len(result.evidence) == 1
    evidence = result.evidence[0]
    assert evidence.title == "memory/notes/N-approved.md"
    assert evidence.source_ref == "memory/notes/N-approved.md"
    assert evidence.locator == "memory/notes/N-approved.md"
    assert evidence.finding == "Approved scoped evidence."
    assert evidence.excerpt == "Approved scoped evidence."
    assert "truncated" in evidence.limitations.lower()
    assert forged_argument not in repr(evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_result_path",
    [
        "C:/outside/secret.md",
        "//server/share/secret.md",
        "../outside/secret.md",
        "memory/../outside/secret.md",
        "memory\\notes\\secret.md",
        "memory/notes/secret.md:stream",
        "other/notes/secret.md",
    ],
)
async def test_noncanonical_file_result_path_cannot_become_evidence(
    unsafe_result_path: str,
) -> None:
    policy = FilePolicy("memory/notes/requested.md")
    tool = SuccessfulFileTool(
        {
            "path": unsafe_result_path,
            "format": "markdown",
            "content": "must not become evidence",
            "truncated": False,
        }
    )

    final = await _run(policy, tool, "s0-unsafe-result")

    assert final["result"].evidence == ()


@pytest.mark.asyncio
async def test_unavailable_reader_is_unoffered_but_a_forged_call_is_denied_once() -> None:
    forged_argument = "C:/private/host-secret.md"
    policy = FilePolicy(forged_argument)
    tool = UnavailableDeniedFileTool()

    final = await _run(policy, tool, "s0-file-denied")

    assert "file_reader" not in policy.offered_tool_names[0]
    assert len(tool.calls) == 1
    assert final["result"].evidence == ()
    finished = [
        event for event in final["execution_events"] if event["kind"] == "tool_finished"
    ]
    assert len(finished) == 1
    assert finished[0]["tool"] == "file_reader"
    assert finished[0]["ok"] is False
    assert finished[0]["retries"] == 0
    tool_messages = [message for message in final["messages"] if message["role"] == "tool"]
    assert len(tool_messages) == 1
    assert "not authorized" in tool_messages[0]["content"]
    assert forged_argument not in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_graph_built_deny_all_exposes_reader_in_later_runtime_scope(
    tmp_path: Path,
) -> None:
    memory = tmp_path / "M-runtime"
    note = memory / "notes" / "N-approved.md"
    note.parent.mkdir(parents=True)
    note.write_text("Runtime-scoped evidence.", encoding="utf-8")
    policy = FilePolicy("notes/N-approved.md")
    tool = FileReaderTool()
    identity = _identity("s0-late-bound-scope")
    graph = build_research_agent_graph(policy, [tool])

    assert tool.is_available() is False
    with file_reader_scope({"memory": memory}):
        final = await graph.ainvoke(
            create_research_agent_state(
                ResearchTask("file-task", "Inspect the supplied file."),
                identity,
                AgentLimits(max_retries_per_action=1, max_total_retries=1),
            ),
            config={"configurable": {"thread_id": identity.thread_id}},
        )

    assert "file_reader" in policy.offered_tool_names[0]
    assert final["result"].evidence[0].source_ref == "memory/notes/N-approved.md"
    assert "Runtime-scoped evidence." in final["result"].evidence[0].finding
    assert tool.is_available() is False


@pytest.mark.asyncio
async def test_scoped_artifact_root_reads_only_its_execution_tree(tmp_path: Path) -> None:
    vault = tmp_path / "Vault"
    own = vault / "Artifacts" / "root-a"
    other = vault / "Artifacts" / "root-b"
    own.mkdir(parents=True)
    other.mkdir(parents=True)
    (own / "artifact-own.json").write_text('{"result":"own"}', encoding="utf-8")
    (other / "artifact-other.json").write_text('{"result":"other"}', encoding="utf-8")
    reader = FileReaderTool()

    with file_reader_scope({"artifact": ScopedFileRoot(vault, "Artifacts/root-a")}):
        result = await reader.execute(root="artifact", path="artifact-own.json")
        assert result["content"] == '{"result":"own"}'
        assert len(result["content_hash"]) == 64
        with pytest.raises(FileReaderError):
            await reader.execute(root="artifact", path="../root-b/artifact-other.json")
