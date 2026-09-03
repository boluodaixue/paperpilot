"""W1 Web acceptance tests for durable Memory pointers in Chat history."""
from __future__ import annotations

import json
import time
from typing import Any

import pytest
from tests._research_assessment import assessment_response
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

import web.server as server
from src.research.memory import MarkdownMemoryStore
from src.research.runtime import build_research_runtime


class FixedTool:
    name = "web_search"

    def get_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "fixed search",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def execute(self, **_kwargs: Any) -> dict[str, Any]:
        return {"results": [{
            "title": "W1 source",
            "url": "https://example.test/w1",
            "snippet": "A source-locatable W1 finding.",
        }]}


class FixedPolicy:
    def __call__(self, messages, *, tools=None):
        assessment = assessment_response(messages)
        if assessment is not None:
            return assessment
        system = str(messages[0].get("content", ""))
        if "before research begins" in system:
            return {"content": json.dumps({
                "objective": "Verify the selected Memory",
                "scope": ["W1 Web pointer"],
                "directions": ["fixed direction"],
                "constraints": ["cite locations"],
                "expected_output": "Evidence-backed Markdown report",
            }), "tool_calls": []}
        if messages[-1]["role"] == "tool" or tools == []:
            return {"content": json.dumps({
                "status": "completed",
                "summary": "Fixed W1 summary.",
                "findings": ["A source-locatable W1 finding."],
                "unresolved": [],
            }), "tool_calls": []}
        return {"content": "", "tool_calls": [{
            "id": "call-search",
            "type": "function",
            "function": {"name": "web_search", "arguments": "{}"},
        }]}


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    runtime = build_research_runtime(
        config={},
        policy=FixedPolicy(),
        tools=[FixedTool()],
        memory_store=MarkdownMemoryStore(tmp_path / "vault"),
        checkpointer=InMemorySaver(),
    )
    monkeypatch.setattr(server, "CHAT_DB_PATH", str(tmp_path / "chat.db"))
    server.get_chat_store._store = None
    server.get_research_runtime._runtime = runtime
    server._TASKS.clear()
    with TestClient(server.app) as client:
        yield client, runtime
    server._TASKS.clear()
    server.get_chat_store._store = None
    server.get_research_runtime._runtime = None


def _wait_result(client: TestClient, task_id: str) -> dict[str, Any]:
    for _ in range(100):
        response = client.get(f"/api/tasks/{task_id}/result")
        if response.status_code == 200:
            return response.json()
        time.sleep(0.02)
    raise AssertionError("research task did not finish")


def test_selected_memory_flows_to_chat_pointer_without_report_copy(web_client):
    client, runtime = web_client
    descriptor = runtime.create_memory("Web selected Memory", memory_id="M-web-pointer")

    aligned = client.post("/api/alignment", json={
        "session_id": "selected-memory",
        "memory_id": descriptor.memory_id,
        "message": "Research the W1 pointer",
    })
    assert aligned.status_code == 200
    assert aligned.json()["memory_id"] == descriptor.memory_id

    task_id = aligned.json()["task_id"]
    started = client.post("/api/research", json={
        "task_id": task_id,
        "session_id": "selected-memory",
    })
    assert started.status_code == 200
    assert started.json()["memory_id"] == descriptor.memory_id

    result = _wait_result(client, task_id)
    assert result["memory_id"] == descriptor.memory_id
    manifest = result["manifest"]
    assert manifest["report_path"].startswith(descriptor.relative_path)
    assert all(
        path.startswith(descriptor.relative_path)
        for path in manifest["evidence_paths"] + manifest["source_paths"]
    )

    raw = server.get_chat_store().get_messages("selected-memory")[-1]
    pointer = json.loads(raw["content"])
    assert raw["kind"] == "report"
    assert pointer["memory_id"] == descriptor.memory_id
    assert pointer["manifest"] == manifest
    assert pointer["research_status"] == result["research_status"]
    assert pointer["termination_reason"] == result["termination_reason"]
    assert pointer["output_status"] == result["output_status"]
    assert "report_md" not in pointer
    assert "Fixed W1 summary" not in raw["content"]

    expanded = client.get("/api/sessions/selected-memory/messages").json()[-1]
    assert expanded["memory_id"] == descriptor.memory_id
    assert expanded["manifest"] == manifest
    assert expanded["research_status"] == result["research_status"]
    assert expanded["termination_reason"] == result["termination_reason"]
    assert expanded["output_status"] == result["output_status"]
    assert "Fixed W1 summary" in expanded["content"]


def test_modify_rejects_explicit_memory_switch_but_allows_omission(web_client):
    client, runtime = web_client
    first = runtime.create_memory("First", memory_id="M-first")
    second = runtime.create_memory("Second", memory_id="M-second")
    aligned = client.post("/api/alignment", json={
        "session_id": "fixed-memory",
        "memory_id": first.memory_id,
        "message": "Initial scope",
    }).json()

    conflict = client.post("/api/alignment", json={
        "session_id": "fixed-memory",
        "task_id": aligned["task_id"],
        "memory_id": second.memory_id,
        "message": "Move to the other Memory",
    })
    assert conflict.status_code == 409
    assert server._TASKS[aligned["task_id"]].memory_id == first.memory_id

    revised = client.post("/api/alignment", json={
        "session_id": "fixed-memory",
        "task_id": aligned["task_id"],
        "message": "Keep the Memory and narrow the scope",
    })
    assert revised.status_code == 200
    assert revised.json()["memory_id"] == first.memory_id


def test_w6_web_entry_rejects_legacy_alignment_without_memory_id(web_client):
    client, _runtime = web_client
    aligned = client.post("/api/alignment", json={
        "session_id": "legacy-memory",
        "message": "Legacy research",
    })
    assert aligned.status_code == 400
    assert "managed Memory" in aligned.json()["detail"]
    assert server.get_chat_store().get_memory_binding("legacy-memory") is None
    assert server.get_chat_store().get_messages("legacy-memory") == []
    assert not server._TASKS
